"""Persistent, redacted idempotency state for Admin test-message delivery."""

import hashlib
import sqlite3

import pytest

from news_digest.storage import db

NOW = "2026-07-28T08:00:00+00:00"
LATER = "2026-07-28T08:01:00+00:00"
EDITION = "2026-07-28"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_schema_v3_migrates_to_redacted_test_attempt_store(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES ('schema_version', '3');"
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0] == str(db.SCHEMA_VERSION)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(email_test_attempts)").fetchall()
    }
    assert columns == {
        "key_hash",
        "request_fingerprint",
        "edition_date",
        "status",
        "sent_count",
        "failed_count",
        "unknown_count",
        "skipped_count",
        "total_count",
        "error_category",
        "error_stage",
        "retry_allowed",
        "next_action",
        "created_at",
        "updated_at",
    }
    conn.close()


def test_begin_is_atomic_for_same_key_and_unresolved_fingerprint(tmp_path):
    path = tmp_path / "news.db"
    key_a = _digest("idempotency-key-a")
    key_b = _digest("idempotency-key-b")
    fingerprint = _digest("edition plus redacted recipient/configuration identity")
    first = db.connect(path)
    second = db.connect(path)

    started = db.begin_test_attempt(first, key_a, fingerprint, EDITION, 2, NOW)
    assert started.disposition == "started"
    assert started.attempt.status == "running"
    assert started.attempt.total_count == 2

    same_key = db.begin_test_attempt(second, key_a, fingerprint, EDITION, 2, NOW)
    assert same_key.disposition == "existing"
    assert same_key.attempt == started.attempt

    new_key = db.begin_test_attempt(second, key_b, fingerprint, EDITION, 2, NOW)
    assert new_key.disposition == "blocked"
    assert new_key.attempt.key_hash == key_a
    assert db.test_attempt_by_key_hash(second, key_b) is None
    assert db.latest_test_attempt_by_fingerprint(second, fingerprint) == started.attempt
    first.close()
    second.close()


def test_unknown_finish_persists_and_blocks_same_or_new_key_after_reconnect(tmp_path):
    path = tmp_path / "news.db"
    key_a = _digest("idempotency-key-a")
    key_b = _digest("idempotency-key-b")
    fingerprint = _digest("same test request")
    conn = db.connect(path)
    db.begin_test_attempt(conn, key_a, fingerprint, EDITION, 1, NOW)
    attempt = db.finish_test_attempt(
        conn,
        key_a,
        "unknown",
        LATER,
        sent_count=0,
        failed_count=0,
        unknown_count=1,
        skipped_count=0,
        error_category="timeout",
        error_stage="data_final_response",
        retry_allowed=False,
        next_action="wait_and_verify_delivery",
    )
    assert attempt.status == "unknown"
    assert attempt.retry_allowed is False
    conn.close()

    reopened = db.connect(path)
    same_key = db.begin_test_attempt(reopened, key_a, fingerprint, EDITION, 1, LATER)
    new_key = db.begin_test_attempt(reopened, key_b, fingerprint, EDITION, 1, LATER)
    assert same_key.disposition == "existing"
    assert same_key.attempt == attempt
    assert new_key.disposition == "blocked"
    assert new_key.attempt == attempt
    reopened.close()


def test_recover_interrupted_running_attempt_is_conservative_and_persistent(tmp_path):
    path = tmp_path / "news.db"
    key_a = _digest("idempotency-key-a")
    key_b = _digest("idempotency-key-b")
    fingerprint = _digest("same request after server restart")
    conn = db.connect(path)
    db.begin_test_attempt(conn, key_a, fingerprint, EDITION, 3, NOW)
    conn.close()

    restarted = db.connect(path)
    assert db.recover_interrupted_test_attempts(restarted, LATER) == 1
    recovered = db.test_attempt_by_key_hash(restarted, key_a)
    assert recovered is not None
    assert recovered.status == "unknown"
    assert recovered.unknown_count == 3
    assert recovered.retry_allowed is False
    assert recovered.next_action == "wait_and_verify_delivery"
    assert recovered.error_category == "worker_interrupted"
    assert recovered.error_stage == "unknown"
    blocked = db.begin_test_attempt(restarted, key_b, fingerprint, EDITION, 3, LATER)
    assert blocked.disposition == "blocked"
    restarted.close()

    reopened = db.connect(path)
    assert db.test_attempt_by_key_hash(reopened, key_a) == recovered
    reopened.close()


def test_terminal_known_attempt_allows_new_key_for_same_fingerprint(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    key_a = _digest("idempotency-key-a")
    key_b = _digest("idempotency-key-b")
    fingerprint = _digest("retryable deterministic failure")
    db.begin_test_attempt(conn, key_a, fingerprint, EDITION, 2, NOW)
    db.finish_test_attempt(
        conn,
        key_a,
        "failed",
        LATER,
        sent_count=0,
        failed_count=2,
        unknown_count=0,
        skipped_count=0,
        error_category="authentication",
        error_stage="auth",
        retry_allowed=True,
        next_action="retry_test",
    )

    retried = db.begin_test_attempt(conn, key_b, fingerprint, EDITION, 2, LATER)
    assert retried.disposition == "started"
    assert [attempt.key_hash for attempt in db.test_attempts_by_fingerprint(conn, fingerprint)] == [
        key_b,
        key_a,
    ]
    conn.close()


def test_store_rejects_raw_identifiers_and_unredacted_error_fields(tmp_path):
    path = tmp_path / "news.db"
    conn = db.connect(path)
    fingerprint = _digest("safe fingerprint")
    with pytest.raises(ValueError, match="key_hash"):
        db.begin_test_attempt(conn, "raw-idempotency-key", fingerprint, EDITION, 1, NOW)

    key_hash = _digest("raw-idempotency-key")
    db.begin_test_attempt(conn, key_hash, fingerprint, EDITION, 1, NOW)
    with pytest.raises(ValueError, match="error_category"):
        db.finish_test_attempt(
            conn,
            key_hash,
            "failed",
            LATER,
            sent_count=0,
            failed_count=1,
            unknown_count=0,
            skipped_count=0,
            error_category="authentication: user@example.com secret-password",
            error_stage="auth",
            retry_allowed=True,
            next_action="retry_test",
        )
    with pytest.raises(ValueError, match="error_stage"):
        db.finish_test_attempt(
            conn,
            key_hash,
            "failed",
            LATER,
            sent_count=0,
            failed_count=1,
            unknown_count=0,
            skipped_count=0,
            error_category="authentication",
            error_stage="535 user@example.com secret-password",
            retry_allowed=True,
            next_action="retry_test",
        )
    conn.close()

    contents = path.read_bytes()
    for secret in (
        b"raw-idempotency-key",
        b"user@example.com",
        b"secret-password",
        b"535 ",
    ):
        assert secret not in contents

    reader = sqlite3.connect(path)
    assert reader.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    assert reader.execute("SELECT COUNT(*) FROM email_delivery_runs").fetchone()[0] == 0
    reader.close()
