import json
import sqlite3

import pytest

from news_digest.storage import db


def _v13_database(path):
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO translation_tasks"
        " (task_id, edition_date, article_id, article_title, provider_id, status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("old", "2026-09-27", "old", "old", "provider", "failed", "2026-09-27", "2026-09-27"),
    )
    conn.execute(
        "INSERT INTO translation_attempts"
        " (task_id, attempt_number, owner, kind, status, started_at, requests_json)"
        " VALUES (?,?,?,?,?,?,?)",
        ("old", 1, "worker", "automatic", "failed", "2026-09-27", "[]"),
    )
    conn.execute("ALTER TABLE translation_attempts DROP COLUMN http_status")
    conn.execute("ALTER TABLE translation_attempts DROP COLUMN timings_json")
    conn.execute("UPDATE meta SET value = '13' WHERE key = 'schema_version'")
    conn.commit()
    facts = db.database_facts(conn)
    conn.close()
    return facts


def test_v14_migration_preserves_attempts_and_creates_verified_backup(tmp_path):
    path = tmp_path / "news.db"
    facts = _v13_database(path)
    conn = db.connect(path)
    assert db.schema_is_current(conn)
    after = db.database_facts(conn)
    assert all(
        after[table] == value
        for table, value in facts.items()
        if table not in {"meta", "translation_attempts"}
    )
    assert after["translation_attempts"]["rows"] == facts["translation_attempts"]["rows"]
    attempt = conn.execute(
        "SELECT http_status, timings_json FROM translation_attempts WHERE task_id = 'old'"
    ).fetchone()
    assert tuple(attempt) == (None, None)
    conn.close()
    with sqlite3.connect(tmp_path / "news.db.pre-v14.bak") as backup:
        assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (
            "13",
        )
        assert db.database_facts(backup) == facts
    conn = db.connect(path)
    assert db.schema_is_current(conn)
    conn.close()


def test_v14_migration_rolls_back_interrupted_schema_change(tmp_path, monkeypatch):
    path = tmp_path / "news.db"
    facts = _v13_database(path)

    def fail(conn):
        conn.execute("ALTER TABLE translation_attempts ADD COLUMN http_status INTEGER")
        raise RuntimeError("injected")

    monkeypatch.setattr(db, "_apply_v14_schema", fail)
    with pytest.raises(RuntimeError, match="injected"):
        db.connect(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (
            "13",
        )
        assert "http_status" not in {
            row[1] for row in conn.execute("PRAGMA table_info(translation_attempts)")
        }
        assert db.database_facts(conn) == facts


def test_v14_migration_reaccepts_v13_marker_with_additive_columns(tmp_path):
    path = tmp_path / "news.db"
    _v13_database(path)
    conn = db.connect(path)
    conn.execute("UPDATE meta SET value = '13' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    assert db.schema_is_current(conn)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM translation_attempts").fetchone()[0] == 1
    conn.close()


def test_attempt_telemetry_persists_http_status_and_rejects_unsafe_fields(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    task_id = "a" * 64
    started = "2026-09-27T12:00:00+00:00"
    finished = "2026-09-27T12:01:00+00:00"
    conn.execute(
        "INSERT INTO translation_tasks"
        " (task_id, edition_date, article_id, article_title, provider_id, status,"
        " attempt_count, lease_owner, lease_expires_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id, "2026-09-27", "active", "active", "provider", "running",
            1, "worker", "2026-09-27T12:15:00+00:00", started, started,
        ),
    )
    conn.execute(
        "INSERT INTO translation_attempts"
        " (task_id, attempt_number, owner, kind, status, started_at)"
        " VALUES (?,?,?,?,?,?)",
        (task_id, 1, "worker", "automatic", "running", started),
    )
    conn.commit()
    timings = {
        "requests": [{
            "dns_ms": 12, "first_byte_ms": 90000, "stream_ms": 15,
            "total_ms": 90027, "http_status": 504,
        }],
        "attempt_total_ms": 90030,
        "validation_ms": None,
    }
    with pytest.raises(ValueError, match="invalid request timing fields"):
        db._safe_attempt_timings(
            {**timings, "requests": [{**timings["requests"][0], "body": "secret"}]}
        )
    db.finish_translation_task_failure(
        conn,
        task_id,
        owner="worker",
        now=finished,
        error_code="PROVIDER_5XX",
        error_category="provider_infrastructure",
        failure_stage="receiving_response",
        diagnostic_id="safe-id",
        http_status=504,
        timings=timings,
    )
    attempt = db.list_translation_attempts(conn, task_id)[0]
    assert attempt.http_status == 504
    assert json.loads(attempt.timings_json) == timings
    assert "secret" not in attempt.timings_json
    conn.close()
