"""EasyPay-compatible payment protocol helpers without network side effects."""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from news_digest.config import decode_smtp_password


class PaymentError(RuntimeError):
    """A rejected payment configuration or gateway message."""

    def __init__(self, message: str, *, code: str = "PAYMENT_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EpayConfig:
    base_url: str
    merchant_id: str
    merchant_key: str = field(repr=False)
    payment_type: str
    site_url: str
    order_ttl_seconds: int = 300
    amount_hold_seconds: int = 3600

    def __post_init__(self) -> None:
        normalized_base = normalize_api_base(self.base_url)
        object.__setattr__(self, "base_url", normalized_base)
        _validate_url(normalized_base, "EPAY_API_BASE")
        _validate_url(self.site_url, "NEWS_SITE_URL")
        if not self.merchant_id.strip():
            raise PaymentError("EPAY_PID is required")
        if not self.merchant_key:
            raise PaymentError("EPAY_PKEY is required")
        if self.payment_type not in {"alipay", "wxpay"}:
            raise PaymentError("EPAY_PAYMENT_TYPE is invalid")
        if not 60 <= self.order_ttl_seconds <= 3600:
            raise PaymentError("EPAY_ORDER_TTL_SECONDS must be between 60 and 3600")
        if not self.order_ttl_seconds <= self.amount_hold_seconds <= 86400:
            raise PaymentError(
                "EPAY_AMOUNT_HOLD_SECONDS must be between the order TTL and 86400"
            )


@dataclass(frozen=True)
class PaymentNotification:
    merchant_order_no: str
    provider_trade_no: str
    amount_cents: int


@dataclass(frozen=True)
class PaymentCreation:
    provider_trade_no: str
    payment_url: str


@dataclass(frozen=True)
class PaymentQuery:
    merchant_order_no: str
    provider_trade_no: str
    amount_cents: int
    trade_status: Literal["TRADE_SUCCESS", "WAIT_BUYER_PAY", "TRADE_CLOSED"]


NEWS_NOTIFY_PATH = "/subscribe/api/payment/easypay"
_MAX_GATEWAY_RESPONSE_BYTES = 64 * 1024
_GATEWAY_TIMEOUT_SECONDS = 10


def _contains_unsafe_url_character(value: str) -> bool:
    return "\\" in value or ";" in value or any(
        character.isspace()
        or ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _canonical_hostname(value: str, field_name: str) -> str:
    if "%" in value:
        raise PaymentError(f"{field_name} contains an invalid hostname")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if ":" in value or re.fullmatch(r"[0-9.]+", value):
            raise PaymentError(f"{field_name} contains an invalid hostname") from None
        try:
            hostname = value.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            raise PaymentError(f"{field_name} contains an invalid hostname") from None
        absolute = hostname.endswith(".")
        dns_name = hostname[:-1] if absolute else hostname
        labels = dns_name.split(".")
        if (
            not dns_name
            or len(dns_name) > 253
            or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                is None
                for label in labels
            )
        ):
            raise PaymentError(f"{field_name} contains an invalid hostname") from None
        return dns_name + ("." if absolute else "")
    return str(address).casefold()


def _url_origin(value: str, field_name: str) -> tuple[str, str, int]:
    if _contains_unsafe_url_character(value):
        raise PaymentError(f"{field_name} contains unsupported URL characters")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise PaymentError(f"{field_name} is invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PaymentError(f"{field_name} must be an absolute HTTP URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise PaymentError(f"{field_name} contains unsupported URL components")
    try:
        port = parsed.port
    except ValueError:
        raise PaymentError(f"{field_name} has an invalid port") from None
    hostname = parsed.hostname
    if not hostname:
        raise PaymentError(f"{field_name} must include a hostname")
    hostname = _canonical_hostname(hostname, field_name)
    if port == 0:
        raise PaymentError(f"{field_name} has an invalid port")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise PaymentError(f"{field_name} must use HTTPS outside loopback")
    return parsed.scheme, hostname, port or (443 if parsed.scheme == "https" else 80)


def _validate_url(value: str, field_name: str) -> None:
    _url_origin(value, field_name)
    parsed = urlsplit(value)
    if parsed.query:
        raise PaymentError(f"{field_name} contains unsupported URL components")


def _validate_payment_url(value: str, allowed_base_url: str) -> None:
    if _url_origin(value, "payment URL") != _url_origin(
        allowed_base_url, "EPAY_API_BASE"
    ):
        raise PaymentError("payment URL must use the EasyPay API origin")


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_base_url: str) -> None:
        self.allowed_base_url = allowed_base_url
        self.allowed_origin = _url_origin(allowed_base_url, "EPAY_API_BASE")
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute_url = urljoin(req.full_url, newurl)
        if _url_origin(absolute_url, "payment gateway redirect") != self.allowed_origin:
            raise PaymentError("payment gateway redirect changed origin")
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


def _open_gateway(request: Request, *, allowed_base_url: str, timeout: int):
    opener = build_opener(_SameOriginRedirectHandler(allowed_base_url))
    return opener.open(request, timeout=timeout)


def normalize_api_base(value: str) -> str:
    if _contains_unsafe_url_character(value):
        raise PaymentError("EPAY_API_BASE contains unsupported URL characters")
    normalized = value.strip().rstrip("/")
    lowered = normalized.lower()
    for endpoint in ("/submit.php", "/mapi.php", "/api.php"):
        if lowered.endswith(endpoint):
            return normalized[: -len(endpoint)].rstrip("/")
    return normalized


def config_from_env() -> EpayConfig | None:
    return config_from_mapping(os.environ)


def config_from_mapping(env: Mapping[str, str]) -> EpayConfig | None:
    enabled = env.get("EPAY_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise PaymentError("EPAY_ENABLED must be true or false")
    if enabled == "false":
        return None
    return settlement_config_from_mapping(env)


def settlement_config_from_mapping(env: Mapping[str, str]) -> EpayConfig | None:
    raw_base = env.get("EPAY_API_BASE", env.get("EPAY_BASE_URL", "")).strip()
    raw_pid = env.get("EPAY_PID", env.get("EPAY_MERCHANT_ID", "")).strip()
    raw_key = env.get("EPAY_PKEY", env.get("EPAY_MERCHANT_KEY", ""))
    if not raw_base and not raw_pid and not raw_key:
        return None
    try:
        ttl = int(env.get("EPAY_ORDER_TTL_SECONDS", "300"))
        hold = int(env.get("EPAY_AMOUNT_HOLD_SECONDS", "3600"))
    except ValueError as error:
        raise PaymentError("EasyPay time settings must be integers") from error
    return EpayConfig(
        base_url=raw_base,
        merchant_id=raw_pid,
        merchant_key=decode_smtp_password(raw_key),
        payment_type=env.get("EPAY_PAYMENT_TYPE", "alipay").strip(),
        site_url=env.get("NEWS_SITE_URL", "").strip().rstrip("/"),
        order_ttl_seconds=ttl,
        amount_hold_seconds=hold,
    )


def sign_fields(fields: dict[str, str], merchant_key: str) -> str:
    pairs = [
        f"{key}={value}"
        for key, value in sorted(fields.items())
        if key not in {"sign", "sign_type"} and value not in {None, ""}
    ]
    payload = "&".join(pairs) + merchant_key
    return hashlib.md5(payload.encode("utf-8")).hexdigest()  # noqa: S324 - protocol


def signature_valid(fields: dict[str, str], merchant_key: str) -> bool:
    supplied = fields.get("sign", "")
    return len(supplied) == 32 and hmac.compare_digest(
        sign_fields(fields, merchant_key).lower(), supplied.lower()
    )


def config_identity(config: EpayConfig) -> str:
    """Return an irreversible identity for fields required to settle an order."""
    canonical = "\0".join(
        (
            config.base_url,
            config.merchant_id,
            config.merchant_key,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()



def payment_origin(config: EpayConfig) -> str:
    """Return the validated gateway origin for a CSP source expression."""
    scheme, hostname, port = _url_origin(config.base_url, "EPAY_API_BASE")
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    port_suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{host}{port_suffix}"


def money_to_cents(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)(?:\.([0-9]{1,2}))?", value.strip())
    if match is None:
        raise PaymentError("payment amount is invalid")
    cents = int(match.group(1)) * 100 + int((match.group(2) or "").ljust(2, "0") or "0")
    if cents <= 0:
        raise PaymentError("payment amount must be positive")
    return cents


def cents_to_money(amount_cents: int) -> str:
    if type(amount_cents) is not int or amount_cents <= 0:
        raise PaymentError("payment amount must be a positive integer")
    return f"{amount_cents // 100}.{amount_cents % 100:02d}"


def merchant_order_number() -> str:
    return "news_" + secrets.token_hex(12)


def _payment_fields(
    config: EpayConfig,
    *,
    merchant_order_no: str,
    amount_cents: int,
    subject: str,
) -> dict[str, str]:
    if not re.fullmatch(r"news_[A-Za-z0-9_-]{1,74}", merchant_order_no):
        raise PaymentError("merchant order number must use the news_ namespace")
    fields = {
        "pid": config.merchant_id,
        "type": config.payment_type,
        "out_trade_no": merchant_order_no,
        "notify_url": config.site_url + NEWS_NOTIFY_PATH,
        "return_url": config.site_url + "/payment/return",
        "name": subject[:127],
        "money": cents_to_money(amount_cents),
    }
    fields["sign"] = sign_fields(fields, config.merchant_key)
    fields["sign_type"] = "MD5"
    return fields


def _payment_url(base_url: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaymentError("payment gateway omitted the payment URL")
    if _contains_unsafe_url_character(value):
        raise PaymentError("payment URL contains unsupported URL characters")
    candidate = value.strip()
    if candidate.startswith("/"):
        candidate = urljoin(base_url.rstrip("/") + "/", candidate)
    _validate_payment_url(candidate, base_url)
    return candidate


def create_payment(
    config: EpayConfig,
    *,
    merchant_order_no: str,
    amount_cents: int,
    subject: str,
) -> PaymentCreation:
    fields = _payment_fields(
        config,
        merchant_order_no=merchant_order_no,
        amount_cents=amount_cents,
        subject=subject,
    )
    request = Request(
        config.base_url + "/mapi.php",
        data=urlencode(fields).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "news-digest/1",
        },
        method="POST",
    )
    try:
        with _open_gateway(
            request,
            allowed_base_url=config.base_url,
            timeout=_GATEWAY_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(_MAX_GATEWAY_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise PaymentError(f"payment gateway returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise PaymentError("payment gateway request failed") from None
    if len(raw) > _MAX_GATEWAY_RESPONSE_BYTES:
        raise PaymentError("payment gateway response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PaymentError("payment gateway returned invalid JSON") from None
    if not isinstance(payload, dict) or str(payload.get("code")) != "1":
        error_code = payload.get("error_code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else None
        if error_code == "AMOUNT_OCCUPIED" or message == "AMOUNT_OCCUPIED":
            raise PaymentError(
                "payment amount is occupied", code="AMOUNT_OCCUPIED"
            )
        raise PaymentError(
            "payment gateway rejected order creation", code="GATEWAY_REJECTED"
        )
    trade_no = payload.get("trade_no")
    if not isinstance(trade_no, str) or not 1 <= len(trade_no.strip()) <= 128:
        raise PaymentError("payment gateway omitted the trade number")
    pay_url = payload.get("payurl") or payload.get("payurl2")
    return PaymentCreation(
        provider_trade_no=trade_no.strip(),
        payment_url=_payment_url(config.base_url, pay_url),
    )


def _query_fields(payload: object) -> dict[str, str]:
    required = {
        "code",
        "msg",
        "pid",
        "out_trade_no",
        "trade_no",
        "money",
        "status",
        "trade_status",
        "sign_type",
        "sign",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise PaymentError("payment query response fields are invalid")
    fields: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise PaymentError("payment query response fields are invalid")
        fields[key] = str(value)
    return fields


def query_payment(
    config: EpayConfig,
    *,
    merchant_order_no: str,
    expected_amount_cents: int | None = None,
) -> PaymentQuery:
    if not re.fullmatch(r"news_[A-Za-z0-9_-]{1,74}", merchant_order_no):
        raise PaymentError("merchant order number is invalid")
    request = Request(
        config.base_url + "/api.php",
        data=urlencode(
            {
                "act": "order",
                "pid": config.merchant_id,
                "key": config.merchant_key,
                "out_trade_no": merchant_order_no,
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "news-digest/1",
        },
        method="POST",
    )
    try:
        with _open_gateway(
            request,
            allowed_base_url=config.base_url,
            timeout=_GATEWAY_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(_MAX_GATEWAY_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise PaymentError(f"payment gateway returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise PaymentError("payment gateway request failed") from None
    if len(raw) > _MAX_GATEWAY_RESPONSE_BYTES:
        raise PaymentError("payment gateway response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PaymentError("payment gateway returned invalid JSON") from None
    fields = _query_fields(payload)
    if fields["code"] != "1" or fields["msg"] != "success":
        raise PaymentError("payment gateway rejected order query")
    if fields["sign_type"].upper() != "MD5" or not signature_valid(
        fields, config.merchant_key
    ):
        raise PaymentError("payment query signature is invalid")
    if fields["pid"] != config.merchant_id:
        raise PaymentError("payment query merchant does not match")
    if fields["out_trade_no"] != merchant_order_no:
        raise PaymentError("payment query order does not match")
    trade_status = fields["trade_status"]
    if trade_status not in {"TRADE_SUCCESS", "WAIT_BUYER_PAY", "TRADE_CLOSED"}:
        raise PaymentError("payment query status is invalid")
    expected_status = "1" if trade_status == "TRADE_SUCCESS" else "0"
    if fields["status"] != expected_status:
        raise PaymentError("payment query status fields disagree")
    provider_trade_no = fields["trade_no"].strip()
    if not provider_trade_no or len(provider_trade_no) > 128:
        raise PaymentError("payment query trade number is invalid")
    amount_cents = money_to_cents(fields["money"])
    if expected_amount_cents is not None and amount_cents != expected_amount_cents:
        raise PaymentError("payment query amount does not match")
    return PaymentQuery(
        merchant_order_no=merchant_order_no,
        provider_trade_no=provider_trade_no,
        amount_cents=amount_cents,
        trade_status=trade_status,
    )


def parse_notification(
    config: EpayConfig, fields: dict[str, str]
) -> PaymentNotification:
    if fields.get("sign_type", "").upper() != "MD5":
        raise PaymentError("unsupported payment signature type")
    if not signature_valid(fields, config.merchant_key):
        raise PaymentError("payment signature is invalid")
    if fields.get("pid") != config.merchant_id:
        raise PaymentError("payment merchant does not match")
    if fields.get("type") != config.payment_type:
        raise PaymentError("payment type does not match")
    if fields.get("trade_status") != "TRADE_SUCCESS":
        raise PaymentError("payment is not complete")
    merchant_order_no = fields.get("out_trade_no", "").strip()
    provider_trade_no = fields.get("trade_no", "").strip()
    if not re.fullmatch(r"news_[A-Za-z0-9_-]{1,74}", merchant_order_no):
        raise PaymentError("merchant order number is invalid")
    if not provider_trade_no or len(provider_trade_no) > 128:
        raise PaymentError("provider trade number is invalid")
    return PaymentNotification(
        merchant_order_no=merchant_order_no,
        provider_trade_no=provider_trade_no,
        amount_cents=money_to_cents(fields.get("money", "")),
    )
