"""Admin model-provider persistence, migration, validation, and test state."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import queue
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from news_digest.config import (
    TranslationConfig,
    normalize_translation_base_url,
    translation_config_from_env,
)
from news_digest.config_io import (
    atomic_write_bytes_unlocked,
    locked_path,
    update_text,
)

PROFILES_FILE = ".env.providers.local"
ENV_FILE = ".env.local"
TESTS_FILE = "provider-tests.json"
API_TYPES = {"openai_chat", "anthropic_messages"}
_DNS_TIMEOUT_SECONDS = 10.0
_PRODUCTION_PROFILES_FILE = "providers.json"
_PRODUCTION_PROFILES_MODE = 0o640
_ENV_KEYS = (
    "TRANSLATION_API_BASE_URL",
    "TRANSLATION_API_KEY",
    "TRANSLATION_MODEL",
    "TRANSLATION_API_TYPE",
    "TRANSLATION_STREAM",
    "TRANSLATION_REASONING_EFFORT",
)


class AdminConfigError(ValueError):
    """Safe validation or lifecycle error intended for an Admin response."""


def empty_profiles() -> dict[str, Any]:
    return {"providers": {}}


def _normalize_provider(name: str, raw: dict[str, Any], *, legacy_default: bool) -> dict[str, Any]:
    api_type = str(raw.get("api_type", "openai_chat")).strip()
    if api_type not in API_TYPES:
        raise AdminConfigError(f"档案 {name} 的 api_type 无效")
    stream = raw.get("stream", True)
    enabled = raw.get("enabled", True)
    is_default = raw.get("is_default", legacy_default)
    if not all(isinstance(value, bool) for value in (stream, enabled, is_default)):
        raise AdminConfigError(f"档案 {name} 的布尔字段无效")
    reasoning_effort = str(raw.get("reasoning_effort", "")).strip().lower()
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
        raise AdminConfigError(f"档案 {name} 的推理强度无效")
    provider = {
        "name": name,
        "base_url": str(raw.get("base_url", "")).strip().rstrip("/"),
        "api_key": str(raw.get("api_key", "")),
        "model": str(raw.get("model", "")).strip(),
        "api_type": api_type,
        "stream": stream,
        "reasoning_effort": reasoning_effort,
        "enabled": enabled,
        "is_default": is_default,
    }
    if provider["is_default"] and not provider["enabled"]:
        raise AdminConfigError("默认档案必须处于启用状态")
    return provider


def migrate_profiles(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AdminConfigError("供应商档案格式无效")
    raw_providers = raw.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise AdminConfigError("供应商档案格式无效")
    legacy_active = str(raw.get("active", "")).strip()
    providers: dict[str, dict[str, Any]] = {}
    defaults: list[str] = []
    for key, value in raw_providers.items():
        name = str(key).strip()
        if not name or not isinstance(value, dict):
            raise AdminConfigError("供应商名称或档案格式无效")
        provider = _normalize_provider(name, value, legacy_default=name == legacy_active)
        if provider["is_default"]:
            defaults.append(name)
        providers[name] = provider
    if len(defaults) > 1:
        raise AdminConfigError("只能有一个默认档案")
    return {"providers": providers}


def load_profiles(root: Path, filename: str = PROFILES_FILE) -> dict[str, Any]:
    path = Path(root) / filename
    if not path.is_file():
        return empty_profiles()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdminConfigError("供应商档案无法读取") from error
    return migrate_profiles(raw)


def save_profiles(root: Path, data: dict[str, Any], filename: str = PROFILES_FILE) -> None:
    normalized = migrate_profiles(data)
    content = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = Path(root) / filename
    with locked_path(path):
        atomic_write_bytes_unlocked(path, content, mode=_profiles_mode(filename))


def update_profiles(
    root: Path,
    transform: Callable[[dict[str, Any]], None],
    filename: str = PROFILES_FILE,
) -> dict[str, Any]:
    path = Path(root) / filename
    with locked_path(path):
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise AdminConfigError("供应商档案无法读取") from error
            data = migrate_profiles(raw)
        else:
            data = empty_profiles()
        transform(data)
        normalized = migrate_profiles(data)
        content = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write_bytes_unlocked(path, content, mode=_profiles_mode(filename))
        return normalized


def _profiles_mode(filename: str) -> int:
    return _PRODUCTION_PROFILES_MODE if filename == _PRODUCTION_PROFILES_FILE else 0o600


def default_provider(data: dict[str, Any]) -> dict[str, Any]:
    defaults = [p for p in data["providers"].values() if p["is_default"]]
    if not defaults:
        raise AdminConfigError("未设置默认翻译档案；请先启用并设为默认")
    provider = defaults[0]
    if not provider["enabled"]:
        raise AdminConfigError("默认翻译档案已停用；请设置可用默认档案")
    return provider


def provider_from_request(
    body: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    allowed = {
        "name",
        "base_url",
        "api_key",
        "model",
        "api_type",
        "stream",
        "reasoning_effort",
        "enabled",
    }
    unknown = set(body) - allowed
    if unknown:
        raise AdminConfigError(f"不支持的字段：{', '.join(sorted(unknown))}")
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 80 or any(ord(character) < 32 for character in name):
        raise AdminConfigError("名称不能为空且不能含控制字符")
    existing = existing or {}
    api_key = str(body.get("api_key", "")).strip() or str(existing.get("api_key", ""))
    raw = {
        "base_url": str(body.get("base_url", "")).strip(),
        "api_key": api_key,
        "model": str(body.get("model", "")).strip(),
        "api_type": body.get("api_type", existing.get("api_type", "openai_chat")),
        "stream": body.get("stream", existing.get("stream", True)),
        "reasoning_effort": body.get(
            "reasoning_effort", existing.get("reasoning_effort", "")
        ),
        "enabled": body.get("enabled", existing.get("enabled", True)),
        "is_default": existing.get("is_default", False),
    }
    provider = _normalize_provider(name, raw, legacy_default=False)
    if not provider["base_url"] or not provider["model"] or not provider["api_key"]:
        raise AdminConfigError("base_url、model、api_key 均不能为空（编辑时 key 可留空沿用）")
    try:
        provider["base_url"] = normalize_translation_base_url(provider["base_url"])
    except ValueError as error:
        raise AdminConfigError(str(error)) from error
    return provider


def translation_config(
    provider: dict[str, Any],
    *,
    timeout_seconds: float = 15.0,
) -> TranslationConfig:
    return TranslationConfig(
        base_url=provider["base_url"],
        api_key=provider["api_key"],
        model=provider["model"],
        timeout_seconds=timeout_seconds,
        max_tokens=8,
        cache_dir=Path("var/data/translations"),
        api_type=provider["api_type"],
        stream=provider["stream"],
        reasoning_effort=provider.get("reasoning_effort", ""),
    )


def write_env_local(root: Path, provider: dict[str, Any], filename: str = ENV_FILE) -> None:
    values = {
        "TRANSLATION_API_BASE_URL": provider["base_url"],
        "TRANSLATION_API_KEY": provider["api_key"],
        "TRANSLATION_MODEL": provider["model"],
        "TRANSLATION_API_TYPE": provider["api_type"],
        "TRANSLATION_STREAM": str(provider["stream"]).lower(),
        "TRANSLATION_REASONING_EFFORT": provider.get("reasoning_effort", ""),
    }
    path = Path(root) / filename

    def transform(current: str) -> str:
        lines = current.splitlines()
        output: list[str] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            key = stripped.partition("=")[0].strip() if "=" in stripped else ""
            if not stripped.startswith("#") and key in values:
                output.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                output.append(line)
        output.extend(f"{key}={values[key]}" for key in _ENV_KEYS if key not in seen)
        return "\n".join(output) + "\n"

    update_text(path, transform)


def mask_provider(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": provider["name"],
        "base_url": provider["base_url"],
        "key_set": bool(provider["api_key"]),
        "model": provider["model"],
        "api_type": provider["api_type"],
        "stream": provider["stream"],
        "reasoning_effort": provider.get("reasoning_effort", ""),
        "enabled": provider["enabled"],
        "is_default": provider["is_default"],
    }


def _fingerprint_secret(root: Path) -> bytes:
    path = Path(root) / ".provider-test-secret"
    with locked_path(path):
        if path.is_file():
            return path.read_bytes()
        secret = secrets.token_hex(32).encode("ascii")
        atomic_write_bytes_unlocked(path, secret)
        return secret


def provider_fingerprint(root: Path, provider: dict[str, Any]) -> str:
    public = {
        key: provider.get(key, "")
        for key in (
            "name",
            "base_url",
            "model",
            "api_type",
            "stream",
            "reasoning_effort",
            "enabled",
        )
    }
    public_digest = hashlib.sha256(
        json.dumps(public, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    secret = _fingerprint_secret(root)
    key_digest = hmac.new(secret, provider["api_key"].encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(f"{public_digest}:{key_digest}".encode("ascii")).hexdigest()


def load_test_states(root: Path, filename: str = TESTS_FILE) -> dict[str, Any]:
    path = Path(root) / filename
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_test_state(
    root: Path,
    name: str,
    result: dict[str, Any],
    filename: str = TESTS_FILE,
) -> None:
    path = Path(root) / filename
    with locked_path(path):
        states = load_test_states(root, filename)
        states[name] = result
        content = (json.dumps(states, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write_bytes_unlocked(path, content)


def remove_test_state(root: Path, name: str, filename: str = TESTS_FILE) -> None:
    path = Path(root) / filename
    if not path.exists():
        return
    with locked_path(path):
        states = load_test_states(root, filename)
        if name not in states:
            return
        del states[name]
        content = (json.dumps(states, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write_bytes_unlocked(path, content)


def current_test_state(root: Path, provider: dict[str, Any]) -> dict[str, Any] | None:
    result = load_test_states(root).get(provider["name"])
    if not isinstance(result, dict):
        return None
    public = {key: value for key, value in result.items() if key != "fingerprint"}
    public["stale"] = not hmac.compare_digest(
        str(result.get("fingerprint", "")), provider_fingerprint(root, provider)
    )
    return public


def assert_recent_success(root: Path, provider: dict[str, Any], *, max_age_seconds: int) -> bool:
    state = load_test_states(root).get(provider["name"])
    if not isinstance(state, dict) or state.get("status") != "success":
        return False
    try:
        tested_at_epoch = float(state["tested_at_epoch"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        hmac.compare_digest(str(state.get("fingerprint", "")), provider_fingerprint(root, provider))
        and tested_at_epoch >= time.time() - max_age_seconds
    )


def _default_resolver(hostname: str, port: int) -> Iterable[str]:
    return {
        sockaddr[0]
        for _, _, _, _, sockaddr in socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    }


def validate_public_https_target(
    base_url: str,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
    *,
    timeout_seconds: float = _DNS_TIMEOUT_SECONDS,
) -> str:
    try:
        normalized = normalize_translation_base_url(base_url)
        parts = urlsplit(normalized)
        port = parts.port or 443
    except ValueError as error:
        raise AdminConfigError(str(error)) from error
    if parts.scheme != "https" or not parts.hostname:
        raise AdminConfigError("生产 API 测试只允许 HTTPS 公网目标")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        raise AdminConfigError("生产 API 测试不允许使用 IP 地址")
    local_names = {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }
    if parts.hostname.casefold() in local_names:
        raise AdminConfigError("生产 API 测试目标不是公网主机")
    outcome: queue.SimpleQueue[tuple[bool, object]] = queue.SimpleQueue()

    def resolve() -> None:
        try:
            outcome.put((True, list((resolver or _default_resolver)(parts.hostname, port))))
        except BaseException as error:
            outcome.put((False, error))

    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    worker.join(max(0.0, timeout_seconds))
    if worker.is_alive():
        raise AdminConfigError("API 主机 DNS 解析超时")
    succeeded, value = outcome.get()
    if not succeeded:
        if isinstance(value, (OSError, socket.gaierror)):
            raise AdminConfigError("API 主机 DNS 解析失败") from value
        raise AdminConfigError("API 主机 DNS 解析失败")
    addresses = list(value)
    if not addresses:
        raise AdminConfigError("API 主机 DNS 无可用结果")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise AdminConfigError("API 主机 DNS 返回非法地址") from error
        if (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address.is_loopback
            or address.is_link_local
            or address.is_private
        ):
            raise AdminConfigError("API 主机的全部 DNS 结果都必须是公网地址")
    return normalized


def runtime_translation_config(
    profiles_path: Path,
    environ: Mapping[str, str],
    *,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> TranslationConfig:
    """Load the unique default profile as the production translation authority."""
    path = Path(profiles_path)
    provider = default_provider(load_profiles(path.parent, path.name))
    base_url = normalize_translation_base_url(provider["base_url"])
    merged = dict(environ)
    merged.update(
        {
            "TRANSLATION_API_BASE_URL": base_url,
            "TRANSLATION_API_KEY": provider["api_key"],
            "TRANSLATION_MODEL": provider["model"],
            "TRANSLATION_API_TYPE": provider["api_type"],
            "TRANSLATION_STREAM": str(provider["stream"]).lower(),
            "TRANSLATION_REASONING_EFFORT": provider.get("reasoning_effort", ""),
        }
    )
    try:
        return translation_config_from_env(merged)
    except ValueError as error:
        raise AdminConfigError(str(error)) from error
