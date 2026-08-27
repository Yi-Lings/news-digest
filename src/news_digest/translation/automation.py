"""Persistent per-article translation scheduling primitives.

The module stores only closed error codes and diagnostic identifiers. Exception text,
article bodies, provider endpoints, and raw responses never enter automation state.
"""

import datetime as dt
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from news_digest.models import Article, DailyEdition
from news_digest.storage import db
from news_digest.translation.client import TranslationError
from news_digest.translation.schema import InvalidTranslation
from news_digest.translation.service import Translator, translate_article_once

FailureStage = Literal[
    "waiting",
    "connect_provider",
    "waiting_model",
    "receiving_response",
    "schema_validation",
    "saving_translation",
    "waiting_build",
    "building",
]


@dataclass(frozen=True)
class TranslationFailure:
    error_code: str
    error_category: str
    provider_outcome: db.ProviderOutcome
    auto_retry: bool
    http_status: int | None


@dataclass(frozen=True)
class TranslationWorkClaim:
    task: db.TranslationTask | None
    is_probe: bool = False
    blocked_reason: str | None = None


@dataclass
class AutomationRunResult:
    considered: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    probes: int = 0
    cache_hits: int = 0


def classify_translation_failure(
    error: Exception,
    *,
    stage: FailureStage,
) -> TranslationFailure:
    del stage  # The persisted stage is supplied separately; classification remains content-free.
    if isinstance(error, InvalidTranslation):
        return TranslationFailure(
            "SCHEMA_VALIDATION_FAILED", "schema", "content_failure", True, None
        )
    if not isinstance(error, TranslationError):
        return TranslationFailure(
            "UNPARSEABLE_RESPONSE", "response_format", "content_failure", True, None
        )

    status = error.status
    if status == 401:
        return TranslationFailure("AUTH_401", "authentication", "configuration_failure", False, 401)
    if status == 403:
        return TranslationFailure(
            "UPSTREAM_ERROR", "provider_infrastructure", "provider_failure", False, 403
        )
    if error.category in {"configuration", "endpoint", "request"}:
        return TranslationFailure(
            "CONFIGURATION_INVALID", "configuration", "configuration_failure", False, status
        )
    if error.category == "rate_limit" and status == 429:
        return TranslationFailure(
            "RATE_LIMIT_429", "provider_infrastructure", "provider_failure", True, status
        )
    if (
        error.category in {"provider", "provider_permanent"}
        and status is not None
        and status >= 500
    ):
        return TranslationFailure(
            "PROVIDER_5XX", "provider_infrastructure", "provider_failure", True, status
        )
    if error.category in {"connection_timeout", "read_timeout", "total_timeout"}:
        return TranslationFailure(
            "REQUEST_TIMEOUT", "provider_infrastructure", "provider_failure", True, status
        )
    if error.category == "termination_unconfirmed":
        return TranslationFailure(
            "REQUEST_TIMEOUT", "provider_infrastructure", "provider_failure", False, status
        )
    if error.category == "request_cancelled":
        return TranslationFailure(
            "REQUEST_CANCELLED", "cancelled", "content_failure", True, status
        )
    if error.category in {"network", "tls"}:
        return TranslationFailure(
            "NETWORK_CONNECT_FAILED",
            "provider_infrastructure",
            "provider_failure",
            True,
            status,
        )
    if error.category == "empty_response":
        return TranslationFailure(
            "EMPTY_RESPONSE", "response_format", "content_failure", True, status
        )
    return TranslationFailure(
        "UNPARSEABLE_RESPONSE", "response_format", "content_failure", True, status
    )


def provider_concurrency_limit(
    circuit: db.ProviderCircuit | None,
    *,
    normal_limit: int,
) -> int:
    if type(normal_limit) is not int or normal_limit < 1:
        raise ValueError("normal_limit must be a positive integer")
    if circuit is None or not circuit.recovery_mode:
        return normal_limit
    return 1


def claim_translation_work(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    now: str,
    lease_seconds: int,
    manual_retry: bool = False,
    manual_probe: bool = False,
) -> TranslationWorkClaim:
    task = db.translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    circuit = db.get_provider_circuit(conn, task.provider_id)
    if circuit is None or circuit.state == "closed":
        claimed = db.claim_translation_task(
            conn,
            task_id,
            owner=owner,
            now=now,
            lease_seconds=lease_seconds,
            manual=manual_retry,
        )
        return TranslationWorkClaim(claimed)
    if circuit.state == "configuration_blocked" and not manual_probe:
        return TranslationWorkClaim(None, blocked_reason="CONFIGURATION_INVALID")
    if circuit.state == "half_open":
        return TranslationWorkClaim(None, blocked_reason="CIRCUIT_OPEN")

    probe_claimed = db.claim_provider_probe(
        conn,
        task.provider_id,
        task_id=task_id,
        owner=owner,
        now=now,
        lease_seconds=lease_seconds,
        manual=manual_probe,
    )
    if not probe_claimed:
        return TranslationWorkClaim(None, blocked_reason="CIRCUIT_OPEN")
    claimed = db.claim_translation_task(
        conn,
        task_id,
        owner=owner,
        now=now,
        lease_seconds=lease_seconds,
        manual=manual_retry or manual_probe,
        probe=True,
    )
    if claimed is None:
        db.release_provider_probe(conn, task.provider_id, owner=owner, now=now)
        return TranslationWorkClaim(None, blocked_reason="TASK_LOCKED")
    return TranslationWorkClaim(claimed, is_probe=True)


def fail_translation_work(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    now: str,
    error: Exception,
    stage: FailureStage,
) -> db.TranslationTask:
    task = db.translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    failure = classify_translation_failure(error, stage=stage)
    diagnostic_id = secrets.token_hex(8)
    failed = db.finish_translation_task_failure(
        conn,
        task_id,
        owner=owner,
        now=now,
        error_code=failure.error_code,
        error_category=failure.error_category,
        failure_stage=stage,
        diagnostic_id=diagnostic_id,
        http_status=failure.http_status,
        auto_retry=failure.auto_retry,
    )
    circuit = db.get_provider_circuit(conn, task.provider_id)
    if circuit is not None and circuit.state == "half_open" and circuit.probe_owner == owner:
        db.finish_provider_probe(
            conn,
            task.provider_id,
            owner=owner,
            outcome=failure.provider_outcome,
            now=now,
        )
    else:
        db.record_provider_outcome(
            conn,
            task.provider_id,
            outcome=failure.provider_outcome,
            now=now,
        )
    return failed


def succeed_translation_work(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    now: str,
) -> db.TranslationTask:
    task = db.translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    succeeded = db.finish_translation_task_success(conn, task_id, owner=owner, now=now)
    circuit = db.get_provider_circuit(conn, task.provider_id)
    if circuit is not None and circuit.state == "half_open" and circuit.probe_owner == owner:
        db.finish_provider_probe(conn, task.provider_id, owner=owner, outcome="success", now=now)
    else:
        db.record_provider_outcome(conn, task.provider_id, outcome="success", now=now)
    return succeeded


class TranslationAutomationRunner:
    """Synchronous worker core; callers decide how often to invoke it."""

    def __init__(
        self,
        *,
        database: Path,
        provider_id: str,
        translator: Translator,
        cache_dir: Path,
        build_callback: Callable[[str], str],
        delivery_callback: Callable[[str, str], bool],
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.database = database
        self.provider_id = provider_id
        self.translator = translator
        self.cache_dir = cache_dir
        self.build_callback = build_callback
        self.delivery_callback = delivery_callback
        self.clock = clock

    @staticmethod
    def _iso(now: dt.datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(dt.UTC).isoformat()

    def _completion_timestamp(self, fallback: dt.datetime) -> str:
        return self._iso(self.clock() if self.clock is not None else fallback)

    def _cancel_requested(self, task_id: str) -> bool:
        conn = db.connect(self.database)
        try:
            task = db.translation_task(conn, task_id)
            return task is not None and task.cancel_requested_at is not None
        finally:
            conn.close()

    def seed_edition(self, edition: DailyEdition, *, now) -> None:
        timestamp = self._iso(now)
        conn = db.connect(self.database)
        try:
            db.upsert_articles(conn, edition.date, edition.articles)
            db.upsert_briefs(conn, edition.date, edition.briefs)
            db.ensure_automation_edition(
                conn, edition.date, target_count=len(edition.articles), now=timestamp
            )
            for article in edition.articles:
                db.ensure_translation_task(
                    conn,
                    edition_date=edition.date,
                    article_id=article.url,
                    article_title=article.title_en,
                    provider_id=self.provider_id,
                    now=timestamp,
                )
        finally:
            conn.close()

    @staticmethod
    def _article_for_task(conn: sqlite3.Connection, task: db.TranslationTask) -> Article:
        edition = db.get_edition(conn, task.edition_date)
        if edition is not None:
            for article in edition.articles:
                if article.url == task.article_id:
                    return article
        raise RuntimeError("translation task article is missing")

    def run_ready(
        self,
        *,
        now,
        owner: str,
        max_tasks: int = 1,
        lease_seconds: int = 120,
    ) -> AutomationRunResult:
        if type(max_tasks) is not int or max_tasks < 1:
            raise ValueError("max_tasks must be a positive integer")
        timestamp = self._iso(now)
        conn = db.connect(self.database)
        try:
            candidates: list[db.TranslationTask] = []
            for edition_date in db.unfinished_automation_edition_dates(conn):
                candidates.extend(
                    db.list_ready_translation_tasks(
                        conn, edition_date, now=timestamp
                    )
                )
            circuit = db.get_provider_circuit(conn, self.provider_id)
            candidates.sort(
                key=lambda task: (
                    task.manual_probe_requested_at is None,
                    task.manual_retry_requested_at is None,
                    task.status == "pending",
                )
            )
            if circuit is not None and circuit.state == "open":
                candidates.sort(
                    key=lambda task: (
                        task.manual_probe_requested_at is None,
                        task.status == "pending",
                    )
                )
        finally:
            conn.close()

        result = AutomationRunResult()
        for candidate in candidates[:max_tasks]:
            result.considered += 1
            conn = db.connect(self.database)
            try:
                claim = claim_translation_work(
                    conn,
                    candidate.task_id,
                    owner=owner,
                    now=timestamp,
                    lease_seconds=lease_seconds,
                    manual_retry=candidate.manual_retry_requested_at is not None,
                    manual_probe=candidate.manual_probe_requested_at is not None,
                )
                if claim.task is None:
                    result.blocked += 1
                    continue
                article = self._article_for_task(conn, claim.task)
                result.claimed += 1
                result.probes += int(claim.is_probe)
            finally:
                conn.close()

            try:
                translated, cache_hit = translate_article_once(
                    article,
                    self.translator,
                    self.cache_dir,
                    cancel_requested=lambda task_id=candidate.task_id: self._cancel_requested(
                        task_id
                    ),
                )
            except Exception as error:
                completed_at = self._completion_timestamp(now)
                conn = db.connect(self.database)
                try:
                    if (
                        isinstance(error, TranslationError)
                        and error.category == "request_cancelled"
                        and error.termination_confirmed
                    ):
                        db.confirm_translation_task_cancelled(
                            conn,
                            candidate.task_id,
                            owner=owner,
                            now=completed_at,
                            request_terminated=True,
                        )
                    else:
                        fail_translation_work(
                            conn,
                            candidate.task_id,
                            owner=owner,
                            now=completed_at,
                            error=error,
                            stage="waiting_model",
                        )
                finally:
                    conn.close()
                result.failed += 1
                continue

            completed_at = self._completion_timestamp(now)
            conn = db.connect(self.database)
            try:
                db.upsert_articles(conn, candidate.edition_date, [translated])
                succeed_translation_work(
                    conn, candidate.task_id, owner=owner, now=completed_at
                )
                db.mark_translation_ready_for_build(
                    conn, candidate.task_id, now=completed_at
                )
            finally:
                conn.close()
            result.succeeded += 1
            result.cache_hits += int(cache_hit)
        return result

    def flush_build(
        self,
        *,
        now,
        owner: str,
        force: bool = False,
        lease_seconds: int = 300,
    ) -> bool:
        timestamp = self._iso(now)
        conn = db.connect(self.database)
        try:
            claimed = None
            for edition_date in db.pending_automation_build_dates(conn):
                claimed = db.claim_automation_build(
                    conn,
                    edition_date,
                    owner=owner,
                    now=timestamp,
                    lease_seconds=lease_seconds,
                    force=force,
                )
                if claimed is not None:
                    break
        finally:
            conn.close()
        if claimed is None:
            return False

        try:
            self.build_callback(claimed.edition_date)
        except Exception:
            completed_at = self._completion_timestamp(now)
            conn = db.connect(self.database)
            try:
                db.finish_automation_build(
                    conn,
                    claimed.edition_date,
                    owner=owner,
                    now=completed_at,
                    succeeded=False,
                )
            finally:
                conn.close()
            return False

        completed_at = self._completion_timestamp(now)
        conn = db.connect(self.database)
        try:
            db.finish_automation_build(
                conn,
                claimed.edition_date,
                owner=owner,
                now=completed_at,
                succeeded=True,
            )
        finally:
            conn.close()
        return True

    def flush_delivery(self, *, edition_date: str, now) -> bool:
        timestamp = self._iso(now)
        conn = db.connect(self.database)
        try:
            db.expire_automation_deliveries_before(
                conn, edition_date, now=timestamp
            )
            delivery_key = db.claim_automation_delivery(
                conn, edition_date, now=timestamp
            )
        finally:
            conn.close()
        if delivery_key is None:
            return False

        try:
            delivered = self.delivery_callback(edition_date, delivery_key)
        except Exception:
            delivered = False
        completed_at = self._completion_timestamp(now)
        conn = db.connect(self.database)
        try:
            db.finish_automation_delivery(
                conn,
                edition_date,
                delivery_key=delivery_key,
                now=completed_at,
                succeeded=bool(delivered),
            )
        finally:
            conn.close()
        return bool(delivered)
