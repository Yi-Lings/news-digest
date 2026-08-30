"""Double opt-in subscription and one-click unsubscribe domain service.

Public HTTP handlers should return only the constant public messages from this module. Complete
mailbox addresses and raw tokens are returned solely to internal mail-delivery orchestration.
No function performs network or filesystem IO.
"""

import datetime as dt
import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from news_digest.config import normalize_email_address
from news_digest.storage import db

_SUBMISSION_MESSAGE = "如果该地址可以订阅，我们会发送一封确认邮件。"
_CONFIRM_MESSAGE = "确认请求已处理；如链接有效，订阅将生效。"
_UNSUBSCRIBE_MESSAGE = "退订请求已处理；如链接有效，后续邮件将停止。"
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class SubscriptionSubmission:
    """Uniform public response plus optional internal confirmation-mail work."""

    public_message: str
    confirmation_token: str | None = None
    confirmation_url: str | None = None
    recipient: str | None = None

    @property
    def should_send_confirmation(self) -> bool:
        return self.confirmation_token is not None


@dataclass(frozen=True)
class TokenActionResult:
    """A token result that never includes a mailbox address or subscription state."""

    accepted: bool
    public_message: str


@dataclass(frozen=True)
class UnsubscribePageData:
    """GET-only page data; constructing it never mutates subscription state."""

    token_accepted: bool
    public_message: str
    one_click_post_value: str = "List-Unsubscribe=One-Click"


@dataclass(frozen=True)
class RecipientUnsubscribe:
    """Internal per-recipient data used while constructing one private message."""

    recipient: str
    url: str


def _utc_iso(value: dt.datetime) -> str:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(dt.UTC).isoformat(timespec="seconds")


def _expiry(now: dt.datetime, lifetime: dt.timedelta) -> str:
    if lifetime <= dt.timedelta(0):
        raise ValueError("token lifetime must be positive")
    return _utc_iso(now + lifetime)


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_digest(token: str) -> str:
    """Return the only token representation that may be persisted."""
    if not isinstance(token, str) or not token or len(token) > 512:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_token(token: str) -> str:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise ValueError("generated token does not meet the security contract")
    if any(character.isspace() for character in token) or "/" in token:
        raise ValueError("generated token is not URL-safe")
    return token


def _valid_hostname(hostname: str) -> bool:
    if len(hostname) > 253:
        return False
    for label in hostname.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                not (character.isascii() and (character.isalnum() or character == "-"))
                for character in label
            )
        ):
            return False
    return True


def _public_https_base(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("public URL must be an absolute HTTPS base URL")
    raw = base_url.strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError("public URL must be an absolute HTTPS base URL") from error
    if (
        not raw
        or raw != base_url
        or parts.scheme.lower() != "https"
        or not parts.netloc
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in raw)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("public URL must be an absolute HTTPS base URL")
    hostname = host.casefold()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if (
            len(labels) < 2
            or hostname.endswith((".local", ".localhost", ".internal", ".lan"))
            or not _valid_hostname(hostname)
        ):
            raise ValueError("public URL must use a public hostname") from None
        rendered_host = hostname
    else:
        if not address.is_global:
            raise ValueError("public URL must use a public hostname")
        rendered_host = f"[{hostname}]" if address.version == 6 else hostname
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def public_https_base(base_url: str) -> str:
    """Validate and normalize the production public site base URL."""
    return _public_https_base(base_url)


def _loopback_http_base(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("loopback URL must be an absolute HTTP URL")
    raw = base_url.strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError("loopback URL must be an absolute HTTP URL") from error
    if (
        not raw
        or raw != base_url
        or parts.scheme.lower() != "http"
        or host != "127.0.0.1"
        or port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in raw)
        or not 1 <= port <= 65535
    ):
        raise ValueError("loopback URL must use http://127.0.0.1 with an explicit port")
    return urlunsplit(("http", f"127.0.0.1:{port}", "", "", ""))


def _token_url(
    base_url: str,
    path: str,
    token: str,
    *,
    allow_loopback_http: bool = False,
) -> str:
    base = _loopback_http_base(base_url) if allow_loopback_http else _public_https_base(base_url)
    return f"{base}{path}/{token}"


def submit_subscription(
    conn,
    email: str,
    base_url: str,
    now: dt.datetime,
    *,
    allow_loopback_http: bool = False,
    lifetime: dt.timedelta = dt.timedelta(hours=24),
    token_factory=_new_token,
) -> SubscriptionSubmission:
    """Start double opt-in without revealing whether the address already exists.

    Active and disabled duplicates are no-ops. A pending duplicate with a live token is also a
    no-op. An unsubscribed address starts a fresh pending flow and can become active only after
    its new confirmation token is consumed.
    """
    normalized = normalize_email_address(email, "subscription email")
    token = _safe_token(token_factory())
    url = _token_url(
        base_url,
        "/subscribe/confirm",
        token,
        allow_loopback_http=allow_loopback_http,
    )
    should_send = db.begin_public_subscription(
        conn,
        normalized,
        _utc_iso(now),
        token_digest(token),
        _expiry(now, lifetime),
    )
    if not should_send:
        return SubscriptionSubmission(_SUBMISSION_MESSAGE)
    return SubscriptionSubmission(_SUBMISSION_MESSAGE, token, url, normalized)


def abandon_confirmation(conn, token: str) -> bool:
    """Discard confirmation work that could not be delivered, leaving the row pending."""
    return db.abandon_confirmation_token(conn, token_digest(token))


def confirm_subscription(conn, token: str, now: dt.datetime) -> TokenActionResult:
    accepted = db.consume_confirmation_token(conn, token_digest(token), _utc_iso(now))
    return TokenActionResult(accepted, _CONFIRM_MESSAGE)


def prepare_unsubscribe(
    conn,
    recipient: str,
    base_url: str,
    now: dt.datetime,
    *,
    lifetime: dt.timedelta = dt.timedelta(days=90),
    token_factory=_new_token,
) -> RecipientUnsubscribe | None:
    """Create a dedicated unsubscribe token for one currently active recipient."""
    normalized = normalize_email_address(recipient, "recipient")
    token = _safe_token(token_factory())
    url = _token_url(base_url, "/unsubscribe", token)
    issued = db.issue_unsubscribe_token(
        conn,
        normalized,
        token_digest(token),
        _expiry(now, lifetime),
        _utc_iso(now),
    )
    return RecipientUnsubscribe(normalized, url) if issued else None


def unsubscribe_page_data(conn, token: str, now: dt.datetime) -> UnsubscribePageData:
    """Validate a GET request without consuming its token or changing subscription state."""
    state = db.inspect_subscription_token(conn, token_digest(token), "unsubscribe", _utc_iso(now))
    return UnsubscribePageData(state is not None, _UNSUBSCRIBE_MESSAGE)


def unsubscribe_one_click(conn, token: str, now: dt.datetime) -> TokenActionResult:
    """Process RFC 8058-style POST idempotently; invalid or expired tokens fail safely."""
    accepted = db.consume_unsubscribe_token(conn, token_digest(token), _utc_iso(now))
    return TokenActionResult(accepted, _UNSUBSCRIBE_MESSAGE)


def add_admin_test_recipient(conn, email: str, now: dt.datetime) -> bool:
    """Add or reactivate one unique address while preserving its original audit source."""
    normalized = normalize_email_address(email, "Admin test recipient")
    return db.add_admin_test_recipient(conn, normalized, _utc_iso(now))


def disable_admin_test_recipient(conn, email: str, now: dt.datetime) -> bool:
    normalized = normalize_email_address(email, "Admin test recipient")
    return db.disable_admin_test_recipient(conn, normalized, _utc_iso(now))


def enable_subscription_id(conn, subscription_id: int, now: dt.datetime) -> bool:
    if type(subscription_id) is not int or subscription_id < 1:
        raise ValueError("subscription id must be a positive integer")
    return db.enable_subscription_id(conn, subscription_id, _utc_iso(now))


def disable_subscription_id(conn, subscription_id: int, now: dt.datetime) -> bool:
    if type(subscription_id) is not int or subscription_id < 1:
        raise ValueError("subscription id must be a positive integer")
    return db.disable_subscription_id(conn, subscription_id, _utc_iso(now))


def delete_subscription_id(conn, subscription_id: int) -> bool:
    if type(subscription_id) is not int or subscription_id < 1:
        raise ValueError("subscription id must be a positive integer")
    return db.delete_subscription_id(conn, subscription_id)


def active_subscription_recipient_id(conn, subscription_id: int) -> str | None:
    if type(subscription_id) is not int or subscription_id < 1:
        raise ValueError("subscription id must be a positive integer")
    return db.active_subscription_recipient_id(conn, subscription_id)


def import_legacy_smtp_recipients_once(
    conn, recipients: tuple[str, ...], now: dt.datetime
) -> bool:
    normalized = tuple(
        normalize_email_address(address, "legacy SMTP recipient") for address in recipients
    )
    return db.import_legacy_smtp_recipients_once(conn, normalized, _utc_iso(now))


def disable_admin_test_recipient_id(conn, subscription_id: int, now: dt.datetime) -> bool:
    """Compatibility wrapper for the former Admin-test-only name."""
    return disable_subscription_id(conn, subscription_id, now)


def active_recipients(conn) -> tuple[str, ...]:
    """Single source for automatic, retry, and manual delivery recipient selection."""
    return db.active_subscription_recipients(conn)


def delivery_recipients(
    conn, edition_date: str, now: dt.datetime, *, retry_failed_only: bool = False
) -> tuple[str, ...]:
    """Select paid active recipients while respecting per-edition delivery state."""
    return db.eligible_delivery_recipients(
        conn, edition_date, _utc_iso(now), retry_failed_only=retry_failed_only
    )


def admin_subscription_list(conn) -> list[db.AdminSubscriptionState]:
    """Return only masked addresses and hashed references for Admin output."""
    return db.admin_subscription_states(conn)


def admin_subscription_counts(conn) -> dict[db.SubscriptionStatus, int]:
    return db.subscription_counts(conn)
