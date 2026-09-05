import datetime as dt
import json
from dataclasses import replace

import pytest

from news_digest.config import BuildConfig
from news_digest.delivery import publisher
from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.pipeline import build_editions
from news_digest.static_resources import resolve_static_resource
from news_digest.storage import db
from news_digest.storage.history import restore_history
from news_digest.translation.schema import (
    build_sentence_snapshot,
    parse_translation,
    result_to_dict,
)
from news_digest.translation.service import cache_key

DATE = "2026-09-05"
NOW = "2026-09-05T00:00:00+00:00"


def edition(date=DATE):
    article = Article(
        slug="old-link",
        source="Fixture",
        title_en="Story",
        summary_en="Summary",
        title_zh="Title",
        summary_zh="Summary",
        author="Fixture",
        published_at=NOW,
        url="https://example.test/story",
        reading_minutes=1,
        paragraphs=[Paragraph(en="First sentence. Second sentence.", zh="First.Second.")],
        translated_by="fixture",
    )
    return DailyEdition(date, [article], generation=1, result_revisions={article.url: 1})


def test_multi_edition_records_survive_release_pruning(tmp_path):
    root = tmp_path / "site"
    config = BuildConfig(root, "https://example.test")
    old = edition("2026-09-04")
    first = build_editions([edition(), old], config)
    for _ in range(6):
        build_editions([edition(), old], config)
    assert not first.exists()
    assert len(list((root / "releases").iterdir())) == 5
    assert publisher.resolve_published_release(root, edition_date=old.date).edition == old
    assert publisher.load_publication_record(root, old.date).edition == old


def test_publication_index_can_recover_after_current_switch(tmp_path, monkeypatch):
    root = tmp_path / "site"
    original = publisher.persist_publication_index

    def interrupted(_root):
        raise OSError("interrupted after switch")

    monkeypatch.setattr(publisher, "persist_publication_index", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        build_editions([edition()], BuildConfig(root, "https://example.test"))
    publication = publisher.resolve_published_release(root)
    original(root)
    assert (
        publisher.load_publication_record(root, DATE).edition_sha256 == publication.edition_sha256
    )
    assert len(list((root / "releases").iterdir())) == 1


def test_build_keeps_unconfirmed_legacy_pages_and_archive_dates(tmp_path):
    root = tmp_path / "site"
    config = BuildConfig(root, "https://example.test")
    build_editions([edition("2026-09-04")], config)
    legacy = root / "current" / "issues" / "2026-09-04" / "old-link.html"
    original = legacy.read_bytes()
    build_editions([edition(), DailyEdition("2026-09-04", source_status="unavailable")], config)
    assert legacy.read_bytes() == original
    assert "2026-09-04" in (root / "current/archive/index.html").read_text(encoding="utf-8")
    assert not (root / "current/.editions/2026-09-04.json").exists()


def test_duplicate_page_paths_fail_before_switch(tmp_path):
    root = tmp_path / "site"
    config = BuildConfig(root, "https://example.test")
    build_editions([edition()], config)
    previous = (root / "current").resolve()
    invalid = edition()
    invalid.articles.append(replace(invalid.articles[0], url="https://example.test/other"))
    with pytest.raises(ValueError, match="Duplicate"):
        build_editions([invalid], config)
    assert (root / "current").resolve() == previous


@pytest.mark.parametrize("splitter", [None, "unsupported"])
def test_history_restores_published_identity_and_only_matching_cache(tmp_path, splitter):
    root, database, cache = tmp_path / "site", tmp_path / "news.db", tmp_path / "cache"
    original = edition()
    build_editions([original], BuildConfig(root, "https://example.test"))
    provider = "default-" + "a" * 64
    conn = db.connect(database)
    try:
        db.seed_edition_items(
            conn,
            original,
            provider_id=provider,
            snapshots={original.articles[0].url: "[2]"},
            now=NOW,
        )
        with conn:
            conn.execute("UPDATE automation_editions SET history_status = 'pending'")
            conn.execute(
                "UPDATE translation_tasks SET status = 'succeeded', segmentation_json = 'bad'"
            )
        before_attempts = [tuple(row) for row in conn.execute("SELECT * FROM translation_attempts")]
    finally:
        conn.close()
    cache.mkdir()
    result = parse_translation(
        json.dumps(
            {
                "title_zh": "Title",
                "summary_zh": "Summary",
                "sentences_zh": [["First.", "Second."]],
            }
        ),
        1,
        frozen_counts=[2],
    )
    data = result_to_dict(result)
    if splitter:
        data["splitter_version"] = splitter
    # The manifest's paragraph must exactly match the cached result.
    assert original.articles[0].paragraphs[0].zh == result.paragraphs_zh[0]
    build_editions([original], BuildConfig(root, "https://example.test"))
    (cache / f"{cache_key(original.articles[0], 'a' * 64)}.json").write_text(json.dumps(data))
    report = restore_history(database, root, cache)
    assert report["editions"] == 1
    assert report["results"] == (0 if splitter else 1)
    conn = db.connect(database)
    try:
        recovered = db.frozen_edition(conn, DATE)
        assert recovered.articles == original.articles
        task = db.active_translation_tasks(conn, DATE)[0]
        assert json.loads(task.segmentation_json) == build_sentence_snapshot(
            [p.en for p in original.articles[0].paragraphs]
        )
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM translation_attempts")
        ] == before_attempts
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert restore_history(database, root, cache)["editions"] == 0


def test_missing_history_does_not_reselect_pool(tmp_path):
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        db.upsert_articles(conn, DATE, edition().articles)
    finally:
        conn.close()
    assert restore_history(database, tmp_path / "site", tmp_path / "cache")["source_only"] == 1
    conn = db.connect(database)
    try:
        frozen = db.frozen_edition(conn, DATE)
        assert frozen.source_status == "unavailable"
        assert frozen.articles == []
    finally:
        conn.close()


def test_build_lease_recovery_and_revision_confirmation(tmp_path):
    current = edition()
    conn = db.connect(tmp_path / "news.db")
    try:
        db.seed_edition_items(
            conn,
            current,
            provider_id="fixture",
            snapshots={current.articles[0].url: "[2]"},
            now=NOW,
        )
        with conn:
            conn.execute("UPDATE edition_items SET result_revision = 1")
            conn.execute(
                "UPDATE translation_tasks SET status = 'succeeded', success_generation = 1"
            )
            conn.execute("UPDATE automation_editions SET dirty_generation = 1")
        first = db.claim_automation_build(conn, DATE, owner="old", now=NOW, lease_seconds=1)
        assert first is not None
        later = (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=2)).isoformat()
        assert db.claim_automation_build(conn, DATE, owner="new", now=later, lease_seconds=30)
        bad = replace(current, result_revisions={current.articles[0].url: 0})
        assert not db.publication_covers_build(conn, DATE, 1, bad)
        with pytest.raises(RuntimeError, match="committed"):
            db.finish_automation_build(
                conn,
                DATE,
                owner="new",
                now=later,
                succeeded=True,
                publication=bad,
            )
        state = db.finish_automation_build(
            conn,
            DATE,
            owner="new",
            now=later,
            succeeded=True,
            publication=current,
        )
        assert state.online_count == 1
        assert state.built_generation == 1
    finally:
        conn.close()


def test_static_resource_policy_serves_pages_but_not_private_metadata(tmp_path):
    build_editions([edition()], BuildConfig(tmp_path, "https://example.test"))
    root = tmp_path / "current"
    assert resolve_static_resource(root, "/") is not None
    assert resolve_static_resource(root, "/issues/2026-09-05/") is not None
    assert resolve_static_resource(root, "/assets/app.js") is not None
    assert resolve_static_resource(root, "/release.json") is None
    assert resolve_static_resource(root, "/.editions/2026-09-05.json") is None


def test_history_recovers_durable_record_after_original_html_release_is_pruned(tmp_path):
    root = tmp_path / "site"
    config = BuildConfig(root, "https://example.test")
    old = edition("2026-09-01")
    first = build_editions([old], config)
    for day in range(2, 8):
        build_editions([edition(f"2026-09-{day:02d}")], config)
    assert not first.exists()
    assert not (root / "current/.editions/2026-09-01.json").exists()
    database = tmp_path / "news.db"
    assert restore_history(database, root, tmp_path / "cache")["editions"] == 7
    conn = db.connect(database)
    try:
        assert db.frozen_edition(conn, old.date).articles == old.articles
    finally:
        conn.close()


def test_running_history_is_deferred_without_marking_source_only(tmp_path):
    root = tmp_path / "site"
    current = edition()
    build_editions([current], BuildConfig(root, "https://example.test"))
    database = tmp_path / "news.db"
    conn = db.connect(database)
    try:
        db.seed_edition_items(
            conn, current, provider_id="fixture",
            snapshots={current.articles[0].url: "[2]"}, now=NOW,
        )
        with conn:
            conn.execute("UPDATE automation_editions SET history_status = 'pending'")
        task = db.active_translation_tasks(conn, DATE)[0]
        db.claim_translation_task(conn, task.task_id, owner="worker", now=NOW, lease_seconds=60)
    finally:
        conn.close()
    assert restore_history(database, root, tmp_path / "cache")["editions"] == 0
    conn = db.connect(database)
    try:
        row = conn.execute("SELECT history_status FROM automation_editions").fetchone()
        assert row[0] == "pending"
    finally:
        conn.close()


def test_deployment_migration_restores_only_latest_edition(tmp_path):
    root, database = tmp_path / "site", tmp_path / "news.db"
    old = edition("2026-09-04")
    build_editions([edition(), old], BuildConfig(root, "https://example.test"))
    report = restore_history(database, root, tmp_path / "cache", latest_only=True)
    assert report["editions"] == 1
    conn = db.connect(database)
    try:
        assert db.frozen_edition(conn, DATE).articles == edition().articles
        assert db.frozen_edition(conn, old.date).source_status == "unavailable"
        assert not db.active_translation_tasks(conn, old.date)
    finally:
        conn.close()
