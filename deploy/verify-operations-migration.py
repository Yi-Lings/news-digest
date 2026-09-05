"""Verify schema 11-to-12 on a private copy; never rebuild or translate content."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from news_digest.delivery.publisher import load_release_manifest
from news_digest.storage import db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    root = args.snapshot.resolve()
    database = root / "news.db"
    if not database.is_file():
        parser.error("snapshot news.db is missing")
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (
            "11",
        ), "an unmigrated schema 11 snapshot is required"
        before = db.verify_database(conn)
    pages = {
        str(path.relative_to(root / "current")): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "current").rglob("*")
        if path.is_file()
    }
    with closing(db.connect(database)) as conn:
        after = db.verify_database(conn)
        assert all(after[name] == facts for name, facts in before.items() if name != "meta")
        assert db.schema_is_current(conn)
        publication = load_release_manifest(root / "current")
        assert db.publication_covers_build(
            conn, publication.release_date, publication.edition.generation, publication.edition
        )
        edition = db.operational_snapshot(
            conn, date=publication.release_date, now=publication.published_at.isoformat()
        )["edition"]
        report = {
            "schema": db.SCHEMA_VERSION,
            "unchanged_tables": len(before) - 1,
            "fact_rows": {name: facts["rows"] for name, facts in before.items()},
            "publication": publication.release_name,
            "edition": edition,
            "unchanged_files": len(pages),
        }
    with closing(db.connect(database)) as conn:
        assert db.verify_database(conn) == after, "migration is not idempotent"
    assert all(
        hashlib.sha256((root / "current" / name).read_bytes()).hexdigest() == digest
        for name, digest in pages.items()
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
