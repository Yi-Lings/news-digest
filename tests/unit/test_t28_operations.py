import datetime as dt
import json
import shutil
import sqlite3
import tarfile

import pytest

from news_digest import operations, site_config
from news_digest.admin_email import read_env
from news_digest.config import load_env_file, parse_env_text
from news_digest.delivery.publisher import write_release_manifest
from news_digest.models import DailyEdition
from news_digest.storage import db

NOW = "2026-09-05T10:00:00+00:00"


def test_all_dotenv_readers_reject_duplicates_without_values(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    content = "SMTP_PASSWORD=secret-one\nSMTP_PASSWORD=secret-two\n"
    path.write_text(content)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    for read in (
        lambda: parse_env_text(content),
        lambda: read_env(path),
        lambda: load_env_file(path),
    ):
        with pytest.raises(ValueError, match="duplicate configuration key: SMTP_PASSWORD") as error:
            read()
        assert "secret" not in str(error.value)


def test_configuration_projection_recovers_from_file_and_db_failure(tmp_path, monkeypatch):
    source, target = tmp_path / ".env", tmp_path / "site/.env"
    database = tmp_path / "news.db"
    source.write_text("NEWS_SITE_URL=https://news.example.com\nSMTP_PORT=587\n")
    old = site_config.recover_environment(
        source, target, database, site_url="https://news.example.com"
    )
    source.write_text("NEWS_SITE_URL=https://news.example.com\nSMTP_PORT=465\n")
    with monkeypatch.context() as patch:
        patch.setattr(site_config, "atomic_write_text", lambda *_: (_ for _ in ()).throw(OSError()))
        with pytest.raises(OSError):
            site_config.recover_environment(
                source, target, database, site_url="https://news.example.com"
            )
    conn = db.connect(database)
    assert site_config.configuration_status(source, target, conn)["state"] == "pending"
    assert db.get_setting(conn, "configuration_applied_revision") == old
    conn.execute("BEGIN IMMEDIATE")
    site_config.apply_environment(source, target, conn, site_url="https://news.example.com")
    conn.rollback()
    assert site_config.configuration_status(source, target, conn)["state"] == "pending"
    assert db.get_setting(conn, "configuration_applied_revision") == old
    new = site_config.recover_environment(
        source, target, database, site_url="https://news.example.com"
    )
    assert old != new
    assert site_config.configuration_status(source, target, conn)["state"] == "applied"
    conn.close()


def test_business_notifications_only_on_persistent_failure_and_recovery(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    start = dt.datetime.fromisoformat(NOW)

    def record(seconds, unhealthy):
        return db.record_operational_event(
            conn,
            key="outbox",
            unhealthy=unhealthy,
            detail={},
            now=(start + dt.timedelta(seconds=seconds)).isoformat(),
        )

    assert record(0, True) is None
    assert record(599, True) is None
    assert record(600, True) == "unhealthy"
    assert record(601, True) is None
    assert record(602, False) == "recovered"
    assert record(603, False) is None
    conn.close()


def test_backup_round_trip_preserves_business_facts_and_rejects_tampering(tmp_path):
    data = tmp_path / "data"
    database = data / "news.db"
    conn = db.connect(database)
    db.upsert_pending_user(
        conn,
        email="backup@example.com",
        email_key="b" * 64,
        password_hash="not-a-real-password",
        now=NOW,
    )
    conn.close()
    site = tmp_path / "site"
    (site / "current").mkdir(parents=True)
    (site / "current/index.html").write_text("<html>Published edition</html>")
    issue = site / "current/issues/2026-09-05"
    issue.mkdir(parents=True)
    (issue / "index.html").write_text("<html>Issue</html>")
    write_release_manifest(site / "current", "2026-09-05-01", DailyEdition("2026-09-05"))
    shutil.copytree(site / "current", site / "releases/2026-09-05-01")
    (data / "translations").mkdir()
    (data / "translations/result.json").write_text('{"result":"retained"}')
    config = tmp_path / "config"
    config.mkdir()
    (config / ".env").write_text("NEWS_SITE_URL=https://news.example.com\n")
    projected = tmp_path / "site-config"
    projected.mkdir()
    (projected / ".env").write_text((config / ".env").read_text())
    secrets = tmp_path / "site-secret"
    secrets.mkdir()
    (secrets / "site-secret").write_text("test-secret")
    archive = operations.create_backup(
        database, data, site, config, projected, secrets, tmp_path / "backups"
    )
    result = operations.verify_backup(archive)
    assert result["verified"] and result["tables"] >= 20
    conn = db.connect(database)
    assert db.get_setting(conn, "backup_verified_at")
    conn.close()
    extracted = tmp_path / "isolated"
    with tarfile.open(archive) as bundle:
        bundle.extractall(extracted, filter="data")
    assert (extracted / "site-secret/site-secret").read_text() == "test-secret"
    (extracted / "site/current/issues/2026-09-05/index.html").unlink()
    manifest_path = extracted / "backup.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"] = operations._hashes(extracted)
    manifest_path.write_text(json.dumps(manifest))
    incomplete = tmp_path / "incomplete.tar.gz"
    with tarfile.open(incomplete, "w:gz") as bundle:
        for path in extracted.iterdir():
            bundle.add(path, arcname=path.name)
    with pytest.raises(ValueError, match="issue page is missing"):
        operations.verify_backup(incomplete)
    (extracted / "config/.env").write_text("NEWS_SITE_URL=https://changed.example.com\n")
    damaged = tmp_path / "damaged.tar.gz"
    with tarfile.open(damaged, "w:gz") as bundle:
        for path in extracted.iterdir():
            bundle.add(path, arcname=path.name)
    with pytest.raises(ValueError, match="fingerprints"):
        operations.verify_backup(damaged)


def _v11_database(path):
    conn = db.connect(path)
    for table in (
        "entitlement_changes",
        "payment_cases",
        "payment_case_actions",
        "payment_checks",
        "operational_events",
        "fetch_reports",
    ):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
    conn.commit()
    facts = db.database_facts(conn)
    conn.close()
    return facts


def test_v12_migration_preserves_all_existing_tables_and_is_idempotent(tmp_path):
    path = tmp_path / "news.db"
    facts = _v11_database(path)
    conn = db.connect(path)
    after = db.database_facts(conn)
    assert all(after[table] == value for table, value in facts.items() if table != "meta")
    conn.close()
    conn = db.connect(path)
    assert db.database_facts(conn) == after
    conn.close()
    with sqlite3.connect(tmp_path / "news.db.pre-v12.bak") as backup:
        assert db.database_facts(backup) == facts


def test_v12_migration_rolls_back_schema_and_facts(tmp_path, monkeypatch):
    path = tmp_path / "news.db"
    facts = _v11_database(path)

    def fail(conn):
        conn.execute("CREATE TABLE injected(id INTEGER)")
        raise RuntimeError("injected")

    monkeypatch.setattr(db, "_apply_v12_schema", fail)
    with pytest.raises(RuntimeError, match="injected"):
        db.connect(path)
    with sqlite3.connect(path) as conn:
        assert db.database_facts(conn) == facts


def test_cleanup_is_bounded_and_keeps_financial_and_delivery_tables(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    for index in range(5):
        db.issue_email_code(
            conn,
            email_key=f"{index:064x}",
            purpose="register",
            code_digest="a" * 64,
            ttl_seconds=10,
            now=NOW,
        )
    before = db.database_facts(conn)
    deleted = db.cleanup_temporary_data(conn, now="2026-11-01T00:00:00+00:00", limit=2)
    assert deleted["email_codes"] == 2
    after = db.database_facts(conn)
    for table in ("orders", "entitlement_changes", "email_deliveries", "payment_cases"):
        assert after[table] == before[table]
    conn.close()


def test_config_serializer_preserves_quoted_literal_and_whitespace():
    values = {"SMTP_USERNAME": ' "quoted" ', "SMTP_PASSWORD": "plain"}
    rendered = site_config.render_site_environment(values)
    assert parse_env_text(rendered)["SMTP_USERNAME"] == values["SMTP_USERNAME"]
    assert (
        parse_env_text("SMTP_PASSWORD=' literal password '\n")["SMTP_PASSWORD"]
        == " literal password "
    )
