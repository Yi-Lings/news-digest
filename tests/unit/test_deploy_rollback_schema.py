"""Rollback keeps post-migration writes while restoring the old schema marker."""

import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "rollback-schema.py"


def _version(database: Path) -> str:
    with sqlite3.connect(database) as conn:
        return conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "news.db"
    before = tmp_path / "before.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '13')")
        conn.execute("CREATE TABLE translation_attempts (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO translation_attempts VALUES (1)")
        conn.commit()
        with sqlite3.connect(before) as snapshot:
            conn.backup(snapshot)
        conn.execute("ALTER TABLE translation_attempts ADD COLUMN http_status INTEGER")
        conn.execute("ALTER TABLE translation_attempts ADD COLUMN timings_json TEXT")
        conn.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
        conn.execute(
            "INSERT INTO translation_attempts VALUES (2, 504, '{\"total_ms\":90000}')"
        )
    return database, before


def _run(database: Path, before: Path, checkpoint: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, str(SCRIPT), "--database", str(database),
            "--before", str(before), "--checkpoint", str(checkpoint),
            "--old-schema", "13",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_rollback_preserves_post_migration_rows_and_takes_checkpoint(tmp_path: Path):
    database, before = _fixture(tmp_path)
    checkpoint = tmp_path / "checkpoint.db"
    result = _run(database, before, checkpoint)
    assert result.returncode == 0, result.stderr
    assert _version(database) == "13"
    assert _version(checkpoint) == "14"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT id, http_status FROM translation_attempts").fetchall() == [
            (1, None), (2, 504)
        ]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_rollback_rejects_unexpected_schema_without_mutation(tmp_path: Path):
    database, before = _fixture(tmp_path)
    checkpoint = tmp_path / "checkpoint.db"
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE translation_attempts ADD COLUMN unexpected TEXT")
    result = _run(database, before, checkpoint)
    assert result.returncode != 0
    assert _version(database) == "14"
    assert not checkpoint.exists()


def test_rollback_rejects_wrong_previous_image_schema(tmp_path: Path):
    database, before = _fixture(tmp_path)
    checkpoint = tmp_path / "checkpoint.db"
    with sqlite3.connect(before) as conn:
        conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
    result = _run(database, before, checkpoint)
    assert result.returncode != 0
    assert _version(database) == "14"
    assert not checkpoint.exists()
