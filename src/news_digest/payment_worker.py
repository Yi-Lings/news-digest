"""Bounded account settlement work, independent of the translation worker lock."""

import datetime as dt
from dataclasses import replace
from pathlib import Path

from news_digest import accounts, payments
from news_digest.storage import db


def settlement_config(order, config):
    if config is None:
        raise payments.PaymentError("payment settlement configuration is unavailable")
    if order.payment_type in {"alipay", "wxpay"}:
        config = replace(config, payment_type=order.payment_type)
    if order.payment_config_id and payments.config_identity(config) != order.payment_config_id:
        raise payments.PaymentError("payment settlement configuration does not match")
    return config


def reconcile(db_path: Path, order, config, query_callback):
    config = settlement_config(order, config)
    result = query_callback(
        config, merchant_order_no=order.merchant_order_no, expected_amount_cents=order.amount_cents
    )
    if (
        result.merchant_order_no != order.merchant_order_no
        or result.amount_cents != order.amount_cents
    ):
        raise payments.PaymentError("payment query identity does not match")
    conn = db.connect(db_path)
    try:
        now = dt.datetime.now(dt.UTC).isoformat()
        if result.trade_status == "TRADE_SUCCESS":
            return db.confirm_payment_order(
                conn,
                merchant_order_no=result.merchant_order_no,
                provider_trade_no=result.provider_trade_no,
                amount_cents=result.amount_cents,
                now=now,
                amount_hold_seconds=config.amount_hold_seconds,
                plan_days=accounts.PLAN_DAYS,
            )
        return db.record_payment_query_status(
            conn,
            order_id=order.id,
            trade_status=result.trade_status,
            expected_updated_at=order.updated_at,
            now=now,
        )
    finally:
        conn.close()


def run_once(db_path: Path, config_loader, query_callback, *, owner: str) -> bool:
    conn = db.connect(db_path)
    try:
        order = db.claim_payment_check(conn, now=dt.datetime.now(dt.UTC).isoformat(), owner=owner)
    finally:
        conn.close()
    if order is None:
        return False
    error = None
    try:
        reconcile(db_path, order, config_loader(), query_callback)
    except Exception as exc:  # a durable claim must be released even on configuration failure
        error = type(exc).__name__
    conn = db.connect(db_path)
    try:
        db.finish_payment_check(
            conn,
            order_id=order.id,
            owner=owner,
            now=dt.datetime.now(dt.UTC).isoformat(),
            error=error,
        )
    finally:
        conn.close()
    return True
