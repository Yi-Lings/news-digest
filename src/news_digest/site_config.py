"""Project only the runtime secrets required by the public Site service."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from news_digest.admin_email import read_env
from news_digest.config_io import atomic_write_text

SITE_ENV_KEYS = (
    "NEWS_SITE_URL",
    "NEWS_TIMEZONE",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_SECURITY",
    "SMTP_FROM",
    "EPAY_ENABLED",
    "EPAY_API_BASE",
    "EPAY_PID",
    "EPAY_PKEY",
    "EPAY_PAYMENT_TYPE",
    "EPAY_ORDER_TTL_SECONDS",
    "EPAY_AMOUNT_HOLD_SECONDS",
)


def render_site_environment(environ: Mapping[str, str]) -> str:
    lines = ["# Generated from /config/.env; do not edit directly."]
    for key in SITE_ENV_KEYS:
        value = environ.get(key, "")
        if "\r" in value or "\n" in value:
            raise ValueError(f"{key} must be a single-line value")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def sync_site_environment(source: Path, target: Path) -> None:
    source_path = Path(source)
    target_path = Path(target)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("Site environment projection must use a separate file")
    atomic_write_text(target_path, render_site_environment(read_env(source_path)))
