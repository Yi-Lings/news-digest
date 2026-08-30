"""Offline Admin HTTP security, provider lifecycle, and probe tests."""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from news_digest import accounts, payments
from news_digest.admin_email import read_env
from news_digest.admin_providers import (
    AdminConfigError,
    current_test_state,
    default_provider,
    load_profiles,
    migrate_profiles,
    provider_fingerprint,
    runtime_translation_config,
    save_profiles,
    update_profiles,
    validate_public_https_target,
    write_env_local,
)
from news_digest.cli import _run_preview
from news_digest.config import BuildConfig
from news_digest.config_io import atomic_write_text
from news_digest.delivery import subscriptions
from news_digest.delivery.delivery_service import DeliveryServiceReport
from news_digest.delivery.mailer import DeliveryReport, RecipientDeliveryResult
from news_digest.models import Article, BriefItem, DailyEdition, Paragraph
from news_digest.pipeline import build_editions
from news_digest.preview_server import (
    ADMIN_HTML,
    PreviewHandler,
    apr1_hash,
    create_server,
    mask_key,
)
from news_digest.storage import db
from news_digest.translation.client import ApiTranslator, TranslationError

PANEL_PASSWORD = "test-password-1"
PUBLIC_IPS = lambda host, port: ["93.184.216.34"]  # noqa: E731
PROVIDER = {
    "name": "claude",
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-test-abcdefghijklmnop",
    "model": "demo-model",
    "api_type": "openai_chat",
    "stream": True,
    "enabled": True,
    "is_default": False,
}
FORM = {key: value for key, value in PROVIDER.items() if key != "is_default"}
_SERVER_ORIGINS: dict[int, tuple[tuple[str, str, int], tuple[str, str, int]]] = {}


def _default_body(root: Path, *, confirm: bool = False) -> dict[str, Any]:
    provider = load_profiles(root, "providers.json")["providers"]["claude"]
    body: dict[str, Any] = {
        "name": "claude",
        "expected_fingerprint": provider_fingerprint(root, provider),
    }
    if confirm:
        body["confirm_untested"] = True
    return body


def _start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    _SERVER_ORIGINS[port] = (server.admin_origin, server.public_origin)
    return port


def _origin(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _origin_url(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    default_port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port != default_port else "")


def _request(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
    *,
    cookie: str = "",
    csrf: str = "",
    origin: str | None = None,
    content_type: str | None = "application/json",
    headers: dict[str, str] | None = None,
    decode_json: bool = True,
) -> tuple[int, Any, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    if isinstance(body, dict):
        payload = json.dumps(body).encode()
    else:
        payload = body
    request_headers = dict(headers or {})
    configured = _SERVER_ORIGINS.get(port)
    is_public = path.startswith(("/subscribe/", "/unsubscribe/"))
    expected_origin = (
        _origin_url(configured[1 if is_public else 0])
        if configured is not None
        else _origin(port)
    )
    request_headers.setdefault("Host", urlsplit(expected_origin).netloc)
    request_headers["Origin"] = origin if origin is not None else expected_origin
    if content_type is not None:
        request_headers["Content-Type"] = content_type
    if cookie:
        request_headers["Cookie"] = cookie
    if csrf:
        request_headers["X-CSRF-Token"] = csrf
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    data = json.loads(raw.decode("utf-8")) if raw and decode_json else raw.decode("utf-8")
    return response.status, data, response_headers


def _login(port: int, password: str = PANEL_PASSWORD) -> tuple[str, str]:
    status, _, headers = _request(
        port,
        "POST",
        "/admin/api/login",
        {"username": "admin", "password": password},
    )
    assert status == 200
    cookie = headers["set-cookie"].split(";", 1)[0]
    status, data, _ = _request(
        port,
        "GET",
        "/admin/api/providers",
        cookie=cookie,
        content_type=None,
    )
    assert status == 200
    return cookie, data["csrf_token"]


def _post(
    port: int,
    path: str,
    body: dict[str, Any],
    auth: tuple[str, str],
) -> tuple[int, dict[str, Any]]:
    status, data, _ = _request(port, "POST", path, body, cookie=auth[0], csrf=auth[1])
    return status, data


def _admin_edition():
    return DailyEdition(
        date="2026-07-27",
        articles=[
            Article(
                slug="admin-story",
                source="BBC News",
                title_en="Admin published story",
                title_zh="管理端已发布文章",
                summary_en="Published summary.",
                summary_zh="已发布摘要。",
                author="Reporter",
                published_at="2026-07-27T00:00:00+00:00",
                url="https://source.example.com/story",
                reading_minutes=3,
                paragraphs=[Paragraph(en="Body.", zh="正文。")],
                translated_by="model@p2",
            )
        ],
        briefs=[
            BriefItem(
                title_en="Admin brief",
                title_zh="管理端简讯",
                source="NPR",
                url="https://source.example.com/brief",
            )
        ],
    )


@pytest.fixture
def mail_admin_server(tmp_path):
    site_root = tmp_path / "site-root"
    build_editions([_admin_edition()], BuildConfig(site_root, "http://unused"))
    (tmp_path / ".env").write_text(
        "NEWS_SITE_URL=https://news.example.com\nNEWS_TIMEZONE=Asia/Shanghai\n"
        "EMAIL_DELIVERY_ENABLED=false\nSMTP_HOST=smtp.example.com\nSMTP_PORT=587\n"
        "SMTP_USERNAME=operator\nSMTP_PASSWORD=saved-secret\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\nSMTP_RECIPIENTS=saved@example.com\n"
        "EMAIL_MAINS_ENABLED=true\nEMAIL_BRIEFS_ENABLED=true\n"
        "EMAIL_MAIN_LIMIT=1\nEMAIL_BRIEF_LIMIT=1\nEMAIL_LANGUAGE=bi\n"
        "EMAIL_SOURCE_FILTERS=\nEMAIL_LAYOUT=digest\nEMAIL_SUMMARY_LENGTH=standard\n"
        "EMAIL_CATCHUP_WINDOW_HOURS=6\n",
        encoding="utf-8",
    )
    (tmp_path / "htpasswd-admin").write_text(
        f"admin:{apr1_hash(PANEL_PASSWORD)}\n", encoding="utf-8"
    )
    smtp_calls = []
    smtp_smoke_calls = []
    delivery_calls = []

    def smtp_test(config, resolver):
        smtp_calls.append(config)

    def smtp_smoke(config, resolver):
        smtp_smoke_calls.append(config)
        return DeliveryReport((RecipientDeliveryResult("safe-ref", "sent"),))

    def delivery(mode, **kwargs):
        delivery_calls.append((mode, kwargs))
        return DeliveryServiceReport(
            "run-1" if mode != "test" else None,
            "2026-07-27-01",
            "2026-07-27",
            mode,
            "sent",
            1,
            1,
            0,
            0,
            0,
            False,
            "not_requested",
            message="done",
        )

    server = create_server(
        tmp_path,
        tmp_path,
        0,
        env_file=".env",
        serve_static=False,
        htpasswd_file=tmp_path / "htpasswd-admin",
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
        output_root=site_root,
        timezone="Asia/Shanghai",
        resolver=PUBLIC_IPS,
        smtp_test_callback=smtp_test,
        smtp_smoke_callback=smtp_smoke,
        delivery_callback=delivery,
        sensitive_limit=20,
        site_env_path=tmp_path / "site-config" / ".env",
    )
    conn = db.connect(tmp_path / "news.db")
    user = db.upsert_pending_user(
        conn,
        email="saved@example.com",
        email_key=db.delivery_recipient_key("saved@example.com"),
        password_hash=accounts.hash_password("paid-recipient-password"),
        now="2026-08-30T00:00:00+00:00",
    )
    user = db.activate_user(
        conn, email_key=user.email_key, now="2026-08-30T00:00:01+00:00"
    )
    db.grant_paid_until(
        conn,
        user.id,
        plan="yearly",
        paid_until="2027-08-30T00:00:00+00:00",
        now="2026-08-30T00:00:02+00:00",
    )
    conn.close()
    server.smtp_smoke_calls = smtp_smoke_calls
    port = _start(server)
    yield tmp_path, port, smtp_calls, delivery_calls, server
    server.shutdown()
    server.server_close()


def _mail_form(**overrides):
    body = {
        "delivery_enabled": False,
        "host": "smtp.unsaved.example.com",
        "port": 2525,
        "username": "operator",
        "password": "",
        "security": "starttls",
        "sender": "news@example.com",
        "recipients": ["unsaved@example.com"],
        "mains_enabled": True,
        "briefs_enabled": True,
        "main_limit": 1,
        "brief_limit": 1,
        "language": "bi",
        "source_filters": [],
        "layout": "digest",
        "summary_length": "standard",
        "catchup_window_hours": 6,
    }
    body.update(overrides)
    return body


def _payment_form(**overrides):
    body = {
        "enabled": True,
        "api_base": "https://pay.example.com/submit.php",
        "pid": "10001",
        "pkey": "merchant-secret",
        "payment_type": "alipay",
        "order_ttl_seconds": 300,
        "amount_hold_seconds": 3600,
    }
    body.update(overrides)
    return body


def _add_admin_subscription(root: Path, email: str = "saved@example.com") -> int:
    conn = db.connect(root / "news.db")
    try:
        subscriptions.add_admin_test_recipient(
            conn,
            email,
            dt.datetime(2026, 7, 27, tzinfo=dt.UTC),
        )
        state = db.subscription_by_email(conn, email)
        assert state is not None
        return state.id
    finally:
        conn.close()


def test_local_preview_wires_mail_subscriptions_and_fake_smtp(
    tmp_path, monkeypatch
):
    site_root = tmp_path / "site-root"
    build_editions([_admin_edition()], BuildConfig(site_root, "http://unused"))
    (tmp_path / ".env.local").write_text(
        "NEWS_OUTPUT_PATH=site-root\n"
        "NEWS_DATABASE_PATH=news.db\n"
        "NEWS_SITE_URL=http://127.0.0.1:8618\n"
        "NEWS_TIMEZONE=Asia/Hong_Kong\n"
        "EMAIL_DELIVERY_ENABLED=false\n"
        "SMTP_HOST=smtp.example.com\n"
        "SMTP_PORT=587\n"
        "SMTP_USERNAME=operator\n"
        "SMTP_PASSWORD=saved-secret\n"
        "SMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\n"
        "SMTP_RECIPIENTS=saved@example.com\n"
        "EMAIL_MAINS_ENABLED=true\n"
        "EMAIL_BRIEFS_ENABLED=true\n"
        "EMAIL_MAIN_LIMIT=1\n"
        "EMAIL_BRIEF_LIMIT=1\n",
        encoding="utf-8",
    )
    for name in (
        "NEWS_OUTPUT_PATH",
        "NEWS_DATABASE_PATH",
        "NEWS_SITE_URL",
        "NEWS_TIMEZONE",
        "EMAIL_DELIVERY_ENABLED",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_SECURITY",
        "SMTP_FROM",
        "SMTP_RECIPIENTS",
        "EMAIL_MAINS_ENABLED",
        "EMAIL_BRIEFS_ENABLED",
        "EMAIL_MAIN_LIMIT",
        "EMAIL_BRIEF_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    smtp_calls = []
    started = threading.Event()
    captured = {}
    original_create_server = create_server

    def smtp_test(config, resolver):
        smtp_calls.append(config)

    def wired_create_server(*args, **kwargs):
        server = original_create_server(
            *args,
            **kwargs,
            resolver=PUBLIC_IPS,
            smtp_test_callback=smtp_test,
        )
        captured["server"] = server
        started.set()
        return server

    monkeypatch.setattr("news_digest.preview_server.create_server", wired_create_server)
    result = []
    thread = threading.Thread(target=lambda: result.append(_run_preview(0)), daemon=True)
    thread.start()
    assert started.wait(5)
    server = captured["server"]
    port = server.server_address[1]
    try:
        status, providers, _ = _request(
            port, "GET", "/admin/api/providers", content_type=None
        )
        assert status == 200
        auth = ("", providers["csrf_token"])

        status, settings, _ = _request(
            port, "GET", "/admin/api/mail/settings", content_type=None
        )
        assert status == 200
        assert settings["timezone"] == "Asia/Hong_Kong"
        assert settings["current_release"]["date"] == "2026-07-27"

        status, subscription_data, _ = _request(
            port, "GET", "/admin/api/subscriptions", content_type=None
        )
        assert status == 200
        assert subscription_data["counts"] == {
            "pending": 0,
            "active": 1,
            "unsubscribed": 0,
            "disabled": 0,
        }
        assert subscription_data["public_subscription_enabled"] is False

        status, data = _post(port, "/admin/api/mail/test-connection", _mail_form(), auth)
        assert status == 200 and data["ok"] is True
        assert len(smtp_calls) == 1
    finally:
        server.shutdown()
        thread.join(5)
    assert result == [0]


def test_wired_mail_admin_without_current_release_keeps_settings_available(tmp_path):
    output_root = tmp_path / "site-root"
    output_root.mkdir()
    server = create_server(
        tmp_path,
        output_root,
        0,
        db_path=tmp_path / "news.db",
        site_url="http://127.0.0.1:8618",
        output_root=output_root,
        public_subscription_enabled=False,
    )
    port = _start(server)
    try:
        status, data, _ = _request(
            port, "GET", "/admin/api/mail/settings", content_type=None
        )
        assert status == 200
        assert data["current_release"] is None
        assert data["preview_validation"]["category"] == "release"
        assert "邮件 Admin 尚未完整接线" not in data["preview_validation"]["message"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def local_server(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    calls: list[Any] = []

    def probe(config):
        calls.append(config)
        return " Hello <unsafe> "

    server = create_server(tmp_path, site, 0, probe_callback=probe)
    port = _start(server)
    yield tmp_path, port, calls, server
    server.shutdown()
    server.server_close()


@pytest.fixture
def public_server(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\nSMTP_RECIPIENTS=admin@example.com\n",
        encoding="utf-8",
    )
    sent: list[tuple[Any, str, str]] = []

    def confirmation_sender(config, recipient, url):
        sent.append((config, recipient, url))

    server = create_server(
        tmp_path,
        site,
        0,
        env_file=".env",
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
        confirmation_sender=confirmation_sender,
        sensitive_limit=3,
    )
    port = _start(server)
    yield tmp_path, port, sent, server
    server.shutdown()
    server.server_close()


@pytest.fixture
def prod_server(tmp_path):
    (tmp_path / ".env").write_text(
        "NEWS_SITE_URL=https://news.example.com\nTRANSLATION_MODEL=old-model\n",
        encoding="utf-8",
    )
    save_profiles(tmp_path, {"providers": {"claude": PROVIDER}}, "providers.json")
    (tmp_path / "htpasswd-admin").write_text(
        f"admin:{apr1_hash(PANEL_PASSWORD)}\n",
        encoding="utf-8",
    )
    calls: list[Any] = []

    def probe(config):
        calls.append(config)
        return "Hello"

    server = create_server(
        tmp_path,
        tmp_path,
        0,
        env_file=".env",
        profiles_file="providers.json",
        serve_static=False,
        htpasswd_file=tmp_path / "htpasswd-admin",
        db_path=tmp_path / "news.db",
        resolver=PUBLIC_IPS,
        probe_callback=probe,
        sensitive_limit=20,
    )
    port = _start(server)
    yield tmp_path, port, calls, server
    server.shutdown()
    server.server_close()


def _public_csrf(
    port: int, *, origin: str = "https://news.example.com"
) -> tuple[str, str]:
    status, data, headers = _request(
        port,
        "GET",
        "/subscribe/api/csrf",
        content_type=None,
        origin=origin,
        headers={"Host": urlsplit(origin).netloc},
    )
    assert status == 200
    return headers["set-cookie"].split(";", 1)[0], data["csrf_token"]


def _subscribe(
    port: int,
    email: str,
    *,
    cookie: str | None = None,
    csrf: str | None = None,
    website: str = "",
    origin: str | None = None,
    content_type: str = "application/json",
    headers: dict[str, str] | None = None,
):
    expected_origin = origin or "https://news.example.com"
    request_headers = {"Host": urlsplit(expected_origin).netloc, **(headers or {})}
    if cookie is None or csrf is None:
        cookie, csrf = _public_csrf(port, origin=expected_origin)
    return _request(
        port,
        "POST",
        "/subscribe/api/",
        {"email": email, "website": website, "csrf_token": csrf},
        cookie=cookie,
        origin=expected_origin,
        content_type=content_type,
        headers=request_headers,
    )


def test_apr1_and_mask_do_not_expose_secret_fragments():
    assert apr1_hash("secret123", "abcdefgh") == "$apr1$abcdefgh$aQ26yFH6V5G5PJBY/utXg/"
    assert mask_key("") == ""
    assert mask_key("sk-test-abcdefghijklmnop") == "已设置"


def test_legacy_profiles_migrate_schema_and_active_to_unique_default(tmp_path):
    legacy = {
        "active": "claude",
        "providers": {
            "claude": {
                "base_url": "https://api.example.com/v1",
                "api_key": "key",
                "model": "m",
            },
            "gpt": {
                "base_url": "https://other.example/v1",
                "api_key": "key2",
                "model": "m2",
            },
        },
    }
    migrated = migrate_profiles(legacy)
    claude = migrated["providers"]["claude"]
    assert claude == {
        "name": "claude",
        "base_url": "https://api.example.com/v1",
        "api_key": "key",
        "model": "m",
        "api_type": "openai_chat",
        "stream": True,
        "enabled": True,
        "is_default": True,
    }
    assert migrated["providers"]["gpt"]["is_default"] is False
    save_profiles(tmp_path, legacy)
    assert load_profiles(tmp_path) == migrated


def test_default_provider_fails_clearly_without_default():
    with pytest.raises(AdminConfigError, match="未设置默认"):
        default_provider({"providers": {"claude": PROVIDER}})


def test_atomic_writes_are_0600_and_preserve_unmanaged_env_keys(tmp_path):
    env = tmp_path / ".env.local"
    env.write_text(
        "# keep\nNEWS_HTTP_PROXY=http://127.0.0.1:2231\n"
        'TRANSLATION_API_BASE_URL= "https://old/v1"\nTRANSLATION_MODEL=old-model\n',
        encoding="utf-8",
    )
    write_env_local(tmp_path, PROVIDER)
    content = env.read_text(encoding="utf-8")
    assert "# keep" in content
    assert "NEWS_HTTP_PROXY=http://127.0.0.1:2231" in content
    assert "TRANSLATION_API_BASE_URL=https://api.example.com/v1" in content
    assert "TRANSLATION_API_TYPE=openai_chat" in content
    assert "TRANSLATION_STREAM=true" in content
    assert "old-model" not in content
    if os.name != "nt":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600

    profiles = tmp_path / "providers.json"
    save_profiles(tmp_path, {"providers": {"claude": PROVIDER}}, "providers.json")
    if os.name != "nt":
        assert stat.S_IMODE(profiles.stat().st_mode) == 0o640
    atomic_write_text(tmp_path / "secret", "value")
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "secret").stat().st_mode) == 0o600


def test_runtime_translation_uses_unique_default_not_stale_env(tmp_path):
    selected = {**PROVIDER, "is_default": True, "api_type": "anthropic_messages"}
    save_profiles(tmp_path, {"providers": {"claude": selected}}, "providers.json")
    config = runtime_translation_config(
        tmp_path / "providers.json",
        {
            "TRANSLATION_API_BASE_URL": "https://stale.example.com/v1",
            "TRANSLATION_API_KEY": "stale-secret",
            "TRANSLATION_MODEL": "stale-model",
            "TRANSLATION_API_TYPE": "openai_chat",
            "TRANSLATION_TIMEOUT_SECONDS": "42",
            "NEWS_DATA_DIR": str(tmp_path / "data"),
        },
        resolver=PUBLIC_IPS,
    )
    assert config.base_url == PROVIDER["base_url"]
    assert config.api_key == PROVIDER["api_key"]
    assert config.model == PROVIDER["model"]
    assert config.api_type == "anthropic_messages"
    assert config.timeout_seconds == 42

    save_profiles(tmp_path, {"providers": {"claude": PROVIDER}}, "providers.json")
    with pytest.raises(AdminConfigError, match="未设置默认"):
        runtime_translation_config(
            tmp_path / "providers.json", {}, resolver=PUBLIC_IPS
        )


def test_runtime_translation_rejects_non_public_default_target(tmp_path):
    save_profiles(
        tmp_path,
        {"providers": {"claude": {**PROVIDER, "is_default": True}}},
        "providers.json",
    )
    config = runtime_translation_config(tmp_path / "providers.json", {})
    translator = ApiTranslator(config, resolver=lambda host, port: ["127.0.0.1"])
    with pytest.raises(TranslationError, match="公网"):
        translator.probe()


def test_concurrent_profile_updates_do_not_lose_writes(tmp_path):
    barrier = threading.Barrier(12)

    def worker(index: int) -> None:
        barrier.wait()

        def add(data):
            provider = {**PROVIDER, "name": f"p{index}", "model": f"m{index}"}
            data["providers"][provider["name"]] = provider

        update_profiles(tmp_path, add, "providers.json")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert len(load_profiles(tmp_path, "providers.json")["providers"]) == 12


def test_ssrf_rejects_all_non_public_and_unsafe_targets():
    unsafe = [
        "http://api.example.com/v1",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?q=x",
        "https://api.example.com/v1#x",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://api.example.com/v1/messages",
        "https://api.example.com/v1/v1",
    ]
    for url in unsafe:
        with pytest.raises(AdminConfigError):
            validate_public_https_target(url, PUBLIC_IPS)
    for address in [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "::1",
        "fe80::1",
        "fc00::1",
    ]:
        with pytest.raises(AdminConfigError, match="全部 DNS 结果"):
            validate_public_https_target(
                "https://api.example.com/v1",
                lambda host, port, value=address: ["93.184.216.34", value],
            )
    assert (
        validate_public_https_target("https://api.example.com/prefix", PUBLIC_IPS)
        == "https://api.example.com/prefix/v1"
    )


def test_public_target_dns_validation_has_hard_timeout():
    def slow_resolver(host, port):
        time.sleep(0.05)
        return ["93.184.216.34"]

    started = time.monotonic()
    with pytest.raises(AdminConfigError, match="DNS 解析超时"):
        validate_public_https_target(
            "https://api.example.com/v1",
            slow_resolver,
            timeout_seconds=0.01,
        )
    assert time.monotonic() - started < 0.04


def test_production_never_serves_config_static_and_requires_login(prod_server):
    _, port, _, _ = prod_server
    status, data, _ = _request(port, "GET", "/.env", content_type=None)
    assert status == 404
    assert "TRANSLATION" not in json.dumps(data)
    status, _, _ = _request(port, "GET", "/admin/api/providers", content_type=None)
    assert status == 401
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/login",
        {"username": "admin", "password": "wrong"},
    )
    assert status == 401


def test_login_requires_origin_host_json_and_body_limit(prod_server):
    _, port, _, _ = prod_server
    body = {"username": "admin", "password": PANEL_PASSWORD}
    status, _, _ = _request(port, "POST", "/admin/api/login", body, origin="https://evil.test")
    assert status == 403
    status, _, _ = _request(port, "POST", "/admin/api/login", body, origin="null")
    assert status == 403
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/login",
        body,
        content_type="text/plain",
    )
    assert status == 415
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/login",
        b"x" * 20_000,
    )
    assert status == 413


def test_production_admin_uses_configured_host_not_matching_rebinding_host(tmp_path):
    htpasswd = tmp_path / "htpasswd-admin"
    htpasswd.write_text(
        f"admin:{apr1_hash(PANEL_PASSWORD)}\n",
        encoding="utf-8",
    )
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        htpasswd_file=htpasswd,
        site_url="https://news.example.com",
    )
    port = _start(server)
    body = {"username": "admin", "password": PANEL_PASSWORD}
    try:
        status, _, _ = _request(
            port,
            "POST",
            "/admin/api/login",
            body,
            origin="https://news.example.com",
            headers={"Host": "news.example.com"},
        )
        assert status == 200
        status, _, _ = _request(
            port,
            "POST",
            "/admin/api/login",
            body,
            origin="https://rebind.example",
            headers={"Host": "rebind.example"},
        )
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()


def test_admin_opaque_origin_requires_explicit_loopback_browser_compat(tmp_path):
    htpasswd = tmp_path / "htpasswd-admin"
    htpasswd.write_text(
        f"admin:{apr1_hash(PANEL_PASSWORD)}\n",
        encoding="utf-8",
    )
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        htpasswd_file=htpasswd,
        loopback_browser_compat=True,
    )
    port = _start(server)
    try:
        body = {"username": "admin", "password": PANEL_PASSWORD}
        status, _, _ = _request(
            port, "POST", "/admin/api/login", body, origin="null"
        )
        assert status == 200
        status, _, _ = _request(
            port,
            "POST",
            "/admin/api/login",
            body,
            origin="null",
            headers={"Host": f"rebind.example:{port}"},
        )
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()


def test_mutating_post_requires_session_bound_csrf_and_origin(prod_server):
    root, port, _, _ = prod_server
    cookie, csrf = _login(port)
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/providers",
        FORM,
        cookie=cookie,
    )
    assert status == 403
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/providers",
        FORM,
        cookie=cookie,
        csrf=csrf,
        origin="https://evil.test",
    )
    assert status == 403
    assert load_profiles(root, "providers.json")["providers"]["claude"]["model"] == "demo-model"


def test_get_provider_response_has_key_set_csrf_and_no_key_material(prod_server):
    _, port, _, _ = prod_server
    cookie, csrf = _login(port)
    status, data, _ = _request(
        port,
        "GET",
        "/admin/api/providers",
        cookie=cookie,
        content_type=None,
    )
    assert status == 200 and data["csrf_token"] == csrf
    provider = data["providers"]["claude"]
    assert provider["key_set"] is True
    assert len(provider["configuration_fingerprint"]) == 64
    assert "api_key" not in provider
    assert PROVIDER["api_key"] not in json.dumps(data)


def test_probe_uses_unsaved_values_exactly_once_and_only_persists_test_state(local_server):
    root, port, calls, _ = local_server
    save_profiles(root, {"providers": {"claude": PROVIDER}})
    env = root / ".env.local"
    env.write_text("NEWS_KEEP=yes\n", encoding="utf-8")
    before_profiles = (root / ".env.providers.local").read_bytes()
    before_env = env.read_bytes()
    unsaved = {
        **FORM,
        "base_url": "https://unsaved.example/prefix",
        "model": "unsaved-model",
        "api_type": "anthropic_messages",
        "stream": False,
        "api_key": "",
    }
    status, data, _ = _request(port, "POST", "/admin/api/providers/test", unsaved)
    assert status == 200
    assert len(calls) == 1
    assert calls[0].base_url == "https://unsaved.example/prefix/v1"
    assert calls[0].model == "unsaved-model"
    assert calls[0].api_key == PROVIDER["api_key"]
    assert calls[0].api_type == "anthropic_messages"
    assert calls[0].stream is False
    assert data["connection_auth"]["status"] == "success"
    assert data["model_return"]["status"] == "success"
    assert data["configuration_state"] == "unsaved"
    assert data["output"] == "Hello <unsafe>"
    assert (root / ".env.providers.local").read_bytes() == before_profiles
    assert env.read_bytes() == before_env
    assert not (root / "var").exists()
    assert (root / "provider-tests.json").is_file()


def test_probe_labels_configuration_matching_saved_profile(local_server):
    root, port, _, _ = local_server
    save_profiles(root, {"providers": {"claude": PROVIDER}})

    status, data, _ = _request(port, "POST", "/admin/api/providers/test", FORM)

    assert status == 200
    assert data["configuration_state"] == "matching_saved"


@pytest.mark.parametrize(
    ("category", "connection_status", "model_status"),
    [
        ("tls", "failed", "not_run"),
        ("total_timeout", "failed", "not_run"),
        ("response_format", "success", "failed"),
    ],
)
def test_probe_error_reports_the_correct_stage(
    prod_server, category, connection_status, model_status
):
    _, port, _, server = prod_server
    auth = _login(port)

    def fail(config):
        raise TranslationError("safe failure", category=category)

    server.probe_callback = fail
    status, data = _post(port, "/admin/api/providers/test", FORM, auth)
    assert status == 502
    assert data["category"] == category
    assert data["connection_auth"]["status"] == connection_status
    assert data["model_return"]["status"] == model_status


def test_probe_timeout_after_response_started_reports_model_stage(prod_server):
    _, port, _, server = prod_server
    auth = _login(port)

    def fail(config):
        raise TranslationError(
            "safe failure",
            category="total_timeout",
            response_started=True,
        )

    server.probe_callback = fail
    status, data = _post(port, "/admin/api/providers/test", FORM, auth)
    assert status == 502
    assert data["category"] == "total_timeout"
    assert data["connection_auth"]["status"] == "success"
    assert data["model_return"]["status"] == "failed"


def test_probe_redacts_current_api_key_from_response_and_test_state(prod_server):
    root, port, _, server = prod_server
    auth = _login(port)
    server.probe_callback = lambda config: f"echo {config.api_key}"

    status, data = _post(port, "/admin/api/providers/test", FORM, auth)
    assert status == 200
    serialized = json.dumps(data, ensure_ascii=False)
    assert PROVIDER["api_key"] not in serialized
    assert "[已脱敏]" in data["output"]
    assert PROVIDER["api_key"] not in (root / "provider-tests.json").read_text(
        encoding="utf-8"
    )

    status, listed, _ = _request(
        port,
        "GET",
        "/admin/api/providers",
        cookie=auth[0],
        content_type=None,
    )
    assert status == 200
    assert PROVIDER["api_key"] not in json.dumps(listed, ensure_ascii=False)


def test_probe_rejects_message_field_without_calling_probe(local_server):
    _, port, calls, _ = local_server
    status, data, _ = _request(
        port,
        "POST",
        "/admin/api/providers/test",
        {**FORM, "message": "user supplied"},
    )
    assert status == 400
    assert "message" in data["error"]
    assert calls == []


def test_probe_fingerprint_includes_secret_change_without_exposing_it(tmp_path):
    first = provider_fingerprint(tmp_path, PROVIDER)
    changed_key = provider_fingerprint(tmp_path, {**PROVIDER, "api_key": "different-secret"})
    changed_model = provider_fingerprint(tmp_path, {**PROVIDER, "model": "other"})
    assert len({first, changed_key, changed_model}) == 3
    assert PROVIDER["api_key"] not in first


def test_saved_test_state_becomes_stale_after_saved_config_change(local_server):
    root, port, _, _ = local_server
    save_profiles(root, {"providers": {"claude": PROVIDER}})
    status, _, _ = _request(port, "POST", "/admin/api/providers/test", FORM)
    assert status == 200
    state = current_test_state(root, PROVIDER)
    assert state is not None and state["stale"] is False
    assert current_test_state(root, {**PROVIDER, "api_key": "changed"})["stale"] is True


def test_provider_test_ssrf_is_applied_before_probe_in_production(prod_server):
    _, port, calls, server = prod_server
    auth = _login(port)
    server.resolver = lambda host, port: ["169.254.169.254"]
    status, data = _post(port, "/admin/api/providers/test", FORM, auth)
    assert status == 400
    assert data["category"] == "configuration"
    assert calls == []


def test_provider_save_and_default_apply_ssrf_guard_in_production(prod_server):
    root, port, _, server = prod_server
    auth = _login(port)
    server.resolver = lambda host, port: ["169.254.169.254"]
    status, data = _post(port, "/admin/api/providers", FORM, auth)
    assert status == 400
    assert "公网" in data["error"]
    status, data = _post(port, "/admin/api/providers/default", _default_body(root), auth)
    assert status == 400
    assert "公网" in data["error"]


def test_probe_mutex_rejects_overlapping_test(prod_server):
    _, port, _, server = prod_server
    auth = _login(port)
    assert server.probe_lock.acquire(blocking=False)
    try:
        status, data = _post(port, "/admin/api/providers/test", FORM, auth)
    finally:
        server.probe_lock.release()
    assert status == 409
    assert "正在运行" in data["error"]


def test_authenticated_admin_actions_are_not_rate_limited(tmp_path):
    (tmp_path / "htpasswd-admin").write_text(
        f"admin:{apr1_hash(PANEL_PASSWORD)}\n",
        encoding="utf-8",
    )
    server = create_server(
        tmp_path,
        tmp_path,
        0,
        serve_static=False,
        htpasswd_file=tmp_path / "htpasswd-admin",
        resolver=PUBLIC_IPS,
        probe_callback=lambda config: "ok",
        sensitive_limit=1,
        sensitive_window=60,
    )
    port = _start(server)
    try:
        auth = _login(port)
        status, _ = _post(port, "/admin/api/providers/test", FORM, auth)
        assert status == 200
        status, _data = _post(port, "/admin/api/providers/test", FORM, auth)
        assert status == 200
        status, _ = _post(port, "/admin/api/providers", FORM, auth)
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_save_edit_key_blank_and_provider_lifecycle(prod_server):
    root, port, _, _ = prod_server
    auth = _login(port)
    status, _ = _post(
        port,
        "/admin/api/providers",
        {**FORM, "base_url": "https://new.example.com", "model": "new-model", "api_key": ""},
        auth,
    )
    assert status == 200
    stored = load_profiles(root, "providers.json")["providers"]["claude"]
    assert stored["api_key"] == PROVIDER["api_key"]
    assert stored["base_url"] == "https://new.example.com/v1"
    assert stored["model"] == "new-model"

    status, data = _post(port, "/admin/api/providers/default", _default_body(root), auth)
    assert status == 409 and data["confirmation_required"] is True
    status, data = _post(
        port,
        "/admin/api/providers/default",
        _default_body(root, confirm=True),
        auth,
    )
    assert status == 200 and data["active"] == "claude"
    assert default_provider(load_profiles(root, "providers.json"))["name"] == "claude"
    env = (root / ".env").read_text(encoding="utf-8")
    assert "NEWS_SITE_URL=https://news.example.com" in env
    assert "TRANSLATION_MODEL=new-model" in env

    status, _ = _post(
        port,
        "/admin/api/providers",
        {**FORM, "base_url": "https://edited.example.com", "model": "edited-model", "api_key": ""},
        auth,
    )
    assert status == 200
    env = read_env(root / ".env")
    assert env["TRANSLATION_API_BASE_URL"] == "https://edited.example.com/v1"
    assert env["TRANSLATION_MODEL"] == "edited-model"

    status, _ = _post(
        port,
        "/admin/api/providers",
        {"name": "claude", "delete": True},
        auth,
    )
    assert status == 409
    status, _ = _post(
        port,
        "/admin/api/providers",
        {
            **FORM,
            "base_url": "https://new.example.com",
            "model": "new-model",
            "api_key": "",
            "enabled": False,
        },
        auth,
    )
    assert status == 400
    status, _ = _post(port, "/admin/api/translation/disable", {"confirm": True}, auth)
    assert status == 200
    assert not any(
        provider["is_default"]
        for provider in load_profiles(root, "providers.json")["providers"].values()
    )
    status, _ = _post(
        port,
        "/admin/api/providers",
        {"name": "claude", "delete": True},
        auth,
    )
    assert status == 200


def test_successful_probe_allows_default_without_second_confirmation(prod_server):
    root, port, calls, _ = prod_server
    auth = _login(port)
    status, _ = _post(port, "/admin/api/providers/test", FORM, auth)
    assert status == 200 and len(calls) == 1
    status, data = _post(port, "/admin/api/providers/default", _default_body(root), auth)
    assert status == 200 and data["active"] == "claude"
    assert default_provider(load_profiles(root, "providers.json"))["name"] == "claude"


def test_default_rejects_profile_changed_between_precheck_and_locked_update(
    prod_server, monkeypatch
):
    root, port, _, _ = prod_server
    auth = _login(port)
    body = _default_body(root, confirm=True)

    def racing_update(project_root, transform, filename):
        def edit(data):
            data["providers"]["claude"]["model"] = "raced-model"

        update_profiles(project_root, edit, filename)
        return update_profiles(project_root, transform, filename)

    monkeypatch.setattr("news_digest.preview_server.update_profiles", racing_update)
    status, data = _post(port, "/admin/api/providers/default", body, auth)

    assert status == 409
    assert "修改" in data["error"]
    stored = load_profiles(root, "providers.json")["providers"]["claude"]
    assert stored["model"] == "raced-model" and stored["is_default"] is False


def test_password_change_rotates_session_and_uses_atomic_file(prod_server):
    root, port, _, _ = prod_server
    auth = _login(port)
    status, _ = _post(
        port,
        "/admin/api/password",
        {"current_password": PANEL_PASSWORD, "password": "new-password-1"},
        auth,
    )
    assert status == 200
    if os.name != "nt":
        assert stat.S_IMODE((root / "htpasswd-admin").stat().st_mode) == 0o600
    status, _, _ = _request(
        port,
        "GET",
        "/admin/api/providers",
        cookie=auth[0],
        content_type=None,
    )
    assert status == 401
    _login(port, "new-password-1")


def test_public_submission_is_uniform_sends_fake_confirmation_and_not_delivery(public_server):
    root, port, sent, _ = public_server
    status, first, _ = _subscribe(port, "reader@example.com")
    assert status == 202
    assert len(sent) == 1
    config, recipient, confirmation_url = sent[0]
    assert config.host == "smtp.example.com"
    assert recipient == "reader@example.com"
    assert confirmation_url.startswith("https://news.example.com/subscribe/confirm/")

    status, duplicate, _ = _subscribe(port, "reader@example.com")
    status_unknown, unknown, _ = _subscribe(port, "other@example.com")
    assert status == status_unknown == 202
    assert first == duplicate == unknown
    assert len(sent) == 2

    conn = db.connect(root / "news.db")
    assert db.subscription_by_email(conn, "reader@example.com").status == "pending"
    assert conn.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    conn.close()


def test_public_subscription_switch_closes_new_submission_endpoints(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (tmp_path / ".env.local").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\n",
        encoding="utf-8",
    )
    server = create_server(
        tmp_path,
        site,
        0,
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
        public_subscription_enabled=False,
    )
    port = _start(server)
    try:
        status, _, _ = _request(
            port, "GET", "/subscribe/api/csrf", content_type=None
        )
        assert status == 404
        status, _, _ = _request(
            port,
            "POST",
            "/subscribe/api/",
            {"email": "reader@example.com", "website": "", "csrf_token": "unused"},
        )
        assert status == 404
        conn = db.connect(tmp_path / "news.db")
        assert db.subscription_counts(conn) == {
            "pending": 0,
            "active": 0,
            "unsubscribed": 0,
            "disabled": 0,
        }
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_public_subscription_ready_without_legacy_smtp_recipients(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (tmp_path / ".env").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\n",
        encoding="utf-8",
    )
    server = create_server(
        tmp_path,
        site,
        0,
        env_file=".env",
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
    )
    port = _start(server)
    try:
        status, data, headers = _request(
            port, "GET", "/subscribe/api/csrf", content_type=None
        )
        assert status == 200 and data["csrf_token"]
        assert "Secure" in headers["set-cookie"]
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_public_submission_confirms_against_same_preview_database(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    sent: list[tuple[Any, str, str]] = []
    (tmp_path / ".env").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\n",
        encoding="utf-8",
    )

    def confirmation_sender(config, recipient, url):
        sent.append((config, recipient, url))

    server = create_server(
        tmp_path,
        site,
        0,
        env_file=".env",
        db_path=tmp_path / "news.db",
        site_url="",
        confirmation_sender=confirmation_sender,
        public_subscription_enabled=True,
        loopback_public_subscription=True,
    )
    port = _start(server)
    try:
        status, data, headers = _request(
            port, "GET", "/subscribe/api/csrf", content_type=None
        )
        assert status == 200 and data["csrf_token"]
        assert "Secure" not in headers["set-cookie"]

        loopback_origin = f"http://127.0.0.1:{port}"
        status, _, _ = _subscribe(
            port, "reader@example.com", origin=loopback_origin
        )
        assert status == 202
        assert len(sent) == 1
        confirmation_url = sent[0][2]
        assert confirmation_url.startswith(
            f"http://127.0.0.1:{port}/subscribe/confirm/"
        )

        status, _, _ = _request(
            port,
            "GET",
            urlsplit(confirmation_url).path,
            content_type=None,
            decode_json=False,
        )
        assert status == 200
        conn = db.connect(tmp_path / "news.db")
        assert db.subscription_by_email(conn, "reader@example.com").status == "active"
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_public_submission_rejects_dns_rebinding_host(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (tmp_path / ".env").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\n",
        encoding="utf-8",
    )
    sent = []
    server = create_server(
        tmp_path,
        site,
        0,
        env_file=".env",
        db_path=tmp_path / "news.db",
        confirmation_sender=lambda *args: sent.append(args),
        public_subscription_enabled=True,
        loopback_public_subscription=True,
    )
    port = _start(server)
    rebound_host = f"rebind.example:{port}"
    try:
        status, _, headers = _request(
            port,
            "GET",
            "/subscribe/api/csrf",
            content_type=None,
            origin=f"http://{rebound_host}",
            headers={"Host": rebound_host},
        )
        assert status == 403
        assert "set-cookie" not in headers

        cookie, csrf = _public_csrf(
            port, origin=f"http://127.0.0.1:{port}"
        )
        status, _, _ = _subscribe(
            port,
            "reader@example.com",
            cookie=cookie,
            csrf=csrf,
            origin=f"http://{rebound_host}",
            headers={"Host": rebound_host},
        )
        assert status == 403
        assert sent == []
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_public_submission_requires_static_preview(tmp_path):
    with pytest.raises(ValueError, match="static preview mode"):
        create_server(
            tmp_path,
            tmp_path,
            0,
            serve_static=False,
            loopback_public_subscription=True,
        )


def test_public_submission_requires_origin_json_and_bound_csrf(public_server):
    _, port, sent, _ = public_server
    status, _, headers = _request(
        port,
        "GET",
        "/subscribe/api/csrf",
        content_type=None,
        origin="https://rebind.example",
        headers={"Host": "rebind.example"},
    )
    assert status == 403 and "set-cookie" not in headers
    cookie, csrf = _public_csrf(port)
    status, _, _ = _subscribe(
        port, "reader@example.com", cookie=cookie, csrf=csrf, origin="https://evil.test"
    )
    assert status == 403
    status, _, _ = _subscribe(
        port, "reader@example.com", cookie=cookie, csrf=csrf, content_type="text/plain"
    )
    assert status == 415
    status, _, _ = _subscribe(port, "reader@example.com", cookie=cookie, csrf="bad")
    assert status == 403
    other_cookie, other_csrf = _public_csrf(port)
    status, _, _ = _subscribe(port, "reader@example.com", cookie=cookie, csrf=other_csrf)
    assert status == 403
    assert other_cookie != cookie
    assert sent == []


def test_public_honeypot_length_crlf_and_rate_limit_do_not_send(public_server):
    root, port, sent, server = public_server
    status, honey, _ = _subscribe(port, "reader@example.com", website="bot")
    status_long, long_body, _ = _subscribe(port, "x" * 255)
    status_crlf, crlf, _ = _subscribe(port, "reader@example.com\r\nBcc:x@example.com")
    assert (status, status_long, status_crlf) == (202, 202, 202)
    assert honey == long_body == crlf
    assert sent == []
    conn = db.connect(root / "news.db")
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1
    conn.close()

    server.rate_events.clear()
    server.sensitive_limit = 1
    status, _, _ = _subscribe(port, "reader@example.com")
    limited, limited_data, _ = _subscribe(port, "second@example.com")
    assert status == 202 and limited == 429
    assert limited_data == honey
    assert len(sent) == 1


def test_public_rate_limit_uses_valid_nginx_real_ip(public_server):
    _, port, sent, server = public_server
    server.rate_events.clear()
    server.sensitive_limit = 1
    first, _, _ = _subscribe(
        port,
        "first@example.com",
        headers={"X-Real-IP": "93.184.216.34"},
    )
    second, _, _ = _subscribe(
        port,
        "second@example.com",
        headers={"X-Real-IP": "142.250.72.14"},
    )
    limited, _, _ = _subscribe(
        port,
        "third@example.com",
        headers={"X-Real-IP": "93.184.216.34"},
    )
    assert (first, second, limited) == (202, 202, 429)
    assert len(sent) == 2


def test_public_subscription_readiness_tracks_runtime_mail_pause_and_resume(public_server):
    root, port, sent, _ = public_server
    status, _, _ = _request(port, "GET", "/subscribe/api/csrf", content_type=None)
    assert status == 200

    env_path = root / ".env"
    enabled = env_path.read_text(encoding="utf-8")
    env_path.write_text(
        enabled.replace("EMAIL_DELIVERY_ENABLED=true", "EMAIL_DELIVERY_ENABLED=false"),
        encoding="utf-8",
    )
    status, _, _ = _request(port, "GET", "/subscribe/api/csrf", content_type=None)
    assert status == 404
    assert sent == []

    env_path.write_text(enabled, encoding="utf-8")
    status, _, _ = _request(port, "GET", "/subscribe/api/csrf", content_type=None)
    assert status == 200


def test_confirmation_failure_removes_live_token_but_leaves_pending_for_retry(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (tmp_path / ".env").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\nSMTP_RECIPIENTS=admin@example.com\n",
        encoding="utf-8",
    )

    def fail(config, recipient, url):
        raise RuntimeError("offline fake failure")

    server = create_server(
        tmp_path,
        site,
        0,
        env_file=".env",
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
        confirmation_sender=fail,
    )
    port = _start(server)
    try:
        status, _, _ = _subscribe(port, "reader@example.com")
        assert status == 202
        conn = db.connect(tmp_path / "news.db")
        assert db.subscription_by_email(conn, "reader@example.com").status == "pending"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM subscription_tokens WHERE purpose='confirm'"
            ).fetchone()[0]
            == 0
        )
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_site_admin_account_controls_require_auth_csrf_and_are_idempotent(
    prod_server, monkeypatch
):
    root, port, _, server = prod_server
    status, _data, _headers = _request(port, "GET", "/admin/api/site/overview", content_type=None)
    assert status == 401
    auth = _login(port)
    conn = db.connect(root / "news.db")
    try:
        user = db.upsert_pending_user(
            conn,
            email="member@example.com",
            email_key=db.delivery_recipient_key("member@example.com"),
            password_hash="pbkdf2_sha256$1$00$00",
            now="2026-08-30T12:00:00+00:00",
        )
    finally:
        conn.close()
    status, _data = _post(
        port,
        "/admin/api/site/user-status",
        {"user_id": user.id, "status": "disabled"},
        (auth[0], "wrong"),
    )
    assert status == 403
    status, data = _post(
        port, "/admin/api/site/user-status", {"user_id": user.id, "status": "disabled"}, auth
    )
    assert status == 200 and data["status"] == "disabled"
    status, data = _post(
        port, "/admin/api/site/user-status", {"user_id": user.id, "status": "active"}, auth
    )
    assert status == 200 and data["status"] == "active"
    status, data = _post(
        port,
        "/admin/api/site/user-grant",
        {"user_id": user.id, "plan": "monthly", "days": 31},
        auth,
    )
    assert status == 200 and data["paid_until"]
    first_paid_until = dt.datetime.fromisoformat(data["paid_until"])
    status, data = _post(
        port,
        "/admin/api/site/user-grant",
        {"user_id": user.id, "plan": "yearly", "days": 366},
        auth,
    )
    assert status == 200 and data["plan"] == "yearly" and data["days_added"] == 366
    second_paid_until = dt.datetime.fromisoformat(data["paid_until"])
    assert second_paid_until == first_paid_until + dt.timedelta(days=366)
    status, data = _post(
        port,
        "/admin/api/site/user-grant",
        {"user_id": user.id, "plan": "yearly", "days": 366},
        auth,
    )
    assert status == 200
    assert dt.datetime.fromisoformat(data["paid_until"]) == second_paid_until + dt.timedelta(
        days=366
    )
    status, _data = _post(
        port,
        "/admin/api/site/user-grant",
        {"user_id": user.id, "plan": "yearly", "days": 31.5},
        auth,
    )
    assert status == 409
    status, data = _post(
        port,
        "/admin/api/site/user-subscription-clear",
        {"user_id": user.id, "confirm": True},
        auth,
    )
    assert status == 200 and data["plan"] is None and data["paid_until"] is None
    status, _data = _post(
        port,
        "/admin/api/site/user-subscription-clear",
        {"user_id": user.id, "confirm": False},
        auth,
    )
    assert status == 409
    conn = db.connect(root / "news.db")
    try:
        approved = db.create_order(
            conn,
            user_id=user.id,
            plan="monthly",
            amount_cents=990,
            payment_ref="approval-test",
            now="2026-08-30T12:01:00+00:00",
        )
        rejected = db.create_order(
            conn,
            user_id=user.id,
            plan="yearly",
            amount_cents=9900,
            payment_ref="rejection-test",
            now="2026-08-30T12:02:00+00:00",
        )
    finally:
        conn.close()
    status, data = _post(
        port,
        "/admin/api/site/order-decide",
        {"order_id": approved.id, "approve": True},
        auth,
    )
    assert status == 200 and data["status"] == "approved"
    status, _data = _post(
        port,
        "/admin/api/site/order-decide",
        {"order_id": approved.id, "approve": True},
        auth,
    )
    assert status == 409
    status, data = _post(
        port,
        "/admin/api/site/order-decide",
        {"order_id": rejected.id, "approve": False},
        auth,
    )
    assert status == 200 and data["status"] == "rejected"
    status, _data = _post(
        port,
        "/admin/api/site/order-decide",
        {"order_id": rejected.id, "approve": "false"},
        auth,
    )
    assert status == 409
    status, data = _post(
        port,
        "/admin/api/site/settings",
        {
            "paywall_enabled": True,
            "monthly_price_cents": 1234,
            "monthly_discount_percent": 25,
            "yearly_discount_percent": 10,
        },
        auth,
    )
    assert status == 200 and data["ok"] is True
    status, data = _post(port, "/admin/api/site/settings", {"monthly_price_cents": -1}, auth)
    assert status == 409
    status, data = _post(
        port, "/admin/api/site/settings", {"monthly_price_cents": 12.5}, auth
    )
    assert status == 409
    status, data = _post(
        port, "/admin/api/site/settings", {"monthly_discount_percent": 101}, auth
    )
    assert status == 409
    status, data = _post(
        port, "/admin/api/site/settings", {"monthly_discount_percent": 12.5}, auth
    )
    assert status == 409
    status, data = _post(
        port, "/admin/api/site/settings", {"paywall_enabled": "true"}, auth
    )
    assert status == 409
    valid_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
        "A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    status, data = _post(
        port, "/admin/api/site/settings", {"payment_qr_data_url": valid_png}, auth
    )
    assert status == 200 and data["ok"] is True
    large_png = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"x" * (600 * 1024)
    ).decode("ascii")
    status, data = _post(
        port, "/admin/api/site/settings", {"payment_qr_data_url": large_png}, auth
    )
    assert status == 200 and data["ok"] is True
    for invalid_qr in (
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "data:image/png;base64,bm90LWEtcG5n",
        "data:image/png;base64,%%%",
    ):
        status, _data = _post(
            port,
            "/admin/api/site/settings",
            {"payment_qr_data_url": invalid_qr},
            auth,
        )
        assert status == 409
    generated = iter(["ABCD-EFGH", "ABCD-EFGH", "JKLM-NPQR"])
    monkeypatch.setattr(
        "news_digest.preview_server.accounts.generate_redemption_code",
        lambda: next(generated),
    )
    status, data = _post(port, "/admin/api/site/codes", {"plan": "monthly", "count": 1}, auth)
    assert status == 200 and len(data["codes"]) == 1
    first_code = data["codes"][0]
    assert first_code == "ABCD-EFGH"
    status, data = _post(port, "/admin/api/site/codes", {"plan": "monthly", "count": 1}, auth)
    assert status == 200 and data["codes"] == ["JKLM-NPQR"]
    conn = db.connect(root / "news.db")
    try:
        with conn:
            conn.execute(
                "UPDATE redemption_codes SET status = 'revoked' WHERE prefix = ?",
                (accounts.redemption_prefix(first_code),),
            )
    finally:
        conn.close()
    status, overview, _headers = _request(
        port, "GET", "/admin/api/site/overview", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert all(item["status"] != "revoked" for item in overview["codes"])
    visible_code = next(item for item in overview["codes"] if item["prefix"] == "JKLM-NPQ")
    assert visible_code["code"] == "JKLM-NPQR"
    code_id = next(
        item["id"] for item in overview["codes"] if item["prefix"] == "JKLM-NPQ"
    )
    status, data = _post(
        port, "/admin/api/site/code-delete", {"code_id": code_id}, auth
    )
    assert status == 200 and data["ok"] is True
    status, overview, _headers = _request(
        port, "GET", "/admin/api/site/overview", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert all(item["id"] != code_id for item in overview["codes"])
    status, _data = _post(
        port, "/admin/api/site/code-delete", {"code_id": code_id}, auth
    )
    assert status == 409
    status, data = _post(port, "/admin/api/site/overview", {}, auth)
    assert status == 404
    status, overview, _headers = _request(
        port, "GET", "/admin/api/site/overview", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert first_code not in json.dumps(overview)


def test_site_payment_settings_require_auth_csrf_and_never_return_pkey(
    mail_admin_server,
):
    root, port, _, _, _ = mail_admin_server
    status, _data, _headers = _request(
        port, "POST", "/admin/api/site/payment-settings", _payment_form()
    )
    assert status == 401

    auth = _login(port)
    status, _data = _post(
        port,
        "/admin/api/site/payment-settings",
        _payment_form(),
        (auth[0], "wrong"),
    )
    assert status == 403

    status, data = _post(
        port, "/admin/api/site/payment-settings", _payment_form(), auth
    )
    assert status == 200
    assert data["payment"]["pkey_set"] is True
    assert data["payment"]["notify_url"] == (
        "https://news.example.com/subscribe/api/payment/easypay"
    )
    assert "merchant-secret" not in json.dumps(data)
    saved = read_env(root / ".env")
    encoded_pkey = saved["EPAY_PKEY"]
    assert encoded_pkey.startswith("nd-b64-v1:")
    assert saved["EPAY_API_BASE"] == "https://pay.example.com"

    status, data = _post(
        port,
        "/admin/api/site/payment-settings",
        _payment_form(pkey="", payment_type="wxpay"),
        auth,
    )
    assert status == 200 and data["payment"]["payment_type"] == "wxpay"
    assert read_env(root / ".env")["EPAY_PKEY"] == encoded_pkey

    status, overview, _headers = _request(
        port, "GET", "/admin/api/site/overview", cookie=auth[0], content_type=None
    )
    assert status == 200 and overview["payment"]["pkey_set"] is True
    serialized = json.dumps(overview)
    assert "merchant-secret" not in serialized
    assert encoded_pkey not in serialized


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_base": "http://pay.example.com"},
        {"pid": ""},
        {"pkey": ""},
        {"payment_type": "card&invalid"},
        {"order_ttl_seconds": 59},
        {"order_ttl_seconds": 300.0},
        {"amount_hold_seconds": 299},
    ],
)
def test_site_payment_settings_reject_invalid_configuration(
    mail_admin_server, overrides
):
    _, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, data = _post(
        port,
        "/admin/api/site/payment-settings",
        _payment_form(**overrides),
        auth,
    )
    assert status == 409
    assert data["category"] == "configuration"


def test_site_payment_pkey_clear_requires_confirmation_and_disables_gateway(
    mail_admin_server,
):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, _data = _post(
        port, "/admin/api/site/payment-settings", _payment_form(), auth
    )
    assert status == 200
    status, _data = _post(
        port,
        "/admin/api/site/payment-clear-pkey",
        {"confirm": False},
        auth,
    )
    assert status == 409
    status, data = _post(
        port,
        "/admin/api/site/payment-clear-pkey",
        {"confirm": True},
        auth,
    )
    assert status == 200 and data == {
        "ok": True,
        "pkey_set": False,
        "enabled": False,
    }
    saved = read_env(root / ".env")
    assert saved["EPAY_ENABLED"] == "false"
    assert saved["EPAY_PKEY"] == ""


def test_unsettled_order_allows_disable_but_blocks_payment_identity_change_and_clear(
    mail_admin_server,
):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, _data = _post(
        port, "/admin/api/site/payment-settings", _payment_form(), auth
    )
    assert status == 200
    env = read_env(root / ".env")
    config = payments.settlement_config_from_mapping(
        {**env, "NEWS_SITE_URL": "https://news.example.com"}
    )
    assert config is not None
    conn = db.connect(root / "news.db")
    now = dt.datetime.now(dt.UTC).isoformat()
    user = db.upsert_pending_user(
        conn,
        email="locked-payment@example.com",
        email_key=db.delivery_recipient_key("locked-payment@example.com"),
        password_hash="hash",
        now=now,
    )
    user = db.activate_user(conn, email_key=user.email_key, now=now)
    db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_locked",
        payment_type=config.payment_type,
        payment_config_id=payments.config_identity(config),
        now=now,
        ttl_seconds=config.order_ttl_seconds,
        amount_hold_seconds=config.amount_hold_seconds,
    )
    conn.close()

    status, _data = _post(
        port,
        "/admin/api/site/payment-settings",
        _payment_form(enabled=False, pkey=""),
        auth,
    )
    assert status == 200
    status, data = _post(
        port,
        "/admin/api/site/payment-settings",
        _payment_form(enabled=False, pkey="replacement-secret"),
        auth,
    )
    assert status == 409 and data["category"] == "configuration"
    status, data = _post(
        port,
        "/admin/api/site/payment-clear-pkey",
        {"confirm": True},
        auth,
    )
    assert status == 409 and data["category"] == "configuration"


def test_admin_reconciles_paid_order_through_shared_settlement_transaction(
    mail_admin_server, monkeypatch
):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, _data = _post(
        port, "/admin/api/site/payment-settings", _payment_form(), auth
    )
    assert status == 200
    config = payments.settlement_config_from_mapping(
        {**read_env(root / ".env"), "NEWS_SITE_URL": "https://news.example.com"}
    )
    assert config is not None
    conn = db.connect(root / "news.db")
    now = dt.datetime.now(dt.UTC).isoformat()
    user = db.upsert_pending_user(
        conn,
        email="reconcile-admin@example.com",
        email_key=db.delivery_recipient_key("reconcile-admin@example.com"),
        password_hash="hash",
        now=now,
    )
    user = db.activate_user(conn, email_key=user.email_key, now=now)
    order, _ = db.reserve_payment_order(
        conn,
        user_id=user.id,
        plan="monthly",
        base_amount_cents=990,
        merchant_order_no="news_admin_reconcile",
        payment_type=config.payment_type,
        payment_config_id=payments.config_identity(config),
        now=now,
        ttl_seconds=config.order_ttl_seconds,
        amount_hold_seconds=config.amount_hold_seconds,
    )
    db.record_payment_order_created(
        conn,
        order_id=order.id,
        provider_trade_no="FASTPAY-ADMIN-1",
        payment_url="https://pay.example.com/pay/FASTPAY-ADMIN-1",
        now=now,
    )
    conn.close()

    monkeypatch.setattr(
        "news_digest.preview_server.payments.query_payment",
        lambda _config, **kwargs: payments.PaymentQuery(
            merchant_order_no=kwargs["merchant_order_no"],
            provider_trade_no="FASTPAY-ADMIN-1",
            amount_cents=kwargs["expected_amount_cents"],
            trade_status="TRADE_SUCCESS",
        ),
    )
    status, data = _post(
        port, "/admin/api/site/payment-reconcile", {"order_id": order.id}, auth
    )
    assert status == 200 and data["status"] == "paid"
    conn = db.connect(root / "news.db")
    assert db.order_by_id(conn, order.id).status == "paid"
    assert db.user_by_id(conn, user.id).paid_until is not None
    conn.close()


def test_site_settings_reject_price_below_gateway_minimum(mail_admin_server):
    _, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, data = _post(
        port,
        "/admin/api/site/settings",
        {
            "monthly_price_cents": 10,
            "yearly_price_cents": 9900,
            "monthly_discount_percent": 0,
            "yearly_discount_percent": 0,
            "paywall_enabled": True,
        },
        auth,
    )
    assert status == 409 and data["category"] == "lifecycle"


def test_site_overview_survives_invalid_saved_payment_intervals(mail_admin_server):
    root, port, _, _, _ = mail_admin_server
    with (root / ".env").open("a", encoding="utf-8") as handle:
        handle.write(
            "EPAY_ORDER_TTL_SECONDS=invalid\n"
            "EPAY_AMOUNT_HOLD_SECONDS=invalid\n"
        )
    auth = _login(port)
    status, overview, _headers = _request(
        port, "GET", "/admin/api/site/overview", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert overview["payment"]["order_ttl_seconds"] == 300
    assert overview["payment"]["amount_hold_seconds"] == 3600


def test_site_account_can_be_promoted_and_revoked_as_admin(prod_server):
    root, port, _, _ = prod_server
    conn = db.connect(root / "news.db")
    try:
        user = db.upsert_pending_user(
            conn,
            email="editor@example.com",
            email_key=db.delivery_recipient_key("editor@example.com"),
            password_hash=accounts.hash_password("editor-password-123"),
            now="2026-08-30T12:00:00+00:00",
        )
        db.activate_user(conn, email_key=user.email_key, now="2026-08-30T12:01:00+00:00")
    finally:
        conn.close()

    root_auth = _login(port)
    status, data = _post(
        port,
        "/admin/api/site/user-admin",
        {"user_id": user.id, "is_admin": True, "confirm": True},
        root_auth,
    )
    assert status == 200 and data["is_admin"] is True
    status, overview, _headers = _request(
        port, "GET", "/admin/api/site/overview", cookie=root_auth[0], content_type=None
    )
    assert status == 200
    promoted = next(item for item in overview["users"] if item["id"] == user.id)
    assert promoted["is_admin"] is True

    status, _data, headers = _request(
        port,
        "POST",
        "/admin/api/login",
        {"username": "editor@example.com", "password": "editor-password-123"},
    )
    assert status == 200
    site_admin_cookie = headers["set-cookie"].split(";", 1)[0]
    status, provider_data, _headers = _request(
        port,
        "GET",
        "/admin/api/providers",
        cookie=site_admin_cookie,
        content_type=None,
    )
    assert status == 200
    status, _data, _headers = _request(
        port,
        "POST",
        "/admin/api/password",
        {"current_password": "editor-password-123", "password": "replacement-123"},
        cookie=site_admin_cookie,
        csrf=provider_data["csrf_token"],
    )
    assert status == 403

    status, data = _post(
        port,
        "/admin/api/site/user-admin",
        {"user_id": user.id, "is_admin": False, "confirm": True},
        root_auth,
    )
    assert status == 200 and data["is_admin"] is False
    status, _data, _headers = _request(
        port,
        "GET",
        "/admin/api/providers",
        cookie=site_admin_cookie,
        content_type=None,
    )
    assert status == 401
    status, _data, _headers = _request(
        port,
        "POST",
        "/admin/api/login",
        {"username": "editor@example.com", "password": "editor-password-123"},
    )
    assert status == 401


def test_confirmation_unknown_keeps_latch_and_logs_only_safe_diagnostics(tmp_path, capsys):
    site = tmp_path / "site"
    site.mkdir()
    (tmp_path / ".env").write_text(
        "EMAIL_DELIVERY_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=true\n"
        "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\nSMTP_RECIPIENTS=admin@example.com\n",
        encoding="utf-8",
    )
    calls = []

    def unknown(config, recipient, url):
        calls.append(True)
        return DeliveryReport(
            (
                RecipientDeliveryResult(
                    "safe-ref",
                    "unknown",
                    error_category="smtp_protocol",
                    delivery_uncertain=True,
                    accepted_possible=True,
                    error_stage="data_final_response",
                ),
            )
        )

    server = create_server(
        tmp_path,
        site,
        0,
        env_file=".env",
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
        confirmation_sender=unknown,
    )
    port = _start(server)
    try:
        first_status, _, _ = _subscribe(port, "reader@example.com")
        second_status, _, _ = _subscribe(port, "reader@example.com")
        assert first_status == second_status == 202
        assert calls == [True]

        conn = db.connect(tmp_path / "news.db")
        assert db.subscription_by_email(conn, "reader@example.com").status == "pending"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM subscription_tokens WHERE purpose='confirm'"
            ).fetchone()[0]
            == 1
        )
        conn.close()

        output = capsys.readouterr().out
        assert (
            "public-subscription confirmation_unknown category=smtp_protocol "
            "stage=data_final_response action=verify_provider_queue_before_retry"
        ) in output
        assert "reader@example.com" not in output
    finally:
        server.shutdown()
        server.server_close()


def test_public_confirm_and_unsubscribe_http_are_uniform_idempotent_and_safe(public_server):
    root, port, sent, _ = public_server
    _subscribe(port, "reader@example.com")
    confirmation_token = sent[0][2].rsplit("/", 1)[1]
    status_rebound, _page, _ = _request(
        port,
        "GET",
        f"/subscribe/confirm/{confirmation_token}",
        content_type=None,
        headers={"Host": "rebind.example"},
        decode_json=False,
    )
    assert status_rebound == 200
    conn = db.connect(root / "news.db")
    assert db.subscription_by_email(conn, "reader@example.com").status == "pending"
    conn.close()
    status, valid_html, _ = _request(
        port,
        "GET",
        f"/subscribe/confirm/{confirmation_token}",
        content_type=None,
        headers={"Host": "news.example.com"},
        decode_json=False,
    )
    status_bad, bad_html, _ = _request(
        port,
        "GET",
        "/subscribe/confirm/tampered-token-value",
        content_type=None,
        headers={"Host": "news.example.com"},
        decode_json=False,
    )
    assert status == status_bad == 200
    assert valid_html == bad_html

    conn = db.connect(root / "news.db")
    assert db.subscription_by_email(conn, "reader@example.com").status == "active"
    unsubscribe = subscriptions.prepare_unsubscribe(
        conn,
        "reader@example.com",
        "https://news.example.com",
        dt.datetime.now(dt.UTC),
        token_factory=lambda: "unsubscribe-token-with-at-least-thirty-two-bytes-http",
    )
    token = unsubscribe.url.rsplit("/", 1)[1]
    conn.close()

    status, page, _ = _request(
        port,
        "GET",
        f"/unsubscribe/{token}",
        content_type=None,
        headers={"Host": "news.example.com"},
        decode_json=False,
    )
    assert status == 200
    conn = db.connect(root / "news.db")
    assert db.subscription_by_email(conn, "reader@example.com").status == "active"
    conn.close()
    body = b"List-Unsubscribe=One-Click"
    for _ in range(2):
        status, result, _ = _request(
            port,
            "POST",
            f"/unsubscribe/{token}",
            body,
            content_type="application/x-www-form-urlencoded",
            headers={"Host": "news.example.com"},
            decode_json=False,
        )
        assert status == 200 and "reader@example.com" not in result
    assert status == 200
    assert "reader@example.com" not in page
    conn = db.connect(root / "news.db")
    assert db.subscription_by_email(conn, "reader@example.com").status == "unsubscribed"
    conn.close()


def test_public_pages_and_logs_never_expose_injected_markup_or_token(public_server, capsys):
    _, port, _, _ = public_server
    raw_token = "%3Cscript%3Ealert(1)%3C/script%3E"
    status, page, _ = _request(
        port,
        "GET",
        f"/unsubscribe/{raw_token}",
        content_type=None,
        headers={"Host": "news.example.com"},
        decode_json=False,
    )
    assert status == 200
    assert "<script>" not in page
    status, _, _ = _request(
        port,
        "POST",
        "/unsubscribe/not-a-token",
        b"wrong=body",
        content_type="application/x-www-form-urlencoded",
        headers={"Host": "news.example.com"},
        decode_json=False,
    )
    assert status == 400
    captured = capsys.readouterr()
    assert raw_token not in captured.out + captured.err
    assert "alert(1)" not in captured.out + captured.err


def test_static_frontend_compose_nginx_and_cli_wiring():
    root = Path(__file__).resolve().parents[2]
    paths = {
        name: (root / path).read_text(encoding="utf-8")
        for name, path in {
            "home": "src/news_digest/templates/home.html",
            "js": "src/news_digest/static/app.js",
            "compose": "deploy/compose.yaml",
            "nginx": "deploy/nginx/news.conf",
            "cli": "src/news_digest/cli.py",
        }.items()
    }
    assert "data-subscribe-form" not in paths["home"]
    assert "会员订阅与每日简报" in paths["home"]
    assert 'href="/subscribe"' in paths["home"]
    assert 'fetch("/subscribe/api/' in paths["js"]
    assert "http://" not in paths["js"] and "https://" not in paths["js"]
    admin = paths["compose"].split("  admin:", 1)[1].split("\nvolumes:", 1)[0]
    assert "- news-data:/data" in admin
    assert "- news-site:/site:ro" in admin
    assert "/srv/news-digest/config:/config" in admin
    for route in ("/subscribe/api/", "/subscribe/confirm/", "/unsubscribe/"):
        assert f"location {route}" in paths["nginx"]
    assert paths["nginx"].count("access_log off;") >= 2
    assert "proxy_pass http://127.0.0.1:8619" in paths["nginx"]
    assert "zone=news_subscribe_ratelimit" in paths["nginx"]
    assert "frame-ancestors 'none'" in paths["nginx"]
    assert 'Path("/data/news.db")' in paths["cli"]
    assert 'load_env_file(config_dir / ".env")' in paths["cli"]
    assert "db_path=database" in paths["cli"] and "site_url=site_url" in paths["cli"]


def test_deployment_schedule_artifacts_are_consistent():
    root = Path(__file__).resolve().parents[2]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    bootstrap = (root / "deploy/bootstrap.sh").read_text(encoding="utf-8")
    compose = (root / "deploy/compose.yaml").read_text(encoding="utf-8")
    dockerfile = (root / "deploy/docker/Dockerfile.worker").read_text(encoding="utf-8")
    service = (root / "deploy/systemd/news-digest.service").read_text(encoding="utf-8")
    timer = (root / "deploy/systemd/news-digest.timer").read_text(encoding="utf-8")

    assert "NEWS_TIMEZONE=Asia/Shanghai" in env_example
    assert "NEWS_TIMEZONE=Asia/Shanghai" in bootstrap
    assert 'count == 1 && value == "Asia/Shanghai"' in bootstrap
    assert bootstrap.index('count == 1 && value == "Asia/Shanghai"') < bootstrap.index(
        'install_file "${SRC_DIR}/news-digest.timer"'
    )
    assert "OnCalendar=*-*-* 08:00:00 Asia/Shanghai" in timer
    assert "Type=oneshot" in service
    assert "docker compose -f /srv/news-digest/compose.yaml run --rm worker" in service
    worker = compose.split("  worker:", 1)[1].split("  web:", 1)[0]
    assert "command:" not in worker
    assert "抓取→翻译→构建→投递" in dockerfile
    assert 'CMD ["run", "--yes"]' in dockerfile


def test_mail_settings_get_is_redacted_and_save_does_not_connect(mail_admin_server):
    root, port, smtp_calls, delivery_calls, _ = mail_admin_server
    auth = _login(port)
    status, data, _ = _request(
        port, "GET", "/admin/api/mail/settings", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert data["password_set"] is True
    assert "password" not in data
    assert "saved-secret" not in json.dumps(data)
    assert data["timezone"] == "Asia/Shanghai" and data["schedule_time"] == "08:00"
    assert data["current_release"]["sources"] == ["BBC News", "NPR"]
    assert data["current_release"]["main_items"] == [
        {"source": "BBC News", "has_zh": True}
    ]
    assert data["preview_metadata"]["main_count"] == 1

    status, data = _post(port, "/admin/api/mail/settings", _mail_form(), auth)
    assert status == 200 and data["password_set"] is True
    values = read_env(root / ".env")
    assert values["SMTP_HOST"] == "smtp.unsaved.example.com"
    assert values["SMTP_PASSWORD"] == "saved-secret"
    site_values = read_env(root / "site-config" / ".env")
    assert site_values["SMTP_HOST"] == "smtp.unsaved.example.com"
    assert site_values["SMTP_PASSWORD"] == "saved-secret"
    assert "SMTP_RECIPIENTS" not in site_values
    assert "TRANSLATION_API_KEY" not in site_values
    assert smtp_calls == [] and delivery_calls == []


def test_legacy_release_keeps_smtp_settings_and_connection_test_available(tmp_path):
    site_root = tmp_path / "site-root"
    release = build_editions([_admin_edition()], BuildConfig(site_root, "http://unused"))
    (release / "release.json").unlink()
    (tmp_path / ".env").write_text(
        "NEWS_SITE_URL=https://news.example.com\nNEWS_TIMEZONE=Asia/Shanghai\n"
        "EMAIL_DELIVERY_ENABLED=false\nSMTP_HOST=smtp.example.com\nSMTP_PORT=587\n"
        "SMTP_USERNAME=operator\nSMTP_PASSWORD=saved-secret\nSMTP_SECURITY=starttls\n"
        "SMTP_FROM=news@example.com\nSMTP_RECIPIENTS=saved@example.com\n"
        "EMAIL_MAINS_ENABLED=true\nEMAIL_BRIEFS_ENABLED=true\n"
        "EMAIL_MAIN_LIMIT=1\nEMAIL_BRIEF_LIMIT=1\n",
        encoding="utf-8",
    )
    smtp_calls = []
    delivery_calls = []

    def smtp_test(config, resolver):
        smtp_calls.append(config)

    def delivery(mode, **kwargs):
        delivery_calls.append((mode, kwargs))
        return DeliveryServiceReport(
            run_id="unexpected",
            release_name="unexpected",
            edition_date="2026-07-27",
            mode=mode,
            status="sent",
            total_count=1,
            sent_count=1,
            failed_count=0,
            unknown_count=0,
            skipped_count=0,
            degraded=False,
            archive_status="not_requested",
        )

    server = create_server(
        tmp_path,
        site_root,
        0,
        env_file=".env",
        profiles_file="providers.json",
        db_path=tmp_path / "news.db",
        site_url="https://news.example.com",
        output_root=site_root,
        resolver=PUBLIC_IPS,
        smtp_test_callback=smtp_test,
        delivery_callback=delivery,
        sensitive_limit=20,
    )
    port = _start(server)
    try:
        subscription_id = _add_admin_subscription(tmp_path)
        status, providers, _ = _request(
            port, "GET", "/admin/api/providers", content_type=None
        )
        assert status == 200
        auth = ("", providers["csrf_token"])

        status, settings, _ = _request(
            port, "GET", "/admin/api/mail/settings", content_type=None
        )
        assert status == 200
        assert settings["current_release"] is None
        assert settings["preview_validation"]["category"] == "release"
        assert "manifest" in settings["preview_validation"]["message"]

        form = _mail_form(main_limit=99, brief_limit=77)
        status, data = _post(port, "/admin/api/mail/settings", form, auth)
        assert status == 200 and data["ok"] is True
        assert read_env(tmp_path / ".env")["EMAIL_MAIN_LIMIT"] == "99"

        status, data = _post(
            port,
            "/admin/api/mail/test-connection",
            {**form, "language": "ignored-by-smtp-only-test", "main_limit": -1},
            auth,
        )
        assert status == 200 and data["ok"] is True
        assert len(smtp_calls) == 1

        status, data = _post(port, "/admin/api/mail/preview", {}, auth)
        assert status == 400 and data["category"] == "release"
        assert "manifest" in data["error"]

        status, data = _post(
            port,
            "/admin/api/mail/test-message",
            {
                "confirm": True,
                "idempotency_key": "legacy-release-test-0001",
                "subscription_id": subscription_id,
                "settings": form,
            },
            auth,
        )
        assert status == 400 and data["category"] == "release"
        assert "manifest" in data["error"]
        assert len(smtp_calls) == 1 and delivery_calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_cross_edition_invalid_preview_keeps_settings_and_delivery_status_editable(
    mail_admin_server,
):
    root, port, _, _, _ = mail_admin_server
    env_path = root / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "EMAIL_SOURCE_FILTERS=", "EMAIL_SOURCE_FILTERS=Reuters"
        ),
        encoding="utf-8",
    )
    conn = db.connect(root / "news.db")
    db.start_delivery_run(
        conn,
        "run-historical",
        "2026-07-26",
        "auto",
        "2026-07-26T00:00:00+00:00",
        1,
        False,
    )
    db.finish_delivery_run(
        conn,
        "run-historical",
        "completed",
        "2026-07-26T00:01:00+00:00",
        sent_count=1,
    )
    conn.close()
    auth = _login(port)

    status, settings, _ = _request(
        port, "GET", "/admin/api/mail/settings", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert settings["source_filters"] == ["Reuters"]
    assert settings["preview_metadata"] is None
    assert settings["preview_validation"]["valid"] is False
    assert settings["preview_validation"]["category"] == "configuration"
    assert settings["preview_validation"]["message"]

    status, delivery, _ = _request(
        port, "GET", "/admin/api/delivery", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert delivery["latest_run"]["run_id"] == "run-historical"
    assert delivery["current_release"]["edition_date"] == "2026-07-27"
    assert delivery["current_preview"] is None
    assert delivery["preview_validation"]["valid"] is False
    assert delivery["states"] == []


def test_unsaved_connection_uses_form_password_fallback_without_persistence(mail_admin_server):
    root, port, smtp_calls, _, _ = mail_admin_server
    auth = _login(port)
    before = (root / ".env").read_bytes()
    status, data = _post(
        port,
        "/admin/api/mail/test-connection",
        _mail_form(port=65535),
        auth,
    )
    assert status == 200 and data["category"] == "success"
    assert len(smtp_calls) == 1
    assert smtp_calls[0].host == "smtp.unsaved.example.com"
    assert smtp_calls[0].port == 65535
    assert smtp_calls[0].password == "saved-secret"
    assert (root / ".env").read_bytes() == before


def test_smtp_test_mutex_and_post_guards(mail_admin_server):
    _, port, smtp_calls, _, server = mail_admin_server
    auth = _login(port)
    assert server.smtp_lock.acquire(blocking=False)
    try:
        status, data = _post(port, "/admin/api/mail/test-connection", _mail_form(), auth)
    finally:
        server.smtp_lock.release()
    assert status == 409 and data["category"] == "busy"
    assert smtp_calls == []

    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/mail/settings",
        _mail_form(),
        cookie=auth[0],
        origin="https://evil.test",
        csrf=auth[1],
    )
    assert status == 403
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/mail/settings",
        _mail_form(),
        cookie=auth[0],
        csrf=auth[1],
        content_type="text/plain",
    )
    assert status == 415
    status, _, _ = _request(
        port,
        "POST",
        "/admin/api/mail/settings",
        _mail_form(),
        cookie=auth[0],
        content_type="application/json",
    )
    assert status == 403


def test_test_message_shares_the_smtp_mutex(mail_admin_server):
    root, port, _, calls, server = mail_admin_server
    auth = _login(port)
    subscription_id = _add_admin_subscription(root)
    assert server.smtp_lock.acquire(blocking=False)
    try:
        status, data = _post(
            port,
            "/admin/api/mail/test-message",
            {
                "confirm": True,
                "idempotency_key": "smtp-shared-lock-0001",
                "subscription_id": subscription_id,
                "settings": _mail_form(),
            },
            auth,
        )
    finally:
        server.smtp_lock.release()
    assert status == 409 and data["category"] == "busy"
    assert calls == []


def test_test_attempt_fingerprint_tracks_the_html_notice_transport():
    smtp = SimpleNamespace(sender="news@example.com", recipients=("reader@example.com",))
    content = SimpleNamespace(layout="digest")
    release = SimpleNamespace(release_name="2026-07-27-01", release_date="2026-07-27")
    linked = SimpleNamespace(subject="subject", text="text", html='<a href="https://example.com">x</a>')
    link_free = SimpleNamespace(subject="subject", text="text", html="<p>x</p>")
    text_changed = SimpleNamespace(subject="subject", text="changed", html="<p>x</p>")

    first = PreviewHandler._test_attempt_fingerprint(smtp, content, release, linked)
    second = PreviewHandler._test_attempt_fingerprint(smtp, content, release, link_free)
    third = PreviewHandler._test_attempt_fingerprint(smtp, content, release, text_changed)

    assert first != second
    assert second == third
    assert len(first) == len(second) == len(third) == 64
    assert "example.com" not in first + second + third


def test_manual_fingerprint_tracks_only_the_html_notice_transport():
    release = SimpleNamespace(release_name="2026-07-27-01", edition_sha256="a" * 64)
    linked = SimpleNamespace(subject="subject", text="text", html="<p>first</p>")
    html_changed = SimpleNamespace(subject="subject", text="text", html="<p>second</p>")
    text_changed = SimpleNamespace(subject="subject", text="changed", html="<p>second</p>")

    first = SimpleNamespace(release=release, rendered=linked, recipient_hashes=("r1",))
    second = SimpleNamespace(release=release, rendered=html_changed, recipient_hashes=("r1",))
    third = SimpleNamespace(release=release, rendered=text_changed, recipient_hashes=("r1",))

    first_hash = PreviewHandler._manual_fingerprint(None, first)
    second_hash = PreviewHandler._manual_fingerprint(None, second)
    third_hash = PreviewHandler._manual_fingerprint(None, third)

    assert first_hash != second_hash
    assert second_hash == third_hash
    assert len(first_hash) == len(second_hash) == len(third_hash) == 64


def test_test_message_requires_an_explicit_subscription_id(mail_admin_server):
    _, port, _, calls, server = mail_admin_server
    auth = _login(port)
    status, data = _post(
        port,
        "/admin/api/mail/test-message",
        {
            "confirm": True,
            "idempotency_key": "test-message-missing-recipient-0001",
            "settings": _mail_form(),
        },
        auth,
    )
    assert status == 400 and data["category"] == "configuration"
    assert calls == [] and server.smtp_smoke_calls == []


def test_test_message_uses_one_selected_subscription_is_idempotent_and_not_formal(
    mail_admin_server,
):
    root, port, _, calls, _ = mail_admin_server
    auth = _login(port)
    selected_id = _add_admin_subscription(root, "selected@example.com")
    _add_admin_subscription(root, "not-selected@example.com")
    body = {
        "confirm": True,
        "idempotency_key": "test-message-key-0001",
        "subscription_id": selected_id,
        "settings": _mail_form(recipients=["attacker@example.com"]),
    }
    status, first = _post(port, "/admin/api/mail/test-message", body, auth)
    status_again, second = _post(port, "/admin/api/mail/test-message", body, auth)
    assert status == status_again == 200
    assert first["duplicate"] is False and second["duplicate"] is True
    assert len(calls) == 1 and calls[0][0] == "test"
    smtp = calls[0][1]["smtp_config"]
    assert smtp.recipients == ("selected@example.com",)
    content = calls[0][1]["content_config"]
    assert content.main_limit == 1 and content.brief_limit == 1
    assert "attacker@example.com" not in repr(calls)
    assert "not-selected@example.com" not in repr(calls)
    conn = db.connect(root / "news.db")
    assert conn.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    conn.close()

    status, data = _post(
        port,
        "/admin/api/mail/test-message",
        {**body, "confirm": False, "idempotency_key": "test-message-key-0002"},
        auth,
    )
    assert status == 409 and data["category"] == "confirmation"


def test_smtp_smoke_uses_one_selected_subscription_and_has_an_independent_latch(
    mail_admin_server,
):
    root, port, _, calls, server = mail_admin_server
    auth = _login(port)
    selected_id = _add_admin_subscription(root, "smoke-selected@example.com")
    _add_admin_subscription(root, "smoke-not-selected@example.com")

    def unknown_delivery(mode, **kwargs):
        calls.append((mode, kwargs))
        return DeliveryServiceReport(
            run_id=None,
            release_name="2026-07-27-01",
            edition_date="2026-07-27",
            mode="test",
            status="failed",
            total_count=1,
            sent_count=0,
            failed_count=0,
            unknown_count=1,
            skipped_count=0,
            degraded=False,
            archive_status="not_requested",
            error_category="smtp_protocol",
            error_stage="data_final_response",
        )

    server.delivery_callback = unknown_delivery
    digest_body = {
        "confirm": True,
        "idempotency_key": "digest-unknown-before-smoke-0001",
        "subscription_id": selected_id,
        "settings": _mail_form(),
    }
    status, digest = _post(port, "/admin/api/mail/test-message", digest_body, auth)
    assert status == 200 and digest["unknown_count"] == 1

    smoke_body = {
        "confirm": True,
        "idempotency_key": "smtp-smoke-message-key-0001",
        "subscription_id": selected_id,
        "settings": _mail_form(recipients=["attacker@example.com"]),
        "kind": "smtp_smoke",
    }
    status, first = _post(port, "/admin/api/mail/test-message", smoke_body, auth)
    status_again, duplicate = _post(port, "/admin/api/mail/test-message", smoke_body, auth)

    assert status == status_again == 200
    assert first["sent_count"] == 1 and first["unknown_count"] == 0
    assert first["test_kind"] == "smtp_smoke"
    assert duplicate["duplicate"] is True
    assert len(server.smtp_smoke_calls) == 1
    assert server.smtp_smoke_calls[0].recipients == ("smoke-selected@example.com",)
    assert "smoke-not-selected@example.com" not in repr(server.smtp_smoke_calls)
    conn = db.connect(root / "news.db")
    assert conn.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0
    conn.close()


def test_test_message_unknown_returns_safe_actionable_diagnostics(mail_admin_server):
    root, port, _, calls, server = mail_admin_server

    def unknown_delivery(mode, **kwargs):
        calls.append((mode, kwargs))
        return DeliveryServiceReport(
            run_id=None,
            release_name="2026-07-27-01",
            edition_date="2026-07-27",
            mode="test",
            status="failed",
            total_count=1,
            sent_count=0,
            failed_count=0,
            unknown_count=1,
            skipped_count=0,
            degraded=False,
            archive_status="not_requested",
            error_category="timeout",
            message="redacted",
            error_stage="data_final_response",
        )

    server.delivery_callback = unknown_delivery
    auth = _login(port)
    subscription_id = _add_admin_subscription(root)
    body = {
        "confirm": True,
        "idempotency_key": "test-message-unknown-0001",
        "subscription_id": subscription_id,
        "settings": _mail_form(),
    }
    status, first = _post(port, "/admin/api/mail/test-message", body, auth)
    status_again, duplicate = _post(port, "/admin/api/mail/test-message", body, auth)

    assert status == status_again == 200
    assert first["ok"] is False
    assert (first["sent_count"], first["failed_count"], first["unknown_count"]) == (0, 0, 1)
    assert first["error_stage"] == "data_final_response"
    assert first["error_category"] == "timeout"
    assert first["retry_allowed"] is False
    assert "不要立即重发" in first["next_action"]
    assert "SMTP 服务端投递/队列日志" in first["next_action"]
    assert "持久记录" in first["idempotency_warning"]
    assert duplicate["duplicate"] is True
    assert duplicate["unknown_count"] == 1
    assert duplicate["error_stage"] == "data_final_response"
    assert len(calls) == 1
    serialized = json.dumps(first, ensure_ascii=False)
    assert "saved@example.com" not in serialized
    assert "saved-secret" not in serialized

    status_new, blocked = _post(
        port,
        "/admin/api/mail/test-message",
        {**body, "idempotency_key": "test-message-unknown-0002"},
        auth,
    )
    assert status_new == 409
    assert blocked["category"] == "unknown_pending"
    assert blocked["retry_allowed"] is False
    assert "SMTP 服务端投递/队列日志" in blocked["next_action"]
    assert len(calls) == 1

    restarted = create_server(
        root,
        root,
        0,
        env_file=".env",
        serve_static=False,
        htpasswd_file=root / "htpasswd-admin",
        db_path=root / "news.db",
        site_url="https://news.example.com",
        output_root=root / "site-root",
        timezone="Asia/Shanghai",
        resolver=PUBLIC_IPS,
        delivery_callback=unknown_delivery,
        sensitive_limit=20,
    )
    restarted_port = _start(restarted)
    try:
        restarted_auth = _login(restarted_port)
        status_restart, after_restart = _post(
            restarted_port,
            "/admin/api/mail/test-message",
            {**body, "idempotency_key": "test-message-unknown-0003"},
            restarted_auth,
        )
        assert status_restart == 409
        assert after_restart["category"] == "unknown_pending"
        assert after_restart["retry_allowed"] is False
        assert len(calls) == 1
    finally:
        restarted.shutdown()
        restarted.server_close()
    for token in (
        "data.error_stage",
        "data.error_category",
        "data.next_action",
        "发生阶段",
        "错误分类",
        "下一步",
    ):
        assert token in ADMIN_HTML


@pytest.mark.parametrize(
    ("stage", "guidance"),
    [
        ("connect", "网络路由"),
        ("tls", "TLS 证书"),
        ("auth", "授权码"),
        ("mail", "MAIL FROM"),
        ("rcpt", "RCPT TO"),
        ("data_command", "DATA 命令"),
    ],
)
def test_test_message_failed_stage_has_actionable_guidance(mail_admin_server, stage, guidance):
    root, port, _, _, server = mail_admin_server

    def failed_delivery(mode, **kwargs):
        return DeliveryServiceReport(
            run_id=None,
            release_name="2026-07-27-01",
            edition_date="2026-07-27",
            mode="test",
            status="failed",
            total_count=1,
            sent_count=0,
            failed_count=1,
            unknown_count=0,
            skipped_count=0,
            degraded=False,
            archive_status="not_requested",
            error_category="smtp_protocol",
            error_stage=stage,
        )

    server.delivery_callback = failed_delivery
    auth = _login(port)
    subscription_id = _add_admin_subscription(root)
    status, data = _post(
        port,
        "/admin/api/mail/test-message",
        {
            "confirm": True,
            "idempotency_key": f"test-message-failed-{stage}",
            "subscription_id": subscription_id,
            "settings": _mail_form(),
        },
        auth,
    )

    assert status == 200
    assert data["error_stage"] == stage
    assert data["error_category"] == "smtp_protocol"
    assert data["retry_allowed"] is True
    assert guidance in data["next_action"]


def test_preview_reuses_saved_builder_and_has_no_smtp_or_state(mail_admin_server):
    root, port, smtp_calls, delivery_calls, _ = mail_admin_server
    auth = _login(port)
    status, data = _post(port, "/admin/api/mail/preview", {}, auth)
    assert status == 200
    assert data["subject"] == "Cheapcoding News 已更新｜2026-07-27"
    assert "Admin published story" in data["text"]
    assert "管理端已发布文章" in data["html"]
    assert data["main_count"] == data["brief_count"] == 1
    assert smtp_calls == [] and delivery_calls == []
    conn = db.connect(root / "news.db")
    assert db.latest_delivery_run(conn) is None
    conn.close()


def test_mail_save_rejects_a_filter_that_makes_current_release_empty(mail_admin_server):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    before = (root / ".env").read_bytes()
    status, data = _post(
        port,
        "/admin/api/mail/settings",
        _mail_form(source_filters=["NPR"], briefs_enabled=False, main_limit=1),
        auth,
    )
    assert status == 400
    assert "empty" in data["error"]
    assert (root / ".env").read_bytes() == before


def test_admin_subscription_list_add_disable_and_reactivate_are_unified(mail_admin_server):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, data = _post(
        port, "/admin/api/subscriptions/add", {"email": "internal@example.com"}, auth
    )
    assert status == 200 and data == {"ok": True}
    status, listing, _ = _request(
        port, "GET", "/admin/api/subscriptions", cookie=auth[0], content_type=None
    )
    assert status == 200
    item = next(
        item for item in listing["items"] if item["email_masked"] == "i***@e***.com"
    )
    assert item["email_masked"] == "i***@e***.com"
    assert "source" not in item
    assert "internal@example.com" not in json.dumps(listing)
    status, _ = _post(
        port,
        "/admin/api/subscriptions/disable",
        {"id": item["id"], "confirm": True},
        auth,
    )
    assert status == 200

    conn = db.connect(root / "news.db")
    token = "public-confirm-token-that-is-long-enough-000000"
    subscriptions.submit_subscription(
        conn,
        "public@example.com",
        "https://news.example.com",
        dt.datetime.now(dt.UTC),
        token_factory=lambda: token,
    )
    subscriptions.confirm_subscription(conn, token, dt.datetime.now(dt.UTC))
    unsubscribe = subscriptions.prepare_unsubscribe(
        conn,
        "public@example.com",
        "https://news.example.com",
        dt.datetime.now(dt.UTC),
        token_factory=lambda: "public-unsubscribe-token-that-is-long-enough-0000",
    )
    subscriptions.unsubscribe_one_click(
        conn, unsubscribe.url.rsplit("/", 1)[1], dt.datetime.now(dt.UTC)
    )
    conn.close()
    status, data = _post(
        port, "/admin/api/subscriptions/add", {"email": "public@example.com"}, auth
    )
    assert status == 200 and data == {"ok": True}
    conn = db.connect(root / "news.db")
    state = db.subscription_by_email(conn, "public@example.com")
    assert state is not None and state.status == "active" and state.source == "public"
    conn.close()


def test_admin_subscription_add_enable_disable_delete_lifecycle(mail_admin_server):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    status, _ = _post(
        port,
        "/admin/api/subscriptions/add",
        {"email": "lifecycle@example.com"},
        auth,
    )
    assert status == 200

    conn = db.connect(root / "news.db")
    state = db.subscription_by_email(conn, "lifecycle@example.com")
    assert state is not None and state.status == "active"
    subscription_id = state.id
    conn.close()

    for action, expected_status in (
        ("disable", "disabled"),
        ("enable", "active"),
        ("disable", "disabled"),
    ):
        status, data = _post(
            port,
            f"/admin/api/subscriptions/{action}",
            {"id": subscription_id, "confirm": True},
            auth,
        )
        assert status == 200 and data["ok"] is True
        conn = db.connect(root / "news.db")
        assert db.subscription_by_email(conn, "lifecycle@example.com").status == expected_status
        conn.close()

    status, data = _post(
        port,
        "/admin/api/subscriptions/delete",
        {"id": subscription_id, "confirm": True},
        auth,
    )
    assert status == 200 and data["ok"] is True
    conn = db.connect(root / "news.db")
    assert db.subscription_by_email(conn, "lifecycle@example.com") is None
    conn.close()
    status, listing, _ = _request(
        port, "GET", "/admin/api/subscriptions", cookie=auth[0], content_type=None
    )
    assert status == 200
    assert all(item["id"] != subscription_id for item in listing["items"])


def test_public_and_admin_add_share_one_case_insensitive_subscription_record(
    mail_admin_server,
):
    root, port, _, _, _ = mail_admin_server
    auth = _login(port)
    conn = db.connect(root / "news.db")
    token = "shared-public-first-confirm-token-that-is-long-enough"
    subscriptions.submit_subscription(
        conn,
        "Shared-Public@Example.com",
        "https://news.example.com",
        dt.datetime.now(dt.UTC),
        token_factory=lambda: token,
    )
    subscriptions.confirm_subscription(conn, token, dt.datetime.now(dt.UTC))
    public_state = db.subscription_by_email(conn, "shared-public@example.com")
    assert public_state is not None and public_state.source == "public"
    conn.close()

    status, data = _post(
        port,
        "/admin/api/subscriptions/add",
        {"email": "shared-public@example.com"},
        auth,
    )
    assert status == 200 and data["ok"] is True
    conn = db.connect(root / "news.db")
    merged = db.subscription_by_email(conn, "SHARED-PUBLIC@example.com")
    assert merged is not None and merged.id == public_state.id
    assert merged.source == "public" and merged.status == "active"
    assert conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE email_key = ?",
        (db.delivery_recipient_key("shared-public@example.com"),),
    ).fetchone()[0] == 1

    assert subscriptions.add_admin_test_recipient(
        conn,
        "shared-admin@example.com",
        dt.datetime.now(dt.UTC),
    )
    admin_state = db.subscription_by_email(conn, "shared-admin@example.com")
    assert admin_state is not None
    submission = subscriptions.submit_subscription(
        conn,
        "SHARED-ADMIN@EXAMPLE.COM",
        "https://news.example.com",
        dt.datetime.now(dt.UTC),
        token_factory=lambda: "shared-admin-confirm-token-that-must-not-be-used",
    )
    assert not submission.should_send_confirmation
    merged_admin = db.subscription_by_email(conn, "shared-admin@example.com")
    assert merged_admin is not None and merged_admin.id == admin_state.id
    assert conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE email_key = ?",
        (db.delivery_recipient_key("shared-admin@example.com"),),
    ).fetchone()[0] == 1
    conn.close()


def test_delivery_status_retries_and_manual_preview_gate(mail_admin_server):
    root, port, _, calls, _ = mail_admin_server
    auth = _login(port)
    conn = db.connect(root / "news.db")
    db.start_delivery_run(
        conn,
        "run-status-1",
        "2026-07-27",
        "auto",
        "2026-07-27T00:00:00+00:00",
        3,
        True,
    )
    db.finish_delivery_run(
        conn,
        "run-status-1",
        "partial",
        "2026-07-27T00:01:00+00:00",
        sent_count=1,
        failed_count=1,
        unknown_count=1,
        error_category="partial_refusal",
    )
    conn.close()
    status, data, _ = _request(
        port, "GET", "/admin/api/delivery", cookie=auth[0], content_type=None
    )
    assert status == 200 and data["current_preview"]["edition_date"] == "2026-07-27"
    assert data["timezone"] == "Asia/Shanghai" and data["schedule_time"] == "08:00"
    assert data["next_schedule"]
    assert data["latest_run"] == {
        "run_id": "run-status-1",
        "edition_date": "2026-07-27",
        "mode": "auto",
        "status": "partial",
        "started_at": "2026-07-27T00:00:00+00:00",
        "finished_at": "2026-07-27T00:01:00+00:00",
        "total_count": 3,
        "sent_count": 1,
        "failed_count": 1,
        "unknown_count": 1,
        "degraded": True,
        "error_category": "partial_refusal",
    }

    status, data = _post(
        port,
        "/admin/api/delivery/retry-failed",
        {"confirm": True, "edition": "2026-07-27"},
        auth,
    )
    assert status == 200 and calls[-1][0] == "retry_failed"
    status, data = _post(
        port,
        "/admin/api/delivery/retry-unknown",
        {"confirm": True, "confirm_duplicate_risk": False, "edition": "2026-07-27"},
        auth,
    )
    assert status == 409 and data["category"] == "confirmation"
    status, _ = _post(
        port,
        "/admin/api/delivery/retry-unknown",
        {"confirm": True, "confirm_duplicate_risk": True, "edition": "2026-07-27"},
        auth,
    )
    assert status == 200 and calls[-1][0] == "retry_unknown"
    assert calls[-1][1]["confirm_unknown"] is True

    status, data = _post(
        port,
        "/admin/api/delivery/manual",
        {
            "edition": "2026-07-27",
            "preview_token": "missing",
            "fingerprint": "missing",
            "confirm": True,
        },
        auth,
    )
    assert status == 409
    status, preview = _post(
        port, "/admin/api/delivery/manual-preview", {"edition": "2026-07-27"}, auth
    )
    assert status == 200 and preview["preview_token"] and preview["fingerprint"]
    env = read_env(root / ".env")
    env["EMAIL_LANGUAGE"] = "en"
    (root / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in env.items()) + "\n",
        encoding="utf-8",
    )
    status, data = _post(
        port,
        "/admin/api/delivery/manual",
        {
            "edition": "2026-07-27",
            "preview_token": preview["preview_token"],
            "fingerprint": preview["fingerprint"],
            "confirm": True,
        },
        auth,
    )
    assert status == 409


def test_manual_preview_ignores_legacy_recipient_changes_after_import(mail_admin_server):
    root, port, _, calls, _ = mail_admin_server
    auth = _login(port)
    status, preview = _post(
        port, "/admin/api/delivery/manual-preview", {"edition": "2026-07-27"}, auth
    )
    assert status == 200 and preview["recipient_count"] == 1
    env_path = root / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "SMTP_RECIPIENTS=saved@example.com",
            "SMTP_RECIPIENTS=replacement@example.com",
        ),
        encoding="utf-8",
    )

    status, data = _post(
        port,
        "/admin/api/delivery/manual",
        {
            "edition": "2026-07-27",
            "preview_token": preview["preview_token"],
            "fingerprint": preview["fingerprint"],
            "confirm": True,
        },
        auth,
    )
    assert status == 200 and data["ok"] is True
    assert len(calls) == 1 and calls[0][0] == "manual"


def test_admin_dom_never_uses_innerhtml_for_user_values_and_has_no_message_input():
    lowered = ADMIN_HTML.lower()
    assert ADMIN_HTML.startswith("<!DOCTYPE html>")
    assert "innerhtml" not in lowered
    assert ".textcontent" in lowered
    assert "replacechildren" in lowered
    assert 'id="test"' in lowered
    assert 'id="f-type"' in lowered
    assert 'id="f-stream"' in lowered
    assert 'name="message"' not in lowered
    assert 'id="message"' not in lowered
    assert 'id="m-recipients"' not in lowered
    assert "sendmailtest(event.currenttarget" in lowered
    assert "subscription_id" in ADMIN_HTML
    assert '"/admin/api/subscriptions/enable"' in ADMIN_HTML
    assert '"/admin/api/subscriptions/disable"' in ADMIN_HTML
    assert '"/admin/api/subscriptions/delete"' in ADMIN_HTML
    assert "固定发送一次" in ADMIN_HTML
    assert "恰好发送 1 次固定 Hi" in ADMIN_HTML
    assert 'id="mail-estimate"' in ADMIN_HTML
    assert "updateMailEstimate" in ADMIN_HTML
    assert "实际发送内容（HTML 更新通知）" in ADMIN_HTML
    assert "邮件 HTML 预览" in ADMIN_HTML
    assert "月刊会员基准价(元)" in ADMIN_HTML
    assert "年刊会员基准价(元)" in ADMIN_HTML
    assert "包月" not in ADMIN_HTML
    assert "包年" not in ADMIN_HTML
    assert 'id="site-monthly" type="number" min="0.11" max="100000" step="0.01"' in ADMIN_HTML
    assert 'id="site-yearly" type="number" min="0.11" max="100000" step="0.01"' in ADMIN_HTML
    assert "function yuanToCents(value)" in ADMIN_HTML
    assert "function centsToYuan(value)" in ADMIN_HTML
    assert 'monthly_price_cents: monthlyPriceCents' in ADMIN_HTML
    assert 'yearly_price_cents: yearlyPriceCents' in ADMIN_HTML
    assert '"/admin/api/site/user-admin"' in ADMIN_HTML
    assert '"/admin/api/site/user-subscription-clear"' in ADMIN_HTML
    assert "设为管理员" in ADMIN_HTML
    assert "撤销管理员" in ADMIN_HTML
    assert "增加时长" in ADMIN_HTML
    assert "清除订阅" in ADMIN_HTML
    assert "剩余时长" in ADMIN_HTML
    assert "remainingSubscriptionDays" in ADMIN_HTML
    assert 'grantPlan.value === "yearly" ? "366" : "31"' in ADMIN_HTML
    assert 'grantPlan.addEventListener("change"' in ADMIN_HTML
    assert '"/admin/api/site/code-delete"' in ADMIN_HTML
    assert 'button("删除"' in ADMIN_HTML
    assert 'data.codes.join("\\n")' in ADMIN_HTML
    assert 'data.codes.join("\\\\n")' not in ADMIN_HTML
    assert 'item.code || item.prefix + "••••"' in ADMIN_HTML
    assert "未使用卡密会在后台持续明文显示" in ADMIN_HTML
    assert '<div class="stack" id="site-management-sections">' in ADMIN_HTML
    assert '<h3>用户账号</h3>' in ADMIN_HTML
    assert '<h3>支付订单</h3>' in ADMIN_HTML
    assert "在线支付成功后由网关回调自动开通，无需人工审批" in ADMIN_HTML
    assert "确认收款并开通" not in ADMIN_HTML
    assert 'api("/admin/api/site/order-decide"' not in ADMIN_HTML
    for payment_field in (
        "item.merchant_order_no",
        "item.base_amount_cents",
        "item.amount_cents",
        "item.amount_offset_cents",
        "item.provider_trade_no",
        "item.expires_at",
        "item.last_error_code",
    ):
        assert payment_field in ADMIN_HTML
    assert "#site-users, #site-orders, #site-codes" in ADMIN_HTML
    for field_name in (
        "run.run_id",
        "run.started_at",
        "run.finished_at",
        "run.total_count",
        "run.degraded",
        "run.error_category",
        "data.next_schedule",
    ):
        assert field_name in ADMIN_HTML
    for label in ("模型接口", "邮件设置", "订阅管理", "用户与付费", "翻译状态", "投递状态"):
        assert label in ADMIN_HTML
    for action in (
        "测试连接",
        "保存更改",
        "预览邮件",
        "发送验证邮件",
        "发送测试邮件",
        "开启每日投递",
    ):
        assert action in ADMIN_HTML
    assert "prefers-reduced-motion" in lowered
    assert "prefers-reduced-motion: no-preference" in lowered
    assert "aria-selected" in lowered
    assert 'role="tablist"' in lowered
    assert lowered.count('role="tab"') == 6
    assert lowered.count('role="tabpanel"') == 6
    assert 'aria-controls="models"' in lowered
    assert 'aria-controls="mail"' in lowered
    assert 'aria-controls="subscriptions"' in lowered
    assert 'aria-controls="site"' in lowered
    assert 'aria-controls="translations"' in lowered
    assert 'aria-controls="delivery"' in lowered
    assert 'node.classname = "data-table"' in lowered
    assert "cell.dataset.label = headers[index]" in ADMIN_HTML
    assert 'node.setattribute("aria-busy", "true")' in lowered
    assert ".finally(function () { setbusy(action, false); })" in lowered
    assert 'var action = field("mail-preview")' in lowered
    assert 'data.category === "unknown_pending"' in ADMIN_HTML
    assert "未重新发送：已有 unknown 投递尚未核对" in ADMIN_HTML
    assert 'id="mail-send-smoke"' not in lowered
    assert '"smtp_smoke"' in ADMIN_HTML
    assert 'action.dataset.retryAllowed = "false"' in ADMIN_HTML
    assert "@media (max-width: 900px)" in lowered
    assert "@media (max-width: 820px)" in lowered
    assert "@media (max-width: 640px)" in lowered
    assert "@media (max-width: 520px)" in lowered
    assert "#delivery-summary strong" in lowered
    assert (
        "th { color: var(--muted); font-size: .72rem; font-weight: 600; "
        "white-space: nowrap; }"
    ) in lowered
    for label in ("标识", "收件人标识", "错误分类"):
        assert f'.data-table td[data-label="{label}"]' in ADMIN_HTML
    assert "*, *::before, *::after" in ADMIN_HTML
    assert "position: fixed" not in lowered
    for token in ("#fbfaf7", "#1c1b17", "#ae2f24", "#5c5a52", "#ddd8cc", "#f4f1e8"):
        assert token in lowered


def test_safe_error_ignores_client_disconnect_while_writing_response():
    class DisconnectedWriter:
        def write(self, body):
            raise ConnectionAbortedError

    handler = object.__new__(PreviewHandler)
    handler.wfile = DisconnectedWriter()
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: None
    handler._security_headers = lambda: None
    handler.end_headers = lambda: None

    handler._safe_error(RuntimeError("redacted"), status=502)
