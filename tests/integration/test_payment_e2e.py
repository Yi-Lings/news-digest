"""Loopback HTTP integration for the EasyPay order and settlement flow."""

import datetime as dt
import hashlib
import http.client
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from news_digest import accounts
from news_digest.payments import (
    EpayConfig,
    config_identity,
    query_payment,
    sign_fields,
    signature_valid,
)
from news_digest.site_server import create_site_server
from news_digest.storage import db


class _FakeEasyPay:
    def __init__(self) -> None:
        self.pid = "1001"
        self.key = "loopback-merchant-key"
        self.payment_type = "alipay"
        self.orders: dict[str, dict[str, str]] = {}
        self.create_requests: list[dict[str, str]] = []
        self.query_requests: list[dict[str, str]] = []
        self.errors: list[str] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args) -> None:
                return

            def _form(self) -> dict[str, str]:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                return {
                    key: values[0]
                    for key, values in urllib.parse.parse_qs(
                        body, keep_blank_values=True
                    ).items()
                }

            def _json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                fields = self._form()
                if self.path == "/mapi.php":
                    gateway._create(self, fields)
                    return
                if self.path == "/api.php":
                    gateway._query(self, fields)
                    return
                self._json(404, {"code": 0})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def _reject(self, handler: BaseHTTPRequestHandler, message: str) -> None:
        self.errors.append(message)
        handler._json(400, {"code": 0, "msg": "rejected"})

    def _create(self, handler: BaseHTTPRequestHandler, fields: dict[str, str]) -> None:
        if fields.get("pid") != self.pid or fields.get("type") != self.payment_type:
            self._reject(handler, "creation identity mismatch")
            return
        if fields.get("sign_type") != "MD5" or not signature_valid(fields, self.key):
            self._reject(handler, "creation signature mismatch")
            return
        order_no = fields.get("out_trade_no", "")
        if not order_no.startswith("news_"):
            self._reject(handler, "creation namespace mismatch")
            return
        self.create_requests.append(fields)
        order = self.orders.setdefault(
            order_no,
            {
                "trade_no": "fake-trade-0001",
                "money": fields["money"],
                "trade_status": "WAIT_BUYER_PAY",
            },
        )
        handler._json(
            200,
            {
                "code": 1,
                "trade_no": order["trade_no"],
                "payurl": self.base_url
                + f"/pay/{order['trade_no']}?channel={self.payment_type}",
            },
        )

    def _query(self, handler: BaseHTTPRequestHandler, fields: dict[str, str]) -> None:
        order = self.orders.get(fields.get("out_trade_no", ""))
        if (
            fields.get("act") != "order"
            or fields.get("pid") != self.pid
            or fields.get("key") != self.key
            or order is None
        ):
            self._reject(handler, "query identity mismatch")
            return
        self.query_requests.append(fields)
        trade_status = order["trade_status"]
        payload = {
            "code": "1",
            "msg": "success",
            "pid": self.pid,
            "out_trade_no": fields["out_trade_no"],
            "trade_no": order["trade_no"],
            "money": order["money"],
            "status": "1" if trade_status == "TRADE_SUCCESS" else "0",
            "trade_status": trade_status,
            "sign_type": "MD5",
        }
        payload["sign"] = sign_fields(payload, self.key)
        handler._json(200, payload)

    def mark_paid(self, merchant_order_no: str) -> None:
        self.orders[merchant_order_no]["trade_status"] = "TRADE_SUCCESS"

    def notify(self, merchant_order_no: str, notify_url: str) -> tuple[int, str]:
        order = self.orders[merchant_order_no]
        fields = {
            "pid": self.pid,
            "trade_no": order["trade_no"],
            "out_trade_no": merchant_order_no,
            "type": self.payment_type,
            "name": "Cheapcoding News monthly plan",
            "money": order["money"],
            "trade_status": "TRADE_SUCCESS",
            "sign_type": "MD5",
        }
        fields["sign"] = sign_fields(fields, self.key)
        parsed = urllib.parse.urlsplit(notify_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
        try:
            body = urllib.parse.urlencode(fields)
            connection.request(
                "POST",
                parsed.path,
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    fields: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> tuple[int, http.client.HTTPMessage, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers: dict[str, str] = {}
    body = None
    if fields is not None:
        body = urllib.parse.urlencode(fields)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookies:
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read().decode("utf-8")
    finally:
        connection.close()


def _site_files(tmp_path: Path) -> Path:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<html><body>home</body></html>", encoding="utf-8")
    return site_dir


def test_loopback_easypay_network_order_query_callback_is_idempotent(tmp_path):
    gateway = _FakeEasyPay()
    config_holder: dict[str, EpayConfig] = {}
    site = create_site_server(
        site_dir=_site_files(tmp_path),
        db_path=tmp_path / "news.db",
        secret_file=tmp_path / "site-secret",
        port=0,
        secure_cookies=False,
        scheme="http",
        payment_config_loader=lambda: config_holder["config"],
        payment_settlement_config_loader=lambda: config_holder["config"],
    )
    site_port = site.server_address[1]
    site_url = f"http://127.0.0.1:{site_port}"
    config = EpayConfig(
        base_url=gateway.base_url,
        merchant_id=gateway.pid,
        merchant_key=gateway.key,
        payment_type=gateway.payment_type,
        site_url=site_url,
        order_ttl_seconds=300,
        amount_hold_seconds=3600,
    )
    config_holder["config"] = config
    site_thread = threading.Thread(target=site.serve_forever, daemon=True)
    site_thread.start()

    try:
        now = dt.datetime.now(dt.UTC)
        connection = db.connect(tmp_path / "news.db")
        user = db.upsert_pending_user(
            connection,
            email="network-buyer@example.com",
            email_key=db.delivery_recipient_key("network-buyer@example.com"),
            password_hash=accounts.hash_password("password123"),
            now=now.isoformat(),
        )
        user = db.activate_user(connection, email_key=user.email_key, now=now.isoformat())
        db.set_settings(
            connection,
            {"monthly_price_cents": "999", "monthly_discount_percent": "20"},
            now=now.isoformat(),
        )
        session_token = "loopback-session-token-with-enough-entropy"
        db.create_user_session(
            connection,
            token_digest=hashlib.sha256(session_token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=(now + dt.timedelta(days=1)).isoformat(),
            now=now.isoformat(),
        )
        connection.close()

        status, headers, page = _request(
            site_port,
            "GET",
            "/account",
            cookies={"nd_user_session": session_token},
        )
        assert status == 200
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        csrf_cookie = next(
            value.split(";", 1)[0].split("=", 1)[1]
            for value in headers.get_all("Set-Cookie") or []
            if value.startswith("nd_site_csrf=")
        )
        cookies = {"nd_user_session": session_token, "nd_site_csrf": csrf_cookie}
        status, headers, _page = _request(
            site_port,
            "POST",
            "/order",
            fields={"csrf": csrf, "plan": "monthly"},
            cookies=cookies,
        )
        assert status == 302
        payment_url = headers["Location"]
        assert urllib.parse.urlsplit(payment_url).netloc == f"127.0.0.1:{gateway.port}"

        connection = db.connect(tmp_path / "news.db")
        order = db.list_user_orders(connection, user_id=user.id)[0]
        assert order.status == "pending"
        assert order.base_amount_cents == 799
        assert order.amount_cents == 799
        assert order.amount_offset_cents == 0
        assert order.payment_type == "alipay"
        assert order.payment_config_id == config_identity(config)
        assert order.payment_url == payment_url
        assert order.provider_trade_no == "fake-trade-0001"
        assert db.user_by_id(connection, user.id).paid_until is None
        connection.close()

        created = gateway.create_requests[0]
        assert created["out_trade_no"] == order.merchant_order_no
        assert created["money"] == "7.99"
        assert created["notify_url"] == site_url + "/subscribe/api/payment/easypay"
        assert created["return_url"] == site_url + "/payment/return"

        pending = query_payment(
            config,
            merchant_order_no=order.merchant_order_no,
            expected_amount_cents=order.amount_cents,
        )
        assert pending.trade_status == "WAIT_BUYER_PAY"
        gateway.mark_paid(order.merchant_order_no)
        paid = query_payment(
            config,
            merchant_order_no=order.merchant_order_no,
            expected_amount_cents=order.amount_cents,
        )
        assert paid.trade_status == "TRADE_SUCCESS"
        assert len(gateway.query_requests) == 2

        notify_url = created["notify_url"]
        assert gateway.notify(order.merchant_order_no, notify_url) == (200, "success")
        connection = db.connect(tmp_path / "news.db")
        settled = db.order_by_id(connection, order.id)
        first_paid_until = db.user_by_id(connection, user.id).paid_until
        connection.close()
        assert settled is not None
        assert settled.status == "paid"
        assert settled.provider_trade_no == paid.provider_trade_no
        assert settled.amount_cents == paid.amount_cents
        assert settled.paid_at is not None
        assert first_paid_until is not None

        assert gateway.notify(order.merchant_order_no, notify_url) == (200, "success")
        connection = db.connect(tmp_path / "news.db")
        assert db.order_by_id(connection, order.id).status == "paid"
        assert db.user_by_id(connection, user.id).paid_until == first_paid_until
        connection.close()
        assert gateway.errors == []
    finally:
        site.shutdown()
        site_thread.join()
        site.server_close()
        gateway.close()
