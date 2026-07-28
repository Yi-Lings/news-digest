# ruff: noqa: E501
"""Loopback static preview and authenticated production Admin server."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import ipaddress
import json
import secrets
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
    public_subscription_enabled_from_env,
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
    validate_smtp,
)
from news_digest.delivery.publisher import resolve_published_release
from news_digest.rendering.email import render_email_preview
from news_digest.storage import db
from news_digest.translation.client import ApiTranslator, TranslationError

_APR1_CHARS = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SESSION_TTL_SECONDS = 7 * 24 * 3600
_SESSION_COOKIE = "nd_admin_session"
_PUBLIC_CSRF_COOKIE = "nd_public_csrf"
_PUBLIC_CSRF_TTL_SECONDS = 2 * 3600
_PUBLIC_BODY_LIMIT = 1_024
_PUBLIC_EMAIL_LIMIT = 254
_PUBLIC_TOKEN_LIMIT = 512
_BODY_LIMIT = 16_384
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
_PUBLIC_SUBMISSION_MESSAGE = "如果该地址可以订阅，我们会发送一封确认邮件。"
_PUBLIC_CONFIRM_MESSAGE = "确认请求已处理；如链接有效，订阅将生效。"
_PUBLIC_UNSUBSCRIBE_MESSAGE = "退订请求已处理；如链接有效，后续邮件将停止。"


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


def _public_csrf_token(secret: bytes, nonce: str, expires_at: int) -> str:
    payload = f"{nonce}|{expires_at}"
    digest = hmac.new(secret, f"public-csrf|{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{digest}"


def _verify_public_csrf(secret: bytes, cookie_nonce: str, token: str, now: float) -> bool:
    parts = token.split("|")
    if len(parts) != 3 or not cookie_nonce:
        return False
    nonce, raw_expires, digest = parts
    try:
        expires_at = int(raw_expires)
    except ValueError:
        return False
    if expires_at < now or nonce != cookie_nonce:
        return False
    expected = hmac.new(
        secret, f"public-csrf|{nonce}|{expires_at}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


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
    project_root: Path
    env_file: str
    profiles_file: str
    serve_static_files: bool
    loopback_public_subscription: bool
    htpasswd_file: Path | None
    db_path: Path | None
    translation_db_path: Path | None
    site_url: str
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


class PreviewHandler(SimpleHTTPRequestHandler):
    """Static site plus authenticated ``/admin`` HTML and JSON API."""

    server: _AdminServer

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
        token = self._session_token()
        if not token:
            return False
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        return _verify_session(_session_secret(secret_file), token)

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
        host = self.headers.get("Host", "")
        if not origin or not host or "," in origin or "," in host:
            return False
        try:
            parts = urlsplit(origin)
            _ = parts.port
        except ValueError:
            return False
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.path
            and not parts.query
            and not parts.fragment
            and parts.netloc.casefold() == host.casefold()
        )

    def _csrf_for_response(self) -> str:
        if not self._login_required:
            return ""
        token = self._session_token()
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        return _csrf_token(_session_secret(secret_file), token)

    def _admin_actor(self) -> str:
        token = self._session_token()
        if token:
            username = token.partition("|")[0]
            if username:
                return username[:128]
        return "local-admin"

    def _valid_public_csrf(self, token: str) -> bool:
        return _verify_public_csrf(
            self.server.public_secret,
            self._cookie(_PUBLIC_CSRF_COOKIE),
            token,
            self.server.clock(),
        )

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
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
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
        if path == "/admin/api/translations":
            self._handle_translations_get()
            return
        if path == "/admin/api/translations/events":
            self._handle_translation_events()
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
        if length < 0 or length > _BODY_LIMIT:
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
        elif path == "/admin/api/translations/retry":
            self._limited("translation-retry", lambda: self._handle_translation_retry(body))
        elif path == "/admin/api/translations/cancel":
            self._limited("translation-cancel", lambda: self._handle_translation_cancel(body))
        elif path == "/admin/api/translations/probe":
            self._limited("translation-probe", lambda: self._handle_translation_probe(body))
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
                "can_change_password": self.server.htpasswd_file is not None,
            },
        )

    def _public_endpoint_ready(self) -> bool:
        return self.server.db_path is not None and (
            self.server.loopback_public_subscription or bool(self.server.site_url)
        )

    def _public_subscription_base(self) -> str:
        if self.server.loopback_public_subscription:
            return f"http://127.0.0.1:{self.server.server_port}"
        return subscriptions.public_https_base(self.server.site_url)

    def _public_request_host_valid(self) -> bool:
        host = self.headers.get("Host", "")
        if not host or "," in host:
            return False
        if self.server.loopback_public_subscription:
            return host == f"127.0.0.1:{self.server.server_port}"
        return True

    def _public_submission_ready(self) -> bool:
        if not self.server.public_subscription_enabled or not self._public_endpoint_ready():
            return False
        try:
            env = read_env(self._env_path())
            if not public_subscription_enabled_from_env(env):
                return False
            self._public_subscription_base()
            smtp = smtp_config_from_env(env)
            if not smtp.delivery_enabled:
                return False
            validate_smtp(smtp, require_recipients=False)
        except (AdminEmailError, MailError, ValueError):
            return False
        return True

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
        if not self._public_submission_ready():
            self._json(404, {"error": "接口未启用"})
            return
        if not self._public_request_host_valid():
            self._json(403, {"error": "Host 无效"})
            return
        nonce = secrets.token_urlsafe(32)
        expires_at = int(self.server.clock()) + _PUBLIC_CSRF_TTL_SECONDS
        token = _public_csrf_token(self.server.public_secret, nonce, expires_at)
        cookie = (
            f"{_PUBLIC_CSRF_COOKIE}={nonce}; Max-Age={_PUBLIC_CSRF_TTL_SECONDS}; "
            "Path=/subscribe/api/; SameSite=Strict"
        )
        if not self.server.loopback_public_subscription:
            cookie += "; Secure"
        self._json(200, {"csrf_token": token}, extra_headers={"Set-Cookie": cookie})

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
        if not self._public_submission_ready():
            self._json(404, {"error": "接口未启用"})
            return
        if not self._public_request_host_valid():
            self._json(403, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        if not self._same_origin():
            self._json(403, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            self._json(415, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        length, length_error = self._public_length(_PUBLIC_BODY_LIMIT)
        if length_error is not None:
            self._json(
                413 if length_error == "too_large" else 411,
                {"message": _PUBLIC_SUBMISSION_MESSAGE},
            )
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        if (
            not isinstance(body, dict)
            or set(body) != {"email", "website", "csrf_token"}
            or not isinstance(body.get("email"), str)
            or not isinstance(body.get("website"), str)
            or not isinstance(body.get("csrf_token"), str)
        ):
            self._json(400, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        if not self._valid_public_csrf(body["csrf_token"]):
            self._json(403, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        if not self._consume_sensitive_limit("public-subscribe"):
            self._json(429, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return
        email = body["email"]
        if (
            body["website"]
            or len(body["website"]) > 0
            or not email
            or len(email) > _PUBLIC_EMAIL_LIMIT
            or "\r" in email
            or "\n" in email
        ):
            self._json(202, {"message": _PUBLIC_SUBMISSION_MESSAGE})
            return

        conn = None
        submission = None
        try:
            conn = db.connect(self.server.db_path)
            submission = subscriptions.submit_subscription(
                conn,
                email,
                self._public_subscription_base(),
                dt.datetime.now(dt.UTC),
                allow_loopback_http=self.server.loopback_public_subscription,
            )
            if submission.should_send_confirmation:
                if not self._public_submission_ready():
                    raise MailError("configuration")
                smtp = self._saved_smtp_config()
                if not smtp.delivery_enabled:
                    raise MailError("configuration")
                report = self.server.confirmation_sender(
                    smtp, submission.recipient or "", submission.confirmation_url or ""
                )
                if report is not None and report.unknown_count:
                    result = next(item for item in report.results if item.status == "unknown")
                    print(
                        "public-subscription confirmation_unknown "
                        f"category={result.error_category or 'smtp_protocol'} "
                        f"stage={result.error_stage or 'data_final_response'} "
                        "action=verify_provider_queue_before_retry"
                    )
        except Exception as error:
            if conn is not None and submission is not None and submission.confirmation_token:
                subscriptions.abandon_confirmation(conn, submission.confirmation_token)
            category = getattr(error, "category", "configuration")
            print(f"public-subscription confirmation_failed category={category}")
        finally:
            if conn is not None:
                conn.close()
        self._json(202, {"message": _PUBLIC_SUBMISSION_MESSAGE})

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
        except (AdminEmailError, DeliveryServiceError, ValueError) as error:
            self._safe_error(error)
            return
        self._json(200, {"ok": True, "password_set": bool(smtp.password)})

    def _handle_mail_clear_password(self, body: dict[str, Any]) -> None:
        if set(body) != {"confirm"}:
            self._json(400, {"error": "清除密码字段无效", "category": "configuration"})
            return
        try:
            clear_password(self._env_path(), confirm=body.get("confirm") is True)
        except AdminEmailError as error:
            self._safe_error(error)
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
            added = subscriptions.add_admin_test_recipient(
                conn, body["email"], dt.datetime.now(dt.UTC)
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
            enabled = subscriptions.enable_subscription_id(
                conn, body["id"], dt.datetime.now(dt.UTC)
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

    def _translation_payload(self) -> dict[str, Any]:
        if self.server.translation_db_path is None:
            raise RuntimeError("翻译任务数据库未配置")
        conn = db.connect(self.server.translation_db_path)
        try:
            edition = db.latest_automation_edition(conn)
            if edition is None:
                return {
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
            tasks = db.list_translation_tasks(conn, edition.edition_date)
            provider_id = tasks[0].provider_id if tasks else ""
            circuit = db.get_provider_circuit(conn, provider_id) if provider_id else None
            circuit_state = circuit.state if circuit else "closed"
            now = dt.datetime.fromtimestamp(self.server.clock(), dt.UTC)

            def parsed(value: str | None) -> dt.datetime | None:
                return dt.datetime.fromisoformat(value) if value else None

            def next_executable(task: db.TranslationTask) -> str | None:
                if task.status not in {"pending", "retry_wait"}:
                    return None
                if task.manual_retry_requested_at or task.manual_probe_requested_at:
                    return task.next_retry_at or now.isoformat()
                candidates = [parsed(task.next_retry_at)]
                if circuit_state == "open" and circuit is not None:
                    candidates.append(parsed(circuit.next_probe_at))
                available = [value for value in candidates if value is not None]
                return max(available).isoformat() if available else now.isoformat()

            def queue_state(task: db.TranslationTask) -> str:
                if task.status == "running":
                    return (
                        "waiting_cancel_confirmation"
                        if task.cancel_requested_at is not None
                        else "executing"
                    )
                if task.status in {"pending", "retry_wait"}:
                    if circuit_state == "configuration_blocked":
                        return "blocked"
                    if task.manual_retry_requested_at or task.manual_probe_requested_at:
                        return "waiting_dispatch"
                    available = parsed(next_executable(task))
                    if circuit_state == "open" and (
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
                    if task.status in {"failed", "retry_wait", "cancelled", "pending"}
                ),
                key=lambda task: task.status == "pending",
            )
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
                    "last_updated": last_updated,
                },
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
                        "retry_allowed": (
                            task.status in {"failed", "retry_wait", "cancelled"}
                            and circuit_state == "closed"
                        ),
                        "cancel_allowed": (
                            task.status == "running" and task.cancel_requested_at is None
                        ),
                    }
                    for task in tasks
                ],
                "probe_task_id": (
                    probe_candidates[0].task_id
                    if circuit_state == "open" and probe_candidates
                    else None
                ),
                "csrf_token": self._csrf_for_response(),
            }
        finally:
            conn.close()

    def _handle_translations_get(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        try:
            payload = self._translation_payload()
        except RuntimeError as error:
            self._json(503, {"error": str(error), "category": "configuration"})
            return
        self._json(200, payload)

    def _handle_translation_events(self) -> None:
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        try:
            payload = self._translation_payload()
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
        self._json(202, {"ok": True, "status": task.status})

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
        self._json(202, {"ok": True, "cancel_requested": task.cancel_requested_at is not None})

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
                queued = db.queue_provider_probe(
                    conn,
                    task.provider_id,
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
        self._json(202, {"ok": True, "status": queued.status})

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
        token = self._public_token(raw_token)
        if self._public_endpoint_ready() and token:
            conn = db.connect(self.server.db_path)
            try:
                subscriptions.confirm_subscription(conn, token, dt.datetime.now(dt.UTC))
            finally:
                conn.close()
        self._html(_public_result_page("确认订阅", _PUBLIC_CONFIRM_MESSAGE))

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

    def _limited(self, action: str, callback: Callable[[], None]) -> None:
        if not self._consume_sensitive_limit(action):
            self._json(429, {"error": "敏感操作过于频繁，请稍后重试"})
            return
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
        if not verify_htpasswd(self.server.htpasswd_file, username, password):
            time.sleep(0.1)
            self._json(401, {"error": "用户名或口令不正确"})
            return
        secret_file = self.server.htpasswd_file.parent / "session-secret"
        secret = _session_secret(secret_file)
        expires_at = int(time.time()) + _SESSION_TTL_SECONDS
        self._set_session_cookie(_sign_session(secret, username, expires_at), _SESSION_TTL_SECONDS)

    def _handle_password(self, body: dict[str, Any]) -> None:
        if self.server.htpasswd_file is None:
            self._json(404, {"error": "本模式不提供网页改密"})
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
    server.htpasswd_file = Path(htpasswd_file) if htpasswd_file is not None else None
    server.db_path = Path(db_path) if db_path is not None else None
    server.translation_db_path = (
        Path(translation_db_path) if translation_db_path is not None else server.db_path
    )
    server.site_url = site_url
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
  <label for="username">用户名</label>
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
  display: flex; align-items: center; gap: .8rem; min-width: 0;
  border-bottom: 3px double var(--ink); padding: .5rem 0 .9rem;
}
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
input:hover, textarea:hover, select:hover { border-color: var(--ink); }
textarea { min-height: 6rem; resize: vertical; }
.checks { display: flex; flex-wrap: wrap; align-items: center; gap: .35rem 1rem; margin-top: .55rem; }
.checks label { display: inline-flex; align-items: center; gap: .35rem; margin: 0; color: var(--ink); }
.checks input { width: auto; min-height: auto; }
.actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 9.5rem), 1fr)); gap: .5rem; margin-top: 1rem; }
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
#subscription-list, #delivery-list { max-width: 100%; overflow-x: auto; }
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
  .data-table td { grid-template-columns: minmax(4.8rem, .38fr) minmax(0, 1fr); }
}
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
  </header>
  <nav class="nav" role="tablist" aria-label="管理职责">
    <button class="tab" id="tab-models" role="tab" data-tab="models" aria-controls="models" aria-selected="true">模型接口</button>
    <button class="tab" id="tab-mail" role="tab" data-tab="mail" aria-controls="mail" aria-selected="false">邮件设置</button>
    <button class="tab" id="tab-subscriptions" role="tab" data-tab="subscriptions" aria-controls="subscriptions" aria-selected="false">订阅管理</button>
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

  <section class="workspace" id="subscriptions" role="tabpanel" aria-labelledby="tab-subscriptions" hidden>
    <h2 id="subscriptions-heading">订阅管理</h2>
    <p class="note">主页订阅与 Admin 手工添加共用这份名单；正式邮件只投递 active 账号。</p>
    <div id="subscription-stats" class="stats"></div>
    <section class="panel">
      <label for="subscription-email">新增订阅账号</label>
      <input id="subscription-email" type="email">
      <div class="actions"><button id="subscription-add">新增账号</button><button id="subscriptions-refresh">刷新名单</button></div>
      <div id="subscription-list"></div>
    </section>
  </section>

  <section class="workspace" id="translations" role="tabpanel" aria-labelledby="tab-translations" hidden>
    <div class="translation-head">
      <div>
        <h2 id="translations-heading">翻译状态</h2>
        <p id="translation-edition" class="note">暂无自动化刊期</p>
      </div>
      <button id="translation-refresh" type="button">刷新</button>
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
    if (tab.dataset.tab === "subscriptions") { loadSubscriptions(); }
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
function loadSubscriptions() {
  api("/admin/api/subscriptions").then(function (data) {
    var stats = field("subscription-stats"); stats.replaceChildren();
    ["pending", "active", "unsubscribed", "disabled"].forEach(function (name) { statCard(stats, name, data.counts[name]); });
    var rows = data.items.map(function (item) {
      var actions = document.createElement("div"); actions.className = "actions";
      if (item.status === "active") {
        actions.appendChild(button("发送验证邮件", function (event) {
          if (!confirm("确认只向 " + item.email_masked + " 发送一封小体积 SMTP 验证邮件？")) { return; }
          sendMailTest(event.currentTarget, "smtp_smoke", "SMTP 验证邮件", item.id);
        }));
        actions.appendChild(button("发送测试邮件", function (event) {
          if (!confirm("确认只向 " + item.email_masked + " 发送一封测试邮件？")) { return; }
          sendMailTest(event.currentTarget, "digest", "测试邮件", item.id);
        }));
        actions.appendChild(button("停用", function () {
          if (!confirm("确认停用订阅账号 " + item.email_masked + "？")) { return; }
          api("/admin/api/subscriptions/disable", {id: item.id, confirm: true}).then(function () { say("订阅账号已停用。", true); loadSubscriptions(); }).catch(function (error) { say(error.message, false); });
        }, "danger"));
      } else if (item.status === "disabled") {
        actions.appendChild(button("启用", function () {
          api("/admin/api/subscriptions/enable", {id: item.id, confirm: true}).then(function () { say("订阅账号已启用。", true); loadSubscriptions(); }).catch(function (error) { say(error.message, false); });
        }));
      }
      actions.appendChild(button("删除", function () {
        if (!confirm("确认永久删除订阅账号 " + item.email_masked + "？")) { return; }
        api("/admin/api/subscriptions/delete", {id: item.id, confirm: true}).then(function () { say("订阅账号已删除。", true); loadSubscriptions(); }).catch(function (error) { say(error.message, false); });
      }, "danger"));
      return [item.email_masked, item.recipient_key, item.status, item.updated_at, actions];
    });
    field("subscription-list").replaceChildren(table(["地址", "标识", "状态", "更新时间", "操作"], rows));
  }).catch(function (error) { say(error.message, false); });
}
field("subscription-add").addEventListener("click", function () {
  api("/admin/api/subscriptions/add", {email: field("subscription-email").value}).then(function () { field("subscription-email").value = ""; say("订阅账号已新增并自动进入统一名单。", true); loadSubscriptions(); }).catch(function (error) { say(error.message, false); });
});
field("subscriptions-refresh").addEventListener("click", loadSubscriptions);

var translationFilter = "all";
var translationState = null;
var translationSource = null;
var translationPoll = null;
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
    addText(node, "strong", "等待终止确认"); return node;
  }
  if (item.queue_state === "waiting_probe") {
    addText(node, "strong", "可立即探测"); return node;
  }
  if (item.queue_state === "waiting_backoff" && item.next_executable_at) {
    addText(node, "strong", timeText(item.next_executable_at));
    addText(node, "small", secondsUntil(item.next_executable_at) + " 秒后");
    return node;
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
function renderTranslations(data) {
  translationState = data;
  if (data.csrf_token) { csrf = data.csrf_token; }
  var edition = data.edition;
  field("translation-edition").textContent = edition ?
    "刊期 " + edition.date + " · " + edition.status + " · 更新 " + timeText(edition.last_updated) :
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
    if (!data.probe_task_id || !confirm("确认跳过冷却并用一篇失败文章执行正式 schema 探测？")) { return; }
    var action = event.currentTarget; setBusy(action, true);
    api("/admin/api/translations/probe", {task_id: data.probe_task_id, confirm: true})
      .then(function () { say("受控探测已排队。", true); return loadTranslations(); })
      .catch(function (error) { say(error.message, false); })
      .finally(function () { setBusy(action, false); });
  });
  probeButton.disabled = !data.probe_task_id;
  probeButton.title = data.probe_task_id ? "执行一次正式单篇探测" : "当前无需探测";
  provider.appendChild(probeButton);

  var stats = field("translation-stats"); stats.replaceChildren();
  [["总任务", "total"], ["已上线", "online"], ["翻译中", "running"], ["待重试", "retry_wait"], ["失败", "failed"]]
    .forEach(function (entry) { statCard(stats, entry[0], data.summary[entry[1]]); });
  var items = data.items.filter(translationMatches);
  var rows = items.map(function (item) {
    var title = document.createElement("span"); title.className = "translation-title";
    title.textContent = item.title; title.title = item.title; title.tabIndex = 0;
    var actions = document.createElement("div"); actions.className = "translation-actions";
    if (item.retry_allowed) {
      actions.appendChild(button("立即重试", function (event) { queueTranslationRetry(item, event.currentTarget); }));
    }
    if (item.cancel_allowed) {
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
  return api("/admin/api/translations")
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
  translationSource = new EventSource("/admin/api/translations/events");
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
  api("/admin/api/delivery/retry-failed", {confirm: true, edition: currentEdition}).then(function (data) { say("失败重试完成：成功 " + data.sent_count, data.ok); loadDelivery(); }).catch(function (error) { say(error.message, false); });
});
field("retry-unknown").addEventListener("click", function () {
  if (!confirm("unknown 可能已送达，重试可能产生重复邮件。是否承担此风险并继续？")) { return; }
  api("/admin/api/delivery/retry-unknown", {confirm: true, confirm_duplicate_risk: true, edition: currentEdition}).then(function (data) { say("unknown 风险重试完成：成功 " + data.sent_count, data.ok); loadDelivery(); }).catch(function (error) { say(error.message, false); });
});
field("manual-preview").addEventListener("click", function () {
  api("/admin/api/delivery/manual-preview", {edition: field("manual-edition").value}).then(function (data) { manualPreview = data; field("manual-send").disabled = false; showMailPreview(data); say("指定刊期已预览；确认后默认只发送未成功者。", true); }).catch(function (error) { say(error.message, false); });
});
field("manual-send").addEventListener("click", function () {
  if (!manualPreview || !confirm("确认发送刚刚预览的指定刊期？默认只投递未成功者。")) { return; }
  api("/admin/api/delivery/manual", {edition: manualPreview.edition_date, preview_token: manualPreview.preview_token, fingerprint: manualPreview.fingerprint, confirm: true}).then(function (data) { manualPreview = null; field("manual-send").disabled = true; say("人工投递完成：成功 " + data.sent_count, data.ok); loadDelivery(); }).catch(function (error) { say(error.message, false); });
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
