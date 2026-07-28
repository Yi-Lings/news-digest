"""Unified manifest-only publication delivery tests; all SMTP behavior is fake/offline."""

import datetime as dt
import json
import smtplib
import sqlite3
from dataclasses import replace

import pytest

from news_digest.config import BuildConfig, SmtpConfig
from news_digest.delivery.delivery_service import (
    DeliveryServiceError,
    DeliveryServiceReport,
    deliver_published,
    preview_published,
)
from news_digest.delivery.email_content import build_email_message
from news_digest.delivery.mailer import DeliveryReport, RecipientDeliveryResult
from news_digest.delivery.publisher import resolve_published_release
from news_digest.models import Article, BriefItem, DailyEdition, Paragraph
from news_digest.pipeline import build_editions
from news_digest.storage import db

DATE = "2026-07-27"
SITE = "https://news.cheapcoding.top"
NOW = dt.datetime(2026, 7, 27, 1, 0, tzinfo=dt.UTC)  # 09:00 Asia/Shanghai


def _edition(date=DATE):
    return DailyEdition(
        date=date,
        articles=[
            Article(
                slug="published-story",
                source="BBC News",
                title_en="Published headline",
                title_zh="已发布标题",
                summary_en="Published summary.",
                summary_zh="已发布摘要。",
                author="Reporter",
                published_at=f"{date}T00:30:00+00:00",
                url="https://source.example.com/story",
                reading_minutes=3,
                paragraphs=[Paragraph(en="Body.", zh="正文。")],
                translated_by="model@p2",
            )
        ],
        briefs=[
            BriefItem(
                title_en="Published brief",
                title_zh="已发布简讯",
                source="NPR",
                url="https://source.example.com/brief",
            )
        ],
    )


def _published(tmp_path, date=DATE):
    root = tmp_path / "site"
    release = build_editions([_edition(date)], BuildConfig(root, "http://unused"))
    return root, release


def _smtp(recipients=("admin@example.com",), *, enabled=True):
    return SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="",
        password="",
        sender="news@example.com",
        recipients=recipients,
        delivery_enabled=enabled,
        security="starttls",
    )


class FakeSMTP:
    messages = []
    fail_for = set()
    unknown_for = set()
    on_ehlo = None

    def __init__(self, *args, **kwargs):
        self.debuglevel = 0
        self._data_reply_count = 0
        self._disconnect_after_data = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def ehlo(self):
        if type(self).on_ehlo is not None:
            type(self).on_ehlo()
        return 250, b"ok"

    def starttls(self, context=None):
        return 220, b"ready"

    def mail(self, *args, **kwargs):
        return 250, b"ok"

    def rcpt(self, *args, **kwargs):
        return 250, b"ok"

    def data(self, *args, **kwargs):
        return smtplib.SMTP.data(self, *args, **kwargs)

    def putcmd(self, *args, **kwargs):
        return None

    def getreply(self):
        self._data_reply_count += 1
        return (354, b"continue") if self._data_reply_count == 1 else (250, b"queued")

    def send(self, data):
        if self._disconnect_after_data:
            raise smtplib.SMTPServerDisconnected("during DATA write")
        return None

    def send_message(self, message):
        recipient = str(message["To"])
        type(self).messages.append(message)
        self._disconnect_after_data = recipient in self.unknown_for
        self.mail("news@example.com")
        self.rcpt(recipient)
        if recipient in self.fail_for:
            return {recipient: (550, b"rejected")}
        self.data(message.as_bytes())
        return {}


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.messages = []
    FakeSMTP.fail_for = set()
    FakeSMTP.unknown_for = set()
    FakeSMTP.on_ehlo = None


def _deliver(tmp_path, mode="manual", *, smtp=None, archive_dir=None, **kwargs):
    root, release = _published(tmp_path)
    smtp_config = smtp or _smtp()
    if mode == "test":
        conn = db.connect(tmp_path / "news.db")
        for recipient in smtp_config.recipients:
            db.add_admin_test_recipient(conn, recipient, NOW.isoformat())
        conn.close()
    report = deliver_published(
        mode,
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=smtp_config,
        now=NOW,
        archive_dir=archive_dir,
        smtp_factory=FakeSMTP,
        **kwargs,
    )
    return root, release, report


def test_build_writes_valid_self_contained_current_manifest(tmp_path):
    root, release = _published(tmp_path)
    manifest = json.loads((release / "release.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["release_name"] == release.name
    assert manifest["release_date"] == DATE
    assert manifest["edition"]["articles"][0]["title_en"] == "Published headline"
    loaded = resolve_published_release(root)
    assert loaded.release_name == release.name
    assert loaded.edition == _edition()


def test_preview_is_manifest_only_and_validates_date_links_and_damage(tmp_path):
    root, release = _published(tmp_path)
    conn = db.connect(tmp_path / "news.db")
    db.upsert_articles(conn, "2099-01-01", [replace(_edition().articles[0], title_en="DB latest")])
    conn.close()
    preview = preview_published(
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        edition_date=DATE,
    )
    assert "Published headline" in preview.rendered.text
    assert "DB latest" not in preview.rendered.text
    assert SITE not in preview.rendered.html
    assert "完整内容请访问 Cheapcoding News 官网" in preview.rendered.html
    built = build_email_message(
        _edition(), SITE, "news@example.com", ("preview@example.com",), expected_date=DATE
    )
    assert preview.rendered.subject == str(built["Subject"])
    assert preview.rendered.html == built.get_content()
    assert built.get_body(preferencelist=("html",)) is not None
    assert built.get_body(preferencelist=("plain",)) is None

    payload = json.loads((release / "release.json").read_text(encoding="utf-8"))
    payload["release_name"] = "../escape"
    (release / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DeliveryServiceError, match="identity|name"):
        preview_published(output_root=root, database=tmp_path / "news.db", site_url=SITE)


def test_preview_uses_builder_before_smtp_sender_is_configured(tmp_path):
    root, _ = _published(tmp_path)
    preview = preview_published(
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        smtp_config=SmtpConfig(
            host="",
            port=465,
            username="",
            password="",
            sender="",
            recipients=(),
            delivery_enabled=False,
            security="implicit_tls",
        ),
    )
    assert preview.rendered.subject == f"Cheapcoding News 已更新｜{DATE}"


def test_explicit_date_requires_retained_manifest_and_current_cannot_escape(tmp_path):
    root, _ = _published(tmp_path)
    with pytest.raises(DeliveryServiceError, match="retained"):
        preview_published(
            output_root=root,
            database=tmp_path / "news.db",
            site_url=SITE,
            edition_date="2026-07-26",
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    current = root / "current"
    if current.is_symlink() or current.is_junction():
        current.unlink()
    else:
        import shutil

        shutil.rmtree(current)
    try:
        current.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating an escaping symlink requires Windows developer mode")
    with pytest.raises(DeliveryServiceError, match="escapes"):
        preview_published(output_root=root, database=tmp_path / "news.db", site_url=SITE)


def test_disabled_skips_without_smtp_or_state(tmp_path):
    root, _, report = _deliver(tmp_path, "auto", smtp=_smtp(enabled=False))
    assert report.status == "skipped"
    assert report.message == "邮件未启用，已跳过"
    assert FakeSMTP.messages == []
    conn = db.connect(tmp_path / "news.db")
    assert db.latest_delivery_run(conn) is None
    conn.close()
    assert resolve_published_release(root).release_date == DATE


def test_latest_delivery_run_can_select_latest_scheduled_auto_run(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    db.start_delivery_run(conn, "auto-run", DATE, "auto", "2026-07-27T00:00:00+00:00", 1, False)
    db.finish_delivery_run(conn, "auto-run", "completed", "2026-07-27T00:01:00+00:00")
    db.start_delivery_run(
        conn, "manual-run", DATE, "manual", "2026-07-27T02:00:00+00:00", 1, False
    )
    db.finish_delivery_run(conn, "manual-run", "completed", "2026-07-27T02:01:00+00:00")

    assert db.latest_delivery_run(conn).run_id == "manual-run"
    assert db.latest_delivery_run(conn, mode="auto").run_id == "auto-run"
    conn.close()


def test_incomplete_config_and_invalid_site_url_fail_before_smtp(tmp_path):
    root, _ = _published(tmp_path)
    with pytest.raises(DeliveryServiceError, match="SMTP"):
        deliver_published(
            "manual",
            output_root=root,
            database=tmp_path / "news.db",
            site_url=SITE,
            timezone="Asia/Shanghai",
            smtp_config=replace(_smtp(), host=""),
            now=NOW,
            smtp_factory=FakeSMTP,
        )
    with pytest.raises(ValueError, match="NEWS_SITE_URL"):
        deliver_published(
            "manual",
            output_root=root,
            database=tmp_path / "news2.db",
            site_url="http://news.example.com",
            timezone="Asia/Shanghai",
            smtp_config=_smtp(),
            now=NOW,
            smtp_factory=FakeSMTP,
        )
    assert FakeSMTP.messages == []


def test_all_success_private_messages_have_body_and_header_unsubscribe(tmp_path):
    _, _, report = _deliver(
        tmp_path,
        smtp=_smtp(("one@example.com", "two@example.com")),
        archive_dir=tmp_path / "mail",
    )
    assert report.status == "sent" and report.sent_count == 2
    assert report.archive_status == "archived"
    conn = db.connect(tmp_path / "news.db")
    states = db.delivery_states(conn, DATE)
    assert all(state.attempt_count == 1 for state in states)
    assert all(state.run_id == report.run_id for state in states)
    assert all(state.started_at and state.finished_at for state in states)
    latest = db.latest_delivery_run(conn)
    assert latest is not None and latest.run_id == report.run_id
    assert latest.status == "completed" and latest.sent_count == 2
    assert latest.degraded is False
    conn.close()
    assert len(FakeSMTP.messages) == 2
    for message in FakeSMTP.messages:
        serialized = message.as_string()
        assert "List-Unsubscribe" in serialized
        assert "/unsubscribe/" in message.get_content()
        assert message.get_body(preferencelist=("html",)) is not None
        assert message.get_body(preferencelist=("plain",)) is None
        other = "two@example.com" if str(message["To"]) == "one@example.com" else "one@example.com"
        assert other not in serialized
    assert len(list((tmp_path / "mail").glob("*.eml"))) == 1
    archived = next((tmp_path / "mail").glob("*.eml")).read_text(
        encoding="utf-8", errors="ignore"
    )
    assert "one@example.com" not in archived
    assert "two@example.com" not in archived
    assert "/unsubscribe/" not in archived


def test_partial_translation_records_degraded_status(tmp_path):
    edition = _edition()
    edition.articles[0] = replace(
        edition.articles[0], title_zh="", summary_zh="", translated_by=""
    )
    root = tmp_path / "site"
    build_editions([edition], BuildConfig(root, "http://unused"))
    report = deliver_published(
        "manual",
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(),
        now=NOW,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert report.degraded is True
    conn = db.connect(tmp_path / "news.db")
    assert db.delivery_states(conn, DATE)[0].degraded is True
    assert db.latest_delivery_run(conn).degraded is True
    conn.close()


def test_partial_retry_failed_no_duplicate_and_unknown_requires_confirmation(tmp_path):
    smtp = _smtp(("ok@example.com", "failed@example.com", "unknown@example.com"))
    FakeSMTP.fail_for = {"failed@example.com"}
    FakeSMTP.unknown_for = {"unknown@example.com"}
    _, _, first = _deliver(tmp_path, smtp=smtp)
    assert (first.sent_count, first.failed_count, first.unknown_count) == (1, 1, 1)
    assert first.error_category == "partial_refusal"

    FakeSMTP.messages = []
    FakeSMTP.fail_for = set()
    FakeSMTP.unknown_for = set()
    root = tmp_path / "site"
    retry = deliver_published(
        "retry_failed",
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=smtp,
        now=NOW,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert retry.sent_count == 1
    assert [str(message["To"]) for message in FakeSMTP.messages] == ["failed@example.com"]

    with pytest.raises(DeliveryServiceError, match="显式确认"):
        deliver_published(
            "retry_unknown",
            output_root=root,
            database=tmp_path / "news.db",
            site_url=SITE,
            timezone="Asia/Shanghai",
            smtp_config=smtp,
            now=NOW,
            smtp_factory=FakeSMTP,
        )
    confirmed = deliver_published(
        "retry_unknown",
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=smtp,
        now=NOW,
        confirm_unknown=True,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert confirmed.sent_count == 1
    assert [str(message["To"]) for message in FakeSMTP.messages] == [
        "failed@example.com",
        "unknown@example.com",
    ]


@pytest.mark.parametrize(
    ("source_stage", "expected_stage"),
    [
        ("data_final_response", "data_final_response"),
        ("https://private.example.test/response", "unknown"),
    ],
)
def test_test_mode_aggregates_only_safe_error_stages(
    tmp_path, monkeypatch, source_stage, expected_stage
):
    def fail_delivery(*args, **kwargs):
        return DeliveryReport(
            (
                RecipientDeliveryResult(
                    "safe-recipient-ref",
                    "unknown",
                    "timeout",
                    delivery_uncertain=True,
                    accepted_possible=True,
                    error_stage=source_stage,
                ),
            )
        )

    monkeypatch.setattr("news_digest.delivery.delivery_service.deliver", fail_delivery)
    _, _, report = _deliver(tmp_path, mode="test")

    assert (report.sent_count, report.failed_count, report.unknown_count) == (0, 0, 1)
    assert report.error_category == "timeout"
    assert report.error_stage == expected_stage
    assert report.retry_allowed is False


@pytest.mark.parametrize(
    ("total", "sent", "failed", "unknown", "skipped", "expected"),
    [
        (1, 0, 1, 0, 0, True),
        (1, 1, 0, 0, 0, False),
        (2, 1, 1, 0, 0, False),
        (1, 0, 0, 1, 0, False),
        (1, 0, 0, 0, 1, False),
        (0, 0, 0, 0, 0, False),
    ],
)
def test_report_retry_is_allowed_only_when_every_recipient_failed(
    total, sent, failed, unknown, skipped, expected
):
    report = DeliveryServiceReport(
        run_id=None,
        release_name="2026-07-27-01",
        edition_date=DATE,
        mode="test",
        status="failed",
        total_count=total,
        sent_count=sent,
        failed_count=failed,
        unknown_count=unknown,
        skipped_count=skipped,
        degraded=False,
        archive_status="not_requested",
    )

    assert report.retry_allowed is expected


def test_test_mode_rejects_multiple_selected_subscriptions(tmp_path):
    with pytest.raises(DeliveryServiceError, match="每次只能选择一个"):
        _deliver(
            tmp_path,
            mode="test",
            smtp=_smtp(("first@example.com", "second@example.com")),
        )
    assert FakeSMTP.messages == []


def test_unsubscribed_tombstone_excludes_saved_admin_and_public_is_merged(tmp_path):
    root, _ = _published(tmp_path)
    database = tmp_path / "news.db"
    conn = db.connect(database)
    db.add_admin_test_recipient(conn, "blocked@example.com", NOW.isoformat())
    db.disable_admin_test_recipient(conn, "blocked@example.com", NOW.isoformat())
    db.add_admin_test_recipient(conn, "public@example.com", NOW.isoformat())
    conn.execute("UPDATE subscriptions SET source = 'public' WHERE email = 'public@example.com'")
    conn.commit()
    conn.close()

    report = deliver_published(
        "manual",
        output_root=root,
        database=database,
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(("blocked@example.com", "admin@example.com")),
        now=NOW,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert report.sent_count == 2
    assert {str(message["To"]) for message in FakeSMTP.messages} == {
        "admin@example.com",
        "public@example.com",
    }


def test_concurrent_claim_sends_only_once(tmp_path):
    root, _ = _published(tmp_path)
    database = tmp_path / "news.db"
    conn = db.connect(database)
    db.synchronize_admin_test_recipients(conn, ("reader@example.com",), NOW.isoformat())
    db.ensure_delivery_recipients(conn, DATE, ("reader@example.com",), NOW.isoformat())
    assert db.claim_delivery(conn, DATE, "reader@example.com", NOW.isoformat(), run_id="other")
    conn.close()
    report = deliver_published(
        "manual",
        output_root=root,
        database=database,
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(("reader@example.com",)),
        now=NOW,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert report.status == "skipped"
    assert FakeSMTP.messages == []


def test_unsubscribe_after_claim_is_rechecked_immediately_before_data(tmp_path):
    root, _ = _published(tmp_path)
    database = tmp_path / "news.db"

    def unsubscribe_during_smtp_setup():
        FakeSMTP.on_ehlo = None
        other = db.connect(database)
        with other:
            other.execute(
                "UPDATE subscriptions SET status='unsubscribed' WHERE email_key = ?",
                (db.delivery_recipient_key("reader@example.com"),),
            )
        other.close()

    FakeSMTP.on_ehlo = unsubscribe_during_smtp_setup
    report = deliver_published(
        "manual",
        output_root=root,
        database=database,
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(("reader@example.com",)),
        now=NOW,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert report.status == "skipped" and report.skipped_count == 1
    assert FakeSMTP.messages == []
    conn = db.connect(database)
    assert db.delivery_states(conn, DATE) == []
    conn.close()


def test_unsubscribe_after_rcpt_prevents_data_and_cancels_claim(tmp_path):
    root, _ = _published(tmp_path)
    database = tmp_path / "news.db"

    class RcptUnsubscribeSMTP(FakeSMTP):
        data_calls = 0

        def rcpt(self, *args, **kwargs):
            other = db.connect(database)
            with other:
                other.execute(
                    "UPDATE subscriptions SET status='unsubscribed' WHERE email_key = ?",
                    (db.delivery_recipient_key("reader@example.com"),),
                )
            other.close()
            return 250, b"ok"

        def data(self, *args, **kwargs):
            type(self).data_calls += 1
            return super().data(*args, **kwargs)

    report = deliver_published(
        "manual",
        output_root=root,
        database=database,
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(("reader@example.com",)),
        now=NOW,
        archive_dir=None,
        smtp_factory=RcptUnsubscribeSMTP,
    )
    assert report.status == "skipped" and report.skipped_count == 1
    assert RcptUnsubscribeSMTP.data_calls == 0
    conn = db.connect(database)
    assert db.delivery_states(conn, DATE) == []
    conn.close()


def test_archive_failure_fails_service_without_changing_sent(tmp_path, monkeypatch):
    root, _ = _published(tmp_path)

    def fail_archive(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("news_digest.delivery.delivery_service.write_eml", fail_archive)
    report = deliver_published(
        "manual",
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(),
        now=NOW,
        archive_dir=tmp_path / "mail",
        smtp_factory=FakeSMTP,
    )
    assert report.status == "failed" and report.archive_status == "failed"
    conn = db.connect(tmp_path / "news.db")
    assert db.delivery_summary(conn, DATE).sent == 1
    assert db.archive_state(conn, DATE).status == "failed"
    conn.close()


def test_auto_catchup_window_cannot_be_bypassed_by_just_built_release(tmp_path):
    root, release = _published(tmp_path)
    late = dt.datetime(2026, 7, 27, 15, 0, tzinfo=dt.UTC)  # 23:00 local
    with pytest.raises(DeliveryServiceError, match="补跑窗口"):
        deliver_published(
            "auto",
            output_root=root,
            database=tmp_path / "news.db",
            site_url=SITE,
            timezone="Asia/Shanghai",
            smtp_config=_smtp(),
            now=late,
            environ={"EMAIL_CATCHUP_WINDOW_HOURS": "6"},
            smtp_factory=FakeSMTP,
        )
    old_root, _ = _published(tmp_path / "old", "2026-07-26")
    with pytest.raises(DeliveryServiceError, match="当天刊期"):
        deliver_published(
            "auto",
            output_root=old_root,
            database=tmp_path / "old.db",
            site_url=SITE,
            timezone="Asia/Shanghai",
            smtp_config=_smtp(),
            now=NOW,
            smtp_factory=FakeSMTP,
        )
    with pytest.raises(DeliveryServiceError, match="补跑窗口"):
        deliver_published(
            "auto",
            output_root=root,
            database=tmp_path / "news2.db",
            site_url=SITE,
            timezone="Asia/Shanghai",
            smtp_config=_smtp(),
            now=late,
            just_built_release_name=release.name,
            archive_dir=None,
            smtp_factory=FakeSMTP,
        )
    with pytest.raises(DeliveryServiceError, match="0 至 24"):
        deliver_published(
            "auto",
            output_root=root,
            database=tmp_path / "news3.db",
            site_url=SITE,
            timezone="Asia/Shanghai",
            smtp_config=_smtp(),
            now=NOW,
            environ={"EMAIL_CATCHUP_WINDOW_HOURS": "25"},
            smtp_factory=FakeSMTP,
        )


def test_disabled_auto_delivery_ignores_invalid_legacy_smtp_fields(tmp_path):
    root, _ = _published(tmp_path)
    report = deliver_published(
        "auto",
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        environ={
            "EMAIL_DELIVERY_ENABLED": "false",
            "SMTP_PORT": "invalid",
            "SMTP_SECURITY": "starttls",
            "SMTP_USE_TLS": "true",
        },
        now=NOW,
        smtp_factory=FakeSMTP,
    )
    assert report.status == "skipped"
    assert FakeSMTP.messages == []


def test_test_mode_uses_only_saved_admin_and_does_not_write_formal_state(tmp_path):
    root, _ = _published(tmp_path)
    conn = db.connect(tmp_path / "news.db")
    db.add_admin_test_recipient(conn, "saved@example.com", NOW.isoformat())
    conn.close()
    report = deliver_published(
        "test",
        output_root=root,
        database=tmp_path / "news.db",
        site_url=SITE,
        timezone="Asia/Shanghai",
        smtp_config=_smtp(("saved@example.com",)),
        now=NOW,
        archive_dir=None,
        smtp_factory=FakeSMTP,
    )
    assert report.sent_count == 1
    assert str(FakeSMTP.messages[0]["Subject"]).startswith("[测试]")
    assert FakeSMTP.messages[0]["List-Unsubscribe"] is None
    conn = db.connect(tmp_path / "news.db")
    assert conn.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    assert db.latest_delivery_run(conn) is None
    conn.close()


def test_schema_v2_migrates_delivery_attempt_and_run_columns(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES ('schema_version', '2');"
        "CREATE TABLE articles (url TEXT PRIMARY KEY, date TEXT NOT NULL, slug TEXT NOT NULL,"
        " source TEXT NOT NULL, translated_by TEXT NOT NULL DEFAULT '',"
        " content_status TEXT NOT NULL,"
        " published_at TEXT NOT NULL, payload TEXT NOT NULL);"
        "CREATE TABLE briefs (url TEXT PRIMARY KEY, date TEXT NOT NULL, payload TEXT NOT NULL);"
        "CREATE TABLE email_deliveries ("
        "edition_date TEXT NOT NULL, recipient_key TEXT NOT NULL, status TEXT NOT NULL,"
        "error_category TEXT, updated_at TEXT NOT NULL,"
        "PRIMARY KEY (edition_date, recipient_key));"
        "INSERT INTO email_deliveries VALUES "
        "('2026-07-27', 'existing-key', 'sent', NULL, '2026-07-27T00:00:00+00:00');"
    )
    legacy.commit()
    legacy.close()
    conn = db.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(email_deliveries)")}
    assert {"attempt_count", "run_id", "started_at", "finished_at", "degraded"} <= columns
    row = conn.execute(
        "SELECT status, attempt_count, degraded FROM email_deliveries"
        " WHERE recipient_key = 'existing-key'"
    ).fetchone()
    assert tuple(row) == ("sent", 0, 0)
    assert db.latest_delivery_run(conn) is None
    conn.close()
