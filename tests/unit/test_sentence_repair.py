"""Offline regressions for frozen sources and automatic invalid-slot repair."""

import copy
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from news_digest.config import TranslationConfig
from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.storage import db
from news_digest.translation import schema, service
from news_digest.translation.automation import TranslationAutomationRunner
from news_digest.translation.client import ApiTranslator, TranslationError

NOW = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures/translations/valid-response.json"


def article():
    return Article(
        slug="story",
        source="Fixture",
        title_en="Story",
        summary_en="Summary",
        author="Fixture",
        published_at=NOW.isoformat(),
        url="https://example.test/story",
        reading_minutes=1,
        paragraphs=[Paragraph(en="The count was 42. It rose again. They kept records.")],
    )


def candidate(slots=None):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["sentences_zh"] = [slots if slots is not None else ["Good first.", "", "Good last."]]
    return data


class Translator:
    label = "fixture@p7"
    model = "fixture"
    cache_identity = "fixture"
    timeout_seconds = 600.0

    def __init__(self, data=None, repair=None):
        self.data = candidate() if data is None else data
        self.repair = repair
        self.calls = []

    def translate_request(self, article, **kwargs):
        self.calls.append(("article", kwargs))
        return json.dumps(self.data)

    def translate_sentence_repair(self, **kwargs):
        self.calls.append(("sentence", kwargs))
        if isinstance(self.repair, Exception):
            raise self.repair
        return self.repair or json.dumps(
            {
                "paragraph_index": kwargs["paragraph_index"],
                "sentence_index": kwargs["sentence_index"],
                "translation_zh": "Repaired sentence.",
            }
        )


def translate(tmp_path, translator, **kwargs):
    sources = [schema.split_sentences(article().paragraphs[0].en)]
    return service.translate_article_once(
        article(),
        translator,
        tmp_path,
        frozen_counts=[3],
        frozen_sentences=sources,
        **kwargs,
    )


@pytest.mark.parametrize("value", ["", " ", None, 42, {}, []])
def test_invalid_slot_keeps_candidate_and_exact_coordinate(value):
    data = candidate(["Good first.", value, "Good last."])
    with pytest.raises(schema.InvalidTranslation) as caught:
        schema.parse_translation(json.dumps(data), 1, frozen_counts=[3])
    assert caught.value.sentence_failures == ((1, 2),)
    assert caught.value.candidate == data


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        "null",
        "not json",
        '{"paragraph_index":1,"sentence_index":2,"translation_zh":""}',
        '{"paragraph_index":1,"sentence_index":2,"translation_zh":null}',
        '{"paragraph_index":1,"sentence_index":3,"translation_zh":"ok"}',
        '{"paragraph_index":true,"sentence_index":2,"translation_zh":"ok"}',
        '{"paragraph_index":1,"sentence_index":2,"translation_zh":"ok","extra":1}',
        '{"paragraph_index":1,"sentence_index":2,"sentence_index":2,"translation_zh":"ok"}',
        'prefix {"paragraph_index":1,"sentence_index":2,"translation_zh":"ok"}',
    ],
)
def test_repair_protocol_rejects_ambiguous_or_invalid_output(raw):
    with pytest.raises(schema.InvalidTranslation):
        schema.parse_sentence_repair(raw, paragraph_index=1, sentence_index=2)


def test_only_target_changes_and_learning_fields_survive(tmp_path):
    translator = Translator()
    accepted = []
    translated, hit = translate(tmp_path, translator, on_result=accepted.append)
    assert not hit
    assert [kind for kind, _ in translator.calls] == ["article", "sentence"]
    kwargs = translator.calls[1][1]
    assert (kwargs["paragraph_index"], kwargs["sentence_index"]) == (1, 2)
    assert kwargs["source_sentence"] == "It rose again."
    assert kwargs["context_before"] == "The count was 42."
    assert accepted[0].sentences_zh == [["Good first.", "Repaired sentence.", "Good last."]]
    assert [vars(item) for item in accepted[0].vocabulary] == translator.data["vocabulary"]
    assert translated.paragraphs[0].zh == "".join(accepted[0].sentences_zh[0])
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_multiple_failures_share_three_requests(tmp_path):
    translator = Translator(candidate(["", "Good middle.", None]))
    _, hit = translate(tmp_path, translator)
    assert not hit
    assert [kwargs.get("sentence_index") for _, kwargs in translator.calls] == [None, 1, 3]


def test_exhaustion_does_not_publish_or_cache_partial_translation(tmp_path):
    translator = Translator(candidate(["", "", ""]))
    with pytest.raises(schema.InvalidTranslation):
        translate(tmp_path, translator)
    assert len(translator.calls) == 3
    assert list(tmp_path.glob("*.json")) == []


def test_wrong_repair_id_retries_same_slot_without_whole_article(tmp_path):
    translator = Translator(
        repair=json.dumps(
            {
                "paragraph_index": 1,
                "sentence_index": 3,
                "translation_zh": "Wrong target.",
            }
        )
    )
    with pytest.raises(schema.InvalidTranslation):
        translate(tmp_path, translator)
    assert [kind for kind, _ in translator.calls] == ["article", "sentence", "sentence"]
    assert translator.data["sentences_zh"][0][1] == ""


def test_structural_errors_never_guess_a_sentence(tmp_path):
    translator = Translator(candidate(["Only one."]))
    with pytest.raises(schema.InvalidTranslation):
        translate(tmp_path, translator)
    assert [kind for kind, _ in translator.calls] == ["article"] * 3


@pytest.mark.parametrize(
    "category",
    ["request_cancelled", "termination_unconfirmed", "total_timeout"],
)
def test_cancellation_and_timeout_propagate(tmp_path, category):
    translator = Translator(
        repair=TranslationError(
            "redacted",
            category=category,
            termination_confirmed=category != "termination_unconfirmed",
        )
    )
    with pytest.raises(TranslationError) as caught:
        translate(tmp_path, translator)
    assert caught.value.category == category
    assert len(translator.calls) == 2
    assert not list(tmp_path.glob("*.json"))


def test_valid_cache_and_numeric_diagnostics_do_not_trigger_requests(tmp_path):
    translator = Translator(candidate(["Number omitted.", "Second.", "Third."]))
    translate(tmp_path, translator)
    saved = next(tmp_path.glob("*.json")).read_bytes()
    _, hit = translate(tmp_path, translator)
    assert hit and len(translator.calls) == 1
    assert next(tmp_path.glob("*.json")).read_bytes() == saved


def test_elapsed_budget_is_shared(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    translator = Translator()
    original = translator.translate_request

    def slow(*args, **kwargs):
        clock[0] = 601.0
        return original(*args, **kwargs)

    translator.translate_request = slow
    with pytest.raises(TranslationError, match="deadline"):
        translate(tmp_path, translator)
    assert len(translator.calls) == 1


def test_frozen_snapshot_rejects_drift_without_resegmenting(monkeypatch):
    paragraphs = [article().paragraphs[0].en]
    snapshot = schema.build_sentence_snapshot(paragraphs)
    monkeypatch.setattr(schema, "split_sentences", lambda _: pytest.fail("resegmented"))
    assert len(schema.read_sentence_snapshot(snapshot, paragraphs)[0]) == 3
    with pytest.raises(ValueError, match="hash"):
        schema.read_sentence_snapshot(snapshot, ["Other. Source. Text."])
    bad = copy.deepcopy(snapshot)
    bad["paragraphs"][0]["sentences"][1] = "Invented."
    with pytest.raises(ValueError, match="text"):
        schema.read_sentence_snapshot(bad, paragraphs)


@pytest.mark.parametrize("api_type", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_repair_uses_existing_provider_adapter(tmp_path, api_type, stream):
    raw = '{"paragraph_index":1,"sentence_index":2,"translation_zh":"Fixed."}'

    def handler(request):
        data = json.loads(request.content)
        assert data["model"] == "custom-model"
        assert data["stream"] is stream
        assert "P1S2" in data["messages"][-1]["content"]
        if api_type == "openai_chat":
            assert request.url.path.endswith("/chat/completions")
            if stream:
                event = json.dumps({"choices": [{"delta": {"content": raw}}]})
                return httpx.Response(200, text=f"data: {event}\n\ndata: [DONE]\n\n")
            return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})
        assert request.url.path.endswith("/messages")
        if stream:
            event = json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": raw},
                }
            )
            return httpx.Response(
                200, text=(f"data: {event}\n\n" + 'data: {"type":"message_stop"}\n\n')
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": raw}]})

    translator = ApiTranslator(
        TranslationConfig(
            base_url="https://example.test/v1",
            api_key="fixture",
            model="custom-model",
            timeout_seconds=10,
            max_tokens=2048,
            cache_dir=tmp_path,
            api_type=api_type,
            stream=stream,
        ),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (
            translator.translate_sentence_repair(
                title_en="Story",
                paragraph_index=1,
                sentence_index=2,
                source_sentence="Original.",
                previous_translation="",
                context_before="",
                context_after="",
                evidence=[],
                timeout_seconds=5,
            )
            == raw
        )
    finally:
        translator.close()


def runner(tmp_path, translator):
    return TranslationAutomationRunner(
        database=tmp_path / "news.db",
        provider_id="provider-fixture",
        translator=translator,
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: "release",
        delivery_callback=lambda date, key: True,
    )


def test_automation_persists_full_result_audit_and_build_in_one_commit(tmp_path):
    worker = runner(tmp_path, Translator())
    worker.seed_edition(DailyEdition(NOW.date().isoformat(), [article()]), now=NOW)
    assert worker.run_ready(now=NOW, owner="worker").succeeded == 1
    conn = db.connect(worker.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        item = db.translation_item(conn, task.task_id)
        assert item["result_revision"] == 1
        assert json.loads(item["result_json"])["sentences_zh"][0][1] == "Repaired sentence."
        assert task.success_generation == 1
        assert db.automation_edition(conn, "2026-09-05").dirty_generation == 1
        attempt = db.list_translation_attempts(conn, task.task_id)[0]
        assert attempt.provider_id == "provider-fixture"
        requests = json.loads(attempt.requests_json)
        assert [entry["target"] for entry in requests] == [None, "P1S2"]
        assert attempt.status == "succeeded"
    finally:
        conn.close()
    assert worker.run_ready(now=NOW, owner="second").claimed == 0


def test_refetch_does_not_move_frozen_article_or_replace_sentences(tmp_path):
    worker = runner(tmp_path, Translator())
    first = DailyEdition("2026-09-05", [article()])
    worker.seed_edition(first, now=NOW)
    changed = replace(article(), paragraphs=[Paragraph(en="Changed source.")])
    worker.seed_edition(DailyEdition("2026-09-05", [changed]), now=NOW)
    worker.seed_edition(DailyEdition("2026-09-06", [changed]), now=NOW)
    conn = db.connect(worker.database)
    try:
        db.upsert_articles(conn, "2026-09-06", [changed])
        assert db.get_edition(conn, "2026-09-05").articles[0].paragraphs == article().paragraphs
        assert db.get_edition(conn, "2026-09-06").articles[0].paragraphs == changed.paragraphs
    finally:
        conn.close()


def test_success_transaction_rolls_back_when_build_intent_fails(tmp_path, monkeypatch):
    worker = runner(tmp_path, Translator())
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    monkeypatch.setattr(
        db,
        "mark_translation_ready_for_build",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fixture rollback")),
    )
    with pytest.raises(RuntimeError, match="rollback"):
        worker.run_ready(now=NOW, owner="worker")
    conn = db.connect(worker.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        assert task.status == "running"
        assert db.translation_item(conn, task.task_id)["result_json"] is None
        assert db.automation_edition(conn, "2026-09-05").dirty_generation == 0
    finally:
        conn.close()


def test_legacy_count_snapshot_uses_bounded_whole_article_correction(tmp_path):
    translator = Translator()
    with pytest.raises(schema.InvalidTranslation):
        service.translate_article_once(article(), translator, tmp_path, frozen_counts=[3])
    assert [kind for kind, _ in translator.calls] == ["article"] * 3


def test_repair_protocol_update_does_not_invalidate_valid_article_cache(monkeypatch):
    key = service.cache_key(article(), "fixture")
    monkeypatch.setattr(schema, "SENTENCE_REPAIR_PROTOCOL_VERSION", "sr-next")
    assert service.cache_key(article(), "fixture") == key


def test_duplicate_provider_tasks_do_not_count_or_schedule_twice(tmp_path):
    worker = runner(tmp_path, Translator())
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    assert worker.run_ready(now=NOW, owner="worker").succeeded == 1
    conn = db.connect(worker.database)
    try:
        historical = db.ensure_translation_task(
            conn,
            edition_date="2026-09-05",
            article_id=article().url,
            article_title="Story",
            provider_id="old-provider",
            now=NOW.isoformat(),
        )
        assert (
            db.claim_translation_task(
                conn,
                historical.task_id,
                owner="other",
                now=NOW.isoformat(),
                lease_seconds=300,
            )
            is None
        )
        assert len(db.active_translation_tasks(conn, "2026-09-05")) == 1
    finally:
        conn.close()
    assert worker.flush_build(now=NOW + dt.timedelta(seconds=3), owner="builder")
    conn = db.connect(worker.database)
    try:
        state = db.automation_edition(conn, "2026-09-05")
        assert (state.target_count, state.succeeded_count, state.online_count) == (1, 1, 1)
        assert state.status == "complete"
    finally:
        conn.close()


def test_rebinding_preserves_previous_provider_attempt(tmp_path):
    worker = runner(tmp_path, Translator(candidate(["", "", ""])))
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    assert worker.run_ready(now=NOW, owner="worker").failed == 1
    conn = db.connect(worker.database)
    try:
        old = db.active_translation_tasks(conn, "2026-09-05")[0]
        counts = db.retry_edition_failed_tasks(
            conn,
            "2026-09-05",
            now=NOW.isoformat(),
            actor="admin",
            provider_id="new-default",
        )
        assert counts["queued"] == 1
        current = db.active_translation_tasks(conn, "2026-09-05")[0]
        assert current.task_id != old.task_id
        assert current.provider_id == "new-default"
        assert db.translation_task(conn, old.task_id).provider_id == "provider-fixture"
        assert db.list_translation_attempts(conn, old.task_id)[0].provider_id == "provider-fixture"
        assert current.segmentation_json == old.segmentation_json
    finally:
        conn.close()


def legacy_database(path):
    conn = db.connect(path)
    db.upsert_articles(conn, "2026-09-05", [article()])
    db.ensure_automation_edition(conn, "2026-09-05", target_count=1, now=NOW.isoformat())
    task = db.ensure_translation_task(
        conn,
        edition_date="2026-09-05",
        article_id=article().url,
        article_title="Story",
        provider_id="legacy",
        now=NOW.isoformat(),
        segmentation_json="[3]",
    )
    db.claim_translation_task(
        conn,
        task.task_id,
        owner="legacy-worker",
        now=NOW.isoformat(),
        lease_seconds=300,
    )
    conn.executescript(
        "DROP TABLE edition_items;"
        "ALTER TABLE translation_attempts DROP COLUMN provider_id;"
        "ALTER TABLE translation_attempts DROP COLUMN requests_json;"
        "ALTER TABLE automation_editions DROP COLUMN briefs_json;"
        "UPDATE meta SET value = '10' WHERE key = 'schema_version';"
    )
    conn.close()
    return task


def test_v10_migration_backups_and_preserves_sources_attempts_and_legacy_counts(tmp_path):
    path = tmp_path / "news.db"
    old = legacy_database(path)
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.translation_item(conn, old.task_id)["segmentation_json"] == "[3]"
        attempt = db.list_translation_attempts(conn, old.task_id)[0]
        assert attempt.provider_id is None
        assert attempt.status == "running"
        assert db.frozen_edition(conn, "2026-09-05").articles[0] == article()
    finally:
        conn.close()
    backup = db.sqlite3.connect(path.with_name("news.db.pre-v11.bak"))
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone() == ("10",)
        assert backup.execute("SELECT COUNT(*) FROM translation_attempts").fetchone() == (1,)
    finally:
        backup.close()


def test_migration_rolls_back_schema_and_keeps_backup(tmp_path, monkeypatch):
    path = tmp_path / "news.db"
    legacy_database(path)
    apply = db._apply_v11_schema

    def interrupted(conn):
        apply(conn)
        raise RuntimeError("migration interrupted")

    monkeypatch.setattr(db, "_apply_v11_schema", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        db.connect(path)
    conn = db.sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'",
        ).fetchone() == ("10",)
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'edition_items'",
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT COUNT(*) FROM translation_attempts").fetchone() == (1,)
    finally:
        conn.close()


def test_inactive_tasks_cannot_starve_ready_limit_or_queue_manual_work(tmp_path):
    worker = runner(tmp_path, Translator())
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    conn = db.connect(worker.database)
    try:
        historical = db.ensure_translation_task(
            conn,
            edition_date="2026-09-05",
            article_id="https://example.test/not-a-member",
            article_title="Historical",
            provider_id="old",
            now=(NOW - dt.timedelta(days=1)).isoformat(),
        )
        current = db.active_translation_tasks(conn, "2026-09-05")[0]
        assert [
            t.task_id
            for t in db.list_ready_translation_tasks(
                conn,
                "2026-09-05",
                now=NOW.isoformat(),
                limit=1,
            )
        ] == [current.task_id]
        with pytest.raises(RuntimeError, match="Historical"):
            db.queue_translation_task_dispatch(
                conn,
                historical.task_id,
                now=NOW.isoformat(),
                actor="test",
            )
    finally:
        conn.close()


def test_forced_retry_keeps_last_result_until_success_and_advances_generation(tmp_path):
    translator = Translator()
    worker = runner(tmp_path, translator)
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    worker.run_ready(now=NOW, owner="first")
    conn = db.connect(worker.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        original = db.translation_item(conn, task.task_id)
        db.queue_translation_task_retry(
            conn,
            task.task_id,
            now=NOW.isoformat(),
            actor="test",
            force=True,
        )
        assert db.translation_item(conn, task.task_id)["payload"] == original["payload"]
    finally:
        conn.close()
    translator.data = candidate(["", "", ""])
    assert worker.run_ready(now=NOW, owner="failed-redo").failed == 1
    conn = db.connect(worker.database)
    try:
        item = db.translation_item(conn, task.task_id)
        assert item["payload"] == original["payload"]
        assert item["result_revision"] == 1 and item["force_refresh"] == 1
        db.queue_translation_task_retry(conn, task.task_id, now=NOW.isoformat(), actor="test")
    finally:
        conn.close()
    translator.data = candidate(["New first.", "New second.", "New third."])
    result = worker.run_ready(now=NOW, owner="successful-redo")
    assert result.succeeded == 1 and result.cache_hits == 0
    conn = db.connect(worker.database)
    try:
        item = db.translation_item(conn, task.task_id)
        assert item["result_revision"] == 2 and item["force_refresh"] == 0
        assert db.automation_edition(conn, "2026-09-05").dirty_generation == 2
        assert (
            db.get_edition(conn, "2026-09-05").articles[0].paragraphs[0].zh.startswith("New first.")
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "category", ["request_cancelled", "termination_unconfirmed", "total_timeout"]
)
def test_audit_failure_does_not_hide_request_termination(tmp_path, category):
    translator = Translator(repair=TranslationError("stopped", category=category))

    def audit(number, target, outcome, elapsed):
        if outcome == category:
            raise RuntimeError("database unavailable")

    with pytest.raises(TranslationError) as error:
        translate(tmp_path, translator, on_request=audit)
    assert error.value.category == category


@pytest.mark.parametrize("timeout", [1200, 3600])
def test_timeout_configuration_extends_task_lease(tmp_path, timeout):
    translator = Translator()
    translator.timeout_seconds = timeout
    worker = runner(tmp_path, translator)
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    original = translator.translate_request

    def inspect_lease(*args, **kwargs):
        conn = db.connect(worker.database)
        try:
            task = db.active_translation_tasks(conn, "2026-09-05")[0]
            assert dt.datetime.fromisoformat(task.lease_expires_at) == NOW + dt.timedelta(
                seconds=timeout + 60
            )
        finally:
            conn.close()
        return original(*args, **kwargs)

    translator.translate_request = inspect_lease
    assert worker.run_ready(now=NOW, owner="slow-model").succeeded == 1


def test_automatic_probes_share_execution_cap(tmp_path):
    worker = runner(tmp_path, Translator())
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    conn = db.connect(worker.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        for index in range(3):
            timestamp = (NOW + dt.timedelta(minutes=index)).isoformat()
            assert db.claim_translation_task(
                conn, task.task_id, now=timestamp, owner="worker", lease_seconds=300, probe=True,
            )
            db.finish_translation_task_failure(
                conn, task.task_id, owner="worker", now=timestamp, error_code="REQUEST_TIMEOUT",
                error_category="provider_infrastructure", failure_stage="waiting_model",
                diagnostic_id="fixture",
            )
        task = db.translation_task(conn, task.task_id)
        assert task.status == "failed" and not task.auto_retry
        assert db._automatic_translation_attempts(conn, task.task_id) == 3
    finally:
        conn.close()


def test_three_automatic_executions_then_only_explicit_retry(tmp_path):
    translator = Translator()

    def unavailable(*args, **kwargs):
        raise TranslationError("unavailable", category="provider", status=503)

    translator.translate_request = unavailable
    worker = runner(tmp_path, translator)
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    for index in range(3):
        assert worker.run_ready(now=NOW + dt.timedelta(minutes=index), owner="worker").failed == 1
    assert worker.run_ready(now=NOW + dt.timedelta(minutes=4), owner="worker").claimed == 0
    conn = db.connect(worker.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        assert task.status == "failed" and not task.auto_retry
        db.queue_translation_task_retry(
            conn,
            task.task_id,
            now=(NOW + dt.timedelta(minutes=4)).isoformat(),
            actor="test",
        )
    finally:
        conn.close()
    assert worker.run_ready(now=NOW + dt.timedelta(minutes=4), owner="manual").claimed == 1


def test_cli_translate_and_redo_update_frozen_result(tmp_path, monkeypatch):
    from news_digest import cli
    from news_digest.config import FetchConfig

    fetch = FetchConfig(None, 24, "Asia/Hong_Kong", tmp_path / "data")
    translator = Translator()
    translator.close = lambda: None
    config = TranslationConfig(
        base_url="https://example.test",
        api_key="fixture",
        model="fixture",
        timeout_seconds=600,
        max_tokens=2048,
        cache_dir=tmp_path / "cache",
    )
    conn = db.connect(fetch.database)
    try:
        db.upsert_articles(conn, "2026-09-05", [article()])
    finally:
        conn.close()
    monkeypatch.setattr(cli, "_fetch_config", lambda _: fetch)
    monkeypatch.setattr(cli, "_runtime_translation_config", lambda: config)
    monkeypatch.setattr("news_digest.translation.client.ApiTranslator", lambda _: translator)
    assert cli._run_translate("2026-09-05", None, True, frozenset()) == 0
    translator.data = candidate(["New first.", "New second.", "New third."])
    assert cli._run_translate("2026-09-05", 0, True, frozenset({"story"})) == 0
    conn = db.connect(fetch.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        assert db.translation_item(conn, task.task_id)["result_revision"] == 2
        assert (
            db.get_edition(conn, "2026-09-05").articles[0].paragraphs[0].zh.startswith("New first.")
        )
        assert len(db.list_translation_attempts(conn, task.task_id)) == 2
    finally:
        conn.close()


@pytest.mark.parametrize("mismatch", ["owner", "attempt", "source", "cancel", "expired"])
def test_stale_success_cannot_replace_frozen_result(tmp_path, mismatch):
    worker = runner(tmp_path, Translator())
    worker.seed_edition(DailyEdition("2026-09-05", [article()]), now=NOW)
    result = schema.parse_translation(json.dumps(candidate(["First.", "Second.", "Third."])), 1)
    conn = db.connect(worker.database)
    try:
        task = db.active_translation_tasks(conn, "2026-09-05")[0]
        db.claim_translation_task(
            conn, task.task_id, owner="worker", now=NOW.isoformat(), lease_seconds=900
        )
        if mismatch == "source":
            with conn:
                conn.execute("UPDATE edition_items SET source_hash = 'changed'")
        if mismatch == "cancel":
            db.request_translation_task_cancel(
                conn, task.task_id, now=NOW.isoformat(), actor="test"
            )
        with pytest.raises(RuntimeError):
            db.finish_translation_task_success(
                conn,
                task.task_id,
                owner="different" if mismatch == "owner" else "worker",
                expected_attempt=2 if mismatch == "attempt" else 1,
                now=(NOW + dt.timedelta(seconds=901 if mismatch == "expired" else 1)).isoformat(),
                article=schema.apply_translation(article(), result, "fixture"),
                result_json=json.dumps(schema.result_to_dict(result)),
            )
        assert db.translation_item(conn, task.task_id)["result_revision"] == 0
        assert db.translation_task(conn, task.task_id).status == "running"
        assert db.automation_edition(conn, "2026-09-05").dirty_generation == 0
    finally:
        conn.close()
