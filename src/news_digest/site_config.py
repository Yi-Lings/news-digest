"""Project only the runtime secrets required by the public Site service."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from news_digest import payments
from news_digest.admin_email import read_env
from news_digest.config import parse_dotenv_value, parse_env_text
from news_digest.config_io import atomic_write_text
from news_digest.storage import db

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
        encoded = (
            json.dumps(value, ensure_ascii=False)
            if parse_dotenv_value(value) != value
            else value
        )
        lines.append(f"{key}={encoded}")
    return "\n".join(lines) + "\n"


def sync_site_environment(source: Path, target: Path) -> None:
    source_path = Path(source)
    target_path = Path(target)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("Site environment projection must use a separate file")
    atomic_write_text(target_path, render_site_environment(read_env(source_path)))


def environment_revision(values: Mapping[str, str]) -> str:
    return hashlib.sha256(render_site_environment(values).encode("utf-8")).hexdigest()


def configuration_status(source: Path, target: Path | None, conn) -> dict:
    desired = environment_revision(read_env(source))
    applied = db.get_setting(conn, "configuration_applied_revision")
    projected = environment_revision(read_env(target)) if target and target.is_file() else None
    return {
        "desired_revision": desired,
        "applied_revision": applied,
        "state": "applied" if desired == applied == projected else "pending",
    }


def apply_environment(source: Path, target: Path, conn, *, site_url: str) -> str:
    """Source is authoritative; caller holds a DB write transaction until activation commits."""
    if source.resolve() == target.resolve():
        raise ValueError("Site environment projection must use a separate file")
    values = read_env(source)
    if site_url and values.get("NEWS_SITE_URL", "").rstrip("/") != site_url.rstrip("/"):
        raise ValueError("NEWS_SITE_URL change requires an Admin restart")
    config = payments.settlement_config_from_mapping(values)
    identity = payments.config_identity(config) if config else None
    now = dt.datetime.now(dt.UTC).isoformat()
    bound = db.unsettled_payment_config_ids(conn, now=now)
    if bound and bound != {identity}:
        raise ValueError("unsettled orders require the existing payment identity")
    rendered = render_site_environment(values)
    revision = environment_revision(values)
    projected = target.read_text(encoding="utf-8") if target.is_file() else ""
    if projected != rendered:
        atomic_write_text(target, rendered)
    if parse_env_text(target.read_text(encoding="utf-8")) != parse_env_text(rendered):
        raise ValueError("Site projection readback mismatch")
    db.set_active_payment_config_id(conn, payment_config_id=identity, now=now)
    db.record_configuration_applied(conn, revision=revision, now=now)
    return revision


def recover_environment(source: Path, target: Path, db_path: Path, *, site_url: str) -> str:
    conn = db.connect(db_path)
    try:
        db.begin_payment_config_update(conn, now=dt.datetime.now(dt.UTC).isoformat())
        revision = apply_environment(source, target, conn, site_url=site_url)
        conn.commit()
        return revision
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
