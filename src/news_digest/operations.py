"""Local recovery packages and small, persistent business health checks."""

import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
import zoneinfo
from contextlib import closing
from pathlib import Path

from news_digest.storage import db


def readiness(database: Path, site_dir: Path) -> bool:
    try:
        with closing(
            sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        ) as conn:
            current_schema = db.schema_is_current(conn)
        return current_schema and (site_dir / "index.html").is_file()
    except (OSError, sqlite3.Error):
        return False


def business_status(database: Path, site_dir: Path, *, timezone: str, now: dt.datetime) -> dict:
    local = now.astimezone(zoneinfo.ZoneInfo(timezone))
    conn = db.connect(database)
    try:
        state = db.operational_snapshot(conn, date=local.date().isoformat(), now=now.isoformat())
    finally:
        conn.close()
    edition = state["edition"]
    backup = state["backup_verified_at"]
    state["checks"] = {
        "readiness": not readiness(database, site_dir),
        "edition_overdue": local.hour >= 10
        and (
            not edition
            or edition["online_count"] < edition["target_count"]
            or edition["target_count"] == 0
        ),
        "translation_blocked": bool(state["tasks"].get("configuration_blocked")),
        "mail_unknown": bool(state["delivery"].get("unknown")),
        "mail_failed": bool(state["delivery"].get("failed")),
        "outbox_overdue": bool(state["outbox_overdue"]),
        "payment_review": bool(state["payment_cases_open"]),
        "backup_overdue": not backup
        or now - dt.datetime.fromisoformat(backup) > dt.timedelta(hours=26),
    }
    state["database_bytes"] = database.stat().st_size
    state["disk_free_bytes"] = shutil.disk_usage(database.parent).free
    state["checks"]["disk_low"] = state["disk_free_bytes"] < 256 * 1024 * 1024
    return state


def monitor(
    database: Path, site_dir: Path, *, timezone: str, configuration_failed: bool = False
) -> dict:
    now = dt.datetime.now(dt.UTC)
    state = business_status(database, site_dir, timezone=timezone, now=now)
    state["checks"]["configuration_pending"] = configuration_failed
    conn = db.connect(database)
    try:
        for key, unhealthy in state["checks"].items():
            event = db.record_operational_event(
                conn,
                key=key,
                unhealthy=unhealthy,
                detail={"date": state["date"]},
                now=now.isoformat(),
            )
            if event:
                logging.getLogger(__name__).log(
                    logging.ERROR if event == "unhealthy" else logging.WARNING,
                    "business_alert key=%s state=%s date=%s",
                    key,
                    event,
                    state["date"],
                )
    finally:
        conn.close()
    return state


def _hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != root / "backup.json":
            with path.open("rb") as handle:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(
                    handle, "sha256"
                ).hexdigest()
    return result


def verify_backup(archive: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=".news-restore-", dir=archive.parent) as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as bundle:
            if any(not (member.isfile() or member.isdir()) for member in bundle.getmembers()):
                raise ValueError("backup must not contain links or special files")
            bundle.extractall(root, filter="data")
        manifest = json.loads((root / "backup.json").read_text(encoding="utf-8"))
        if _hashes(root) != manifest["files"]:
            raise ValueError("backup file fingerprints do not match")
        with closing(sqlite3.connect(root / "data/news.db")) as conn:
            conn.row_factory = sqlite3.Row
            if db.verify_database(conn) != manifest["database_facts"]:
                raise ValueError("backup database facts do not match")
            from news_digest.delivery.publisher import load_release_manifest

            current = root / "site/current"
            publication = load_release_manifest(current)
            retained = load_release_manifest(
                root / "site/releases" / publication.release_name,
                expected_release_name=publication.release_name,
            )
            if publication.edition_sha256 != retained.edition_sha256:
                raise ValueError("backup current release identity does not match")
            if not db.publication_covers_build(
                conn, publication.release_date, publication.edition.generation, publication.edition
            ):
                raise ValueError("backup publication does not match database results")
        if not (root / "site/current/index.html").is_file():
            raise ValueError("backup has no published homepage")
        from news_digest.admin_email import read_env

        read_env(root / "config/.env")
        read_env(root / "site-config/.env")
        return {
            "created_at": manifest["created_at"],
            "files": len(manifest["files"]),
            "tables": len(manifest["database_facts"]),
            "verified": True,
        }


def create_backup(
    database: Path,
    data_dir: Path,
    site_root: Path,
    config_dir: Path,
    site_config_dir: Path,
    secret_dir: Path,
    destination: Path,
    *,
    keep: int = 14,
) -> Path:
    started = time.monotonic()
    for source in (site_root, config_dir, site_config_dir, secret_dir):
        if destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("backup destination must be outside its source directories")
    for required in (config_dir / ".env", site_config_dir / ".env", secret_dir / "site-secret"):
        if not required.is_file():
            raise ValueError("backup source configuration or site secret is missing")
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    name = dt.datetime.now(dt.UTC).strftime("daily-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=destination) as temporary:
        root = Path(temporary)
        for source, target in (
            (site_root / "current", root / "site/current"),
            (site_root / ".published", root / "site/.published"),
            (site_root / "releases", root / "site/releases"),
            (data_dir / "translations", root / "data/translations"),
            (data_dir / "mail", root / "data/mail"),
            (config_dir, root / "config"),
            (site_config_dir, root / "site-config"),
            (secret_dir, root / "site-secret"),
        ):
            if source.is_dir():
                shutil.copytree(
                    source.resolve(), target, ignore=shutil.ignore_patterns("*.lock", "*.tmp")
                )
        (root / "data").mkdir(exist_ok=True)
        facts = db.online_backup(database, root / "data/news.db")
        now = dt.datetime.now(dt.UTC).isoformat()
        manifest = {"created_at": now, "database_facts": facts, "files": _hashes(root)}
        (root / "backup.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        pending = destination / f".{name}.tmp"
        try:
            with tarfile.open(pending, "w:gz") as bundle:
                for path in root.iterdir():
                    bundle.add(path, arcname=path.name)
            os.chmod(pending, 0o600)
            verify_backup(pending)
            with pending.open("r+b") as handle:
                os.fsync(handle.fileno())
            archive = destination / f"{name}.tar.gz"
            os.replace(pending, archive)
        finally:
            pending.unlink(missing_ok=True)
    conn = db.connect(database)
    try:
        db.set_settings(
            conn,
            {
                "backup_verified_at": now,
                "backup_archive": archive.name,
                "backup_duration_seconds": str(round(time.monotonic() - started, 3)),
            },
            now=now,
        )
        db.cleanup_temporary_data(conn, now=now)
    finally:
        conn.close()
    for old in sorted(destination.glob("daily-*.tar.gz"), reverse=True)[max(1, keep) :]:
        old.unlink()
    return archive
