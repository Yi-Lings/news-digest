"""Unified, manifest-only publication preview and recipient delivery orchestration."""

from __future__ import annotations

import datetime as dt
import os
import uuid
import zoneinfo
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, get_args

from news_digest.config import (
    SmtpConfig,
    email_delivery_enabled_from_env,
    normalize_recipients,
    smtp_config_from_env,
)
from news_digest.delivery import subscriptions
from news_digest.delivery.email_content import EmailContentConfig, build_email_message
from news_digest.delivery.mailer import (
    ErrorStage,
    MailError,
    RecipientDeliveryResult,
    deliver,
    deliver_recipient,
    validate_smtp,
    write_eml,
)
from news_digest.delivery.publisher import PublishedRelease, resolve_published_release
from news_digest.rendering.email import RenderedEmail, render_email_preview
from news_digest.storage import db

DeliveryMode = Literal["auto", "manual", "retry_failed", "retry_unknown", "test"]
ReportErrorStage = ErrorStage | Literal["multiple", "unknown"]
_SAFE_ERROR_STAGES = frozenset(get_args(ErrorStage))


class DeliveryServiceError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class PublishedPreview:
    release: PublishedRelease
    rendered: RenderedEmail
    recipient_count: int
    recipient_hashes: tuple[str, ...]


@dataclass(frozen=True)
class DeliveryServiceReport:
    run_id: str | None
    release_name: str
    edition_date: str
    mode: DeliveryMode
    status: Literal["sent", "partial", "failed", "skipped", "preview"]
    total_count: int
    sent_count: int
    failed_count: int
    unknown_count: int
    skipped_count: int
    degraded: bool
    archive_status: Literal["not_requested", "archived", "failed"]
    error_category: str | None = None
    message: str = ""
    error_stage: ReportErrorStage | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in {"sent", "skipped", "preview"} and self.archive_status != "failed"

    @property
    def retry_allowed(self) -> bool:
        return (
            self.total_count > 0
            and self.failed_count == self.total_count
            and self.sent_count == 0
            and self.unknown_count == 0
            and self.skipped_count == 0
        )


def _utc_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(dt.UTC).isoformat(timespec="seconds")


def email_content_config_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    published_main_count: int = 6,
    published_brief_count: int = 5,
) -> EmailContentConfig:
    env = os.environ if environ is None else environ

    def enabled(name: str, default: bool) -> bool:
        raw = env.get(name)
        if raw is None:
            return default
        value = raw.strip().lower()
        if value not in {"true", "false"}:
            raise DeliveryServiceError("configuration", f"{name} 必须是 true 或 false")
        return value == "true"

    def count(name: str, default: int) -> int:
        try:
            value = int(env.get(name, str(default)))
        except ValueError as error:
            raise DeliveryServiceError("configuration", f"{name} 必须是非负整数") from error
        if value < 0:
            raise DeliveryServiceError("configuration", f"{name} 必须是非负整数")
        return value

    sources = tuple(
        value.strip() for value in env.get("EMAIL_SOURCE_FILTERS", "").split(",") if value.strip()
    )
    try:
        return EmailContentConfig(
            mains_enabled=enabled("EMAIL_MAINS_ENABLED", True),
            briefs_enabled=enabled("EMAIL_BRIEFS_ENABLED", True),
            main_limit=count("EMAIL_MAIN_LIMIT", min(6, published_main_count)),
            brief_limit=count("EMAIL_BRIEF_LIMIT", min(5, published_brief_count)),
            language=env.get("EMAIL_LANGUAGE", "bi").strip(),
            source_filters=sources,
            layout=env.get("EMAIL_LAYOUT", "digest").strip(),
            summary_length=env.get("EMAIL_SUMMARY_LENGTH", "standard").strip(),
        )
    except ValueError as error:
        raise DeliveryServiceError("configuration", str(error)) from error


def catchup_window_hours(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    try:
        hours = int(env.get("EMAIL_CATCHUP_WINDOW_HOURS", "6"))
    except ValueError as error:
        raise DeliveryServiceError(
            "configuration", "EMAIL_CATCHUP_WINDOW_HOURS 必须是 0 至 24 的整数"
        ) from error
    if not 0 <= hours <= 24:
        raise DeliveryServiceError(
            "configuration", "EMAIL_CATCHUP_WINDOW_HOURS 必须是 0 至 24 的整数"
        )
    return hours


def _load_release(output_root: Path, edition_date: str | None) -> PublishedRelease:
    try:
        return resolve_published_release(output_root, edition_date=edition_date)
    except ValueError as error:
        raise DeliveryServiceError("release", str(error)) from error


def _coordinated_recipients(conn, smtp: SmtpConfig, now: dt.datetime) -> tuple[str, ...]:
    saved = normalize_recipients(list(smtp.recipients))
    if saved:
        db.import_legacy_smtp_recipients_once(conn, saved, _utc_iso(now))
    return db.active_subscription_recipients(conn)


def _validate_auto_window(
    release: PublishedRelease,
    timezone: str,
    now: dt.datetime,
    window_hours: int,
    *,
    just_built_release_name: str | None,
) -> None:
    try:
        local_now = now.astimezone(zoneinfo.ZoneInfo(timezone))
    except zoneinfo.ZoneInfoNotFoundError as error:
        raise DeliveryServiceError("configuration", "NEWS_TIMEZONE 不是有效 IANA 时区") from error
    if release.release_date != local_now.date().isoformat():
        raise DeliveryServiceError("outside_window", "自动投递只允许 NEWS_TIMEZONE 当天刊期")
    if just_built_release_name is not None:
        if just_built_release_name != release.release_name:
            raise DeliveryServiceError("release", "刚构建 release 与 current manifest 不一致")
    scheduled = dt.datetime.combine(local_now.date(), dt.time(8, 0), tzinfo=local_now.tzinfo)
    deadline = scheduled + dt.timedelta(hours=window_hours)
    if local_now < scheduled:
        raise DeliveryServiceError("outside_window", "当前尚未到 08:00，自动投递已跳过")
    if local_now > deadline:
        raise DeliveryServiceError("outside_window", "已超过自动补跑窗口，旧刊不自动补发")


def preview_published(
    *,
    output_root: Path,
    database: Path,
    site_url: str,
    content_config: EmailContentConfig | None = None,
    smtp_config: SmtpConfig | None = None,
    edition_date: str | None = None,
    environ: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
    test: bool = False,
) -> PublishedPreview:
    """Preview one retained manifest without SMTP, state mutation, or DB edition lookup."""
    now = now or dt.datetime.now(dt.UTC)
    release = _load_release(output_root, edition_date)
    config = content_config or email_content_config_from_env(
        environ,
        published_main_count=len(release.edition.articles),
        published_brief_count=len(release.edition.briefs),
    )
    metadata = render_email_preview(
        release.edition,
        site_url,
        config,
        test=test,
        expected_date=release.release_date,
    )
    recipients: tuple[str, ...] = ()
    if smtp_config is not None:
        conn = db.connect(database)
        try:
            recipients = (
                db.active_subscription_recipients(conn)
                if test
                else db.paid_subscription_recipients(conn, _utc_iso(now))
            )
        finally:
            conn.close()
    message = build_email_message(
        release.edition,
        site_url,
        (
            smtp_config.sender
            if smtp_config is not None and smtp_config.sender.strip()
            else "preview@invalid.example"
        ),
        ("preview@invalid.example",),
        config,
        test=test,
        expected_date=release.release_date,
    )
    html_body = message.get_body(preferencelist=("html",))
    if html_body is None:
        raise DeliveryServiceError("message", "邮件 builder 未生成 text/html 正文")
    rendered = RenderedEmail(
        str(message["Subject"]),
        metadata.text,
        html_body.get_content(),
        metadata.metadata,
    )
    recipient_hashes = tuple(sorted(db.delivery_recipient_key(item) for item in recipients))
    return PublishedPreview(release, rendered, len(recipients), recipient_hashes)


def _recipient_selection(
    conn,
    mode: DeliveryMode,
    release_date: str,
    all_recipients: tuple[str, ...],
    now_iso: str,
) -> tuple[str, ...]:
    if mode == "test":
        return tuple(
            address
            for address in all_recipients
            if (state := db.subscription_by_email(conn, address)) is not None
            and state.status == "active"
        )
    if mode == "retry_failed":
        return db.eligible_delivery_recipients(
            conn, release_date, now_iso, retry_failed_only=True
        )
    if mode == "retry_unknown":
        recipients = db.unknown_delivery_recipients(conn, release_date, now_iso)
        if not recipients:
            return ()
        db.reset_unknown_deliveries(conn, release_date, recipients, now_iso)
        return recipients
    recipients = db.eligible_delivery_recipients(conn, release_date, now_iso)
    db.ensure_delivery_recipients(conn, release_date, recipients, now_iso)
    return recipients


def _outcome(results: list[RecipientDeliveryResult], skipped: int) -> str:
    if not results:
        return "skipped" if skipped else "failed"
    if all(result.status == "sent" for result in results):
        return "sent"
    if any(result.status == "sent" for result in results):
        return "partial"
    return "failed"


def _result_error_category(
    results: list[RecipientDeliveryResult], archive_error: str | None
) -> str | None:
    if archive_error is not None:
        return archive_error
    categories = {
        result.error_category
        for result in results
        if result.status in {"failed", "unknown"} and result.error_category
    }
    if not categories:
        return None
    return categories.pop() if len(categories) == 1 else "partial_refusal"


def _result_error_stage(results: list[RecipientDeliveryResult]) -> ReportErrorStage | None:
    relevant = [result for result in results if result.status in {"failed", "unknown"}]
    if not relevant:
        return None
    stages = {
        result.error_stage if result.error_stage in _SAFE_ERROR_STAGES else "unknown"
        for result in relevant
    }
    return stages.pop() if len(stages) == 1 else "multiple"


def _reconcile_completed_run_safely(
    conn,
    run_id: str,
    edition_date: str,
    now_iso: str,
) -> db.DeliveryReconciliationResult | Literal["failed"]:
    # SMTP outcomes are already durable here. A summary-sync failure must never
    # turn a successful send into a retryable delivery failure.
    try:
        return db.reconcile_completed_delivery_run(conn, run_id, edition_date, now_iso)
    except Exception:
        return "failed"


def deliver_published(
    mode: DeliveryMode,
    *,
    output_root: Path,
    database: Path,
    site_url: str,
    timezone: str,
    smtp_config: SmtpConfig | None = None,
    content_config: EmailContentConfig | None = None,
    edition_date: str | None = None,
    environ: Mapping[str, str] | None = None,
    now: dt.datetime | None = None,
    clock: Callable[[], dt.datetime] | None = None,
    just_built_release_name: str | None = None,
    confirm_unknown: bool = False,
    archive_dir: Path | None = Path("var/mail"),
    smtp_factory=None,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> DeliveryServiceReport:
    """Deliver one retained release through the only formal delivery state machine."""
    if mode not in {"auto", "manual", "retry_failed", "retry_unknown", "test"}:
        raise DeliveryServiceError("configuration", "未知投递模式")
    if mode == "retry_unknown" and not confirm_unknown:
        raise DeliveryServiceError("confirmation", "unknown 可能已送达，必须显式确认重复风险")
    clock = clock or (lambda: dt.datetime.now(dt.UTC))
    now = now or clock()
    now_iso = _utc_iso(now)
    release = _load_release(output_root, edition_date)
    if mode == "auto":
        enabled = (
            smtp_config.delivery_enabled
            if smtp_config is not None
            else email_delivery_enabled_from_env(environ)
        )
        if not enabled:
            return DeliveryServiceReport(
                None,
                release.release_name,
                release.release_date,
                mode,
                "skipped",
                0,
                0,
                0,
                0,
                0,
                False,
                "not_requested",
                message="邮件未启用，已跳过",
            )
    if mode == "auto":
        _validate_auto_window(
            release,
            timezone,
            now,
            catchup_window_hours(environ),
            just_built_release_name=just_built_release_name,
        )
    smtp = smtp_config or smtp_config_from_env(environ)
    if mode != "test" and not smtp.delivery_enabled:
        raise DeliveryServiceError("configuration", "邮件投递未启用")
    try:
        validate_smtp(
            smtp,
            require_recipients=False,
            resolver=resolver,
            validate_target=smtp_factory is None or resolver is not None,
        )
    except MailError as error:
        raise DeliveryServiceError("configuration", str(error)) from error
    config = content_config or email_content_config_from_env(
        environ,
        published_main_count=len(release.edition.articles),
        published_brief_count=len(release.edition.briefs),
    )

    conn = db.connect(database)
    run_id: str | None = None
    try:
        if mode != "test":
            stale_before = _utc_iso(now - dt.timedelta(minutes=10))
            db.recover_interrupted_deliveries(conn, now_iso, stale_before=stale_before)
            db.finalize_ineligible_deliveries(conn, release.release_date, now_iso)
        all_recipients = (
            normalize_recipients(list(smtp.recipients))
            if mode == "test"
            else _coordinated_recipients(conn, smtp, now)
        )
        if mode == "test" and len(all_recipients) != 1:
            raise DeliveryServiceError(
                "configuration", "测试邮件每次只能选择一个 active 订阅账号"
            )
        rendered = render_email_preview(
            release.edition,
            site_url,
            config,
            test=mode == "test",
            expected_date=release.release_date,
        )
        recipients = _recipient_selection(conn, mode, release.release_date, all_recipients, now_iso)
        if mode == "test" and not recipients:
            raise DeliveryServiceError("configuration", "所选订阅账号不是 active 状态")
        if mode != "test":
            run_id = uuid.uuid4().hex
            db.start_delivery_run(
                conn,
                run_id,
                release.release_date,
                mode,
                now_iso,
                len(recipients),
                rendered.metadata.degraded,
            )
            if not recipients:
                finished_at = _utc_iso(clock())
                db.finish_delivery_run(conn, run_id, "completed", finished_at)
                state_sync_result = _reconcile_completed_run_safely(
                    conn, run_id, release.release_date, finished_at
                )
                completion_ready = db.delivery_completion_ready(
                    conn, release.release_date, finished_at
                )
                state_sync_ok = state_sync_result == "reconciled" or (
                    state_sync_result == "not_applicable" and completion_ready
                )
                return DeliveryServiceReport(
                    run_id,
                    release.release_name,
                    release.release_date,
                    mode,
                    "failed" if mode == "auto" and not state_sync_ok else "skipped",
                    0,
                    0,
                    0,
                    0,
                    0,
                    rendered.metadata.degraded,
                    "not_requested",
                    error_category=None if state_sync_ok else "state_sync_failed",
                    message=(
                        "没有待投递收件人；已成功者不会重复发送"
                        if state_sync_ok
                        else "投递事实已持久化，但刊期状态同步失败；"
                        "禁止重发，请检查投递审计。"
                    ),
                )

        results: list[RecipientDeliveryResult] = []
        skipped = 0
        archive_message = None
        if archive_dir is not None and recipients:
            archive_message = build_email_message(
                release.edition,
                site_url,
                smtp.sender,
                ("archive@invalid.example",),
                config,
                test=mode == "test",
                expected_date=release.release_date,
            )
            del archive_message["To"]
        for recipient in recipients:
            if mode == "test":
                message = build_email_message(
                    release.edition,
                    site_url,
                    smtp.sender,
                    (recipient,),
                    config,
                    test=True,
                    expected_date=release.release_date,
                )
                report = deliver(
                    message,
                    replace(smtp, recipients=(recipient,)),
                    smtp_factory=smtp_factory,
                    resolver=resolver,
                )
                results.extend(report.results)
                continue
            prepared = subscriptions.prepare_unsubscribe(conn, recipient, site_url, now)
            if prepared is None:
                skipped += 1
                continue
            if not db.claim_delivery(
                conn,
                release.release_date,
                recipient,
                now_iso,
                run_id=run_id,
                degraded=rendered.metadata.degraded,
            ):
                skipped += 1
                continue
            message = build_email_message(
                release.edition,
                site_url,
                smtp.sender,
                (recipient,),
                config,
                test=mode == "test",
                expected_date=release.release_date,
                unsubscribe_url=prepared.url,
            )
            try:
                result = deliver_recipient(
                    message,
                    replace(smtp, recipients=()),
                    recipient,
                    unsubscribe_url=prepared.url,
                    smtp_factory=smtp_factory,
                    resolver=resolver,
                    pre_send_check=lambda recipient=recipient: (
                        db.paid_subscription_recipient_active(
                            conn, recipient, _utc_iso(clock())
                        )
                    ),
                )
            except BaseException:
                if mode != "test":
                    db.finish_delivery(
                        conn,
                        release.release_date,
                        recipient,
                        "unknown",
                        _utc_iso(dt.datetime.now(dt.UTC)),
                        "local_interruption",
                    )
                raise
            if result.status == "skipped":
                db.cancel_delivery_claim(
                    conn, release.release_date, recipient, run_id or ""
                )
                skipped += 1
                continue
            results.append(result)
            if mode != "test":
                db.finish_delivery(
                    conn,
                    release.release_date,
                    recipient,
                    result.status,
                    _utc_iso(clock()),
                    result.error_category,
                )

        sent_count = sum(result.status == "sent" for result in results)
        failed_count = sum(result.status == "failed" for result in results)
        unknown_count = sum(result.status == "unknown" for result in results)
        status = _outcome(results, skipped)
        archive_status: Literal["not_requested", "archived", "failed"] = "not_requested"
        archive_error = None
        if archive_dir is not None and archive_message is not None:
            archive_name = (
                f"{release.release_date}-test"
                if mode == "test"
                else f"{release.release_date}-{run_id}"
            )
            try:
                write_eml(archive_message, archive_dir, archive_name)
            except OSError:
                archive_status = "failed"
                archive_error = "archive_failed"
            else:
                archive_status = "archived"
        error_category = _result_error_category(results, archive_error)
        error_stage = _result_error_stage(results)
        state_sync_ok = True
        if mode != "test":
            finished_at = _utc_iso(clock())
            if archive_status == "failed":
                db.mark_archive(
                    conn, release.release_date, "failed", finished_at, "archive_failed"
                )
            elif archive_status == "archived":
                db.mark_archive(conn, release.release_date, "archived", finished_at)
            run_status = (
                "completed"
                if status in {"sent", "skipped"} and archive_status != "failed"
                else "partial"
                if status == "partial"
                else "failed"
            )
            db.finish_delivery_run(
                conn,
                run_id,
                run_status,
                finished_at,
                sent_count=sent_count,
                failed_count=failed_count,
                unknown_count=unknown_count,
                error_category=error_category,
            )
            if run_status == "completed":
                state_sync_result = _reconcile_completed_run_safely(
                    conn, run_id, release.release_date, finished_at
                )
                state_sync_ok = state_sync_result in {"reconciled", "not_applicable"}
        if archive_status == "failed":
            status = "failed"
        if not state_sync_ok:
            error_category = "state_sync_failed"
        return DeliveryServiceReport(
            run_id,
            release.release_name,
            release.release_date,
            mode,
            status,
            len(recipients),
            sent_count,
            failed_count,
            unknown_count,
            skipped,
            rendered.metadata.degraded,
            archive_status,
            error_category=error_category,
            message=(
                "投递事实已持久化，但刊期状态同步失败；"
                "禁止重发，请检查投递审计。"
                if not state_sync_ok
                else "投递完成"
                if status == "sent"
                else "投递未全部成功"
            ),
            error_stage=error_stage,
        )
    except Exception:
        if run_id is not None:
            try:
                latest = db.latest_delivery_run(conn)
                if latest is not None and latest.run_id == run_id and latest.status == "running":
                    db.finish_delivery_run(
                        conn,
                        run_id,
                        "failed",
                        _utc_iso(dt.datetime.now(dt.UTC)),
                        error_category="service_error",
                    )
            except Exception:
                pass
        raise
    finally:
        conn.close()
