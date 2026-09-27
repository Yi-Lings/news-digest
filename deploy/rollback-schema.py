#!/usr/bin/env python3
"""Rewind the additive schema 14 marker for a verified schema 13 deployment.

The database contents are preserved. A consistent checkpoint is written before
changing the marker, so a failed service restart can be investigated safely.
"""

import argparse
import sqlite3
from pathlib import Path


def rewind(db_path: Path, before_path: Path, checkpoint_path: Path, old_schema: int) -> None:
    if old_schema not in (13, 14):
        raise ValueError("previous image schema is not supported for automatic rollback")
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("current database is missing or is a symlink")
    if not before_path.is_file() or before_path.is_symlink():
        raise ValueError("pre-migration backup is missing or is a symlink")
    if checkpoint_path.exists():
        raise ValueError("rollback checkpoint already exists")

    with sqlite3.connect(before_path.resolve().as_uri() + "?mode=ro", uri=True) as before:
        previous = before.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if previous != (str(old_schema),):
            raise ValueError("previous image schema differs from pre-migration database")
        before_columns = {
            row[1] for row in before.execute("PRAGMA table_info(translation_attempts)")
        }

    with sqlite3.connect(db_path, timeout=30) as current:
        current_version = current.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if current_version not in ((str(old_schema),), ("14",)):
            raise ValueError("current schema is not eligible for automatic rollback")
        current_columns = {
            row[1] for row in current.execute("PRAGMA table_info(translation_attempts)")
        }
        expected_columns = before_columns | {"http_status", "timings_json"}
        if current_version == ("14",) and old_schema == 13:
            if current_columns != expected_columns:
                raise ValueError("schema 14 differs from the expected additive migration")
        elif current_columns != before_columns:
            raise ValueError("database columns differ from the pre-migration schema")
        if current.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("current database integrity check failed")

        with sqlite3.connect(checkpoint_path) as checkpoint:
            current.backup(checkpoint)
            if checkpoint.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("rollback checkpoint integrity check failed")

        if current_version == ("14",) and old_schema == 13:
            current.execute("BEGIN IMMEDIATE")
            try:
                row = current.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                if row != ("14",):
                    raise ValueError("schema changed during rollback")
                current.execute(
                    "UPDATE meta SET value='13' WHERE key='schema_version'"
                )
                current.commit()
            except BaseException:
                current.rollback()
                raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--old-schema", type=int, required=True)
    args = parser.parse_args()
    rewind(args.database, args.before, args.checkpoint, args.old_schema)
