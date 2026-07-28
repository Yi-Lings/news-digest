"""Admin-managed SMTP/content settings persistence and public-target validation."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from news_digest.config import (
    SmtpConfig,
    encode_smtp_password,
    parse_dotenv_value,
    smtp_config_from_env,
)
from news_digest.config_io import update_text
from news_digest.delivery.delivery_service import (
    DeliveryServiceError,
    catchup_window_hours,
    email_content_config_from_env,
)
from news_digest.delivery.email_content import EmailContentConfig

Resolver = Callable[[str, int], Iterable[str]]

MANAGED_KEYS = (
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
    "EMAIL_LANGUAGE",
    "EMAIL_SOURCE_FILTERS",
    "EMAIL_LAYOUT",
    "EMAIL_SUMMARY_LENGTH",
    "EMAIL_CATCHUP_WINDOW_HOURS",
)

_SMTP_FIELDS = {
    "delivery_enabled",
    "host",
    "port",
    "username",
    "password",
    "security",
    "sender",
}
_CONTENT_FIELDS = {
    "mains_enabled",
    "briefs_enabled",
    "main_limit",
    "brief_limit",
    "language",
    "source_filters",
    "layout",
    "summary_length",
    "catchup_window_hours",
}
_HOST_LABEL_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
)


class AdminEmailError(ValueError):
    """Safe validation error suitable for an Admin JSON response."""

    def __init__(self, message: str, *, category: str = "configuration") -> None:
        self.category = category
        super().__init__(message)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        parsed = parse_dotenv_value(value)
        values[key] = parsed
    return values


def _bool_value(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise AdminEmailError(f"{field} 必须是布尔值")
    return value


def _int_value(value: Any, field: str) -> int:
    if type(value) is not int:
        raise AdminEmailError(f"{field} 必须是整数")
    return value


def _string_value(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise AdminEmailError(f"{field} 必须是字符串")
    if "\r" in value or "\n" in value:
        raise AdminEmailError(f"{field} 包含非法换行")
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AdminEmailError(f"{field} 必须是字符串列表")
    return tuple(value)


def _smtp_form_env(
    body: Mapping[str, Any],
    saved_env: Mapping[str, str],
    *,
    saved_recipients: bool,
) -> dict[str, str]:
    fields = set(body)
    expected = _SMTP_FIELDS | _CONTENT_FIELDS
    if fields not in (expected, expected | {"recipients"}):
        raise AdminEmailError("邮件设置字段无效")
    new_password = _string_value(body["password"], "password")
    password = encode_smtp_password(new_password)
    if not new_password:
        password = saved_env.get("SMTP_PASSWORD", "")
    return {
        "EMAIL_DELIVERY_ENABLED": str(
            _bool_value(body["delivery_enabled"], "delivery_enabled")
        ).lower(),
        "SMTP_HOST": _string_value(body["host"], "host").strip(),
        "SMTP_PORT": str(_int_value(body["port"], "port")),
        "SMTP_USERNAME": _string_value(body["username"], "username").strip(),
        "SMTP_PASSWORD": password,
        "SMTP_SECURITY": _string_value(body["security"], "security").strip(),
        "SMTP_FROM": _string_value(body["sender"], "sender").strip(),
        # Kept only for the one-time database compatibility import. Admin forms
        # no longer edit this legacy recipient list.
        "SMTP_RECIPIENTS": saved_env.get("SMTP_RECIPIENTS", ""),
    }


def _form_env(
    body: Mapping[str, Any],
    saved_env: Mapping[str, str],
    *,
    saved_recipients: bool,
) -> dict[str, str]:
    values = _smtp_form_env(body, saved_env, saved_recipients=saved_recipients)
    sources = ",".join(_string_list(body["source_filters"], "source_filters"))
    values.update(
        {
        "EMAIL_MAINS_ENABLED": str(_bool_value(body["mains_enabled"], "mains_enabled")).lower(),
        "EMAIL_BRIEFS_ENABLED": str(_bool_value(body["briefs_enabled"], "briefs_enabled")).lower(),
        "EMAIL_MAIN_LIMIT": str(_int_value(body["main_limit"], "main_limit")),
        "EMAIL_BRIEF_LIMIT": str(_int_value(body["brief_limit"], "brief_limit")),
        "EMAIL_LANGUAGE": _string_value(body["language"], "language").strip(),
        "EMAIL_SOURCE_FILTERS": sources,
        "EMAIL_LAYOUT": _string_value(body["layout"], "layout").strip(),
        "EMAIL_SUMMARY_LENGTH": _string_value(body["summary_length"], "summary_length").strip(),
        "EMAIL_CATCHUP_WINDOW_HOURS": str(
            _int_value(body["catchup_window_hours"], "catchup_window_hours")
        ),
        }
    )
    return values


def smtp_config_from_form(
    body: Mapping[str, Any],
    saved_env: Mapping[str, str],
    *,
    saved_recipients: bool = False,
) -> SmtpConfig:
    values = _smtp_form_env(body, saved_env, saved_recipients=saved_recipients)
    try:
        return smtp_config_from_env(values)
    except ValueError as error:
        raise AdminEmailError(str(error)) from None


def configs_from_form(
    body: Mapping[str, Any],
    saved_env: Mapping[str, str],
    *,
    published_main_count: int | None,
    published_brief_count: int | None,
    saved_recipients: bool = False,
) -> tuple[SmtpConfig, EmailContentConfig, dict[str, str]]:
    values = _form_env(body, saved_env, saved_recipients=saved_recipients)
    try:
        smtp = smtp_config_from_env(values)
        content_kwargs = {}
        if published_main_count is not None and published_brief_count is not None:
            content_kwargs = {
                "published_main_count": published_main_count,
                "published_brief_count": published_brief_count,
            }
        content = email_content_config_from_env(values, **content_kwargs)
        if (
            published_main_count is not None
            and content.mains_enabled
            and content.main_limit > published_main_count
        ):
            raise ValueError("main_limit exceeds the published edition")
        if (
            published_brief_count is not None
            and content.briefs_enabled
            and content.brief_limit > published_brief_count
        ):
            raise ValueError("brief_limit exceeds the published edition")
        catchup_window_hours(values)
    except (ValueError, DeliveryServiceError) as error:
        raise AdminEmailError(str(error)) from None
    return smtp, content, values


def _default_resolver(host: str, port: int) -> Iterable[str]:
    return {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}


def _valid_domain(host: str) -> bool:
    if len(host) > 253 or "." not in host:
        return False
    labels = host.split(".")
    return all(
        label
        and len(label) <= 63
        and label[0] != "-"
        and label[-1] != "-"
        and set(label) <= _HOST_LABEL_CHARACTERS
        for label in labels
    )


def validate_smtp_target(
    host: str,
    port: int,
    resolver: Resolver | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Require an ASCII domain whose complete DNS result set is globally routable."""
    if not isinstance(host, str) or not isinstance(port, int) or type(port) is bool:
        raise AdminEmailError("SMTP 目标无效")
    normalized = host.strip().rstrip(".").casefold()
    if normalized != host.strip().casefold() or not 1 <= port <= 65535:
        raise AdminEmailError("SMTP host 必须是公网域名，端口必须为 1–65535")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise AdminEmailError("SMTP host 必须填写域名，不能填写 IP 地址") from None
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError:
        raise AdminEmailError("SMTP host 必须是 ASCII 域名") from None
    if not _valid_domain(normalized) or normalized.endswith(
        (".local", ".localhost", ".internal", ".lan", ".home", ".invalid", ".test")
    ):
        raise AdminEmailError("SMTP host 必须是可解析的公网域名")
    try:
        addresses = tuple(dict.fromkeys((resolver or _default_resolver)(normalized, port)))
    except (OSError, ValueError):
        raise AdminEmailError("SMTP 服务器 DNS 解析失败", category="dns") from None
    if not addresses:
        raise AdminEmailError("SMTP 服务器 DNS 解析失败", category="dns")
    try:
        parsed = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError:
        raise AdminEmailError("SMTP DNS 返回了非法地址", category="dns") from None
    if any(
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        for address in parsed
    ):
        raise AdminEmailError("SMTP 的全部 DNS 结果都必须是公网地址")
    return normalized, tuple(str(address) for address in parsed)


def validate_smtp_config_target(
    config: SmtpConfig,
    resolver: Resolver | None = None,
) -> tuple[str, tuple[str, ...]]:
    return validate_smtp_target(config.host, config.port, resolver)


def _replace_managed_env(current: str, values: Mapping[str, str]) -> str:
    managed = set(MANAGED_KEYS) | {"SMTP_USE_TLS"}
    lines = current.splitlines()
    output: list[str] = []
    inserted = False
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in managed:
            if not inserted:
                output.extend(_managed_env_lines(values))
                inserted = True
            continue
        output.append(line)
    if not inserted:
        if output and output[-1]:
            output.append("")
        output.extend(_managed_env_lines(values))
    return "\n".join(output).rstrip("\n") + "\n"


def _managed_env_lines(values: Mapping[str, str]) -> list[str]:
    lines: list[str] = []
    for name in MANAGED_KEYS:
        lines.append(f"{name}={values[name]}")
    return lines


def save_settings(
    env_path: Path,
    body: Mapping[str, Any],
    *,
    published_main_count: int | None,
    published_brief_count: int | None,
    resolver: Resolver | None = None,
) -> tuple[SmtpConfig, EmailContentConfig]:
    result: dict[str, Any] = {}

    def transform(current: str) -> str:
        saved = read_env_text(current)
        smtp, content, values = configs_from_form(
            body,
            saved,
            published_main_count=published_main_count,
            published_brief_count=published_brief_count,
        )
        # Pausing delivery must remain possible on a fresh/partially configured install.
        # Re-enabling is a hard gate: the exact same validation used by tests/delivery must pass.
        if smtp.delivery_enabled:
            from news_digest.delivery.mailer import MailError, validate_smtp

            try:
                validate_smtp(
                    smtp,
                    require_recipients=False,
                    resolver=resolver,
                    validate_target=True,
                )
            except MailError as error:
                raise AdminEmailError(str(error), category=error.category) from None
        elif smtp.host:
            # A disabled but populated target is still validated; only wholly absent SMTP
            # settings may be saved without DNS/network target validation.
            validate_smtp_config_target(smtp, resolver)
        result.update(smtp=smtp, content=content)
        return _replace_managed_env(current, values)

    update_text(env_path, transform)
    return result["smtp"], result["content"]


def clear_password(env_path: Path, *, confirm: bool) -> None:
    if confirm is not True:
        raise AdminEmailError("清除 SMTP 密码需要 confirm=true")

    def transform(current: str) -> str:
        values = read_env_text(current)
        values.setdefault("SMTP_PASSWORD", "")
        values["SMTP_PASSWORD"] = ""
        lines = current.splitlines()
        output: list[str] = []
        replaced = False
        for line in lines:
            key = line.strip().partition("=")[0].strip() if "=" in line else ""
            if key == "SMTP_PASSWORD":
                if not replaced:
                    output.append("SMTP_PASSWORD=")
                    replaced = True
                continue
            output.append(line)
        if not replaced:
            output.append("SMTP_PASSWORD=")
        return "\n".join(output).rstrip("\n") + "\n"

    update_text(env_path, transform)


def read_env_text(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        parsed = parse_dotenv_value(value)
        values[key] = parsed
    return values


def settings_payload(
    environ: Mapping[str, str],
    *,
    published_main_count: int | None,
    published_brief_count: int | None,
) -> dict[str, Any]:
    try:
        smtp = smtp_config_from_env(environ)
        content_kwargs = {}
        if published_main_count is not None and published_brief_count is not None:
            content_kwargs = {
                "published_main_count": published_main_count,
                "published_brief_count": published_brief_count,
            }
        content = email_content_config_from_env(environ, **content_kwargs)
        window = catchup_window_hours(environ)
    except (ValueError, DeliveryServiceError) as error:
        raise AdminEmailError(str(error)) from None
    return {
        "delivery_enabled": smtp.delivery_enabled,
        "host": smtp.host,
        "port": smtp.port,
        "username": smtp.username,
        "password_set": bool(smtp.password),
        "security": smtp.security,
        "sender": smtp.sender,
        "mains_enabled": content.mains_enabled,
        "briefs_enabled": content.briefs_enabled,
        "main_limit": content.main_limit,
        "brief_limit": content.brief_limit,
        "language": content.language,
        "source_filters": list(content.source_filters),
        "layout": content.layout,
        "summary_length": content.summary_length,
        "catchup_window_hours": window,
    }
