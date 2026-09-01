"""Admin persistence for EasyPay-compatible settings."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from news_digest.admin_email import read_env, read_env_text
from news_digest.config import encode_smtp_password
from news_digest.config_io import update_text
from news_digest.payments import (
    NEWS_NOTIFY_PATH,
    PaymentError,
    config_from_mapping,
    config_identity,
    normalize_api_base,
    settlement_config_from_mapping,
)

MANAGED_KEYS = (
    "EPAY_ENABLED",
    "EPAY_API_BASE",
    "EPAY_PID",
    "EPAY_PKEY",
    "EPAY_PAYMENT_TYPE",
    "EPAY_ORDER_TTL_SECONDS",
    "EPAY_AMOUNT_HOLD_SECONDS",
)
_LEGACY_KEYS = {
    "EPAY_BASE_URL",
    "EPAY_MERCHANT_ID",
    "EPAY_MERCHANT_KEY",
}
_REQUEST_FIELDS = {
    "enabled",
    "api_base",
    "pid",
    "pkey",
    "payment_type",
    "order_ttl_seconds",
    "amount_hold_seconds",
}


class AdminPaymentError(ValueError):
    pass


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise AdminPaymentError(f"{field} 必须是单行字符串")
    return value.strip()


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        raise AdminPaymentError(f"{field} 必须是整数")
    return value


def _saved_value(env: Mapping[str, str], primary: str, legacy: str) -> str:
    return env.get(primary, env.get(legacy, ""))


def _payload_integer(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def settings_payload(env: Mapping[str, str], site_url: str) -> dict[str, Any]:
    api_base = _saved_value(env, "EPAY_API_BASE", "EPAY_BASE_URL")
    pid = _saved_value(env, "EPAY_PID", "EPAY_MERCHANT_ID")
    pkey = _saved_value(env, "EPAY_PKEY", "EPAY_MERCHANT_KEY")
    base = site_url.rstrip("/")
    return {
        "enabled": env.get("EPAY_ENABLED", "false").strip().lower() == "true",
        "api_base": api_base,
        "pid": pid,
        "pkey_set": bool(pkey),
        "payment_type": env.get("EPAY_PAYMENT_TYPE", "alipay"),
        "order_ttl_seconds": _payload_integer(
            env, "EPAY_ORDER_TTL_SECONDS", 300
        ),
        "amount_hold_seconds": _payload_integer(
            env, "EPAY_AMOUNT_HOLD_SECONDS", 3600
        ),
        "notify_url": base + NEWS_NOTIFY_PATH if base else "",
        "return_url": base + "/payment/return" if base else "",
    }


def _replace_settings(current: str, values: Mapping[str, str]) -> str:
    managed = set(MANAGED_KEYS) | _LEGACY_KEYS
    output: list[str] = []
    inserted = False
    for line in current.splitlines():
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in managed:
            if not inserted:
                output.extend(f"{name}={values[name]}" for name in MANAGED_KEYS)
                inserted = True
            continue
        output.append(line)
    if not inserted:
        if output and output[-1]:
            output.append("")
        output.extend(f"{name}={values[name]}" for name in MANAGED_KEYS)
    return "\n".join(output).rstrip("\n") + "\n"


def save_settings(
    env_path: Path,
    body: Mapping[str, Any],
    site_url: str,
    *,
    settlement_locked: bool = False,
) -> dict[str, Any]:
    if set(body) != _REQUEST_FIELDS:
        raise AdminPaymentError("支付设置字段无效")
    if type(body["enabled"]) is not bool:
        raise AdminPaymentError("enabled 必须是布尔值")
    result: dict[str, Any] = {}

    def transform(current: str) -> str:
        saved = read_env_text(current)
        new_pkey = _string(body["pkey"], "PKey")
        pkey = (
            encode_smtp_password(new_pkey)
            if new_pkey
            else _saved_value(saved, "EPAY_PKEY", "EPAY_MERCHANT_KEY")
        )
        values = {
            "EPAY_ENABLED": str(body["enabled"]).lower(),
            "EPAY_API_BASE": normalize_api_base(
                _string(body["api_base"], "API 地址")
            ),
            "EPAY_PID": _string(body["pid"], "PID"),
            "EPAY_PKEY": pkey,
            "EPAY_PAYMENT_TYPE": _string(body["payment_type"], "支付类型"),
            "EPAY_ORDER_TTL_SECONDS": str(
                _integer(body["order_ttl_seconds"], "订单有效期")
            ),
            "EPAY_AMOUNT_HOLD_SECONDS": str(
                _integer(body["amount_hold_seconds"], "金额冻结期")
            ),
        }
        raw_types = [
            t.strip().lower()
            for t in values["EPAY_PAYMENT_TYPE"].split(",")
            if t.strip()
        ]
        if body["enabled"]:
            if not raw_types or not set(raw_types).issubset({"alipay", "wxpay"}):
                raise AdminPaymentError("启用在线支付时，请至少选择支付宝或微信支付中的一种")
        else:
            if raw_types and not set(raw_types).issubset({"alipay", "wxpay"}):
                raise AdminPaymentError("支付类型无效，请选择支付宝或微信支付")
        candidate = {**saved, **values, "NEWS_SITE_URL": site_url}
        if body["enabled"]:
            try:
                config_from_mapping(candidate)
            except PaymentError as error:
                raise AdminPaymentError(str(error)) from None
        else:
            ttl = int(values["EPAY_ORDER_TTL_SECONDS"])
            hold = int(values["EPAY_AMOUNT_HOLD_SECONDS"])
            if not 60 <= ttl <= 3600 or not ttl <= hold <= 86400:
                raise AdminPaymentError("订单有效期或金额冻结期超出允许范围")
        if settlement_locked:
            try:
                existing_config = settlement_config_from_mapping(
                    {**saved, "NEWS_SITE_URL": site_url}
                )
                candidate_config = settlement_config_from_mapping(candidate)
            except PaymentError as error:
                raise AdminPaymentError(str(error)) from None
            if (
                existing_config is None
                or candidate_config is None
                or config_identity(existing_config) != config_identity(candidate_config)
            ):
                raise AdminPaymentError("存在未结算订单，不能替换支付商户、密钥、类型或 API 地址")
        result.update(values)
        return _replace_settings(current, values)

    update_text(env_path, transform)
    return settings_payload({**read_env(env_path), **result}, site_url)


def clear_pkey(env_path: Path, *, settlement_locked: bool = False) -> None:
    if settlement_locked:
        raise AdminPaymentError("存在未结算订单，不能清除支付密钥")

    def transform(current: str) -> str:
        saved = read_env_text(current)
        values = {
            "EPAY_ENABLED": "false",
            "EPAY_API_BASE": _saved_value(saved, "EPAY_API_BASE", "EPAY_BASE_URL"),
            "EPAY_PID": _saved_value(saved, "EPAY_PID", "EPAY_MERCHANT_ID"),
            "EPAY_PKEY": "",
            "EPAY_PAYMENT_TYPE": saved.get("EPAY_PAYMENT_TYPE", "alipay"),
            "EPAY_ORDER_TTL_SECONDS": saved.get("EPAY_ORDER_TTL_SECONDS", "300"),
            "EPAY_AMOUNT_HOLD_SECONDS": saved.get("EPAY_AMOUNT_HOLD_SECONDS", "3600"),
        }
        return _replace_settings(current, values)

    update_text(env_path, transform)
