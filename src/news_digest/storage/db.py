"""SQLite 存储:按日归档文章与快讯,payload 序列化为 JSON。全项目的 SQL 仅出现在本模块。"""

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from news_digest.models import (
    Article,
    BriefItem,
    DailyEdition,
    article_from_dict,
    article_to_dict,
)

SCHEMA_VERSION = 4

DeliveryStatus = Literal["pending", "sending", "sent", "failed", "unknown"]
ArchiveStatus = Literal["pending", "archived", "failed"]
DeliveryRunStatus = Literal["running", "completed", "partial", "failed", "skipped"]
SubscriptionStatus = Literal["pending", "active", "unsubscribed", "disabled"]
SubscriptionSource = Literal["public", "admin_test"]
SubscriptionTokenPurpose = Literal["confirm", "unsubscribe"]
TestAttemptStatus = Literal["running", "completed", "failed", "unknown"]
TestAttemptDisposition = Literal["started", "existing", "blocked"]
TestAttemptNextAction = Literal[
    "none",
    "retry_test",
    "wait_and_verify_delivery",
    "do_not_repeat_whole_test",
]

_TEST_ATTEMPT_ERROR_CATEGORIES = frozenset(
    {
        "archive_failed",
        "authentication",
        "configuration",
        "connection_refused",
        "dns",
        "network",
        "partial_refusal",
        "rate_limited",
        "recipient_inactive",
        "recipient_rejected",
        "sender_rejected",
        "service_error",
        "smtp_protocol",
        "starttls_unsupported",
        "timeout",
        "tls",
        "worker_interrupted",
    }
)
_TEST_ATTEMPT_ERROR_STAGES = frozenset(
    {
        "auth",
        "authentication",
        "configuration",
        "connect",
        "data_command",
        "data_final_response",
        "data_write",
        "dns",
        "ehlo",
        "mail",
        "multiple",
        "noop",
        "rcpt",
        "starttls",
        "tls",
        "unknown",
    }
)
_TEST_ATTEMPT_NEXT_ACTIONS = frozenset(
    {
        "none",
        "retry_test",
        "wait_and_verify_delivery",
        "do_not_repeat_whole_test",
    }
)


@dataclass(frozen=True)
class DeliveryState:
    """Per-edition recipient state, exposed only through a stable redacted key.

    A claimed row is ``sending`` before SMTP DATA. If SMTP may have accepted DATA but
    the local ``sent`` commit is not known to have completed, callers must set
    ``unknown``. Automatic retry APIs exclude both ``sent`` and ``unknown``; only an
    explicit operator decision may reset ``unknown``. This is at-most-once automation,
    not strict exactly-once delivery, so the SMTP-accept/local-commit crash window can
    still produce a duplicate after a manually confirmed retry.
    """

    edition_date: str
    recipient_key: str
    status: DeliveryStatus
    error_category: str | None
    updated_at: str
    attempt_count: int
    run_id: str | None
    started_at: str | None
    finished_at: str | None
    degraded: bool


@dataclass(frozen=True)
class DeliveryRun:
    run_id: str
    edition_date: str
    mode: str
    status: DeliveryRunStatus
    started_at: str
    finished_at: str | None
    total_count: int
    sent_count: int
    failed_count: int
    unknown_count: int
    degraded: bool
    error_category: str | None


@dataclass(frozen=True)
class ArchiveState:
    """EML archive outcome, deliberately independent from SMTP recipient outcomes."""

    edition_date: str
    status: ArchiveStatus
    detail: str | None
    updated_at: str


@dataclass(frozen=True)
class DeliverySummary:
    pending: int = 0
    sending: int = 0
    sent: int = 0
    failed: int = 0
    unknown: int = 0
    legacy_sent_detail: str | None = None


@dataclass(frozen=True)
class SubscriptionState:
    """A deliverable subscription record; addresses are intentionally available only here."""

    id: int
    email: str
    status: SubscriptionStatus
    source: SubscriptionSource
    created_at: str
    updated_at: str
    confirmed_at: str | None
    unsubscribed_at: str | None


@dataclass(frozen=True)
class AdminSubscriptionState:
    """Admin-safe subscription view with no complete mailbox address."""

    id: int
    email_masked: str
    recipient_key: str
    status: SubscriptionStatus
    source: SubscriptionSource
    created_at: str
    updated_at: str
    confirmed_at: str | None
    unsubscribed_at: str | None


@dataclass(frozen=True)
class SubscriptionTokenState:
    purpose: SubscriptionTokenPurpose
    expires_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class TestAttempt:
    """Redacted, durable outcome of one Admin test-message request."""

    key_hash: str
    request_fingerprint: str
    edition_date: str
    status: TestAttemptStatus
    sent_count: int
    failed_count: int
    unknown_count: int
    skipped_count: int
    total_count: int
    error_category: str | None
    error_stage: str | None
    retry_allowed: bool
    next_action: TestAttemptNextAction
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BeginTestAttemptResult:
    disposition: TestAttemptDisposition
    attempt: TestAttempt


_TEST_ATTEMPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_test_attempts (
    key_hash TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    edition_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'unknown')),
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK(sent_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_count >= 0),
    unknown_count INTEGER NOT NULL DEFAULT 0 CHECK(unknown_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK(skipped_count >= 0),
    total_count INTEGER NOT NULL CHECK(total_count > 0),
    error_category TEXT CHECK(error_category IS NULL OR error_category IN (
        'archive_failed', 'authentication', 'configuration', 'connection_refused', 'dns',
        'network', 'partial_refusal', 'rate_limited', 'recipient_inactive',
        'recipient_rejected', 'sender_rejected', 'service_error', 'smtp_protocol',
        'starttls_unsupported', 'timeout', 'tls', 'worker_interrupted'
    )),
    error_stage TEXT CHECK(error_stage IS NULL OR error_stage IN (
        'auth', 'authentication', 'configuration', 'connect', 'data_command',
        'data_final_response', 'data_write', 'dns', 'ehlo', 'mail', 'noop', 'rcpt',
        'starttls', 'tls', 'unknown'
    )),
    retry_allowed INTEGER NOT NULL DEFAULT 0 CHECK(retry_allowed IN (0, 1)),
    next_action TEXT NOT NULL CHECK(next_action IN (
        'none', 'retry_test', 'wait_and_verify_delivery', 'do_not_repeat_whole_test'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_test_attempts_fingerprint_created
    ON email_test_attempts(request_fingerprint, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_email_test_attempts_unresolved_fingerprint
    ON email_test_attempts(request_fingerprint) WHERE status IN ('running', 'unknown');
"""


_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    slug TEXT NOT NULL,
    source TEXT NOT NULL,
    translated_by TEXT NOT NULL DEFAULT '',
    content_status TEXT NOT NULL,
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_date ON articles(date);
CREATE TABLE IF NOT EXISTS briefs (
    url TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_briefs_date ON briefs(date);
CREATE TABLE IF NOT EXISTS email_deliveries (
    edition_date TEXT NOT NULL,
    recipient_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    error_category TEXT,
    updated_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    started_at TEXT,
    finished_at TEXT,
    degraded INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0, 1)),
    PRIMARY KEY (edition_date, recipient_key)
);
CREATE INDEX IF NOT EXISTS idx_email_deliveries_date_status
    ON email_deliveries(edition_date, status);
CREATE TABLE IF NOT EXISTS email_delivery_runs (
    run_id TEXT PRIMARY KEY,
    edition_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'partial', 'failed', 'skipped')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    degraded INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0, 1)),
    error_category TEXT
);
CREATE INDEX IF NOT EXISTS idx_email_delivery_runs_started
    ON email_delivery_runs(started_at DESC);
CREATE TABLE IF NOT EXISTS email_archives (
    edition_date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('pending', 'archived', 'failed')),
    detail TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    email_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'unsubscribed', 'disabled')),
    source TEXT NOT NULL CHECK(source IN ('public', 'admin_test')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    unsubscribed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status);
CREATE TABLE IF NOT EXISTS subscription_tokens (
    token_digest TEXT PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK(purpose IN ('confirm', 'unsubscribe')),
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscription_tokens_subscription_purpose
    ON subscription_tokens(subscription_id, purpose);
"""
    + _TEST_ATTEMPT_SCHEMA
)


def _delivery_columns(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(email_deliveries)")}


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    columns = _delivery_columns(conn)
    additions = (
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("run_id", "TEXT"),
        ("started_at", "TEXT"),
        ("finished_at", "TEXT"),
        ("degraded", "INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0, 1))"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE email_deliveries ADD COLUMN {name} {definition}")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    conn.executescript(_TEST_ATTEMPT_SCHEMA)


def connect(path: Path) -> sqlite3.Connection:
    """打开数据库,父目录与表按需创建,并校验 schema 版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif row["value"] in {"1", "2", "3"} and SCHEMA_VERSION == 4:
        if row["value"] in {"1", "2"}:
            _migrate_to_v3(conn)
        _migrate_to_v4(conn)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif row["value"] != str(SCHEMA_VERSION):
        found = row["value"]
        conn.close()
        raise RuntimeError(f"schema 版本不匹配:库中为 {found},代码期望 {SCHEMA_VERSION},需迁移")
    return conn


def _validate_test_attempt_digest(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")


def _validate_test_attempt_date(value: str) -> None:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("edition_date must be an ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError("edition_date must be an ISO date")


def _validate_test_attempt_timestamp(value: str) -> None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("attempt timestamp must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise ValueError("attempt timestamp must include a timezone")


def _test_attempt(row: sqlite3.Row) -> TestAttempt:
    return TestAttempt(
        key_hash=row["key_hash"],
        request_fingerprint=row["request_fingerprint"],
        edition_date=row["edition_date"],
        status=row["status"],
        sent_count=row["sent_count"],
        failed_count=row["failed_count"],
        unknown_count=row["unknown_count"],
        skipped_count=row["skipped_count"],
        total_count=row["total_count"],
        error_category=row["error_category"],
        error_stage=row["error_stage"],
        retry_allowed=bool(row["retry_allowed"]),
        next_action=row["next_action"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _test_attempt_select() -> str:
    return (
        "SELECT key_hash, request_fingerprint, edition_date, status, sent_count, failed_count,"
        " unknown_count, skipped_count, total_count, error_category, error_stage, retry_allowed,"
        " next_action, created_at, updated_at FROM email_test_attempts"
    )


def test_attempt_by_key_hash(conn: sqlite3.Connection, key_hash: str) -> TestAttempt | None:
    _validate_test_attempt_digest(key_hash, "key_hash")
    row = conn.execute(_test_attempt_select() + " WHERE key_hash = ?", (key_hash,)).fetchone()
    return _test_attempt(row) if row else None


def test_attempts_by_fingerprint(
    conn: sqlite3.Connection, request_fingerprint: str
) -> list[TestAttempt]:
    _validate_test_attempt_digest(request_fingerprint, "request_fingerprint")
    rows = conn.execute(
        _test_attempt_select()
        + " WHERE request_fingerprint = ? ORDER BY created_at DESC, key_hash DESC",
        (request_fingerprint,),
    ).fetchall()
    return [_test_attempt(row) for row in rows]


def latest_test_attempt_by_fingerprint(
    conn: sqlite3.Connection, request_fingerprint: str
) -> TestAttempt | None:
    attempts = test_attempts_by_fingerprint(conn, request_fingerprint)
    return attempts[0] if attempts else None


def begin_test_attempt(
    conn: sqlite3.Connection,
    key_hash: str,
    request_fingerprint: str,
    edition_date: str,
    total_count: int,
    now: str,
) -> BeginTestAttemptResult:
    """Claim a test send, replay the same key, or block an unresolved equivalent request."""
    _validate_test_attempt_digest(key_hash, "key_hash")
    _validate_test_attempt_digest(request_fingerprint, "request_fingerprint")
    _validate_test_attempt_date(edition_date)
    _validate_test_attempt_timestamp(now)
    if type(total_count) is not int or total_count <= 0:
        raise ValueError("total_count must be a positive integer")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            _test_attempt_select() + " WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        if existing is not None:
            conn.commit()
            return BeginTestAttemptResult("existing", _test_attempt(existing))
        blocker = conn.execute(
            _test_attempt_select()
            + " WHERE request_fingerprint = ? AND status IN ('running', 'unknown')"
            " ORDER BY created_at DESC, key_hash DESC LIMIT 1",
            (request_fingerprint,),
        ).fetchone()
        if blocker is not None:
            conn.commit()
            return BeginTestAttemptResult("blocked", _test_attempt(blocker))
        conn.execute(
            "INSERT INTO email_test_attempts"
            " (key_hash, request_fingerprint, edition_date, status, total_count, retry_allowed,"
            " next_action, created_at, updated_at)"
            " VALUES (?, ?, ?, 'running', ?, 0, 'none', ?, ?)",
            (key_hash, request_fingerprint, edition_date, total_count, now, now),
        )
        created = conn.execute(
            _test_attempt_select() + " WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if created is None:
        raise RuntimeError("test attempt was not created")
    return BeginTestAttemptResult("started", _test_attempt(created))


def _validate_test_attempt_finish(
    status: TestAttemptStatus,
    *,
    sent_count: int,
    failed_count: int,
    unknown_count: int,
    skipped_count: int,
    total_count: int,
    error_category: str | None,
    error_stage: str | None,
    retry_allowed: bool,
    next_action: TestAttemptNextAction,
) -> None:
    if status not in {"completed", "failed", "unknown"}:
        raise ValueError("terminal test attempt status is invalid")
    counts = (sent_count, failed_count, unknown_count, skipped_count)
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("test attempt counts must be non-negative integers")
    if sum(counts) != total_count:
        raise ValueError("test attempt counts must sum to total_count")
    if error_category is not None and error_category not in _TEST_ATTEMPT_ERROR_CATEGORIES:
        raise ValueError("error_category is not a safe closed value")
    if error_stage is not None and error_stage not in _TEST_ATTEMPT_ERROR_STAGES:
        raise ValueError("error_stage is not a safe closed value")
    if type(retry_allowed) is not bool:
        raise ValueError("retry_allowed must be boolean")
    if next_action not in _TEST_ATTEMPT_NEXT_ACTIONS:
        raise ValueError("next_action is not a safe closed value")

    retry_expected = (
        total_count > 0
        and failed_count == total_count
        and sent_count == unknown_count == skipped_count == 0
    )
    if retry_allowed != retry_expected:
        raise ValueError("retry_allowed does not match deterministic failure counts")
    if unknown_count:
        expected_status = "unknown"
        expected_action = (
            "do_not_repeat_whole_test"
            if sent_count or failed_count or skipped_count
            else "wait_and_verify_delivery"
        )
    elif retry_expected:
        expected_status = "failed"
        expected_action = "retry_test"
    else:
        expected_status = "completed"
        expected_action = (
            "do_not_repeat_whole_test" if sent_count and (failed_count or skipped_count) else "none"
        )
    if status != expected_status:
        raise ValueError("status does not match test attempt counts")
    if next_action != expected_action:
        raise ValueError("next_action does not match test attempt counts")


def finish_test_attempt(
    conn: sqlite3.Connection,
    key_hash: str,
    status: Literal["completed", "failed", "unknown"],
    now: str,
    *,
    sent_count: int,
    failed_count: int,
    unknown_count: int,
    skipped_count: int,
    error_category: str | None,
    error_stage: str | None,
    retry_allowed: bool,
    next_action: TestAttemptNextAction,
) -> TestAttempt:
    """Persist one redacted terminal report; raw SMTP text is rejected by closed fields."""
    _validate_test_attempt_digest(key_hash, "key_hash")
    _validate_test_attempt_timestamp(now)
    row = conn.execute(
        "SELECT total_count FROM email_test_attempts WHERE key_hash = ?", (key_hash,)
    ).fetchone()
    if row is None:
        raise RuntimeError("test attempt does not exist")
    _validate_test_attempt_finish(
        status,
        sent_count=sent_count,
        failed_count=failed_count,
        unknown_count=unknown_count,
        skipped_count=skipped_count,
        total_count=row["total_count"],
        error_category=error_category,
        error_stage=error_stage,
        retry_allowed=retry_allowed,
        next_action=next_action,
    )
    with conn:
        cursor = conn.execute(
            "UPDATE email_test_attempts SET status = ?, sent_count = ?, failed_count = ?,"
            " unknown_count = ?, skipped_count = ?, error_category = ?, error_stage = ?,"
            " retry_allowed = ?, next_action = ?, updated_at = ?"
            " WHERE key_hash = ? AND status = 'running'",
            (
                status,
                sent_count,
                failed_count,
                unknown_count,
                skipped_count,
                error_category,
                error_stage,
                int(retry_allowed),
                next_action,
                now,
                key_hash,
            ),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("test attempt is not running")
    attempt = test_attempt_by_key_hash(conn, key_hash)
    if attempt is None:
        raise RuntimeError("test attempt does not exist")
    return attempt


def recover_interrupted_test_attempts(conn: sqlite3.Connection, now: str) -> int:
    """Conservatively quarantine all test attempts left running by a stopped server."""
    _validate_test_attempt_timestamp(now)
    with conn:
        cursor = conn.execute(
            "UPDATE email_test_attempts SET status = 'unknown', sent_count = 0, failed_count = 0,"
            " unknown_count = total_count, skipped_count = 0,"
            " error_category = 'worker_interrupted', error_stage = 'unknown', retry_allowed = 0,"
            " next_action = 'wait_and_verify_delivery', updated_at = ? WHERE status = 'running'",
            (now,),
        )
    return cursor.rowcount


def upsert_articles(conn: sqlite3.Connection, date: str, articles: list[Article]) -> None:
    """按 url 写入文章,单个事务提交。

    覆盖规则:库中已翻译(translated_by 非空)的行不被未翻译的新版本覆盖,
    避免重新抓取刷掉翻译成果;其余情况一律用新数据整体覆盖。
    """
    with conn:
        for article in articles:
            if not article.translated_by:
                row = conn.execute(
                    "SELECT translated_by FROM articles WHERE url = ?", (article.url,)
                ).fetchone()
                if row is not None and row["translated_by"]:
                    continue
            conn.execute(
                "INSERT OR REPLACE INTO articles"
                " (url, date, slug, source, translated_by, content_status, published_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    article.url,
                    date,
                    article.slug,
                    article.source,
                    article.translated_by,
                    article.content_status,
                    article.published_at,
                    json.dumps(article_to_dict(article), ensure_ascii=False),
                ),
            )


def upsert_briefs(conn: sqlite3.Connection, date: str, briefs: list[BriefItem]) -> None:
    """按 url 写入快讯,直接覆盖,单个事务提交。"""
    with conn:
        for brief in briefs:
            conn.execute(
                "INSERT OR REPLACE INTO briefs (url, date, payload) VALUES (?, ?, ?)",
                (brief.url, date, json.dumps(vars(brief), ensure_ascii=False)),
            )


def get_edition(conn: sqlite3.Connection, date: str) -> DailyEdition | None:
    """组装指定日期的日刊;该日期完全无数据时返回 None。"""
    article_rows = conn.execute(
        "SELECT payload FROM articles WHERE date = ? ORDER BY published_at DESC", (date,)
    ).fetchall()
    brief_rows = conn.execute(
        "SELECT payload FROM briefs WHERE date = ? ORDER BY url", (date,)
    ).fetchall()
    if not article_rows and not brief_rows:
        return None
    return DailyEdition(
        date=date,
        articles=[article_from_dict(json.loads(row["payload"])) for row in article_rows],
        briefs=[BriefItem(**json.loads(row["payload"])) for row in brief_rows],
    )


def _subscription_state(row: sqlite3.Row) -> SubscriptionState:
    return SubscriptionState(
        id=row["id"],
        email=row["email"],
        status=row["status"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        confirmed_at=row["confirmed_at"],
        unsubscribed_at=row["unsubscribed_at"],
    )


def subscription_by_email(conn: sqlite3.Connection, email: str) -> SubscriptionState | None:
    row = conn.execute(
        "SELECT id, email, status, source, created_at, updated_at, confirmed_at,"
        " unsubscribed_at FROM subscriptions WHERE email_key = ?",
        (delivery_recipient_key(email),),
    ).fetchone()
    return _subscription_state(row) if row else None


def admin_test_recipient_state(conn: sqlite3.Connection, email: str) -> SubscriptionState | None:
    """Legacy Admin lookup; source is audit metadata and does not limit management."""
    row = conn.execute(
        "SELECT id, email, status, source, created_at, updated_at, confirmed_at,"
        " unsubscribed_at FROM subscriptions WHERE email_key = ?",
        (delivery_recipient_key(email),),
    ).fetchone()
    return _subscription_state(row) if row else None


def begin_public_subscription(
    conn: sqlite3.Connection,
    email: str,
    now: str,
    confirm_digest: str,
    confirm_expires_at: str,
) -> bool:
    """Atomically start or refresh pending confirmation work.

    Returns whether the caller must send a confirmation message. Active/disabled rows and
    pending rows with a live token are intentional no-ops, keeping public responses
    indistinguishable while concurrent submissions produce only one usable token.
    """
    email_key = delivery_recipient_key(email)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, status FROM subscriptions WHERE email_key = ?", (email_key,)
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO subscriptions"
                " (email, email_key, status, source, created_at, updated_at)"
                " VALUES (?, ?, 'pending', 'public', ?, ?)",
                (email, email_key, now, now),
            )
            subscription_id = cursor.lastrowid
        elif row["status"] == "unsubscribed":
            subscription_id = row["id"]
            conn.execute(
                "UPDATE subscriptions SET email = ?, status = 'pending', source = 'public',"
                " updated_at = ?, confirmed_at = NULL, unsubscribed_at = NULL WHERE id = ?",
                (email, now, subscription_id),
            )
            conn.execute(
                "DELETE FROM subscription_tokens WHERE subscription_id = ?",
                (subscription_id,),
            )
        elif row["status"] == "pending":
            subscription_id = row["id"]
            live = conn.execute(
                "SELECT 1 FROM subscription_tokens WHERE subscription_id = ?"
                " AND purpose = 'confirm' AND consumed_at IS NULL AND expires_at >= ? LIMIT 1",
                (subscription_id, now),
            ).fetchone()
            if live:
                conn.commit()
                return False
            conn.execute(
                "DELETE FROM subscription_tokens WHERE subscription_id = ? AND purpose = 'confirm'",
                (subscription_id,),
            )
            conn.execute(
                "UPDATE subscriptions SET email = ?, updated_at = ? WHERE id = ?",
                (email, now, subscription_id),
            )
        else:
            conn.commit()
            return False

        conn.execute(
            "INSERT INTO subscription_tokens"
            " (token_digest, subscription_id, purpose, expires_at, consumed_at, created_at)"
            " VALUES (?, ?, 'confirm', ?, NULL, ?)",
            (confirm_digest, subscription_id, confirm_expires_at, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def abandon_confirmation_token(conn: sqlite3.Connection, token_digest: str) -> bool:
    """Remove one unsent confirmation token so a pending request can be retried safely."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM subscription_tokens"
            " WHERE token_digest = ? AND purpose = 'confirm' AND consumed_at IS NULL",
            (token_digest,),
        )
    return cursor.rowcount == 1


def consume_confirmation_token(conn: sqlite3.Connection, token_digest: str, now: str) -> bool:
    """Consume one live confirmation token exactly once."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT t.subscription_id, t.expires_at, t.consumed_at, s.status"
            " FROM subscription_tokens AS t"
            " JOIN subscriptions AS s ON s.id = t.subscription_id"
            " WHERE t.token_digest = ? AND t.purpose = 'confirm'",
            (token_digest,),
        ).fetchone()
        if (
            row is None
            or row["expires_at"] < now
            or row["consumed_at"] is not None
            or row["status"] != "pending"
        ):
            conn.commit()
            return False
        conn.execute(
            "UPDATE subscription_tokens SET consumed_at = ? WHERE token_digest = ?",
            (now, token_digest),
        )
        conn.execute(
            "UPDATE subscriptions SET status = 'active', updated_at = ?,"
            " confirmed_at = ?, unsubscribed_at = NULL WHERE id = ? AND status = 'pending'",
            (now, now, row["subscription_id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def issue_unsubscribe_token(
    conn: sqlite3.Connection,
    email: str,
    token_digest: str,
    expires_at: str,
    now: str,
) -> bool:
    """Store a recipient-specific unsubscribe token only for an active subscription."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM subscriptions WHERE email_key = ? AND status = 'active'",
            (delivery_recipient_key(email),),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO subscription_tokens"
            " (token_digest, subscription_id, purpose, expires_at, consumed_at, created_at)"
            " VALUES (?, ?, 'unsubscribe', ?, NULL, ?)",
            (token_digest, row["id"], expires_at, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def inspect_subscription_token(
    conn: sqlite3.Connection,
    token_digest: str,
    purpose: SubscriptionTokenPurpose,
    now: str,
) -> SubscriptionTokenState | None:
    """Validate a GET token without exposing or changing its subscription state."""
    row = conn.execute(
        "SELECT purpose, expires_at, consumed_at FROM subscription_tokens"
        " WHERE token_digest = ? AND purpose = ? AND expires_at >= ?",
        (token_digest, purpose, now),
    ).fetchone()
    return SubscriptionTokenState(**dict(row)) if row else None


def consume_unsubscribe_token(conn: sqlite3.Connection, token_digest: str, now: str) -> bool:
    """One-click unsubscribe, returning the same success for first and repeated live POSTs."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT t.subscription_id, t.expires_at, t.consumed_at"
            " FROM subscription_tokens AS t"
            " WHERE t.token_digest = ? AND t.purpose = 'unsubscribe'",
            (token_digest,),
        ).fetchone()
        if row is None or row["expires_at"] < now:
            conn.commit()
            return False
        if row["consumed_at"] is None:
            conn.execute(
                "UPDATE subscription_tokens SET consumed_at = ? WHERE token_digest = ?",
                (now, token_digest),
            )
        conn.execute(
            "UPDATE subscriptions SET status = 'unsubscribed', updated_at = ?,"
            " unsubscribed_at = ? WHERE id = ? AND status != 'unsubscribed'",
            (now, now, row["subscription_id"]),
        )
        conn.execute(
            "DELETE FROM subscription_tokens WHERE subscription_id = ? AND purpose = 'confirm'",
            (row["subscription_id"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def add_admin_test_recipient(conn: sqlite3.Connection, email: str, now: str) -> bool:
    """Add or reactivate one unique address while preserving its audit source."""
    email_key = delivery_recipient_key(email)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, status FROM subscriptions WHERE email_key = ?", (email_key,)
        ).fetchone()
        if row is not None:
            if row["status"] != "active":
                conn.execute(
                    "UPDATE subscriptions SET email = ?, status = 'active', updated_at = ?,"
                    " confirmed_at = COALESCE(confirmed_at, ?), unsubscribed_at = NULL"
                    " WHERE id = ?",
                    (email, now, now, row["id"]),
                )
                conn.execute(
                    "DELETE FROM subscription_tokens WHERE subscription_id = ?",
                    (row["id"],),
                )
            conn.commit()
            return True
        conn.execute(
            "INSERT INTO subscriptions"
            " (email, email_key, status, source, created_at, updated_at, confirmed_at)"
            " VALUES (?, ?, 'active', 'admin_test', ?, ?, ?)",
            (email, email_key, now, now, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def disable_admin_test_recipient(conn: sqlite3.Connection, email: str, now: str) -> bool:
    """Disable one address regardless of its audit source."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM subscriptions WHERE email_key = ? AND status != 'disabled'",
            (delivery_recipient_key(email),),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        cursor = conn.execute(
            "UPDATE subscriptions SET status = 'disabled', updated_at = ?"
            " WHERE id = ? AND status != 'disabled'",
            (now, row["id"]),
        )
        conn.execute(
            "DELETE FROM subscription_tokens WHERE subscription_id = ?",
            (row["id"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.rowcount == 1


def enable_subscription_id(
    conn: sqlite3.Connection, subscription_id: int, now: str
) -> bool:
    """Reactivate one disabled row regardless of its audit source."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE subscriptions SET status = 'active', updated_at = ?,"
            " confirmed_at = COALESCE(confirmed_at, ?), unsubscribed_at = NULL"
            " WHERE id = ? AND status = 'disabled'",
            (now, now, subscription_id),
        )
        if cursor.rowcount:
            conn.execute(
                "DELETE FROM subscription_tokens WHERE subscription_id = ?",
                (subscription_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.rowcount == 1


def disable_subscription_id(
    conn: sqlite3.Connection, subscription_id: int, now: str
) -> bool:
    """Disable one active row regardless of its audit source."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE subscriptions SET status = 'disabled', updated_at = ?"
            " WHERE id = ? AND status = 'active'",
            (now, subscription_id),
        )
        if cursor.rowcount:
            conn.execute(
                "DELETE FROM subscription_tokens WHERE subscription_id = ?",
                (subscription_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.rowcount == 1


def delete_subscription_id(conn: sqlite3.Connection, subscription_id: int) -> bool:
    """Delete one Admin-visible row; foreign-key cascade removes all of its tokens."""
    with conn:
        cursor = conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
    return cursor.rowcount == 1


def active_subscription_recipient_id(
    conn: sqlite3.Connection, subscription_id: int
) -> str | None:
    """Return the complete address only when the selected row is currently active."""
    row = conn.execute(
        "SELECT email FROM subscriptions WHERE id = ? AND status = 'active'",
        (subscription_id,),
    ).fetchone()
    return row["email"] if row else None


def disable_admin_test_recipient_id(
    conn: sqlite3.Connection, subscription_id: int, now: str
) -> bool:
    """Compatibility wrapper for the former Admin-test-only name."""
    return disable_subscription_id(conn, subscription_id, now)


def coordinated_delivery_recipients(
    conn: sqlite3.Connection, admin_recipients: tuple[str, ...]
) -> tuple[str, ...]:
    """Merge saved Admin recipients with active public subscriptions, honoring tombstones."""
    recipients: list[str] = []
    seen: set[str] = set()
    for address in admin_recipients:
        key = delivery_recipient_key(address)
        row = conn.execute(
            "SELECT status FROM subscriptions WHERE email_key = ?", (key,)
        ).fetchone()
        if row is not None and row["status"] != "active":
            continue
        if key not in seen:
            seen.add(key)
            recipients.append(address)
    rows = conn.execute(
        "SELECT email, email_key FROM subscriptions"
        " WHERE status = 'active' AND source = 'public' ORDER BY id"
    ).fetchall()
    for row in rows:
        if row["email_key"] not in seen:
            seen.add(row["email_key"])
            recipients.append(row["email"])
    return tuple(recipients)


def import_legacy_smtp_recipients_once(
    conn: sqlite3.Connection, recipients: tuple[str, ...], now: str
) -> bool:
    """Import the legacy SMTP list once; later calls never rewrite managed state."""
    marker = "subscriptions:legacy_smtp_recipients_imported"
    wanted = {delivery_recipient_key(address): address for address in recipients}
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM meta WHERE key = ?", (marker,)).fetchone():
            conn.commit()
            return False
        for email_key, address in wanted.items():
            row = conn.execute(
                "SELECT id, status FROM subscriptions WHERE email_key = ?", (email_key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO subscriptions"
                    " (email, email_key, status, source, created_at, updated_at, confirmed_at)"
                    " VALUES (?, ?, 'active', 'admin_test', ?, ?, ?)",
                    (address, email_key, now, now, now),
                )
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (marker, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def synchronize_admin_test_recipients(
    conn: sqlite3.Connection, admin_recipients: tuple[str, ...], now: str
) -> None:
    """Make the saved Admin list authoritative without overriding any existing tombstone."""
    wanted = {delivery_recipient_key(address): address for address in admin_recipients}
    try:
        conn.execute("BEGIN IMMEDIATE")
        active_rows = conn.execute(
            "SELECT email_key FROM subscriptions WHERE source = 'admin_test' AND status = 'active'"
        ).fetchall()
        for row in active_rows:
            if row["email_key"] not in wanted:
                conn.execute(
                    "UPDATE subscriptions SET status = 'disabled', updated_at = ?"
                    " WHERE email_key = ? AND source = 'admin_test' AND status = 'active'",
                    (now, row["email_key"]),
                )
        for email_key, address in wanted.items():
            existing = conn.execute(
                "SELECT status FROM subscriptions WHERE email_key = ?", (email_key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO subscriptions"
                    " (email, email_key, status, source, created_at, updated_at, confirmed_at)"
                    " VALUES (?, ?, 'active', 'admin_test', ?, ?, ?)",
                    (address, email_key, now, now, now),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def active_subscription_recipients(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return complete addresses only for actual delivery, never for Admin rendering."""
    rows = conn.execute(
        "SELECT email FROM subscriptions WHERE status = 'active' ORDER BY id"
    ).fetchall()
    return tuple(row["email"] for row in rows)


def eligible_delivery_recipients(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    retry_failed_only: bool = False,
) -> tuple[str, ...]:
    """Select active addresses eligible for automatic/manual delivery or failed-only retry.

    The status join is computed at query time, so an unsubscribe immediately excludes existing
    pending/failed rows. ``sent``, ``sending``, and ``unknown`` are never returned.
    """
    if retry_failed_only:
        rows = conn.execute(
            "SELECT s.email FROM subscriptions AS s"
            " JOIN email_deliveries AS d ON d.recipient_key = s.email_key"
            " WHERE s.status = 'active' AND d.edition_date = ? AND d.status = 'failed'"
            " ORDER BY s.id",
            (edition_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.email FROM subscriptions AS s"
            " LEFT JOIN email_deliveries AS d"
            " ON d.recipient_key = s.email_key AND d.edition_date = ?"
            " WHERE s.status = 'active' AND (d.status IS NULL OR d.status IN ('pending', 'failed'))"
            " ORDER BY s.id",
            (edition_date,),
        ).fetchall()
    return tuple(row["email"] for row in rows)


def unknown_delivery_recipients(conn: sqlite3.Connection, edition_date: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT s.email FROM subscriptions AS s"
        " JOIN email_deliveries AS d ON d.recipient_key = s.email_key"
        " WHERE s.status = 'active' AND d.edition_date = ? AND d.status = 'unknown'"
        " ORDER BY s.id",
        (edition_date,),
    ).fetchall()
    return tuple(row["email"] for row in rows)


def _mask_email(email: str) -> str:
    local, domain = email.rsplit("@", 1)
    domain_name, separator, suffix = domain.partition(".")
    masked_local = f"{local[:1]}***"
    masked_domain = f"{domain_name[:1]}***"
    return f"{masked_local}@{masked_domain}{separator}{suffix}"


def admin_subscription_states(conn: sqlite3.Connection) -> list[AdminSubscriptionState]:
    """Return an Admin-safe list; callers cannot accidentally serialize full addresses."""
    rows = conn.execute(
        "SELECT id, email, email_key, status, source, created_at, updated_at, confirmed_at,"
        " unsubscribed_at FROM subscriptions ORDER BY id"
    ).fetchall()
    return [
        AdminSubscriptionState(
            id=row["id"],
            email_masked=_mask_email(row["email"]),
            recipient_key=row["email_key"][:12],
            status=row["status"],
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            confirmed_at=row["confirmed_at"],
            unsubscribed_at=row["unsubscribed_at"],
        )
        for row in rows
    ]


def subscription_counts(conn: sqlite3.Connection) -> dict[SubscriptionStatus, int]:
    counts = {status: 0 for status in ("pending", "active", "unsubscribed", "disabled")}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM subscriptions GROUP BY status"
    ).fetchall()
    for row in rows:
        counts[row["status"]] = row["count"]
    return counts


def delivery_recipient_key(address: str) -> str:
    """Stable, case-insensitive, non-reversible recipient identity stored in SQLite."""
    return hashlib.sha256(address.strip().casefold().encode()).hexdigest()


def ensure_delivery_recipients(
    conn: sqlite3.Connection, edition_date: str, recipients: tuple[str, ...], now: str
) -> None:
    """Create pending rows without resetting existing sent/failed/unknown outcomes."""
    with conn:
        for address in recipients:
            conn.execute(
                "INSERT OR IGNORE INTO email_deliveries"
                " (edition_date, recipient_key, status, error_category, updated_at)"
                " VALUES (?, ?, 'pending', NULL, ?)",
                (edition_date, delivery_recipient_key(address), now),
            )


def recover_interrupted_deliveries(
    conn: sqlite3.Connection, now: str, *, stale_before: str | None = None
) -> int:
    """Quarantine stale ``sending`` rows after a crashed worker as possibly delivered."""
    cutoff = stale_before or now
    with conn:
        cursor = conn.execute(
            "UPDATE email_deliveries SET status = 'unknown',"
            " error_category = 'worker_interrupted', updated_at = ?, finished_at = ?"
            " WHERE status = 'sending' AND COALESCE(started_at, updated_at) < ?",
            (now, now, cutoff),
        )
    return cursor.rowcount


def claim_delivery(
    conn: sqlite3.Connection,
    edition_date: str,
    recipient: str,
    now: str,
    *,
    run_id: str | None = None,
    degraded: bool = False,
) -> bool:
    """Atomically claim only pending/failed work; sent/unknown/sending are never retried.

    ``BEGIN IMMEDIATE`` serializes competing writers. The status is committed as
    ``sending`` before callers enter SMTP DATA, making a second worker lose the claim.
    """
    recipient_key = delivery_recipient_key(recipient)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE email_deliveries"
            " SET status = 'sending', error_category = NULL, updated_at = ?,"
            " attempt_count = attempt_count + 1, run_id = ?, started_at = ?,"
            " finished_at = NULL, degraded = ?"
            " WHERE edition_date = ? AND recipient_key = ?"
            " AND status IN ('pending', 'failed')",
            (
                now,
                run_id,
                now,
                int(degraded),
                edition_date,
                recipient_key,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.rowcount == 1


def finish_delivery(
    conn: sqlite3.Connection,
    edition_date: str,
    recipient: str,
    status: Literal["sent", "failed", "unknown"],
    now: str,
    error_category: str | None = None,
) -> None:
    """Commit one claimed outcome.

    Use ``unknown`` whenever SMTP acceptance may have happened but a durable ``sent``
    record cannot be proven. Automatic claims intentionally exclude ``unknown``.
    """
    recipient_key = delivery_recipient_key(recipient)
    with conn:
        cursor = conn.execute(
            "UPDATE email_deliveries SET status = ?, error_category = ?, updated_at = ?,"
            " finished_at = ?"
            " WHERE edition_date = ? AND recipient_key = ? AND status = 'sending'",
            (status, error_category, now, now, edition_date, recipient_key),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("delivery row is not claimed")


def cancel_delivery_claim(
    conn: sqlite3.Connection,
    edition_date: str,
    recipient: str,
    run_id: str,
) -> None:
    """Remove a claim when the recipient becomes inactive before SMTP DATA."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM email_deliveries WHERE edition_date = ? AND recipient_key = ?"
            " AND status = 'sending' AND run_id = ?",
            (edition_date, delivery_recipient_key(recipient), run_id),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("delivery row is not claimed by this run")


def reset_unknown_deliveries(
    conn: sqlite3.Connection, edition_date: str, recipients: tuple[str, ...], now: str
) -> int:
    """Atomically reset an explicit, already-confirmed unknown recipient set."""
    keys = tuple(delivery_recipient_key(recipient) for recipient in recipients)
    if not keys:
        return 0
    placeholders = ",".join("?" for _ in keys)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE email_deliveries SET status = 'pending', error_category = NULL,"
            " updated_at = ?, finished_at = NULL"
            f" WHERE edition_date = ? AND status = 'unknown' AND recipient_key IN ({placeholders})",
            (now, edition_date, *keys),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.rowcount


def reset_unknown_delivery(
    conn: sqlite3.Connection, edition_date: str, recipient: str, now: str
) -> bool:
    """Explicit operator-confirmed reset; this may duplicate mail already accepted by SMTP."""
    return reset_unknown_deliveries(conn, edition_date, (recipient,), now) == 1


def delivery_states(conn: sqlite3.Connection, edition_date: str) -> list[DeliveryState]:
    rows = conn.execute(
        "SELECT edition_date, recipient_key, status, error_category, updated_at,"
        " attempt_count, run_id, started_at, finished_at, degraded"
        " FROM email_deliveries WHERE edition_date = ? ORDER BY recipient_key",
        (edition_date,),
    ).fetchall()
    return [
        DeliveryState(
            edition_date=row["edition_date"],
            recipient_key=row["recipient_key"],
            status=row["status"],
            error_category=row["error_category"],
            updated_at=row["updated_at"],
            attempt_count=row["attempt_count"],
            run_id=row["run_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            degraded=bool(row["degraded"]),
        )
        for row in rows
    ]


def delivery_summary(conn: sqlite3.Connection, edition_date: str) -> DeliverySummary:
    """Summarize structured state and expose legacy sent metadata without treating it as rows."""
    counts = {status: 0 for status in ("pending", "sending", "sent", "failed", "unknown")}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM email_deliveries"
        " WHERE edition_date = ? GROUP BY status",
        (edition_date,),
    ).fetchall()
    for row in rows:
        counts[row["status"]] = row["count"]
    return DeliverySummary(**counts, legacy_sent_detail=sent_detail(conn, edition_date))


def start_delivery_run(
    conn: sqlite3.Connection,
    run_id: str,
    edition_date: str,
    mode: str,
    started_at: str,
    total_count: int,
    degraded: bool,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO email_delivery_runs"
            " (run_id, edition_date, mode, status, started_at, total_count, degraded)"
            " VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (run_id, edition_date, mode, started_at, total_count, int(degraded)),
        )


def finish_delivery_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: DeliveryRunStatus,
    finished_at: str,
    *,
    sent_count: int = 0,
    failed_count: int = 0,
    unknown_count: int = 0,
    error_category: str | None = None,
) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE email_delivery_runs SET status = ?, finished_at = ?, sent_count = ?,"
            " failed_count = ?, unknown_count = ?, error_category = ? WHERE run_id = ?",
            (
                status,
                finished_at,
                sent_count,
                failed_count,
                unknown_count,
                error_category,
                run_id,
            ),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("delivery run does not exist")


def latest_delivery_run(conn: sqlite3.Connection, *, mode: str | None = None) -> DeliveryRun | None:
    query = (
        "SELECT run_id, edition_date, mode, status, started_at, finished_at, total_count,"
        " sent_count, failed_count, unknown_count, degraded, error_category"
        " FROM email_delivery_runs"
    )
    parameters: tuple[str, ...] = ()
    if mode is not None:
        query += " WHERE mode = ?"
        parameters = (mode,)
    row = conn.execute(
        query + " ORDER BY started_at DESC, run_id DESC LIMIT 1", parameters
    ).fetchone()
    if row is None:
        return None
    return DeliveryRun(
        run_id=row["run_id"],
        edition_date=row["edition_date"],
        mode=row["mode"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        total_count=row["total_count"],
        sent_count=row["sent_count"],
        failed_count=row["failed_count"],
        unknown_count=row["unknown_count"],
        degraded=bool(row["degraded"]),
        error_category=row["error_category"],
    )


def mark_archive(
    conn: sqlite3.Connection,
    edition_date: str,
    status: ArchiveStatus,
    now: str,
    detail: str | None = None,
) -> None:
    """Record EML archive outcome independently; ``detail`` must not contain secrets/addresses."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO email_archives"
            " (edition_date, status, detail, updated_at) VALUES (?, ?, ?, ?)",
            (edition_date, status, detail, now),
        )


def archive_state(conn: sqlite3.Connection, edition_date: str) -> ArchiveState | None:
    row = conn.execute(
        "SELECT edition_date, status, detail, updated_at FROM email_archives"
        " WHERE edition_date = ?",
        (edition_date,),
    ).fetchone()
    return ArchiveState(**dict(row)) if row else None


def mark_sent(conn: sqlite3.Connection, date: str, detail: str) -> None:
    """Legacy compatibility marker; new delivery orchestration uses email_deliveries rows."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"sent:{date}", detail),
        )


def sent_detail(conn: sqlite3.Connection, date: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"sent:{date}",)).fetchone()
    return row["value"] if row else None


def list_dates(conn: sqlite3.Connection) -> list[str]:
    """articles 与 briefs 两表出现过的日期并集,降序。"""
    rows = conn.execute(
        "SELECT date FROM articles UNION SELECT date FROM briefs ORDER BY date DESC"
    ).fetchall()
    return [row["date"] for row in rows]
