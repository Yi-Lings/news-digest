import datetime as dt
import http.client
import json
import threading

from news_digest.preview_server import ADMIN_HTML, create_server
from news_digest.storage import db


def _at(seconds: int = 0) -> str:
    return (
        dt.datetime(2026, 7, 28, 0, 0, tzinfo=dt.UTC) + dt.timedelta(seconds=seconds)
    ).isoformat()


def _request(port: int, method: str, path: str, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    content_type = response.getheader("Content-Type", "")
    connection.close()
    if "application/json" in content_type:
        return response.status, json.loads(raw.decode())
    return response.status, raw.decode()


def _seed(database):
    conn = db.connect(database)
    try:
        failed = db.ensure_translation_task(
            conn,
            edition_date="2026-07-28",
            article_id="article-failed",
            article_title="A long but safe failed article title",
            provider_id="provider-default",
            now=_at(),
        )
        running = db.ensure_translation_task(
            conn,
            edition_date="2026-07-28",
            article_id="article-running",
            article_title="Running article",
            provider_id="provider-default",
            now=_at(),
        )
        db.ensure_automation_edition(
            conn, "2026-07-28", target_count=2, now=_at()
        )
        db.claim_translation_task(
            conn, failed.task_id, owner="worker-f", now=_at(), lease_seconds=60
        )
        db.finish_translation_task_failure(
            conn,
            failed.task_id,
            owner="worker-f",
            now=_at(1),
            error_code="NETWORK_CONNECT_FAILED",
            error_category="provider_infrastructure",
            failure_stage="connect_provider",
            diagnostic_id="safe-diagnostic",
        )
        db.claim_translation_task(
            conn, running.task_id, owner="worker-r", now=_at(), lease_seconds=60
        )
        return failed, running
    finally:
        conn.close()


def test_translation_admin_http_queues_actions_without_provider_calls(tmp_path):
    database = tmp_path / "news.db"
    failed, running = _seed(database)
    wakeups = []
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        db_path=database,
        translation_wakeup_callback=lambda: wakeups.append("wake"),
        clock=lambda: dt.datetime.fromisoformat(_at(2)).timestamp(),
        sensitive_limit=20,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert payload["edition"]["date"] == "2026-07-28"
        assert payload["summary"] == {
            "total": 2,
            "online": 0,
            "running": 1,
            "retry_wait": 1,
            "failed": 0,
        }
        assert payload["provider"]["current_concurrency"] == 1
        assert payload["provider"]["waiting_dispatch_count"] == 0
        assert payload["provider"]["waiting_backoff_count"] == 1
        assert payload["provider"]["waiting_cancel_count"] == 0
        assert payload["provider"]["waiting_probe_count"] == 0
        states = {item["task_id"]: item for item in payload["items"]}
        assert states[failed.task_id]["queue_state"] == "waiting_backoff"
        assert states[failed.task_id]["next_executable_at"] == _at(16)
        assert states[running.task_id]["queue_state"] == "executing"
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "safe-diagnostic" in encoded
        assert "provider raw response" not in encoded

        status, queued = _request(
            port,
            "POST",
            "/admin/api/translations/retry",
            {"task_id": failed.task_id},
        )
        assert status == 202 and queued["status"] == "retry_wait"

        status, cancelled = _request(
            port,
            "POST",
            "/admin/api/translations/cancel",
            {"task_id": running.task_id, "confirm": True},
        )
        assert status == 202 and cancelled["cancel_requested"]
        conn = db.connect(database)
        try:
            still_running = db.translation_task(conn, running.task_id)
        finally:
            conn.close()
        assert still_running.status == "running"
        assert still_running.cancel_requested_at is not None
        assert wakeups == ["wake", "wake"]

        status, updated = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert updated["provider"]["current_concurrency"] == 0
        assert updated["provider"]["waiting_dispatch_count"] == 1
        assert updated["provider"]["waiting_backoff_count"] == 0
        assert updated["provider"]["waiting_cancel_count"] == 1
        states = {item["task_id"]: item for item in updated["items"]}
        assert states[failed.task_id]["queue_state"] == "waiting_dispatch"
        assert states[running.task_id]["queue_state"] == "waiting_cancel_confirmation"
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_manual_probe_is_single_and_sse_is_redacted(tmp_path):
    database = tmp_path / "news.db"
    failed, _ = _seed(database)
    conn = db.connect(database)
    try:
        for second in range(2, 7):
            db.record_provider_outcome(
                conn,
                "provider-default",
                outcome="provider_failure",
                now=_at(second),
            )
    finally:
        conn.close()

    server = create_server(tmp_path, tmp_path, 0, serve_static=False, db_path=database)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert payload["provider"]["state"] == "open"
        assert payload["probe_task_id"] == failed.task_id

        body = {"task_id": failed.task_id, "confirm": True}
        assert _request(port, "POST", "/admin/api/translations/probe", body)[0] == 202
        assert _request(port, "POST", "/admin/api/translations/probe", body)[0] == 409

        status, event = _request(port, "GET", "/admin/api/translations/events")
        assert status == 200
        assert "event: translation-state" in event
        assert "safe-diagnostic" in event
        assert "provider raw response" not in event
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_dom_contract_and_reduced_motion():
    assert ADMIN_HTML.index('data-tab="subscriptions"') < ADMIN_HTML.index(
        'data-tab="translations"'
    ) < ADMIN_HTML.index('data-tab="delivery"')
    for token in [
        'id="translation-stats"',
        'id="translation-provider"',
        'id="translation-list"',
        'data-translation-filter="running"',
        'data-translation-filter="retry_wait"',
        'data-translation-filter="failed"',
        'data-translation-filter="online"',
        '"/admin/api/translations/retry"',
        '"/admin/api/translations/cancel"',
        '"/admin/api/translations/probe"',
        'new EventSource("/admin/api/translations/events")',
        "translationPoll = setInterval(loadTranslations, 3000)",
        "prefers-reduced-motion: reduce",
        "-webkit-line-clamp: 2",
        "waiting_dispatch: \"等待调度\"",
        "waiting_backoff: \"失败退避\"",
        "waiting_cancel_confirmation: \"等待终止确认\"",
        "waiting_probe: \"等待熔断探测\"",
        "下一次执行",
    ]:
        assert token in ADMIN_HTML
