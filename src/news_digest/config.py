"""Configuration loading and validation."""

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

SITE_NAME = "Cheapcoding News"
SITE_TAGLINE = "每日双语新闻"
MAX_TRANSLATION_TIMEOUT_SECONDS = 3600
_SMTP_PASSWORD_ENCODING_PREFIX = "nd-b64-v1:"


def encode_smtp_password(value: str) -> str:
    """Encode a password as an interpolation-safe dotenv token."""
    if not value:
        return ""
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return _SMTP_PASSWORD_ENCODING_PREFIX + encoded


def decode_smtp_password(value: str) -> str:
    """Decode Admin-managed SMTP password tokens while accepting legacy plaintext."""
    if not value.startswith(_SMTP_PASSWORD_ENCODING_PREFIX):
        return value
    encoded = value.removeprefix(_SMTP_PASSWORD_ENCODING_PREFIX)
    if not encoded:
        return value
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if base64.b64encode(decoded).decode("ascii") != encoded:
            return value
        return decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return value


@dataclass(frozen=True)
class BuildConfig:
    """Settings needed by the static site build; no secrets involved."""

    output_root: Path
    site_url: str
    public_subscription_enabled: bool = False


def build_config_from_env(environ: Mapping[str, str] | None = None) -> BuildConfig:
    """Read build settings from the given mapping (defaults to os.environ).

    Missing values fall back to local-development defaults so that commands
    which do not need them are never blocked.
    """
    env = os.environ if environ is None else environ
    return BuildConfig(
        output_root=Path(env.get("NEWS_OUTPUT_PATH", "var/site")),
        site_url=env.get("NEWS_SITE_URL", "http://127.0.0.1:8618").rstrip("/"),
        public_subscription_enabled=_boolean_env(
            env, "PUBLIC_SUBSCRIPTION_ENABLED", False
        ),
    )


def public_subscription_enabled_from_env(
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    return _boolean_env(env, "PUBLIC_SUBSCRIPTION_ENABLED", False)


def parse_dotenv_value(raw_value: str) -> str:
    """Parse the project's minimal dotenv value syntax."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        if isinstance(parsed, str):
            return parsed
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def load_env_file(path: Path | None = None) -> None:
    """Merge KEY=VALUE lines from .env.local into os.environ; existing vars win.

    仅由 CLI 命令入口调用；测试不得触达（计划 §6 约束）。
    """
    path = path or Path(".env.local")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = parse_dotenv_value(value)
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class TranslationConfig:
    """翻译接口配置；缺省为空，真实调用前由客户端校验并提示。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    max_tokens: int
    cache_dir: Path
    api_type: Literal["openai_chat", "anthropic_messages"] = "openai_chat"
    stream: bool = True
    # 空字符串表示自动：不向接口发送 reasoning_effort。
    reasoning_effort: Literal[
        "", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = ""


_TRANSLATION_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_TRANSLATION_ENDPOINT_SEGMENTS = {"messages", "responses"}
_DEFAULT_TRANSLATION_TIMEOUT_SECONDS = 600.0


def normalize_translation_base_url(value: str) -> str:
    """Validate a base-only provider URL and append one trailing ``/v1``."""
    raw = value.strip()
    if not raw:
        return ""
    if "\\" in raw or any(character.isspace() for character in raw):
        raise ValueError("TRANSLATION_API_BASE_URL must be a safe HTTPS base URL")

    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError("TRANSLATION_API_BASE_URL is invalid") from error
    if hostname is not None:
        try:
            hostname.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("TRANSLATION_API_BASE_URL hostname must use ASCII") from error
    if (
        parts.scheme.lower() != "https"
        or not parts.netloc
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "TRANSLATION_API_BASE_URL must be an HTTPS base URL without "
            "credentials, query, or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("TRANSLATION_API_BASE_URL port must be from 1 to 65535")
    if ":" in hostname and not parts.netloc.startswith("["):
        raise ValueError("TRANSLATION_API_BASE_URL IPv6 host must use brackets")

    path = parts.path
    if "%" in path or "//" in path:
        raise ValueError("TRANSLATION_API_BASE_URL contains an unsafe path prefix")
    path = path.rstrip("/")
    segments = path.removeprefix("/").split("/") if path else []
    if any(
        not _TRANSLATION_PATH_SEGMENT.fullmatch(segment) or segment in {".", ".."}
        for segment in segments
    ):
        raise ValueError("TRANSLATION_API_BASE_URL contains an unsafe path prefix")

    lowered = [segment.lower() for segment in segments]
    contains_chat_endpoint = any(
        lowered[index : index + 2] == ["chat", "completions"] for index in range(len(lowered) - 1)
    )
    if contains_chat_endpoint or any(
        segment in _TRANSLATION_ENDPOINT_SEGMENTS for segment in lowered
    ):
        raise ValueError("TRANSLATION_API_BASE_URL must not include a complete operation endpoint")

    v1_positions = [index for index, segment in enumerate(lowered) if segment == "v1"]
    if len(v1_positions) > 1 or (v1_positions and v1_positions[0] != len(segments) - 1):
        raise ValueError("TRANSLATION_API_BASE_URL must contain at most one trailing /v1")
    if not v1_positions:
        segments.append("v1")

    normalized_path = "/" + "/".join(segments)
    return urlunsplit(("https", parts.netloc, normalized_path, "", ""))


def translation_config_from_env(environ: Mapping[str, str] | None = None) -> TranslationConfig:
    env = os.environ if environ is None else environ
    api_type = env.get("TRANSLATION_API_TYPE", "openai_chat").strip()
    if api_type not in {"openai_chat", "anthropic_messages"}:
        raise ValueError("TRANSLATION_API_TYPE must be openai_chat or anthropic_messages")
    reasoning_effort = env.get("TRANSLATION_REASONING_EFFORT", "").strip().lower()
    if reasoning_effort == "auto":
        reasoning_effort = ""
    if reasoning_effort not in {
        "",
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError(
            "TRANSLATION_REASONING_EFFORT must be empty, none, minimal, low, "
            "medium, high, xhigh, or max"
        )
    return TranslationConfig(
        base_url=normalize_translation_base_url(env.get("TRANSLATION_API_BASE_URL", "")),
        api_key=env.get("TRANSLATION_API_KEY", ""),
        model=env.get("TRANSLATION_MODEL", ""),
        # 长文 + reasoning_effort=max 的生成可能接近 8 分钟；默认 10 分钟
        # 保留有限余量，同时仍由显式硬总时限阻止请求无限挂起。
        timeout_seconds=float(
            env.get("TRANSLATION_TIMEOUT_SECONDS", str(int(_DEFAULT_TRANSLATION_TIMEOUT_SECONDS)))
        ),
        # 两类协议均要求长文译文有充足输出余量
        max_tokens=int(env.get("TRANSLATION_MAX_TOKENS", "8192")),
        cache_dir=Path(env.get("NEWS_DATA_DIR", "var/data")) / "translations",
        api_type=api_type,
        stream=_boolean_env(env, "TRANSLATION_STREAM", True),
        reasoning_effort=cast(
            Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            reasoning_effort,
        ),
    )


@dataclass(frozen=True, repr=False)
class SmtpConfig:
    """SMTP 投递配置；连接与发送只由显式调用触发。"""

    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]
    delivery_enabled: bool = False
    security: Literal["implicit_tls", "starttls"] = "implicit_tls"

    def __repr__(self) -> str:
        return "SmtpConfig(<redacted>)"


_EMAIL_PATTERN = re.compile(r"^[^\s@<>(),;:\\[\]]+@[^\s@<>(),;:\\[\]]+(?:\.[^\s@<>(),;:\\[\]]+)+$")


def _boolean_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return normalized == "true"


def email_delivery_enabled_from_env(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Read only the global delivery switch without parsing stale SMTP fields."""
    env = os.environ if environ is None else environ
    return _boolean_env(env, "EMAIL_DELIVERY_ENABLED", False)


def normalize_email_address(value: str, field: str) -> str:
    """Normalize and validate one bare mailbox address (display names are unsupported)."""
    address = value.strip()
    if "\r" in address or "\n" in address:
        raise ValueError(f"{field} contains CR/LF")
    if not address or len(address) > 254 or not _EMAIL_PATTERN.fullmatch(address):
        raise ValueError(f"{field} is not a valid email address")
    return address


def normalize_recipients(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate recipients and de-duplicate them case-insensitively, preserving order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        address = normalize_email_address(value, "SMTP_RECIPIENTS")
        key = address.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(address)
    return tuple(normalized)


def _smtp_security(env: Mapping[str, str]) -> Literal["implicit_tls", "starttls"]:
    raw_security = env.get("SMTP_SECURITY")
    legacy_tls = env.get("SMTP_USE_TLS")
    if raw_security is not None and raw_security.strip():
        security = raw_security.strip().lower()
        if security not in {"implicit_tls", "starttls"}:
            raise ValueError("SMTP_SECURITY must be implicit_tls or starttls")
        if legacy_tls is not None:
            legacy_enabled = _boolean_env(env, "SMTP_USE_TLS", True)
            migrated = "implicit_tls" if legacy_enabled else "starttls"
            if security != migrated:
                raise ValueError("SMTP_SECURITY conflicts with legacy SMTP_USE_TLS")
        return cast(Literal["implicit_tls", "starttls"], security)

    if legacy_tls is not None:
        # Deterministic migration: the former true selected implicit TLS; the former
        # false selected the non-implicit path, which is now always upgraded with STARTTLS.
        return "implicit_tls" if _boolean_env(env, "SMTP_USE_TLS", True) else "starttls"
    return "implicit_tls"


def smtp_config_from_env(environ: Mapping[str, str] | None = None) -> SmtpConfig:
    env = os.environ if environ is None else environ
    raw_port = env.get("SMTP_PORT", "465")
    try:
        port = int(raw_port or "465")
    except ValueError as error:
        raise ValueError("SMTP_PORT must be an integer from 1 to 65535") from error
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be from 1 to 65535")

    username = env.get("SMTP_USERNAME", "").strip()
    password = decode_smtp_password(env.get("SMTP_PASSWORD", ""))
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must both be set or both be empty")

    security = _smtp_security(env)
    raw_recipients = env.get("SMTP_RECIPIENTS", "")
    recipient_parts = raw_recipients.split(",") if raw_recipients else []
    if any(not value.strip() for value in recipient_parts):
        raise ValueError("SMTP_RECIPIENTS contains an empty item")
    recipients = normalize_recipients(recipient_parts)
    delivery_enabled = email_delivery_enabled_from_env(env)

    sender_raw = env.get("SMTP_FROM", "")
    sender = normalize_email_address(sender_raw, "SMTP_FROM") if sender_raw else ""
    return SmtpConfig(
        host=env.get("SMTP_HOST", "").strip(),
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        delivery_enabled=delivery_enabled,
        security=security,
    )


@dataclass(frozen=True)
class FetchConfig:
    """Settings for the real-news fetch stage."""

    proxy: str | None
    window_hours: int
    timezone: str
    data_dir: Path
    db_path: Path | None = None  # None 时取 data_dir/news.db

    @property
    def database(self) -> Path:
        return self.db_path if self.db_path is not None else self.data_dir / "news.db"


def fetch_config_from_env(environ: Mapping[str, str] | None = None) -> FetchConfig:
    env = os.environ if environ is None else environ
    data_dir = Path(env.get("NEWS_DATA_DIR", "var/data"))
    db_env = env.get("NEWS_DATABASE_PATH", "")
    return FetchConfig(
        proxy=env.get("NEWS_HTTP_PROXY") or None,
        window_hours=int(env.get("NEWS_FETCH_WINDOW_HOURS", "24")),
        timezone=env.get("NEWS_TIMEZONE") or "Asia/Shanghai",
        data_dir=data_dir,
        db_path=Path(db_env) if db_env else None,
    )
