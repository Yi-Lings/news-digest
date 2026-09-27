import datetime as dt
import json

from news_digest import operations
from news_digest.storage import db

NOW = dt.datetime(2026, 9, 27, 12, tzinfo=dt.UTC)


def test_business_status_reports_only_actionable_translation_faults(tmp_path):
    database = tmp_path / "news.db"
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("ready")
    conn = db.connect(database)
    old = "2026-09-27T11:00:00+00:00"
    future = "2026-09-27T13:00:00+00:00"
    for task_id, provider_id, status, retry in (
        ("due", "provider-due", "retry_wait", old),
        ("future", "provider-future", "retry_wait", future),
        ("terminal", "provider-terminal", "failed", None),
    ):
        conn.execute(
            "INSERT INTO translation_tasks"
            " (task_id, edition_date, article_id, article_title, provider_id, status,"
            " next_retry_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "2026-09-27", task_id, task_id, provider_id, status, retry, old, old),
        )
        conn.execute(
            "INSERT INTO provider_circuits"
            " (provider_id, state, next_probe_at, updated_at) VALUES (?,?,?,?)",
            (provider_id, "open", old, old),
        )
    conn.execute(
        "INSERT INTO translation_admin_actions"
        " (action_id, task_id, provider_id, action, actor, status, requested_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("stale", "due", "provider-due", "retry", "admin", "requested", old),
    )
    for number, started in ((1, old), (2, "2026-09-25T10:00:00+00:00")):
        conn.execute(
            "INSERT INTO translation_attempts"
            " (task_id, attempt_number, owner, kind, status, started_at, http_status)"
            " VALUES (?,?,?,?,?,?,?)",
            ("due", number, "worker", "automatic", "failed", started, 504),
        )
    conn.execute(
        "INSERT INTO translation_attempts"
        " (task_id, attempt_number, owner, kind, status, started_at, http_status, timings_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            "due", 3, "worker", "automatic", "succeeded", old, 200,
            json.dumps({"requests": [{"http_status": 504}, {"http_status": 200}]}),
        ),
    )
    conn.commit()
    conn.close()

    result = operations.business_status(database, site, timezone="UTC", now=NOW)
    assert result["translation_health"] == {
        "probe_overdue": 1,
        "retry_action_stale": 1,
        "provider_504_recent": 2,
    }
    assert result["checks"]["probe_overdue"]
    assert result["checks"]["retry_action_stale"]
    assert "provider_504_recent" not in result["checks"]

    conn = db.connect(database)
    conn.execute("UPDATE translation_tasks SET next_retry_at = ? WHERE task_id = 'due'", (future,))
    conn.execute(
        "UPDATE translation_admin_actions SET status = 'completed' WHERE action_id = 'stale'"
    )
    conn.execute("UPDATE translation_attempts SET started_at = ?", ("2026-09-25T10:00:00+00:00",))
    conn.commit()
    conn.close()
    result = operations.business_status(database, site, timezone="UTC", now=NOW)
    assert result["translation_health"] == {
        "probe_overdue": 0,
        "retry_action_stale": 0,
        "provider_504_recent": 0,
    }
