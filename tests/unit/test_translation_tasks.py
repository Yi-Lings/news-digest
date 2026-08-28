import datetime as dt

import pytest

from news_digest.storage import db
from news_digest.translation.automation import (
    claim_translation_work,
    classify_translation_failure,
    fail_translation_work,
    provider_concurrency_limit,
)
from news_digest.translation.client import TranslationError
from news_digest.translation.schema import InvalidTranslation

UTC = dt.UTC


def _at(seconds: int = 0) -> str:
    return (dt.datetime(2026, 7, 28, 0, 0, tzinfo=UTC) + dt.timedelta(seconds=seconds)).isoformat()


def _task(conn, *, article_id: str = "article-1", now: str = _at()):
    return db.ensure_translation_task(
        conn,
        edition_date="2026-07-28",
        article_id=article_id,
        article_title=f"Title {article_id}",
        provider_id="provider-default",
        now=now,
    )


def test_translation_task_creation_is_idempotent_and_persistent(tmp_path):
    path = tmp_path / "digest.db"
    conn = db.connect(path)
    created = _task(conn)
    repeated = _task(conn, now=_at(1))
    assert repeated == created
    conn.close()

    reopened = db.connect(path)
    assert db.translation_task(reopened, created.task_id) == created
    assert db.list_translation_tasks(reopened, "2026-07-28") == [created]


def test_pending_task_reopens_archived_edition_for_resume(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    db.ensure_automation_edition(conn, "2026-07-28", target_count=1, now=_at())
    _task(conn)
    with conn:
        conn.execute(
            "UPDATE automation_editions SET status = 'delivered' WHERE edition_date = ?",
            ("2026-07-28",),
        )

    assert db.unfinished_automation_edition_dates(conn) == ["2026-07-28"]


def test_running_task_never_has_a_retry_time(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)

    claimed = db.claim_translation_task(
        conn,
        task.task_id,
        owner="worker-a",
        now=_at(),
        lease_seconds=30,
    )

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.attempt_count == 1
    assert claimed.failed_at is None
    assert claimed.next_retry_at is None


def test_pending_task_dispatch_is_audited_and_claimable_immediately(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)

    queued = db.queue_translation_task_dispatch(
        conn, task.task_id, now=_at(1), actor="admin"
    )
    assert queued.status == "pending"
    assert queued.manual_retry_requested_at == _at(1)
    assert queued.manual_action_id

    claimed = db.claim_translation_task(
        conn, task.task_id, owner="worker-a", now=_at(2), lease_seconds=30
    )
    assert claimed is not None
    action = conn.execute(
        "SELECT action, status, result_code FROM translation_admin_actions"
        " WHERE action_id = ?",
        (queued.manual_action_id,),
    ).fetchone()
    assert tuple(action) == ("dispatch", "running", None)


def test_probe_request_during_half_open_returns_active_probe_task(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )
    assert db.claim_provider_probe(
        conn,
        "provider-default",
        task_id=task.task_id,
        owner="probe-worker",
        now=_at(64),
        lease_seconds=30,
    )

    same = db.queue_provider_probe(
        conn,
        "provider-default",
        task.task_id,
        now=_at(65),
        actor="admin",
    )
    assert same.task_id == task.task_id
    assert db.get_provider_circuit(conn, "provider-default").state == "half_open"


@pytest.mark.parametrize(
    ("attempt_number", "delay_seconds"),
    [(1, 15), (2, 30), (3, 60), (4, 120), (5, 300), (6, 300)],
)
def test_retry_delay_starts_at_failed_at(tmp_path, attempt_number, delay_seconds):
    conn = db.connect(tmp_path / f"digest-{attempt_number}.db")
    task = _task(conn)
    failed = None
    for attempt in range(1, attempt_number + 1):
        started_at = _at(attempt * 1_000)
        failed_at = _at(attempt * 1_000 + 400)
        claimed = db.claim_translation_task(
            conn,
            task.task_id,
            owner=f"worker-{attempt}",
            now=started_at,
            lease_seconds=600,
            manual=attempt > 1,
        )
        assert claimed is not None
        failed = db.finish_translation_task_failure(
            conn,
            task.task_id,
            owner=f"worker-{attempt}",
            now=failed_at,
            error_code="NETWORK_CONNECT_FAILED",
            error_category="provider_infrastructure",
            failure_stage="connect_provider",
            diagnostic_id=f"diag-{attempt}",
        )

    assert failed is not None
    assert failed.failed_at == _at(attempt_number * 1_000 + 400)
    expected = dt.datetime.fromisoformat(failed.failed_at) + dt.timedelta(seconds=delay_seconds)
    assert failed.next_retry_at == expected.isoformat()


def test_atomic_task_lease_allows_only_one_connection(tmp_path):
    path = tmp_path / "digest.db"
    first = db.connect(path)
    task = _task(first)
    second = db.connect(path)

    claim_a = db.claim_translation_task(
        first, task.task_id, owner="worker-a", now=_at(), lease_seconds=30
    )
    claim_b = db.claim_translation_task(
        second, task.task_id, owner="worker-b", now=_at(), lease_seconds=30
    )

    assert claim_a is not None
    assert claim_b is None
    assert len(db.list_translation_attempts(first, task.task_id)) == 1


def test_expired_lease_is_not_reclaimed_while_old_request_is_unconfirmed(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    assert db.claim_translation_task(
        conn, task.task_id, owner="worker-a", now=_at(), lease_seconds=15
    )

    assert (
        db.claim_translation_task(
            conn,
            task.task_id,
            owner="worker-b",
            now=_at(16),
            lease_seconds=15,
            manual=True,
        )
        is None
    )
    assert (
        db.recover_interrupted_translation_tasks(conn, now=_at(16), process_terminated=False) == 0
    )
    assert db.translation_task(conn, task.task_id).status == "running"


def test_expired_lease_can_resume_after_process_termination_is_confirmed(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    assert db.claim_translation_task(
        conn, task.task_id, owner="old-worker", now=_at(), lease_seconds=15
    )
    assert db.recover_interrupted_translation_tasks(
        conn, now=_at(16), process_terminated=True
    ) == 1
    recovered = db.translation_task(conn, task.task_id)
    assert recovered.status == "retry_wait"
    assert recovered.error_code == "REQUEST_CANCELLED"
    assert recovered.failed_at == _at(16)
    assert db.claim_translation_task(
        conn,
        task.task_id,
        owner="new-worker",
        now=_at(17),
        lease_seconds=15,
        manual=True,
    )


def test_cancel_action_is_recovered_when_worker_dies_after_cancel_request(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    assert db.claim_translation_task(
        conn, task.task_id, owner="old-worker", now=_at(), lease_seconds=10
    )
    db.request_translation_task_cancel(conn, task.task_id, now=_at(1), actor="admin")
    assert db.recover_interrupted_translation_tasks(
        conn, now=_at(11), process_terminated=True
    ) == 1
    action = conn.execute(
        "SELECT action, status, result_code FROM translation_admin_actions"
        " WHERE task_id = ? ORDER BY requested_at",
        (task.task_id,),
    ).fetchall()
    assert [tuple(row) for row in action] == [("cancel", "recovered", "RECOVERED")]


def test_expired_probe_lease_is_released_for_future_probe(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )
    assert db.claim_provider_probe(
        conn, "provider-default", task_id=task.task_id, owner="dead-worker",
        now=_at(64), lease_seconds=10, manual=True
    )
    assert db.recover_interrupted_translation_tasks(
        conn, now=_at(75), process_terminated=True
    ) == 0
    circuit = db.get_provider_circuit(conn, "provider-default")
    assert circuit.state == "open"
    assert circuit.probe_task_id is None
    assert db.queue_provider_probe(
        conn, "provider-default", task.task_id, now=_at(76), actor="admin"
    )


def test_cancel_request_does_not_change_running_state_until_termination_confirmed(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    assert db.claim_translation_task(
        conn, task.task_id, owner="worker-a", now=_at(), lease_seconds=30
    )

    requested = db.request_translation_task_cancel(conn, task.task_id, now=_at(1))
    assert requested.cancel_requested_at == _at(1)
    assert requested.status == "running"
    assert (
        db.confirm_translation_task_cancelled(
            conn,
            task.task_id,
            owner="worker-a",
            now=_at(2),
            request_terminated=False,
        )
        is None
    )
    still_running = db.translation_task(conn, task.task_id)
    assert still_running.status == "running"
    assert still_running.lease_owner == "worker-a"

    cancelled = db.confirm_translation_task_cancelled(
        conn,
        task.task_id,
        owner="worker-a",
        now=_at(3),
        request_terminated=True,
    )
    assert cancelled is not None
    assert cancelled.status == "retry_wait"
    assert cancelled.error_code == "REQUEST_CANCELLED"
    assert cancelled.failed_at == _at(3)
    assert cancelled.next_retry_at == _at(18)
    assert cancelled.lease_owner is None


def test_expired_cancel_can_queue_recovery_and_is_marked_recovered(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    assert db.claim_translation_task(
        conn, task.task_id, owner="worker-a", now=_at(), lease_seconds=10
    )
    db.request_translation_task_cancel(conn, task.task_id, now=_at(1), actor="admin")
    action_id, already_queued = db.queue_translation_task_recovery(
        conn, task.task_id, now=_at(11), actor="admin"
    )
    assert already_queued is False
    assert isinstance(action_id, str)
    assert db.recover_interrupted_translation_tasks(
        conn, now=_at(12), process_terminated=True
    ) == 1
    recovered = db.translation_task(conn, task.task_id)
    assert recovered.status == "retry_wait"
    action = conn.execute(
        "SELECT status, result_code FROM translation_admin_actions WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    assert tuple(action) == ("recovered", "RECOVERED")


def test_duplicate_cancel_request_is_rejected_and_audited_once(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    assert db.claim_translation_task(
        conn, task.task_id, owner="worker-a", now=_at(), lease_seconds=30
    )

    db.request_translation_task_cancel(
        conn, task.task_id, now=_at(1), actor="admin-a"
    )
    with pytest.raises(RuntimeError, match="not running"):
        db.request_translation_task_cancel(
            conn, task.task_id, now=_at(2), actor="admin-b"
        )

    actions = conn.execute(
        "SELECT action, actor, status FROM translation_admin_actions"
        " WHERE task_id = ? ORDER BY requested_at",
        (task.task_id,),
    ).fetchall()
    assert [tuple(row) for row in actions] == [("cancel", "admin-a", "requested")]


def test_progress_updates_are_closed_and_preserve_stable_task_shape(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    running = db.claim_translation_task(
        conn, task.task_id, owner="worker-a", now=_at(), lease_seconds=30
    )
    assert running.current_stage == "connect_provider"
    assert running.hard_timeout_at == _at(30)

    assert db.update_translation_task_progress(
        conn,
        task.task_id,
        owner="worker-a",
        stage="receiving_response",
        now=_at(2),
        received_chunks=4,
    )
    updated = db.translation_task(conn, task.task_id)
    assert updated.current_stage == "receiving_response"
    assert updated.received_chunks == 4
    assert updated.last_activity_at == _at(2)
    with pytest.raises(ValueError, match="safe closed"):
        db.update_translation_task_progress(
            conn,
            task.task_id,
            owner="worker-a",
            stage="raw provider output",
            now=_at(3),
        )

def test_provider_circuit_opens_on_fifth_infrastructure_failure(tmp_path):
    conn = db.connect(tmp_path / "digest.db")

    for failure in range(1, 6):
        circuit = db.record_provider_outcome(
            conn,
            "provider-default",
            outcome="provider_failure",
            now=_at(failure),
        )
        assert circuit.consecutive_failures == failure
        assert circuit.state == ("open" if failure == 5 else "closed")

    assert circuit.next_probe_at == _at(65)


def test_provider_success_resets_failures_and_content_errors_do_not_count(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    db.record_provider_outcome(conn, "provider-default", outcome="provider_failure", now=_at(1))
    unchanged = db.record_provider_outcome(
        conn, "provider-default", outcome="content_failure", now=_at(2)
    )
    assert unchanged.consecutive_failures == 1

    reset = db.record_provider_outcome(conn, "provider-default", outcome="success", now=_at(3))
    assert reset.state == "closed"
    assert reset.consecutive_failures == 0


@pytest.mark.parametrize(
    ("probe_failure_number", "expected_delay"),
    [(1, 120), (2, 300), (3, 300)],
)
def test_failed_half_open_probe_uses_120_then_300_second_cooldowns(
    tmp_path, probe_failure_number, expected_delay
):
    conn = db.connect(tmp_path / f"digest-{probe_failure_number}.db")
    task = _task(conn)
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )

    now_seconds = 64
    for probe_number in range(1, probe_failure_number + 1):
        assert db.claim_provider_probe(
            conn,
            "provider-default",
            task_id=task.task_id,
            owner=f"probe-{probe_number}",
            now=_at(now_seconds),
            lease_seconds=30,
        )
        circuit = db.finish_provider_probe(
            conn,
            "provider-default",
            owner=f"probe-{probe_number}",
            outcome="provider_failure",
            now=_at(now_seconds + 10),
        )
        if probe_number < probe_failure_number:
            now_seconds = int(
                (
                    dt.datetime.fromisoformat(circuit.next_probe_at)
                    - dt.datetime.fromisoformat(_at())
                ).total_seconds()
            )

    assert circuit.next_probe_at == _at(now_seconds + 10 + expected_delay)


def test_automatic_and_manual_probe_compete_for_one_persistent_lease(tmp_path):
    path = tmp_path / "digest.db"
    first = db.connect(path)
    task = _task(first)
    for failure in range(5):
        db.record_provider_outcome(
            first, "provider-default", outcome="provider_failure", now=_at(failure)
        )
    second = db.connect(path)

    assert db.claim_provider_probe(
        first,
        "provider-default",
        task_id=task.task_id,
        owner="automatic",
        now=_at(64),
        lease_seconds=30,
    )
    assert not db.claim_provider_probe(
        second,
        "provider-default",
        task_id=task.task_id,
        owner="manual",
        now=_at(64),
        lease_seconds=30,
        manual=True,
    )


def test_only_one_manual_probe_can_be_queued_for_provider(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    first = _task(conn, article_id="article-1")
    second = _task(conn, article_id="article-2")
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )

    db.queue_provider_probe(
        conn,
        "provider-default",
        first.task_id,
        now=_at(6),
        actor="admin-a",
    )
    with pytest.raises(RuntimeError, match="already queued"):
        db.queue_provider_probe(
            conn,
            "provider-default",
            second.task_id,
            now=_at(7),
            actor="admin-b",
        )

    actions = conn.execute(
        "SELECT task_id, actor, status FROM translation_admin_actions"
        " WHERE action = 'probe'"
    ).fetchall()
    assert [tuple(row) for row in actions] == [
        (first.task_id, "admin-a", "requested")
    ]


def test_configuration_blocked_task_can_be_claimed_for_controlled_probe(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    db.record_provider_outcome(
        conn, "provider-default", outcome="configuration_failure", now=_at()
    )
    db.queue_provider_probe(
        conn, "provider-default", task.task_id, now=_at(1), actor="admin"
    )
    claimed = claim_translation_work(
        conn, task.task_id, owner="probe-worker", now=_at(2),
        lease_seconds=30, manual_probe=True
    )
    assert claimed.task is not None
    assert claimed.is_probe


def test_configuration_failure_blocks_until_controlled_test_succeeds(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    blocked = db.record_provider_outcome(
        conn,
        "provider-default",
        outcome="configuration_failure",
        now=_at(),
    )
    assert blocked.state == "configuration_blocked"
    assert blocked.next_probe_at is None
    assert not db.clear_provider_configuration_block(
        conn, "provider-default", now=_at(1), controlled_test_succeeded=False
    )
    assert db.get_provider_circuit(conn, "provider-default").state == "configuration_blocked"

    assert db.clear_provider_configuration_block(
        conn, "provider-default", now=_at(2), controlled_test_succeeded=True
    )
    assert db.get_provider_circuit(conn, "provider-default").state == "closed"


@pytest.mark.parametrize(
    ("error", "code", "outcome", "auto_retry"),
    [
        (
            TranslationError("redacted", category="provider", status=400),
            "UPSTREAM_ERROR",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="authentication", status=401),
            "UPSTREAM_ERROR",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="authentication", status=403),
            "UPSTREAM_ERROR",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="rate_limit", status=429),
            "RATE_LIMIT_429",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="provider", status=503),
            "PROVIDER_5XX",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="network"),
            "NETWORK_CONNECT_FAILED",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="total_timeout"),
            "REQUEST_TIMEOUT",
            "provider_failure",
            True,
        ),
        (
            TranslationError("redacted", category="empty_response"),
            "EMPTY_RESPONSE",
            "content_failure",
            False,
        ),
        (
            TranslationError("redacted", category="response_format"),
            "UNPARSEABLE_RESPONSE",
            "content_failure",
            False,
        ),
        (InvalidTranslation("redacted"), "SCHEMA_VALIDATION_FAILED", "content_failure", False),
    ],
)
def test_translation_errors_map_to_closed_safe_failures(error, code, outcome, auto_retry):
    failure = classify_translation_failure(error, stage="schema_validation")
    assert failure.error_code == code
    assert failure.provider_outcome == outcome
    assert failure.auto_retry is auto_retry
    assert "redacted" not in repr(failure)


def test_open_circuit_blocks_normal_claim_but_allows_one_due_probe(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )

    blocked = claim_translation_work(
        conn,
        task.task_id,
        owner="worker-a",
        now=_at(63),
        lease_seconds=30,
    )
    assert blocked.task is None
    assert blocked.blocked_reason == "CIRCUIT_OPEN"

    probe = claim_translation_work(
        conn,
        task.task_id,
        owner="worker-a",
        now=_at(64),
        lease_seconds=30,
    )
    assert probe.task is not None
    assert probe.is_probe
    assert db.get_provider_circuit(conn, "provider-default").state == "half_open"

    duplicate = claim_translation_work(
        conn,
        task.task_id,
        owner="worker-b",
        now=_at(64),
        lease_seconds=30,
        manual_probe=True,
    )
    assert duplicate.task is None


def test_probe_schema_failure_closes_circuit_but_keeps_task_retryable(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )
    claimed = claim_translation_work(
        conn,
        task.task_id,
        owner="probe-worker",
        now=_at(64),
        lease_seconds=30,
    )
    assert claimed.task is not None

    failed = fail_translation_work(
        conn,
        task.task_id,
        owner="probe-worker",
        now=_at(65),
        error=InvalidTranslation("must not persist"),
        stage="schema_validation",
    )

    assert failed.status == "failed"
    assert failed.next_retry_at is None
    assert not failed.auto_retry
    circuit = db.get_provider_circuit(conn, "provider-default")
    assert circuit.state == "closed"
    assert circuit.recovery_mode
    assert provider_concurrency_limit(circuit, normal_limit=4) == 1


def test_two_successes_after_probe_leave_recovery_concurrency_limit(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    for failure in range(5):
        db.record_provider_outcome(
            conn, "provider-default", outcome="provider_failure", now=_at(failure)
        )
    assert db.claim_provider_probe(
        conn,
        "provider-default",
        task_id=task.task_id,
        owner="probe",
        now=_at(64),
        lease_seconds=30,
    )
    circuit = db.finish_provider_probe(
        conn,
        "provider-default",
        owner="probe",
        outcome="success",
        now=_at(65),
    )
    assert provider_concurrency_limit(circuit, normal_limit=4) == 1
    circuit = db.record_provider_outcome(conn, "provider-default", outcome="success", now=_at(66))
    assert provider_concurrency_limit(circuit, normal_limit=4) == 1
    circuit = db.record_provider_outcome(conn, "provider-default", outcome="success", now=_at(67))
    assert provider_concurrency_limit(circuit, normal_limit=4) == 4


def test_upstream_auth_failure_is_retryable_and_opens_provider_circuit(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    claimed = claim_translation_work(
        conn,
        task.task_id,
        owner="worker-a",
        now=_at(),
        lease_seconds=30,
    )
    assert claimed.task is not None

    failed = fail_translation_work(
        conn,
        task.task_id,
        owner="worker-a",
        now=_at(1),
        error=TranslationError("secret-bearing raw error", category="authentication", status=401),
        stage="connect_provider",
    )

    assert failed.status == "retry_wait"
    assert failed.error_code == "UPSTREAM_ERROR"
    assert failed.next_retry_at is not None
    assert db.get_provider_circuit(conn, "provider-default").state == "closed"
    assert "secret-bearing" not in repr(failed)


def _succeed_task(conn, task, *, owner: str, started: int, finished: int):
    assert db.claim_translation_task(
        conn,
        task.task_id,
        owner=owner,
        now=_at(started),
        lease_seconds=30,
    )
    return db.finish_translation_task_success(
        conn, task.task_id, owner=owner, now=_at(finished)
    )


def test_build_generation_does_not_mark_late_success_online(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    first = _task(conn, article_id="first")
    second = _task(conn, article_id="second")
    db.ensure_automation_edition(
        conn, "2026-07-28", target_count=2, now=_at()
    )

    _succeed_task(conn, first, owner="translate-1", started=1, finished=2)
    db.mark_translation_ready_for_build(conn, first.task_id, now=_at(2))
    assert db.claim_automation_build(
        conn,
        "2026-07-28",
        owner="builder-1",
        now=_at(4),
        lease_seconds=30,
    )

    _succeed_task(conn, second, owner="translate-2", started=5, finished=6)
    db.mark_translation_ready_for_build(conn, second.task_id, now=_at(6))
    partial = db.finish_automation_build(
        conn,
        "2026-07-28",
        owner="builder-1",
        now=_at(7),
        succeeded=True,
    )

    assert partial.status == "build_pending"
    assert partial.built_generation == 1
    assert partial.dirty_generation == 2
    assert db.translation_task(conn, first.task_id).build_status == "online"
    assert db.translation_task(conn, second.task_id).build_status == "build_pending"

    assert db.claim_automation_build(
        conn,
        "2026-07-28",
        owner="builder-2",
        now=_at(8),
        lease_seconds=30,
        force=True,
    )
    complete = db.finish_automation_build(
        conn,
        "2026-07-28",
        owner="builder-2",
        now=_at(9),
        succeeded=True,
    )
    assert complete.status == "complete"
    assert complete.online_count == 2


def test_build_completes_when_manual_retries_add_extra_tasks(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    first = _task(conn, article_id="first")
    second = _task(conn, article_id="second")
    db.ensure_automation_edition(conn, "2026-07-28", target_count=1, now=_at())

    _succeed_task(conn, first, owner="translate-1", started=1, finished=2)
    db.mark_translation_ready_for_build(conn, first.task_id, now=_at(2), debounce_seconds=0)
    _succeed_task(conn, second, owner="translate-2", started=3, finished=4)
    db.mark_translation_ready_for_build(conn, second.task_id, now=_at(4), debounce_seconds=0)

    assert db.claim_automation_build(
        conn, "2026-07-28", owner="builder", now=_at(5), lease_seconds=30, force=True
    )
    complete = db.finish_automation_build(
        conn, "2026-07-28", owner="builder", now=_at(6), succeeded=True
    )
    assert complete.status == "complete"
    assert complete.succeeded_count == 2


def test_complete_edition_delivery_claim_is_persistent_and_single(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    task = _task(conn)
    db.ensure_automation_edition(
        conn, "2026-07-28", target_count=1, now=_at()
    )
    _succeed_task(conn, task, owner="translate", started=1, finished=2)
    db.mark_translation_ready_for_build(conn, task.task_id, now=_at(2), debounce_seconds=0)
    assert db.claim_automation_build(
        conn,
        "2026-07-28",
        owner="builder",
        now=_at(2),
        lease_seconds=30,
    )
    db.finish_automation_build(
        conn,
        "2026-07-28",
        owner="builder",
        now=_at(3),
        succeeded=True,
    )

    key = db.claim_automation_delivery(conn, "2026-07-28", now=_at(4))
    assert key is not None
    assert db.claim_automation_delivery(conn, "2026-07-28", now=_at(4)) is None
    conn.close()

    reopened = db.connect(tmp_path / "digest.db")
    assert db.claim_automation_delivery(reopened, "2026-07-28", now=_at(5)) is None
    delivered = db.finish_automation_delivery(
        reopened,
        "2026-07-28",
        delivery_key=key,
        now=_at(6),
        succeeded=True,
    )
    assert delivered.status == "delivered"
    assert db.claim_automation_delivery(reopened, "2026-07-28", now=_at(7)) is None


def test_v4_to_v7_migration_writes_verified_online_backups(tmp_path):
    path = tmp_path / "digest.db"
    conn = db.connect(path)
    conn.executescript(
        """
        DROP TABLE automation_editions;
        DROP TABLE provider_circuits;
        DROP TABLE translation_attempts;
        DROP TABLE translation_tasks;
        UPDATE meta SET value = '4' WHERE key = 'schema_version';
        """
    )
    conn.close()

    migrated = db.connect(path)
    assert migrated.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"] == "7"
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    migrated.close()

    backup_path = path.with_name("digest.db.pre-v5.bak")
    assert backup_path.is_file()
    backup = db.sqlite3.connect(backup_path)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "4"
    finally:
        backup.close()
    v6_backup = path.with_name("digest.db.pre-v6.bak")
    assert v6_backup.is_file()
    backup = db.sqlite3.connect(v6_backup)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "5"
    finally:
        backup.close()

    v7_backup = path.with_name("digest.db.pre-v7.bak")
    assert v7_backup.is_file()
    backup = db.sqlite3.connect(v7_backup)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "6"
    finally:
        backup.close()
