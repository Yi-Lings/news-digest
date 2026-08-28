"""SQLite 存储:按日归档文章与快讯,payload 序列化为 JSON。全项目的 SQL 仅出现在本模块。"""

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
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

SCHEMA_VERSION = 7

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
TranslationTaskStatus = Literal[
    "pending",
    "running",
    "failed",
    "retry_wait",
    "succeeded",
    "configuration_blocked",
    "cancelled",
]
TranslationBuildStatus = Literal["build_pending", "built", "online"]
TranslationAttemptKind = Literal["automatic", "manual", "probe"]
TranslationAttemptStatus = Literal["running", "succeeded", "failed", "cancelled"]
TranslationAdminActionType = Literal[
    "dispatch", "retry", "cancel", "probe", "unblock", "recover"
]
TranslationAdminActionStatus = Literal[
    "requested", "running", "completed", "rejected", "timed_out", "recovered"
]
ProviderCircuitState = Literal["closed", "open", "half_open", "configuration_blocked"]
ProviderOutcome = Literal[
    "success", "provider_failure", "content_failure", "configuration_failure"
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


@dataclass(frozen=True)
class TranslationTask:
    task_id: str
    edition_date: str
    article_id: str
    article_title: str
    provider_id: str
    status: TranslationTaskStatus
    build_status: TranslationBuildStatus
    attempt_count: int
    success_generation: int | None
    error_code: str | None
    error_category: str | None
    http_status: int | None
    current_stage: str
    failure_stage: str | None
    auto_retry: bool
    diagnostic_id: str | None
    failed_at: str | None
    next_retry_at: str | None
    started_at: str | None
    finished_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    hard_timeout_at: str | None
    cancel_requested_at: str | None
    manual_retry_requested_at: str | None
    manual_probe_requested_at: str | None
    manual_action_id: str | None
    received_chunks: int
    last_activity_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TranslationAttempt:
    id: int
    task_id: str
    attempt_number: int
    owner: str
    kind: TranslationAttemptKind
    status: TranslationAttemptStatus
    started_at: str
    finished_at: str | None
    error_code: str | None
    error_category: str | None
    failure_stage: str | None
    diagnostic_id: str | None


@dataclass(frozen=True)
class TranslationAdminAction:
    action_id: str
    task_id: str | None
    provider_id: str
    action: TranslationAdminActionType
    actor: str
    status: TranslationAdminActionStatus
    requested_at: str
    started_at: str | None
    finished_at: str | None
    result_code: str | None


@dataclass(frozen=True)
class ProviderCircuit:
    provider_id: str
    state: ProviderCircuitState
    consecutive_failures: int
    open_count: int
    recovery_successes: int
    recovery_mode: bool
    opened_at: str | None
    next_probe_at: str | None
    probe_task_id: str | None
    probe_owner: str | None
    probe_lease_expires_at: str | None
    last_outcome: str | None
    updated_at: str


@dataclass(frozen=True)
class AutomationEdition:
    edition_date: str
    status: str
    target_count: int
    succeeded_count: int
    online_count: int
    dirty_generation: int
    built_generation: int
    building_generation: int | None
    build_not_before: str | None
    build_owner: str | None
    build_lease_expires_at: str | None
    delivery_key: str | None
    delivery_started_at: str | None
    delivery_finished_at: str | None
    last_error_code: str | None
    created_at: str
    updated_at: str


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


_TRANSLATION_AUTOMATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS translation_tasks (
    task_id TEXT PRIMARY KEY,
    edition_date TEXT NOT NULL,
    article_id TEXT NOT NULL,
    article_title TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'failed', 'retry_wait', 'succeeded',
        'configuration_blocked', 'cancelled'
    )),
    build_status TEXT NOT NULL DEFAULT 'build_pending'
        CHECK(build_status IN ('build_pending', 'built', 'online')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    success_generation INTEGER CHECK(success_generation IS NULL OR success_generation > 0),
    error_code TEXT,
    error_category TEXT,
    http_status INTEGER,
    current_stage TEXT NOT NULL DEFAULT 'waiting' CHECK(current_stage IN (
        'waiting', 'connect_provider', 'waiting_model', 'receiving_response',
        'schema_validation', 'saving_translation', 'waiting_build', 'building', 'online'
    )),
    failure_stage TEXT,
    auto_retry INTEGER NOT NULL DEFAULT 1 CHECK(auto_retry IN (0, 1)),
    diagnostic_id TEXT,
    failed_at TEXT,
    next_retry_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    hard_timeout_at TEXT,
    cancel_requested_at TEXT,
    manual_retry_requested_at TEXT,
    manual_probe_requested_at TEXT,
    manual_action_id TEXT,
    received_chunks INTEGER NOT NULL DEFAULT 0 CHECK(received_chunks >= 0),
    last_activity_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (edition_date, article_id, provider_id),
    CHECK(status = 'running' OR (lease_owner IS NULL AND lease_expires_at IS NULL)),
    CHECK(status != 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK(status != 'running' OR next_retry_at IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_translation_tasks_edition_status
    ON translation_tasks(edition_date, status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_translation_tasks_provider_status
    ON translation_tasks(provider_id, status, next_retry_at);

CREATE TABLE IF NOT EXISTS translation_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES translation_tasks(task_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
    owner TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('automatic', 'manual', 'probe')),
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    error_category TEXT,
    failure_stage TEXT,
    diagnostic_id TEXT,
    UNIQUE (task_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_translation_attempts_task_started
    ON translation_attempts(task_id, started_at DESC);

CREATE TABLE IF NOT EXISTS translation_admin_actions (
    action_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES translation_tasks(task_id) ON DELETE SET NULL,
    provider_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN (
        'dispatch', 'retry', 'cancel', 'probe', 'unblock', 'recover'
    )),
    actor TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'requested', 'running', 'completed', 'rejected', 'timed_out', 'recovered'
    )),
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    result_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_translation_admin_actions_requested
    ON translation_admin_actions(requested_at DESC);

CREATE TABLE IF NOT EXISTS provider_circuits (
    provider_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN (
        'closed', 'open', 'half_open', 'configuration_blocked'
    )),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures >= 0),
    open_count INTEGER NOT NULL DEFAULT 0 CHECK(open_count >= 0),
    recovery_successes INTEGER NOT NULL DEFAULT 0 CHECK(recovery_successes >= 0),
    recovery_mode INTEGER NOT NULL DEFAULT 0 CHECK(recovery_mode IN (0, 1)),
    opened_at TEXT,
    next_probe_at TEXT,
    probe_task_id TEXT REFERENCES translation_tasks(task_id),
    probe_owner TEXT,
    probe_lease_expires_at TEXT,
    last_outcome TEXT,
    updated_at TEXT NOT NULL,
    CHECK(state = 'half_open' OR (
        probe_task_id IS NULL AND probe_owner IS NULL AND probe_lease_expires_at IS NULL
    )),
    CHECK(state != 'half_open' OR (
        probe_task_id IS NOT NULL AND probe_owner IS NOT NULL AND probe_lease_expires_at IS NOT NULL
    ))
);

CREATE TABLE IF NOT EXISTS automation_editions (
    edition_date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN (
        'translating', 'build_pending', 'building', 'partial', 'complete',
        'build_failed', 'delivery_pending', 'delivered'
    )),
    target_count INTEGER NOT NULL CHECK(target_count >= 0),
    succeeded_count INTEGER NOT NULL DEFAULT 0 CHECK(succeeded_count >= 0),
    online_count INTEGER NOT NULL DEFAULT 0 CHECK(online_count >= 0),
    dirty_generation INTEGER NOT NULL DEFAULT 0 CHECK(dirty_generation >= 0),
    built_generation INTEGER NOT NULL DEFAULT 0 CHECK(built_generation >= 0),
    building_generation INTEGER CHECK(building_generation IS NULL OR building_generation > 0),
    build_not_before TEXT,
    build_owner TEXT,
    build_lease_expires_at TEXT,
    delivery_key TEXT,
    delivery_started_at TEXT,
    delivery_finished_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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


def _migrate_to_v5(conn: sqlite3.Connection, path: Path) -> None:
    backup_path = path.with_name(f"{path.name}.pre-v5.bak")
    if path.is_file() and not backup_path.exists():
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
            row = backup.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError("schema v5 迁移前数据库备份校验失败")
        except Exception:
            backup.close()
            backup_path.unlink(missing_ok=True)
            raise
        finally:
            try:
                backup.close()
            except sqlite3.Error:
                pass
    conn.executescript(_TRANSLATION_AUTOMATION_SCHEMA)


def _migrate_to_v6(conn: sqlite3.Connection, path: Path) -> None:
    """Extend durable Admin actions without dropping existing audit rows."""
    backup_path = path.with_name(f"{path.name}.pre-v6.bak")
    if path.is_file() and not backup_path.exists():
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
            row = backup.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError("schema v6 迁移前数据库备份校验失败")
        finally:
            backup.close()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE translation_admin_actions RENAME TO translation_admin_actions_v5")
        conn.execute(
            "CREATE TABLE translation_admin_actions ("
            "action_id TEXT PRIMARY KEY,"
            "task_id TEXT REFERENCES translation_tasks(task_id) ON DELETE SET NULL,"
            "provider_id TEXT NOT NULL,"
            "action TEXT NOT NULL CHECK(action IN ("
            "'dispatch', 'retry', 'cancel', 'probe', 'unblock', 'recover')),"
            "actor TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('requested', 'running', 'completed',"
            " 'rejected', 'timed_out', 'recovered')),"
            "requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, result_code TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO translation_admin_actions "
            "(action_id, task_id, provider_id, action, actor, status, requested_at, "
            "started_at, finished_at, result_code) "
            "SELECT action_id, task_id, provider_id, action, actor, status, requested_at, "
            "started_at, finished_at, result_code FROM translation_admin_actions_v5"
        )
        conn.execute("DROP TABLE translation_admin_actions_v5")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_translation_admin_actions_requested "
            "ON translation_admin_actions(requested_at DESC)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_to_v7(conn: sqlite3.Connection, path: Path) -> None:
    """Add the explicit dispatch action without rewriting existing audit rows."""
    backup_path = path.with_name(f"{path.name}.pre-v7.bak")
    if path.is_file() and not backup_path.exists():
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
            row = backup.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise RuntimeError("schema v7 迁移前数据库备份校验失败")
        finally:
            backup.close()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE translation_admin_actions RENAME TO translation_admin_actions_v6")
        conn.execute(
            "CREATE TABLE translation_admin_actions ("
            "action_id TEXT PRIMARY KEY,"
            "task_id TEXT REFERENCES translation_tasks(task_id) ON DELETE SET NULL,"
            "provider_id TEXT NOT NULL,"
            "action TEXT NOT NULL CHECK(action IN ("
            "'dispatch', 'retry', 'cancel', 'probe', 'unblock', 'recover')),"
            "actor TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('requested', 'running', 'completed',"
            " 'rejected', 'timed_out', 'recovered')),"
            "requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, result_code TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO translation_admin_actions "
            "(action_id, task_id, provider_id, action, actor, status, requested_at, "
            "started_at, finished_at, result_code) "
            "SELECT action_id, task_id, provider_id, action, actor, status, requested_at, "
            "started_at, finished_at, result_code FROM translation_admin_actions_v6"
        )
        conn.execute("DROP TABLE translation_admin_actions_v6")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_translation_admin_actions_requested "
            "ON translation_admin_actions(requested_at DESC)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def connect(path: Path) -> sqlite3.Connection:
    """打开数据库,父目录与表按需创建,并校验 schema 版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.executescript(_TRANSLATION_AUTOMATION_SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif row["value"] in {"1", "2", "3", "4"} and SCHEMA_VERSION == 7:
        if row["value"] in {"1", "2"}:
            _migrate_to_v3(conn)
        if row["value"] in {"1", "2", "3"}:
            _migrate_to_v4(conn)
        _migrate_to_v5(conn, path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("5",),
        )
        conn.commit()
        _migrate_to_v6(conn, path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("6",),
        )
        conn.commit()
        _migrate_to_v7(conn, path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif row["value"] == "5" and SCHEMA_VERSION == 7:
        _migrate_to_v6(conn, path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            ("6",),
        )
        conn.commit()
        _migrate_to_v7(conn, path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif row["value"] == "6" and SCHEMA_VERSION == 7:
        _migrate_to_v7(conn, path)
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
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


_TRANSLATION_ERROR_CODES = frozenset(
    {
        "AUTH_401",
        "AUTH_403",
        "UPSTREAM_ERROR",
        "RATE_LIMIT_429",
        "PROVIDER_5XX",
        "NETWORK_CONNECT_FAILED",
        "REQUEST_TIMEOUT",
        "EMPTY_RESPONSE",
        "UNPARSEABLE_RESPONSE",
        "SCHEMA_VALIDATION_FAILED",
        "TASK_DATA_MISSING",
        "CONFIGURATION_INVALID",
        "REQUEST_CANCELLED",
        "CIRCUIT_OPEN",
        "BUILD_FAILED",
    }
)
_TRANSLATION_ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "upstream",
        "configuration",
        "provider_infrastructure",
        "response_format",
        "schema",
        "data_integrity",
        "cancelled",
        "build",
    }
)
_TRANSLATION_FAILURE_STAGES = frozenset(
    {
        "waiting",
        "connect_provider",
        "waiting_model",
        "receiving_response",
        "schema_validation",
        "saving_translation",
        "waiting_build",
        "building",
    }
)
_TASK_RETRY_DELAYS = (15, 30, 60, 120, 300)
_CIRCUIT_COOLDOWNS = (60, 120, 300)
_NON_RETRYABLE_TRANSLATION_ERRORS = (
    "EMPTY_RESPONSE",
    "UNPARSEABLE_RESPONSE",
    "SCHEMA_VALIDATION_FAILED",
    "TASK_DATA_MISSING",
)


def _automation_timestamp(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("automation timestamp must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise ValueError("automation timestamp must include a timezone")
    return parsed.astimezone(dt.UTC).isoformat()


def _future_timestamp(value: str, seconds: int) -> str:
    return (dt.datetime.fromisoformat(value) + dt.timedelta(seconds=seconds)).isoformat()


def _non_empty(value: str, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-empty and at most {maximum} characters")
    return value


def _translation_task_id(edition_date: str, article_id: str, provider_id: str) -> str:
    payload = f"{edition_date}\n{article_id}\n{provider_id}".encode()
    return hashlib.sha256(payload).hexdigest()


def _translation_task(row: sqlite3.Row) -> TranslationTask:
    return TranslationTask(
        task_id=row["task_id"],
        edition_date=row["edition_date"],
        article_id=row["article_id"],
        article_title=row["article_title"],
        provider_id=row["provider_id"],
        status=row["status"],
        build_status=row["build_status"],
        attempt_count=row["attempt_count"],
        success_generation=row["success_generation"],
        error_code=row["error_code"],
        error_category=row["error_category"],
        http_status=row["http_status"],
        current_stage=row["current_stage"],
        failure_stage=row["failure_stage"],
        auto_retry=bool(row["auto_retry"]),
        diagnostic_id=row["diagnostic_id"],
        failed_at=row["failed_at"],
        next_retry_at=row["next_retry_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        hard_timeout_at=row["hard_timeout_at"],
        cancel_requested_at=row["cancel_requested_at"],
        manual_retry_requested_at=row["manual_retry_requested_at"],
        manual_probe_requested_at=row["manual_probe_requested_at"],
        manual_action_id=row["manual_action_id"],
        received_chunks=row["received_chunks"],
        last_activity_at=row["last_activity_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _translation_task_select() -> str:
    return (
        "SELECT task_id, edition_date, article_id, article_title, provider_id, status,"
        " build_status, attempt_count, success_generation, error_code, error_category,"
        " http_status, current_stage, failure_stage,"
        " auto_retry, diagnostic_id, failed_at, next_retry_at, started_at, finished_at,"
        " lease_owner, lease_expires_at, hard_timeout_at, cancel_requested_at,"
        " manual_retry_requested_at, manual_probe_requested_at, manual_action_id,"
        " received_chunks, last_activity_at, created_at, updated_at"
        " FROM translation_tasks"
    )


def translation_task(conn: sqlite3.Connection, task_id: str) -> TranslationTask | None:
    _validate_test_attempt_digest(task_id, "task_id")
    row = conn.execute(_translation_task_select() + " WHERE task_id = ?", (task_id,)).fetchone()
    return _translation_task(row) if row else None


def queued_provider_probe(
    conn: sqlite3.Connection, provider_id: str
) -> TranslationTask | None:
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    row = conn.execute(
        _translation_task_select()
        + " WHERE provider_id = ? AND manual_probe_requested_at IS NOT NULL"
        " ORDER BY manual_probe_requested_at LIMIT 1",
        (provider_id,),
    ).fetchone()
    return _translation_task(row) if row else None


def list_translation_tasks(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    status: TranslationTaskStatus | None = None,
) -> list[TranslationTask]:
    _validate_test_attempt_date(edition_date)
    parameters: list[str] = [edition_date]
    where = " WHERE edition_date = ?"
    if status is not None:
        where += " AND status = ?"
        parameters.append(status)
    rows = conn.execute(
        _translation_task_select() + where + " ORDER BY created_at, task_id", parameters
    ).fetchall()
    return [_translation_task(row) for row in rows]


def ensure_translation_task(
    conn: sqlite3.Connection,
    *,
    edition_date: str,
    article_id: str,
    article_title: str,
    provider_id: str,
    now: str,
) -> TranslationTask:
    _validate_test_attempt_date(edition_date)
    article_id = _non_empty(article_id, "article_id", maximum=2048)
    article_title = _non_empty(article_title, "article_title", maximum=1000)
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    now = _automation_timestamp(now)
    task_id = _translation_task_id(edition_date, article_id, provider_id)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO translation_tasks"
            " (task_id, edition_date, article_id, article_title, provider_id, status,"
            " build_status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', 'build_pending', ?, ?)",
            (task_id, edition_date, article_id, article_title, provider_id, now, now),
        )
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task was not created")
    return task


def _translation_attempt(row: sqlite3.Row) -> TranslationAttempt:
    return TranslationAttempt(
        id=row["id"],
        task_id=row["task_id"],
        attempt_number=row["attempt_number"],
        owner=row["owner"],
        kind=row["kind"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_code=row["error_code"],
        error_category=row["error_category"],
        failure_stage=row["failure_stage"],
        diagnostic_id=row["diagnostic_id"],
    )


def list_translation_attempts(
    conn: sqlite3.Connection, task_id: str
) -> list[TranslationAttempt]:
    _validate_test_attempt_digest(task_id, "task_id")
    rows = conn.execute(
        "SELECT id, task_id, attempt_number, owner, kind, status, started_at, finished_at,"
        " error_code, error_category, failure_stage, diagnostic_id"
        " FROM translation_attempts WHERE task_id = ? ORDER BY attempt_number",
        (task_id,),
    ).fetchall()
    return [_translation_attempt(row) for row in rows]


def latest_translation_admin_action(
    conn: sqlite3.Connection, task_id: str
) -> TranslationAdminAction | None:
    _validate_test_attempt_digest(task_id, "task_id")
    row = conn.execute(
        "SELECT action_id, task_id, provider_id, action, actor, status, requested_at,"
        " started_at, finished_at, result_code FROM translation_admin_actions"
        " WHERE task_id = ? ORDER BY requested_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return TranslationAdminAction(
        action_id=row["action_id"],
        task_id=row["task_id"],
        provider_id=row["provider_id"],
        action=row["action"],
        actor=row["actor"],
        status=row["status"],
        requested_at=row["requested_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result_code=row["result_code"],
    )


def claim_translation_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    now: str,
    lease_seconds: int,
    manual: bool = False,
    probe: bool = False,
) -> TranslationTask | None:
    _validate_test_attempt_digest(task_id, "task_id")
    owner = _non_empty(owner, "owner", maximum=128)
    now = _automation_timestamp(now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    lease_expires_at = _future_timestamp(now, lease_seconds)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, next_retry_at, attempt_count, manual_retry_requested_at,"
            " manual_probe_requested_at, manual_action_id"
            " FROM translation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation task does not exist")
        allowed = row["status"] == "pending" or row["status"] in {"retry_wait", "failed"} and (
            manual or (row["next_retry_at"] is not None and row["next_retry_at"] <= now)
        )
        if probe and row["status"] == "configuration_blocked":
            allowed = True
        if not allowed:
            conn.commit()
            return None
        attempt_number = row["attempt_count"] + 1
        manual = manual or row["manual_retry_requested_at"] is not None
        probe = probe or row["manual_probe_requested_at"] is not None
        kind: TranslationAttemptKind = "probe" if probe else (
            "manual" if manual else "automatic"
        )
        cursor = conn.execute(
            "UPDATE translation_tasks SET status = 'running', attempt_count = ?,"
            " error_code = NULL, error_category = NULL, http_status = NULL,"
            " current_stage = 'connect_provider', failure_stage = NULL,"
            " diagnostic_id = NULL, received_chunks = 0, cancel_requested_at = NULL,"
            " manual_retry_requested_at = NULL, manual_probe_requested_at = NULL,"
            " failed_at = NULL,"
            " next_retry_at = NULL, started_at = ?, finished_at = NULL, lease_owner = ?,"
            " lease_expires_at = ?, hard_timeout_at = ?, last_activity_at = ?, updated_at = ?"
            " WHERE task_id = ? AND status = ?",
            (
                attempt_number,
                now,
                owner,
                lease_expires_at,
                lease_expires_at,
                now,
                now,
                task_id,
                row["status"],
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.execute(
            "INSERT INTO translation_attempts"
            " (task_id, attempt_number, owner, kind, status, started_at)"
            " VALUES (?, ?, ?, ?, 'running', ?)",
            (task_id, attempt_number, owner, kind, now),
        )
        if row["manual_action_id"] is not None:
            conn.execute(
                "UPDATE translation_admin_actions SET status = 'running', started_at = ?"
                " WHERE action_id = ? AND status = 'requested'",
                (now, row["manual_action_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return translation_task(conn, task_id)


def touch_translation_task(
    conn: sqlite3.Connection, task_id: str, *, owner: str, now: str, lease_seconds: int
) -> bool:
    now = _automation_timestamp(now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    with conn:
        cursor = conn.execute(
            "UPDATE translation_tasks SET lease_expires_at = ?, last_activity_at = ?,"
            " hard_timeout_at = ?, updated_at = ?"
            " WHERE task_id = ? AND status = 'running' AND lease_owner = ?",
            (
                _future_timestamp(now, lease_seconds),
                now,
                _future_timestamp(now, lease_seconds),
                now,
                task_id,
                owner,
            ),
        )
    return cursor.rowcount == 1


def update_translation_task_progress(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    stage: str,
    now: str,
    received_chunks: int | None = None,
) -> bool:
    if stage not in _TRANSLATION_FAILURE_STAGES:
        raise ValueError("stage is not a safe closed value")
    if received_chunks is not None and (
        type(received_chunks) is not int or received_chunks < 0
    ):
        raise ValueError("received_chunks must be a non-negative integer")
    now = _automation_timestamp(now)
    assignments = "current_stage = ?, last_activity_at = ?, updated_at = ?"
    parameters: list[object] = [stage, now, now]
    if received_chunks is not None:
        assignments += ", received_chunks = ?"
        parameters.append(received_chunks)
    parameters.extend((task_id, owner))
    with conn:
        cursor = conn.execute(
            f"UPDATE translation_tasks SET {assignments}"
            " WHERE task_id = ? AND status = 'running' AND lease_owner = ?",
            parameters,
        )
    return cursor.rowcount == 1


def request_translation_task_cancel(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: str,
    actor: str = "local-admin",
) -> TranslationTask:
    _validate_test_attempt_digest(task_id, "task_id")
    actor = _non_empty(actor, "actor", maximum=128)
    now = _automation_timestamp(now)
    action_id = uuid.uuid4().hex
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT provider_id FROM translation_tasks"
            " WHERE task_id = ? AND status = 'running' AND cancel_requested_at IS NULL",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation task is not running")
        conn.execute(
            "UPDATE translation_tasks SET cancel_requested_at = ?, updated_at = ?"
            " WHERE task_id = ?",
            (now, now, task_id),
        )
        conn.execute(
            "INSERT INTO translation_admin_actions"
            " (action_id, task_id, provider_id, action, actor, status, requested_at)"
            " VALUES (?, ?, ?, 'cancel', ?, 'requested', ?)",
            (action_id, task_id, row["provider_id"], actor, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    return task


def queue_translation_task_dispatch(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: str,
    actor: str,
) -> TranslationTask:
    """Request immediate scheduling for a pending task through an audited action."""
    _validate_test_attempt_digest(task_id, "task_id")
    actor = _non_empty(actor, "actor", maximum=128)
    now = _automation_timestamp(now)
    action_id = uuid.uuid4().hex
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT provider_id, status, manual_retry_requested_at, next_retry_at"
            " FROM translation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation task does not exist")
        if row["status"] != "pending":
            raise RuntimeError("translation task is not waiting for dispatch")
        if row["manual_retry_requested_at"] is not None:
            raise RuntimeError("translation dispatch is already queued")
        circuit = conn.execute(
            "SELECT state FROM provider_circuits WHERE provider_id = ?",
            (row["provider_id"],),
        ).fetchone()
        if circuit is not None and circuit["state"] != "closed":
            raise RuntimeError("provider circuit requires a controlled probe")
        conn.execute(
            "INSERT INTO translation_admin_actions"
            " (action_id, task_id, provider_id, action, actor, status, requested_at)"
            " VALUES (?, ?, ?, 'dispatch', ?, 'requested', ?)",
            (action_id, task_id, row["provider_id"], actor, now),
        )
        conn.execute(
            "UPDATE translation_tasks SET auto_retry = 1, next_retry_at = ?,"
            " manual_retry_requested_at = ?, manual_action_id = ?, updated_at = ?"
            " WHERE task_id = ? AND status = 'pending'",
            (now, now, action_id, now, task_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    return task


def queue_translation_task_retry(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: str,
    actor: str,
) -> TranslationTask:
    _validate_test_attempt_digest(task_id, "task_id")
    actor = _non_empty(actor, "actor", maximum=128)
    now = _automation_timestamp(now)
    action_id = uuid.uuid4().hex
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT provider_id, status, manual_retry_requested_at"
            " FROM translation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation task does not exist")
        if row["status"] not in {
            "failed",
            "retry_wait",
            "cancelled",
            "configuration_blocked",
        }:
            raise RuntimeError("translation task is not retryable")
        if row["manual_retry_requested_at"] is not None:
            raise RuntimeError("translation retry is already queued")
        circuit = conn.execute(
            "SELECT state FROM provider_circuits WHERE provider_id = ?",
            (row["provider_id"],),
        ).fetchone()
        if circuit is not None and circuit["state"] != "closed":
            raise RuntimeError("provider circuit requires a controlled probe")
        conn.execute(
            "INSERT INTO translation_admin_actions"
            " (action_id, task_id, provider_id, action, actor, status, requested_at)"
            " VALUES (?, ?, ?, 'retry', ?, 'requested', ?)",
            (action_id, task_id, row["provider_id"], actor, now),
        )
        conn.execute(
            "UPDATE translation_tasks SET status = 'retry_wait', auto_retry = 1,"
            " next_retry_at = ?, manual_retry_requested_at = ?, manual_action_id = ?,"
            " updated_at = ? WHERE task_id = ?",
            (now, now, action_id, now, task_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    return task


def queue_provider_probe(
    conn: sqlite3.Connection,
    provider_id: str,
    task_id: str,
    *,
    now: str,
    actor: str,
) -> TranslationTask:
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    _validate_test_attempt_digest(task_id, "task_id")
    actor = _non_empty(actor, "actor", maximum=128)
    now = _automation_timestamp(now)
    action_id = uuid.uuid4().hex
    try:
        conn.execute("BEGIN IMMEDIATE")
        circuit = conn.execute(
            "SELECT state, probe_task_id FROM provider_circuits WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        if circuit is None:
            raise RuntimeError("provider circuit is not probeable")
        if circuit["state"] == "half_open":
            active_task_id = circuit["probe_task_id"]
            if not active_task_id:
                raise RuntimeError("provider probe is already running")
            active_task = conn.execute(
                _translation_task_select() + " WHERE task_id = ?",
                (active_task_id,),
            ).fetchone()
            if active_task is None:
                raise RuntimeError("provider probe task does not exist")
            conn.commit()
            return _translation_task(active_task)
        if circuit["state"] not in {"open", "configuration_blocked"}:
            raise RuntimeError("provider circuit is not probeable")
        queued_probe = conn.execute(
            "SELECT 1 FROM translation_tasks WHERE provider_id = ?"
            " AND manual_probe_requested_at IS NOT NULL LIMIT 1",
            (provider_id,),
        ).fetchone()
        if queued_probe is not None:
            raise RuntimeError("provider probe is already queued")
        task_row = conn.execute(
            "SELECT status, manual_probe_requested_at FROM translation_tasks"
            " WHERE task_id = ? AND provider_id = ?",
            (task_id, provider_id),
        ).fetchone()
        if task_row is None or task_row["status"] not in {
            "pending",
            "failed",
            "retry_wait",
            "cancelled",
            "configuration_blocked",
        }:
            raise RuntimeError("translation task cannot be used as a probe")
        if task_row["manual_probe_requested_at"] is not None:
            raise RuntimeError("provider probe is already queued")
        conn.execute(
            "INSERT INTO translation_admin_actions"
            " (action_id, task_id, provider_id, action, actor, status, requested_at)"
            " VALUES (?, ?, ?, 'probe', ?, 'requested', ?)",
            (action_id, task_id, provider_id, actor, now),
        )
        conn.execute(
            "UPDATE translation_tasks SET status = CASE WHEN status = 'pending'"
            " THEN 'pending' ELSE 'retry_wait' END, auto_retry = 1, next_retry_at = ?,"
            " manual_retry_requested_at = ?, manual_probe_requested_at = ?,"
            " manual_action_id = ?, updated_at = ? WHERE task_id = ?",
            (now, now, now, action_id, now, task_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    return task


def confirm_translation_task_cancelled(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    now: str,
    request_terminated: bool,
) -> TranslationTask | None:
    if not request_terminated:
        return None
    now = _automation_timestamp(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt_count, manual_action_id FROM translation_tasks"
            " WHERE task_id = ? AND status = 'running' AND lease_owner = ?"
            " AND cancel_requested_at IS NOT NULL",
            (task_id, owner),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation cancellation is not active for this worker")
        delay = _TASK_RETRY_DELAYS[min(row["attempt_count"], len(_TASK_RETRY_DELAYS)) - 1]
        diagnostic_id = f"cancel-{task_id[:16]}-{row['attempt_count']}"
        conn.execute(
            "UPDATE translation_tasks SET status = 'retry_wait',"
            " error_code = 'REQUEST_CANCELLED', error_category = 'cancelled',"
            " current_stage = 'waiting', failure_stage = 'waiting_model', auto_retry = 1,"
            " diagnostic_id = ?, failed_at = ?, next_retry_at = ?, finished_at = ?,"
            " lease_owner = NULL, lease_expires_at = NULL, hard_timeout_at = NULL,"
            " cancel_requested_at = NULL, last_activity_at = ?, updated_at = ?"
            " WHERE task_id = ?",
            (
                diagnostic_id,
                now,
                _future_timestamp(now, delay),
                now,
                now,
                now,
                task_id,
            ),
        )
        conn.execute(
            "UPDATE translation_attempts SET status = 'cancelled', finished_at = ?,"
            " error_code = 'REQUEST_CANCELLED', error_category = 'cancelled',"
            " failure_stage = 'waiting_model', diagnostic_id = ?"
            " WHERE task_id = ? AND attempt_number = ? AND status = 'running' AND owner = ?",
            (now, diagnostic_id, task_id, row["attempt_count"], owner),
        )
        conn.execute(
            "UPDATE translation_admin_actions SET status = 'completed', started_at ="
            " COALESCE(started_at, ?), finished_at = ?, result_code = 'REQUEST_CANCELLED'"
            " WHERE action_id = (SELECT action_id FROM translation_admin_actions"
            " WHERE task_id = ? AND action = 'cancel' AND status = 'requested'"
            " ORDER BY requested_at DESC LIMIT 1)",
            (now, now, task_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return translation_task(conn, task_id)


def _validate_translation_failure(
    error_code: str,
    error_category: str,
    failure_stage: str,
    diagnostic_id: str,
    http_status: int | None,
) -> None:
    if error_code not in _TRANSLATION_ERROR_CODES:
        raise ValueError("error_code is not a safe closed value")
    if error_category not in _TRANSLATION_ERROR_CATEGORIES:
        raise ValueError("error_category is not a safe closed value")
    if failure_stage not in _TRANSLATION_FAILURE_STAGES:
        raise ValueError("failure_stage is not a safe closed value")
    _non_empty(diagnostic_id, "diagnostic_id", maximum=128)
    if http_status is not None and (
        type(http_status) is not int or not 100 <= http_status <= 599
    ):
        raise ValueError("http_status must be a valid HTTP status")


def finish_translation_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    owner: str,
    now: str,
    error_code: str,
    error_category: str,
    failure_stage: str,
    diagnostic_id: str,
    http_status: int | None = None,
    auto_retry: bool = True,
) -> TranslationTask:
    _validate_translation_failure(
        error_code, error_category, failure_stage, diagnostic_id, http_status
    )
    now = _automation_timestamp(now)
    # Only locally validated configuration errors may permanently block a
    # provider. Upstream HTTP/auth/permission failures remain retryable.
    configuration_blocked = error_code == "CONFIGURATION_INVALID"
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt_count, manual_action_id FROM translation_tasks"
            " WHERE task_id = ? AND status = 'running' AND lease_owner = ?",
            (task_id, owner),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation task is not owned by this worker")
        status = "configuration_blocked" if configuration_blocked else (
            "retry_wait" if auto_retry else "failed"
        )
        delay = _TASK_RETRY_DELAYS[min(row["attempt_count"], len(_TASK_RETRY_DELAYS)) - 1]
        next_retry_at = _future_timestamp(now, delay) if status == "retry_wait" else None
        conn.execute(
            "UPDATE translation_tasks SET status = ?, error_code = ?, error_category = ?,"
            " http_status = ?, current_stage = ?, failure_stage = ?, auto_retry = ?,"
            " diagnostic_id = ?,"
            " failed_at = ?, next_retry_at = ?, finished_at = ?, lease_owner = NULL,"
            " lease_expires_at = NULL, hard_timeout_at = NULL, cancel_requested_at = NULL,"
            " last_activity_at = ?, updated_at = ? WHERE task_id = ?",
            (
                status,
                error_code,
                error_category,
                http_status,
                failure_stage,
                failure_stage,
                int(auto_retry and not configuration_blocked),
                diagnostic_id,
                now,
                next_retry_at,
                now,
                now,
                now,
                task_id,
            ),
        )
        conn.execute(
            "UPDATE translation_attempts SET status = 'failed', finished_at = ?,"
            " error_code = ?, error_category = ?, failure_stage = ?, diagnostic_id = ?"
            " WHERE task_id = ? AND attempt_number = ? AND status = 'running' AND owner = ?",
            (
                now,
                error_code,
                error_category,
                failure_stage,
                diagnostic_id,
                task_id,
                row["attempt_count"],
                owner,
            ),
        )
        if row["manual_action_id"] is not None:
            conn.execute(
                "UPDATE translation_admin_actions SET status = 'completed', finished_at = ?,"
                " result_code = ? WHERE action_id = ? AND status = 'running'",
                (now, error_code, row["manual_action_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    return task


def finish_translation_task_success(
    conn: sqlite3.Connection, task_id: str, *, owner: str, now: str
) -> TranslationTask:
    now = _automation_timestamp(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt_count, manual_action_id FROM translation_tasks"
            " WHERE task_id = ? AND status = 'running' AND lease_owner = ?",
            (task_id, owner),
        ).fetchone()
        if row is None:
            raise RuntimeError("translation task is not owned by this worker")
        conn.execute(
            "UPDATE translation_tasks SET status = 'succeeded', error_code = NULL,"
            " error_category = NULL, http_status = NULL, current_stage = 'waiting_build',"
            " failure_stage = NULL,"
            " auto_retry = 0, diagnostic_id = NULL, failed_at = NULL, next_retry_at = NULL,"
            " finished_at = ?, lease_owner = NULL, lease_expires_at = NULL,"
            " hard_timeout_at = NULL, cancel_requested_at = NULL,"
            " last_activity_at = ?, updated_at = ? WHERE task_id = ?",
            (now, now, now, task_id),
        )
        conn.execute(
            "UPDATE translation_attempts SET status = 'succeeded', finished_at = ?"
            " WHERE task_id = ? AND attempt_number = ? AND status = 'running' AND owner = ?",
            (now, task_id, row["attempt_count"], owner),
        )
        if row["manual_action_id"] is not None:
            conn.execute(
                "UPDATE translation_admin_actions SET status = 'completed', finished_at = ?,"
                " result_code = 'SUCCEEDED' WHERE action_id = ? AND status = 'running'",
                (now, row["manual_action_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    task = translation_task(conn, task_id)
    if task is None:
        raise RuntimeError("translation task does not exist")
    return task


def recover_interrupted_translation_tasks(
    conn: sqlite3.Connection,
    *,
    now: str,
    process_terminated: bool,
) -> int:
    """Recover only expired leases after the old process is known to be stopped."""
    now = _automation_timestamp(now)
    if not process_terminated:
        return 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Older workers incorrectly left content/schema failures in retry_wait.
        # Normalize those rows before scheduling so an upgrade cannot revive an
        # infinite retry loop.
        placeholders = ", ".join("?" for _ in _NON_RETRYABLE_TRANSLATION_ERRORS)
        # A manual retry deliberately reuses ``auto_retry`` and ``retry_wait``
        # so the normal worker can claim it.  Do not mistake that explicit
        # action for a legacy automatic retry during startup recovery.
        conn.execute(
            "UPDATE translation_tasks SET status = 'retry_wait', auto_retry = 1,"
            " next_retry_at = COALESCE(manual_retry_requested_at, ?),"
            " updated_at = ? WHERE status = 'failed' AND auto_retry = 0"
            " AND error_code IN (" + placeholders + ")"
            " AND manual_retry_requested_at IS NOT NULL"
            " AND manual_probe_requested_at IS NULL"
            " AND manual_action_id IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM translation_admin_actions"
            " WHERE translation_admin_actions.action_id = translation_tasks.manual_action_id"
            " AND translation_admin_actions.task_id = translation_tasks.task_id"
            " AND translation_admin_actions.action = 'retry'"
            " AND translation_admin_actions.status = 'requested')",
            (now, now, *_NON_RETRYABLE_TRANSLATION_ERRORS),
        )
        conn.execute(
            "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
            " next_retry_at = NULL, current_stage = CASE error_code"
            " WHEN 'SCHEMA_VALIDATION_FAILED' THEN 'schema_validation'"
            " WHEN 'EMPTY_RESPONSE' THEN 'receiving_response'"
            " WHEN 'UNPARSEABLE_RESPONSE' THEN 'receiving_response'"
            " ELSE current_stage END, failure_stage = CASE error_code"
            " WHEN 'SCHEMA_VALIDATION_FAILED' THEN 'schema_validation'"
            " WHEN 'EMPTY_RESPONSE' THEN 'receiving_response'"
            " WHEN 'UNPARSEABLE_RESPONSE' THEN 'receiving_response'"
            " ELSE failure_stage END, updated_at = ?"
            " WHERE status IN ('retry_wait', 'failed') AND auto_retry = 1"
            " AND manual_retry_requested_at IS NULL"
            " AND manual_probe_requested_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM translation_admin_actions"
            " WHERE translation_admin_actions.action_id = translation_tasks.manual_action_id"
            " AND translation_admin_actions.task_id = translation_tasks.task_id"
            " AND translation_admin_actions.action IN ('retry', 'probe')"
            " AND translation_admin_actions.status IN ('requested', 'running'))"
            f" AND error_code IN ({placeholders})",
            (now, *_NON_RETRYABLE_TRANSLATION_ERRORS),
        )
        rows = conn.execute(
            "SELECT task_id, attempt_count, lease_owner, cancel_requested_at FROM translation_tasks"
            " WHERE status = 'running' AND lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            delay = _TASK_RETRY_DELAYS[min(row["attempt_count"], len(_TASK_RETRY_DELAYS)) - 1]
            conn.execute(
                "UPDATE translation_tasks SET status = 'retry_wait',"
                " error_code = 'REQUEST_CANCELLED', error_category = 'cancelled',"
                " current_stage = 'waiting_model', failure_stage = 'waiting_model',"
                " auto_retry = 1,"
                " diagnostic_id = 'worker-restarted', failed_at = ?, next_retry_at = ?,"
                " finished_at = ?, lease_owner = NULL, lease_expires_at = NULL,"
                " hard_timeout_at = NULL, cancel_requested_at = NULL,"
                " last_activity_at = ?, updated_at = ? WHERE task_id = ?",
                (now, _future_timestamp(now, delay), now, now, now, row["task_id"]),
            )
            conn.execute(
                "UPDATE translation_attempts SET status = 'cancelled', finished_at = ?,"
                " error_code = 'REQUEST_CANCELLED', error_category = 'cancelled',"
                " failure_stage = 'waiting_model', diagnostic_id = 'worker-restarted'"
                " WHERE task_id = ? AND attempt_number = ? AND status = 'running'"
                " AND owner = ?",
                (now, row["task_id"], row["attempt_count"], row["lease_owner"]),
            )
            conn.execute(
                "UPDATE translation_admin_actions SET status = 'recovered',"
                " started_at = COALESCE(started_at, ?), finished_at = ?,"
                " result_code = 'RECOVERED' WHERE task_id = ? AND action = 'recover'"
                " AND status IN ('requested', 'running')",
                (now, now, row["task_id"]),
            )
            if row["cancel_requested_at"] is not None:
                conn.execute(
                    "UPDATE translation_admin_actions SET status = 'recovered',"
                    " started_at = COALESCE(started_at, ?), finished_at = ?,"
                    " result_code = 'RECOVERED' WHERE task_id = ? AND action = 'cancel'"
                    " AND status IN ('requested', 'running')",
                    (now, now, row["task_id"]),
                )
        # A worker can die after claiming a half-open provider probe. Reopen
        # the circuit and release that probe lease so Admin can probe again.
        probe_rows = conn.execute(
            "SELECT provider_id, probe_task_id, open_count FROM provider_circuits"
            " WHERE state = 'half_open' AND probe_lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for row in probe_rows:
            next_open_count = row["open_count"] + 1
            conn.execute(
                "UPDATE provider_circuits SET state = 'open', open_count = ?,"
                " opened_at = ?, next_probe_at = ?, probe_task_id = NULL,"
                " probe_owner = NULL, probe_lease_expires_at = NULL,"
                " last_outcome = 'provider_failure', updated_at = ?"
                " WHERE provider_id = ? AND state = 'half_open'",
                (
                    next_open_count,
                    now,
                    _future_timestamp(now, _circuit_cooldown(next_open_count)),
                    now,
                    row["provider_id"],
                ),
            )
            if row["probe_task_id"]:
                conn.execute(
                    "UPDATE translation_tasks SET status = 'retry_wait', auto_retry = 1,"
                    " error_code = 'REQUEST_TIMEOUT', error_category = 'provider_infrastructure',"
                    " failure_stage = 'waiting_model', current_stage = 'waiting_model',"
                    " failed_at = ?, next_retry_at = ?, finished_at = ?,"
                    " manual_probe_requested_at = NULL, manual_retry_requested_at = NULL,"
                    " manual_action_id = NULL, updated_at = ? WHERE task_id = ?"
                    " AND status IN ('running', 'retry_wait', 'pending')",
                    (
                        now,
                        _future_timestamp(now, _TASK_RETRY_DELAYS[0]),
                        now,
                        now,
                        row["probe_task_id"],
                    ),
                )
                conn.execute(
                    "UPDATE translation_attempts SET status = 'failed', finished_at = ?,"
                    " error_code = 'REQUEST_TIMEOUT',"
                    " error_category = 'provider_infrastructure',"
                    " failure_stage = 'waiting_model', diagnostic_id = 'probe-expired'"
                    " WHERE task_id = ? AND status = 'running'",
                    (now, row["probe_task_id"]),
                )
                conn.execute(
                    "UPDATE translation_admin_actions SET status = 'completed',"
                    " started_at = COALESCE(started_at, ?), finished_at = ?,"
                    " result_code = 'REQUEST_TIMEOUT' WHERE task_id = ?"
                    " AND action = 'probe' AND status IN ('requested', 'running')",
                    (now, now, row["probe_task_id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def list_ready_translation_tasks(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    now: str,
    limit: int | None = None,
) -> list[TranslationTask]:
    _validate_test_attempt_date(edition_date)
    now = _automation_timestamp(now)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("limit must be a positive integer")
    sql = (
        _translation_task_select()
        + " WHERE edition_date = ? AND (status = 'pending' OR"
        " (status IN ('failed', 'retry_wait') AND auto_retry = 1 AND next_retry_at <= ?))"
        " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,"
        " COALESCE(next_retry_at, created_at), task_id"
    )
    parameters: list[object] = [edition_date, now]
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    return [_translation_task(row) for row in conn.execute(sql, parameters).fetchall()]


def count_running_translation_tasks(conn: sqlite3.Connection, provider_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM translation_tasks"
        " WHERE provider_id = ? AND status = 'running'",
        (provider_id,),
    ).fetchone()
    return int(row["count"])


def _automation_edition(row: sqlite3.Row) -> AutomationEdition:
    return AutomationEdition(
        edition_date=row["edition_date"],
        status=row["status"],
        target_count=row["target_count"],
        succeeded_count=row["succeeded_count"],
        online_count=row["online_count"],
        dirty_generation=row["dirty_generation"],
        built_generation=row["built_generation"],
        building_generation=row["building_generation"],
        build_not_before=row["build_not_before"],
        build_owner=row["build_owner"],
        build_lease_expires_at=row["build_lease_expires_at"],
        delivery_key=row["delivery_key"],
        delivery_started_at=row["delivery_started_at"],
        delivery_finished_at=row["delivery_finished_at"],
        last_error_code=row["last_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _automation_edition_select() -> str:
    return (
        "SELECT edition_date, status, target_count, succeeded_count, online_count,"
        " dirty_generation, built_generation, building_generation, build_not_before,"
        " build_owner, build_lease_expires_at, delivery_key, delivery_started_at,"
        " delivery_finished_at, last_error_code, created_at, updated_at"
        " FROM automation_editions"
    )


def automation_edition(
    conn: sqlite3.Connection, edition_date: str
) -> AutomationEdition | None:
    _validate_test_attempt_date(edition_date)
    row = conn.execute(
        _automation_edition_select() + " WHERE edition_date = ?", (edition_date,)
    ).fetchone()
    return _automation_edition(row) if row else None


def latest_automation_edition(conn: sqlite3.Connection) -> AutomationEdition | None:
    row = conn.execute(
        _automation_edition_select() + " ORDER BY edition_date DESC LIMIT 1"
    ).fetchone()
    return _automation_edition(row) if row else None


def automation_edition_dates(conn: sqlite3.Connection) -> list[str]:
    """Return automation刊期 dates, newest first, for Admin selection."""
    rows = conn.execute(
        "SELECT edition_date FROM automation_editions ORDER BY edition_date DESC"
    ).fetchall()
    return [row["edition_date"] for row in rows]


def automation_problem_dates(conn: sqlite3.Connection) -> list[str]:
    """Return dates with translation tasks requiring attention, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT edition_date FROM translation_tasks "
        "WHERE status IN ('pending', 'running', 'failed', 'retry_wait', "
        "'configuration_blocked', 'cancelled') "
        "ORDER BY edition_date DESC"
    ).fetchall()
    return [row["edition_date"] for row in rows]


def ensure_automation_edition(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    target_count: int,
    now: str,
) -> AutomationEdition:
    _validate_test_attempt_date(edition_date)
    if type(target_count) is not int or target_count < 0:
        raise ValueError("target_count must be a non-negative integer")
    now = _automation_timestamp(now)
    with conn:
        conn.execute(
            "INSERT INTO automation_editions"
            " (edition_date, status, target_count, created_at, updated_at)"
            " VALUES (?, 'translating', ?, ?, ?)"
            " ON CONFLICT(edition_date) DO UPDATE SET target_count = excluded.target_count,"
            " updated_at = excluded.updated_at",
            (edition_date, target_count, now, now),
        )
    edition = automation_edition(conn, edition_date)
    if edition is None:
        raise RuntimeError("automation edition was not created")
    return edition


def unfinished_automation_edition_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT edition_date FROM automation_editions"
        " WHERE status NOT IN ('delivered', 'delivery_pending')"
        " AND NOT (status = 'complete' AND last_error_code = 'DELIVERY_EXPIRED')"
        " ORDER BY edition_date DESC"
    ).fetchall()
    return [row["edition_date"] for row in rows]


def pending_automation_build_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT edition_date FROM automation_editions"
        " WHERE dirty_generation > built_generation ORDER BY edition_date"
    ).fetchall()
    return [row["edition_date"] for row in rows]


def expire_automation_deliveries_before(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    now: str,
) -> int:
    _validate_test_attempt_date(edition_date)
    now = _automation_timestamp(now)
    with conn:
        cursor = conn.execute(
            "UPDATE automation_editions SET last_error_code = 'DELIVERY_EXPIRED',"
            " delivery_finished_at = ?, updated_at = ?"
            " WHERE edition_date < ? AND status = 'complete' AND delivery_key IS NULL"
            " AND COALESCE(last_error_code, '') != 'DELIVERY_EXPIRED'",
            (now, now, edition_date),
        )
    return cursor.rowcount


def mark_translation_ready_for_build(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: str,
    debounce_seconds: int = 2,
) -> AutomationEdition:
    now = _automation_timestamp(now)
    if type(debounce_seconds) is not int or not 0 <= debounce_seconds <= 60:
        raise ValueError("debounce_seconds must be between 0 and 60")
    try:
        conn.execute("BEGIN IMMEDIATE")
        task_row = conn.execute(
            "SELECT edition_date, status, success_generation FROM translation_tasks"
            " WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None or task_row["status"] != "succeeded":
            raise RuntimeError("only a succeeded task can enter build")
        edition_row = conn.execute(
            "SELECT dirty_generation FROM automation_editions WHERE edition_date = ?",
            (task_row["edition_date"],),
        ).fetchone()
        if edition_row is None:
            raise RuntimeError("automation edition does not exist")
        if task_row["success_generation"] is None:
            generation = edition_row["dirty_generation"] + 1
            conn.execute(
                "UPDATE translation_tasks SET success_generation = ?,"
                " build_status = 'build_pending', current_stage = 'waiting_build',"
                " failure_stage = NULL, updated_at = ?"
                " WHERE task_id = ?",
                (generation, now, task_id),
            )
            conn.execute(
                "UPDATE automation_editions SET status ="
                " CASE WHEN status = 'building' THEN 'building' ELSE 'build_pending' END,"
                " dirty_generation = ?, build_not_before = ?,"
                " succeeded_count = (SELECT COUNT(*) FROM translation_tasks"
                "   WHERE edition_date = ? AND status = 'succeeded'),"
                " last_error_code = NULL, updated_at = ? WHERE edition_date = ?",
                (
                    generation,
                    _future_timestamp(now, debounce_seconds),
                    task_row["edition_date"],
                    now,
                    task_row["edition_date"],
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    edition = automation_edition(conn, task_row["edition_date"])
    if edition is None:
        raise RuntimeError("automation edition does not exist")
    return edition


def claim_automation_build(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    owner: str,
    now: str,
    lease_seconds: int,
    force: bool = False,
) -> AutomationEdition | None:
    _validate_test_attempt_date(edition_date)
    owner = _non_empty(owner, "owner", maximum=128)
    now = _automation_timestamp(now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, dirty_generation, built_generation, build_not_before"
            " FROM automation_editions WHERE edition_date = ?",
            (edition_date,),
        ).fetchone()
        due = row is not None and (
            force or row["build_not_before"] is None or row["build_not_before"] <= now
        )
        if (
            row is None
            or row["status"] == "building"
            or row["dirty_generation"] <= row["built_generation"]
            or not due
        ):
            conn.commit()
            return None
        conn.execute(
            "UPDATE automation_editions SET status = 'building', building_generation = ?,"
            " build_owner = ?, build_lease_expires_at = ?, updated_at = ?"
            " WHERE edition_date = ? AND status != 'building'",
            (
                row["dirty_generation"],
                owner,
                _future_timestamp(now, lease_seconds),
                now,
                edition_date,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return automation_edition(conn, edition_date)


def finish_automation_build(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    owner: str,
    now: str,
    succeeded: bool,
) -> AutomationEdition:
    now = _automation_timestamp(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT target_count, dirty_generation, building_generation"
            " FROM automation_editions"
            " WHERE edition_date = ? AND status = 'building' AND build_owner = ?",
            (edition_date, owner),
        ).fetchone()
        if row is None or row["building_generation"] is None:
            raise RuntimeError("automation build is not owned by this worker")
        if not succeeded:
            conn.execute(
                "UPDATE automation_editions SET status = 'build_failed',"
                " building_generation = NULL, build_owner = NULL,"
                " build_lease_expires_at = NULL, last_error_code = 'BUILD_FAILED',"
                " updated_at = ? WHERE edition_date = ?",
                (now, edition_date),
            )
        else:
            generation = row["building_generation"]
            conn.execute(
                "UPDATE translation_tasks SET build_status = 'online',"
                " current_stage = 'online', failure_stage = NULL, updated_at = ?"
                " WHERE edition_date = ? AND status = 'succeeded'"
                " AND success_generation <= ?",
                (now, edition_date, generation),
            )
            counts = conn.execute(
                "SELECT"
                " SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded_count,"
                " SUM(CASE WHEN build_status = 'online' THEN 1 ELSE 0 END) AS online_count"
                " FROM translation_tasks WHERE edition_date = ?",
                (edition_date,),
            ).fetchone()
            succeeded_count = int(counts["succeeded_count"] or 0)
            online_count = int(counts["online_count"] or 0)
            complete = (
                succeeded_count == row["target_count"]
                and online_count == row["target_count"]
                and generation == row["dirty_generation"]
            )
            status = "complete" if complete else (
                "build_pending" if generation < row["dirty_generation"] else "partial"
            )
            conn.execute(
                "UPDATE automation_editions SET status = ?, succeeded_count = ?,"
                " online_count = ?, built_generation = ?, building_generation = NULL,"
                " build_not_before = CASE WHEN ? < dirty_generation THEN ? ELSE NULL END,"
                " build_owner = NULL, build_lease_expires_at = NULL, last_error_code = NULL,"
                " updated_at = ? WHERE edition_date = ?",
                (
                    status,
                    succeeded_count,
                    online_count,
                    generation,
                    generation,
                    now,
                    now,
                    edition_date,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    edition = automation_edition(conn, edition_date)
    if edition is None:
        raise RuntimeError("automation edition does not exist")
    return edition


def claim_automation_delivery(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    now: str,
) -> str | None:
    now = _automation_timestamp(now)
    delivery_key = hashlib.sha256(f"automation-delivery\n{edition_date}".encode()).hexdigest()
    with conn:
        cursor = conn.execute(
            "UPDATE automation_editions SET status = 'delivery_pending', delivery_key = ?,"
            " delivery_started_at = ?, updated_at = ?"
            " WHERE edition_date = ? AND status = 'complete' AND delivery_key IS NULL",
            (delivery_key, now, now, edition_date),
        )
    return delivery_key if cursor.rowcount == 1 else None


def finish_automation_delivery(
    conn: sqlite3.Connection,
    edition_date: str,
    *,
    delivery_key: str,
    now: str,
    succeeded: bool,
) -> AutomationEdition:
    _validate_test_attempt_digest(delivery_key, "delivery_key")
    now = _automation_timestamp(now)
    with conn:
        cursor = conn.execute(
            "UPDATE automation_editions SET status = ?, delivery_finished_at = ?,"
            " last_error_code = ?, updated_at = ?"
            " WHERE edition_date = ? AND status = 'delivery_pending' AND delivery_key = ?",
            (
                "delivered" if succeeded else "complete",
                now,
                None if succeeded else "DELIVERY_FAILED",
                now,
                edition_date,
                delivery_key,
            ),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("automation delivery claim is not active")
    if not succeeded:
        with conn:
            conn.execute(
                "UPDATE automation_editions SET delivery_key = NULL, delivery_started_at = NULL"
                " WHERE edition_date = ?",
                (edition_date,),
            )
    edition = automation_edition(conn, edition_date)
    if edition is None:
        raise RuntimeError("automation edition does not exist")
    return edition


def _provider_circuit(row: sqlite3.Row) -> ProviderCircuit:
    return ProviderCircuit(
        provider_id=row["provider_id"],
        state=row["state"],
        consecutive_failures=row["consecutive_failures"],
        open_count=row["open_count"],
        recovery_successes=row["recovery_successes"],
        recovery_mode=bool(row["recovery_mode"]),
        opened_at=row["opened_at"],
        next_probe_at=row["next_probe_at"],
        probe_task_id=row["probe_task_id"],
        probe_owner=row["probe_owner"],
        probe_lease_expires_at=row["probe_lease_expires_at"],
        last_outcome=row["last_outcome"],
        updated_at=row["updated_at"],
    )


def _provider_circuit_select() -> str:
    return (
        "SELECT provider_id, state, consecutive_failures, open_count, recovery_successes,"
        " recovery_mode,"
        " opened_at, next_probe_at, probe_task_id, probe_owner, probe_lease_expires_at,"
        " last_outcome, updated_at FROM provider_circuits"
    )


def get_provider_circuit(
    conn: sqlite3.Connection, provider_id: str
) -> ProviderCircuit | None:
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    row = conn.execute(
        _provider_circuit_select() + " WHERE provider_id = ?", (provider_id,)
    ).fetchone()
    return _provider_circuit(row) if row else None


def _ensure_provider_circuit(
    conn: sqlite3.Connection, provider_id: str, now: str
) -> ProviderCircuit:
    conn.execute(
        "INSERT OR IGNORE INTO provider_circuits"
        " (provider_id, state, updated_at) VALUES (?, 'closed', ?)",
        (provider_id, now),
    )
    row = conn.execute(
        _provider_circuit_select() + " WHERE provider_id = ?", (provider_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("provider circuit was not created")
    return _provider_circuit(row)


def _circuit_cooldown(open_count: int) -> int:
    return _CIRCUIT_COOLDOWNS[min(max(open_count, 1), len(_CIRCUIT_COOLDOWNS)) - 1]


def record_provider_outcome(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    outcome: ProviderOutcome,
    now: str,
) -> ProviderCircuit:
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    now = _automation_timestamp(now)
    if outcome not in {
        "success",
        "provider_failure",
        "content_failure",
        "configuration_failure",
    }:
        raise ValueError("provider outcome is invalid")
    try:
        conn.execute("BEGIN IMMEDIATE")
        circuit = _ensure_provider_circuit(conn, provider_id, now)
        if circuit.state == "half_open":
            raise RuntimeError("half-open outcome must finish the active probe")
        state = circuit.state
        failures = circuit.consecutive_failures
        open_count = circuit.open_count
        recovery_successes = circuit.recovery_successes
        recovery_mode = circuit.recovery_mode
        opened_at = circuit.opened_at
        next_probe_at = circuit.next_probe_at
        if outcome == "success":
            state = "closed"
            failures = 0
            open_count = 0
            if recovery_mode:
                recovery_successes += 1
                recovery_mode = recovery_successes < 2
            else:
                recovery_successes = 0
            opened_at = None
            next_probe_at = None
        elif outcome == "content_failure":
            pass
        elif outcome == "configuration_failure":
            state = "configuration_blocked"
            opened_at = now
            next_probe_at = None
            recovery_successes = 0
            recovery_mode = False
        elif state == "closed":
            failures += 1
            recovery_successes = 0
            recovery_mode = False
            if failures >= 5:
                state = "open"
                open_count += 1
                opened_at = now
                next_probe_at = _future_timestamp(now, _circuit_cooldown(open_count))
        conn.execute(
            "UPDATE provider_circuits SET state = ?, consecutive_failures = ?,"
            " open_count = ?, recovery_successes = ?, recovery_mode = ?, opened_at = ?,"
            " next_probe_at = ?,"
            " last_outcome = ?, updated_at = ? WHERE provider_id = ?",
            (
                state,
                failures,
                open_count,
                recovery_successes,
                int(recovery_mode),
                opened_at,
                next_probe_at,
                outcome,
                now,
                provider_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result = get_provider_circuit(conn, provider_id)
    if result is None:
        raise RuntimeError("provider circuit does not exist")
    return result


def claim_provider_probe(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    task_id: str,
    owner: str,
    now: str,
    lease_seconds: int,
    manual: bool = False,
) -> bool:
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    _validate_test_attempt_digest(task_id, "task_id")
    owner = _non_empty(owner, "owner", maximum=128)
    now = _automation_timestamp(now)
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    try:
        conn.execute("BEGIN IMMEDIATE")
        circuit = _ensure_provider_circuit(conn, provider_id, now)
        due = circuit.next_probe_at is not None and circuit.next_probe_at <= now
        if circuit.state not in {"open", "configuration_blocked"} or not (manual or due):
            conn.commit()
            return False
        task_exists = conn.execute(
            "SELECT 1 FROM translation_tasks WHERE task_id = ? AND provider_id = ?",
            (task_id, provider_id),
        ).fetchone()
        if task_exists is None:
            raise RuntimeError("probe task does not exist for this provider")
        cursor = conn.execute(
            "UPDATE provider_circuits SET state = 'half_open', probe_task_id = ?,"
            " probe_owner = ?, probe_lease_expires_at = ?, last_outcome = NULL, updated_at = ?"
            " WHERE provider_id = ? AND state IN ('open', 'configuration_blocked')",
            (task_id, owner, _future_timestamp(now, lease_seconds), now, provider_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.rowcount == 1


def finish_provider_probe(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    owner: str,
    outcome: ProviderOutcome,
    now: str,
) -> ProviderCircuit:
    if outcome not in {
        "success",
        "provider_failure",
        "content_failure",
        "configuration_failure",
    }:
        raise ValueError("provider outcome is invalid")
    now = _automation_timestamp(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        circuit = _ensure_provider_circuit(conn, provider_id, now)
        if circuit.state != "half_open" or circuit.probe_owner != owner:
            raise RuntimeError("provider probe is not owned by this worker")
        if outcome in {"success", "content_failure"}:
            state = "closed"
            failures = 0
            open_count = 0
            recovery_successes = 0
            recovery_mode = True
            opened_at = None
            next_probe_at = None
        elif outcome == "configuration_failure":
            state = "configuration_blocked"
            failures = circuit.consecutive_failures
            open_count = circuit.open_count
            recovery_successes = 0
            recovery_mode = False
            opened_at = now
            next_probe_at = None
        else:
            state = "open"
            failures = circuit.consecutive_failures + 1
            open_count = circuit.open_count + 1
            recovery_successes = 0
            recovery_mode = False
            opened_at = now
            next_probe_at = _future_timestamp(now, _circuit_cooldown(open_count))
        conn.execute(
            "UPDATE provider_circuits SET state = ?, consecutive_failures = ?,"
            " open_count = ?, recovery_successes = ?, recovery_mode = ?, opened_at = ?,"
            " next_probe_at = ?,"
            " probe_task_id = NULL, probe_owner = NULL, probe_lease_expires_at = NULL,"
            " last_outcome = ?, updated_at = ? WHERE provider_id = ?",
            (
                state,
                failures,
                open_count,
                recovery_successes,
                int(recovery_mode),
                opened_at,
                next_probe_at,
                outcome,
                now,
                provider_id,
            ),
        )
        if outcome in {"success", "content_failure"}:
            conn.execute(
                "UPDATE translation_tasks SET status = 'pending', auto_retry = 1,"
                " next_retry_at = NULL, error_code = NULL, error_category = NULL,"
                " failure_stage = NULL, finished_at = NULL, updated_at = ?"
                " WHERE provider_id = ? AND status = 'configuration_blocked'",
                (now, provider_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result = get_provider_circuit(conn, provider_id)
    if result is None:
        raise RuntimeError("provider circuit does not exist")
    return result


def release_provider_probe(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    owner: str,
    now: str,
) -> bool:
    """Release a probe lease when its task could not be claimed; no provider call occurred."""
    now = _automation_timestamp(now)
    with conn:
        cursor = conn.execute(
            "UPDATE provider_circuits SET state = 'open', probe_task_id = NULL,"
            " probe_owner = NULL, probe_lease_expires_at = NULL, updated_at = ?"
            " WHERE provider_id = ? AND state = 'half_open' AND probe_owner = ?",
            (now, provider_id, owner),
        )
    return cursor.rowcount == 1


def clear_provider_configuration_block(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    now: str,
    controlled_test_succeeded: bool,
) -> bool:
    if not controlled_test_succeeded:
        return False
    now = _automation_timestamp(now)
    with conn:
        cursor = conn.execute(
            "UPDATE provider_circuits SET state = 'closed', consecutive_failures = 0,"
            " open_count = 0, recovery_successes = 0, recovery_mode = 0, opened_at = NULL,"
            " next_probe_at = NULL,"
            " probe_task_id = NULL, probe_owner = NULL, probe_lease_expires_at = NULL,"
            " last_outcome = 'success', updated_at = ?"
            " WHERE provider_id = ? AND state = 'configuration_blocked'",
            (now, provider_id),
        )
        circuit_changed = cursor.rowcount == 1
        if circuit_changed:
            conn.execute(
                "UPDATE translation_tasks SET status = 'pending', auto_retry = 1,"
                " next_retry_at = NULL, error_code = NULL, error_category = NULL,"
                " failure_stage = NULL, finished_at = NULL, updated_at = ?"
                " WHERE provider_id = ? AND status = 'configuration_blocked'",
                (now, provider_id),
            )
    return circuit_changed


def unblock_provider_configuration(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    task_id: str,
    now: str,
    actor: str,
) -> tuple[str, bool]:
    """Close a verified configuration circuit and audit the recovery command."""
    provider_id = _non_empty(provider_id, "provider_id", maximum=128)
    _validate_test_attempt_digest(task_id, "task_id")
    actor = _non_empty(actor, "actor", maximum=128)
    now = _automation_timestamp(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        circuit = conn.execute(
            "SELECT state FROM provider_circuits WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        if circuit is None:
            raise RuntimeError("provider circuit does not exist")
        task = conn.execute(
            "SELECT status FROM translation_tasks WHERE task_id = ? AND provider_id = ?",
            (task_id, provider_id),
        ).fetchone()
        if task is None:
            raise RuntimeError("translation task does not exist")
        existing = conn.execute(
            "SELECT action_id FROM translation_admin_actions"
            " WHERE provider_id = ? AND action = 'unblock'"
            " AND status IN ('requested', 'running') ORDER BY requested_at DESC LIMIT 1",
            (provider_id,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return existing["action_id"], True
        if circuit["state"] != "configuration_blocked":
            raise RuntimeError("provider configuration is not blocked")
        action_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO translation_admin_actions"
            " (action_id, task_id, provider_id, action, actor, status, requested_at)"
            " VALUES (?, ?, ?, 'unblock', ?, 'requested', ?)",
            (action_id, task_id, provider_id, actor, now),
        )
        conn.execute(
            "UPDATE provider_circuits SET state = 'closed', consecutive_failures = 0,"
            " open_count = 0, recovery_successes = 0, recovery_mode = 0,"
            " opened_at = NULL, next_probe_at = NULL, probe_task_id = NULL,"
            " probe_owner = NULL, probe_lease_expires_at = NULL,"
            " last_outcome = 'success', updated_at = ? WHERE provider_id = ?",
            (now, provider_id),
        )
        conn.execute(
            "UPDATE translation_tasks SET status = 'pending', auto_retry = 1,"
            " next_retry_at = NULL, error_code = NULL, error_category = NULL,"
            " failure_stage = NULL, finished_at = NULL, manual_action_id = NULL,"
            " updated_at = ? WHERE provider_id = ? AND status = 'configuration_blocked'",
            (now, provider_id),
        )
        conn.execute(
            "UPDATE translation_admin_actions SET status = 'completed',"
            " started_at = ?, finished_at = ?, result_code = 'UNBLOCKED'"
            " WHERE action_id = ?",
            (now, now, action_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return action_id, False


def queue_translation_task_recovery(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: str,
    actor: str,
) -> tuple[str, bool]:
    """Queue safe recovery after a cancellation lease has expired."""
    _validate_test_attempt_digest(task_id, "task_id")
    actor = _non_empty(actor, "actor", maximum=128)
    now = _automation_timestamp(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            "SELECT provider_id, status, cancel_requested_at, lease_expires_at"
            " FROM translation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise RuntimeError("translation task does not exist")
        if task["status"] != "running" or task["cancel_requested_at"] is None:
            raise RuntimeError("translation cancellation is not awaiting confirmation")
        if task["lease_expires_at"] is None or task["lease_expires_at"] > now:
            raise RuntimeError("translation cancellation is still being confirmed")
        existing = conn.execute(
            "SELECT action_id FROM translation_admin_actions"
            " WHERE task_id = ? AND action = 'recover'"
            " AND status IN ('requested', 'running') ORDER BY requested_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return existing["action_id"], True
        action_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO translation_admin_actions"
            " (action_id, task_id, provider_id, action, actor, status, requested_at)"
            " VALUES (?, ?, ?, 'recover', ?, 'requested', ?)",
            (action_id, task_id, task["provider_id"], actor, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return action_id, False


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
