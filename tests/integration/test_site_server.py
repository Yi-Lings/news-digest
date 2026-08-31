"""站点服务集成测试:注册/登录、付费墙矩阵、免费额度、订单与卡密、付费开关。"""

import datetime as dt
import hashlib
import http.client
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from news_digest import accounts
from news_digest.accounts import generate_redemption_code, redemption_digest, redemption_prefix
from news_digest.cli import _run_site_admin
from news_digest.payments import (
    EpayConfig,
    PaymentCreation,
    PaymentError,
    PaymentQuery,
    config_identity,
    sign_fields,
)
from news_digest.site_server import create_site_server, load_site_secret
from news_digest.storage import db

NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.UTC)
RELEASE_DATE = "2026-08-30"
ARCHIVE_DATE = "2026-08-29"


def _write_site(tmp_path: Path) -> Path:
    site = tmp_path / "site" / "current"
    for date in (RELEASE_DATE, ARCHIVE_DATE):
        issue = site / "issues" / date
        issue.mkdir(parents=True, exist_ok=True)
        (issue / "index.html").write_text(
            f"<html><body>issue {date}</body></html>", encoding="utf-8"
        )
        (issue / "lead-story.html").write_text(
            f"<html><head><title>Lead {date}</title></head><body>full text {date}</body></html>",
            encoding="utf-8",
        )
        (issue / "second-story.html").write_text(
            f"<html><body>second {date}</body></html>", encoding="utf-8"
        )
    (site / "index.html").write_text(
        "<html><body>home<!--ADMIN_NAV--></body></html>", encoding="utf-8"
    )
    assets = site / "assets"
    assets.mkdir()
    (assets / "style.css").write_text("body { color: black; }", encoding="utf-8")
    (assets / "app.js").write_text('"use strict";', encoding="utf-8")
    (site / "release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_name": f"{RELEASE_DATE}-01",
                "release_date": RELEASE_DATE,
                "published_at": NOW.isoformat(),
                "edition_sha256": "0" * 64,
                "edition": {
                    "date": RELEASE_DATE,
                    "articles": [{"slug": "lead-story"}, {"slug": "second-story"}],
                },
            }
        ),
        encoding="utf-8",
    )
    return site


class SiteHarness:
    def __init__(
        self,
        tmp_path: Path,
        code_sender=None,
        *,
        loopback_browser_compat: bool = False,
        payment_config: EpayConfig | None | bool = True,
        payment_config_loader=None,
        payment_settlement_config_loader=None,
        payment_create_callback=None,
        payment_query_callback=None,
        site_url: str = "",
    ):
        self.db_path = tmp_path / "news.db"
        self.sent_codes: list[tuple[str, str, str]] = []

        def fake_sender(email: str, code: str, purpose: str) -> None:
            self.sent_codes.append((email, code, purpose))

        self.payment_config = (
            EpayConfig(
                base_url="https://pay.example.test",
                merchant_id="1001",
                merchant_key="merchant-secret",
                payment_type="alipay",
                site_url="http://127.0.0.1",
                order_ttl_seconds=300,
                amount_hold_seconds=3600,
            )
            if payment_config is True
            else payment_config
        )
        self.payment_create_calls = []

        def fake_payment_create(config, **kwargs):
            self.payment_create_calls.append((config, kwargs))
            return PaymentCreation(
                provider_trade_no="gateway-10001",
                payment_url="https://pay.example.test/pay/gateway-10001",
            )

        def fake_payment_query(_config, **kwargs):
            return PaymentQuery(
                merchant_order_no=kwargs["merchant_order_no"],
                provider_trade_no="gateway-10001",
                amount_cents=kwargs["expected_amount_cents"],
                trade_status="WAIT_BUYER_PAY",
            )

        self.server = create_site_server(
            site_dir=_write_site(tmp_path),
            db_path=self.db_path,
            secret_file=tmp_path / "site-secret",
            port=0,
            secure_cookies=bool(site_url),
            scheme="https" if site_url else "http",
            site_url=site_url,
            code_sender=code_sender or fake_sender,
            loopback_browser_compat=loopback_browser_compat,
            payment_config=self.payment_config,
            payment_config_loader=payment_config_loader,
            payment_settlement_config_loader=payment_settlement_config_loader,
            payment_create_callback=payment_create_callback or fake_payment_create,
            payment_query_callback=payment_query_callback or fake_payment_query,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, method: str, path: str, body: str = "", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body or None, headers=headers or {})
            response = conn.getresponse()
            content = response.read().decode("utf-8")
            return response.status, response.headers, content
        finally:
            conn.close()

    def get(
        self,
        path: str,
        cookies: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        headers = dict(extra_headers or {})
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return self.request("GET", path, headers=headers)

    def post(
        self,
        path: str,
        fields: dict[str, str],
        cookies: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        *,
        wait_for_mail: bool = True,
    ):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.update(extra_headers or {})
        response = self.request(
            "POST", path, body=urllib.parse.urlencode(fields), headers=headers
        )
        if wait_for_mail and path in {"/register", "/forgot"}:
            self.server.wait_for_account_mail_idle()
        return response

    def csrf_pair(self, page: str, headers) -> tuple[str, dict[str, str]]:
        nonce_cookie = None
        for chunk in headers.get_all("Set-Cookie") or []:
            if chunk.startswith("nd_site_csrf="):
                nonce_cookie = chunk.split(";", 1)[0]
        assert nonce_cookie, "expected CSRF nonce cookie"
        token = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        return token, {"nd_site_csrf": nonce_cookie.split("=", 1)[1]}

    def registration_fields(
        self, page: str, email: str, password: str = "password123"
    ) -> dict[str, str]:
        captcha_id = page.split('name="captcha_id" value="', 1)[1].split('"', 1)[0]
        with self.server.captcha_lock:
            captcha_answer = self.server.captchas[captcha_id][0]
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        return {
            "csrf": csrf,
            "email": email,
            "password": password,
            "password_confirm": password,
            "captcha_id": captcha_id,
            "captcha_answer": captcha_answer,
        }

    def member_session(
        self, email: str, *, paid: bool = False, is_admin: bool = False
    ) -> tuple[db.User, dict[str, str]]:
        conn = db.connect(self.db_path)
        user = db.upsert_pending_user(
            conn,
            email=email,
            email_key=db.delivery_recipient_key(email),
            password_hash=accounts.hash_password("password123"),
            now=NOW.isoformat(),
        )
        user = db.activate_user(conn, email_key=user.email_key, now=NOW.isoformat())
        if is_admin:
            user = db.set_user_admin(conn, user.id, is_admin=True, now=NOW.isoformat())
        if paid:
            user = db.grant_paid_until(
                conn,
                user.id,
                plan="monthly",
                paid_until=(NOW + dt.timedelta(days=31)).isoformat(),
                now=NOW.isoformat(),
            )
        token = f"session-token-{user.id}-with-enough-entropy-123456"
        db.create_user_session(
            conn,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=(NOW + dt.timedelta(days=3650)).isoformat(),
            now=NOW.isoformat(),
        )
        conn.close()
        return user, {"nd_user_session": token}

    def set_paywall(self, enabled: bool) -> None:
        conn = db.connect(self.db_path)
        db.set_settings(
            conn, {"paywall_enabled": "true" if enabled else "false"}, now=NOW.isoformat()
        )
        conn.close()

    def anon_cookie(self, headers) -> dict[str, str]:
        for chunk in headers.get_all("Set-Cookie") or []:
            if chunk.startswith("nd_anon="):
                return {"nd_anon": chunk.split(";", 1)[0].split("=", 1)[1]}
        return {}


@pytest.fixture()
def site(tmp_path):
    harness = SiteHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.stop()


def test_opaque_origin_requires_explicit_loopback_browser_compat(tmp_path):
    strict = SiteHarness(tmp_path / "strict")
    try:
        strict.member_session("strict@example.com")
        status, headers, page = strict.get("/login")
        token, cookies = strict.csrf_pair(page, headers)
        status, _headers, page = strict.post(
            "/login",
            {"csrf": token, "email": "strict@example.com", "password": "password123"},
            cookies=cookies,
            extra_headers={"Origin": "null"},
        )
        assert status == 403 and "来源校验失败" in page
    finally:
        strict.stop()

    compat = SiteHarness(tmp_path / "compat", loopback_browser_compat=True)
    try:
        compat.member_session("compat@example.com")
        status, headers, page = compat.get("/login")
        token, cookies = compat.csrf_pair(page, headers)
        status, headers, _page = compat.post(
            "/login",
            {"csrf": token, "email": "compat@example.com", "password": "password123"},
            cookies=cookies,
            extra_headers={"Origin": "null"},
        )
        assert status == 302
        assert any(
            value.startswith("nd_user_session=")
            for value in headers.get_all("Set-Cookie") or []
        )

        status, headers, page = compat.get("/login")
        token, cookies = compat.csrf_pair(page, headers)
        status, _headers, _page = compat.post(
            "/login",
            {"csrf": token, "email": "compat@example.com", "password": "password123"},
            cookies=cookies,
            extra_headers={
                "Origin": "null",
                "Host": f"rebind.example:{compat.port}",
            },
        )
        assert status == 403
    finally:
        compat.stop()


def test_site_rejects_matching_rebinding_host_and_accepts_configured_production_host(
    tmp_path,
):
    local = SiteHarness(tmp_path / "local")
    try:
        status, headers, page = local.get("/login")
        token, cookies = local.csrf_pair(page, headers)
        rebound = f"rebind.example:{local.port}"
        status, _headers, page = local.post(
            "/login",
            {"csrf": token, "email": "missing@example.com", "password": "password123"},
            cookies=cookies,
            extra_headers={"Host": rebound, "Origin": f"http://{rebound}"},
        )
        assert status == 403 and "来源校验失败" in page
    finally:
        local.stop()

    production = SiteHarness(
        tmp_path / "production", site_url="https://news.example.test"
    )
    try:
        trusted = {"Host": "news.example.test", "Origin": "https://news.example.test"}
        status, headers, page = production.get("/login", extra_headers=trusted)
        token, cookies = production.csrf_pair(page, headers)
        status, _headers, page = production.post(
            "/login",
            {"csrf": token, "email": "missing@example.com", "password": "password123"},
            cookies=cookies,
            extra_headers=trusted,
        )
        assert status == 200 and "邮箱或密码不正确" in page

        rebound = "rebind.example"
        status, _headers, page = production.post(
            "/login",
            {"csrf": token, "email": "missing@example.com", "password": "password123"},
            cookies=cookies,
            extra_headers={"Host": rebound, "Origin": f"https://{rebound}"},
        )
        assert status == 403 and "来源校验失败" in page
    finally:
        production.stop()


class TestStaticAndPaywall:
    def test_mutable_pages_styles_and_scripts_are_not_served_stale(self, site):
        for path in ("/", "/assets/style.css", "/assets/app.js"):
            status, headers, _content = site.get(path)
            assert status == 200
            assert headers["Cache-Control"] == "no-store"

    def test_subscription_page_displays_discounted_prices(self, site):
        conn = db.connect(site.db_path)
        db.set_settings(
            conn,
            {
                "monthly_list_price_cents": "3600",
                "monthly_price_cents": "990",
                "yearly_list_price_cents": "10000",
                "yearly_price_cents": "9000",
                "payment_qr_data_url": "data:image/png;base64,legacy",
            },
            now=NOW.isoformat(),
        )
        conn.close()
        status, headers, content = site.get("/subscribe")
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert content.count('<span class="price-original">') == 2
        assert "月刊会员" in content and "年刊会员" in content
        assert "包月" not in content and "包年" not in content
        assert "-72.5%" in content and "¥9.9" in content
        assert "-10%" in content and "¥90" in content
        assert "收款二维码" not in content and "data:image/png" not in content
        assert "支付成功后自动开通" in content
        assert "form-action 'self' https://pay.example.test" in headers[
            "Content-Security-Policy"
        ]
        assert accounts.price_cents(
            {"monthly_price_cents": "999", "monthly_discount_percent": "20"},
            "monthly",
        ) == 799

    def test_payment_csp_allows_only_the_configured_gateway_origin(self, tmp_path):
        configured = EpayConfig(
            base_url="https://pay.example.test/gateway",
            merchant_id="1001",
            merchant_key="merchant-secret",
            payment_type="alipay",
            site_url="http://127.0.0.1",
        )
        enabled = SiteHarness(tmp_path / "enabled", payment_config=configured)
        disabled = SiteHarness(tmp_path / "disabled", payment_config=None)
        try:
            status, headers, _page = enabled.get("/subscribe")
            policy = headers["Content-Security-Policy"]
            assert status == 200
            assert "form-action 'self' https://pay.example.test" in policy
            assert "pay.example.test/gateway" not in policy

            status, headers, _page = disabled.get("/subscribe")
            assert status == 200
            assert "form-action 'self'" in headers["Content-Security-Policy"]
            assert "pay.example.test" not in headers["Content-Security-Policy"]
        finally:
            enabled.stop()
            disabled.stop()

    def test_paywall_off_serves_archive(self, site):
        site.set_paywall(False)
        status, _headers, content = site.get(f"/issues/{ARCHIVE_DATE}/lead-story.html")
        assert status == 200
        assert "full text" in content

    def test_archive_locked_when_paywall_on(self, site):
        site.set_paywall(True)
        status, headers, content = site.get(f"/issues/{ARCHIVE_DATE}/lead-story.html")
        assert status == 200
        assert "full text" not in content
        assert "归档" in content
        assert headers["Cache-Control"] == "no-store"

    def test_issue_index_stays_public(self, site):
        site.set_paywall(True)
        status, _headers, content = site.get(f"/issues/{ARCHIVE_DATE}/")
        assert status == 200
        assert f"issue {ARCHIVE_DATE}" in content

    def test_latest_edition_first_click_free_then_locked(self, site):
        site.set_paywall(True)
        status, headers, content = site.get(f"/issues/{RELEASE_DATE}/lead-story.html")
        assert status == 200 and "full text" not in content
        assert "确认使用今日免费额度" in content
        anon = site.anon_cookie(headers)
        assert anon, "first gated visit must issue the anonymous identity"
        token, csrf_cookie = site.csrf_pair(content, headers)
        conn = db.connect(site.db_path)
        assert conn.execute("SELECT COUNT(*) FROM free_reads").fetchone()[0] == 0
        conn.close()
        status, _headers, content = site.post(
            "/free-read",
            {
                "csrf": token,
                "article_path": f"/issues/{RELEASE_DATE}/lead-story.html",
            },
            cookies={**anon, **csrf_cookie},
        )
        assert status == 200 and "full text" in content
        # 同一匿名身份回看第一篇:放行。
        _status, _headers, content = site.get(
            f"/issues/{RELEASE_DATE}/lead-story.html", cookies=anon
        )
        assert "full text" in content
        # 同一匿名身份看另一篇:付费墙。
        _status, _headers, content = site.get(
            f"/issues/{RELEASE_DATE}/second-story.html", cookies=anon
        )
        assert "full text" not in content
        assert "名额" in content

    def test_paid_user_reads_archive(self, site):
        site.set_paywall(True)
        conn = db.connect(site.db_path)
        user = db.upsert_pending_user(
            conn,
            email="paid@example.com",
            email_key=db.delivery_recipient_key("paid@example.com"),
            password_hash="pbkdf2_sha256$1$00$00",
            now=NOW.isoformat(),
        )
        db.activate_user(conn, email_key=user.email_key, now=NOW.isoformat())
        db.grant_paid_until(
            conn,
            user.id,
            plan="monthly",
            paid_until=(NOW + dt.timedelta(days=30)).isoformat(),
            now=NOW.isoformat(),
        )
        refreshed = db.user_by_id(conn, user.id)
        token = "paid-session-token-with-enough-entropy"
        db.create_user_session(
            conn,
            token_digest=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=(NOW + dt.timedelta(days=3650)).isoformat(),
            now=NOW.isoformat(),
        )
        conn.close()
        assert refreshed.status == "active"
        status, headers, content = site.get(
            f"/issues/{ARCHIVE_DATE}/lead-story.html",
            cookies={"nd_user_session": token},
        )
        assert status == 200 and "full text" in content
        assert headers["Cache-Control"] == "no-store"
        assert "img-src 'self' data: https:" in headers["Content-Security-Policy"]
        assert "style-src 'self' 'unsafe-inline'" in headers["Content-Security-Policy"]


class TestMembership:
    def test_newsletter_requires_paid_member_and_can_be_toggled(self, site):
        user, cookies = site.member_session("member@example.com")
        status, headers, page = site.get("/subscribe", cookies=cookies)
        assert status == 200
        assert page.count('form method="post" action="/order"') == 2
        assert 'href="/register">注册</a>或<a href="/login">登录</a>后' not in page
        assert "也可以在「我的账户」兑换卡密" in page
        assert "只有有效付费会员" not in page
        assert "当前账号尚无有效付费会员" in page
        assert 'action="/newsletter"' not in page

        conn = db.connect(site.db_path)
        db.grant_paid_until(
            conn,
            user.id,
            plan="monthly",
            paid_until=(NOW + dt.timedelta(days=31)).isoformat(),
            now=NOW.isoformat(),
        )
        db.set_settings(
            conn,
            {"monthly_discount_percent": "20", "yearly_discount_percent": "30"},
            now=NOW.isoformat(),
        )
        conn.close()
        status, headers, page = site.get("/subscribe", cookies=cookies)
        token, csrf_cookie = site.csrf_pair(page, headers)
        assert status == 200 and 'action="/newsletter"' in page
        assert "订阅每日简报" in page
        assert 'class="plans-grid"' in page
        assert 'class="discount-badge"' in page
        status, _headers, page = site.post(
            "/newsletter",
            {"csrf": token, "action": "enable"},
            cookies={**cookies, **csrf_cookie},
        )
        assert status == 200 and "每日简报订阅已更新" in page
        conn = db.connect(site.db_path)
        assert db.subscription_by_email(conn, user.email).status == "active"
        conn.close()

        token = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        status, _headers, page = site.post(
            "/newsletter",
            {"csrf": token, "action": "disable"},
            cookies={**cookies, **csrf_cookie},
        )
        assert status == 200
        conn = db.connect(site.db_path)
        assert db.subscription_by_email(conn, user.email).status == "unsubscribed"
        conn.close()

    def test_admin_navigation_and_admin_cookie_only_for_admin_account(self, site):
        status, _headers, page = site.get("/")
        assert status == 200 and 'href="/admin/"' not in page
        _user, ordinary_cookie = site.member_session("ordinary@example.com")
        status, _headers, page = site.get("/", cookies=ordinary_cookie)
        assert status == 200 and 'href="/admin/"' not in page

        site.member_session("admin@example.com", is_admin=True)
        status, headers, page = site.get("/login")
        token, csrf_cookie = site.csrf_pair(page, headers)
        status, headers, _page = site.post(
            "/login",
            {"csrf": token, "email": "admin@example.com", "password": "password123"},
            cookies=csrf_cookie,
        )
        assert status == 302
        set_cookies = headers.get_all("Set-Cookie") or []
        assert any(value.startswith("nd_user_session=") for value in set_cookies)
        assert any(value.startswith("nd_admin_session=") for value in set_cookies)
        user_token = next(
            value.split(";", 1)[0].split("=", 1)[1]
            for value in set_cookies
            if value.startswith("nd_user_session=")
        )
        status, _headers, page = site.get(
            "/", cookies={"nd_user_session": user_token}
        )
        assert status == 200 and 'class="admin-entry" href="/admin/"' in page

    def test_ordinary_login_clears_stale_admin_cookie(self, site):
        site.member_session("ordinary-login@example.com")
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        cookies["nd_admin_session"] = "stale-admin-session"
        status, headers, _page = site.post(
            "/login",
            {
                "csrf": token,
                "email": "ordinary-login@example.com",
                "password": "password123",
            },
            cookies=cookies,
        )
        assert status == 302
        set_cookies = headers.get_all("Set-Cookie") or []
        assert any(
            value.startswith("nd_admin_session=;") and "Max-Age=0" in value
            for value in set_cookies
        )


class TestRegistration:
    def test_login_and_registration_pages_are_separate(self, site):
        status, _headers, login_page = site.get("/login")
        assert status == 200
        assert 'action="/login"' in login_page
        assert 'href="/register"' in login_page
        assert 'action="/register"' not in login_page
        assert 'action="/verify"' not in login_page

        status, _headers, register_page = site.get("/register")
        assert status == 200
        assert 'action="/register"' in register_page
        assert 'action="/verify"' not in register_page
        assert 'name="password_confirm"' in register_page
        assert 'name="captcha_answer"' in register_page
        assert "随机图形验证码" in register_page
        assert "获取邮箱验证码" in register_page
        assert 'action="/login"' not in register_page
        assert 'href="/login"' in register_page

    def test_password_confirmation_and_captcha_are_required(self, site):
        status, headers, page = site.get("/register")
        _token, cookies = site.csrf_pair(page, headers)
        fields = site.registration_fields(page, "mismatch@example.com")
        fields["password_confirm"] = "different-password"
        status, _headers, content = site.post("/register", fields, cookies=cookies)
        assert status == 200 and "两次输入的密码不一致" in content
        status, headers, page = site.get("/register")
        _token, cookies = site.csrf_pair(page, headers)
        fields = site.registration_fields(page, "captcha@example.com")
        fields["captcha_answer"] = "WRONG"
        status, _headers, content = site.post("/register", fields, cookies=cookies)
        assert status == 200 and "图形验证码错误或已过期" in content
        assert not site.sent_codes

    def test_email_dimension_rate_limit_is_independent_of_client(self, site):
        email_key = db.delivery_recipient_key("limited@example.com")
        now = dt.datetime.now(dt.UTC).timestamp()
        for index in range(site.server.limiter.limit):
            assert site.server.limiter.allow(f"client-{index}", "login", now)
            assert site.server.limiter.allow(f"email:{email_key}", "login", now)
        assert not site.server.limiter.allow(f"email:{email_key}", "login", now)

    def test_loopback_proxy_uses_real_client_ip_for_rate_limit(self, site):
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        for index in range(site.server.limiter.limit):
            status, _headers, _page = site.post(
                "/register",
                {
                    "csrf": token,
                    "email": f"bot-{index}@example.com",
                    "password": "password123",
                    "website": "filled",
                },
                cookies=cookies,
                extra_headers={"X-Real-IP": "198.51.100.10"},
            )
            assert status == 200
        status, _headers, _page = site.post(
            "/register",
            {
                "csrf": token,
                "email": "other-client@example.com",
                "password": "password123",
                "website": "filled",
            },
            cookies=cookies,
            extra_headers={"X-Real-IP": "198.51.100.11"},
        )
        assert status == 200

    def test_existing_new_account_and_honeypot_use_constant_response(self, site):
        conn = db.connect(site.db_path)
        user = db.upsert_pending_user(
            conn,
            email="existing@example.com",
            email_key=db.delivery_recipient_key("existing@example.com"),
            password_hash="pbkdf2_sha256$1$00$00",
            now=NOW.isoformat(),
        )
        db.activate_user(conn, email_key=user.email_key, now=NOW.isoformat())
        conn.close()
        status, headers, page = site.get("/register")
        _token, cookies = site.csrf_pair(page, headers)
        before = len(site.sent_codes)
        status, _headers, existing_page = site.post(
            "/register",
            site.registration_fields(page, "existing@example.com"),
            cookies=cookies,
        )

        status, headers, page = site.get("/register")
        _token, new_cookies = site.csrf_pair(page, headers)
        status, _headers, new_page = site.post(
            "/register",
            site.registration_fields(page, "new-response@example.com"),
            cookies=new_cookies,
        )
        token = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        status, _headers, honeypot_page = site.post(
            "/register",
            {
                "csrf": token,
                "email": "bot@example.com",
                "password": "password123",
                "website": "https://spam.invalid",
            },
            cookies=new_cookies,
        )
        assert status == 200
        for response in (existing_page, new_page, honeypot_page):
            assert "如果该邮箱可以注册" in response
            assert 'action="/verify"' in response

        def normalized(response: str, email: str) -> str:
            response = response.replace(email, "{email}")
            return re.sub(
                r'name="csrf" value="[^"]+"',
                'name="csrf" value="{csrf}"',
                response,
            )

        expected = normalized(new_page, "new-response@example.com")
        assert normalized(existing_page, "existing@example.com") == expected
        assert normalized(honeypot_page, "bot@example.com") == expected
        assert len(site.sent_codes) == before + 1
        assert site.sent_codes[-1][0] == "new-response@example.com"

    def test_failed_code_delivery_stays_durable_for_retry(self, tmp_path):
        def failing_sender(_email: str, _code: str, _purpose: str) -> None:
            raise RuntimeError("smtp unavailable")

        harness = SiteHarness(tmp_path, code_sender=failing_sender)
        try:
            status, headers, page = harness.get("/register")
            _token, cookies = harness.csrf_pair(page, headers)
            status, _headers, page = harness.post(
                "/register",
                harness.registration_fields(page, "failed@example.com"),
                cookies=cookies,
            )
            assert status == 200
            assert "如果该邮箱可以注册" in page
            assert 'action="/verify"' in page
            conn = db.connect(harness.db_path)
            try:
                assert conn.execute(
                    "SELECT COUNT(*) FROM email_codes WHERE purpose = 'register'"
                ).fetchone()[0] == 1
                assert conn.execute(
                    "SELECT status FROM account_mail_outbox WHERE purpose = 'register'"
                ).fetchone()[0] == "pending"
            finally:
                conn.close()
        finally:
            harness.stop()

    def test_registration_response_does_not_wait_for_smtp(self, tmp_path):
        entered = threading.Event()
        release = threading.Event()
        delivered = []

        def blocking_sender(email: str, code: str, purpose: str) -> None:
            delivered.append((email, code, purpose))
            entered.set()
            assert release.wait(5)

        harness = SiteHarness(tmp_path, code_sender=blocking_sender)
        try:
            status, headers, page = harness.get("/register")
            _token, cookies = harness.csrf_pair(page, headers)
            started = time.monotonic()
            status, _headers, _page = harness.post(
                "/register",
                harness.registration_fields(page, "nonblocking@example.com"),
                cookies=cookies,
                wait_for_mail=False,
            )
            elapsed = time.monotonic() - started
            assert status == 200 and elapsed < 0.5
            assert entered.wait(1)
            conn = db.connect(harness.db_path)
            try:
                assert db.account_mail_ready(
                    conn, now=dt.datetime.now(dt.UTC).isoformat()
                )
            finally:
                conn.close()
            release.set()
            assert harness.server.wait_for_account_mail_idle(2)
            assert len(delivered) == 1
        finally:
            release.set()
            harness.stop()

    def test_existing_registration_and_unknown_reset_do_not_enqueue_mail(self, tmp_path):
        delivered = []
        harness = SiteHarness(tmp_path, code_sender=lambda *args: delivered.append(args))
        try:
            conn = db.connect(harness.db_path)
            user = db.upsert_pending_user(
                conn,
                email="existing-no-mail@example.com",
                email_key=db.delivery_recipient_key("existing-no-mail@example.com"),
                password_hash="pbkdf2_sha256$1$00$00",
                now=NOW.isoformat(),
            )
            db.activate_user(conn, email_key=user.email_key, now=NOW.isoformat())
            conn.close()

            status, headers, page = harness.get("/register")
            _token, cookies = harness.csrf_pair(page, headers)
            status, _headers, _page = harness.post(
                "/register",
                harness.registration_fields(page, "existing-no-mail@example.com"),
                cookies=cookies,
            )
            assert status == 200
            status, headers, page = harness.get("/forgot")
            token, cookies = harness.csrf_pair(page, headers)
            status, _headers, _page = harness.post(
                "/forgot",
                {"csrf": token, "email": "unknown-no-mail@example.com"},
                cookies=cookies,
            )
            assert status == 200 and delivered == []
            conn = db.connect(harness.db_path)
            try:
                assert conn.execute(
                    "SELECT COUNT(*) FROM account_mail_outbox"
                ).fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            harness.stop()

    def test_server_start_consumes_mail_persisted_before_restart(self, tmp_path):
        db_path = tmp_path / "news.db"
        secret = load_site_secret(tmp_path / "site-secret")
        assert len(secret) == 32
        conn = db.connect(db_path)
        email = "restart-outbox@example.com"
        user = db.upsert_pending_user(
            conn,
            email=email,
            email_key=db.delivery_recipient_key(email),
            password_hash="password-hash",
            now=NOW.isoformat(),
        )
        db.issue_email_code_with_outbox(
            conn,
            email_key=user.email_key,
            purpose="register",
            code_digest="b" * 64,
            delivery_token="persisted-delivery-token-with-entropy",
            ttl_seconds=600,
            now=dt.datetime.now(dt.UTC).isoformat(),
        )
        conn.close()
        delivered = []
        harness = SiteHarness(tmp_path, code_sender=lambda *args: delivered.append(args))
        try:
            harness.server.wake_account_mail()
            assert harness.server.wait_for_account_mail_idle(2)
            assert len(delivered) == 1
            assert delivered[0][0] == email and delivered[0][2] == "register"
        finally:
            harness.stop()

    def test_register_sends_code_and_activates(self, site):
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        status, _headers, page = site.post(
            "/register",
            site.registration_fields(page, "new@example.com"),
            cookies=cookies,
        )
        assert status == 200
        assert "如果该邮箱可以注册" in page
        assert 'action="/verify"' in page
        assert site.sent_codes and site.sent_codes[-1][0] == "new@example.com"
        assert site.sent_codes[-1][2] == "register"
        code = site.sent_codes[-1][1]
        status, headers, page = site.post(
            "/verify",
            {"csrf": token, "email": "new@example.com", "code": code},
            cookies=cookies,
        )
        assert status == 200
        assert "邮箱验证成功，请使用密码登录" in page
        assert not any(
            value.startswith("nd_user_session=")
            for value in headers.get_all("Set-Cookie") or []
        )
        conn = db.connect(site.db_path)
        user = db.user_by_email_key(
            conn, db.delivery_recipient_key("new@example.com")
        )
        conn.close()
        assert user is not None and user.status == "active"

    def test_registered_account_can_be_promoted_by_cli_and_enter_admin(
        self, site, monkeypatch
    ):
        email = "cli-admin@example.com"
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        status, _headers, page = site.post(
            "/register", site.registration_fields(page, email), cookies=cookies
        )
        assert status == 200 and "如果该邮箱可以注册" in page
        code = site.sent_codes[-1][1]
        status, _headers, page = site.post(
            "/verify",
            {"csrf": token, "email": email, "code": code},
            cookies=cookies,
        )
        assert status == 200 and "邮箱验证成功" in page

        monkeypatch.setenv("NEWS_DATABASE_PATH", str(site.db_path))
        assert _run_site_admin(email, revoke=False, yes=True) == 0

        status, headers, page = site.get("/login")
        token, csrf_cookie = site.csrf_pair(page, headers)
        status, headers, _page = site.post(
            "/login",
            {"csrf": token, "email": email, "password": "password123"},
            cookies=csrf_cookie,
        )
        assert status == 302
        set_cookies = headers.get_all("Set-Cookie") or []
        assert any(value.startswith("nd_admin_session=") for value in set_cookies)
        user_token = next(
            value.split(";", 1)[0].split("=", 1)[1]
            for value in set_cookies
            if value.startswith("nd_user_session=")
        )
        status, _headers, page = site.get(
            "/", cookies={"nd_user_session": user_token}
        )
        assert status == 200 and 'class="admin-entry" href="/admin/"' in page

    def test_wrong_code_rejected(self, site):
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        site.post(
            "/register",
            site.registration_fields(page, "bad@example.com"),
            cookies=cookies,
        )
        status, _headers, page = site.post(
            "/verify",
            {"csrf": token, "email": "bad@example.com", "code": "000000"},
            cookies=cookies,
        )
        assert "验证码错误" in page

    def test_login_with_password(self, site):
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        site.post(
            "/register",
            site.registration_fields(page, "login@example.com"),
            cookies=cookies,
        )
        conn = db.connect(site.db_path)
        conn.close()
        # 通过摘要反推不了明文码,直接从 fake sender 取。
        code = site.sent_codes[-1][1]
        assert code
        site.post(
            "/verify",
            {"csrf": token, "email": "login@example.com", "code": code},
            cookies=cookies,
        )
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        status, headers, _page = site.post(
            "/login",
            {"csrf": token, "email": "login@example.com", "password": "password123"},
            cookies=cookies,
        )
        assert status == 302
        session_cookies = [
            chunk.split(";", 1)[0]
            for chunk in headers.get_all("Set-Cookie") or []
            if chunk.startswith("nd_user_session=")
        ]
        assert session_cookies
        wrong_status, _h, _c = site.post(
            "/login",
            {"csrf": token, "email": "login@example.com", "password": "wrong-password"},
            cookies=cookies,
        )
        assert wrong_status == 200

    def test_unknown_login_uses_precomputed_dummy_hash(self, site, monkeypatch):
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)

        def unexpected_hash(_password: str) -> str:
            pytest.fail("unknown login must not generate a fresh password hash")

        monkeypatch.setattr(accounts, "hash_password", unexpected_hash)
        status, _headers, page = site.post(
            "/login",
            {
                "csrf": token,
                "email": "missing@example.com",
                "password": "password123",
            },
            cookies=cookies,
        )
        assert status == 200 and "邮箱或密码不正确" in page

    def test_disabled_user_cannot_login(self, site):
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        site.post(
            "/register",
            site.registration_fields(page, "disabled@example.com"),
            cookies=cookies,
        )
        code = site.sent_codes[-1][1]
        site.post(
            "/verify",
            {"csrf": token, "email": "disabled@example.com", "code": code},
            cookies=cookies,
        )
        conn = db.connect(site.db_path)
        user = db.user_by_email_key(
            conn, db.delivery_recipient_key("disabled@example.com")
        )
        db.set_user_status(conn, user.id, status="disabled", now=NOW.isoformat())
        conn.close()
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        status, _headers, page = site.post(
            "/login",
            {"csrf": token, "email": "disabled@example.com", "password": "password123"},
            cookies=cookies,
        )
        assert status == 200 and "邮箱或密码不正确" in page

    def test_password_reset_uses_email_code(self, site):
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        site.post(
            "/register",
            site.registration_fields(page, "reset@example.com"),
            cookies=cookies,
        )
        code = site.sent_codes[-1][1]
        site.post(
            "/verify",
            {"csrf": token, "email": "reset@example.com", "code": code},
            cookies=cookies,
        )
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        _status, login_headers, _page = site.post(
            "/login",
            {"csrf": token, "email": "reset@example.com", "password": "password123"},
            cookies=cookies,
        )
        session_cookie = next(
            chunk.split(";", 1)[0].split("=", 1)[1]
            for chunk in login_headers.get_all("Set-Cookie") or []
            if chunk.startswith("nd_user_session=")
        )
        status, headers, page = site.get("/forgot")
        token, cookies = site.csrf_pair(page, headers)
        status, _headers, page = site.post(
            "/forgot", {"csrf": token, "email": "reset@example.com"}, cookies=cookies
        )
        assert status == 200 and "如果账号存在" in page
        reset_code = site.sent_codes[-1][1]
        assert site.sent_codes[-1][2] == "reset"
        status, _headers, page = site.post(
            "/reset",
            {
                "csrf": token,
                "email": "reset@example.com",
                "code": reset_code,
                "password": "new-password-123",
            },
            cookies=cookies,
        )
        assert status == 200 and "密码已更新" in page
        status, headers, _page = site.get(
            "/account", cookies={"nd_user_session": session_cookie}
        )
        assert status == 302 and headers["Location"] == "/login"

    def test_logout_requires_post_csrf_and_revokes_session(self, site):
        status, headers, page = site.get("/register")
        token, cookies = site.csrf_pair(page, headers)
        site.post(
            "/register",
            site.registration_fields(page, "logout@example.com"),
            cookies=cookies,
        )
        code = site.sent_codes[-1][1]
        site.post(
            "/verify",
            {"csrf": token, "email": "logout@example.com", "code": code},
            cookies=cookies,
        )
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        _status, login_headers, _page = site.post(
            "/login",
            {"csrf": token, "email": "logout@example.com", "password": "password123"},
            cookies=cookies,
        )
        session_cookie = next(
            chunk.split(";", 1)[0].split("=", 1)[1]
            for chunk in login_headers.get_all("Set-Cookie") or []
            if chunk.startswith("nd_user_session=")
        )
        session_cookies = {**cookies, "nd_user_session": session_cookie}
        status, _headers, _page = site.get("/logout", cookies=session_cookies)
        assert status == 404
        status, headers, _page = site.post("/logout", {}, cookies=session_cookies)
        assert status == 302 and headers["Location"] == "/account"
        status, _headers, _page = site.get("/account", cookies=session_cookies)
        assert status == 200
        status, headers, _page = site.post(
            "/logout", {"csrf": token}, cookies=session_cookies
        )
        assert status == 302 and headers["Location"] == "/"
        status, headers, _page = site.get("/account", cookies=session_cookies)
        assert status == 302 and headers["Location"] == "/login"

    def test_password_reset_delivery_failure_stays_durable_for_retry(self, tmp_path):
        def failing_sender(_email: str, _code: str, _purpose: str) -> None:
            raise RuntimeError("smtp unavailable")

        harness = SiteHarness(tmp_path, code_sender=failing_sender)
        try:
            conn = db.connect(harness.db_path)
            user = db.upsert_pending_user(
                conn,
                email="reset-fail@example.com",
                email_key=db.delivery_recipient_key("reset-fail@example.com"),
                password_hash="pbkdf2_sha256$1$00$00",
                now=NOW.isoformat(),
            )
            db.activate_user(conn, email_key=user.email_key, now=NOW.isoformat())
            conn.close()
            status, headers, page = harness.get("/forgot")
            token, cookies = harness.csrf_pair(page, headers)
            status, _headers, _page = harness.post(
                "/forgot",
                {"csrf": token, "email": "reset-fail@example.com"},
                cookies=cookies,
            )
            assert status == 200
            conn = db.connect(harness.db_path)
            try:
                assert conn.execute(
                    "SELECT COUNT(*) FROM email_codes WHERE purpose = 'reset'"
                ).fetchone()[0] == 1
                assert conn.execute(
                    "SELECT status FROM account_mail_outbox WHERE purpose = 'reset'"
                ).fetchone()[0] == "pending"
            finally:
                conn.close()
        finally:
            harness.stop()


class TestOrders:
    def _notification(self, site, order: db.Order, **overrides: str) -> dict[str, str]:
        fields = {
            "pid": site.payment_config.merchant_id,
            "trade_no": "gateway-10001",
            "out_trade_no": order.merchant_order_no,
            "type": site.payment_config.payment_type,
            "name": "Cheapcoding News monthly plan",
            "money": f"{order.amount_cents / 100:.2f}",
            "trade_status": "TRADE_SUCCESS",
            "sign_type": "MD5",
        }
        fields.update(overrides)
        fields["sign"] = sign_fields(fields, site.payment_config.merchant_key)
        return fields

    def test_order_redirects_to_epay_and_callback_opens_membership_once(self, site):
        conn = db.connect(site.db_path)
        user = db.upsert_pending_user(
            conn,
            email="buyer@example.com",
            email_key=db.delivery_recipient_key("buyer@example.com"),
            password_hash="pbkdf2_sha256$1$00$00",
            now=NOW.isoformat(),
        )
        db.activate_user(conn, email_key=user.email_key, now=NOW.isoformat())
        db.set_settings(
            conn,
            {
                "monthly_list_price_cents": "3600",
                "monthly_price_cents": "990",
                "monthly_discount_percent": "0",
            },
            now=NOW.isoformat(),
        )
        session_token = "buyer-session-token-with-enough-entropy"
        db.create_user_session(
            conn,
            token_digest=hashlib.sha256(session_token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=(NOW + dt.timedelta(days=3650)).isoformat(),
            now=NOW.isoformat(),
        )
        conn.close()
        status, headers, page = site.get("/login")
        csrf, cookies = site.csrf_pair(page, headers)
        cookies["nd_user_session"] = session_token
        status, redirect_headers, _page = site.post(
            "/order",
            {"csrf": csrf, "plan": "monthly"},
            cookies=cookies,
        )
        assert status == 302
        payment_url = urllib.parse.urlsplit(redirect_headers["Location"])
        assert payment_url.netloc == "pay.example.test"
        assert "merchant-secret" not in redirect_headers["Location"]
        assert len(site.payment_create_calls) == 1
        assert site.payment_create_calls[0][1]["merchant_order_no"].startswith("news_")
        conn = db.connect(site.db_path)
        orders = db.list_orders(conn, limit=10)
        conn.close()
        assert len(orders) == 1
        order = orders[0]
        assert order.base_amount_cents == order.amount_cents == 990
        status, account_headers, account_page = site.get("/account", cookies=cookies)
        assert status == 200
        assert order.merchant_order_no in account_page
        assert "月刊会员" in account_page and "继续支付" in account_page
        assert "基准 ¥" not in account_page and "实付 ¥" not in account_page
        assert "form-action 'self' https://pay.example.test" in account_headers[
            "Content-Security-Policy"
        ]
        fields = self._notification(site, order)
        status, _headers, body = site.post(
            "/subscribe/api/payment/easypay", fields
        )
        assert status == 200 and body == "success"
        status, _headers, return_page = site.get(
            "/payment/return?" + urllib.parse.urlencode(fields)
        )
        assert status == 200 and "会员已自动开通" in return_page
        conn = db.connect(site.db_path)
        first_until = db.user_by_id(conn, user.id).paid_until
        assert db.order_by_id(conn, order.id).status == "paid"
        assert db.order_by_id(conn, order.id).provider_trade_no == "gateway-10001"
        conn.close()
        status, _headers, body = site.post("/payment/notify", fields)
        assert status == 200 and body == "success"
        conn = db.connect(site.db_path)
        assert db.user_by_id(conn, user.id).paid_until == first_until
        conn.close()
        status, _headers, account_page = site.get("/account", cookies=cookies)
        assert status == 200 and "已支付" in account_page
        assert order.merchant_order_no in account_page
        assert "月刊会员" in account_page
        assert "基准 ¥" not in account_page and "实付 ¥" not in account_page
        assert "计划:monthly" not in account_page
        assert "payment_ref" not in account_page and "收款二维码" not in account_page
        assert 'href="/forgot"' in account_page
        assert "使用邮箱验证码修改密码" in account_page

    def test_callback_rejects_bad_signature_and_amount(self, site):
        user, cookies = site.member_session("callback@example.com")
        status, headers, page = site.get("/account", cookies=cookies)
        csrf, csrf_cookies = site.csrf_pair(page, headers)
        cookies.update(csrf_cookies)
        status, redirect_headers, _page = site.post(
            "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
        )
        assert status == 302 and redirect_headers["Location"]
        conn = db.connect(site.db_path)
        order = db.list_user_orders(conn, user_id=user.id)[0]
        conn.close()
        wrong_signature = self._notification(site, order)
        wrong_signature["sign"] = "0" * 32
        status, _headers, body = site.post("/payment/notify", wrong_signature)
        assert status == 400 and body == "fail"
        wrong_amount = self._notification(site, order, money="99.99")
        status, _headers, body = site.post("/payment/notify", wrong_amount)
        assert status == 400 and body == "fail"
        conn = db.connect(site.db_path)
        assert db.order_by_id(conn, order.id).status == "pending"
        assert db.user_by_id(conn, user.id).paid_until is None
        conn.close()

    def test_callback_rejects_wrong_content_type(self, site):
        status, _headers, body = site.request(
            "POST",
            "/subscribe/api/payment/easypay",
            body=json.dumps({"sign": "0" * 32}),
            headers={"Content-Type": "application/json"},
        )
        assert status == 400 and body == "fail"

    def test_gateway_creation_failure_is_visible_and_keeps_order_reconcilable(
        self, tmp_path
    ):
        def fail_create(_config, **_kwargs):
            raise PaymentError("gateway unavailable")

        harness = SiteHarness(tmp_path, payment_create_callback=fail_create)
        try:
            user, cookies = harness.member_session("gateway-fail@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            status, _headers, page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 502 and "订单创建失败" in page
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            conn.close()
            assert order.status == "pending"
            assert order.last_error_code == "GATEWAY_CREATE_FAILED"
        finally:
            harness.stop()

    def test_paid_webhook_wins_when_gateway_create_response_fails(self, tmp_path):
        harness = None

        def settle_then_fail(config, **kwargs):
            conn = db.connect(harness.db_path)
            db.confirm_payment_order(
                conn,
                merchant_order_no=kwargs["merchant_order_no"],
                provider_trade_no="gateway-paid-before-error",
                amount_cents=kwargs["amount_cents"],
                now=dt.datetime.now(dt.UTC).isoformat(),
                amount_hold_seconds=config.amount_hold_seconds,
                plan_days=accounts.PLAN_DAYS,
            )
            conn.close()
            raise PaymentError("create response lost")

        harness = SiteHarness(tmp_path, payment_create_callback=settle_then_fail)
        try:
            user, cookies = harness.member_session("paid-before-error@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            status, response_headers, _page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 303 and response_headers["Location"] == "/account"
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            updated_user = db.user_by_id(conn, user.id)
            conn.close()
            assert order.status == "paid"
            assert updated_user is not None and updated_user.paid_until is not None
        finally:
            harness.stop()

    def test_gateway_creation_failure_retries_same_local_order_once_claimed(
        self, tmp_path
    ):
        calls = []

        def fail_then_succeed(_config, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise PaymentError("gateway unavailable")
            return PaymentCreation(
                provider_trade_no="gateway-recovered",
                payment_url="https://pay.example.test/pay/recovered",
            )

        harness = SiteHarness(tmp_path, payment_create_callback=fail_then_succeed)
        try:
            user, cookies = harness.member_session("gateway-retry@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            first = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert first[0] == 502
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            with conn:
                conn.execute(
                    "UPDATE orders SET status = 'failed' WHERE id = ?",
                    (order.id,),
                )
            conn.close()

            status, _headers, account_page = harness.get("/account", cookies=cookies)
            assert status == 200
            assert "继续支付" in account_page
            second = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert second[0] == 302 and second[1]["Location"].endswith("/recovered")
            assert len(calls) == 2
            assert calls[0]["merchant_order_no"] == calls[1]["merchant_order_no"]
            conn = db.connect(harness.db_path)
            orders = db.list_user_orders(conn, user_id=user.id)
            conn.close()
            assert len(orders) == 1 and orders[0].payment_url == second[1]["Location"]
        finally:
            harness.stop()

    def test_ambiguous_create_retry_does_not_reallocate_after_amount_occupied(
        self, tmp_path
    ):
        calls = []

        def lose_then_report_occupied(_config, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise PaymentError("create response lost")
            raise PaymentError("amount occupied", code="AMOUNT_OCCUPIED")

        harness = SiteHarness(
            tmp_path, payment_create_callback=lose_then_report_occupied
        )
        try:
            user, cookies = harness.member_session("ambiguous-retry@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)

            first = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            second = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )

            assert first[0] == second[0] == 502
            assert len(calls) == 2
            assert calls[0]["merchant_order_no"] == calls[1]["merchant_order_no"]
            assert [call["amount_cents"] for call in calls] == [990, 990]
            conn = db.connect(harness.db_path)
            orders = db.list_user_orders(conn, user_id=user.id)
            conn.close()
            assert len(orders) == 1
            assert orders[0].merchant_order_no == calls[0]["merchant_order_no"]
            assert orders[0].amount_cents == orders[0].base_amount_cents == 990
            assert orders[0].amount_offset_cents == 0

            fields = self._notification(harness, orders[0])
            status, _headers, body = harness.post("/payment/notify", fields)
            assert status == 200 and body == "success"
        finally:
            harness.stop()

    def test_failed_uncertain_order_accepts_late_success_callback_once(self, tmp_path):
        def fail_create(_config, **_kwargs):
            raise PaymentError("create response lost")

        harness = SiteHarness(tmp_path, payment_create_callback=fail_create)
        try:
            user, cookies = harness.member_session("failed-callback@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            status, _headers, _page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 502
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            with conn:
                conn.execute(
                    "UPDATE orders SET status = 'failed', "
                    "last_error_code = 'PAYMENT_WAITING_NO_URL' WHERE id = ?",
                    (order.id,),
                )
            conn.close()

            fields = self._notification(harness, order)
            status, _headers, body = harness.post("/payment/notify", fields)
            assert status == 200 and body == "success"
            conn = db.connect(harness.db_path)
            paid = db.order_by_id(conn, order.id)
            first_until = db.user_by_id(conn, user.id).paid_until
            conn.close()
            assert paid is not None and paid.status == "paid"
            assert first_until is not None

            status, _headers, body = harness.post("/payment/notify", fields)
            assert status == 200 and body == "success"
            conn = db.connect(harness.db_path)
            assert db.user_by_id(conn, user.id).paid_until == first_until
            conn.close()
        finally:
            harness.stop()

    def test_waiting_order_without_url_recreates_same_gateway_order(self, tmp_path):
        calls = []

        def lose_then_recover(_config, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise PaymentError("create response lost")
            return PaymentCreation(
                provider_trade_no="gateway-idempotent",
                payment_url="https://pay.example.test/pay/idempotent",
            )

        harness = SiteHarness(tmp_path, payment_create_callback=lose_then_recover)
        try:
            user, cookies = harness.member_session("waiting-no-url@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            first = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert first[0] == 502
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            conn.execute(
                "UPDATE orders SET updated_at = '2020-01-01T00:00:00+00:00',"
                " expires_at = '2020-01-01T00:00:00+00:00'"
                " WHERE id = ?",
                (order.id,),
            )
            conn.commit()
            conn.close()

            status, _headers, _page = harness.get("/account", cookies=cookies)
            assert status == 200
            conn = db.connect(harness.db_path)
            conn.execute(
                "UPDATE orders SET updated_at = '2020-01-01T00:00:00+00:00'"
                " WHERE id = ?",
                (order.id,),
            )
            conn.commit()
            conn.close()
            status, _headers, _page = harness.get("/account", cookies=cookies)
            assert status == 200
            conn = db.connect(harness.db_path)
            waiting = db.order_by_id(conn, order.id)
            conn.close()
            assert waiting is not None
            assert waiting.payment_url is None
            assert waiting.last_error_code == "PAYMENT_WAITING_NO_URL"

            second = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert second[0] == 302
            assert second[1]["Location"].endswith("/pay/idempotent")
            assert len(calls) == 2
            assert calls[0]["merchant_order_no"] == calls[1]["merchant_order_no"]
            assert calls[0]["amount_cents"] == calls[1]["amount_cents"]
        finally:
            harness.stop()

    def test_payment_unavailable_does_not_fall_back_to_manual_order(self, tmp_path):
        harness = SiteHarness(tmp_path, payment_config=None)
        try:
            _user, cookies = harness.member_session("disabled-pay@example.com")
            status, _headers, subscribe_page = harness.get(
                "/subscribe", cookies=cookies
            )
            assert status == 200
            assert "在线支付暂不可用" in subscribe_page
            assert 'action="/order"' not in subscribe_page
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            assert status == 200 and "在线支付暂不可用" in page
            assert "付款凭证" not in page
            status, _headers, page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 503 and "在线支付暂不可用" in page
        finally:
            harness.stop()

    def test_payment_configuration_loader_is_used_without_site_restart(self, tmp_path):
        first = EpayConfig(
            base_url="https://first.example.test",
            merchant_id="1001",
            merchant_key="first-secret",
            payment_type="alipay",
            site_url="https://news.example.test",
        )
        second = EpayConfig(
            base_url="https://second.example.test",
            merchant_id="2002",
            merchant_key="second-secret",
            payment_type="wxpay",
            site_url="https://news.example.test",
        )
        current = {"config": first}
        harness = SiteHarness(
            tmp_path,
            payment_config=first,
            payment_config_loader=lambda: current["config"],
        )
        try:
            _user, cookies = harness.member_session("dynamic-pay@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            current["config"] = second
            status, _headers, _page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 302
            assert harness.payment_create_calls[0][0] is second
        finally:
            harness.stop()

    def test_repeated_order_submission_reuses_verified_payment_url(self, site):
        user, cookies = site.member_session("reuse-order@example.com")
        status, headers, page = site.get("/account", cookies=cookies)
        csrf, csrf_cookies = site.csrf_pair(page, headers)
        cookies.update(csrf_cookies)
        first = site.post(
            "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
        )
        second = site.post(
            "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
        )
        assert first[0] == second[0] == 302
        assert first[1]["Location"] == second[1]["Location"]
        assert len(site.payment_create_calls) == 1
        conn = db.connect(site.db_path)
        orders = db.list_user_orders(conn, user_id=user.id)
        conn.close()
        assert len(orders) == 1
        assert orders[0].payment_url == first[1]["Location"]
        assert orders[0].settlement_expires_at is not None

    def test_different_plan_does_not_silently_reuse_existing_order(self, site):
        user, cookies = site.member_session("different-plan@example.com")
        status, headers, page = site.get("/account", cookies=cookies)
        csrf, csrf_cookies = site.csrf_pair(page, headers)
        cookies.update(csrf_cookies)
        first = site.post(
            "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
        )
        second = site.post(
            "/order", {"csrf": csrf, "plan": "yearly"}, cookies=cookies
        )
        assert first[0] == 302
        assert second[0] == 409 and "已有另一方案" in second[2]
        assert len(site.payment_create_calls) == 1
        conn = db.connect(site.db_path)
        orders = db.list_user_orders(conn, user_id=user.id)
        conn.close()
        assert len(orders) == 1 and orders[0].plan == "monthly"

    def test_gateway_amount_collision_moves_to_next_slot(self, tmp_path):
        calls = []

        def create_with_collision(_config, **kwargs):
            calls.append(kwargs["amount_cents"])
            if len(calls) == 1:
                raise PaymentError("occupied", code="AMOUNT_OCCUPIED")
            return PaymentCreation("gateway-collision", "https://pay.example.test/pay/ok")

        harness = SiteHarness(tmp_path, payment_create_callback=create_with_collision)
        try:
            user, cookies = harness.member_session("collision@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            status, redirect_headers, _page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 302 and redirect_headers["Location"].endswith("/pay/ok")
            assert calls == [990, 989]
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            conn.close()
            assert order.amount_offset_cents == -1
        finally:
            harness.stop()

    def test_disabling_new_orders_does_not_break_existing_callback(self, tmp_path):
        config = EpayConfig(
            base_url="https://pay.example.test",
            merchant_id="1001",
            merchant_key="merchant-secret",
            payment_type="alipay",
            site_url="http://127.0.0.1",
        )
        state = {"new_orders": config}
        harness = SiteHarness(
            tmp_path,
            payment_config=config,
            payment_config_loader=lambda: state["new_orders"],
            payment_settlement_config_loader=lambda: config,
        )
        try:
            user, cookies = harness.member_session("disabled-callback@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            status, _headers, _page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 302
            conn = db.connect(harness.db_path)
            order = db.list_user_orders(conn, user_id=user.id)[0]
            conn.close()
            state["new_orders"] = None
            fields = self._notification(harness, order)
            status, _headers, body = harness.post("/payment/notify", fields)
            assert status == 200 and body == "success"
        finally:
            harness.stop()

    def test_account_reconciles_lost_callback_with_signed_query_result(self, tmp_path):
        queries = []

        def paid_query(_config, **kwargs):
            queries.append(kwargs)
            return PaymentQuery(
                merchant_order_no=kwargs["merchant_order_no"],
                provider_trade_no="gateway-10001",
                amount_cents=kwargs["expected_amount_cents"],
                trade_status="TRADE_SUCCESS",
            )

        harness = SiteHarness(tmp_path, payment_query_callback=paid_query)
        try:
            user, cookies = harness.member_session("reconcile@example.com")
            status, headers, page = harness.get("/account", cookies=cookies)
            csrf, csrf_cookies = harness.csrf_pair(page, headers)
            cookies.update(csrf_cookies)
            status, _headers, _page = harness.post(
                "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
            )
            assert status == 302
            conn = db.connect(harness.db_path)
            conn.execute(
                "UPDATE orders SET updated_at = '2020-01-01T00:00:00+00:00'"
            )
            conn.commit()
            conn.close()
            status, _headers, page = harness.get("/account", cookies=cookies)
            assert status == 200 and "已支付" in page
            assert len(queries) == 1
            conn = db.connect(harness.db_path)
            assert db.list_user_orders(conn, user_id=user.id)[0].status == "paid"
            assert db.user_by_id(conn, user.id).paid_until is not None
            conn.close()
        finally:
            harness.stop()

    def test_account_does_not_reconcile_an_active_payment_creation_lease(self, tmp_path):
        queries = []

        def unexpected_query(_config, **kwargs):
            queries.append(kwargs)
            raise AssertionError("active payment creation lease must not be queried")

        harness = SiteHarness(tmp_path, payment_query_callback=unexpected_query)
        try:
            user, cookies = harness.member_session("active-create@example.com")
            created_at = (
                dt.datetime.now(dt.UTC) - dt.timedelta(seconds=20)
            ).isoformat()
            conn = db.connect(harness.db_path)
            db.reserve_payment_order(
                conn,
                user_id=user.id,
                plan="monthly",
                base_amount_cents=990,
                merchant_order_no="news_active_create",
                payment_type="alipay",
                payment_config_id=config_identity(harness.payment_config),
                now=created_at,
                ttl_seconds=300,
                amount_hold_seconds=3600,
            )
            conn.close()

            status, _headers, page = harness.get("/account", cookies=cookies)
            assert status == 200 and "等待支付" in page
            assert queries == []
        finally:
            harness.stop()

    def test_logged_in_subscribe_cards_submit_selected_plan_directly(self, site):
        _user, cookies = site.member_session("plans@example.com")
        status, headers, page = site.get("/subscribe", cookies=cookies)
        assert status == 200
        assert page.count('action="/order"') == 2
        assert 'name="plan" value="monthly"' in page
        assert 'name="plan" value="yearly"' in page
        csrf, csrf_cookies = site.csrf_pair(page, headers)
        cookies.update(csrf_cookies)
        status, redirect_headers, _page = site.post(
            "/order", {"csrf": csrf, "plan": "monthly"}, cookies=cookies
        )
        assert status == 302
        assert redirect_headers["Location"].endswith("/pay/gateway-10001")

    def test_account_marks_checkout_deadline_expired_before_render(self, site):
        user, cookies = site.member_session("expired-account@example.com")
        created_at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).isoformat()
        conn = db.connect(site.db_path)
        order = db.create_payment_order(
            conn,
            user_id=user.id,
            plan="monthly",
            base_amount_cents=990,
            merchant_order_no="news_expired_account",
            now=created_at,
            ttl_seconds=300,
            amount_hold_seconds=3600,
        )
        conn.close()
        status, _headers, page = site.get("/account", cookies=cookies)
        assert status == 200 and "已过期 / 已取消" in page
        assert order.merchant_order_no in page
        conn = db.connect(site.db_path)
        assert db.order_by_id(conn, order.id).status == "expired"
        conn.close()

    def test_order_requires_session(self, site):
        status, headers, page = site.get("/login")
        token, cookies = site.csrf_pair(page, headers)
        status, _headers, _page = site.post(
            "/order", {"csrf": token, "plan": "monthly"}, cookies=cookies
        )
        assert status == 302  # 未登录跳转 /login


class TestRedemptionDomain:
    def test_redeem_result_and_account_show_plan_and_expiry(self, site):
        user, cookies = site.member_session("redeem-result@example.com")
        code = generate_redemption_code()
        conn = db.connect(site.db_path)
        db.create_redemption_codes(
            conn,
            entries=[(redemption_digest(code), redemption_prefix(code))],
            plan="yearly",
            note=None,
            created_by="admin",
            now=NOW.isoformat(),
        )
        conn.close()

        status, headers, page = site.get("/account", cookies=cookies)
        assert status == 200
        token, csrf_cookie = site.csrf_pair(page, headers)
        cookies.update(csrf_cookie)
        status, headers, _page = site.post(
            "/redeem", {"csrf": token, "code": code}, cookies=cookies
        )
        assert status == 303
        assert headers["Location"] == "/account?redeemed=1"

        conn = db.connect(site.db_path)
        refreshed = db.user_by_id(conn, user.id)
        conn.close()
        assert refreshed is not None and refreshed.paid_until is not None
        expiry_date = refreshed.paid_until[:10]

        status, _headers, page = site.get(headers["Location"], cookies=cookies)
        assert status == 200
        assert "年刊会员已兑换" in page
        assert f"会员有效期至 {expiry_date}" in page

        status, _headers, account_page = site.get("/account", cookies=cookies)
        assert status == 200
        assert "年刊会员" in account_page
        assert f"会员有效期至 {expiry_date}" in account_page

    def test_redeem_code_extends_and_single_use(self, tmp_path, site):
        conn = db.connect(site.db_path)
        user = db.upsert_pending_user(
            conn,
            email="redeem@example.com",
            email_key="k" * 64,
            password_hash="x",
            now=NOW.isoformat(),
        )
        db.activate_user(conn, email_key="k" * 64, now=NOW.isoformat())
        code = generate_redemption_code()
        db.create_redemption_codes(
            conn,
            entries=[(redemption_digest(code), redemption_prefix(code))],
            plan="yearly",
            note=None,
            created_by="admin",
            now=NOW.isoformat(),
        )
        plan = db.redeem_code(
            conn,
            code_digest=redemption_digest(code),
            user_id=user.id,
            now=NOW.isoformat(),
            plan_days={"monthly": 31, "yearly": 366},
        )
        assert plan == "yearly"
        refreshed = db.user_by_id(conn, user.id)
        assert refreshed.paid_until is not None
        with pytest.raises(RuntimeError):
            db.redeem_code(
                conn,
                code_digest=redemption_digest(code),
                user_id=user.id,
                now=NOW.isoformat(),
            )

        orphan_code = generate_redemption_code()
        db.create_redemption_codes(
            conn,
            entries=[(redemption_digest(orphan_code), redemption_prefix(orphan_code))],
            plan="monthly",
            note=None,
            created_by="admin",
            now=NOW.isoformat(),
        )
        with pytest.raises(RuntimeError, match="user does not exist"):
            db.redeem_code(
                conn,
                code_digest=redemption_digest(orphan_code),
                user_id=user.id + 10_000,
                now=NOW.isoformat(),
            )
        orphan = conn.execute(
            "SELECT status FROM redemption_codes WHERE code_digest = ?",
            (redemption_digest(orphan_code),),
        ).fetchone()
        assert orphan["status"] == "unused"
        conn.close()
