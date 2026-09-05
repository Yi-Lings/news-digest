"""Rehearse migration against an isolated snapshot, never a live database."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from news_digest.config import BuildConfig, FetchConfig
from news_digest.pipeline import build_editions, load_db_editions
from news_digest.storage import db
from news_digest.storage.history import restore_history

FACT_TABLES = (
    "users", "user_sessions", "orders", "redemption_codes", "subscriptions",
    "subscription_tokens", "email_codes", "account_mail_outbox", "email_deliveries",
    "email_delivery_runs", "email_archives", "email_test_attempts", "free_reads", "site_settings",
)


def fingerprints(database):
    with sqlite3.connect(database) as conn:
        return {
            table: (
                len(rows := conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()),
                hashlib.sha256(repr(rows).encode()).hexdigest(),
            )
            for table in FACT_TABLES
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    root = args.snapshot.resolve()
    database = root / "news.db"
    if not database.is_file():
        parser.error("Snapshot news.db is missing")
    before = fingerprints(database)
    report = restore_history(database, root, root / "translations", latest_only=True)
    assert fingerprints(database) == before, "Business facts changed during migration"
    conn = db.connect(database)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
        report["fact_rows"] = {table: value[0] for table, value in before.items()}
        report["edition_states"] = [
            dict(row) for row in conn.execute(
                "SELECT edition_date, target_count, succeeded_count, online_count, history_status"
                " FROM automation_editions ORDER BY edition_date DESC LIMIT 5"
            )
        ]
    finally:
        conn.close()
    assert restore_history(database, root, root / "translations", latest_only=True)["editions"] == 0
    if args.build:
        fetch = FetchConfig(None, 24, "Asia/Hong_Kong", root)
        editions = load_db_editions(fetch)
        unconfirmed = {e.date for e in editions if e.source_status != "manifest"}
        historical_pages = {
            str(path.relative_to(root / "current")): hashlib.sha256(path.read_bytes()).hexdigest()
            for date in unconfirmed for path in (root / "current/issues" / date).glob("*.html")
        }
        build_editions(editions, BuildConfig(root, "https://news.cheapcoding.top"))
        assert all(
            hashlib.sha256((root / "current" / path).read_bytes()).hexdigest() == digest
            for path, digest in historical_pages.items()
        ), "Unconfirmed historical pages changed"
        report["preserved_pages"] = len(historical_pages)
        assert fingerprints(database) == before
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
