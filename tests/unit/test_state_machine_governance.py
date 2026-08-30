"""v1.3.0 状态机治理测试:资格同源、reaper、投递租约、循环内清扫与刊期级恢复。

这些测试针对 1.2.4–1.2.18 锁死热修家族的结构性根因,是 §12B 的回归防线。
"""

import datetime as dt
import json

from news_digest.storage import db


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
    def test_stale_requested_action_times_out_and_releases_task(self, tmp_path):
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
        # 超时:timed_out + 任务手动标志释放,任务留在 retry_wait(auto_retry=1)可调度。
        assert db.reap_stale_admin_actions(conn, now=_at(1200), timeout_seconds=900) == 1
        latest = db.latest_translation_admin_action(conn, task.task_id)
        assert latest.status == "timed_out"
        refreshed = db.translation_task(conn, task.task_id)
        assert refreshed.manual_action_id is None
        assert refreshed.manual_retry_requested_at is None
        assert refreshed.status == "retry_wait"
        assert refreshed.auto_retry is True
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
