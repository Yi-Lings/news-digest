import datetime as dt
import http.client
import json
import threading
import time

from news_digest.admin_providers import provider_fingerprint, save_test_state
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
        conn.execute(
            "UPDATE automation_editions SET last_error_code = 'DELIVERY_EXPIRED'"
            " WHERE edition_date = '2026-07-28'"
        )
        conn.commit()
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
        assert payload["edition"]["error_code"] == "DELIVERY_EXPIRED"
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
        assert states[failed.task_id]["available_actions"] == ["retry"]
        assert states[running.task_id]["queue_state"] == "executing"
        assert states[running.task_id]["available_actions"] == ["cancel"]
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "safe-diagnostic" in encoded
        assert "provider raw response" not in encoded

        conn = db.connect(database)
        try:
            conn.execute(
                "UPDATE automation_editions SET last_error_code = ?"
                " WHERE edition_date = '2026-07-28'",
                ("provider raw response",),
            )
            conn.commit()
        finally:
            conn.close()
        status, redacted = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert redacted["edition"]["error_code"] == "UNKNOWN"
        assert "provider raw response" not in json.dumps(redacted, ensure_ascii=False)

        status, queued = _request(
            port,
            "POST",
            "/admin/api/translations/retry",
            {"task_id": failed.task_id},
        )
        assert status == 202 and queued["status"] == "retry_wait"
        assert isinstance(queued["action_id"], str)

        status, cancelled = _request(
            port,
            "POST",
            "/admin/api/translations/cancel",
            {"task_id": running.task_id, "confirm": True},
        )
        assert status == 202 and cancelled["cancel_requested"]
        assert isinstance(cancelled["action_id"], str)
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
        assert states[failed.task_id]["action"]["type"] == "retry"
        assert states[failed.task_id]["action"]["status"] == "requested"
        assert states[running.task_id]["action"]["type"] == "cancel"
        assert states[running.task_id]["action"]["status"] == "requested"
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_exposes_dispatch_for_pending_closed_task(tmp_path):
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        task = db.ensure_translation_task(
            conn,
            edition_date="2026-07-28",
            article_id="article-pending",
            article_title="Pending article",
            provider_id="provider-default",
            now=_at(),
        )
        db.ensure_automation_edition(conn, "2026-07-28", target_count=1, now=_at())
    finally:
        conn.close()
    wakeups = []
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        db_path=database,
        translation_wakeup_callback=lambda: wakeups.append("wake"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        item = payload["items"][0]
        assert item["queue_state"] == "waiting_dispatch"
        assert item["available_actions"] == ["dispatch"]

        status, queued = _request(
            port,
            "POST",
            "/admin/api/translations/dispatch",
            {"task_id": task.task_id},
        )
        assert status == 202
        assert queued["status"] == "pending"
        assert isinstance(queued["action_id"], str)
        assert wakeups == ["wake"]
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_keeps_pending_task_actionable_when_provider_blocked(tmp_path):
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        db.ensure_translation_task(
            conn,
            edition_date="2026-07-28",
            article_id="article-blocked-pending",
            article_title="Blocked pending article",
            provider_id="provider-default",
            now=_at(),
        )
        db.ensure_automation_edition(conn, "2026-07-28", target_count=1, now=_at())
        db.record_provider_outcome(
            conn, "provider-default", outcome="configuration_failure", now=_at(1)
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
        item = payload["items"][0]
        assert item["available_actions"] == ["unblock"]
        assert item["queue_state"] == "blocked"
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_selects_edition_by_date(tmp_path):
    database = tmp_path / "news.db"
    _seed(database)
    conn = db.connect(database)
    try:
        older = db.ensure_translation_task(
            conn,
            edition_date="2026-07-27",
            article_id="article-older",
            article_title="Older article",
            provider_id="provider-default",
            now=_at(),
        )
        db.ensure_automation_edition(conn, "2026-07-27", target_count=1, now=_at())
        db.claim_translation_task(
            conn, older.task_id, owner="older-worker", now=_at(), lease_seconds=60
        )
        db.finish_translation_task_failure(
            conn, older.task_id, owner="older-worker", now=_at(1),
            error_code="NETWORK_CONNECT_FAILED", error_category="provider_infrastructure",
            failure_stage="connect_provider", diagnostic_id="older-diagnostic"
        )
    finally:
        conn.close()
    server = create_server(tmp_path, tmp_path, 0, serve_static=False, db_path=database)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, latest = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert latest["edition"]["date"] == "2026-07-28"
        assert latest["edition_dates"] == ["2026-07-28", "2026-07-27"]
        status, older = _request(
            port, "GET", "/admin/api/translations?edition=2026-07-27"
        )
        assert status == 200
        assert older["edition"]["date"] == "2026-07-27"
        assert [item["title"] for item in older["items"]] == ["Older article"]
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_keeps_running_edition_visible(tmp_path):
    database = tmp_path / "news.db"
    _seed(database)
    server = create_server(tmp_path, tmp_path, 0, serve_static=False, db_path=database)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert payload["edition_dates"] == ["2026-07-28"]
        assert payload["edition"]["date"] == "2026-07-28"
        assert payload["summary"]["running"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_shows_latest_completed_edition_when_no_problems(tmp_path):
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        task = db.ensure_translation_task(
            conn,
            edition_date="2026-07-28",
            article_id="article-complete",
            article_title="Completed article",
            provider_id="provider-default",
            now=_at(),
        )
        db.ensure_automation_edition(conn, "2026-07-28", target_count=1, now=_at())
        db.claim_translation_task(
            conn, task.task_id, owner="complete-worker", now=_at(), lease_seconds=60
        )
        db.finish_translation_task_success(
            conn, task.task_id, owner="complete-worker", now=_at(1)
        )
        db.mark_translation_ready_for_build(
            conn, task.task_id, now=_at(1), debounce_seconds=0
        )
        db.claim_automation_build(
            conn, "2026-07-28", owner="complete-builder", now=_at(1), lease_seconds=60
        )
        db.finish_automation_build(
            conn, "2026-07-28", owner="complete-builder", now=_at(2), succeeded=True
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
        assert payload["edition"]["date"] == "2026-07-28"
        assert payload["edition_dates"] == []
        assert payload["summary"]["online"] == 1
        assert payload["items"][0]["build_status"] == "online"
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

    wakeups = []
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        db_path=database,
        translation_wakeup_callback=lambda: wakeups.append("wake"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert payload["provider"]["state"] == "open"
        assert payload["probe_task_id"] == failed.task_id

        body = {"task_id": failed.task_id, "confirm": True}
        first_status, first = _request(
            port, "POST", "/admin/api/translations/probe", body
        )
        second_status, second = _request(
            port, "POST", "/admin/api/translations/probe", body
        )
        assert first_status == second_status == 202
        assert first["already_queued"] is False
        assert second["already_queued"] is True
        assert first["action_id"] == second["action_id"]
        assert wakeups == ["wake", "wake"]
        conn = db.connect(database)
        try:
            action_count = conn.execute(
                "SELECT COUNT(*) FROM translation_admin_actions WHERE action = 'probe'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert action_count == 1

        status, event = _request(port, "GET", "/admin/api/translations/events")
        assert status == 200
        assert "event: translation-state" in event
        assert "safe-diagnostic" in event
        assert "provider raw response" not in event
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_unblock_requires_recent_matching_provider_test(tmp_path):
    database = tmp_path / "news.db"
    failed, _ = _seed(database)
    conn = db.connect(database)
    try:
        db.claim_translation_task(
            conn, failed.task_id, owner="config-worker", now=_at(2),
            lease_seconds=30, manual=True
        )
        db.finish_translation_task_failure(
            conn,
            failed.task_id,
            owner="config-worker",
            now=_at(3),
            error_code="CONFIGURATION_INVALID",
            error_category="configuration",
            failure_stage="connect_provider",
            diagnostic_id="config-diagnostic",
            auto_retry=False,
        )
        db.record_provider_outcome(
            conn, "provider-default", outcome="configuration_failure", now=_at()
        )
    finally:
        conn.close()
    provider = {
        "name": "provider-default",
        "base_url": "https://provider.example/v1",
        "api_key": "redacted-key",
        "model": "gpt-test",
        "api_type": "openai_chat",
        "stream": True,
        "enabled": True,
        "is_default": True,
    }
    (tmp_path / ".env.providers.local").write_text(
        json.dumps({"providers": {provider["name"]: provider}}), encoding="utf-8"
    )
    save_test_state(
        tmp_path,
        provider["name"],
        {
            "status": "success",
            "tested_at_epoch": time.time(),
            "fingerprint": provider_fingerprint(tmp_path, provider),
        },
    )
    wakeups = []
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        db_path=database,
        translation_wakeup_callback=lambda: wakeups.append("wake"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(
            port,
            "POST",
            "/admin/api/translations/unblock",
            {"task_id": failed.task_id, "confirm": True},
        )
        assert status == 202
        assert isinstance(payload["action_id"], str)
        assert payload["already_queued"] is False
        assert wakeups == ["wake"]
        conn = db.connect(database)
        try:
            task = db.translation_task(conn, failed.task_id)
            circuit = db.get_provider_circuit(conn, "provider-default")
            action = conn.execute(
                "SELECT action, status, result_code FROM translation_admin_actions"
                " WHERE action_id = ?",
                (payload["action_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert task.status == "pending"
        assert circuit.state == "closed"
        assert tuple(action) == ("unblock", "completed", "UNBLOCKED")
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_exposes_recovery_after_cancel_lease_expiry(tmp_path):
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        task = db.ensure_translation_task(
            conn,
            edition_date="2026-07-28",
            article_id="article-cancelled",
            article_title="Cancelled article",
            provider_id="provider-default",
            now=_at(),
        )
        db.ensure_automation_edition(conn, "2026-07-28", target_count=1, now=_at())
        db.claim_translation_task(
            conn, task.task_id, owner="worker-a", now=_at(), lease_seconds=10
        )
        db.request_translation_task_cancel(conn, task.task_id, now=_at(1), actor="admin")
    finally:
        conn.close()
    wakeups = []
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        db_path=database,
        clock=lambda: dt.datetime.fromisoformat(_at(12)).timestamp(),
        translation_wakeup_callback=lambda: wakeups.append("wake"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, payload = _request(port, "GET", "/admin/api/translations")
        assert status == 200
        assert payload["items"][0]["available_actions"] == ["recover"]
        status, result = _request(
            port,
            "POST",
            "/admin/api/translations/recover",
            {"task_id": task.task_id, "confirm": True},
        )
        assert status == 202
        assert result["already_queued"] is False
        assert isinstance(result["action_id"], str)
        assert wakeups == ["wake"]
    finally:
        server.shutdown()
        server.server_close()


def test_translation_admin_dom_contract_and_reduced_motion():
    assert ADMIN_HTML.index('data-tab="users"') < ADMIN_HTML.index(
        'data-tab="site"'
    ) < ADMIN_HTML.index(
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
        '"/admin/api/translations/dispatch"',
        '"/admin/api/translations/cancel"',
        '"/admin/api/translations/probe"',
        '"/admin/api/translations/recover"',
        'item.available_actions || []',
        "queueTranslationProbe(item, event.currentTarget)",
        "立即调度",
        "解除阻断并重试",
        'item.action.status',
        "探测已在队列，已重新唤醒 worker。",
        'new EventSource("/admin/api/translations/events")',
        "translationPoll = setInterval(loadTranslations, 3000)",
        "prefers-reduced-motion: reduce",
        "-webkit-line-clamp: 2",
        "waiting_dispatch: \"等待调度\"",
        "waiting_backoff: \"失败退避\"",
        "waiting_cancel_confirmation: \"等待终止确认\"",
        "waiting_probe: \"等待熔断探测\"",
        "下一次执行",
        'edition.error_code ? " · 错误 " + edition.error_code : ""',
    ]:
        assert token in ADMIN_HTML
