import datetime as dt
import threading
import time

from news_digest.storage import db
from news_digest.translation.demo import TranslationAutomationDemo, build_demo_edition


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _tasks(database):
    conn = db.connect(database)
    try:
        return db.list_translation_tasks(conn, "2026-07-28")
    finally:
        conn.close()


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_demo_seeds_redacted_persistent_acceptance_states(tmp_path):
    database = tmp_path / "automation-demo.db"
    demo = TranslationAutomationDemo(
        database, build_demo_edition(), tmp_path / "cache"
    )

    tasks = _tasks(database)
    assert len(tasks) == 4
    assert {task.status for task in tasks} == {"retry_wait", "running", "succeeded"}
    assert any(task.build_status == "online" for task in tasks)
    assert {task.error_code for task in tasks if task.error_code} == {
        "NETWORK_CONNECT_FAILED",
        "SCHEMA_VALIDATION_FAILED",
    }
    conn = db.connect(database)
    try:
        circuit = db.get_provider_circuit(conn, "local-demo")
    finally:
        conn.close()
    assert circuit is not None and circuit.state == "open"
    assert demo.runner.translator.calls == {}
    demo._process_one()
    conn = db.connect(database)
    try:
        assert db.get_provider_circuit(conn, "local-demo").state == "open"
    finally:
        conn.close()
    assert demo._timer is None


def test_demo_probe_retry_and_cancel_use_only_target_task(tmp_path):
    database = tmp_path / "automation-demo.db"
    demo = TranslationAutomationDemo(
        database, build_demo_edition(), tmp_path / "cache"
    )
    tasks = _tasks(database)
    failed = next(task for task in tasks if task.error_code == "NETWORK_CONNECT_FAILED")
    schema = next(task for task in tasks if task.error_code == "SCHEMA_VALIDATION_FAILED")
    running = next(task for task in tasks if task.status == "running")

    conn = db.connect(database)
    try:
        db.queue_provider_probe(
            conn,
            "local-demo",
            failed.task_id,
            now=_now(),
            actor="test-admin",
        )
    finally:
        conn.close()
    demo._process_one()
    assert demo.runner.translator.calls == {"automation-demo-3": 1}

    conn = db.connect(database)
    try:
        circuit = db.get_provider_circuit(conn, "local-demo")
        db.queue_translation_task_retry(
            conn, schema.task_id, now=_now(), actor="test-admin"
        )
    finally:
        conn.close()
    assert circuit is not None and circuit.state == "closed"
    demo._process_one()
    assert demo.runner.translator.calls == {
        "automation-demo-1": 1,
        "automation-demo-3": 1,
    }

    conn = db.connect(database)
    try:
        db.request_translation_task_cancel(
            conn, running.task_id, now=_now(), actor="test-admin"
        )
    finally:
        conn.close()
    demo._process_one()
    cancelled = next(task for task in _tasks(database) if task.task_id == running.task_id)
    assert cancelled.status == "retry_wait"
    assert cancelled.error_code == "REQUEST_CANCELLED"
    assert demo.runner.translator.calls == {
        "automation-demo-1": 1,
        "automation-demo-3": 1,
    }
    conn = db.connect(database)
    try:
        actions = conn.execute(
            "SELECT action, actor, status, result_code"
            " FROM translation_admin_actions ORDER BY requested_at"
        ).fetchall()
    finally:
        conn.close()
    assert {tuple(row) for row in actions} == {
        ("probe", "test-admin", "completed", "SUCCEEDED"),
        ("retry", "test-admin", "completed", "SUCCEEDED"),
        ("cancel", "test-admin", "completed", "REQUEST_CANCELLED"),
    }


def test_demo_coalesces_wakeup_submitted_during_active_translation(tmp_path):
    database = tmp_path / "automation-demo.db"
    demo = TranslationAutomationDemo(
        database, build_demo_edition(), tmp_path / "cache"
    )
    tasks = _tasks(database)
    provider_failed = next(
        task for task in tasks if task.error_code == "NETWORK_CONNECT_FAILED"
    )
    schema_failed = next(
        task for task in tasks if task.error_code == "SCHEMA_VALIDATION_FAILED"
    )
    running = next(task for task in tasks if task.status == "running")
    conn = db.connect(database)
    try:
        db.queue_provider_probe(
            conn,
            "local-demo",
            provider_failed.task_id,
            now=_now(),
            actor="test-admin",
        )
    finally:
        conn.close()
    demo._process_one()

    started = threading.Event()
    release = threading.Event()
    original_translate = demo.runner.translator.translate

    def blocking_translate(article):
        started.set()
        assert release.wait(2)
        return original_translate(article)

    demo.runner.translator.translate = blocking_translate
    conn = db.connect(database)
    try:
        db.queue_translation_task_retry(
            conn, schema_failed.task_id, now=_now(), actor="test-admin"
        )
    finally:
        conn.close()
    demo.wakeup()
    assert started.wait(1)

    conn = db.connect(database)
    try:
        db.request_translation_task_cancel(
            conn, running.task_id, now=_now(), actor="test-admin"
        )
    finally:
        conn.close()
    demo.wakeup()
    release.set()

    def cancellation_finished():
        current = next(
            task for task in _tasks(database) if task.task_id == running.task_id
        )
        return current.status == "retry_wait" and current.cancel_requested_at is None

    assert _wait_for(cancellation_finished)
