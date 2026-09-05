# ruff: noqa: E501
"""Loopback static preview and authenticated production Admin server."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import html
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
import zoneinfo
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from news_digest import accounts, admin_payments, payments
from news_digest.admin_email import (
    AdminEmailError,
    clear_password,
    configs_from_form,
    read_env,
    save_settings,
    settings_payload,
    smtp_config_from_form,
    validate_smtp_config_target,
)
from news_digest.admin_providers import (
    ENV_FILE,
    PROFILES_FILE,
    AdminConfigError,
    assert_recent_success,
    current_test_state,
    default_provider,
    load_profiles,
    mask_provider,
    provider_fingerprint,
    provider_from_request,
    remove_test_state,
    save_test_state,
    translation_config,
    update_profiles,
    validate_public_https_target,
    write_env_local,
)
from news_digest.config import (
    SmtpConfig,
    TranslationConfig,
    normalize_email_address,
    smtp_config_from_env,
)
from news_digest.config_io import atomic_write_bytes_unlocked, atomic_write_text, locked_path
from news_digest.delivery import subscriptions
from news_digest.delivery.delivery_service import (
    DeliveryServiceError,
    DeliveryServiceReport,
    PublishedPreview,
    deliver_published,
    email_content_config_from_env,
    preview_published,
)
from news_digest.delivery.mailer import (
    DeliveryReport,
    MailError,
    send_confirmation,
    send_test_email,
    test_connection,
)
from news_digest.delivery.publisher import resolve_published_release
from news_digest.rendering.email import render_email_preview
from news_digest.storage import db
from news_digest.translation.client import (
    ApiTranslator,
    TranslationError,
    translation_cache_identity,
)

_APR1_CHARS = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SESSION_TTL_SECONDS = 7 * 24 * 3600
_SESSION_COOKIE = "nd_admin_session"
_SITE_ADMIN_DUMMY_PASSWORD_HASH = accounts.hash_password("admin-login-timing-equalizer")

_QR_DATA_HEADERS = {
    "data:image/png;base64": b"\x89PNG\r\n\x1a\n",
    "data:image/jpeg;base64": b"\xff\xd8\xff",
    "data:image/webp;base64": b"RIFF",
}


def _validated_qr_data_url(value: object) -> str:
    qr = str(value)
    if not qr:
        return ""
    header, separator, encoded = qr.partition(",")
    signature = _QR_DATA_HEADERS.get(header.lower())
    if not separator or signature is None:
        raise ValueError("QR 仅支持 PNG、JPEG 或 WebP 数据 URL")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("QR 数据不是有效的 Base64 图片") from None
    if not payload:
        raise ValueError("QR 图片不能为空")
    if not payload.startswith(signature):
        raise ValueError("QR 图片内容与声明的 MIME 类型不一致")
    if header.lower() == "data:image/webp;base64" and (
        len(payload) < 12 or payload[8:12] != b"WEBP"
    ):
        raise ValueError("QR 图片内容与声明的 MIME 类型不一致")
    return qr
def _request_origin(value: str) -> tuple[str, str, int]:
    if not value or "\\" in value or any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("request origin is invalid")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("request origin is invalid")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("request origin is invalid") from None
    return (
        parts.scheme,
        parts.hostname.casefold(),
        port or (443 if parts.scheme == "https" else 80),
    )


def _request_host(host: str, scheme: str) -> tuple[str, str, int] | None:
    if (
        not host
        or "," in host
        or "/" in host
        or "\\" in host
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in host)
    ):
        return None
    try:
        parts = urlsplit(f"{scheme}://{host}")
        port = parts.port
    except ValueError:
        return None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    return (
        scheme,
        parts.hostname.casefold(),
        port or (443 if scheme == "https" else 80),
    )
_PUBLIC_TOKEN_LIMIT = 512
_BODY_LIMIT = 16_384
_SITE_SETTINGS_BODY_LIMIT = 20 * 1024 * 1024
_TEST_MAX_AGE_SECONDS = 24 * 3600
_PREVIEW_TEXT_LIMIT = 40_000
_PREVIEW_HTML_LIMIT = 120_000
_MANUAL_PREVIEW_TTL_SECONDS = 15 * 60
_UNKNOWN_TEST_MESSAGE_ACTION = (
    "不要立即重发；先查 SMTP 服务端投递/队列日志，确认该次 DATA 是否已接受或排队。"
)
_IDEMPOTENCY_WARNING = (
    "测试尝试已按脱敏指纹持久记录；未解决的 running/unknown 状态会阻止同一请求重发，"
    "须先人工核对 SMTP 服务端投递/队列日志。"
)
_PUBLIC_SUBSCRIPTION_DISABLED_MESSAGE = "匿名订阅入口已关闭，请登录会员账号管理每日简报"
_PUBLIC_UNSUBSCRIBE_MESSAGE = "退订请求已处理；如链接有效，后续邮件将停止。"
_AUTOMATION_EDITION_ERROR_CODES = frozenset(
    {"BUILD_FAILED", "DELIVERY_FAILED", "DELIVERY_EXPIRED", "NO_ELIGIBLE_RECIPIENTS"}
)


def _to64(value: int, length: int) -> str:
    output = []
    for _ in range(length):
        output.append(_APR1_CHARS[value & 0x3F])
        value >>= 6
    return "".join(output)


def apr1_hash(password: str, salt: str | None = None) -> str:
    """Return an Apache ``$apr1$`` password hash."""
    salt = salt or "".join(secrets.choice(_APR1_CHARS) for _ in range(8))
    password_bytes, salt_bytes = password.encode(), salt.encode()
    context = hashlib.md5(password_bytes + b"$apr1$" + salt_bytes)
    alternative = hashlib.md5(password_bytes + salt_bytes + password_bytes).digest()
    remaining = len(password_bytes)
    while remaining > 0:
        context.update(alternative[: min(16, remaining)])
        remaining -= 16
    bit = len(password_bytes)
    while bit:
        context.update(b"\0" if bit & 1 else password_bytes[:1])
        bit >>= 1
    final = context.digest()
    for round_number in range(1000):
        step = hashlib.md5()
        step.update(password_bytes if round_number & 1 else final)
        if round_number % 3:
            step.update(salt_bytes)
        if round_number % 7:
            step.update(password_bytes)
        step.update(final if round_number & 1 else password_bytes)
        final = step.digest()
    encoded = (
        _to64(final[0] << 16 | final[6] << 8 | final[12], 4)
        + _to64(final[1] << 16 | final[7] << 8 | final[13], 4)
        + _to64(final[2] << 16 | final[8] << 8 | final[14], 4)
        + _to64(final[3] << 16 | final[9] << 8 | final[15], 4)
        + _to64(final[4] << 16 | final[10] << 8 | final[5], 4)
        + _to64(final[11], 2)
    )
    return f"$apr1${salt}${encoded}"


def mask_key(key: str) -> str:
    """Legacy helper: never expose even a fragment of a secret."""
    return "已设置" if key else ""


def _session_secret(secret_file: Path) -> bytes:
    with locked_path(secret_file):
        if secret_file.is_file():
            return secret_file.read_bytes()
        secret = secrets.token_bytes(32)
        atomic_write_bytes_unlocked(secret_file, secret)
        return secret


def _sign_session(secret: bytes, username: str, expires_at: int) -> str:
    payload = f"{username}|{expires_at}"
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{digest}"


def _verify_session(secret: bytes, token: str) -> bool:
    parts = token.split("|")
    if len(parts) != 3:
        return False
    username, expires_at, digest = parts
    try:
        if int(expires_at) < time.time():
            return False
    except ValueError:
        return False
    expected = hmac.new(secret, f"{username}|{expires_at}".encode(), hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), digest)


def _csrf_token(secret: bytes, session_token: str) -> str:
    return hmac.new(secret, f"csrf|{session_token}".encode(), hashlib.sha256).hexdigest()


def verify_htpasswd(htpasswd_file: Path, username: str, password: str) -> bool:
    """Verify the first ``user:$apr1$salt$hash`` line in a password file."""
    if not htpasswd_file.is_file():
        return False
    lines = htpasswd_file.read_text(encoding="utf-8").splitlines()
    if not lines or ":" not in lines[0]:
        return False
    stored_user, stored_hash = lines[0].split(":", 1)
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[1] != "apr1":
        return False
    computed = apr1_hash(password, parts[2])
    return hmac.compare_digest(stored_user, username) and hmac.compare_digest(computed, stored_hash)


def _default_probe(config: TranslationConfig) -> str:
    translator = ApiTranslator(config)
    try:
        return translator.probe()
    finally:
        translator.close()


def _default_confirmation_sender(
    config: SmtpConfig, recipient: str, confirmation_url: str
) -> DeliveryReport:
    return send_confirmation(config, recipient, confirmation_url)


def _default_smtp_test(config: SmtpConfig, resolver) -> None:
    test_connection(config, resolver=resolver)


def _default_smtp_smoke(config: SmtpConfig, resolver) -> DeliveryReport:
    return send_test_email(config, resolver=resolver)


def _default_delivery(mode: str, **kwargs) -> DeliveryServiceReport:
    return deliver_published(mode, **kwargs)


class _AdminServer(ThreadingHTTPServer):
    def service_actions(self) -> None:
        if self.site_env_path is None or self.db_path is None:
            return
        if time.monotonic() < getattr(self, "next_config_recovery", 0):
            return
        self.next_config_recovery = time.monotonic() + 30
        from news_digest.operations import monitor
        from news_digest.site_config import recover_environment

        configuration_failed = False
        try:
            recover_environment(
                self.project_root / self.env_file,
                self.site_env_path,
                self.db_path,
                site_url=self.site_url,
            )
        except Exception:
            configuration_failed = True
        try:
            if self.output_root:
                monitor(
                    self.db_path,
                    self.output_root / "current",
                    timezone=self.timezone,
                    configuration_failed=configuration_failed,
                )
        except Exception as error:
            import logging

            logging.getLogger(__name__).warning("business_monitor error=%s", type(error).__name__)

    project_root: Path
    env_file: str
    profiles_file: str
    serve_static_files: bool
    loopback_public_subscription: bool
    htpasswd_file: Path | None
    db_path: Path | None
    translation_db_path: Path | None
    site_url: str
    admin_origin: tuple[str, str, int]
    public_origin: tuple[str, str, int]
    output_root: Path | None
    timezone: str
    public_secret: bytes
    confirmation_sender: Callable[[SmtpConfig, str, str], DeliveryReport | None]
    resolver: Callable[[str, int], Iterable[str]] | None
    probe_callback: Callable[[TranslationConfig], str]
    smtp_test_callback: Callable[[SmtpConfig, Any], None]
    smtp_smoke_callback: Callable[[SmtpConfig, Any], DeliveryReport]
    delivery_callback: Callable[..., DeliveryServiceReport]
    translation_wakeup_callback: Callable[[], None]
    clock: Callable[[], float]
    sensitive_limit: int
    sensitive_window: float
    rate_lock: threading.Lock
    rate_events: dict[tuple[str, str], deque[float]]
    probe_lock: threading.Lock
    smtp_lock: threading.Lock
    state_lock: threading.Lock
    manual_previews: dict[str, tuple[float, str, str]]
    site_env_path: Path | None


class PreviewHandler(SimpleHTTPRequestHandler):
    """Static site plus authenticated ``/admin`` HTML and JSON API."""

    server: _AdminServer

    def _sync_site_environment(self, conn=None) -> None:
        if self.server.site_env_path is None:
            return
        from news_digest.site_config import apply_environment, recover_environment

        try:
            if conn is not None:
                apply_environment(
                    self._env_path(), self.server.site_env_path, conn, site_url=self.server.site_url
                )
            elif self.server.db_path is not None:
                recover_environment(
                    self._env_path(),
                    self.server.site_env_path,
                    self.server.db_path,
                    site_url=self.server.site_url,
                )
        except (OSError, ValueError, sqlite3.Error):
            raise ValueError("源配置已保存，Site 投影尚未生效；后台将重试恢复") from None

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    @property
    def _login_required(self) -> bool:
        return self.server.htpasswd_file is not None

    def _session_token(self) -> str:
        return self._cookie(_SESSION_COOKIE)

    def _cookie(self, cookie_name: str) -> str:
        cookies = self.headers.get("Cookie", "")
        for chunk in cookies.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == cookie_name and value:
                return value
        return ""

    def _authed(self) -> bool:
        if not self._login_required:
            return True
        return self._root_session_subject() is not None or self._site_admin_user() is not None

    def _root_session_subject(self) -> str | None:
        token = self._session_token()
        if not token or self.server.htpasswd_file is None:
            return None
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        if not _verify_session(_session_secret(secret_file), token):
            return None
        return token.partition("|")[0] or None

    def _site_admin_user(self) -> db.User | None:
        token = self._session_token()
        if self.server.db_path is None or not 32 <= len(token) <= 256:
            return None
        conn = db.connect(self.server.db_path)
        try:
            user_id = db.user_session_owner(
                conn,
                token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
            user = db.user_by_id(conn, user_id) if user_id is not None else None
        finally:
            conn.close()
        if user is None or user.status != "active" or not user.is_admin:
            return None
        return user

    def _valid_csrf(self) -> bool:
        if not self._login_required:
            return True
        token = self._session_token()
        if not token:
            return False
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        expected = _csrf_token(_session_secret(secret_file), token)
        return hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), expected)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin or not self._admin_request_host_valid():
            return False
        if origin == "null":
            return (
                self.server.loopback_browser_compat
                and self.server.admin_origin[1] == "127.0.0.1"
            )
        try:
            return _request_origin(origin) == self.server.admin_origin
        except ValueError:
            return False

    def _admin_request_host_valid(self) -> bool:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return False
        if self.server.admin_origin[1] == "127.0.0.1":
            return values[0] == f"127.0.0.1:{self.server.server_port}"
        return _request_host(values[0], self.server.admin_origin[0]) == self.server.admin_origin

    def _csrf_for_response(self) -> str:
        if not self._login_required:
            return ""
        token = self._session_token()
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        return _csrf_token(_session_secret(secret_file), token)

    def _admin_actor(self) -> str:
        root_subject = self._root_session_subject()
        if root_subject:
            return root_subject[:128]
        user = self._site_admin_user()
        if user is not None:
            return f"site-user:{user.id}"
        return "local-admin"

    def _consume_sensitive_limit(self, action: str) -> bool:
        now = self.server.clock()
        key = (self._rate_client_ip(), action)
        with self.server.rate_lock:
            events = self.server.rate_events[key]
            while events and events[0] <= now - self.server.sensitive_window:
                events.popleft()
            if len(events) >= self.server.sensitive_limit:
                return False
            events.append(now)
            return True

    def _rate_client_ip(self) -> str:
        peer = self.client_address[0]
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not peer_address.is_loopback:
            return str(peer_address)
        forwarded = self.headers.get("X-Real-IP", "").strip()
        if not forwarded or "," in forwarded:
            return str(peer_address)
        try:
            forwarded_address = ipaddress.ip_address(forwarded)
        except ValueError:
            return str(peer_address)
        if not forwarded_address.is_global:
            return str(peer_address)
        return str(forwarded_address)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _html(self, page: str, status: int = 200) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._security_headers()
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if path == "/subscribe/api/csrf":
            self._issue_public_csrf()
            return
        if path.startswith("/subscribe/confirm/"):
            self._handle_public_confirm(path.removeprefix("/subscribe/confirm/"))
            return
        if path.startswith("/unsubscribe/"):
            self._handle_unsubscribe_get(path.removeprefix("/unsubscribe/"))
            return
        if path in {"/admin", "/admin/"}:
            self._html(ADMIN_HTML if self._authed() else LOGIN_HTML)
            return
        if path == "/admin/api/providers":
            self._handle_provider_list()
            return
        if path == "/admin/api/mail/settings":
            self._handle_mail_settings_get()
            return
        if path == "/admin/api/subscriptions":
            self._handle_subscriptions_get()
            return
        if path == "/admin/api/users/overview":
            self._handle_users_overview(
                parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            )
            return
        if path in {"/admin/api/payments/overview", "/admin/api/site/overview"}:
            self._handle_payments_overview()
            return
        if path == "/admin/api/operations":
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            from news_digest.operations import business_status
            from news_digest.site_config import configuration_status

            if self.server.db_path is None or self.server.output_root is None:
                self._json(503, {"error": "运行目录未配置"})
                return
            state = business_status(
                self.server.db_path,
                self.server.output_root / "current",
                timezone=self.server.timezone,
                now=dt.datetime.now(dt.UTC),
            )
            conn = db.connect(self.server.db_path)
            try:
                state["configuration"] = configuration_status(
                    self._env_path(),
                    self.server.site_env_path,
                    conn,
                )
                state["checks"]["configuration_pending"] = (
                    state["configuration"]["state"] != "applied"
                )
            finally:
                conn.close()
            self._json(200, state)
            return
        if path == "/admin/api/translations":
            self._handle_translations_get(parse_qs(urlsplit(self.path).query).get("edition", [None])[0])
            return
        if path == "/admin/api/translations/events":
            self._handle_translation_events(parse_qs(urlsplit(self.path).query).get("edition", [None])[0])
            return
        if path == "/admin/api/delivery":
            self._handle_delivery_get()
            return
        if path.startswith("/admin/api/"):
            self._json(405, {"error": "该接口不接受 GET"})
            return
        if self.server.serve_static_files:
            super().do_GET()
            return
        self._json(404, {"error": "本服务只提供 /admin/ 面板"})

    def send_head(self):
        from news_digest.static_resources import resolve_static_resource

        path = urlsplit(self.path).path
        resource = resolve_static_resource(Path(self.directory), path)
        if not self.server.serve_static_files or resource is None:
            self.send_error(404)
            return None
        self.path = resource[1]
        return super().send_head()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/subscribe/api/":
            self._handle_public_submission()
            return
        if path.startswith("/unsubscribe/"):
            self._handle_unsubscribe_post(path.removeprefix("/unsubscribe/"))
            return
        if not path.startswith("/admin/api/"):
            self._json(404, {"error": "未知接口"})
            return
        if not self._same_origin():
            self._json(403, {"error": "Origin 与 Host 必须严格同源"})
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            self._json(415, {"error": "Content-Type 必须为 application/json"})
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            self._json(411, {"error": "Content-Length 无效"})
            return
        body_limit = (
            _SITE_SETTINGS_BODY_LIMIT if path == "/admin/api/site/settings" else _BODY_LIMIT
        )
        if length < 0 or length > body_limit:
            self._json(413, {"error": "请求过大"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "非法 JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "JSON 请求体必须是对象"})
            return
        if path == "/admin/api/login":
            if not self._consume_sensitive_limit("login"):
                self._json(429, {"error": "登录尝试过于频繁，请稍后重试"})
                return
            self._handle_login(body)
            return
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        if not self._valid_csrf():
            self._json(403, {"error": "CSRF token 无效"})
            return
        if path == "/admin/api/logout":
            if self.server.db_path is not None:
                conn = db.connect(self.server.db_path)
                try:
                    db.revoke_user_session(
                        conn,
                        token_digest=hashlib.sha256(
                            self._session_token().encode("utf-8")
                        ).hexdigest(),
                        now=dt.datetime.now(dt.UTC).isoformat(),
                    )
                finally:
                    conn.close()
            self._set_session_cookie("", 0)
        elif path == "/admin/api/password":
            self._limited("password", lambda: self._handle_password(body))
        elif path == "/admin/api/providers":
            self._handle_save(body)
        elif path == "/admin/api/providers/test":
            self._limited("provider-test", lambda: self._handle_probe(body))
        elif path in {"/admin/api/providers/default", "/admin/api/activate"}:
            self._limited("provider-default", lambda: self._handle_default(body))
        elif path == "/admin/api/translation/disable":
            self._limited("translation-disable", lambda: self._handle_disable_translation(body))
        elif path == "/admin/api/mail/settings":
            self._limited("mail-settings", lambda: self._handle_mail_settings_save(body))
        elif path == "/admin/api/mail/clear-password":
            self._limited("mail-password", lambda: self._handle_mail_clear_password(body))
        elif path == "/admin/api/mail/test-connection":
            self._limited("smtp-test", lambda: self._handle_smtp_test(body))
        elif path == "/admin/api/mail/test-message":
            self._limited("smtp-test", lambda: self._handle_test_message(body))
        elif path == "/admin/api/mail/preview":
            self._limited("mail-preview", lambda: self._handle_mail_preview(body))
        elif path == "/admin/api/subscriptions/add":
            self._limited("subscription-add", lambda: self._handle_subscription_add(body))
        elif path == "/admin/api/subscriptions/disable":
            self._limited("subscription-disable", lambda: self._handle_subscription_disable(body))
        elif path == "/admin/api/subscriptions/enable":
            self._limited("subscription-enable", lambda: self._handle_subscription_enable(body))
        elif path == "/admin/api/subscriptions/delete":
            self._limited("subscription-delete", lambda: self._handle_subscription_delete(body))
        elif path == "/admin/api/site/user-status":
            self._limited("site-user-status", lambda: self._handle_site_user_status(body))
        elif path == "/admin/api/site/user-admin":
            self._limited("site-user-admin", lambda: self._handle_site_user_admin(body))
        elif path == "/admin/api/site/user-grant":
            self._limited("site-user-grant", lambda: self._handle_site_user_grant(body))
        elif path == "/admin/api/site/user-subscription-clear":
            self._limited(
                "site-user-subscription-clear",
                lambda: self._handle_site_user_subscription_clear(body),
            )
        elif path == "/admin/api/site/order-decide":
            self._limited("site-order", lambda: self._handle_site_order_decide(body))
        elif path == "/admin/api/site/codes":
            self._limited("site-codes", lambda: self._handle_site_codes_create(body))
        elif path == "/admin/api/site/code-delete":
            self._limited("site-codes", lambda: self._handle_site_code_delete(body))
        elif path == "/admin/api/site/settings":
            self._limited("site-settings", lambda: self._handle_site_settings(body))
        elif path == "/admin/api/site/payment-settings":
            self._limited(
                "site-payment-settings", lambda: self._handle_site_payment_settings(body)
            )
        elif path == "/admin/api/site/payment-clear-pkey":
            self._limited(
                "site-payment-settings", lambda: self._handle_site_payment_clear_pkey(body)
            )
        elif path == "/admin/api/site/payment-reconcile":
            self._limited(
                "site-payment-reconcile", lambda: self._handle_site_payment_reconcile(body)
            )
        elif path == "/admin/api/site/payment-case":
            self._limited("site-payment-case", lambda: self._handle_site_payment_case(body))
        elif path == "/admin/api/translations/dispatch":
            self._limited("translation-dispatch", lambda: self._handle_translation_dispatch(body))
        elif path == "/admin/api/translations/retry":
            self._limited("translation-retry", lambda: self._handle_translation_retry(body))
        elif path == "/admin/api/translations/retry-edition":
            self._limited("translation-retry", lambda: self._handle_translation_retry_edition(body))
        elif path == "/admin/api/translations/cancel":
            self._limited("translation-cancel", lambda: self._handle_translation_cancel(body))
        elif path == "/admin/api/translations/probe":
            self._limited("translation-probe", lambda: self._handle_translation_probe(body))
        elif path == "/admin/api/translations/unblock":
            self._limited("translation-unblock", lambda: self._handle_translation_unblock(body))
        elif path == "/admin/api/translations/recover":
            self._limited("translation-recover", lambda: self._handle_translation_recover(body))
        elif path == "/admin/api/delivery/retry-failed":
            self._limited("delivery", lambda: self._handle_retry_failed(body))
        elif path == "/admin/api/delivery/retry-unknown":
            self._limited("delivery", lambda: self._handle_retry_unknown(body))
        elif path == "/admin/api/delivery/manual-preview":
            self._limited("delivery-preview", lambda: self._handle_manual_preview(body))
        elif path == "/admin/api/delivery/manual":
            self._limited("delivery", lambda: self._handle_manual_delivery(body))
        else:
            self._json(404, {"error": "未知接口"})

    def _handle_provider_list(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        data = load_profiles(self.server.project_root, self.server.profiles_file)
        providers = {
            name: {
                **mask_provider(provider),
                "configuration_fingerprint": provider_fingerprint(
                    self.server.project_root, provider
                ),
                "last_test": current_test_state(self.server.project_root, provider),
            }
            for name, provider in data["providers"].items()
        }
        defaults = [name for name, item in providers.items() if item["is_default"]]
        self._json(
            200,
            {
                "providers": providers,
                "active": defaults[0] if defaults else "",
                "csrf_token": self._csrf_for_response(),
                "can_change_password": self._root_session_subject() is not None,
            },
        )

    def _public_endpoint_ready(self) -> bool:
        return self.server.db_path is not None and (
            self.server.loopback_public_subscription or bool(self.server.site_url)
        )

    def _public_request_host_valid(self) -> bool:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return False
        host = values[0]
        if self.server.loopback_public_subscription:
            return host == f"127.0.0.1:{self.server.server_port}"
        return _request_host(host, self.server.public_origin[0]) == self.server.public_origin

    def _public_submission_ready(self) -> bool:
        return False

    def _public_token(self, raw: str) -> str:
        token = unquote(raw)
        if (
            not token
            or len(token) > _PUBLIC_TOKEN_LIMIT
            or token != raw
            or "/" in token
            or "\\" in token
            or any(character.isspace() for character in token)
        ):
            return ""
        return token

    def _issue_public_csrf(self) -> None:
        self._json(404, {"error": _PUBLIC_SUBSCRIPTION_DISABLED_MESSAGE})

    def _public_length(self, maximum: int) -> tuple[int | None, str | None]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            return None, "invalid"
        if length < 0:
            return None, "invalid"
        if length > maximum:
            return None, "too_large"
        return length, None

    def _handle_public_submission(self) -> None:
        self._json(404, {"error": _PUBLIC_SUBSCRIPTION_DISABLED_MESSAGE})

    def _saved_smtp_config(self) -> SmtpConfig:
        return smtp_config_from_env(read_env(self._env_path()))

    def _env_path(self) -> Path:
        return self.server.project_root / self.server.env_file

    def _admin_ready(self) -> bool:
        if (
            self.server.db_path is None
            or self.server.output_root is None
            or not self.server.site_url
        ):
            self._json(503, {"error": "邮件 Admin 尚未完整接线", "category": "configuration"})
            return False
        return True

    def _current_release(self):
        if self.server.output_root is None:
            raise AdminEmailError("未配置发布目录")
        try:
            return resolve_published_release(self.server.output_root)
        except ValueError as error:
            raise AdminEmailError(str(error), category="release") from None

    def _published_counts(self) -> tuple[int, int, Any]:
        release = self._current_release()
        return len(release.edition.articles), len(release.edition.briefs), release

    def _schedule_payload(self) -> dict[str, str]:
        now = dt.datetime.fromtimestamp(self.server.clock(), dt.UTC)
        local_now = now.astimezone(zoneinfo.ZoneInfo(self.server.timezone))
        next_schedule = dt.datetime.combine(
            local_now.date(), dt.time(8, 0), tzinfo=local_now.tzinfo
        )
        if next_schedule <= local_now:
            next_schedule += dt.timedelta(days=1)
        return {
            "timezone": self.server.timezone,
            "schedule_time": "08:00",
            "next_schedule": next_schedule.isoformat(timespec="minutes"),
        }

    def _handle_mail_settings_get(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        if not self._admin_ready():
            return
        env = read_env(self._env_path())
        release_error = None
        try:
            main_count, brief_count, release = self._published_counts()
        except AdminEmailError as error:
            if error.category != "release":
                self._safe_error(error)
                return
            main_count = brief_count = None
            release = None
            release_error = error
        try:
            payload = settings_payload(
                env,
                published_main_count=main_count,
                published_brief_count=brief_count,
            )
            schedule = self._schedule_payload()
        except (
            AdminEmailError,
            DeliveryServiceError,
            ValueError,
            zoneinfo.ZoneInfoNotFoundError,
        ) as error:
            self._safe_error(error)
            return
        if release is None:
            preview = None
            preview_validation = self._preview_validation(release_error)
            current_release = None
        else:
            sources = sorted(
                {
                    item.source
                    for item in (*release.edition.articles, *release.edition.briefs)
                    if item.source.strip()
                },
                key=str.casefold,
            )
            current_release = {
                "name": release.release_name,
                "date": release.release_date,
                "main_count": main_count,
                "brief_count": brief_count,
                "sources": sources,
                "main_items": [
                    {
                        "source": item.source,
                        "has_zh": bool(item.title_zh.strip() and item.summary_zh.strip()),
                    }
                    for item in release.edition.articles
                ],
                "brief_items": [
                    {
                        "source": item.source,
                        "has_zh": bool(item.title_zh.strip()),
                    }
                    for item in release.edition.briefs
                ],
            }
            try:
                preview = self._preview_saved()
            except (AdminEmailError, DeliveryServiceError, ValueError) as error:
                preview = None
                preview_validation = self._preview_validation(error)
            else:
                preview_validation = self._preview_validation()
        self._json(
            200,
            {
                **payload,
                "csrf_token": self._csrf_for_response(),
                **schedule,
                "current_release": current_release,
                "preview_metadata": self._preview_metadata(preview) if preview else None,
                "preview_validation": preview_validation,
            },
        )

    def _handle_mail_settings_save(self, body: dict[str, Any]) -> None:
        if not self._admin_ready():
            return
        try:
            try:
                main_count, brief_count, release = self._published_counts()
            except AdminEmailError as error:
                if error.category != "release":
                    raise
                main_count = brief_count = None
                release = None
            if release is not None:
                _, content, _ = configs_from_form(
                    body,
                    read_env(self._env_path()),
                    published_main_count=main_count,
                    published_brief_count=brief_count,
                )
                render_email_preview(
                    release.edition,
                    self.server.site_url,
                    content,
                    expected_date=release.release_date,
                )
            smtp, _ = save_settings(
                self._env_path(),
                body,
                published_main_count=main_count,
                published_brief_count=brief_count,
                resolver=self.server.resolver,
            )
            self._sync_site_environment()
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(error)
            return
        except OSError:
            self._json(503, {"error": "源配置写入失败，请检查存储后重试", "category": "configuration"})
            return
        self._json(200, {"ok": True, "password_set": bool(smtp.password)})

    def _handle_mail_clear_password(self, body: dict[str, Any]) -> None:
        if set(body) != {"confirm"}:
            self._json(400, {"error": "清除密码字段无效", "category": "configuration"})
            return
        try:
            clear_password(self._env_path(), confirm=body.get("confirm") is True)
            self._sync_site_environment()
        except (AdminEmailError, ValueError) as error:
            self._safe_error(error)
            return
        except OSError:
            self._json(503, {"error": "源配置写入失败，请检查存储后重试", "category": "configuration"})
            return
        self._json(200, {"ok": True, "password_set": False})

    def _smtp_form_configs(
        self,
        body: dict[str, Any],
        *,
        saved_recipients: bool,
        saved_content: bool = False,
    ):
        main_count, brief_count, release = self._published_counts()
        saved_env = read_env(self._env_path())
        smtp, content, _ = configs_from_form(
            body,
            saved_env,
            published_main_count=main_count,
            published_brief_count=brief_count,
            saved_recipients=saved_recipients,
        )
        if saved_content:
            content = email_content_config_from_env(
                saved_env,
                published_main_count=main_count,
                published_brief_count=brief_count,
            )
        validate_smtp_config_target(smtp, self.server.resolver)
        return smtp, content, release

    def _smtp_form_config(self, body: dict[str, Any]) -> SmtpConfig:
        smtp = smtp_config_from_form(body, read_env(self._env_path()))
        validate_smtp_config_target(smtp, self.server.resolver)
        return smtp

    def _handle_smtp_test(self, body: dict[str, Any]) -> None:
        if not self._admin_ready():
            return
        if not self.server.smtp_lock.acquire(blocking=False):
            self._json(409, {"error": "已有 SMTP 测试正在运行", "category": "busy"})
            return
        try:
            smtp = self._smtp_form_config(body)
            self.server.smtp_test_callback(smtp, self.server.resolver)
        except (AdminEmailError, MailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(error, status=502 if isinstance(error, MailError) else 400)
            return
        finally:
            self.server.smtp_lock.release()
        self._json(
            200, {"ok": True, "category": "success", "message": "连接与认证成功；未发送邮件"}
        )

    def _valid_idempotency_key(self, value: Any) -> str:
        if (
            not isinstance(value, str)
            or not 16 <= len(value) <= 128
            or any(not (character.isalnum() or character in "-_") for character in value)
        ):
            raise AdminEmailError("idempotency_key 必须是 16–128 位安全字符串")
        return value

    @staticmethod
    def _test_attempt_fingerprint(smtp: SmtpConfig, content, release, rendered) -> str:
        message = "\0".join((rendered.subject, rendered.html)).encode()
        identity = {
            "transport_format": "single_html_notice_v1",
            "release_name": release.release_name,
            "edition_date": release.release_date,
            "sender_ref": hashlib.sha256(smtp.sender.casefold().encode()).hexdigest(),
            "recipient_refs": sorted(
                hashlib.sha256(address.casefold().encode()).hexdigest()
                for address in smtp.recipients
            ),
            "content": vars(content),
            "message_ref": hashlib.sha256(message).hexdigest(),
        }
        encoded = json.dumps(
            identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _smtp_smoke_fingerprint(smtp: SmtpConfig) -> str:
        identity = {
            "kind": "smtp_smoke_v1",
            "sender_ref": hashlib.sha256(smtp.sender.casefold().encode()).hexdigest(),
            "recipient_refs": sorted(
                hashlib.sha256(address.casefold().encode()).hexdigest()
                for address in smtp.recipients
            ),
        }
        encoded = json.dumps(
            identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _smtp_smoke_report(report: DeliveryReport, edition_date: str) -> DeliveryServiceReport:
        results = report.results
        sent_count = report.sent_count
        failed_count = report.failed_count
        unknown_count = report.unknown_count
        skipped_count = sum(result.status == "skipped" for result in results)
        if results and sent_count == len(results):
            status = "sent"
        elif sent_count:
            status = "partial"
        else:
            status = "failed"
        categories = {
            result.error_category
            for result in results
            if result.status in {"failed", "unknown"} and result.error_category
        }
        stages = {
            result.error_stage
            for result in results
            if result.status in {"failed", "unknown"} and result.error_stage
        }
        return DeliveryServiceReport(
            None,
            "smtp-smoke",
            edition_date,
            "test",
            status,
            len(results),
            sent_count,
            failed_count,
            unknown_count,
            skipped_count,
            False,
            "not_requested",
            categories.pop() if len(categories) == 1 else "partial_refusal" if categories else None,
            "SMTP 验证邮件已执行",
            stages.pop() if len(stages) == 1 else "multiple" if stages else None,
        )

    @staticmethod
    def _attempt_report(attempt: db.TestAttempt, release_name: str) -> DeliveryServiceReport:
        if attempt.sent_count == attempt.total_count:
            status = "sent"
        elif attempt.sent_count:
            status = "partial"
        else:
            status = "failed"
        return DeliveryServiceReport(
            None,
            release_name,
            attempt.edition_date,
            "test",
            status,
            attempt.total_count,
            attempt.sent_count,
            attempt.failed_count,
            attempt.unknown_count,
            attempt.skipped_count,
            False,
            "not_requested",
            attempt.error_category,
            "测试尝试已完成" if attempt.status != "running" else "测试尝试正在执行",
            attempt.error_stage,
        )

    @staticmethod
    def _attempt_terminal(report: DeliveryServiceReport) -> tuple[str, str]:
        if report.unknown_count:
            action = (
                "do_not_repeat_whole_test"
                if report.sent_count or report.failed_count or report.skipped_count
                else "wait_and_verify_delivery"
            )
            return "unknown", action
        if report.retry_allowed:
            return "failed", "retry_test"
        if report.sent_count and (report.failed_count or report.skipped_count):
            return "completed", "do_not_repeat_whole_test"
        return "completed", "none"

    def _handle_test_message(self, body: dict[str, Any]) -> None:
        if not self._admin_ready():
            return
        if not self.server.smtp_lock.acquire(blocking=False):
            self._json(409, {"error": "已有 SMTP 测试正在运行", "category": "busy"})
            return
        try:
            if body.get("confirm") is not True:
                self._json(
                    409,
                    {
                        "error": "发送测试邮件需要 confirm=true 和幂等键",
                        "category": "confirmation",
                    },
                )
                return
            allowed = {
                "confirm",
                "idempotency_key",
                "subscription_id",
                "kind",
                "settings",
            }
            if (
                not {"confirm", "idempotency_key", "subscription_id"} <= set(body)
                or set(body) - allowed
            ):
                self._json(
                    400,
                    {
                        "error": "测试邮件必须明确选择一个订阅账号",
                        "category": "configuration",
                    },
                )
                return
            try:
                key = self._valid_idempotency_key(body["idempotency_key"])
                subscription_id = body.get("subscription_id")
                if type(subscription_id) is not int or subscription_id < 1:
                    raise AdminEmailError("subscription_id 必须是正整数")
                conn = db.connect(self.server.db_path)
                try:
                    recipient = subscriptions.active_subscription_recipient_id(
                        conn, subscription_id
                    )
                finally:
                    conn.close()
                if recipient is None:
                    raise AdminEmailError("只能向 active 订阅账号发送测试邮件", category="lifecycle")
                test_kind = body.get("kind", "digest")
                if test_kind not in {"digest", "smtp_smoke"}:
                    raise AdminEmailError("测试邮件 kind 无效")
                saved_env = read_env(self._env_path())
                smtp = replace(smtp_config_from_env(saved_env), recipients=(recipient,))
                validate_smtp_config_target(smtp, self.server.resolver)
                if test_kind == "smtp_smoke":
                    content = release = None
                    edition_date = dt.datetime.fromtimestamp(
                        self.server.clock(), dt.UTC
                    ).date().isoformat()
                    release_name = "smtp-smoke"
                    fingerprint = self._smtp_smoke_fingerprint(smtp)
                else:
                    main_count, brief_count, release = self._published_counts()
                    content = email_content_config_from_env(
                        saved_env,
                        published_main_count=main_count,
                        published_brief_count=brief_count,
                    )
                    edition_date = release.release_date
                    release_name = release.release_name
                    rendered = render_email_preview(
                        release.edition,
                        self.server.site_url,
                        content,
                        test=True,
                        expected_date=release.release_date,
                    )
                    fingerprint = self._test_attempt_fingerprint(
                        smtp, content, release, rendered
                    )
                key_hash = hashlib.sha256(key.encode()).hexdigest()
                now_iso = dt.datetime.fromtimestamp(
                    self.server.clock(), dt.UTC
                ).isoformat(timespec="seconds")
                conn = db.connect(self.server.db_path)
                try:
                    claimed = db.begin_test_attempt(
                        conn,
                        key_hash,
                        fingerprint,
                        edition_date,
                        len(smtp.recipients),
                        now_iso,
                    )
                finally:
                    conn.close()
                if claimed.disposition != "started":
                    report = self._attempt_report(claimed.attempt, release_name)
                    payload = {**self._report_payload(report), "test_kind": test_kind}
                    if claimed.attempt.status == "running":
                        self._json(409, {"error": "同一测试邮件请求正在执行", "category": "busy"})
                    elif claimed.disposition == "blocked":
                        self._json(
                            409,
                            {
                                **payload,
                                "error": _UNKNOWN_TEST_MESSAGE_ACTION,
                                "category": "unknown_pending",
                                "duplicate": False,
                            },
                        )
                    else:
                        self._json(200, {**payload, "duplicate": True})
                    return
                if test_kind == "smtp_smoke":
                    report = self._smtp_smoke_report(
                        self.server.smtp_smoke_callback(smtp, self.server.resolver),
                        edition_date,
                    )
                else:
                    report = self._deliver("test", smtp=smtp, content=content)
                payload = {**self._report_payload(report), "test_kind": test_kind}
                terminal_status, next_action = self._attempt_terminal(report)
                conn = db.connect(self.server.db_path)
                try:
                    db.finish_test_attempt(
                        conn,
                        key_hash,
                        terminal_status,
                        dt.datetime.fromtimestamp(
                            self.server.clock(), dt.UTC
                        ).isoformat(timespec="seconds"),
                        sent_count=report.sent_count,
                        failed_count=report.failed_count,
                        unknown_count=report.unknown_count,
                        skipped_count=report.skipped_count,
                        error_category=report.error_category,
                        error_stage=report.error_stage,
                        retry_allowed=report.retry_allowed,
                        next_action=next_action,
                    )
                finally:
                    conn.close()
            except (AdminEmailError, DeliveryServiceError, MailError, ValueError) as error:
                if "key_hash" in locals():
                    conn = db.connect(self.server.db_path)
                    try:
                        attempt = db.test_attempt_by_key_hash(conn, key_hash)
                        if attempt is not None and attempt.status == "running":
                            db.recover_interrupted_test_attempts(
                                conn,
                                dt.datetime.fromtimestamp(
                                    self.server.clock(), dt.UTC
                                ).isoformat(timespec="seconds"),
                            )
                    finally:
                        conn.close()
                self._safe_error(error, status=502 if not isinstance(error, AdminEmailError) else 400)
                return
            self._json(200, {**payload, "duplicate": False})
        finally:
            self.server.smtp_lock.release()

    def _preview_saved(self, edition_date: str | None = None) -> PublishedPreview:
        if not self._admin_ready():
            raise AdminEmailError("邮件 Admin 尚未完整接线")
        env = read_env(self._env_path())
        smtp = smtp_config_from_env(env)
        database = self.server.db_path
        output_root = self.server.output_root
        if database is None or output_root is None:
            raise AdminEmailError("邮件 Admin 尚未完整接线")
        return preview_published(
            output_root=output_root,
            database=database,
            site_url=self.server.site_url,
            smtp_config=smtp,
            edition_date=edition_date,
            environ=env,
        )

    @staticmethod
    def _preview_metadata(preview: PublishedPreview) -> dict[str, Any]:
        metadata = preview.rendered.metadata
        return {
            "release_name": preview.release.release_name,
            "edition_date": metadata.edition_date,
            "subject": preview.rendered.subject,
            "recipient_count": preview.recipient_count,
            "main_count": metadata.main_count,
            "brief_count": metadata.brief_count,
            "degraded": metadata.degraded,
        }

    def _preview_payload(self, preview: PublishedPreview) -> dict[str, Any]:
        return {
            **self._preview_metadata(preview),
            "text": preview.rendered.text[:_PREVIEW_TEXT_LIMIT],
            "html": preview.rendered.html[:_PREVIEW_HTML_LIMIT],
            "text_truncated": len(preview.rendered.text) > _PREVIEW_TEXT_LIMIT,
            "html_truncated": len(preview.rendered.html) > _PREVIEW_HTML_LIMIT,
        }

    @staticmethod
    def _preview_validation(error: BaseException | None = None) -> dict[str, Any]:
        return {
            "valid": error is None,
            "category": getattr(error, "category", "configuration") if error else None,
            "message": str(error) if error else "",
        }

    def _handle_mail_preview(self, body: dict[str, Any]) -> None:
        if body != {}:
            self._json(400, {"error": "邮件预览只使用已保存内容组合", "category": "configuration"})
            return
        try:
            preview = self._preview_saved()
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(error)
            return
        self._json(200, self._preview_payload(preview))

    def _site_db(self):
        if self.server.db_path is None:
            self._json(503, {"error": "站点数据库未配置", "category": "configuration"})
            return None
        return db.connect(self.server.db_path)

    def _handle_users_overview(self, parameters: dict[str, list[str]]) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        query_values = parameters.get("query", [""])
        page_values = parameters.get("page", ["1"])
        page_size_values = parameters.get("page_size", ["20"])
        if any(len(values) != 1 for values in (query_values, page_values, page_size_values)):
            self._json(400, {"error": "分页参数只能提供一次", "category": "configuration"})
            return
        query = query_values[0].strip()
        try:
            page = int(page_values[0])
            page_size = int(page_size_values[0])
        except ValueError:
            self._json(400, {"error": "分页参数必须是整数", "category": "configuration"})
            return
        if len(query) > 254:
            self._json(400, {"error": "搜索内容过长", "category": "configuration"})
            return
        if page < 1 or not 1 <= page_size <= 100:
            self._json(
                400,
                {
                    "error": "page 必须大于等于 1，page_size 必须在 1 至 100 之间",
                    "category": "configuration",
                },
            )
            return
        conn = self._site_db()
        if conn is None:
            return
        try:
            total = db.count_users(conn, query=query)
            page_count = max(1, (total + page_size - 1) // page_size)
            page = min(page, page_count)
            users = db.list_users(
                conn,
                query=query,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            newsletter_states = {
                user.id: db.subscription_by_email(conn, user.email) for user in users
            }
            changes = {
                user.id: db.list_entitlement_changes(conn, user_id=user.id) for user in users
            }
        finally:
            conn.close()
        self._json(
            200,
            {
                "users": [
                    {
                        "id": user.id,
                        "email": user.email,
                        "status": user.status,
                        "is_admin": user.is_admin,
                        "plan": user.plan,
                        "paid_until": user.paid_until,
                        "entitlement_changes": changes[user.id],
                        "newsletter_subscription_id": (
                            newsletter_states[user.id].id
                            if newsletter_states[user.id] is not None
                            else None
                        ),
                        "newsletter_status": (
                            newsletter_states[user.id].status
                            if newsletter_states[user.id] is not None
                            else None
                        ),
                        "created_at": user.created_at,
                    }
                    for user in users
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
                "csrf_token": self._csrf_for_response(),
            },
        )

    def _handle_payments_overview(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        conn = self._site_db()
        if conn is None:
            return
        try:
            db.expire_payment_orders(conn, now=dt.datetime.now(dt.UTC).isoformat())
            query = parse_qs(urlsplit(self.path).query)
            search = query.get("query", [""])[0][:200]
            try:
                page = max(1, min(int(query.get("page", ["1"])[0]), 100000))
            except ValueError:
                self._json(400, {"error": "分页参数无效"})
                return
            orders = db.list_orders(conn, query=search, offset=(page - 1) * 200)
            cases = {
                case["order_id"]: case
                for case in db.list_payment_cases(conn, order_ids=[order.id for order in orders])
            }
            codes = db.list_redemption_codes(conn)
            settings = {
                key: (
                    db.get_setting(conn, key) if db.get_setting(conn, key) is not None else default
                )
                for key, default in accounts.DEFAULT_SETTINGS.items()
            }
            for plan in accounts.PLANS:
                list_key = f"{plan}_list_price_cents"
                if not settings[list_key].strip():
                    legacy_base = accounts.base_price_cents(settings, plan)
                    legacy_current = accounts.price_cents(settings, plan)
                    settings[list_key] = str(legacy_base)
                    settings[f"{plan}_price_cents"] = str(legacy_current)
            from news_digest.site_config import configuration_status

            config_state = configuration_status(self._env_path(), self.server.site_env_path, conn)
        finally:
            conn.close()
        try:
            payment = admin_payments.settings_payload(
                read_env(self._env_path()), self.server.site_url
            )
        except (TypeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "configuration"})
            return
        self._json(
            200,
            {
                "orders": [
                    {
                        "id": order.id,
                        "user_id": order.user_id,
                        "plan": order.plan,
                        "base_amount_cents": order.base_amount_cents,
                        "amount_cents": order.amount_cents,
                        "amount_offset_cents": order.amount_offset_cents,
                        "merchant_order_no": order.merchant_order_no,
                        "provider_trade_no": order.provider_trade_no,
                        "expires_at": order.expires_at,
                        "settlement_expires_at": order.settlement_expires_at,
                        "payment_type": order.payment_type,
                        "paid_at": order.paid_at,
                        "last_error_code": order.last_error_code,
                        "settlement_case": cases.get(order.id),
                        "status": order.status,
                        "admin_actor": order.admin_actor,
                        "created_at": order.created_at,
                    }
                    for order in orders
                ],
                "codes": [
                    {
                        "id": code.id,
                        "prefix": code.prefix,
                        "code": code.code_plaintext if code.status == "unused" else None,
                        "plan": code.plan,
                        "status": code.status,
                        "note": code.note,
                        "created_at": code.created_at,
                    }
                    for code in codes
                ],
                "settings": settings,
                "payment": payment,
                "configuration": config_state,
                "orders_page": page,
                "csrf_token": self._csrf_for_response(),
            },
        )

    def _handle_site_user_status(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            user_id = int(body.get("user_id"))
            status = body.get("status")
            if status not in {"active", "disabled"}:
                raise ValueError("status 只能是 active 或 disabled")
            user = db.set_user_status(
                conn, user_id, status=status, now=dt.datetime.now(dt.UTC).isoformat()
            )
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "status": user.status})

    def _handle_site_user_admin(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            if set(body) != {"user_id", "is_admin", "confirm"}:
                raise ValueError("user_id、is_admin 与 confirm 为必填字段")
            if type(body.get("user_id")) is not int or type(body.get("is_admin")) is not bool:
                raise ValueError("user_id 或 is_admin 非法")
            if body.get("confirm") is not True:
                raise ValueError("管理员角色变更需要 confirm=true")
            user = db.set_user_admin(
                conn,
                body["user_id"],
                is_admin=body["is_admin"],
                now=dt.datetime.now(dt.UTC).isoformat(),
            )
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "is_admin": user.is_admin})

    def _handle_site_user_grant(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            if set(body) not in (
                {"user_id", "plan", "days"},
                {"user_id", "plan", "days", "operation_id"},
            ):
                raise ValueError("user_id、plan 与 days 为必填字段")
            user_id = body.get("user_id")
            plan = body.get("plan")
            days = body.get("days")
            if (
                type(user_id) is not int
                or plan not in accounts.PLANS
                or type(days) is not int
                or not 1 <= days <= 3660
            ):
                raise ValueError("plan 或 days 非法")
            now = dt.datetime.now(dt.UTC)
            user = db.add_membership_days(
                conn,
                user_id,
                plan=plan,
                days=days,
                now=now.isoformat(),
                operation_id=body.get("operation_id") or f"admin-{secrets.token_hex(16)}",
                actor=self._admin_actor(),
                reason="admin_grant",
            )
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(
            200,
            {"ok": True, "plan": user.plan, "days_added": days, "paid_until": user.paid_until},
        )

    def _handle_site_user_subscription_clear(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            if set(body) != {"user_id", "confirm", "expected_paid_until", "operation_id"}:
                raise ValueError("清除订阅需要当前到期时间与 operation_id")
            if type(body.get("user_id")) is not int or body.get("confirm") is not True:
                raise ValueError("清除订阅需要合法 user_id 与 confirm=true")
            user = db.clear_user_subscription(
                conn,
                body["user_id"],
                now=dt.datetime.now(dt.UTC).isoformat(),
                actor=self._admin_actor(),
                expected_paid_until=body["expected_paid_until"],
                check_expected=True,
                operation_id=body["operation_id"],
            )
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "plan": user.plan, "paid_until": user.paid_until})

    def _handle_site_order_decide(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            if set(body) != {"order_id", "approve"} or not isinstance(
                body.get("approve"), bool
            ):
                raise ValueError("order_id 与布尔 approve 为必填字段")
            order_id = int(body.get("order_id"))
            approve = body["approve"]
            order = db.decide_order(
                conn,
                order_id,
                approve=approve,
                admin_actor=self._admin_actor(),
                now=dt.datetime.now(dt.UTC).isoformat(),
                plan_days=accounts.PLAN_DAYS,
            )
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "status": order.status})

    def _handle_site_codes_create(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            plan = body.get("plan")
            count = int(body.get("count", 1))
            note = (body.get("note") or "")[:200] or None
            if plan not in accounts.PLANS or not 1 <= count <= 50:
                raise ValueError("plan 或 count 非法")
            plaintext = []
            attempts = 0
            while len(plaintext) < count and attempts < count * 20:
                attempts += 1
                code = accounts.generate_redemption_code()
                created = db.create_redemption_code_if_available(
                    conn,
                    code_digest=accounts.redemption_digest(code),
                    prefix=accounts.redemption_prefix(code),
                    code_plaintext=code,
                    plan=plan,
                    note=note,
                    created_by=self._admin_actor(),
                    now=dt.datetime.now(dt.UTC).isoformat(),
                )
                if created:
                    plaintext.append(code)
            if len(plaintext) != count:
                raise RuntimeError("卡密生成冲突过多，请稍后重试")
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "codes": plaintext})

    def _handle_site_code_delete(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            code_id = int(body.get("code_id"))
            db.delete_redemption_code(conn, code_id)
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True})

    def _handle_site_settings(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            entries = {}
            if "paywall_enabled" in body:
                if type(body["paywall_enabled"]) is not bool:
                    raise ValueError("paywall_enabled 非法")
                entries["paywall_enabled"] = (
                    "true" if body["paywall_enabled"] is True else "false"
                )
            for key in (
                "monthly_price_cents",
                "yearly_price_cents",
                "monthly_list_price_cents",
                "yearly_list_price_cents",
            ):
                if key in body:
                    value = body[key]
                    if type(value) is not int or not 0 <= value <= 10_000_000:
                        raise ValueError(f"{key} 非法")
                    entries[key] = str(value)
            for key in ("monthly_discount_percent", "yearly_discount_percent"):
                if key in body:
                    value = body[key]
                    if type(value) is not int or not 0 <= value <= 100:
                        raise ValueError(f"{key} 非法")
                    entries[key] = str(value)
            if "payment_info" in body:
                entries["payment_info"] = str(body["payment_info"])[:500]
            if "payment_qr_data_url" in body:
                entries["payment_qr_data_url"] = _validated_qr_data_url(
                    body["payment_qr_data_url"]
                )
            if "contact_email" in body:
                raw_contact = str(body["contact_email"]).strip()
                if raw_contact:
                    accounts.normalize_email(raw_contact)
                entries["contact_email"] = raw_contact
            if not entries:
                raise ValueError("没有需要保存的设置")
            current_settings = {
                key: db.get_setting(conn, key) or default
                for key, default in accounts.DEFAULT_SETTINGS.items()
            }
            for plan in accounts.PLANS:
                list_key = f"{plan}_list_price_cents"
                price_key = f"{plan}_price_cents"
                discount_key = f"{plan}_discount_percent"
                if (
                    discount_key in entries
                    and list_key not in entries
                    and price_key not in entries
                    and current_settings.get(list_key, "").strip()
                ):
                    raise ValueError("价格已迁移，不能单独修改旧折扣字段")
                if (
                    list_key in entries
                    and price_key not in entries
                    and not current_settings.get(list_key, "").strip()
                ):
                    raise ValueError("划线基准价与会员现价必须同时保存")
                if list_key in entries or (
                    price_key in entries and current_settings.get(list_key, "").strip()
                ):
                    entries[discount_key] = "0"
            candidate_settings = {**current_settings, **entries}
            for plan in accounts.PLANS:
                base = accounts.base_price_cents(candidate_settings, plan)
                list_key = f"{plan}_list_price_cents"
                raw_list = candidate_settings.get(list_key, "").strip()
                current = int(candidate_settings[f"{plan}_price_cents"])
                if raw_list and current > base:
                    raise ValueError("会员现价不能高于划线基准价")
                if accounts.price_cents(candidate_settings, plan) <= 10:
                    raise ValueError("会员现价必须至少为 0.11 元")
            db.set_settings(conn, entries, now=dt.datetime.now(dt.UTC).isoformat())
        except (TypeError, ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True})

    def _handle_site_payment_settings(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        now = dt.datetime.now(dt.UTC).isoformat()
        try:
            settlement_locked = db.begin_payment_config_update(conn, now=now)
            payload = admin_payments.save_settings(
                self._env_path(),
                body,
                self.server.site_url,
                settlement_locked=settlement_locked,
            )
            config = payments.settlement_config_from_mapping(
                {**read_env(self._env_path()), "NEWS_SITE_URL": self.server.site_url}
            )
            db.set_active_payment_config_id(
                conn,
                payment_config_id=(
                    payments.config_identity(config) if config is not None else None
                ),
                now=now,
            )
            self._sync_site_environment(conn)
            conn.commit()
        except (admin_payments.AdminPaymentError, payments.PaymentError, ValueError,
                OSError, sqlite3.Error) as error:
            conn.rollback()
            self._json(409, {"error": str(error), "category": "configuration"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "payment": payload})

    def _handle_site_payment_clear_pkey(self, body: dict[str, Any]) -> None:
        if set(body) != {"confirm"} or body.get("confirm") is not True:
            self._json(409, {"error": "必须明确确认清除 PKey", "category": "configuration"})
            return
        conn = self._site_db()
        if conn is None:
            return
        now = dt.datetime.now(dt.UTC).isoformat()
        try:
            settlement_locked = db.begin_payment_config_update(conn, now=now)
            admin_payments.clear_pkey(
                self._env_path(), settlement_locked=settlement_locked
            )
            db.set_active_payment_config_id(
                conn, payment_config_id=None, now=now
            )
            self._sync_site_environment(conn)
            conn.commit()
        except (admin_payments.AdminPaymentError, payments.PaymentError, ValueError,
                OSError, sqlite3.Error) as error:
            conn.rollback()
            self._json(409, {"error": str(error), "category": "configuration"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True, "pkey_set": False, "enabled": False})

    def _handle_site_payment_case(self, body: dict[str, Any]) -> None:
        conn = self._site_db()
        if conn is None:
            return
        try:
            if set(body) != {"order_id", "action", "reference", "days", "operation_id"}:
                raise ValueError("异常处理参数不完整")
            if type(body["order_id"]) is not int:
                raise ValueError("order_id 非法")
            db.resolve_payment_case(
                conn, **body, actor=self._admin_actor(), now=dt.datetime.now(dt.UTC).isoformat()
            )
        except (ValueError, RuntimeError) as error:
            self._json(409, {"error": str(error), "category": "payment"})
            return
        finally:
            conn.close()
        self._json(200, {"ok": True})

    def _handle_site_payment_reconcile(self, body: dict[str, Any]) -> None:
        if set(body) != {"order_id"} or type(body.get("order_id")) is not int:
            self._json(409, {"error": "订单参数无效", "category": "lifecycle"})
            return
        now_datetime = dt.datetime.fromtimestamp(self.server.clock(), dt.UTC)
        conn = self._site_db()
        if conn is None:
            return
        try:
            order = db.order_by_id(conn, body["order_id"])
        finally:
            conn.close()
        if (
            order is None
            or order.status not in {"pending", "expired", "failed"}
            or not order.merchant_order_no
            or (
                order.last_error_code == "GATEWAY_CREATE_RUNNING"
                and (
                    now_datetime - dt.datetime.fromisoformat(order.updated_at)
                ).total_seconds()
                < db.PAYMENT_CREATION_LEASE_SECONDS
            )
        ):
            self._json(409, {"error": "订单已不可对账", "category": "lifecycle"})
            return
        try:
            config = payments.settlement_config_from_mapping(
                {**read_env(self._env_path()), "NEWS_SITE_URL": self.server.site_url}
            )
            if config is None or (
                order.payment_config_id is not None
                and payments.config_identity(config) != order.payment_config_id
            ):
                raise payments.PaymentError("订单结算配置不匹配")
            result = payments.query_payment(
                config,
                merchant_order_no=order.merchant_order_no,
                expected_amount_cents=order.amount_cents,
            )
            finished_now = dt.datetime.fromtimestamp(
                self.server.clock(), dt.UTC
            ).isoformat()
            conn = self._site_db()
            if conn is None:
                return
            try:
                if result.trade_status == "TRADE_SUCCESS":
                    order = db.confirm_payment_order(
                        conn,
                        merchant_order_no=result.merchant_order_no,
                        provider_trade_no=result.provider_trade_no,
                        amount_cents=result.amount_cents,
                        now=finished_now,
                        amount_hold_seconds=config.amount_hold_seconds,
                        plan_days=accounts.PLAN_DAYS,
                    )
                else:
                    order = db.record_payment_query_status(
                        conn,
                        order_id=order.id,
                        trade_status=result.trade_status,
                        expected_updated_at=order.updated_at,
                        now=finished_now,
                    )
            finally:
                conn.close()
        except (payments.PaymentError, RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "payment"})
            return
        self._json(
            200,
            {
                "ok": True,
                "order_id": order.id,
                "status": order.status,
                "last_error_code": order.last_error_code,
            },
        )

    def _handle_subscriptions_get(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        if self.server.db_path is None:
            self._json(503, {"error": "订阅数据库未配置", "category": "configuration"})
            return
        conn = db.connect(self.server.db_path)
        try:
            legacy_recipients = smtp_config_from_env(
                read_env(self._env_path())
            ).recipients
            if legacy_recipients:
                subscriptions.import_legacy_smtp_recipients_once(
                    conn,
                    legacy_recipients,
                    dt.datetime.now(dt.UTC),
                )
            states = subscriptions.admin_subscription_list(conn)
            counts = subscriptions.admin_subscription_counts(conn)
        finally:
            conn.close()
        self._json(
            200,
            {
                "counts": counts,
                "items": [
                    {
                        "id": item.id,
                        "email_masked": item.email_masked,
                        "recipient_key": item.recipient_key,
                        "status": item.status,
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                        "confirmed_at": item.confirmed_at,
                        "unsubscribed_at": item.unsubscribed_at,
                    }
                    for item in states
                ],
                "public_subscription_enabled": self._public_submission_ready(),
                "csrf_token": self._csrf_for_response(),
            },
        )

    def _handle_subscription_add(self, body: dict[str, Any]) -> None:
        if set(body) != {"email"} or not isinstance(body.get("email"), str):
            self._json(400, {"error": "新增订阅账号字段无效", "category": "configuration"})
            return
        if self.server.db_path is None:
            self._json(503, {"error": "订阅数据库未配置", "category": "configuration"})
            return
        conn = db.connect(self.server.db_path)
        try:
            now = dt.datetime.now(dt.UTC)
            email = normalize_email_address(body["email"], "Admin newsletter recipient")
            existing = db.subscription_by_email(conn, email)
            if existing is not None and existing.status == "disabled":
                self._json(
                    409,
                    {
                        "error": "管理员停用的简报必须使用启用操作并提交 confirm=true",
                        "category": "lifecycle",
                    },
                )
                return
            user = db.user_by_email_key(conn, db.delivery_recipient_key(email))
            if user is None or user.status != "active" or not accounts.is_paid(
                user.paid_until, now
            ):
                self._json(
                    409,
                    {
                        "error": "只有已启用且会员未到期的注册用户可以开启每日简报",
                        "category": "membership",
                    },
                )
                return
            added = subscriptions.add_admin_test_recipient(
                conn, email, now
            )
        except ValueError as error:
            self._safe_error(error)
            return
        finally:
            conn.close()
        if not added:
            self._json(
                409,
                {"error": "账号已存在；请使用对应的启用操作", "category": "lifecycle"},
            )
            return
        self._json(200, {"ok": True})

    def _handle_subscription_disable(self, body: dict[str, Any]) -> None:
        if set(body) != {"id", "confirm"} or body.get("confirm") is not True:
            self._json(
                409,
                {
                    "error": "停用订阅账号需要 id 与 confirm=true",
                    "category": "confirmation",
                },
            )
            return
        if self.server.db_path is None or type(body.get("id")) is not int:
            self._json(400, {"error": "停用订阅账号字段无效", "category": "configuration"})
            return
        conn = db.connect(self.server.db_path)
        try:
            disabled = subscriptions.disable_subscription_id(
                conn, body["id"], dt.datetime.now(dt.UTC)
            )
        except ValueError as error:
            self._safe_error(error)
            return
        finally:
            conn.close()
        if not disabled:
            self._json(409, {"error": "只能停用 active 订阅账号", "category": "lifecycle"})
            return
        self._json(200, {"ok": True})

    def _handle_subscription_enable(self, body: dict[str, Any]) -> None:
        if set(body) != {"id", "confirm"} or body.get("confirm") is not True:
            self._json(
                409,
                {"error": "启用订阅账号需要 id 与 confirm=true", "category": "confirmation"},
            )
            return
        if self.server.db_path is None or type(body.get("id")) is not int:
            self._json(400, {"error": "启用订阅账号字段无效", "category": "configuration"})
            return
        conn = db.connect(self.server.db_path)
        try:
            row = conn.execute(
                "SELECT email, status FROM subscriptions WHERE id = ?", (body["id"],)
            ).fetchone()
            if row is None or row["status"] != "disabled":
                self._json(
                    409,
                    {"error": "只能重新启用 disabled 订阅账号", "category": "lifecycle"},
                )
                return
            now = dt.datetime.now(dt.UTC)
            user = db.user_by_email_key(
                conn, db.delivery_recipient_key(row["email"])
            )
            if user is None or user.status != "active" or not accounts.is_paid(
                user.paid_until, now
            ):
                self._json(
                    409,
                    {
                        "error": "只有已启用且会员未到期的注册用户可以开启每日简报",
                        "category": "membership",
                    },
                )
                return
            enabled = subscriptions.enable_subscription_id(
                conn, body["id"], now
            )
        except ValueError as error:
            self._safe_error(error)
            return
        finally:
            conn.close()
        if not enabled:
            self._json(409, {"error": "只能重新启用 disabled 订阅账号", "category": "lifecycle"})
            return
        self._json(200, {"ok": True})

    def _handle_subscription_delete(self, body: dict[str, Any]) -> None:
        if set(body) != {"id", "confirm"} or body.get("confirm") is not True:
            self._json(
                409,
                {"error": "删除订阅账号需要 id 与 confirm=true", "category": "confirmation"},
            )
            return
        if self.server.db_path is None or type(body.get("id")) is not int:
            self._json(400, {"error": "删除订阅账号字段无效", "category": "configuration"})
            return
        conn = db.connect(self.server.db_path)
        try:
            deleted = subscriptions.delete_subscription_id(conn, body["id"])
        except ValueError as error:
            self._safe_error(error)
            return
        finally:
            conn.close()
        if not deleted:
            self._json(404, {"error": "订阅账号不存在", "category": "lifecycle"})
            return
        self._json(200, {"ok": True})

    def _translation_payload(self, edition_date: str | None = None) -> dict[str, Any]:
        if self.server.translation_db_path is None:
            raise RuntimeError("翻译任务数据库未配置")
        conn = db.connect(self.server.translation_db_path)
        try:
            problem_dates = db.automation_problem_dates(conn)
            selected = db.automation_edition(conn, edition_date) if edition_date else None
            edition_dates = list(problem_dates)
            if selected is not None and selected.edition_date not in edition_dates:
                edition_dates.insert(0, selected.edition_date)
            edition = selected or (
                db.automation_edition(conn, problem_dates[0])
                if problem_dates
                else db.latest_automation_edition(conn)
            )
            if edition is None:
                return {
                    "edition_dates": edition_dates,
                    "edition": None,
                    "summary": {
                        "total": 0,
                        "online": 0,
                        "running": 0,
                        "retry_wait": 0,
                        "failed": 0,
                    },
                    "provider": {
                        "id": "",
                        "state": "closed",
                        "consecutive_failures": 0,
                        "current_concurrency": 0,
                        "queue_count": 0,
                        "waiting_dispatch_count": 0,
                        "waiting_backoff_count": 0,
                        "waiting_cancel_count": 0,
                        "waiting_probe_count": 0,
                        "next_executable_at": None,
                        "next_probe_at": None,
                        "recovery_mode": False,
                    },
                    "items": [],
                    "probe_task_id": None,
                    "csrf_token": self._csrf_for_response(),
                }
            tasks = db.active_translation_tasks(conn, edition.edition_date)
            provider_id = tasks[0].provider_id if tasks else ""
            provider_circuits = {
                task.provider_id: db.get_provider_circuit(conn, task.provider_id)
                for task in tasks
            }
            circuit = provider_circuits.get(provider_id)
            circuit_state = circuit.state if circuit else "closed"
            now = dt.datetime.fromtimestamp(self.server.clock(), dt.UTC)

            def parsed(value: str | None) -> dt.datetime | None:
                return dt.datetime.fromisoformat(value) if value else None

            def task_circuit(task: db.TranslationTask) -> db.ProviderCircuit | None:
                return provider_circuits.get(task.provider_id)

            def next_executable(task: db.TranslationTask) -> str | None:
                if task.status not in {"pending", "retry_wait"}:
                    return None
                if task.manual_retry_requested_at or task.manual_probe_requested_at:
                    return task.next_retry_at or now.isoformat()
                task_provider = task_circuit(task)
                candidates = [parsed(task.next_retry_at)]
                if task_provider is not None and task_provider.state == "open":
                    candidates.append(parsed(task_provider.next_probe_at))
                available = [value for value in candidates if value is not None]
                return max(available).isoformat() if available else now.isoformat()

            def queue_state(task: db.TranslationTask) -> str:
                task_provider = task_circuit(task)
                task_circuit_state = task_provider.state if task_provider else "closed"
                if task.status == "running":
                    return (
                        "waiting_cancel_confirmation"
                        if task.cancel_requested_at is not None
                        else "executing"
                    )
                if task.status in {"pending", "retry_wait"}:
                    if task_circuit_state == "configuration_blocked":
                        return "blocked"
                    if task.manual_retry_requested_at or task.manual_probe_requested_at:
                        return "waiting_dispatch"
                    available = parsed(next_executable(task))
                    if task_circuit_state in {"open", "half_open"} and (
                        available is None or available <= now
                    ):
                        return "waiting_probe"
                    return (
                        "waiting_backoff"
                        if available is not None and available > now
                        else "waiting_dispatch"
                    )
                if task.status == "succeeded":
                    return "complete" if task.build_status == "online" else "waiting_build"
                return "blocked"

            task_queue_states = {task.task_id: queue_state(task) for task in tasks}
            executing_count = sum(
                state == "executing" for state in task_queue_states.values()
            )
            waiting_dispatch_count = sum(
                state == "waiting_dispatch" for state in task_queue_states.values()
            )
            waiting_backoff_count = sum(
                state == "waiting_backoff" for state in task_queue_states.values()
            )
            waiting_cancel_count = sum(
                state == "waiting_cancel_confirmation"
                for state in task_queue_states.values()
            )
            waiting_probe_count = sum(
                state == "waiting_probe" for state in task_queue_states.values()
            )
            executable_times = [
                parsed(next_executable(task))
                for task in tasks
                if task_queue_states[task.task_id]
                in {"waiting_dispatch", "waiting_backoff"}
            ]
            executable_times = [value for value in executable_times if value is not None]
            probe_candidates = sorted(
                (
                    task
                    for task in tasks
                    if task.status in {"failed", "retry_wait", "cancelled", "pending", "configuration_blocked"}
                    and (
                        task_circuit(task) is not None
                        and task_circuit(task).state in {"open", "half_open", "configuration_blocked"}
                    )
                ),
                key=lambda task: task.status == "pending",
            )

            def available_actions(task: db.TranslationTask) -> list[str]:
                # db.task_capabilities 是调度器与 Admin 的同一资格来源;
                # 这里只做展示转换,不再自行维护判定逻辑。
                task_provider = task_circuit(task)
                task_circuit_state = task_provider.state if task_provider else "closed"
                capabilities = db.task_capabilities(
                    status=task.status,
                    cancel_requested_at=task.cancel_requested_at,
                    lease_expires_at=task.lease_expires_at,
                    auto_retry=task.auto_retry,
                    next_retry_at=task.next_retry_at,
                    circuit_state=task_circuit_state,
                    now=now.isoformat(),
                )
                return list(capabilities.actions)

            summary = {
                "total": len(tasks),
                "online": sum(task.build_status == "online" for task in tasks),
                "running": sum(task.status == "running" for task in tasks),
                "retry_wait": sum(task.status == "retry_wait" for task in tasks),
                "failed": sum(
                    task.status in {"failed", "configuration_blocked", "cancelled"}
                    for task in tasks
                ),
            }
            queue_count = sum(
                state
                in {
                    "waiting_dispatch",
                    "waiting_backoff",
                    "waiting_cancel_confirmation",
                    "waiting_probe",
                }
                for state in task_queue_states.values()
            )
            last_updated = max(
                [edition.updated_at]
                + [task.updated_at for task in tasks]
                + ([circuit.updated_at] if circuit else [])
            )
            return {
                "edition": {
                    "date": edition.edition_date,
                    "status": edition.status,
                    "delivery_status": (
                        "skipped" if edition.last_error_code == "NO_ELIGIBLE_RECIPIENTS"
                        else "sent" if edition.status == "delivered" else None
                    ),
                    "delivery_reason": (
                        "no_eligible_recipients"
                        if edition.last_error_code == "NO_ELIGIBLE_RECIPIENTS" else None
                    ),
                    # partial/build_failed 刊期提供一键批量恢复入口。
                    "retry_edition_available": (
                        edition.status in {"partial", "build_failed"} and summary["failed"] > 0
                    ),
                    "error_code": (
                        edition.last_error_code
                        if edition.last_error_code in _AUTOMATION_EDITION_ERROR_CODES
                        else "UNKNOWN" if edition.last_error_code else None
                    ),
                    "last_updated": last_updated,
                },
                "edition_dates": edition_dates,
                "summary": summary,
                "provider": {
                    "id": provider_id,
                    "state": circuit_state,
                    "consecutive_failures": circuit.consecutive_failures if circuit else 0,
                    "current_concurrency": executing_count,
                    "queue_count": queue_count,
                    "waiting_dispatch_count": waiting_dispatch_count,
                    "waiting_backoff_count": waiting_backoff_count,
                    "waiting_cancel_count": waiting_cancel_count,
                    "waiting_probe_count": waiting_probe_count,
                    "next_executable_at": (
                        min(executable_times).isoformat() if executable_times else None
                    ),
                    "next_probe_at": circuit.next_probe_at if circuit else None,
                    "recovery_mode": circuit.recovery_mode if circuit else False,
                },
                "items": [
                    {
                        "task_id": task.task_id,
                        "title": task.article_title,
                        "status": task.status,
                        "stage": task.current_stage,
                        "build_status": task.build_status,
                        "attempt_count": task.attempt_count,
                        "error_code": task.error_code,
                        "error_category": task.error_category,
                        "http_status": task.http_status,
                        "failure_stage": task.failure_stage,
                        "diagnostic_id": task.diagnostic_id,
                        "failed_at": task.failed_at,
                        "next_retry_at": task.next_retry_at,
                        "started_at": task.started_at,
                        "finished_at": task.finished_at,
                        "last_activity_at": task.last_activity_at,
                        "hard_timeout_at": task.hard_timeout_at,
                        "received_chunks": task.received_chunks,
                        "cancel_requested": task.cancel_requested_at is not None,
                        "queue_state": task_queue_states[task.task_id],
                        "next_executable_at": next_executable(task),
                        # The API is the single authority for recovery controls.
                        # The browser must not infer actions from status strings.
                        "available_actions": available_actions(task),
                        "action": (
                            {
                                "action_id": action.action_id,
                                "type": action.action,
                                "status": action.status,
                                "result_code": action.result_code,
                                "requested_at": action.requested_at,
                                "finished_at": action.finished_at,
                            }
                            if (action := db.latest_translation_admin_action(conn, task.task_id))
                            else None
                        ),
                        "retry_allowed": "retry" in available_actions(task),
                        "cancel_allowed": (
                            task.status == "running" and task.cancel_requested_at is None
                        ),
                    }
                    for task in tasks
                ],
                "probe_task_id": (
                    circuit.probe_task_id
                    if circuit_state == "half_open" and circuit is not None
                    else (
                        probe_candidates[0].task_id
                        if circuit_state in {"open", "configuration_blocked"} and probe_candidates
                        else None
                    )
                ),
                "csrf_token": self._csrf_for_response(),
            }
        finally:
            conn.close()

    def _handle_translations_get(self, edition_date: str | None = None) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        try:
            payload = self._translation_payload(edition_date)
        except RuntimeError as error:
            self._json(503, {"error": str(error), "category": "configuration"})
            return
        self._json(200, payload)

    def _handle_translation_events(self, edition_date: str | None = None) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        try:
            payload = self._translation_payload(edition_date)
            event = (
                "retry: 2000\n"
                "event: translation-state\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            self.wfile.write(event)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except RuntimeError as error:
            self._json(503, {"error": str(error), "category": "configuration"})

    @staticmethod
    def _translation_task_body(body: dict[str, Any], *, confirm: bool = False) -> str:
        expected = {"task_id", "confirm"} if confirm else {"task_id"}
        if set(body) != expected or (confirm and body.get("confirm") is not True):
            raise ValueError("翻译任务操作字段无效")
        task_id = body.get("task_id")
        if (
            not isinstance(task_id, str)
            or len(task_id) != 64
            or any(character not in "0123456789abcdef" for character in task_id)
        ):
            raise ValueError("翻译任务 ID 无效")
        return task_id

    def _handle_translation_retry(self, body: dict[str, Any]) -> None:
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        try:
            task_id = self._translation_task_body(body)
            conn = db.connect(self.server.translation_db_path)
            try:
                task = db.queue_translation_task_retry(
                    conn,
                    task_id,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    actor=self._admin_actor(),
                )
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        self._json(
            202,
            {
                "ok": True,
                "status": task.status,
                "action_id": task.manual_action_id,
            },
        )

    def _handle_translation_retry_edition(self, body: dict[str, Any]) -> None:
        """刊期级一键恢复:把该刊期全部终态任务批量重新入队(消灭 partial 死端)。"""
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        if not isinstance(body, dict) or body.get("confirm") is not True:
            self._json(400, {"error": "需要 confirm 确认", "category": "lifecycle"})
            return
        edition_date = body.get("edition_date")
        if not isinstance(edition_date, str):
            self._json(400, {"error": "edition_date 缺失", "category": "lifecycle"})
            return
        try:
            conn = db.connect(self.server.translation_db_path)
            try:
                provider = default_provider(
                    load_profiles(
                        self.server.providers_path.parent,
                        self.server.providers_path.name,
                    )
                )
                counts = db.retry_edition_failed_tasks(
                    conn,
                    edition_date,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    actor=self._admin_actor(),
                    provider_id=(
                        "default-"
                        + translation_cache_identity(
                            provider["api_type"],
                            provider["base_url"],
                            provider["model"],
                            provider.get("reasoning_effort", ""),
                        )[:64]
                    ),
                )
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        self._json(202, {"ok": True, "queued": counts["queued"], "skipped": counts["skipped"]})

    def _handle_translation_dispatch(self, body: dict[str, Any]) -> None:
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        try:
            task_id = self._translation_task_body(body)
            conn = db.connect(self.server.translation_db_path)
            try:
                task = db.queue_translation_task_dispatch(
                    conn,
                    task_id,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    actor=self._admin_actor(),
                )
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        self._json(
            202,
            {
                "ok": True,
                "status": task.status,
                "action_id": task.manual_action_id,
            },
        )

    def _handle_translation_cancel(self, body: dict[str, Any]) -> None:
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        try:
            task_id = self._translation_task_body(body, confirm=True)
            conn = db.connect(self.server.translation_db_path)
            try:
                task = db.request_translation_task_cancel(
                    conn,
                    task_id,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    actor=self._admin_actor(),
                )
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        conn = db.connect(self.server.translation_db_path)
        try:
            action = conn.execute(
                "SELECT action_id FROM translation_admin_actions"
                " WHERE task_id = ? AND action = 'cancel'"
                " ORDER BY requested_at DESC LIMIT 1",
                (task.task_id,),
            ).fetchone()
        finally:
            conn.close()
        self._json(
            202,
            {
                "ok": True,
                "cancel_requested": task.cancel_requested_at is not None,
                "action_id": action["action_id"] if action else None,
            },
        )

    def _handle_translation_probe(self, body: dict[str, Any]) -> None:
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        try:
            task_id = self._translation_task_body(body, confirm=True)
            conn = db.connect(self.server.translation_db_path)
            try:
                task = db.translation_task(conn, task_id)
                if task is None:
                    raise RuntimeError("translation task does not exist")
                already_queued = False
                circuit = db.get_provider_circuit(conn, task.provider_id)
                if circuit is not None and circuit.state == "half_open":
                    queued = db.queue_provider_probe(
                        conn,
                        task.provider_id,
                        task_id,
                        now=dt.datetime.now(dt.UTC).isoformat(),
                        actor=self._admin_actor(),
                    )
                    already_queued = True
                else:
                    try:
                        queued = db.queue_provider_probe(
                            conn,
                            task.provider_id,
                            task_id,
                            now=dt.datetime.now(dt.UTC).isoformat(),
                            actor=self._admin_actor(),
                        )
                    except RuntimeError as error:
                        if str(error) != "provider probe is already queued":
                            raise
                        queued = db.queued_provider_probe(conn, task.provider_id)
                        if queued is None:
                            raise
                        already_queued = True
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        self._json(
            202,
            {
                "ok": True,
                "status": queued.status,
                "already_queued": already_queued,
                "action_id": queued.manual_action_id,
            },
        )

    def _handle_translation_unblock(self, body: dict[str, Any]) -> None:
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        try:
            task_id = self._translation_task_body(body, confirm=True)
            profiles = load_profiles(self.server.project_root, self.server.profiles_file)
            conn = db.connect(self.server.translation_db_path)
            try:
                task = db.translation_task(conn, task_id)
                if task is None:
                    raise RuntimeError("translation task does not exist")
                circuit = db.get_provider_circuit(conn, task.provider_id)
                if circuit is None or circuit.state != "configuration_blocked":
                    raise RuntimeError("provider configuration is not blocked")
            finally:
                conn.close()
            matched = None
            for provider in profiles["providers"].values():
                identity = translation_cache_identity(
                    provider["api_type"],
                    provider["base_url"],
                    provider["model"],
                    provider.get("reasoning_effort", ""),
                )
                if task.provider_id in {
                    provider["name"],
                    f"default-{identity[:64]}",
                }:
                    matched = provider
                    break
            if matched is None or not assert_recent_success(
                self.server.project_root,
                matched,
                max_age_seconds=_TEST_MAX_AGE_SECONDS,
            ):
                raise RuntimeError("请先保存当前配置并完成成功的受控测试")
            conn = db.connect(self.server.translation_db_path)
            try:
                action_id, already_queued = db.unblock_provider_configuration(
                    conn,
                    task.provider_id,
                    task_id=task_id,
                    now=dt.datetime.now(dt.UTC).isoformat(),
                    actor=self._admin_actor(),
                )
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        self._json(
            202,
            {"ok": True, "action_id": action_id, "already_queued": already_queued},
        )

    def _handle_translation_recover(self, body: dict[str, Any]) -> None:
        if self.server.translation_db_path is None:
            self._json(503, {"error": "翻译任务数据库未配置", "category": "configuration"})
            return
        try:
            task_id = self._translation_task_body(body, confirm=True)
            conn = db.connect(self.server.translation_db_path)
            try:
                action_id, already_queued = db.queue_translation_task_recovery(
                    conn,
                    task_id,
                    now=dt.datetime.fromtimestamp(self.server.clock(), dt.UTC).isoformat(),
                    actor=self._admin_actor(),
                )
            finally:
                conn.close()
        except (RuntimeError, ValueError) as error:
            self._json(409, {"error": str(error), "category": "lifecycle"})
            return
        self.server.translation_wakeup_callback()
        self._json(
            202,
            {"ok": True, "action_id": action_id, "already_queued": already_queued},
        )

    def _delivery_status_payload(self, edition_date: str) -> dict[str, Any]:
        if self.server.db_path is None:
            return {"summary": {}, "states": []}
        conn = db.connect(self.server.db_path)
        try:
            summary = db.delivery_summary(conn, edition_date)
            states = db.delivery_states(conn, edition_date)
        finally:
            conn.close()
        return {
            "summary": vars(summary),
            "states": [
                {
                    "recipient_ref": state.recipient_key[:12],
                    "status": state.status,
                    "error_category": state.error_category,
                    "updated_at": state.updated_at,
                    "attempt_count": state.attempt_count,
                    "run_id": state.run_id,
                    "started_at": state.started_at,
                    "finished_at": state.finished_at,
                    "degraded": state.degraded,
                }
                for state in states
            ],
        }

    def _handle_delivery_get(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        if not self._admin_ready():
            return
        try:
            release = self._current_release()
            schedule = self._schedule_payload()
            conn = db.connect(self.server.db_path)
            try:
                latest = db.latest_delivery_run(conn, mode="auto")
            finally:
                conn.close()
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(error)
            return
        try:
            preview = self._preview_saved()
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            preview = None
            preview_validation = self._preview_validation(error)
        else:
            preview_validation = self._preview_validation()
        self._json(
            200,
            {
                "latest_run": vars(latest) if latest else None,
                "current_release": {
                    "name": release.release_name,
                    "edition_date": release.release_date,
                },
                "current_preview": self._preview_metadata(preview) if preview else None,
                "preview_validation": preview_validation,
                **schedule,
                **self._delivery_status_payload(release.release_date),
                "csrf_token": self._csrf_for_response(),
            },
        )

    def _deliver(
        self, mode: str, *, smtp=None, content=None, edition_date=None, confirm_unknown=False
    ):
        database = self.server.db_path
        output_root = self.server.output_root
        if database is None or output_root is None:
            raise AdminEmailError("邮件 Admin 尚未完整接线")
        env = read_env(self._env_path())
        kwargs = {
            "output_root": output_root,
            "database": database,
            "site_url": self.server.site_url,
            "timezone": self.server.timezone,
            "smtp_config": smtp,
            "content_config": content,
            "edition_date": edition_date,
            "environ": env,
            "confirm_unknown": confirm_unknown,
            "resolver": self.server.resolver,
        }
        return self.server.delivery_callback(mode, **kwargs)

    @staticmethod
    def _report_next_action(report: DeliveryServiceReport) -> str:
        if report.unknown_count:
            return _UNKNOWN_TEST_MESSAGE_ACTION
        if not report.failed_count:
            return ""
        actions = {
            "dns": "检查 SMTP 主机 DNS 解析记录与运行环境的解析能力，修正后再测试。",
            "connect": "检查 SMTP 主机、端口、网络路由与防火墙连通性，修正后再测试。",
            "tls": "检查 SMTP 端口、加密模式与服务端 TLS 证书，修正后再测试。",
            "starttls": "检查端口与 STARTTLS 支持是否匹配，修正后再测试。",
            "auth": "检查 SMTP 用户名、授权码与服务端认证策略，修正后再测试。",
            "authentication": "检查 SMTP 用户名、授权码与服务端认证策略，修正后再测试。",
            "mail": "检查发件人地址与服务端 MAIL FROM 策略，修正后再测试。",
            "rcpt": "检查服务端 RCPT TO 拒绝或收件策略日志，修正后再测试。",
            "data_command": "检查服务端 DATA 命令拒绝、限流或内容策略日志，修正后再测试。",
        }
        return actions.get(
            report.error_stage,
            "根据发生阶段和错误分类检查 SMTP 配置及服务端日志，修正后再测试。",
        )

    @staticmethod
    def _report_payload(report: DeliveryServiceReport) -> dict[str, Any]:
        return {
            "ok": report.succeeded,
            "run_id": report.run_id,
            "release_name": report.release_name,
            "edition_date": report.edition_date,
            "mode": report.mode,
            "status": report.status,
            "total_count": report.total_count,
            "sent_count": report.sent_count,
            "failed_count": report.failed_count,
            "unknown_count": report.unknown_count,
            "skipped_count": report.skipped_count,
            "degraded": report.degraded,
            "archive_status": report.archive_status,
            "error_category": report.error_category,
            "error_stage": report.error_stage,
            "retry_allowed": report.retry_allowed,
            "next_action": PreviewHandler._report_next_action(report),
            "idempotency_warning": _IDEMPOTENCY_WARNING if report.mode == "test" else "",
            "message": report.message,
        }

    def _handle_retry_failed(self, body: dict[str, Any]) -> None:
        if set(body) != {"confirm", "edition"} or body.get("confirm") is not True:
            self._json(
                409, {"error": "仅重试失败者需要 confirm=true 与刊期", "category": "confirmation"}
            )
            return
        try:
            report = self._deliver("retry_failed", edition_date=str(body["edition"]))
        except (DeliveryServiceError, ValueError) as error:
            self._safe_error(error, status=502)
            return
        self._json(200, self._report_payload(report))

    def _handle_retry_unknown(self, body: dict[str, Any]) -> None:
        if set(body) != {"confirm", "confirm_duplicate_risk", "edition"} or (
            body.get("confirm") is not True or body.get("confirm_duplicate_risk") is not True
        ):
            self._json(
                409, {"error": "unknown 可能已送达；需要双重风险确认", "category": "confirmation"}
            )
            return
        try:
            report = self._deliver(
                "retry_unknown",
                edition_date=str(body["edition"]),
                confirm_unknown=True,
            )
        except (DeliveryServiceError, ValueError) as error:
            self._safe_error(error, status=502)
            return
        self._json(200, self._report_payload(report))

    def _manual_fingerprint(self, preview: PublishedPreview) -> str:
        material = "|".join(
            (
                "single_html_notice_v1",
                preview.release.release_name,
                preview.release.edition_sha256,
                preview.rendered.subject,
                ",".join(preview.recipient_hashes),
                hashlib.sha256(preview.rendered.html.encode("utf-8")).hexdigest(),
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def _handle_manual_preview(self, body: dict[str, Any]) -> None:
        if set(body) != {"edition"} or not isinstance(body.get("edition"), str):
            self._json(400, {"error": "指定刊期预览字段无效", "category": "configuration"})
            return
        try:
            preview = self._preview_saved(body["edition"])
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(error)
            return
        token = secrets.token_urlsafe(32)
        fingerprint = self._manual_fingerprint(preview)
        with self.server.state_lock:
            self.server.manual_previews[token] = (self.server.clock(), body["edition"], fingerprint)
        self._json(
            200,
            {**self._preview_payload(preview), "preview_token": token, "fingerprint": fingerprint},
        )

    def _handle_manual_delivery(self, body: dict[str, Any]) -> None:
        required = {"edition", "preview_token", "fingerprint", "confirm"}
        if set(body) != required or body.get("confirm") is not True:
            self._json(409, {"error": "人工投递必须先预览并显式确认", "category": "confirmation"})
            return
        try:
            preview = self._preview_saved(str(body["edition"]))
            fingerprint = self._manual_fingerprint(preview)
            with self.server.state_lock:
                saved = self.server.manual_previews.pop(str(body["preview_token"]), None)
            if (
                saved is None
                or saved[0] < self.server.clock() - _MANUAL_PREVIEW_TTL_SECONDS
                or saved[1] != body["edition"]
                or saved[2] != body["fingerprint"]
                or fingerprint != body["fingerprint"]
            ):
                raise AdminEmailError("预览 token 已失效或刊期指纹已变化", category="confirmation")
            report = self._deliver("manual", edition_date=str(body["edition"]))
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(
                error, status=409 if getattr(error, "category", "") == "confirmation" else 502
            )
            return
        self._json(200, self._report_payload(report))

    def _safe_error(self, error: BaseException, *, status: int = 400) -> None:
        category = getattr(error, "category", "configuration")
        message = str(error)
        if isinstance(error, MailError):
            message = str(error)
        self._json(status, {"error": message, "category": category})

    def _handle_public_confirm(self, raw_token: str) -> None:
        self._html(
            _public_result_page("每日简报", _PUBLIC_SUBSCRIPTION_DISABLED_MESSAGE),
            404,
        )

    def _handle_unsubscribe_get(self, raw_token: str) -> None:
        token = self._public_token(raw_token)
        if self._public_endpoint_ready() and token:
            conn = db.connect(self.server.db_path)
            try:
                subscriptions.unsubscribe_page_data(conn, token, dt.datetime.now(dt.UTC))
            finally:
                conn.close()
        self._html(_unsubscribe_page(token))

    def _handle_unsubscribe_post(self, raw_token: str) -> None:
        token = self._public_token(raw_token)
        if not self._public_request_host_valid():
            self._html(_public_result_page("退订", _PUBLIC_UNSUBSCRIBE_MESSAGE), 403)
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        length, length_error = self._public_length(64)
        valid_body = False
        if content_type == "application/x-www-form-urlencoded" and length_error is None:
            try:
                fields = parse_qs(
                    self.rfile.read(length or 0).decode("ascii"),
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                valid_body = fields == {"List-Unsubscribe": ["One-Click"]}
            except (UnicodeDecodeError, ValueError):
                valid_body = False
        if not valid_body:
            self._html(_public_result_page("退订", _PUBLIC_UNSUBSCRIBE_MESSAGE), 400)
            return
        if self._public_endpoint_ready() and token:
            conn = db.connect(self.server.db_path)
            try:
                subscriptions.unsubscribe_one_click(conn, token, dt.datetime.now(dt.UTC))
            finally:
                conn.close()
        self._html(_public_result_page("退订", _PUBLIC_UNSUBSCRIBE_MESSAGE))

    def _limited(self, _action: str, callback: Callable[[], None]) -> None:
        callback()

    def _set_session_cookie(self, token: str, max_age: int) -> None:
        cookie = (
            f"{_SESSION_COOKIE}={token}; Max-Age={max_age}; Path=/; "
            "HttpOnly; SameSite=Strict; Secure"
        )
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", cookie)
        self._security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self, body: dict[str, Any]) -> None:
        if not self._login_required:
            self._json(400, {"error": "本模式无需登录"})
            return
        if set(body) != {"username", "password"}:
            self._json(400, {"error": "登录字段无效"})
            return
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if verify_htpasswd(self.server.htpasswd_file, username, password):
            secret_file = self.server.htpasswd_file.parent / "session-secret"
            secret = _session_secret(secret_file)
            expires_at = int(time.time()) + _SESSION_TTL_SECONDS
            self._set_session_cookie(
                _sign_session(secret, username, expires_at), _SESSION_TTL_SECONDS
            )
            return

        user = None
        if self.server.db_path is not None:
            try:
                key = accounts.email_key(accounts.normalize_email(username))
            except accounts.AccountError:
                key = ""
            conn = db.connect(self.server.db_path)
            try:
                user = db.user_by_email_key(conn, key) if key else None
            finally:
                conn.close()
        stored = user.password_hash if user is not None else _SITE_ADMIN_DUMMY_PASSWORD_HASH
        password_ok = accounts.verify_password(password, stored)
        if user is None or user.status != "active" or not user.is_admin or not password_ok:
            time.sleep(0.1)
            self._json(401, {"error": "管理员账号或口令不正确"})
            return
        now = dt.datetime.now(dt.UTC)
        token = secrets.token_urlsafe(32)
        conn = db.connect(self.server.db_path)
        try:
            db.create_user_session(
                conn,
                token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                user_id=user.id,
                expires_at=(now + dt.timedelta(seconds=_SESSION_TTL_SECONDS)).isoformat(),
                now=now.isoformat(),
            )
        finally:
            conn.close()
        self._set_session_cookie(token, _SESSION_TTL_SECONDS)

    def _handle_password(self, body: dict[str, Any]) -> None:
        if self.server.htpasswd_file is None:
            self._json(404, {"error": "本模式不提供网页改密"})
            return
        if self._root_session_subject() is None:
            self._json(403, {"error": "站点管理员不能修改运维管理员口令"})
            return
        if set(body) != {"current_password", "password"}:
            self._json(400, {"error": "改密字段无效"})
            return
        password = str(body.get("password", ""))
        if len(password) < 8:
            self._json(400, {"error": "口令至少 8 位"})
            return
        username = "admin"
        if self.server.htpasswd_file.is_file():
            lines = self.server.htpasswd_file.read_text(encoding="utf-8").splitlines()
            if lines and ":" in lines[0]:
                username = lines[0].split(":", 1)[0] or "admin"
            current = str(body.get("current_password", ""))
            if not verify_htpasswd(self.server.htpasswd_file, username, current):
                time.sleep(0.1)
                self._json(401, {"error": "当前口令不正确"})
                return
        atomic_write_text(self.server.htpasswd_file, f"{username}:{apr1_hash(password)}\n")
        initial = self.server.htpasswd_file.parent / "admin-password.initial"
        if initial.is_file():
            initial.unlink()
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        if secret_file.is_file():
            secret_file.unlink()
        self._json(200, {"ok": True})

    def _handle_save(self, body: dict[str, Any]) -> None:
        name = str(body.get("name", "")).strip()
        if body.get("delete") is True:
            if set(body) != {"name", "delete"}:
                self._json(400, {"error": "删除字段无效"})
                return

            def delete(data: dict[str, Any]) -> None:
                provider = data["providers"].get(name)
                if provider is None:
                    raise AdminConfigError(f"档案不存在：{name}")
                if provider["is_default"]:
                    raise AdminConfigError("默认档案不可删除；请先设置替代项或停用翻译")
                del data["providers"][name]

            try:
                update_profiles(self.server.project_root, delete, self.server.profiles_file)
                remove_test_state(self.server.project_root, name)
            except AdminConfigError as error:
                self._json(409, {"error": str(error)})
                return
            self._json(200, {"ok": True})
            return

        try:
            current = load_profiles(self.server.project_root, self.server.profiles_file)
            existing = current["providers"].get(name)
            provider = provider_from_request(body, existing)
            if self._login_required:
                provider["base_url"] = validate_public_https_target(
                    provider["base_url"], self.server.resolver
                )
            if existing and existing["is_default"] and not provider["enabled"]:
                raise AdminConfigError("默认档案不可直接停用；请先设置替代项或显式停用翻译")

            def store(data: dict[str, Any]) -> None:
                latest = data["providers"].get(name)
                if latest and latest["is_default"] and not provider["enabled"]:
                    raise AdminConfigError("默认档案不可直接停用")
                provider["is_default"] = bool(latest and latest["is_default"])
                data["providers"][name] = provider

            updated = update_profiles(
                self.server.project_root, store, self.server.profiles_file
            )
            saved = updated["providers"][name]
            if saved["is_default"]:
                write_env_local(self.server.project_root, saved, self.server.env_file)
        except AdminConfigError as error:
            self._json(400, {"error": str(error)})
            return
        self._json(200, {"ok": True})

    def _handle_probe(self, body: dict[str, Any]) -> None:
        if not self.server.probe_lock.acquire(blocking=False):
            self._json(409, {"error": "已有测试连接任务正在运行"})
            return
        try:
            started = self.server.clock()
            deadline = time.monotonic() + 15.0
            name = str(body.get("name", "")).strip()
            current = load_profiles(self.server.project_root, self.server.profiles_file)
            existing = current["providers"].get(name)
            provider = provider_from_request(body, existing)
            if self._login_required:
                provider["base_url"] = validate_public_https_target(
                    provider["base_url"],
                    self.server.resolver,
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdminConfigError("API 测试超过硬总时限")
            result = self._run_probe(provider, started, timeout_seconds=remaining)
            result["configuration_state"] = (
                "matching_saved"
                if existing is not None
                and hmac.compare_digest(
                    result["fingerprint"],
                    provider_fingerprint(self.server.project_root, existing),
                )
                else "unsaved"
            )
            save_test_state(self.server.project_root, name, result)
            response = {key: value for key, value in result.items() if key != "fingerprint"}
            self._json(200 if result["status"] == "success" else 502, response)
        except AdminConfigError as error:
            self._json(400, {"error": str(error), "category": "configuration"})
        finally:
            self.server.probe_lock.release()

    def _run_probe(
        self,
        provider: dict[str, Any],
        started: float,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        tested_at_epoch = self.server.clock()
        base = {
            "protocol": provider["api_type"],
            "model": provider["model"],
            "tested_at_epoch": tested_at_epoch,
            "fingerprint": provider_fingerprint(self.server.project_root, provider),
        }
        try:
            output = self.server.probe_callback(
                translation_config(provider, timeout_seconds=timeout_seconds)
            )
            elapsed = max(0, int((self.server.clock() - started) * 1000))
            safe_output = str(output).replace(provider["api_key"], "[已脱敏]")
            safe_output = " ".join(safe_output.split())[:240]
            if not safe_output:
                raise TranslationError("模型返回为空", category="empty_response")
            return {
                **base,
                "status": "success",
                "category": "success",
                "upstream_status": 200,
                "elapsed_ms": elapsed,
                "connection_auth": {"status": "success", "message": "连接与鉴权成功"},
                "model_return": {"status": "success", "message": "模型已返回内容"},
                "output": safe_output,
            }
        except TranslationError as error:
            elapsed = max(0, int((self.server.clock() - started) * 1000))
            connection_failed = not error.response_started and error.category in {
                "authentication",
                "connection_timeout",
                "tls",
                "network",
                "total_timeout",
            }
            return {
                **base,
                "status": "failed",
                "category": error.category,
                "upstream_status": error.status,
                "elapsed_ms": elapsed,
                "connection_auth": {
                    "status": "failed" if connection_failed else "success",
                    "message": str(error) if connection_failed else "连接与鉴权成功",
                },
                "model_return": {
                    "status": "not_run" if connection_failed else "failed",
                    "message": "模型未返回可解析内容",
                },
                "output": "",
            }

    def _handle_default(self, body: dict[str, Any]) -> None:
        allowed = {"name", "expected_fingerprint", "confirm_untested"}
        if set(body) - allowed or set(body) - {"confirm_untested"} != {
            "name",
            "expected_fingerprint",
        }:
            self._json(400, {"error": "设为默认字段无效"})
            return
        name = str(body.get("name", "")).strip()
        expected_fingerprint = str(body.get("expected_fingerprint", ""))
        if len(expected_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in expected_fingerprint
        ):
            self._json(400, {"error": "档案配置指纹无效"})
            return
        confirmation_required = False
        configuration_changed = False
        try:
            data = load_profiles(self.server.project_root, self.server.profiles_file)
            provider = data["providers"].get(name)
            if provider is None:
                self._json(404, {"error": f"档案不存在：{name}"})
                return
            if not provider["enabled"]:
                raise AdminConfigError("只有已启用档案才能设为默认")
            if not hmac.compare_digest(
                expected_fingerprint,
                provider_fingerprint(self.server.project_root, provider),
            ):
                self._json(409, {"error": "档案已被修改；请刷新后重新确认"})
                return
            if self._login_required:
                provider["base_url"] = validate_public_https_target(
                    provider["base_url"], self.server.resolver
                )

            def select(item: dict[str, Any]) -> None:
                nonlocal confirmation_required, configuration_changed
                latest = item["providers"].get(name)
                if latest is None or not hmac.compare_digest(
                    expected_fingerprint,
                    provider_fingerprint(self.server.project_root, latest),
                ):
                    configuration_changed = True
                    raise AdminConfigError("档案已被修改或删除；请刷新后重新确认")
                if not latest["enabled"]:
                    raise AdminConfigError("只有已启用档案才能设为默认")
                tested = assert_recent_success(
                    self.server.project_root,
                    latest,
                    max_age_seconds=_TEST_MAX_AGE_SECONDS,
                )
                if not tested and body.get("confirm_untested") is not True:
                    confirmation_required = True
                    raise AdminConfigError("该档案尚无未过期的成功测试；确认风险后重试")
                for candidate in item["providers"].values():
                    candidate["is_default"] = candidate["name"] == name

            updated = update_profiles(
                self.server.project_root,
                select,
                self.server.profiles_file,
            )
            selected = default_provider(updated)
            write_env_local(self.server.project_root, selected, self.server.env_file)
        except AdminConfigError as error:
            if confirmation_required:
                self._json(
                    409,
                    {"error": str(error), "confirmation_required": True},
                )
            elif configuration_changed:
                self._json(409, {"error": str(error)})
            else:
                self._json(400, {"error": str(error)})
            return
        self._json(200, {"ok": True, "active": name})

    def _handle_disable_translation(self, body: dict[str, Any]) -> None:
        if body != {"confirm": True}:
            self._json(409, {"error": "显式停用翻译需要 confirm=true"})
            return

        def disable(data: dict[str, Any]) -> None:
            for provider in data["providers"].values():
                provider["is_default"] = False

        update_profiles(self.server.project_root, disable, self.server.profiles_file)
        empty_provider = {
            "base_url": "",
            "api_key": "",
            "model": "",
            "api_type": "openai_chat",
            "stream": True,
        }
        write_env_local(self.server.project_root, empty_provider, self.server.env_file)
        self._json(200, {"ok": True, "active": ""})

    def _json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self._security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return


def create_server(
    root: Path,
    site_dir: Path,
    port: int,
    *,
    env_file: str = ENV_FILE,
    profiles_file: str = PROFILES_FILE,
    serve_static: bool = True,
    htpasswd_file: Path | None = None,
    db_path: Path | None = None,
    translation_db_path: Path | None = None,
    site_url: str = "",
    output_root: Path | None = None,
    timezone: str = "Asia/Shanghai",
    confirmation_sender: Callable[[SmtpConfig, str, str], DeliveryReport | None] | None = None,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
    probe_callback: Callable[[TranslationConfig], str] | None = None,
    smtp_test_callback: Callable[[SmtpConfig, Any], None] | None = None,
    smtp_smoke_callback: Callable[[SmtpConfig, Any], DeliveryReport] | None = None,
    delivery_callback: Callable[..., DeliveryServiceReport] | None = None,
    translation_wakeup_callback: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.time,
    sensitive_limit: int = 4,
    sensitive_window: float = 60.0,
    public_subscription_enabled: bool = True,
    loopback_public_subscription: bool = False,
    loopback_browser_compat: bool = False,
    site_env_path: Path | None = None,
) -> ThreadingHTTPServer:
    if loopback_public_subscription and not serve_static:
        raise ValueError("loopback public subscriptions require static preview mode")
    handler_class = partial(PreviewHandler, directory=str(site_dir))
    server = _AdminServer(("127.0.0.1", port), handler_class)
    server.project_root = Path(root)
    server.env_file = env_file
    server.profiles_file = profiles_file
    server.serve_static_files = serve_static
    server.loopback_public_subscription = loopback_public_subscription
    server.loopback_browser_compat = loopback_browser_compat
    server.htpasswd_file = Path(htpasswd_file) if htpasswd_file is not None else None
    server.db_path = Path(db_path) if db_path is not None else None
    server.translation_db_path = (
        Path(translation_db_path) if translation_db_path is not None else server.db_path
    )
    server.site_url = site_url
    loopback_origin = ("http", "127.0.0.1", server.server_port)
    server.admin_origin = (
        _request_origin(site_url)
        if server.htpasswd_file is not None and site_url
        else loopback_origin
    )
    server.public_origin = (
        loopback_origin
        if loopback_public_subscription or not site_url
        else _request_origin(site_url)
    )
    server.output_root = Path(output_root) if output_root is not None else None
    server.timezone = timezone
    server.public_secret = secrets.token_bytes(32)
    server.confirmation_sender = confirmation_sender or _default_confirmation_sender
    server.resolver = resolver
    server.probe_callback = probe_callback or _default_probe
    server.smtp_test_callback = smtp_test_callback or _default_smtp_test
    server.smtp_smoke_callback = smtp_smoke_callback or _default_smtp_smoke
    server.delivery_callback = delivery_callback or _default_delivery
    server.translation_wakeup_callback = translation_wakeup_callback or (lambda: None)
    server.clock = clock
    server.sensitive_limit = sensitive_limit
    server.sensitive_window = sensitive_window
    server.public_subscription_enabled = public_subscription_enabled
    server.rate_lock = threading.Lock()
    server.rate_events = defaultdict(deque)
    server.probe_lock = threading.Lock()
    server.smtp_lock = threading.Lock()
    server.state_lock = threading.Lock()
    server.manual_previews = {}
    server.site_env_path = Path(site_env_path) if site_env_path is not None else None
    if server.db_path is not None:
        database_existed = server.db_path.exists()
        conn = db.connect(server.db_path)
        try:
            now = dt.datetime.fromtimestamp(clock(), dt.UTC)
            legacy_recipients = smtp_config_from_env(
                read_env(server.project_root / server.env_file)
            ).recipients
            if legacy_recipients:
                subscriptions.import_legacy_smtp_recipients_once(
                    conn, legacy_recipients, now
                )
            if database_existed:
                db.recover_interrupted_test_attempts(
                    conn, now.isoformat(timespec="seconds")
                )
        finally:
            conn.close()
    return server


def _public_result_page(title: str, message: str) -> str:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{safe_title} · Cheapcoding News</title>
<style>:root{{color-scheme:light}}body{{margin:0;background:#fbfaf7;color:#1c1b17;
font:16px/1.7 sans-serif;min-height:100vh;display:grid;place-items:center}}main{{width:min(32rem,
calc(100% - 3rem));border-block:3px double;padding:2rem 0}}h1{{font:1.8rem Georgia,serif}}
a{{color:#8f261d}}</style></head>
<body><main><h1>{safe_title}</h1><p>{safe_message}</p><p><a href="/">返回今日简报</a></p>
</main></body></html>"""


def _unsubscribe_page(token: str) -> str:
    safe_token = html.escape(token, quote=True)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>退订 · Cheapcoding News</title>
<style>:root{{color-scheme:light}}body{{margin:0;background:#fbfaf7;color:#1c1b17;
font:16px/1.7 sans-serif;min-height:100vh;display:grid;place-items:center}}main{{width:min(32rem,
calc(100% - 3rem));border-block:3px double;padding:2rem 0}}h1{{font:1.8rem Georgia,serif}}
button{{font:inherit;background:#1c1b17;color:#fff;border:0;padding:.65rem 1.2rem;cursor:pointer}}
button:focus-visible{{outline:3px solid #ae2f24;outline-offset:2px}}</style></head>
<body><main><h1>退订邮件简报</h1><p>{html.escape(_PUBLIC_UNSUBSCRIBE_MESSAGE)}</p>
<form method="post" action="/unsubscribe/{safe_token}">
<input type="hidden" name="List-Unsubscribe" value="One-Click">
<button type="submit">确认退订</button></form></main></body></html>"""


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>管理登录 · Cheapcoding News</title>
<style>
:root { color-scheme: light; }
body {
  margin: 0; background: #fbfaf7; color: #1c1b17; min-height: 100vh;
  display: grid; place-items: center; font: 15px/1.7 sans-serif;
}
.card {
  width: min(22rem, calc(100% - 3rem)); padding: 2rem 0;
  border-block: 3px double #1c1b17;
}
h1 { font: 1.55rem Georgia, serif; margin: 0; }
.sub, label { color: #5c5a52; font-size: .82rem; }
label { display: block; margin-top: .8rem; }
input, button {
  box-sizing: border-box; width: 100%; font: inherit; padding: .5rem;
  border: 1px solid #cbc5b8;
}
button { margin-top: 1rem; background: #1c1b17; color: #fff; cursor: pointer; }
input:focus-visible, button:focus-visible {
  outline: 3px solid #ae2f24; outline-offset: 2px;
}
#status { color: #9f261d; min-height: 1.5rem; }
</style>
</head>
<body>
<form class="card" id="login-form">
  <h1>Cheapcoding News</h1>
  <p class="sub">模型接口 · 管理登录</p>
  <label for="username">管理员账号（admin 或已授权邮箱）</label>
  <input id="username" autocomplete="username" value="admin">
  <label for="password">口令</label>
  <input id="password" type="password" autocomplete="current-password" autofocus>
  <button type="submit">登录</button>
  <p id="status" role="status"></p>
</form>
<script>
"use strict";
document.getElementById("login-form").addEventListener("submit", function (event) {
  event.preventDefault();
  var status = document.getElementById("status");
  status.textContent = "";
  fetch("/admin/api/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      username: document.getElementById("username").value,
      password: document.getElementById("password").value
    })
  }).then(function (response) {
    if (response.ok) {
      location.reload();
      return null;
    }
    return response.json().then(function (data) {
      throw new Error(data.error || ("HTTP " + response.status));
    });
  }).catch(function (error) {
    status.textContent = error.message;
  });
});
</script>
</body>
</html>
"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>编辑台 · Cheapcoding News 管理</title>
<style>
:root {
  color-scheme: light;
  --paper: #fbfaf7; --ink: #1c1b17; --cinnabar: #ae2f24;
  --cinnabar-bright: #c8402f; --muted: #5c5a52; --line: #ddd8cc;
  --panel: #f4f1e8; --sheet: #fffefb; --success: #24613f; --alert: #9f261d;
  --font-display: Constantia, "Palatino Linotype", Georgia, "Noto Serif SC", serif;
  --font-ui: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  --font-data: Consolas, "Cascadia Mono", "Courier New", monospace;
}
* { box-sizing: border-box; letter-spacing: 0; }
html { background: var(--paper); }
body { margin: 0; background: var(--paper); color: var(--ink); font: 15px/1.6 var(--font-ui); }
.wrap { max-width: 76rem; margin: auto; padding: 1.15rem 1.2rem 5rem; }
h1 { margin: 0; font: 700 1.72rem/1.15 var(--font-display); }
h2 { margin: 0 0 .35rem; font: 700 1.3rem/1.25 var(--font-display); }
h3 { margin: 0 0 .6rem; font-size: .94rem; }
p { overflow-wrap: anywhere; }
code, pre { font-family: var(--font-data); overflow-wrap: anywhere; white-space: pre-wrap; }
.note, .meta { color: var(--muted); font-size: .8rem; }
.mast {
  display: flex; align-items: center; justify-content: space-between; gap: .8rem; min-width: 0;
  border-bottom: 3px double var(--ink); padding: .5rem 0 .9rem;
}
.mast-brand { display: flex; align-items: center; gap: .8rem; min-width: 0; }
.mast-actions { flex: none; }
.mast-home-btn {
  display: inline-flex; align-items: center; justify-content: center;
  padding: .35rem .75rem; font: 700 .82rem/1.2 var(--font-data);
  color: var(--ink); background: var(--sheet); border: 1px solid var(--line);
  text-decoration: none; transition: background .18s ease, border-color .18s ease, color .18s ease;
}
.mast-home-btn:hover {
  background: var(--ink); color: var(--paper); border-color: var(--ink);
}
.panel-head-controls { display: flex; align-items: center; justify-content: space-between; gap: .6rem; margin-bottom: .6rem; }
.panel-head-controls h3 { margin: 0; }
.orders-filter-bar {
  display: grid; grid-template-columns: minmax(0, 1.8fr) minmax(7.5rem, .8fr); gap: .5rem; margin-bottom: .8rem;
}
.table-scroll { max-height: 480px; overflow: auto; border: 1px solid var(--line); background: #fff; }
.admin-brandmark { flex: none; width: 2.65rem; height: 2.65rem; }
.mast-copy { min-width: 0; }
.mast-kicker { margin: 0 0 .08rem; color: var(--cinnabar); font: 700 .68rem/1.3 var(--font-data); }
.mast-title { overflow-wrap: anywhere; }
.nav {
  display: flex; gap: .25rem; overflow-x: auto; padding: .55rem 0 1.25rem;
  position: sticky; top: 0; background: color-mix(in srgb, var(--paper) 96%, transparent);
  border-bottom: 1px solid var(--line); z-index: 2; scrollbar-width: thin;
}
.nav button { flex: 0 0 auto; min-height: 2.35rem; white-space: nowrap; border-color: transparent; background: transparent; }
.nav button:hover { border-color: var(--line); }
.nav button[aria-selected="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
.workspace[hidden] { display: none; }
.workspace { min-width: 0; }
.workspace > .note { max-width: 64rem; margin: .35rem 0 1rem; }
.grid {
  display: grid; grid-template-columns: minmax(19rem, 1fr) minmax(20rem, 1.2fr);
  gap: 1.2rem;
}
.stack { display: grid; grid-template-columns: minmax(0, 1fr); gap: 1.2rem; }
.panel { min-width: 0; background: var(--sheet); border: 1px solid var(--line); padding: 1rem; }
.provider {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .3rem .8rem;
  border-bottom: 1px dotted var(--line); padding: .72rem .25rem; cursor: pointer;
  transition: border-color .2s ease, background .2s ease;
}
.provider:hover { border-bottom-color: var(--cinnabar); background: var(--panel); }
.provider > .meta { grid-column: 1 / -1; min-width: 0; }
.provider:focus-visible, input:focus-visible, textarea:focus-visible,
select:focus-visible, button:focus-visible {
  outline: 2px solid var(--cinnabar); outline-offset: 2px;
}
.provider strong, .provider .meta, td, .result { min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
label { display: block; color: var(--muted); font-size: .78rem; margin-top: .65rem; }
input, textarea, select, button {
  min-height: 2.55rem; border: 1px solid #bcb6aa; border-radius: 0;
  padding: .5rem .62rem; background: #fff; color: var(--ink); font: inherit;
  transition: border-color .18s ease, background .18s ease, color .18s ease, opacity .18s ease;
}
input, textarea, select { box-sizing: border-box; width: 100%; }
input[type="checkbox"] {
  width: 1rem; height: 1rem; min-height: 1rem; padding: 0;
  vertical-align: -.12rem; accent-color: var(--cinnabar);
}
input:hover, textarea:hover, select:hover { border-color: var(--ink); }
textarea { min-height: 6rem; resize: vertical; }
.checks { display: flex; flex-wrap: wrap; align-items: center; gap: .35rem 1rem; margin-top: .55rem; }
.checks label { display: inline-flex; align-items: center; gap: .35rem; margin: 0; color: var(--ink); }
.checks input { width: 1rem; min-height: 1rem; }
.actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 9.5rem), 1fr)); gap: .5rem; margin-top: 1rem; }
.payment-actions { grid-template-columns: minmax(0, 1fr); width: 14rem; max-width: 100%; margin: 0; text-align: left; white-space: normal; overflow-wrap: anywhere; }
.payment-action summary { cursor: pointer; }
.payment-action button { width: 100%; margin-top: .65rem; }
.grant-controls { display: grid; grid-template-columns: minmax(5.5rem, .8fr) minmax(5rem, .65fr) minmax(7rem, 1fr); gap: .35rem; }
.grant-controls input, .grant-controls select, .grant-controls button { min-width: 0; }
.user-list-tools {
  display: grid; grid-template-columns: minmax(14rem, 1fr) auto;
  align-items: end; gap: .65rem 1rem; margin-top: .7rem;
}
.user-list-tools label { margin: 0; }
.user-page-controls { display: flex; align-items: center; justify-content: flex-end; gap: .45rem; }
.user-page-controls button { min-width: 4.5rem; }
#users-page { min-width: 5.5rem; text-align: center; }
#users-summary { margin: .7rem 0 .35rem; }
button { cursor: pointer; }
button:hover:not(:disabled) { border-color: var(--ink); }
button:active:not(:disabled) { background: var(--panel); }
button:disabled { cursor: not-allowed; opacity: .55; }
button[aria-busy="true"] { cursor: wait; opacity: .78; }
button[aria-busy="true"]::after {
  content: ""; display: inline-block; width: .72rem; height: .72rem; margin-left: .45rem;
  border: 1px solid currentColor; border-right-color: transparent; border-radius: 50%; vertical-align: -.08rem;
  animation: admin-spin .7s linear infinite;
}
.primary { background: var(--ink); border-color: var(--ink); color: var(--paper); }
.primary:hover:not(:disabled) { background: #34322c; border-color: #34322c; }
.danger { color: var(--alert); }
.badge { align-self: start; font-size: .7rem; border: 1px solid var(--line); padding: .06rem .35rem; white-space: nowrap; }
.result { margin-top: 1rem; border-left: 3px solid var(--cinnabar); padding: .75rem .85rem; background: var(--panel); white-space: pre-wrap; overflow: auto; }
#status {
  margin: .75rem 0; border: 1px solid currentColor; border-left-width: 3px;
  padding: .65rem .8rem; background: var(--sheet); font-weight: 600;
  overflow-wrap: anywhere;
}
#status:empty { display: none; }
.ok { color: var(--success); }
.err { color: var(--alert); }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .15rem .9rem; }
.span2 { grid-column: 1 / -1; }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .6rem; }
.stat { min-width: 0; border-top: 3px solid var(--cinnabar); background: var(--panel); padding: .65rem; overflow-wrap: anywhere; }
.stat strong { display: block; font: 700 1.35rem/1.2 var(--font-display); }
.translation-head { display: flex; align-items: end; justify-content: space-between; gap: .8rem; margin-bottom: .75rem; }
.translation-controls { display: flex; align-items: end; gap: .45rem; flex-wrap: wrap; }
.translation-controls label { margin: 0; }
.translation-controls select { min-width: 9.5rem; }
.translation-provider {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .25rem .8rem;
  border-block: 1px solid var(--line); padding: .7rem 0; margin-bottom: .8rem;
}
.translation-provider > div { min-width: 0; }
.translation-provider strong, .translation-provider .meta { display: block; overflow-wrap: anywhere; }
.translation-provider button { align-self: center; min-width: 7.5rem; }
.translation-stats { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-bottom: .8rem; }
.segmented { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: .7rem; }
.segmented button { min-height: 2.15rem; padding: .35rem .65rem; background: transparent; }
.segmented button[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
#translation-list { max-width: 100%; overflow-x: auto; min-height: 10rem; }
.translation-title {
  display: block; max-width: 24rem; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-weight: 600;
}
.translation-stage { display: grid; gap: .08rem; min-width: 9rem; }
.translation-stage small, .translation-error small { color: var(--muted); }
.translation-error { display: grid; gap: .08rem; min-width: 8.5rem; }
.translation-actions { display: flex; flex-wrap: wrap; gap: .35rem; min-width: 7.5rem; }
.translation-actions button { min-height: 2.15rem; padding: .35rem .5rem; white-space: nowrap; }
#delivery-summary { min-width: 0; }
#delivery-summary strong { display: block; overflow-wrap: anywhere; word-break: break-word; }
#delivery-list, #site-users, #site-orders, #site-codes {
  max-width: 100%; overflow-x: auto;
}
table { width: 100%; border-collapse: collapse; font-size: .82rem; }
th, td { text-align: left; border-bottom: 1px solid var(--line); padding: .45rem; }
th { color: var(--muted); font-size: .72rem; font-weight: 600; white-space: nowrap; }
.preview-frame { width: 100%; min-height: 30rem; border: 1px solid var(--line); background: #fff; }
.result pre { max-width: 100%; overflow: auto; }
@keyframes admin-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: no-preference) {
  .workspace:not([hidden]) { animation: workspace-in .22s ease-out both; }
  .result:not([hidden]), #status:not(:empty) { animation: feedback-in .2s ease-out both; }
  @keyframes workspace-in { from { opacity: 0; transform: translateY(5px); } }
  @keyframes feedback-in { from { opacity: 0; transform: translateY(4px); } }
  .result:not([hidden]) { animation-name: result-in; }
  @keyframes result-in { from { opacity: 0; transform: translateY(4px); } }
}
@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
}
@media (min-width: 821px) {
  .data-table td { white-space: nowrap; overflow-wrap: normal; word-break: normal; }
  .data-table td[data-label="标识"],
  .data-table td[data-label="收件人标识"],
  .data-table td[data-label="错误分类"],
  .data-table td[data-label="错误代码"],
  .data-table td[data-label="下一次重试"] {
    white-space: normal; overflow-wrap: anywhere; word-break: break-word;
  }
}
@media (max-width: 820px) {
  .data-table, .data-table tbody, .data-table tr, .data-table td { display: block; width: 100%; }
  .data-table thead {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
  }
  .data-table tr { padding: .55rem 0; border-bottom: 1px solid var(--line); }
  .data-table td {
    display: grid; grid-template-columns: minmax(5.5rem, .28fr) minmax(0, 1fr);
    gap: .55rem; border: 0; padding: .24rem 0; text-align: right;
  }
  .data-table td::before { content: attr(data-label); color: var(--muted); font-size: .72rem; text-align: left; }
  .data-table td > button { justify-self: end; min-height: 2.2rem; }
}
@media (max-width: 640px) {
  .fields { grid-template-columns: 1fr; }
  .span2 { grid-column: auto; }
  .stats { grid-template-columns: repeat(2, 1fr); }
  .translation-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .translation-head { align-items: start; }
  .translation-provider { grid-template-columns: 1fr; }
  .translation-provider button { width: 100%; }
  .translation-title {
    max-width: none; white-space: normal; display: -webkit-box; -webkit-box-orient: vertical;
    -webkit-line-clamp: 2; line-clamp: 2;
  }
  .translation-actions { justify-content: flex-end; }
  .user-list-tools { grid-template-columns: 1fr; }
  .user-page-controls {
    display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  }
  .user-page-controls button { min-width: 0; }
  #users-refresh { grid-column: 1 / -1; }
  #users-page { min-width: 0; white-space: nowrap; }
  .wrap { padding-inline: .9rem; }
}
@media (max-width: 520px) {
  .wrap { padding: .7rem .75rem 5rem; }
  h1 { font-size: 1.42rem; }
  .admin-brandmark { width: 2.3rem; height: 2.3rem; }
  .mast { gap: .65rem; padding-bottom: .7rem; }
  .nav { margin-inline: -.75rem; padding-inline: .75rem; }
  .nav button { padding-inline: .58rem; }
  .panel { padding: .8rem; }
  .actions { grid-template-columns: 1fr; }
  .grant-controls { grid-template-columns: 1fr; }
  .data-table td { grid-template-columns: minmax(4.8rem, .38fr) minmax(0, 1fr); }
}
.epay-type-picker { grid-column: span 2; margin: .15rem 0 .45rem; }
.epay-type-title { display: block; font-size: .82rem; font-weight: 600; color: var(--ink); margin-bottom: .35rem; }
.epay-channels-group { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
.epay-channel-btn {
  display: inline-flex !important; align-items: center; gap: .4rem; padding: .28rem .65rem !important;
  border-radius: 3px; font-size: .8rem !important; font-weight: 700 !important; cursor: pointer;
  border: 1.5px solid #383838 !important; transition: all .18s ease; user-select: none;
  background: #1c1b17 !important; color: #8c887b !important; box-shadow: 0 1px 3px rgba(0,0,0,.15);
}
.epay-channel-btn .channel-icon {
  display: inline-flex; align-items: center; justify-content: center; width: 1.15rem; height: 1.15rem;
  border-radius: 50%; font-size: .72rem; font-weight: 900; color: #fff; opacity: .6;
}
.epay-channel-btn.active {
  background: #b35c00 !important; color: #fff !important; border-color: #994e00 !important;
  box-shadow: 0 2px 8px rgba(179,92,0,.4);
}
.epay-channel-btn.active .channel-icon { opacity: 1; }
.epay-channel-btn:hover { transform: translateY(-1px); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important; transition: none !important; animation: none !important;
  }
}
</style>
</head>
<body>
<main class="wrap">
  <header class="mast">
    <div class="mast-brand">
      <svg class="admin-brandmark" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
        <g fill="#1c1b17">
          <rect x="6" y="14" width="42" height="7" rx="1.5"/><rect x="6" y="34" width="34" height="7" rx="1.5"/>
          <rect x="6" y="54" width="42" height="7" rx="1.5"/><rect x="6" y="74" width="26" height="7" rx="1.5"/>
          <rect x="70" y="14" width="9" height="34" rx="1.5"/><rect x="70" y="66" width="9" height="20" rx="1.5"/>
          <rect x="86" y="14" width="7" height="72" rx="1.5"/>
        </g>
        <rect x="52" y="54" width="27" height="7" rx="1.5" fill="#ae2f24"/>
      </svg>
      <div class="mast-copy">
        <p class="mast-kicker">EDITORIAL DESK</p>
        <h1 class="mast-title">Cheapcoding News 编辑台</h1>
      </div>
    </div>
    <div class="mast-actions">
      <a href="/" class="mast-home-btn" title="返回 Cheapcoding News 主页">返回主站</a>
    </div>
  </header>
  <details id="operations-status">
    <summary>运行状态</summary>
    <p id="operations-summary" aria-live="polite"></p>
    <p id="operations-payment-slots"></p>
    <div class="table-scroll" id="operations-sources"></div>
    <div class="table-scroll" id="operations-selection"></div>
  </details>
  <nav class="nav" role="tablist" aria-label="管理职责">
    <button class="tab" id="tab-models" role="tab" data-tab="models" aria-controls="models" aria-selected="true">模型接口</button>
    <button class="tab" id="tab-mail" role="tab" data-tab="mail" aria-controls="mail" aria-selected="false">邮件设置</button>
    <button class="tab" id="tab-users" role="tab" data-tab="users" aria-controls="users" aria-selected="false">用户管理</button>
    <button class="tab" id="tab-site" role="tab" data-tab="site" aria-controls="site" aria-selected="false">付费管理</button>
    <button class="tab" id="tab-translations" role="tab" data-tab="translations" aria-controls="translations" aria-selected="false">翻译状态</button>
    <button class="tab" id="tab-delivery" role="tab" data-tab="delivery" aria-controls="delivery" aria-selected="false">投递状态</button>
  </nav>
  <section class="workspace" id="models" role="tabpanel" aria-labelledby="tab-models">
    <h2 id="models-heading">模型接口</h2>
    <p class="note">
      档案按显式协议完整切换。测试连接固定发送一次 <code>Hi</code>，最多返回少量文字，
      可能产生费用；不保存档案、不切换默认项。不提供任意测试消息输入。
    </p>
    <div class="grid">
      <section class="panel">
        <h3>供应商档案</h3>
        <div id="providers" aria-live="polite"></div>
      </section>
      <section class="panel">
        <h3>新增 / 编辑</h3>
        <p class="meta" id="dirty">当前表单尚未保存</p>
        <label for="f-name">名称</label>
        <input id="f-name" autocomplete="off">
        <label for="f-url">API Base URL（仅 HTTPS base，不填操作 endpoint）</label>
        <input id="f-url" autocomplete="off" placeholder="https://api.example.com/v1">
        <label for="f-key">API Key（编辑既有档案时留空沿用）</label>
        <input id="f-key" type="password" autocomplete="new-password">
        <label for="f-model">模型</label>
        <input id="f-model" autocomplete="off">
        <label for="f-type">协议</label>
        <select id="f-type">
          <option value="openai_chat">OpenAI Chat</option>
          <option value="anthropic_messages">Anthropic Messages</option>
        </select>
        <label for="f-reasoning">GPT 推理强度</label>
        <select id="f-reasoning">
          <option value="">自动（不发送参数）</option>
          <option value="none">none</option>
          <option value="minimal">minimal</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
          <option value="xhigh">xhigh</option>
          <option value="max">max</option>
        </select>
        <p class="meta">仅 OpenAI GPT 模型发送该参数；其他协议或模型自动忽略。</p>
        <div class="checks">
          <label><input id="f-stream" type="checkbox" checked> 流式</label>
          <label><input id="f-enabled" type="checkbox" checked> 启用</label>
        </div>
        <div class="actions">
          <button id="test">测试连接</button>
          <button id="save" class="primary">保存更改</button>
          <button id="default">设为默认</button>
          <button id="delete" class="danger">删除档案</button>
          <button id="disable" class="danger">停用翻译</button>
        </div>
        <div id="test-result" class="result" hidden></div>
      </section>
    </div>
  </section>

  <section class="workspace" id="mail" role="tabpanel" aria-labelledby="tab-mail" hidden>
    <h2 id="mail-heading">邮件设置</h2>
    <p id="mail-schedule" class="note">每日 08:00；正在读取生产时区与当前刊期。</p>
    <section class="panel">
      <div class="fields">
        <div><label for="m-host">SMTP 域名</label><input id="m-host" autocomplete="off" placeholder="smtp.example.com"></div>
        <div><label for="m-port">端口</label><input id="m-port" type="number" min="1" max="65535"></div>
        <div><label for="m-security">加密方式</label><select id="m-security"><option value="implicit_tls">SSL/TLS（常见 465）</option><option value="starttls">STARTTLS（常见 587/2525）</option></select></div>
        <div><label for="m-user">用户名</label><input id="m-user" autocomplete="off"></div>
        <div><label for="m-password">密码（留空沿用已保存值）</label><input id="m-password" type="password" autocomplete="new-password"><span id="m-password-state" class="meta"></span></div>
        <div><label for="m-sender">发件地址</label><input id="m-sender" type="email"></div>
        <div class="checks span2"><label><input id="m-mains" type="checkbox"> 主文</label><label><input id="m-briefs" type="checkbox"> 简讯</label></div>
        <div><label for="m-main-limit">主文上限</label><input id="m-main-limit" type="number" min="0"></div>
        <div><label for="m-brief-limit">简讯上限</label><input id="m-brief-limit" type="number" min="0"></div>
        <div><label for="m-language">语言</label><select id="m-language"><option value="bi">双语</option><option value="zh">仅中文</option><option value="en">仅英文</option></select></div>
        <div><label for="m-layout">版式</label><select id="m-layout"><option value="digest">摘要导读</option><option value="compact">紧凑列表</option></select></div>
        <div><label for="m-summary">摘要长度</label><select id="m-summary"><option value="short">短</option><option value="standard">标准</option><option value="long">长</option></select></div>
        <div><label for="m-catchup">自动补跑窗口（小时）</label><input id="m-catchup" type="number" min="0" max="24"></div>
        <div class="span2"><label for="m-sources">当前刊期来源筛选（Ctrl/Cmd 多选；空为全部）</label><select id="m-sources" multiple size="5"></select></div>
        <div class="checks span2"><label><input id="m-enabled" type="checkbox"> 开启每日投递</label></div>
      </div>
      <p id="mail-estimate" class="note" aria-live="polite"></p>
      <p class="meta">端口统一允许 1–65535；常见组合为 465 + SSL/TLS、587 + STARTTLS、2525 + 供应商指定加密。</p>
      <div class="actions">
        <button id="mail-test">测试连接</button>
        <button id="mail-save" class="primary">保存更改</button>
        <button id="mail-preview">预览邮件</button>
        <button id="mail-clear-password" class="danger">清除已保存密码</button>
      </div>
      <div id="mail-result" class="result" hidden></div>
    </section>
  </section>

  <section class="workspace" id="users" role="tabpanel" aria-labelledby="tab-users" hidden>
    <h2 id="users-heading">用户管理</h2>
    <p class="note">统一管理账号状态、管理员权限、会员计划、有效期和每日简报。正式邮件仅投递已开启简报且会员有效的账号。</p>
    <section class="panel">
      <h3>用户账号</h3>
      <div class="user-list-tools">
        <label for="users-search">搜索用户邮箱
          <input id="users-search" type="search" autocomplete="off" placeholder="输入完整或部分邮箱">
        </label>
        <div class="user-page-controls" aria-label="用户列表分页">
          <button id="users-refresh">刷新</button>
          <button id="users-prev">上一页</button>
          <span id="users-page" class="meta" aria-live="polite">第 1 / 1 页</span>
          <button id="users-next">下一页</button>
        </div>
      </div>
      <p id="users-summary" class="note" aria-live="polite">正在读取用户列表。</p>
      <div id="site-users" aria-live="polite"></div>
    </section>
  </section>

  <section class="workspace" id="site" role="tabpanel" aria-labelledby="tab-site" hidden>
    <h2 id="site-heading">付费管理</h2>
    <p class="note">管理会员定价、自动支付订单、卡密和公开付费开关。在线支付成功后由网关回调自动开通，无需人工审批；未使用卡密会在后台持续明文显示。</p>
    <section class="panel">
      <h3>站点设置</h3>
      <div class="fields">
        <label><input id="site-paywall" type="checkbox"> 开启付费墙</label>
        <label class="span2">联系工单邮箱<input id="site-contact-email" type="email" placeholder="如 support@example.com，用于接收读者工单与反馈"></label>
        <label>月刊会员划线基准价(元)<input id="site-monthly" type="number" min="0.11" max="100000" step="0.01" inputmode="decimal"></label>
        <label>月刊会员现价(元)<input id="site-monthly-sale" type="number" min="0.11" max="100000" step="0.01" inputmode="decimal"><output id="site-monthly-discount-preview" class="note"></output></label>
        <label>年刊会员划线基准价(元)<input id="site-yearly" type="number" min="0.11" max="100000" step="0.01" inputmode="decimal"></label>
        <label>年刊会员现价(元)<input id="site-yearly-sale" type="number" min="0.11" max="100000" step="0.01" inputmode="decimal"><output id="site-yearly-discount-preview" class="note"></output></label>
      </div>
      <div class="actions"><button id="site-settings-save" class="primary">保存设置</button><button id="site-refresh">刷新</button></div>
    </section>
    <section class="panel">
      <h3>EasyPay 支付网关</h3>
      <p class="note">兼容 EasyPay API 下单（mapi.php）。PKey 留空表示保留已保存值，页面不会回显密钥。</p>
      <div class="fields">
        <label><input id="site-epay-enabled" type="checkbox"> 启用在线支付</label>
        <div class="epay-type-picker span2">
          <span class="epay-type-title">支付通道（点击切换，选上为金底，支持多选或单选）</span>
          <div class="epay-channels-group">
            <button type="button" id="site-epay-type-alipay" class="epay-channel-btn active" data-type="alipay">
              <span class="channel-icon" style="background:#1677ff;">支</span> 支付宝
            </button>
            <button type="button" id="site-epay-type-wxpay" class="epay-channel-btn active" data-type="wxpay">
              <span class="channel-icon" style="background:#07c160;">微</span> 微信支付
            </button>
          </div>
          <input type="hidden" id="site-epay-type" value="alipay,wxpay">
        </div>
        <label class="span2">API 地址<input id="site-epay-base" type="url" placeholder="https://pay.example.com"></label>
        <label>商户 ID（PID）<input id="site-epay-pid" autocomplete="off"></label>
        <label>商户密钥（PKey）<input id="site-epay-pkey" type="password" autocomplete="new-password" placeholder="留空保留已保存值"></label>
        <label>订单有效期（秒）<input id="site-epay-ttl" type="number" min="60" max="3600" step="1"></label>
        <label>金额冻结期（秒）<input id="site-epay-hold" type="number" min="60" max="86400" step="1"></label>
        <p class="span2 meta" id="site-epay-pkey-status"></p>
        <p class="span2 meta">异步通知：<code id="site-epay-notify"></code><br>同步返回：<code id="site-epay-return"></code></p>
      </div>
      <div class="actions"><button id="site-epay-save" class="primary">保存支付配置</button><button id="site-epay-clear" class="danger">清除 PKey 并停用</button></div>
    </section>
    <div class="stack" id="site-management-sections">
      <section class="panel">
        <div class="panel-head-controls">
          <h3>支付订单</h3>
          <p id="orders-count" class="meta">共 0 条订单</p>
        </div>
        <div class="orders-filter-bar">
          <input id="orders-search" type="search" placeholder="搜索商户订单号 / 用户 ID / 交易号" autocomplete="off">
          <select id="orders-status-filter">
            <option value="">全部状态</option>
            <option value="paid">已支付</option>
            <option value="pending">等待支付</option>
            <option value="expired">已过期 / 已取消</option>
            <option value="failed">支付异常</option>
          </select>
        </div>
        <div class="table-scroll" id="site-orders" aria-live="polite"></div>
        <div class="actions"><button id="orders-prev" title="上一页" aria-label="上一页">&larr;</button><span id="orders-page"></span><button id="orders-next" title="下一页" aria-label="下一页">&rarr;</button></div>
      </section>
      <section class="panel"><h3>生成卡密</h3>
        <div class="fields"><label>计划<select id="site-code-plan"><option value="monthly">月刊会员</option><option value="yearly">年刊会员</option></select></label><label>数量<input id="site-code-count" type="number" min="1" max="50" value="1"></label><label class="span2">备注<input id="site-code-note"></label></div>
        <div class="actions"><button id="site-code-create" class="primary">生成卡密</button></div><div id="site-code-result" class="result" hidden></div>
        <div id="site-codes" aria-live="polite"></div>
      </section>
    </div>
  </section>

  <section class="workspace" id="translations" role="tabpanel" aria-labelledby="tab-translations" hidden>
    <div class="translation-head">
      <div>
        <h2 id="translations-heading">翻译状态</h2>
        <p id="translation-edition" class="note">暂无自动化刊期</p>
      </div>
      <div class="translation-controls">
        <label for="translation-edition-select">刊期</label>
        <select id="translation-edition-select" aria-label="选择翻译刊期"></select>
        <button id="translation-refresh" type="button">刷新</button>
      </div>
    </div>
    <div id="translation-provider" class="translation-provider"></div>
    <div id="translation-stats" class="stats translation-stats"></div>
    <div class="segmented" role="group" aria-label="翻译任务筛选">
      <button type="button" data-translation-filter="all" aria-pressed="true">全部</button>
      <button type="button" data-translation-filter="running" aria-pressed="false">运行中</button>
      <button type="button" data-translation-filter="retry_wait" aria-pressed="false">待重试</button>
      <button type="button" data-translation-filter="failed" aria-pressed="false">失败</button>
      <button type="button" data-translation-filter="online" aria-pressed="false">已上线</button>
    </div>
    <section class="panel">
      <div id="translation-list" aria-live="polite"></div>
    </section>
  </section>

  <section class="workspace" id="delivery" role="tabpanel" aria-labelledby="tab-delivery" hidden>
    <h2 id="delivery-heading">投递状态</h2>
    <p class="note">默认人工发送只投递未成功者。unknown 可能已送达，必须单独确认重复风险。</p>
    <section class="panel">
      <div id="delivery-summary"></div>
      <div class="actions">
        <button id="delivery-refresh">刷新状态</button>
        <button id="retry-failed">仅重试失败者</button>
        <button id="retry-unknown" class="danger">确认风险并重试 unknown</button>
      </div>
      <label for="manual-edition">指定已保留刊期（YYYY-MM-DD）</label>
      <input id="manual-edition" placeholder="2026-07-27">
      <div class="actions"><button id="manual-preview">先预览指定刊期</button><button id="manual-send" class="primary" disabled>确认人工发送</button></div>
      <div id="delivery-list"></div>
    </section>
  </section>

  <p id="status" role="status" aria-live="polite"></p>
  <section class="panel" id="password-panel" hidden>
    <h2>面板口令</h2>
    <label for="old-password">当前口令</label>
    <input id="old-password" type="password" autocomplete="current-password">
    <label for="new-password">新口令（至少 8 位）</label>
    <input id="new-password" type="password" autocomplete="new-password">
    <div class="actions">
      <button id="change-password">修改口令</button>
      <button id="logout">退出登录</button>
    </div>
  </section>
</main>
<script>
"use strict";
var csrf = "";
var providers = {};
var loadedUsers = [];
var usersPage = 1;
var usersPageSize = 20;
var usersTotal = 0;
var usersRequestSerial = 0;
var usersSearchTimer = null;
var statusEl = document.getElementById("status");
var listEl = document.getElementById("providers");
var resultEl = document.getElementById("test-result");

function field(id) {
  return document.getElementById(id);
}
function say(message, ok) {
  statusEl.textContent = message;
  statusEl.className = ok ? "ok" : "err";
  var workspace = document.querySelector(".workspace:not([hidden])");
  var marker = workspace && workspace.querySelector(".grid, .panel, .stats");
  if (workspace && marker) {
    workspace.insertBefore(statusEl, marker);
  }
}
function setBusy(node, busy) {
  if (busy) {
    node.dataset.wasDisabled = String(node.disabled);
    node.disabled = true;
    node.setAttribute("aria-busy", "true");
  } else {
    node.disabled = node.dataset.wasDisabled === "true";
    node.removeAttribute("aria-busy");
    delete node.dataset.wasDisabled;
  }
}
function api(path, body) {
  var options = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: JSON.stringify(body)
  };
  return fetch(path, options).then(function (response) {
    return response.json().then(function (data) {
      if (response.status === 401) {
        location.reload();
      }
      if (!response.ok) {
        var error = new Error(data.error || ("HTTP " + response.status));
        error.data = data;
        throw error;
      }
      return data;
    });
  });
}
function formBody() {
  return {
    name: field("f-name").value,
    base_url: field("f-url").value,
    api_key: field("f-key").value,
    model: field("f-model").value,
    api_type: field("f-type").value,
    reasoning_effort: field("f-reasoning").value,
    stream: field("f-stream").checked,
    enabled: field("f-enabled").checked
  };
}
function addText(parent, tag, text, className) {
  var node = document.createElement(tag);
  node.textContent = text;
  if (className) {
    node.className = className;
  }
  parent.appendChild(node);
  return node;
}
function button(text, handler, className) {
  var node = document.createElement("button");
  node.type = "button";
  node.textContent = text;
  if (className) { node.className = className; }
  node.addEventListener("click", handler);
  return node;
}
function table(headers, rows) {
  var node = document.createElement("table");
  node.className = "data-table";
  var thead = document.createElement("thead");
  var head = document.createElement("tr");
  headers.forEach(function (value) { addText(head, "th", value); });
  thead.appendChild(head);
  node.appendChild(thead);
  var body = document.createElement("tbody");
  rows.forEach(function (values) {
    var row = document.createElement("tr");
    values.forEach(function (value, index) {
      var cell = document.createElement("td");
      cell.dataset.label = headers[index];
      if (value instanceof Node) { cell.appendChild(value); } else { cell.textContent = String(value); }
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  node.appendChild(body);
  return node;
}
document.querySelectorAll(".tab").forEach(function (tab) {
  tab.addEventListener("click", function () {
    document.querySelectorAll(".tab").forEach(function (item) {
      item.setAttribute("aria-selected", String(item === tab));
    });
    document.querySelectorAll(".workspace").forEach(function (space) {
      space.hidden = space.id !== tab.dataset.tab;
    });
    if (tab.dataset.tab === "mail") { loadMail(); }
    if (tab.dataset.tab === "users") { loadUsers(); }
    if (tab.dataset.tab === "site") { loadPayments(); }
    if (tab.dataset.tab === "translations") { startTranslationUpdates(); }
    else { stopTranslationUpdates(); }
    if (tab.dataset.tab === "delivery") { loadDelivery(); }
  });
});
function loadForm(name) {
  var item = providers[name];
  field("f-name").value = item.name;
  field("f-url").value = item.base_url;
  field("f-key").value = "";
  field("f-model").value = item.model;
  field("f-type").value = item.api_type;
  field("f-reasoning").value = item.reasoning_effort || "";
  field("f-stream").checked = item.stream;
  field("f-enabled").checked = item.enabled;
  field("dirty").textContent = "已载入保存档案；修改后请重新测试";
  resultEl.hidden = true;
}
function render(data) {
  providers = data.providers;
  listEl.replaceChildren();
  var names = Object.keys(providers);
  if (!names.length) {
    addText(listEl, "p", "尚无供应商档案。", "meta");
    return;
  }
  names.forEach(function (name) {
    var item = providers[name];
    var card = document.createElement("article");
    card.className = "provider";
    card.tabIndex = 0;
    card.addEventListener("click", function () { loadForm(name); });
    card.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        loadForm(name);
      }
    });
    addText(card, "strong", name + (item.is_default ? " · 默认" : ""));
    addText(card, "span", item.enabled ? "已启用" : "已停用", "badge");
    addText(
      card,
      "span",
      item.api_type + " · " + item.model + " · " + (item.stream ? "流式" : "非流式"),
      "meta"
    );
    addText(card, "span", "推理强度：" + (item.reasoning_effort || "自动"), "meta");
    if (item.last_test) {
      var text = item.last_test.stale ? "最近测试已过期" :
        (item.last_test.status === "success" ? "最近测试成功" : "最近测试失败");
      var tested = item.last_test.tested_at_epoch ?
        new Date(item.last_test.tested_at_epoch * 1000).toLocaleString() : "时间未知";
      addText(card, "span", text + " · " + tested + " · " +
        item.last_test.protocol + " · " + item.last_test.category + " · " +
        item.last_test.elapsed_ms + " ms", "meta");
      addText(card, "span", "连接/鉴权：" + item.last_test.connection_auth.status +
        " · 模型返回：" + item.last_test.model_return.status, "meta");
    }
    listEl.appendChild(card);
  });
}
function load() {
  api("/admin/api/providers").then(function (data) {
    csrf = data.csrf_token;
    field("password-panel").hidden = !data.can_change_password;
    render(data);
  }).catch(function (error) { say(error.message, false); });
}

document.querySelectorAll("#models input,#models select").forEach(function (element) {
  element.addEventListener("input", function () {
    field("dirty").textContent = "当前表单尚未保存";
    resultEl.hidden = true;
    resultEl.textContent = "";
  });
});
field("test").addEventListener("click", function () {
  if (!confirm("将使用当前表单配置恰好发送 1 次固定 Hi 生成请求。输入为 2 个字符，输出上限 8 tokens，可能产生费用。继续？")) { return; }
  var action = field("test");
  setBusy(action, true);
  say("正在执行一次固定 Hi 请求…", true);
  api("/admin/api/providers/test", formBody()).then(function (data) {
    resultEl.hidden = false;
    resultEl.textContent =
      "配置状态：" + (data.configuration_state === "matching_saved" ? "与已保存配置一致" : "正在测试未保存配置") + "\n" +
      "连接/鉴权：" + data.connection_auth.message + "\n" +
      "模型返回：" + data.model_return.message + "\n" +
      "协议/模型：" + data.protocol + " / " + data.model + "\n" +
      "耗时：" + data.elapsed_ms + " ms\n" +
      "安全输出：" + data.output;
    say("测试完成；测试状态与当前配置指纹绑定。", true);
  }).catch(function (error) {
    resultEl.hidden = false;
    var data = error.data || {};
    resultEl.textContent =
      "连接/鉴权：" + ((data.connection_auth && data.connection_auth.message) || error.message) + "\n" +
      "模型返回：" + ((data.model_return && data.model_return.message) || "未完成") + "\n" +
      "分类/状态：" + (data.category || "configuration") + " / " + (data.upstream_status || "无") + "\n" +
      "协议/模型：" + (data.protocol || field("f-type").value) + " / " + (data.model || field("f-model").value) + "\n" +
      "耗时：" + (data.elapsed_ms === undefined ? "未开始" : data.elapsed_ms + " ms");
    say("测试失败；请按错误阶段修正配置。", false);
  }).finally(function () { setBusy(action, false); });
});
field("save").addEventListener("click", function () {
  var action = field("save");
  setBusy(action, true);
  api("/admin/api/providers", formBody()).then(function () {
    field("f-key").value = "";
    field("dirty").textContent = "已保存";
    say("档案已保存。", true);
    load();
  }).catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
field("default").addEventListener("click", function () {
  var name = field("f-name").value;
  var expected = providers[name] && providers[name].configuration_fingerprint;
  api("/admin/api/providers/default", {name: name, expected_fingerprint: expected}).then(function () {
    say("已设为唯一默认档案。", true);
    load();
  }).catch(function (error) {
    var confirmation = error.data && error.data.confirmation_required;
    if (confirmation && confirm("测试未成功或已过期。仍要设为默认吗？")) {
      api("/admin/api/providers/default", {
        name: name,
        expected_fingerprint: expected,
        confirm_untested: true
      }).then(function () {
        say("已确认风险并设为默认。", true);
        load();
      }).catch(function (second) { say(second.message, false); });
    } else {
      say(error.message, false);
    }
  });
});
field("delete").addEventListener("click", function () {
  var name = field("f-name").value;
  if (name && confirm("确认删除档案 " + name + "？")) {
    api("/admin/api/providers", {name: name, delete: true}).then(function () {
      say("档案已删除。", true);
      load();
    }).catch(function (error) { say(error.message, false); });
  }
});
field("disable").addEventListener("click", function () {
  if (confirm("确认显式停用翻译并清除默认项？")) {
    api("/admin/api/translation/disable", {confirm: true}).then(function () {
      say("翻译已停用；当前无默认档案。", true);
      load();
    }).catch(function (error) { say(error.message, false); });
  }
});
function mailBody() {
  return {
    delivery_enabled: field("m-enabled").checked,
    host: field("m-host").value,
    port: Number(field("m-port").value),
    username: field("m-user").value,
    password: field("m-password").value,
    security: field("m-security").value,
    sender: field("m-sender").value,
    mains_enabled: field("m-mains").checked,
    briefs_enabled: field("m-briefs").checked,
    main_limit: Number(field("m-main-limit").value),
    brief_limit: Number(field("m-brief-limit").value),
    language: field("m-language").value,
    source_filters: Array.from(field("m-sources").selectedOptions).map(function (option) { return option.value; }),
    layout: field("m-layout").value,
    summary_length: field("m-summary").value,
    catchup_window_hours: Number(field("m-catchup").value)
  };
}
function fillMail(data) {
  field("m-enabled").checked = data.delivery_enabled;
  field("m-host").value = data.host;
  field("m-port").value = data.port;
  field("m-user").value = data.username;
  field("m-password").value = "";
  field("m-password-state").textContent = data.password_set ? "已保存密码" : "尚未保存密码";
  field("m-security").value = data.security;
  field("m-sender").value = data.sender;
  field("m-mains").checked = data.mains_enabled;
  field("m-briefs").checked = data.briefs_enabled;
  field("m-main-limit").value = data.main_limit;
  field("m-brief-limit").value = data.brief_limit;
  field("m-language").value = data.language;
  field("m-layout").value = data.layout;
  field("m-summary").value = data.summary_length;
  field("m-catchup").value = data.catchup_window_hours;
  var release = data.current_release;
  var selected = new Set(data.source_filters);
  field("m-sources").replaceChildren();
  (release ? release.sources : []).forEach(function (source) {
    var option = document.createElement("option"); option.value = source; option.textContent = source;
    option.selected = selected.has(source); field("m-sources").appendChild(option);
  });
  field("mail-schedule").textContent = "时区 " + data.timezone + " · 每日 " + data.schedule_time + " · 下次 " + data.next_schedule + " · " + (release ? "当前 " + release.date : "当前刊期不可用");
  field("mail").dataset.releaseAvailable = String(Boolean(release));
  field("mail").dataset.mainItems = JSON.stringify(release ? release.main_items : []);
  field("mail").dataset.briefItems = JSON.stringify(release ? release.brief_items : []);
  updateMailEstimate();
}
function updateMailEstimate() {
  if (field("mail").dataset.releaseAvailable === "false") {
    field("mail-estimate").textContent = "当前 release 不可用；SMTP 设置与连接测试仍可使用。";
    return;
  }
  var selected = new Set(Array.from(field("m-sources").selectedOptions).map(function (option) { return option.value; }));
  function available(name) {
    var raw = field("mail").dataset[name] || "[]";
    return JSON.parse(raw).filter(function (item) { return !selected.size || selected.has(item.source); });
  }
  var mains = field("m-mains").checked ? available("mainItems").slice(0, Math.max(0, Number(field("m-main-limit").value))) : [];
  var briefs = field("m-briefs").checked ? available("briefItems").slice(0, Math.max(0, Number(field("m-brief-limit").value))) : [];
  var needsZh = field("m-language").value !== "en";
  var degraded = needsZh && mains.concat(briefs).some(function (item) { return !item.has_zh; });
  field("mail-estimate").textContent = mains.length || briefs.length ?
    "预计主文 " + mains.length + " 条 · 简讯 " + briefs.length + " 条 · " + (degraded ? "存在缺译降级" : "无缺译降级") :
    "当前组合为空，不能保存或发送。";
}
document.querySelectorAll("#mail input,#mail select").forEach(function (element) {
  element.addEventListener("input", updateMailEstimate);
  element.addEventListener("change", updateMailEstimate);
});
function loadMail() {
  api("/admin/api/mail/settings").then(function (data) {
    csrf = data.csrf_token || csrf; fillMail(data);
    if (!data.preview_validation.valid) {
      if (data.preview_validation.category === "release") {
        say("当前发布工件不可用于邮件内容：" + data.preview_validation.message + "。SMTP 设置与连接测试仍可使用；请先运行 uv run news-digest build 生成新刊期。", false);
      } else {
        say("已保存邮件组合不适用于当前刊期，请修改并保存：" + data.preview_validation.message, false);
      }
    }
  }).catch(function (error) { say(error.message, false); });
}
function showMailPreview(data) {
  var box = field("mail-result"); box.hidden = false; box.replaceChildren();
  addText(box, "strong", data.subject);
  addText(box, "p", "刊期 " + data.edition_date + " · 主文 " + data.main_count + " · 简讯 " + data.brief_count + " · 收件人数 " + data.recipient_count + " · " + (data.degraded ? "降级" : "完整"));
  addText(box, "p", "实际发送内容（HTML 更新通知）");
  var frame = document.createElement("iframe"); frame.className = "preview-frame"; frame.title = "邮件 HTML 预览"; frame.sandbox = ""; frame.srcdoc = data.html; box.appendChild(frame);
}
field("mail-test").addEventListener("click", function () {
  var action = field("mail-test");
  setBusy(action, true);
  say("正在连接并认证；不会发送邮件…", true);
  api("/admin/api/mail/test-connection", mailBody())
    .then(function (data) { say(data.message, true); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
field("mail-save").addEventListener("click", function () {
  var action = field("mail-save");
  setBusy(action, true);
  api("/admin/api/mail/settings", mailBody())
    .then(function () { say("邮件设置已保存；未连接 SMTP。", true); loadMail(); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
field("mail-preview").addEventListener("click", function () {
  var action = field("mail-preview");
  setBusy(action, true);
  api("/admin/api/mail/preview", {})
    .then(showMailPreview)
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
function sendMailTest(action, kind, label, subscriptionId) {
  setBusy(action, true);
  var key = "test-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  api("/admin/api/mail/test-message", {confirm: true, idempotency_key: key, subscription_id: subscriptionId, kind: kind})
    .then(function (data) {
      var message = label + "：成功 " + data.sent_count + "，失败 " + data.failed_count + "，unknown " + data.unknown_count;
      if (data.failed_count || data.unknown_count) {
        message += "；发生阶段 " + (data.error_stage || "未记录") + "；错误分类 " + (data.error_category || "未分类") + "；下一步 " + data.next_action;
      }
      action.dataset.retryAllowed = String(data.retry_allowed);
      say(message, data.ok);
    })
    .catch(function (error) {
      var data = error.data || {};
      if (data.category === "unknown_pending") {
        action.dataset.retryAllowed = "false";
        say(
          label + "未重新发送：已有 unknown 投递尚未核对；发生阶段 " +
          (data.error_stage || "未记录") + "；错误分类 " +
          (data.error_category || "未分类") + "；下一步 " +
          (data.next_action || error.message),
          false
        );
        return;
      }
      say(error.message, false);
    })
    .finally(function () {
      setBusy(action, false);
      if (action.dataset.retryAllowed === "false") { action.disabled = true; }
    });
}
field("mail-clear-password").addEventListener("click", function () {
  if (!confirm("确认独立清除已保存 SMTP 密码？清除后认证配置可能暂时无效。")) { return; }
  api("/admin/api/mail/clear-password", {confirm: true}).then(function () { say("已清除 SMTP 密码。", true); loadMail(); }).catch(function (error) { say(error.message, false); });
});

function statCard(parent, label, value) {
  var card = document.createElement("div"); card.className = "stat";
  addText(card, "strong", String(value)); addText(card, "span", label); parent.appendChild(card);
}
function yuanToCents(value) {
  var match = String(value).trim().match(/^(\d{1,6})(?:\.(\d{1,2}))?$/);
  if (!match) { return null; }
  var cents = Number(match[1]) * 100 + Number((match[2] || "").padEnd(2, "0"));
  return cents <= 10000000 ? cents : null;
}
function centsToYuan(value) {
  return (Number(value) / 100).toFixed(2);
}
function formatBasisPoints(value) {
  var whole = Math.floor(value / 100);
  var fraction = String(value % 100).padStart(2, "0").replace(/0+$/, "");
  return fraction ? whole + "." + fraction : String(whole);
}
function updateDiscountPreview(plan) {
  var base = yuanToCents(field("site-" + plan).value);
  var sale = yuanToCents(field("site-" + plan + "-sale").value);
  var output = field("site-" + plan + "-discount-preview");
  if (base === null || sale === null || base === 0 || sale > base) {
    output.textContent = "现价不得高于划线基准价";
    return;
  }
  if (sale === base) {
    output.textContent = "当前无优惠 · 前台仅显示现价";
    return;
  }
  var priceRatio = Math.floor(sale * 10000 / base);
  var reduction = Math.floor((base - sale) * 10000 / base);
  output.textContent = "现价为基准价 " + formatBasisPoints(priceRatio)
    + "% · 前台显示 -" + formatBasisPoints(reduction) + "%";
}
function updateDiscountPreviews() {
  updateDiscountPreview("monthly");
  updateDiscountPreview("yearly");
}
function remainingSubscriptionDays(paidUntil) {
  if (!paidUntil) { return null; }
  var expiresAt = Date.parse(paidUntil);
  if (!Number.isFinite(expiresAt)) { return null; }
  return Math.max(0, Math.ceil((expiresAt - Date.now()) / 86400000));
}
function renderUsers() {
    var query = field("users-search").value.trim();
    var pageCount = Math.max(1, Math.ceil(usersTotal / usersPageSize));
    var userRows = loadedUsers.map(function (item) {
      var actions = document.createElement("div"); actions.className = "actions";
      var newsletterActions = document.createElement("div"); newsletterActions.className = "actions";
      var remainingDays = remainingSubscriptionDays(item.paid_until);
      var memberActive = item.status === "active" && remainingDays !== null && remainingDays > 0;
      var newsletterId = item.newsletter_subscription_id;
      var newsletterStatus = item.newsletter_status;
      if (newsletterStatus === "active") {
        newsletterActions.appendChild(button("发送验证邮件", function (event) {
          if (!confirm("确认只向该用户发送一封小体积 SMTP 验证邮件？")) { return; }
          sendMailTest(event.currentTarget, "smtp_smoke", "SMTP 验证邮件", newsletterId);
        }));
        newsletterActions.appendChild(button("发送测试邮件", function (event) {
          if (!confirm("确认只向该用户发送一封测试邮件？")) { return; }
          sendMailTest(event.currentTarget, "digest", "测试邮件", newsletterId);
        }));
        newsletterActions.appendChild(button("停用简报", function () {
          if (!confirm("确认由管理员停用该用户的每日简报？")) { return; }
          api("/admin/api/subscriptions/disable", {id: newsletterId, confirm: true})
            .then(function () { say("每日简报已停用。", true); return loadUsers(); })
            .catch(function (error) { say(error.message, false); });
        }, "danger"));
      } else if (newsletterStatus === "disabled" && memberActive) {
        newsletterActions.appendChild(button("启用简报", function () {
          api("/admin/api/subscriptions/enable", {id: newsletterId, confirm: true})
            .then(function () { say("每日简报已启用。", true); return loadUsers(); })
            .catch(function (error) { say(error.message, false); });
        }));
      } else if (memberActive) {
        newsletterActions.appendChild(button("开启简报", function () {
          api("/admin/api/subscriptions/add", {email: item.email})
            .then(function () { say("每日简报已开启。", true); return loadUsers(); })
            .catch(function (error) { say(error.message, false); });
        }));
      }
      if (newsletterId !== null) {
        newsletterActions.appendChild(button("删除简报记录", function () {
          if (!confirm("确认删除该用户的每日简报记录？")) { return; }
          api("/admin/api/subscriptions/delete", {id: newsletterId, confirm: true})
            .then(function () { say("每日简报记录已删除。", true); return loadUsers(); })
            .catch(function (error) { say(error.message, false); });
        }, "danger"));
      }
      var status = item.status === "disabled" ? "启用" : "停用";
      actions.appendChild(button(status, function () {
        api("/admin/api/site/user-status", {
          user_id: item.id, status: item.status === "disabled" ? "active" : "disabled"
        }).then(function () { say("用户状态已更新。", true); loadUsers(); })
          .catch(function (error) { say(error.message, false); });
      }, item.status === "disabled" ? "" : "danger"));
      var grantControls = document.createElement("div"); grantControls.className = "grant-controls";
      var grantPlan = document.createElement("select"); grantPlan.setAttribute("aria-label", "增加时长的订阅计划");
      [["monthly", "月刊会员"], ["yearly", "年刊会员"]].forEach(function (entry) {
        var option = document.createElement("option"); option.value = entry[0]; option.textContent = entry[1];
        option.selected = item.plan === entry[0]; grantPlan.appendChild(option);
      });
      var grantDays = document.createElement("input"); grantDays.type = "number"; grantDays.min = "1";
      grantDays.max = "3660"; grantDays.value = item.plan === "yearly" ? "366" : "31";
      grantDays.setAttribute("aria-label", "增加订阅天数");
      grantPlan.addEventListener("change", function () {
        grantDays.value = grantPlan.value === "yearly" ? "366" : "31";
      });
      var grantAction = button("增加时长", function () {
        setBusy(grantAction, true);
        var command = grantPlan.value + ":" + grantDays.value;
        if (grantAction.dataset.command !== command) {
          grantAction.dataset.command = command; grantAction.dataset.operation = crypto.randomUUID();
        }
        api("/admin/api/site/user-grant", {user_id: item.id, plan: grantPlan.value, days: Number(grantDays.value), operation_id: grantAction.dataset.operation})
          .then(function (result) { say("已增加 " + result.days_added + " 天订阅时长。", true); return loadUsers(); })
          .catch(function (error) { say(error.message, false); })
          .finally(function () { setBusy(grantAction, false); });
      });
      grantControls.append(grantPlan, grantDays, grantAction);
      actions.appendChild(grantControls);
      var history = document.createElement("details");
      var historyTitle = document.createElement("summary"); historyTitle.textContent = "权益记录";
      history.appendChild(historyTitle);
      (item.entitlement_changes || []).forEach(function (entry) {
        var line = document.createElement("p");
        line.textContent = entry.created_at + " " + entry.actor + " " + entry.reason + " "
          + (entry.before_until || "-") + " -> " + (entry.after_until || "-");
        history.appendChild(line);
      });
      actions.appendChild(history);
      if (item.plan || item.paid_until) {
        var clearAction = button("清除订阅", function () {
          if (!confirm("确认清除该账号的订阅计划与剩余时长？账号状态和管理员权限不会改变。")) { return; }
          setBusy(clearAction, true);
          api("/admin/api/site/user-subscription-clear", {user_id: item.id, confirm: true, expected_paid_until: item.paid_until, operation_id: crypto.randomUUID()})
            .then(function () { say("订阅已清除。", true); return loadUsers(); })
            .catch(function (error) { say(error.message, false); })
            .finally(function () { setBusy(clearAction, false); });
        }, "danger");
        actions.appendChild(clearAction);
      }
      if (item.status === "active" || item.is_admin) {
        actions.appendChild(button(item.is_admin ? "撤销管理员" : "设为管理员", function () {
          var prompt = item.is_admin
            ? "确认撤销该账号的后台管理权限？其现有 Admin 会话会立即失效。"
            : "确认授予该账号后台管理权限？该账号将可使用邮箱和密码登录 Admin。";
          if (!confirm(prompt)) { return; }
          api("/admin/api/site/user-admin", {
            user_id: item.id, is_admin: !item.is_admin, confirm: true
          }).then(function () { say("管理员权限已更新。", true); loadUsers(); })
            .catch(function (error) { say(error.message, false); });
        }, item.is_admin ? "danger" : ""));
      }
      var planLabel = item.plan === "monthly" ? "月刊会员" : item.plan === "yearly" ? "年刊会员" : "无订阅";
      var remainingLabel = remainingDays === null ? "无订阅" : remainingDays > 0 ? "剩余 " + remainingDays + " 天" : "已到期";
      var newsletterLabels = {
        active: memberActive ? "已开启" : "已开启（会员失效暂停）",
        pending: "待确认",
        unsubscribed: "用户已退订",
        disabled: "管理员停用"
      };
      var newsletterLabel = newsletterLabels[newsletterStatus] || "未开启";
      return [item.email, item.status, item.is_admin ? "管理员" : "普通用户", planLabel, remainingLabel, item.paid_until || "-", newsletterLabel, newsletterActions, actions];
    });
    field("site-users").replaceChildren(
      table(["邮箱", "状态", "角色", "会员计划", "剩余时长", "到期时间", "每日简报", "简报操作", "账号操作"], userRows)
    );
    field("users-page").textContent = "第 " + usersPage + " / " + pageCount + " 页";
    field("users-prev").disabled = usersPage <= 1;
    field("users-next").disabled = usersPage >= pageCount;
    var start = usersTotal === 0 ? 0 : (usersPage - 1) * usersPageSize + 1;
    var end = start === 0 ? 0 : start + loadedUsers.length - 1;
    var summary = query
      ? "邮箱搜索结果 " + usersTotal + " 个"
      : "账号总数 " + usersTotal + " 个";
    summary += " · 当前显示 " + start + "–" + end + "。";
    if (loadedUsers.length === 0) { summary += " 没有匹配的账号。"; }
    field("users-summary").textContent = summary;
}

field("operations-status").addEventListener("toggle", function () {
  if (!field("operations-status").open) { return; }
  api("/admin/api/operations").then(function (data) {
    var issues = Object.keys(data.checks).filter(function (key) { return data.checks[key]; });
    var config = data.configuration;
    field("operations-summary").textContent = data.date + " | "
      + (issues.length ? issues.join(", ") : "运行正常")
      + " | 备份: " + (data.backup_verified_at || "未验证")
      + " | 配置: " + config.state + " " + (config.applied_revision || "未生效").slice(0, 12)
      + " | DB: " + (data.database_bytes / 1048576).toFixed(1) + " MB";
    var report = data.fetch || {};
    field("operations-payment-slots").textContent = "支付金额槽位: "
      + (data.payment_slots.groups.length ? data.payment_slots.groups.map(function (group) {
        return (group.base_amount_cents / 100).toFixed(2) + " 元: " + group.occupied
          + "/" + data.payment_slots.capacity_per_price;
      }).join(" | ") : "无占用");
    field("operations-sources").replaceChildren(table(
      ["来源", "原始", "有效", "时间窗", "未来异常", "正文提取", "摘要降级", "入选", "失败阶段"],
      Object.entries(report.diagnostics || {}).map(function (entry) {
        var d = entry[1]; return [entry[0], d.raw, d.parsed, d.window, d.future, d.full,
          d.summary, d.selected, JSON.stringify(d.failures || {})];
      })
    ));
    field("operations-selection").replaceChildren(table(["候选", "分数", "选择原因"],
      Object.entries(report.selection || {}).map(function (entry) {
        return [entry[0], entry[1].score, entry[1].reason];
      })
    ));
  }).catch(function (error) { field("operations-summary").textContent = error.message; });
});

function loadUsers() {
  var requestSerial = ++usersRequestSerial;
  var query = encodeURIComponent(field("users-search").value.trim());
  var url = "/admin/api/users/overview?query=" + query
    + "&page=" + usersPage + "&page_size=" + usersPageSize;
  api(url).then(function (data) {
    if (requestSerial !== usersRequestSerial) { return; }
    csrf = data.csrf_token || csrf;
    loadedUsers = data.users;
    usersTotal = data.total;
    usersPage = data.page;
    usersPageSize = data.page_size;
    renderUsers();
  }).catch(function (error) { say(error.message, false); });
}

function getSelectedEpayTypes() {
  var aliBtn = field("site-epay-type-alipay");
  var wxBtn = field("site-epay-type-wxpay");
  var alipay = aliBtn ? aliBtn.classList.contains("active") : false;
  var wxpay = wxBtn ? wxBtn.classList.contains("active") : false;
  if (alipay && wxpay) { return "alipay,wxpay"; }
  if (alipay) { return "alipay"; }
  if (wxpay) { return "wxpay"; }
  return "";
}

function setEpayChannelState(val, enabled) {
  var str = (val || "").toLowerCase();
  var isAlipay = str.indexOf("alipay") !== -1 || str === "both" || str === "all";
  var isWxpay = str.indexOf("wxpay") !== -1 || str === "both" || str === "all";
  if (enabled && !isAlipay && !isWxpay) { isAlipay = true; }
  var btnAli = field("site-epay-type-alipay");
  var btnWx = field("site-epay-type-wxpay");
  if (btnAli) { btnAli.classList.toggle("active", isAlipay); }
  if (btnWx) { btnWx.classList.toggle("active", isWxpay); }
  var typeField = field("site-epay-type");
  if (typeField) { typeField.value = getSelectedEpayTypes(); }
}

function loadPayments() {
  api("/admin/api/payments/overview?page=" + ordersPage + "&query=" + encodeURIComponent(field("orders-search").value.trim())).then(function (data) {
    csrf = data.csrf_token || csrf;
    field("site-paywall").checked = data.settings.paywall_enabled === "true";
    field("site-contact-email").value = data.settings.contact_email || "";
    field("site-monthly").value = centsToYuan(data.settings.monthly_list_price_cents);
    field("site-yearly").value = centsToYuan(data.settings.yearly_list_price_cents);
    field("site-monthly-sale").value = centsToYuan(data.settings.monthly_price_cents);
    field("site-yearly-sale").value = centsToYuan(data.settings.yearly_price_cents);
    updateDiscountPreviews();
    field("site-epay-enabled").checked = data.payment.enabled;
    field("site-epay-base").value = data.payment.api_base || "";
    field("site-epay-pid").value = data.payment.pid || "";
    field("site-epay-pkey").value = "";
    setEpayChannelState(data.payment.payment_type || "", data.payment.enabled);
    field("site-epay-ttl").value = data.payment.order_ttl_seconds;
    field("site-epay-hold").value = data.payment.amount_hold_seconds;
    field("site-epay-pkey-status").textContent = data.payment.pkey_set ? "PKey 已保存" : "PKey 尚未保存";
    field("site-epay-notify").textContent = data.payment.notify_url || "站点域名尚未配置";
    field("site-epay-return").textContent = data.payment.return_url || "站点域名尚未配置";
    allOrders = data.orders || [];
    field("orders-page").textContent = "第 " + data.orders_page + " 页";
    field("orders-prev").disabled = ordersPage <= 1;
    field("orders-next").disabled = allOrders.length < 200;
    renderOrders();

    var codeRows = data.codes.map(function (item) {
      var actions = document.createElement("div"); actions.className = "actions";
      if (item.status === "unused") {
        actions.appendChild(button("删除", function () {
          if (!confirm("确认删除未使用的卡密 " + item.prefix + "？删除后无法恢复。")) { return; }
          api("/admin/api/site/code-delete", {code_id: item.id})
            .then(function () { say("卡密已删除。", true); loadPayments(); })
            .catch(function (error) { say(error.message, false); });
        }, "danger"));
      }
      return [item.code || item.prefix + "••••", item.plan, item.status, item.note || "", item.created_at, actions];
    });
    field("site-codes").replaceChildren(
      table(["卡密", "计划", "状态", "备注", "创建时间", "操作"], codeRows)
    );
  }).catch(function (error) { say(error.message, false); });
}

var allOrders = [];
var ordersPage = 1;
var ordersSearchTimer;

function paymentCaseAction(item, action, title, referenceLabel) {
  var details = document.createElement("details"); details.className = "payment-action";
  addText(details, "summary", title);
  var reference = document.createElement("input"); reference.maxLength = 200; reference.required = true;
  addText(details, "label", referenceLabel).appendChild(reference);
  var deduction = null;
  if (action === "refunded" && item.status === "paid") {
    deduction = document.createElement("input"); deduction.type = "number";
    deduction.min = "0"; deduction.max = "3660"; deduction.step = "1";
    deduction.value = "0"; deduction.required = true;
    addText(details, "label", "扣减会员天数（0 表示不扣减）").appendChild(deduction);
  }
  var submit = button("确认" + title, function () {
    if (!reference.value.trim()) { say("请填写" + referenceLabel + "。", false); reference.focus(); return; }
    if (!reference.reportValidity() || (deduction && !deduction.reportValidity())) { return; }
    var days = deduction ? Number(deduction.value) : 0;
    var message = action === "refunded"
      ? "确认已在支付平台完成退款并登记？本操作不会发起退款，将扣减会员 " + days + " 天。"
      : action === "grant" ? "确认按原订单套餐补开通会员？"
      : action === "closed" ? "确认已核实未到账并关闭异常记录？" : "确认登记争议？会员权益保持不变。";
    if (!confirm(message)) { return; }
    var command = JSON.stringify([action, reference.value.trim(), days]);
    if (submit.dataset.command !== command) {
      submit.dataset.command = command; submit.dataset.operation = crypto.randomUUID();
    }
    setBusy(submit, true);
    api("/admin/api/site/payment-case", {order_id: item.id, action: action,
      reference: reference.value.trim(), days: days, operation_id: submit.dataset.operation})
      .then(function () { say(action === "grant" ? "会员已按原订单套餐开通。" : "处理记录已保存。", true); return loadPayments(); })
      .catch(function (error) { say(error.message, false); })
      .finally(function () { setBusy(submit, false); });
  });
  details.appendChild(submit);
  return details;
}

function renderOrders() {
  var search = (field("orders-search").value || "").trim().toLowerCase();
  var statusFilter = field("orders-status-filter").value || "";
  var filtered = allOrders.filter(function (item) {
    if (statusFilter && item.status !== statusFilter) {
      return false;
    }
    if (search) {
      var no = (item.merchant_order_no || "").toLowerCase();
      var idStr = String(item.id);
      var uidStr = String(item.user_id);
      var tradeNo = (item.provider_trade_no || "").toLowerCase();
      if (no.indexOf(search) === -1 && idStr.indexOf(search) === -1 && uidStr.indexOf(search) === -1 && tradeNo.indexOf(search) === -1) {
        return false;
      }
    }
    return true;
  });
  field("orders-count").textContent = "共 " + allOrders.length + " 条订单" + (filtered.length !== allOrders.length ? "（显示 " + filtered.length + " 条）" : "");
  var orderRows = filtered.map(function (item) {
    var labels = {
      pending: "等待支付", paid: "已支付 / 已开通", expired: "已过期 / 已取消", failed: "支付异常",
      approved: "历史人工开通", rejected: "历史已拒绝"
    };
    var offset = Number(item.amount_offset_cents || 0);
    var offsetLabel = offset === 0 ? "¥0.00" : (offset > 0 ? "+" : "-") + "¥" + (Math.abs(offset) / 100).toFixed(2);
    var actions = document.createElement("div"); actions.className = "actions payment-actions";
    var reconcilable = ["pending", "expired", "failed"].indexOf(item.status) !== -1 &&
      item.merchant_order_no && !item.paid_at;
    if (reconcilable) {
      var reconcileAction = button("重新查询支付状态", function () {
        setBusy(reconcileAction, true);
        api("/admin/api/site/payment-reconcile", {order_id: item.id})
          .then(function (result) {
            say(result.status === "paid" ? "订单已确认支付并开通会员。" : "支付状态已更新。", true);
            return loadPayments();
          })
          .catch(function (error) { say(error.message, false); })
          .finally(function () { setBusy(reconcileAction, false); });
      });
      actions.appendChild(reconcileAction);
    }
    var settlement = item.settlement_case;
    var caseState = settlement ? settlement.state : "";
    var statusLabel = labels[item.status] || "状态未知";
    if (caseState === "refunded") { statusLabel = "已登记退款"; }
    else if (caseState === "closed") { statusLabel = "已核实未到账"; }
    else if (caseState === "disputed") { statusLabel += " / 存在争议"; }
    else if (item.paid_at && item.status !== "paid") { statusLabel = "已到账 / 待开通"; }
    else if (caseState === "unconfirmed") { statusLabel = "到账状态待核实"; }
    if (settlement && settlement.reference) {
      var record = document.createElement("details");
      addText(record, "summary", "处理记录");
      addText(record, "p", settlement.reference);
      actions.appendChild(record);
    }
    if (item.merchant_order_no && ["refunded", "closed"].indexOf(caseState) === -1) {
      if (item.paid_at && item.status !== "paid" && ["received", "disputed"].indexOf(caseState) !== -1) {
        actions.appendChild(paymentCaseAction(item, "grant", "补开通会员", "到账凭证 / 核实依据"));
      }
      if (item.paid_at) {
        actions.appendChild(paymentCaseAction(item, "refunded", "登记退款", "支付平台退款流水号"));
      }
      if ((item.paid_at || settlement) && caseState !== "disputed") {
        actions.appendChild(paymentCaseAction(item, "disputed", "登记争议", "争议原因 / 凭证编号"));
      }
      if (settlement && !item.paid_at) {
        actions.appendChild(paymentCaseAction(item, "closed", "关闭未到账记录", "未到账核实依据"));
      }
    }
    return [
      item.merchant_order_no || "历史 #" + item.id,
      item.user_id,
      item.plan === "monthly" ? "月刊会员" : "年刊会员",
      "¥" + (item.base_amount_cents / 100).toFixed(2),
      "¥" + (item.amount_cents / 100).toFixed(2),
      offsetLabel,
      statusLabel,
      item.provider_trade_no || "-",
      item.expires_at || "-",
      item.last_error_code || "-",
      actions
    ];
  });
  field("site-orders").replaceChildren(
    table(["商户订单号", "用户", "计划", "基准金额", "实付金额", "尾差", "状态", "网关交易号", "支付截止", "错误代码", "操作"], orderRows)
  );
}
field("users-refresh").addEventListener("click", loadUsers);
field("users-search").addEventListener("input", function () {
  usersPage = 1;
  window.clearTimeout(usersSearchTimer);
  usersSearchTimer = window.setTimeout(loadUsers, 250);
});
field("users-prev").addEventListener("click", function () {
  usersPage = Math.max(1, usersPage - 1);
  loadUsers();
});
field("users-next").addEventListener("click", function () {
  usersPage += 1;
  loadUsers();
});
field("site-refresh").addEventListener("click", loadPayments);
field("orders-search").addEventListener("input", function () {
  ordersPage = 1; clearTimeout(ordersSearchTimer); ordersSearchTimer = setTimeout(loadPayments, 300);
});
field("orders-prev").addEventListener("click", function () { ordersPage = Math.max(1, ordersPage - 1); loadPayments(); });
field("orders-next").addEventListener("click", function () { ordersPage++; loadPayments(); });
field("orders-status-filter").addEventListener("change", renderOrders);
["monthly", "yearly"].forEach(function (plan) {
  field("site-" + plan).addEventListener("input", updateDiscountPreviews);
  field("site-" + plan + "-sale").addEventListener("input", updateDiscountPreviews);
});
field("site-settings-save").addEventListener("click", function () {
  var monthlyListPriceCents = yuanToCents(field("site-monthly").value);
  var yearlyListPriceCents = yuanToCents(field("site-yearly").value);
  var monthlyPriceCents = yuanToCents(field("site-monthly-sale").value);
  var yearlyPriceCents = yuanToCents(field("site-yearly-sale").value);
  if (monthlyListPriceCents === null || yearlyListPriceCents === null
      || monthlyPriceCents === null || yearlyPriceCents === null) {
    say("基准价和现价必须是 0.11 至 100000 元之间、最多两位小数的金额。", false);
    return;
  }
  if (monthlyPriceCents > monthlyListPriceCents
      || yearlyPriceCents > yearlyListPriceCents) {
    say("会员现价不能高于划线基准价。", false);
    return;
  }
  var action = field("site-settings-save"); setBusy(action, true);
  api("/admin/api/site/settings", {
    paywall_enabled: field("site-paywall").checked,
    contact_email: field("site-contact-email").value.trim(),
    monthly_list_price_cents: monthlyListPriceCents,
    yearly_list_price_cents: yearlyListPriceCents,
    monthly_price_cents: monthlyPriceCents,
    yearly_price_cents: yearlyPriceCents
  }).then(function () { say("付费设置已保存。", true); loadPayments(); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
var epayAliBtn = field("site-epay-type-alipay");
var epayWxBtn = field("site-epay-type-wxpay");
if (epayAliBtn) {
  epayAliBtn.addEventListener("click", function () {
    var isEnabled = field("site-epay-enabled").checked;
    var willBeActive = !epayAliBtn.classList.contains("active");
    var isWxActive = epayWxBtn ? epayWxBtn.classList.contains("active") : false;
    if (isEnabled && !willBeActive && !isWxActive) {
      say("启用在线支付时，至少需要保留一种已选支付通道。", false);
      return;
    }
    epayAliBtn.classList.toggle("active", willBeActive);
    field("site-epay-type").value = getSelectedEpayTypes();
  });
}
if (epayWxBtn) {
  epayWxBtn.addEventListener("click", function () {
    var isEnabled = field("site-epay-enabled").checked;
    var willBeActive = !epayWxBtn.classList.contains("active");
    var isAliActive = epayAliBtn ? epayAliBtn.classList.contains("active") : false;
    if (isEnabled && !willBeActive && !isAliActive) {
      say("启用在线支付时，至少需要保留一种已选支付通道。", false);
      return;
    }
    epayWxBtn.classList.toggle("active", willBeActive);
    field("site-epay-type").value = getSelectedEpayTypes();
  });
}
field("site-epay-enabled").addEventListener("change", function () {
  var isEnabled = field("site-epay-enabled").checked;
  if (isEnabled) {
    var aliActive = epayAliBtn ? epayAliBtn.classList.contains("active") : false;
    var wxActive = epayWxBtn ? epayWxBtn.classList.contains("active") : false;
    if (!aliActive && !wxActive) {
      if (epayAliBtn) { epayAliBtn.classList.add("active"); }
      if (epayWxBtn) { epayWxBtn.classList.add("active"); }
      field("site-epay-type").value = getSelectedEpayTypes();
    }
  }
});
field("site-epay-save").addEventListener("click", function () {
  var action = field("site-epay-save"); setBusy(action, true);
  api("/admin/api/site/payment-settings", {
    enabled: field("site-epay-enabled").checked,
    api_base: field("site-epay-base").value,
    pid: field("site-epay-pid").value,
    pkey: field("site-epay-pkey").value,
    payment_type: field("site-epay-type").value,
    order_ttl_seconds: Number(field("site-epay-ttl").value),
    amount_hold_seconds: Number(field("site-epay-hold").value)
  }).then(function () { say("支付配置已保存并立即生效。", true); return loadPayments(); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
field("site-epay-clear").addEventListener("click", function () {
  if (!confirm("确认清除已保存的 EasyPay PKey 并停用在线支付？")) { return; }
  var action = field("site-epay-clear"); setBusy(action, true);
  api("/admin/api/site/payment-clear-pkey", {confirm: true})
    .then(function () { say("PKey 已清除，在线支付已停用。", true); return loadPayments(); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});
field("site-code-create").addEventListener("click", function () {
  var action = field("site-code-create"); setBusy(action, true);
  api("/admin/api/site/codes", {
    plan: field("site-code-plan").value,
    count: Number(field("site-code-count").value),
    note: field("site-code-note").value
  }).then(function (data) {
    var box = field("site-code-result"); box.hidden = false;
    box.textContent = "已生成以下卡密，后台列表将持续显示：\n" + data.codes.join("\n");
    say("卡密已生成。", true); loadPayments();
  }).catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
});

var translationFilter = "all";
var translationState = null;
var translationSource = null;
var translationPoll = null;
var translationEdition = "";
var translationStatusLabels = {
  pending: "等待中", running: "翻译中", failed: "失败", retry_wait: "待重试",
  succeeded: "已翻译", configuration_blocked: "配置阻断", cancelled: "已取消"
};
var translationStageLabels = {
  waiting: "等待中", connect_provider: "连接 provider", waiting_model: "等待模型生成",
  receiving_response: "接收响应", schema_validation: "schema 校验",
  saving_translation: "保存翻译", waiting_build: "等待 build", building: "build 中",
  online: "已上线"
};
var translationQueueLabels = {
  executing: "正在执行", waiting_dispatch: "等待调度", waiting_backoff: "失败退避",
  waiting_cancel_confirmation: "等待终止确认", waiting_build: "等待 build",
  waiting_probe: "等待熔断探测", blocked: "已阻断", complete: "已完成"
};
function timeText(value) {
  if (!value) { return "—"; }
  var parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}
function secondsBetween(start, end) {
  if (!start) { return 0; }
  return Math.max(0, Math.round((new Date(end || Date.now()).getTime() - new Date(start).getTime()) / 1000));
}
function secondsUntil(value) {
  if (!value) { return 0; }
  return Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
}
function translationStageNode(item) {
  var node = document.createElement("div"); node.className = "translation-stage";
  addText(node, "strong", translationStageLabels[item.stage] || item.stage);
  if (item.status === "running") {
    addText(node, "small", "已用时 " + secondsBetween(item.started_at) + " 秒");
    addText(node, "small", "硬超时剩余 " + Math.max(0, secondsBetween(new Date(), item.hard_timeout_at)) + " 秒");
    addText(node, "small", "最后活动 " + timeText(item.last_activity_at));
    if (item.received_chunks) { addText(node, "small", "已接收 " + item.received_chunks + " 个分块"); }
  }
  if (item.queue_state && item.queue_state !== "executing" && item.queue_state !== "complete") {
    addText(node, "small", translationQueueLabels[item.queue_state] || item.queue_state);
  }
  if (item.queue_state === "waiting_backoff" && item.next_executable_at) {
    addText(node, "small", secondsUntil(item.next_executable_at) + " 秒后可执行");
  }
  return node;
}
function translationNextNode(item) {
  var node = document.createElement("div"); node.className = "translation-stage";
  if (item.queue_state === "executing") { addText(node, "strong", "正在执行"); return node; }
  if (item.queue_state === "waiting_dispatch") { addText(node, "strong", "可立即调度"); return node; }
  if (item.queue_state === "waiting_cancel_confirmation") {
    addText(node, "strong", "等待终止确认");
    if (item.action) { addText(node, "small", "动作 " + item.action.status); }
    return node;
  }
  if (item.queue_state === "waiting_probe") {
    addText(node, "strong", "可立即探测"); return node;
  }
  if (item.queue_state === "waiting_backoff" && item.next_executable_at) {
    addText(node, "strong", timeText(item.next_executable_at));
    addText(node, "small", secondsUntil(item.next_executable_at) + " 秒后");
    return node;
  }
  if (item.action && item.action.status !== "completed") {
    addText(node, "small", "动作 " + item.action.status);
  }
  addText(node, "strong", "—");
  return node;
}
function translationErrorNode(item) {
  var node = document.createElement("div"); node.className = "translation-error";
  addText(node, "strong", item.error_code || "—");
  if (item.http_status) { addText(node, "small", "HTTP " + item.http_status); }
  if (item.failure_stage) { addText(node, "small", translationStageLabels[item.failure_stage] || item.failure_stage); }
  if (item.diagnostic_id) { addText(node, "small", "诊断 " + item.diagnostic_id); }
  return node;
}
function translationMatches(item) {
  if (translationFilter === "all") { return true; }
  if (translationFilter === "online") { return item.build_status === "online"; }
  if (translationFilter === "failed") {
    return ["failed", "configuration_blocked", "cancelled"].includes(item.status);
  }
  return item.status === translationFilter;
}
function queueTranslationRetry(item, action) {
  setBusy(action, true);
  api("/admin/api/translations/retry", {task_id: item.task_id})
    .then(function () { say("单篇重试已排队，等待 worker 调度。", true); return loadTranslations(); })
    .catch(function (error) {
      if (error.message === "translation task is not retryable") {
        say("任务状态已变化，正在刷新翻译列表。", true);
        return loadTranslations();
      }
      say(error.message, false);
    })
    .finally(function () { setBusy(action, false); });
}
function queueTranslationRetryEdition(item, action) {
  if (!item || !confirm("将把该刊期全部失败/取消/阻断任务重新入队，确认执行？")) { return; }
  setBusy(action, true);
  api("/admin/api/translations/retry-edition", {edition_date: item.date, confirm: true})
    .then(function (result) {
      say("已重新入队 " + result.queued + " 篇（跳过 " + result.skipped + " 篇），等待 worker 调度。", true);
      return loadTranslations();
    })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
}
function queueTranslationDispatch(item, action) {
  setBusy(action, true);
  api("/admin/api/translations/dispatch", {task_id: item.task_id})
    .then(function () { say("任务已排队，等待 worker 调度。", true); return loadTranslations(); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
}
function queueTranslationCancel(item, action) {
  if (!confirm("确认终止这一个运行请求？系统会先关闭执行体，再从确认时间安排重试。")) { return; }
  setBusy(action, true);
  api("/admin/api/translations/cancel", {task_id: item.task_id, confirm: true})
    .then(function () { say("终止请求已提交，等待 worker 确认执行体关闭。", true); return loadTranslations(); })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
}
function queueTranslationUnblock(item, action) {
  if (!confirm("确认使用最近一次成功的受控测试解除 provider 阻断并恢复任务？")) { return; }
  setBusy(action, true);
  api("/admin/api/translations/unblock", {task_id: item.task_id, confirm: true})
    .then(function (result) {
      say(result.already_queued ? "解除阻断已在处理中。" : "已解除阻断并唤醒翻译队列。", true);
      return loadTranslations();
    })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
}
function queueTranslationProbe(item, action) {
  if (!item || !confirm("确认跳过冷却并用一篇失败文章执行正式 schema 探测？")) { return; }
  setBusy(action, true);
  api("/admin/api/translations/probe", {task_id: item.task_id, confirm: true})
    .then(function (result) {
      say(result.already_queued ? "探测已在队列，已重新唤醒 worker。" : "受控探测已排队。", true);
      return loadTranslations();
    })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
}
function queueTranslationRecover(item, action) {
  if (!confirm("确认旧执行体 lease 已过期，并请求恢复为可重试？系统不会直接重发。")) { return; }
  setBusy(action, true);
  api("/admin/api/translations/recover", {task_id: item.task_id, confirm: true})
    .then(function (result) {
      say(result.already_queued ? "恢复命令已在处理中。" : "恢复命令已排队，等待 worker 确认。", true);
      return loadTranslations();
    })
    .catch(function (error) { say(error.message, false); })
    .finally(function () { setBusy(action, false); });
}
function renderTranslations(data) {
  translationState = data;
  if (data.csrf_token) { csrf = data.csrf_token; }
  var editionSelect = field("translation-edition-select");
  var dates = data.edition_dates || [];
  var selectedDate = data.edition ? data.edition.date : (dates[0] || "");
  if (translationEdition && dates.includes(translationEdition)) { selectedDate = translationEdition; }
  editionSelect.replaceChildren();
  dates.forEach(function (date) {
    var option = document.createElement("option");
    option.value = date; option.textContent = date; option.selected = date === selectedDate;
    editionSelect.appendChild(option);
  });
  editionSelect.disabled = !dates.length;
  if (selectedDate) { translationEdition = selectedDate; }
  var edition = data.edition;
  field("translation-edition").textContent = edition ?
    "刊期 " + edition.date + " · " + edition.status +
      (edition.delivery_status ? " · 邮件 " + edition.delivery_status : "") +
      (edition.delivery_reason ? " (" + edition.delivery_reason + ")" : "") +
      (edition.error_code ? " · 错误 " + edition.error_code : "") +
      " · 更新 " + timeText(edition.last_updated) +
      (!dates.length ? " · 当前无待处理任务，显示最近一期结果" : "") :
    "暂无自动化刊期";
  var provider = field("translation-provider"); provider.replaceChildren();
  var providerCopy = document.createElement("div");
  addText(providerCopy, "strong", data.provider.id ? data.provider.id + " · " + data.provider.state : "provider 未建立任务");
  addText(
    providerCopy,
    "span",
    "执行 " + data.provider.current_concurrency +
      " · 待调度 " + data.provider.waiting_dispatch_count +
      " · 退避 " + data.provider.waiting_backoff_count +
      " · 待终止 " + data.provider.waiting_cancel_count +
      " · 待探测 " + data.provider.waiting_probe_count +
      " · 连续失败 " + data.provider.consecutive_failures +
      (data.provider.waiting_backoff_count && data.provider.next_executable_at ?
        " · 最近可执行 " + timeText(data.provider.next_executable_at) : "") +
      (data.provider.next_probe_at ? " · 下次探测 " + timeText(data.provider.next_probe_at) : "") +
      (data.provider.recovery_mode ? " · 恢复限流" : ""),
    "meta"
  );
  provider.appendChild(providerCopy);
  var probeButton = button("立即探测", function (event) {
    queueTranslationProbe(
      data.probe_task_id ? {task_id: data.probe_task_id} : null,
      event.currentTarget
    );
  });
  probeButton.disabled = !data.probe_task_id;
  probeButton.title = data.probe_task_id ? "执行一次正式单篇探测" : "当前无需探测";
  provider.appendChild(probeButton);
  if (edition && edition.retry_edition_available) {
    var retryEditionButton = button("重试全部失败篇", function (event) {
      queueTranslationRetryEdition(edition, event.currentTarget);
    });
    retryEditionButton.title = "批量恢复该刊期的终态任务,消除 partial 停滞";
    provider.appendChild(retryEditionButton);
  }

  var stats = field("translation-stats"); stats.replaceChildren();
  [["总任务", "total"], ["已上线", "online"], ["翻译中", "running"], ["待重试", "retry_wait"], ["失败", "failed"]]
    .forEach(function (entry) { statCard(stats, entry[0], data.summary[entry[1]]); });
  var items = data.items.filter(translationMatches);
  var rows = items.map(function (item) {
    var title = document.createElement("span"); title.className = "translation-title";
    title.textContent = item.title; title.title = item.title; title.tabIndex = 0;
    var actions = document.createElement("div"); actions.className = "translation-actions";
    if ((item.available_actions || []).includes("recover")) {
      actions.appendChild(button("恢复为可重试", function (event) {
        queueTranslationRecover(item, event.currentTarget);
      }));
    }
    if ((item.available_actions || []).includes("dispatch")) {
      actions.appendChild(button("立即调度", function (event) {
        queueTranslationDispatch(item, event.currentTarget);
      }));
    }
    if ((item.available_actions || []).includes("retry")) {
      actions.appendChild(button(
        item.status === "configuration_blocked" ? "解除阻断并重试" : "立即重试",
        function (event) { queueTranslationRetry(item, event.currentTarget); }
      ));
    }
    if ((item.available_actions || []).includes("unblock")) {
      actions.appendChild(button("解除阻断", function (event) {
        queueTranslationUnblock(item, event.currentTarget);
      }));
    }
    if ((item.available_actions || []).includes("probe")) {
      actions.appendChild(button("立即探测", function (event) {
        queueTranslationProbe(item, event.currentTarget);
      }));
    }
    if ((item.available_actions || []).includes("cancel")) {
      actions.appendChild(button("终止", function (event) { queueTranslationCancel(item, event.currentTarget); }, "danger"));
    } else if (item.cancel_requested) {
      addText(actions, "span", "终止中", "meta");
    }
    if (!actions.childNodes.length) { addText(actions, "span", "—", "meta"); }
    return [
      title,
      translationStatusLabels[item.status] || item.status,
      translationStageNode(item),
      item.attempt_count,
      translationErrorNode(item),
      translationNextNode(item),
      item.build_status,
      actions
    ];
  });
  var list = field("translation-list"); list.replaceChildren();
  if (!rows.length) { addText(list, "p", "当前筛选无任务。", "meta"); return; }
  list.appendChild(table(["文章", "状态", "阶段", "尝试", "错误代码", "下一次执行", "上线", "操作"], rows));
}
function loadTranslations() {
  var query = translationEdition ? "?edition=" + encodeURIComponent(translationEdition) : "";
  return api("/admin/api/translations" + query)
    .then(renderTranslations)
    .catch(function (error) { say(error.message, false); });
}
function stopTranslationUpdates() {
  if (translationSource) { translationSource.close(); translationSource = null; }
  if (translationPoll) { clearInterval(translationPoll); translationPoll = null; }
}
function startTranslationUpdates() {
  stopTranslationUpdates();
  loadTranslations();
  if (!("EventSource" in window)) {
    translationPoll = setInterval(loadTranslations, 3000);
    return;
  }
  var query = translationEdition ? "?edition=" + encodeURIComponent(translationEdition) : "";
  translationSource = query ?
    new EventSource("/admin/api/translations/events" + query) :
    new EventSource("/admin/api/translations/events");
  translationSource.addEventListener("translation-state", function (event) {
    renderTranslations(JSON.parse(event.data));
  });
  translationSource.onerror = function () {
    if (translationSource) { translationSource.close(); translationSource = null; }
    if (!translationPoll) { translationPoll = setInterval(loadTranslations, 3000); }
  };
}
document.querySelectorAll("[data-translation-filter]").forEach(function (control) {
  control.addEventListener("click", function () {
    translationFilter = control.dataset.translationFilter;
    document.querySelectorAll("[data-translation-filter]").forEach(function (item) {
      item.setAttribute("aria-pressed", String(item === control));
    });
    if (translationState) { renderTranslations(translationState); }
  });
});
field("translation-refresh").addEventListener("click", loadTranslations);
field("translation-edition-select").addEventListener("change", function (event) {
  translationEdition = event.currentTarget.value;
  startTranslationUpdates();
});

var currentEdition = ""; var manualPreview = null;
function loadDelivery() {
  api("/admin/api/delivery").then(function (data) {
    currentEdition = data.current_release.edition_date; field("manual-edition").value = currentEdition;
    var box = field("delivery-summary"); box.replaceChildren();
    addText(box, "p", "时区 " + data.timezone + " · 每日 " + data.schedule_time + " · 下次 " + data.next_schedule);
    if (data.latest_run) {
      var run = data.latest_run;
      addText(box, "strong", run.mode + " · " + run.status + " · run " + run.run_id);
      addText(box, "p", "刊期 " + run.edition_date + " · 开始 " + run.started_at + " · 结束 " + (run.finished_at || "运行中"));
      addText(box, "p", "总人数 " + run.total_count + " · 成功 " + run.sent_count + " · 失败 " + run.failed_count + " · unknown " + run.unknown_count + " · " + (run.degraded ? "存在缺译降级" : "无缺译降级") + " · 错误分类 " + (run.error_category || "—"));
    } else {
      addText(box, "strong", "暂无正式投递运行");
    }
    if (data.current_preview) {
      addText(box, "p", "当前刊期 " + currentEdition + " · 预览收件人数 " + data.current_preview.recipient_count + " · 主文 " + data.current_preview.main_count + " · 简讯 " + data.current_preview.brief_count + " · 成功 " + data.summary.sent + " · 失败 " + data.summary.failed + " · unknown " + data.summary.unknown + " · 待处理 " + data.summary.pending);
    } else {
      addText(box, "p", "当前刊期 " + currentEdition + " · 邮件组合需先修改并保存：" + data.preview_validation.message);
    }
    var rows = data.states.map(function (item) { return [item.recipient_ref, item.status, item.attempt_count, item.error_category || "—", item.updated_at]; });
    field("delivery-list").replaceChildren(table(["收件人标识", "状态", "尝试", "错误分类", "更新时间"], rows));
  }).catch(function (error) { say(error.message, false); });
}
field("delivery-refresh").addEventListener("click", loadDelivery);
field("retry-failed").addEventListener("click", function () {
  if (!confirm("确认仅重试当前刊期 failed 收件人？sent 与 unknown 不会包含。")) { return; }
  api("/admin/api/delivery/retry-failed", {confirm: true, edition: currentEdition}).then(function (data) { say(data.message || "失败重试完成：成功 " + data.sent_count, data.ok && !data.error_category); loadDelivery(); }).catch(function (error) { say(error.message, false); });
});
field("retry-unknown").addEventListener("click", function () {
  if (!confirm("unknown 可能已送达，重试可能产生重复邮件。是否承担此风险并继续？")) { return; }
  api("/admin/api/delivery/retry-unknown", {confirm: true, confirm_duplicate_risk: true, edition: currentEdition}).then(function (data) { say(data.message || "unknown 风险重试完成：成功 " + data.sent_count, data.ok && !data.error_category); loadDelivery(); }).catch(function (error) { say(error.message, false); });
});
field("manual-preview").addEventListener("click", function () {
  api("/admin/api/delivery/manual-preview", {edition: field("manual-edition").value}).then(function (data) { manualPreview = data; field("manual-send").disabled = false; showMailPreview(data); say("指定刊期已预览；确认后默认只发送未成功者。", true); }).catch(function (error) { say(error.message, false); });
});
field("manual-send").addEventListener("click", function () {
  if (!manualPreview || !confirm("确认发送刚刚预览的指定刊期？默认只投递未成功者。")) { return; }
  api("/admin/api/delivery/manual", {edition: manualPreview.edition_date, preview_token: manualPreview.preview_token, fingerprint: manualPreview.fingerprint, confirm: true}).then(function (data) { manualPreview = null; field("manual-send").disabled = true; say(data.message || "人工投递完成：成功 " + data.sent_count, data.ok && !data.error_category); loadDelivery(); }).catch(function (error) { say(error.message, false); });
});

field("change-password").addEventListener("click", function () {
  api("/admin/api/password", {
    current_password: field("old-password").value,
    password: field("new-password").value
  }).then(function () {
    say("口令已修改，请重新登录。", true);
    location.reload();
  }).catch(function (error) { say(error.message, false); });
});
field("logout").addEventListener("click", function () {
  api("/admin/api/logout", {}).then(function () { location.reload(); });
});
load();
</script>
</body>
</html>
"""
