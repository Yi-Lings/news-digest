"""v1.3.0 状态机治理测试:资格同源、reaper、投递租约、循环内清扫与刊期级恢复。

这些测试针对 1.2.4–1.2.18 锁死热修家族的结构性根因,是 §12B 的回归防线。
"""

import datetime as dt
import json

import pytest

from news_digest.storage import db
from news_digest.translation.automation import (
    claim_translation_work,
    fail_translation_work,
    succeed_translation_work,
)
from news_digest.translation.client import TranslationError


def _at(seconds: int = 0) -> str:
    moment = dt.datetime(2026, 8, 30, 0, 0, tzinfo=dt.UTC) + dt.timedelta(seconds=seconds)
    return moment.isoformat()


def _seed(tmp_path, *, article_count: int = 1):
    conn = db.connect(tmp_path / "news.db")
    db.ensure_automation_edition(conn, "2026-08-30", target_count=article_count, now=_at())
    tasks = []
    for index in range(article_count):
        task = db.ensure_translation_task(
            conn,
            edition_date="2026-08-30",
            article_id=f"https://example.com/a-{index}",
            article_title=f"Article {index}",
            provider_id="provider-1",
            now=_at(),
            segmentation_json=json.dumps([1]),
        )
        tasks.append(task)
    return conn, tasks


def _open_failed_edition(tmp_path, *, provider_id="provider-1"):
    conn, tasks = _seed(tmp_path, article_count=2)
    with conn:
        conn.execute(
            "UPDATE automation_editions SET briefs_json = '[]' WHERE edition_date = ?",
            ("2026-08-30",),
        )
        for position, task in enumerate(tasks):
            conn.execute(
                "INSERT INTO edition_items"
                " (edition_date, article_id, position, source_json, payload, source_hash,"
                " segmentation_json, active_task_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "2026-08-30", task.article_id, position, "{}", "{}",
                    f"source-hash-{position}", json.dumps([1]), task.task_id,
                ),
            )
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
                " error_code = 'PROVIDER_5XX' WHERE task_id = ?",
                (task.task_id,),
            )
    for second in range(1, 6):
        db.record_provider_outcome(
            conn, provider_id, outcome="provider_failure", now=_at(second)
        )
    assert db.get_provider_circuit(conn, provider_id).state == "open"
    return conn, tasks


def test_probe_completion_and_task_state_roll_back_together(tmp_path, monkeypatch):
    conn, tasks = _open_failed_edition(tmp_path)
    try:
        queued = db.queue_provider_probe(
            conn, "provider-1", tasks[0].task_id, now=_at(61), actor="admin"
        )
        claim = claim_translation_work(
            conn, queued.task_id, owner="probe-worker", now=_at(62),
            lease_seconds=900, manual_retry=True, manual_probe=True,
        )
        assert claim.task is not None and claim.is_probe

        original_finish = db.finish_provider_probe

        def interrupted(*args, **kwargs):
            original_finish(*args, **kwargs)
            raise RuntimeError("injected probe interruption")

        monkeypatch.setattr(db, "finish_provider_probe", interrupted)
        with pytest.raises(RuntimeError, match="injected probe interruption"):
            succeed_translation_work(
                conn, queued.task_id, owner="probe-worker", now=_at(63)
            )
        assert db.translation_task(conn, queued.task_id).status == "running"
        assert db.get_provider_circuit(conn, "provider-1").state == "half_open"
        assert db.latest_translation_admin_action(conn, queued.task_id).status == "running"
        assert conn.execute(
            "SELECT status FROM translation_attempts WHERE task_id = ?",
            (queued.task_id,),
        ).fetchone()["status"] == "running"
    finally:
        conn.close()


def test_wakeup_waits_for_retry_after_probe_deadline_and_skips_terminal_only(tmp_path):
    conn, tasks = _seed(tmp_path, article_count=2)
    try:
        for second in range(1, 6):
            db.record_provider_outcome(
                conn, "provider-1", outcome="provider_failure", now=_at(second)
            )
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0"
                " WHERE task_id = ?", (tasks[0].task_id,)
            )
            conn.execute(
                "UPDATE translation_tasks SET status = 'retry_wait', auto_retry = 1,"
                " next_retry_at = ? WHERE task_id = ?", (_at(300), tasks[1].task_id)
            )
        assert db.next_automation_wakeup_at(
            conn, "2026-08-30", now=_at(100)
        ) == _at(300)
        assert not db.automation_due(conn, now=_at(100))
        assert db.automation_due(conn, now=_at(300))

        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
                " next_retry_at = NULL WHERE task_id = ?", (tasks[1].task_id,)
            )
        assert db.next_automation_wakeup_at(conn, "2026-08-30", now=_at(400)) is None
        assert all(not db.automation_due(conn, now=_at(second)) for second in range(400, 4000, 30))
    finally:
        conn.close()


def test_automation_due_detects_expired_task_lease(tmp_path):
    conn, tasks = _seed(tmp_path)
    try:
        assert db.claim_translation_task(
            conn, tasks[0].task_id, owner="stopped-worker", now=_at(),
            lease_seconds=60,
        ) is not None
        assert not db.automation_due(conn, now=_at(59))
        assert db.automation_due(conn, now=_at(60))
    finally:
        conn.close()


def test_configuration_blocked_provider_does_not_schedule_pending_task(tmp_path):
    conn, tasks = _seed(tmp_path, article_count=2)
    try:
        db.record_provider_outcome(
            conn, "provider-1", outcome="configuration_failure", now=_at(1)
        )
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'configuration_blocked',"
                " auto_retry = 0 WHERE task_id = ?", (tasks[0].task_id,)
            )
        assert db.next_automation_wakeup_at(conn, "2026-08-30", now=_at(2)) is None
        assert not db.automation_due(conn, now=_at(2))
    finally:
        conn.close()


class TestTaskCapabilities:
    def test_every_state_has_action_or_explicit_wait(self):
        """不变量:除运行中等待取消确认外,任何状态不得出现空动作。"""
        statuses = [
            "pending",
            "failed",
            "retry_wait",
            "cancelled",
            "configuration_blocked",
            "succeeded",
        ]
        circuits = ["closed", "open", "half_open", "configuration_blocked"]
        for status in statuses:
            for circuit in circuits:
                caps = db.task_capabilities(
                    status=status,
                    cancel_requested_at=None,
                    lease_expires_at=None,
                    auto_retry=False,
                    next_retry_at=None,
                    circuit_state=circuit,
                    now=_at(),
                )
                if status == "succeeded":
                    assert caps.actions == ()
                    continue
                assert caps.actions, f"{status} x {circuit} 出现无动作死端"

    def test_running_cancel_then_recover(self):
        waiting = db.task_capabilities(
            status="running",
            cancel_requested_at=_at(),
            lease_expires_at=_at(600),
            auto_retry=False,
            next_retry_at=None,
            circuit_state="closed",
            now=_at(),
        )
        assert waiting.actions == ()
        expired = db.task_capabilities(
            status="running",
            cancel_requested_at=_at(),
            lease_expires_at=_at(-1),
            auto_retry=False,
            next_retry_at=None,
            circuit_state="closed",
            now=_at(),
        )
        assert expired.actions == ("recover",)

    def test_configuration_blocked_circuit_running_task_keeps_cancel(self):
        """电路阻断不得吞掉运行中任务的取消动作(旧 UI 的优先级缺陷)。"""
        caps = db.task_capabilities(
            status="running",
            cancel_requested_at=None,
            lease_expires_at=_at(600),
            auto_retry=False,
            next_retry_at=None,
            circuit_state="configuration_blocked",
            now=_at(),
        )
        assert caps.actions == ("cancel",)

    def test_backoff_due_is_schedulable(self):
        caps = db.task_capabilities(
            status="retry_wait",
            cancel_requested_at=None,
            lease_expires_at=None,
            auto_retry=True,
            next_retry_at=_at(-1),
            circuit_state="closed",
            now=_at(),
        )
        assert caps.actions == ("retry",)
        assert caps.schedulable is True
        future = db.task_capabilities(
            status="retry_wait",
            cancel_requested_at=None,
            lease_expires_at=None,
            auto_retry=True,
            next_retry_at=_at(120),
            circuit_state="closed",
            now=_at(),
        )
        assert future.schedulable is False


class TestReaper:
    def test_valid_queued_action_survives_long_wait(self, tmp_path):
        conn, tasks = _seed(tmp_path)
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
                " error_code = 'SCHEMA_VALIDATION_FAILED', updated_at = ?"
                " WHERE task_id = ?",
                (_at(), tasks[0].task_id),
            )
        task = db.queue_translation_task_retry(
            conn, tasks[0].task_id, now=_at(), actor="admin"
        )
        assert task.manual_action_id
        # 未超时:不动。
        assert db.reap_stale_admin_actions(conn, now=_at(60), timeout_seconds=900) == 0
        latest = db.latest_translation_admin_action(conn, task.task_id)
        assert latest.status == "requested"
        # A valid queue entry can wait behind a long translation without losing
        # its explicit Admin retry or being charged against the automatic cap.
        assert db.reap_stale_admin_actions(conn, now=_at(1200), timeout_seconds=900) == 0
        latest = db.latest_translation_admin_action(conn, task.task_id)
        assert latest.status == "requested"
        refreshed = db.translation_task(conn, task.task_id)
        assert refreshed.manual_action_id == task.manual_action_id
        assert refreshed.manual_retry_requested_at is not None
        assert refreshed.status == "retry_wait"
        assert refreshed.auto_retry is True
        conn.close()

    def test_orphaned_requested_action_times_out(self, tmp_path):
        conn, tasks = _seed(tmp_path)
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0"
                " WHERE task_id = ?",
                (tasks[0].task_id,),
            )
        task = db.queue_translation_task_retry(
            conn, tasks[0].task_id, now=_at(), actor="admin"
        )
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET manual_action_id = NULL,"
                " manual_retry_requested_at = NULL WHERE task_id = ?",
                (task.task_id,),
            )
        assert db.reap_stale_admin_actions(conn, now=_at(1200), timeout_seconds=900) == 1
        latest = db.latest_translation_admin_action(conn, task.task_id)
        assert latest.status == "timed_out"
        assert latest.result_code == "ACTION_TIMEOUT"
        conn.close()

    def test_cancel_actions_are_never_reaped(self, tmp_path):
        conn, tasks = _seed(tmp_path)
        claimed = db.claim_translation_task(
            conn, tasks[0].task_id, owner="w", now=_at(), lease_seconds=300
        )
        db.request_translation_task_cancel(conn, claimed.task_id, now=_at(1))
        assert db.reap_stale_admin_actions(conn, now=_at(9999), timeout_seconds=900) == 0
        latest = db.latest_translation_admin_action(conn, claimed.task_id)
        assert latest.status == "requested"
        conn.close()


class TestDeliveryLease:
    def test_stale_claim_returns_edition_to_complete(self, tmp_path):
        conn, _tasks = _seed(tmp_path)
        # 直接把刊期推到 complete 并模拟 worker 死亡后的悬挂认领。
        with conn:
            conn.execute(
                "UPDATE automation_editions SET status = 'delivery_pending',"
                " delivery_key = 'a' * 64, delivery_expires_at = ?,"
                " delivery_started_at = ?, updated_at = ? WHERE edition_date = '2026-08-30'",
                (_at(-700), _at(-800), _at(-800)),
            )
        assert db.expire_stale_delivery_claims(conn, now=_at()) == 1
        edition = db.automation_edition(conn, "2026-08-30")
        assert edition.status == "complete"
        assert edition.delivery_key is None
        assert edition.last_error_code == "DELIVERY_FAILED"
        conn.close()

    def test_live_claim_is_untouched(self, tmp_path):
        conn, _tasks = _seed(tmp_path)
        with conn:
            conn.execute(
                "UPDATE automation_editions SET status = 'delivery_pending',"
                " delivery_key = 'a' * 64, delivery_expires_at = ?,"
                " delivery_started_at = ?, updated_at = ? WHERE edition_date = '2026-08-30'",
                (_at(300), _at(), _at()),
            )
        assert db.expire_stale_delivery_claims(conn, now=_at(1)) == 0
        assert db.automation_edition(conn, "2026-08-30").status == "delivery_pending"
        conn.close()


class TestEditionRetry:
    def test_retry_edition_failed_tasks_queues_terminal_tasks(self, tmp_path):
        conn, tasks = _seed(tmp_path, article_count=3)
        # task0: 终态 failed;task1: cancelled;task2: 保持 pending 不受影响。
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
                " error_code = 'SCHEMA_VALIDATION_FAILED', updated_at = ?"
                " WHERE article_id LIKE '%a-0'",
                (_at(),),
            )
            conn.execute(
                "UPDATE translation_tasks SET status = 'cancelled', auto_retry = 0,"
                " updated_at = ? WHERE article_id LIKE '%a-1'",
                (_at(),),
            )
        counts = db.retry_edition_failed_tasks(conn, "2026-08-30", now=_at(2), actor="admin")
        assert counts == {"queued": 2, "skipped": 0}
        statuses = {
            task.article_id: db.translation_task(conn, task.task_id).status
            for task in tasks
        }
        assert statuses[tasks[0].article_id] == "retry_wait"
        assert statuses[tasks[1].article_id] == "retry_wait"
        assert statuses[tasks[2].article_id] == "pending"
        conn.close()

    def test_retry_edition_rebinds_retry_wait_tasks_to_current_provider(self, tmp_path):
        conn, tasks = _seed(tmp_path, article_count=1)
        old_task = tasks[0]
        with conn:
            # T32 rebinding is defined only for frozen edition items; legacy tasks
            # without an active-task pointer are deliberately never mutated in place.
            conn.execute(
                "UPDATE automation_editions SET briefs_json = '[]' WHERE edition_date = ?",
                ("2026-08-30",),
            )
            conn.execute(
                "INSERT INTO edition_items"
                " (edition_date, article_id, position, source_json, payload, source_hash,"
                " segmentation_json, active_task_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "2026-08-30",
                    old_task.article_id,
                    0,
                    "{}",
                    "{}",
                    "source-hash",
                    json.dumps([1]),
                    old_task.task_id,
                ),
            )
            conn.execute(
                "UPDATE translation_tasks SET status = 'retry_wait', auto_retry = 1,"
                " error_code = 'PROVIDER_5XX', error_category = 'provider_infrastructure'"
                " WHERE task_id = ?",
                (old_task.task_id,),
            )
        counts = db.retry_edition_failed_tasks(
            conn,
            "2026-08-30",
            now=_at(2),
            actor="admin",
            provider_id="provider-terra",
        )
        assert counts == {"queued": 1, "skipped": 0}
        historical = db.translation_task(conn, old_task.task_id)
        assert historical is not None
        assert historical.provider_id == old_task.provider_id
        active = db.active_translation_tasks(conn, "2026-08-30")
        assert len(active) == 1
        rebound = active[0]
        assert rebound.task_id != old_task.task_id
        assert rebound.provider_id == "provider-terra"
        assert rebound.status == "retry_wait"
        assert rebound.next_retry_at == _at(2)
        assert rebound.error_code is None
        assert rebound.rebind_from_task_id == old_task.task_id
        assert rebound.rebind_reason == "EDITION_RECOVERY"
        conn.close()

    @pytest.mark.parametrize("rebind", [False, True])
    def test_retry_edition_probes_once_then_drains_open_provider(self, tmp_path, rebind):
        target_provider = "provider-2" if rebind else "provider-1"
        conn, tasks = _open_failed_edition(tmp_path, provider_id=target_provider)

        counts = db.retry_edition_failed_tasks(
            conn, "2026-08-30", now=_at(6), actor="admin", provider_id=target_provider
        )
        assert counts == {"queued": 2, "skipped": 0}
        active = db.active_translation_tasks(conn, "2026-08-30")
        assert len(active) == 2
        assert {task.provider_id for task in active} == {target_provider}
        if rebind:
            assert {task.rebind_from_task_id for task in active} == {
                task.task_id for task in tasks
            }
        probes = [task for task in active if task.manual_probe_requested_at is not None]
        assert len(probes) == 1
        deferred = next(task for task in active if task.task_id != probes[0].task_id)
        assert deferred.status == "retry_wait"
        assert deferred.manual_retry_requested_at is not None
        assert deferred.manual_probe_requested_at is None
        assert db.latest_translation_admin_action(conn, deferred.task_id).status == "requested"

        probe = claim_translation_work(
            conn, probes[0].task_id, owner="worker", now=_at(7), lease_seconds=600,
            manual_retry=True, manual_probe=True,
        )
        assert probe.task is not None and probe.is_probe
        succeed_translation_work(conn, probes[0].task_id, owner="worker", now=_at(8))
        assert db.get_provider_circuit(conn, target_provider).state == "closed"

        remaining = db.translation_task(conn, deferred.task_id)
        claim = claim_translation_work(
            conn, remaining.task_id, owner="worker", now=_at(9), lease_seconds=600,
            manual_retry=remaining.manual_retry_requested_at is not None,
            manual_probe=remaining.manual_probe_requested_at is not None,
        )
        assert claim.task is not None and not claim.is_probe
        attempt = conn.execute(
            "SELECT kind FROM translation_attempts WHERE task_id = ? ORDER BY attempt_number DESC"
            " LIMIT 1",
            (remaining.task_id,),
        ).fetchone()
        assert attempt["kind"] == "manual"
        conn.close()

    def test_retry_edition_rebinds_queued_work_after_failed_probe(self, tmp_path):
        conn, _ = _open_failed_edition(tmp_path)
        assert db.retry_edition_failed_tasks(
            conn, "2026-08-30", now=_at(6), actor="admin", provider_id="provider-1"
        ) == {"queued": 2, "skipped": 0}
        old_tasks = db.active_translation_tasks(conn, "2026-08-30")
        probe = next(task for task in old_tasks if task.manual_probe_requested_at is not None)
        deferred = next(task for task in old_tasks if task.task_id != probe.task_id)
        old_action_id = deferred.manual_action_id
        assert old_action_id is not None

        claimed = claim_translation_work(
            conn, probe.task_id, owner="worker-a", now=_at(7), lease_seconds=600,
            manual_retry=True, manual_probe=True,
        )
        assert claimed.task is not None and claimed.is_probe
        fail_translation_work(
            conn, probe.task_id, owner="worker-a", now=_at(8),
            error=TranslationError("upstream unavailable", category="provider", status=503),
            stage="connect_provider",
        )
        assert db.get_provider_circuit(conn, "provider-1").state == "open"

        # The Admin now selects a healthy default provider. The pending A retry
        # must not prevent the whole edition from moving to B.
        assert db.retry_edition_failed_tasks(
            conn, "2026-08-30", now=_at(9), actor="admin", provider_id="provider-2"
        ) == {"queued": 2, "skipped": 0}
        rebound = db.active_translation_tasks(conn, "2026-08-30")
        assert len(rebound) == 2
        assert {task.provider_id for task in rebound} == {"provider-2"}
        assert {task.rebind_from_task_id for task in rebound} == {
            task.task_id for task in old_tasks
        }
        assert all(task.manual_probe_requested_at is None for task in rebound)
        assert all(
            db.latest_translation_admin_action(conn, task.task_id).status == "requested"
            for task in rebound
        )

        old_action = conn.execute(
            "SELECT status, result_code FROM translation_admin_actions WHERE action_id = ?",
            (old_action_id,),
        ).fetchone()
        assert (old_action["status"], old_action["result_code"]) == (
            "rejected", "PROVIDER_REBOUND"
        )
        old_deferred = db.translation_task(conn, deferred.task_id)
        assert old_deferred.manual_action_id is None
        assert old_deferred.manual_retry_requested_at is None
        conn.close()

    def test_deferred_retry_waits_for_open_circuit_cooldown(self, tmp_path):
        conn, _ = _open_failed_edition(tmp_path)
        assert db.retry_edition_failed_tasks(
            conn, "2026-08-30", now=_at(6), actor="admin", provider_id="provider-1"
        ) == {"queued": 2, "skipped": 0}
        tasks = db.active_translation_tasks(conn, "2026-08-30")
        probe = next(task for task in tasks if task.manual_probe_requested_at is not None)
        deferred = next(task for task in tasks if task.task_id != probe.task_id)
        assert claim_translation_work(
            conn, probe.task_id, owner="worker-a", now=_at(7), lease_seconds=600,
            manual_retry=True, manual_probe=True,
        ).task is not None
        fail_translation_work(
            conn, probe.task_id, owner="worker-a", now=_at(8),
            error=TranslationError("upstream unavailable", category="provider", status=503),
            stage="connect_provider",
        )
        circuit = db.get_provider_circuit(conn, "provider-1")
        assert circuit.state == "open" and circuit.next_probe_at > _at(9)

        blocked = claim_translation_work(
            conn, deferred.task_id, owner="worker-b", now=_at(9), lease_seconds=600,
            manual_retry=True, manual_probe=False,
        )
        assert blocked.task is None
        assert db.translation_task(conn, deferred.task_id).status == "retry_wait"
        assert db.next_automation_wakeup_at(
            conn, "2026-08-30", provider_id="provider-1", now=_at(9)
        ) == circuit.next_probe_at
        assert conn.execute(
            "SELECT COUNT(*) FROM translation_attempts WHERE task_id = ?",
            (deferred.task_id,),
        ).fetchone()[0] == 0
        conn.close()

    def test_batch_retry_waits_through_long_first_translation(self, tmp_path):
        conn, tasks = _seed(tmp_path, article_count=2)
        with conn:
            conn.execute(
                "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
                " error_code = 'SCHEMA_VALIDATION_FAILED' WHERE edition_date = ?",
                ("2026-08-30",),
            )
            conn.execute(
                "UPDATE translation_tasks SET attempt_count = 3 WHERE task_id = ?",
                (tasks[1].task_id,),
            )
            for attempt in range(1, 4):
                conn.execute(
                    "INSERT INTO translation_attempts"
                    " (task_id, attempt_number, owner, kind, status, started_at, provider_id)"
                    " VALUES (?, ?, 'old-worker', 'automatic', 'failed', ?, ?)",
                    (tasks[1].task_id, attempt, _at(-attempt), tasks[1].provider_id),
                )
        assert db.retry_edition_failed_tasks(
            conn, "2026-08-30", now=_at(1), actor="admin"
        ) == {"queued": 2, "skipped": 0}
        first = db.claim_translation_task(
            conn, tasks[0].task_id, owner="worker", now=_at(2), lease_seconds=900,
            manual=True,
        )
        assert first is not None

        db.run_worker_maintenance(conn, now=_at(120))
        waiting = db.translation_task(conn, tasks[1].task_id)
        assert waiting.manual_retry_requested_at is not None
        assert db.latest_translation_admin_action(conn, waiting.task_id).status == "requested"

        db.finish_translation_task_success(
            conn, first.task_id, owner="worker", now=_at(121)
        )
        db.run_worker_maintenance(conn, now=_at(122))
        waiting = db.translation_task(conn, tasks[1].task_id)
        assert waiting.manual_retry_requested_at is not None
        claimed = db.claim_translation_task(
            conn, waiting.task_id, owner="worker", now=_at(123),
            lease_seconds=900, manual=waiting.manual_retry_requested_at is not None,
        )
        assert claimed is not None
        assert db.latest_translation_admin_action(conn, waiting.task_id).status == "running"
        conn.close()


class TestSweep:
    def test_sweep_reclaims_expired_lease_without_process_flag(self, tmp_path):
        conn, tasks = _seed(tmp_path)
        claimed = db.claim_translation_task(
            conn, tasks[0].task_id, owner="dead-worker", now=_at(), lease_seconds=60
        )
        assert claimed.status == "running"
        # 循环内调用(无 process_terminated 门控)即可回收死亡租约。
        assert db.sweep_expired_leases(conn, now=_at(120)) == 1
        recovered = db.translation_task(conn, tasks[0].task_id)
        assert recovered.status == "retry_wait"
        assert recovered.lease_owner is None
        conn.close()

    def test_sweep_keeps_live_lease(self, tmp_path):
        conn, tasks = _seed(tmp_path)
        claimed = db.claim_translation_task(
            conn, tasks[0].task_id, owner="live-worker", now=_at(), lease_seconds=300
        )
        assert db.sweep_expired_leases(conn, now=_at(120)) == 0
        assert db.translation_task(conn, claimed.task_id).status == "running"
        conn.close()


class TestSegmentationFreeze:
    def test_ensure_task_persists_segmentation(self, tmp_path):
        conn, tasks = _seed(tmp_path)
        assert json.loads(tasks[0].segmentation_json) == [1]
        conn.close()

    def test_frozen_counts_guard_against_article_drift(self, tmp_path):
        """快照与文章段落数不一致 → 数据完整性错误,而不是静默换标准。"""
        from news_digest.models import Article, Paragraph
        from news_digest.translation.automation import (
            TranslationAutomationRunner,
            TranslationTaskDataError,
        )

        conn, tasks = _seed(tmp_path)
        runner = TranslationAutomationRunner.__new__(TranslationAutomationRunner)
        article = Article(
            slug="a-0",
            source="S",
            title_en="t",
            summary_en="s",
            author="a",
            published_at=_at(),
            url=tasks[0].article_id,
            reading_minutes=1,
            paragraphs=[Paragraph(en="One."), Paragraph(en="Two.")],
        )
        try:
            runner._frozen_counts(tasks[0], article)
        except TranslationTaskDataError:
            pass
        else:
            raise AssertionError("expected TranslationTaskDataError")
        conn.close()
