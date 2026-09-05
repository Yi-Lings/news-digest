import datetime as dt
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from news_digest import accounts
from news_digest.storage import db

NOW = "2026-09-05T10:00:00+00:00"


def make_user(conn):
    user = db.upsert_pending_user(
        conn, email="test@example.com", email_key="a" * 64, password_hash="test-hash", now=NOW
    )
    db.activate_user(conn, email_key=user.email_key, now=NOW)
    return user


def make_order(conn, user):
    return db.create_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_t28",
        now=NOW,
        ttl_seconds=300,
        amount_hold_seconds=3600,
    )


def confirm(conn, order, now=NOW):
    return db.confirm_payment_order(
        conn,
        merchant_order_no=order.merchant_order_no,
        provider_trade_no="trade_t28",
        amount_cents=order.amount_cents,
        now=now,
        amount_hold_seconds=3600,
        plan_days=accounts.PLAN_DAYS,
    )


def test_concurrent_grants_and_payment_keep_all_days(tmp_path):
    path = tmp_path / "news.db"
    conn = db.connect(path)
    user = make_user(conn)
    order = make_order(conn, user)

    def grant(index):
        other = db.connect(path)
        try:
            if index == 0:
                confirm(other, order)
            else:
                db.add_membership_days(
                    other,
                    user.id,
                    plan="yearly",
                    days=10,
                    operation_id=f"grant-{index}",
                    actor="admin",
                    reason="test",
                    now=NOW,
                )
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(grant, [0, 1, 2, 1, 0]))
    current = db.user_by_id(conn, user.id)
    assert (
        current.paid_until
        == (
            dt.datetime.fromisoformat(NOW) + dt.timedelta(days=accounts.PLAN_DAYS["monthly"] + 20)
        ).isoformat()
    )
    assert current.plan == "yearly"
    assert len(db.list_entitlement_changes(conn, user_id=user.id)) == 3
    with pytest.raises(RuntimeError, match="membership changed"):
        db.clear_user_subscription(
            conn, user.id, now=NOW, check_expected=True, expected_paid_until=None
        )
    assert db.user_by_id(conn, user.id).paid_until == current.paid_until
    conn.close()


@pytest.mark.parametrize("cancelled", [False, True])
def test_late_money_retained_and_granted_once(tmp_path, cancelled):
    conn = db.connect(tmp_path / "news.db")
    user = make_user(conn)
    order = make_order(conn, user)
    if cancelled:
        db.cancel_user_payment_order(conn, order_id=order.id, user_id=user.id, now=NOW)
        assert db.order_by_id(conn, order.id).settlement_expires_at == order.settlement_expires_at
    paid = confirm(conn, order, NOW if cancelled else "2026-09-06T10:00:00+00:00")
    assert paid.last_error_code == "PAYMENT_REVIEW"
    assert db.user_by_id(conn, user.id).paid_until is None
    assert db.list_payment_cases(conn)[0]["state"] == "received"
    for _ in range(2):
        db.resolve_payment_case(
            conn,
            order_id=order.id,
            action="grant",
            reference="checked-1",
            days=0,
            operation_id="resolve-1",
            actor="admin",
            now=NOW,
        )
    assert db.order_by_id(conn, order.id).status == "paid"
    until = db.user_by_id(conn, user.id).paid_until
    confirm(conn, order)
    assert db.user_by_id(conn, user.id).paid_until == until
    assert len(db.list_entitlement_changes(conn, user_id=user.id)) == 1
    conn.close()


def test_refund_requires_evidence_and_deducts_only_explicit_days(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    user = make_user(conn)
    order = make_order(conn, user)
    confirm(conn, order)
    db.add_membership_days(
        conn,
        user.id,
        plan="monthly",
        days=10,
        operation_id="other-purchase",
        actor="admin",
        reason="test",
        now=NOW,
    )
    for _ in range(2):
        db.resolve_payment_case(
            conn,
            order_id=order.id,
            action="refunded",
            reference="refund-1",
            days=accounts.PLAN_DAYS["monthly"],
            operation_id="refund-command",
            actor="admin",
            now=NOW,
        )
    assert db.user_by_id(conn, user.id).paid_until == "2026-09-15T10:00:00+00:00"
    confirm(conn, order)
    assert db.user_by_id(conn, user.id).paid_until == "2026-09-15T10:00:00+00:00"
    assert db.list_payment_cases(conn)[0]["state"] == "refunded"
    assert db.list_payment_cases(conn, order_ids=[order.id])[0]["state"] == "refunded"
    assert db.list_payment_cases(conn, order_ids=[order.id + 1]) == []
    assert db.list_payment_cases(conn, order_ids=[]) == []
    conn.close()


def test_payment_checks_are_leased_bounded_and_retained(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    user = make_user(conn)
    order = make_order(conn, user)
    for index in range(8):
        now = (dt.datetime.fromisoformat(NOW) + dt.timedelta(hours=index + 1)).isoformat()
        assert db.claim_payment_check(conn, now=now, owner="one").id == order.id
        assert db.claim_payment_check(conn, now=now, owner="two") is None
        db.finish_payment_check(conn, order_id=order.id, owner="one", now=now, error="Timeout")
    assert db.claim_payment_check(conn, now="2026-09-10T10:00:00+00:00", owner="one") is None
    assert db.list_payment_cases(conn)[0]["state"] == "unconfirmed"
    conn.close()


def test_last_payment_check_crash_becomes_unconfirmed(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    user = make_user(conn)
    order = make_order(conn, user)
    assert db.claim_payment_check(conn, now="2026-09-05T11:00:00+00:00", owner="dead")
    conn.execute("UPDATE payment_checks SET attempts=8 WHERE order_id=?", (order.id,))
    conn.commit()
    assert db.claim_payment_check(conn, now="2026-09-05T11:03:00+00:00", owner="new") is None
    assert db.list_payment_cases(conn)[0]["state"] == "unconfirmed"
    conn.close()


def test_legacy_paid_order_dispute_cannot_grant_membership_again(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    user = make_user(conn)
    order = make_order(conn, user)
    confirm(conn, order)
    conn.execute("DELETE FROM entitlement_changes")
    conn.commit()
    before = db.user_by_id(conn, user.id).paid_until
    db.resolve_payment_case(
        conn,
        order_id=order.id,
        action="disputed",
        reference="legacy-review",
        days=0,
        operation_id="dispute",
        actor="admin",
        now=NOW,
    )
    with pytest.raises(RuntimeError, match="already granted"):
        db.resolve_payment_case(
            conn,
            order_id=order.id,
            action="grant",
            reference="legacy-review",
            days=0,
            operation_id="grant",
            actor="admin",
            now=NOW,
        )
    assert db.user_by_id(conn, user.id).paid_until == before
    conn.close()


def test_email_confirmation_failure_does_not_consume_code(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    user = db.upsert_pending_user(
        conn, email="test@example.com", email_key="a" * 64, password_hash="hash", now=NOW
    )
    db.issue_email_code(
        conn,
        email_key=user.email_key,
        purpose="register",
        code_digest="b" * 64,
        ttl_seconds=600,
        now=NOW,
    )
    conn.execute(
        "CREATE TRIGGER fail_activation BEFORE UPDATE ON users "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.consume_email_code(
            conn,
            email_key=user.email_key,
            purpose="register",
            code_digest="b" * 64,
            complete_user=True,
            now=NOW,
        )
    conn.execute("DROP TRIGGER fail_activation")
    assert db.consume_email_code(
        conn,
        email_key=user.email_key,
        purpose="register",
        code_digest="b" * 64,
        complete_user=True,
        now=NOW,
    )
    assert db.user_by_id(conn, user.id).status == "active"
    conn.close()
