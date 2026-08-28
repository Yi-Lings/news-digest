import datetime as dt
import json
from collections import Counter
from pathlib import Path

from news_digest.config import BuildConfig, FetchConfig
from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.pipeline import build_editions
from news_digest.storage import db
from news_digest.translation.automation import TranslationAutomationRunner
from news_digest.translation.client import TranslationError
from news_digest.translation.schema import InvalidTranslation


def _at(seconds: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 28, 0, 0, tzinfo=dt.UTC) + dt.timedelta(seconds=seconds)


def _article(number: int) -> Article:
    return Article(
        slug=f"article-{number}",
        source="Fixture Wire",
        title_en=f"Article {number}",
        summary_en="A fixture summary for isolated automation.",
        author="Fixture",
        published_at=_at().isoformat(),
        url=f"https://example.com/article-{number}",
        reading_minutes=1,
        paragraphs=[Paragraph(en=f"Paragraph for article {number}.")],
    )


def _translation(article: Article) -> str:
    return json.dumps(
        {
            "title_zh": f"测试标题 {article.slug}",
            "summary_zh": "用于隔离自动化测试的摘要。",
            "sentences_zh": [["用于自动化测试的段落。"]],
            "vocabulary": [
                {
                    "word": "automation",
                    "phonetic": "/ˌɔːtəˈmeɪʃən/",
                    "meaning_zh": "自动化",
                    "example_en": "Automation runs one task at a time.",
                },
                {
                    "word": "retry",
                    "phonetic": "/ˌriːˈtraɪ/",
                    "meaning_zh": "重试",
                    "example_en": "The worker will retry the failed task.",
                },
                {
                    "word": "persist",
                    "phonetic": "/pərˈsɪst/",
                    "meaning_zh": "持久保存",
                    "example_en": "The task state persists after restart.",
                },
            ],
            "collocations": [
                {
                    "phrase": "retry a task",
                    "meaning_zh": "重试任务",
                    "example_en": "The scheduler can retry a task safely.",
                }
            ],
            "sentence_notes": [
                {
                    "sentence_en": "The task state persists after restart.",
                    "translation_zh": "任务状态会在重启后保留。",
                    "analysis_zh": "主谓结构，after restart 作时间状语。",
                }
            ],
        },
        ensure_ascii=False,
    )


class FakeTranslator:
    label = "fake@automation"
    model = "fake"
    cache_identity = "fake-automation-v1"

    def __init__(self, fail_first: set[str], calls: Counter) -> None:
        self.fail_first = fail_first
        self.calls = calls

    def translate(self, article: Article) -> str:
        self.calls[article.slug] += 1
        if article.slug in self.fail_first and self.calls[article.slug] == 1:
            raise TranslationError("redacted", category="network")
        return _translation(article)


class BuildHarness:
    def __init__(self, database: Path, output_root: Path) -> None:
        self.fetch_config = FetchConfig(
            proxy=None,
            window_hours=24,
            timezone="Asia/Shanghai",
            data_dir=database.parent,
        )
        self.build_config = BuildConfig(output_root=output_root, site_url="https://example.test")
        self.fail_next = False
        self.translated_counts: list[int] = []

    def __call__(self, edition_date: str) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("fixture build failure")
        conn = db.connect(self.fetch_config.database)
        try:
            current = db.get_edition(conn, edition_date)
        finally:
            conn.close()
        assert current is not None
        visible = [article for article in current.articles if article.translated_by]
        self.translated_counts.append(len(visible))
        release = build_editions(
            [DailyEdition(date=edition_date, articles=visible, briefs=current.briefs)],
            self.build_config,
        )
        return release.name


def test_isolated_automation_retries_only_failed_article_and_delivers_once(tmp_path):
    data_dir = tmp_path / "data"
    database = data_dir / "news.db"
    output_root = tmp_path / "site"
    cache_dir = tmp_path / "cache"
    prior = DailyEdition(date="2026-07-27", articles=[_article(99)])
    old_release = build_editions(
        [prior], BuildConfig(output_root=output_root, site_url="https://example.test")
    )

    calls = Counter()
    smtp_calls: list[tuple[str, str]] = []
    build = BuildHarness(database, output_root)
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator({"article-2"}, calls),
        cache_dir=cache_dir,
        build_callback=build,
        delivery_callback=lambda date, key: smtp_calls.append((date, key)) or True,
    )
    edition = DailyEdition(date="2026-07-28", articles=[_article(1), _article(2), _article(3)])
    runner.seed_edition(edition, now=_at())

    result = runner.run_ready(now=_at(1), owner="worker-a", max_tasks=3)
    assert result.succeeded == 2
    assert result.failed == 1
    assert calls == Counter({"article-1": 1, "article-2": 1, "article-3": 1})

    build.fail_next = True
    assert not runner.flush_build(now=_at(3), owner="builder-a")
    assert (output_root / "current").resolve() == old_release.resolve()
    assert smtp_calls == []

    assert runner.flush_build(now=_at(4), owner="builder-a", force=True)
    assert build.translated_counts == [2]
    assert smtp_calls == []

    restarted = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator({"article-2"}, calls),
        cache_dir=cache_dir,
        build_callback=build,
        delivery_callback=lambda date, key: smtp_calls.append((date, key)) or True,
    )
    before_due = restarted.run_ready(now=_at(15), owner="worker-b", max_tasks=3)
    assert before_due.claimed == 0
    assert calls["article-2"] == 1

    retried = restarted.run_ready(now=_at(16), owner="worker-b", max_tasks=3)
    assert retried.succeeded == 1
    assert retried.failed == 0
    assert calls == Counter({"article-2": 2, "article-1": 1, "article-3": 1})
    assert restarted.flush_build(now=_at(18), owner="builder-b")
    assert build.translated_counts == [2, 3]

    assert restarted.flush_delivery(edition_date="2026-07-28", now=_at(19))
    assert len(smtp_calls) == 1
    assert not restarted.flush_delivery(edition_date="2026-07-28", now=_at(20))

    final = db.connect(database)
    try:
        state = db.automation_edition(final, "2026-07-28")
        tasks = db.list_translation_tasks(final, "2026-07-28")
    finally:
        final.close()
    assert state is not None and state.status == "delivered"
    assert all(task.status == "succeeded" and task.build_status == "online" for task in tasks)


def test_delivery_callback_exception_releases_claim_for_retry(tmp_path):
    database = tmp_path / "data" / "news.db"
    build = BuildHarness(database, tmp_path / "site")
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), Counter()),
        cache_dir=tmp_path / "cache",
        build_callback=build,
        delivery_callback=lambda date, key: (_ for _ in ()).throw(
            RuntimeError("fixture delivery failure")
        ),
    )
    runner.seed_edition(
        DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at()
    )
    assert runner.run_ready(now=_at(1), owner="worker-a").succeeded == 1
    assert runner.flush_build(now=_at(2), owner="builder-a", force=True)

    assert not runner.flush_delivery(edition_date="2026-07-28", now=_at(3))
    conn = db.connect(database)
    try:
        failed = db.automation_edition(conn, "2026-07-28")
    finally:
        conn.close()
    assert failed is not None
    assert failed.status == "complete"
    assert failed.delivery_key is None
    assert failed.last_error_code == "DELIVERY_FAILED"

    calls: list[tuple[str, str]] = []
    restarted = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), Counter()),
        cache_dir=tmp_path / "cache",
        build_callback=build,
        delivery_callback=lambda date, key: calls.append((date, key)) or True,
    )
    assert restarted.flush_delivery(edition_date="2026-07-28", now=_at(4))
    assert len(calls) == 1


def test_delivery_targets_current_edition_and_expires_older_failure(tmp_path):
    database = tmp_path / "data" / "news.db"
    build = BuildHarness(database, tmp_path / "site")
    calls = Counter()
    deliveries: list[str] = []
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), calls),
        cache_dir=tmp_path / "cache",
        build_callback=build,
        delivery_callback=lambda date, key: deliveries.append(date) or True,
    )

    for offset, edition_date in enumerate(("2026-07-28", "2026-07-29"), start=1):
        runner.seed_edition(
            DailyEdition(date=edition_date, articles=[_article(offset)]),
            now=_at(offset),
        )
        assert runner.run_ready(
            now=_at(offset + 2), owner=f"worker-{offset}"
        ).succeeded == 1
        assert runner.flush_build(
            now=_at(offset + 4), owner=f"builder-{offset}", force=True
        )

    assert runner.flush_delivery(edition_date="2026-07-29", now=_at(10))
    assert deliveries == ["2026-07-29"]

    conn = db.connect(database)
    try:
        older = db.automation_edition(conn, "2026-07-28")
        current = db.automation_edition(conn, "2026-07-29")
        unfinished = db.unfinished_automation_edition_dates(conn)
    finally:
        conn.close()
    assert older is not None
    assert older.status == "complete"
    assert older.last_error_code == "DELIVERY_EXPIRED"
    assert older.delivery_finished_at == _at(10).isoformat()
    assert current is not None and current.status == "delivered"
    assert unfinished == []


def test_provider_circuit_uses_one_real_task_for_automatic_half_open(tmp_path):
    database = tmp_path / "data" / "news.db"
    calls = Counter()
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator({f"article-{number}" for number in range(1, 7)}, calls),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
    )
    edition = DailyEdition(
        date="2026-07-28", articles=[_article(number) for number in range(1, 7)]
    )
    runner.seed_edition(edition, now=_at())

    first = runner.run_ready(now=_at(1), owner="worker-a", max_tasks=6)
    assert first.failed == 5
    assert first.blocked == 1
    assert sum(calls.values()) == 5
    conn = db.connect(database)
    try:
        circuit = db.get_provider_circuit(conn, "provider-default")
    finally:
        conn.close()
    assert circuit is not None and circuit.state == "open"
    assert circuit.next_probe_at == _at(61).isoformat()

    probe = runner.run_ready(now=_at(61), owner="worker-b", max_tasks=1)
    assert probe.claimed == 1
    assert probe.succeeded == 1
    assert probe.probes == 1
    assert sum(calls.values()) == 6
    conn = db.connect(database)
    try:
        recovered = db.get_provider_circuit(conn, "provider-default")
    finally:
        conn.close()
    assert recovered is not None
    assert recovered.state == "closed"
    assert recovered.recovery_mode


def test_retry_delay_uses_request_completion_time(tmp_path):
    database = tmp_path / "data" / "news.db"
    current = [_at(1)]

    class SlowFailure:
        label = "fake@automation"
        model = "fake"
        cache_identity = "fake-completion-clock"

        def translate(self, article):
            current[0] = _at(11)
            raise TranslationError("redacted", category="network")

    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=SlowFailure(),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
        clock=lambda: current[0],
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())

    result = runner.run_ready(now=_at(1), owner="worker-a")

    assert result.failed == 1
    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
    finally:
        conn.close()
    assert task.started_at == _at(1).isoformat()
    assert task.failed_at == _at(11).isoformat()
    assert task.next_retry_at == _at(26).isoformat()


def test_schema_failure_is_terminal_for_automation_and_records_schema_stage(tmp_path):
    database = tmp_path / "data" / "news.db"

    class SchemaFailureTranslator:
        label = "fake@schema"
        model = "fake"
        cache_identity = "fake-schema-failure"

        def translate(self, article):
            raise InvalidTranslation("redacted schema mismatch")

    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=SchemaFailureTranslator(),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())

    result = runner.run_ready(now=_at(1), owner="worker-a")

    assert result.failed == 1
    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
    finally:
        conn.close()
    assert task.status == "failed"
    assert task.error_code == "SCHEMA_VALIDATION_FAILED"
    assert task.failure_stage == "schema_validation"
    assert not task.auto_retry
    assert task.next_retry_at is None

    later = runner.run_ready(now=_at(301), owner="worker-b")
    assert later.claimed == 0


def test_startup_normalizes_legacy_schema_retry_to_manual_failure(tmp_path):
    database = tmp_path / "data" / "news.db"
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), Counter()),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())

    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
        assert db.claim_translation_task(
            conn,
            task.task_id,
            owner="legacy-worker",
            now=_at(1).isoformat(),
            lease_seconds=30,
        )
        legacy = db.finish_translation_task_failure(
            conn,
            task.task_id,
            owner="legacy-worker",
            now=_at(2).isoformat(),
            error_code="SCHEMA_VALIDATION_FAILED",
            error_category="schema",
            failure_stage="waiting_model",
            diagnostic_id="legacy-schema",
            auto_retry=True,
        )
        assert legacy.status == "retry_wait"
    finally:
        conn.close()

    conn = db.connect(database)
    try:
        assert db.recover_interrupted_translation_tasks(
            conn, now=_at(3).isoformat(), process_terminated=True
        ) == 0
        normalized = db.translation_task(conn, task.task_id)
    finally:
        conn.close()
    assert normalized is not None
    assert normalized.status == "failed"
    assert normalized.failure_stage == "schema_validation"
    assert normalized.next_retry_at is None
    assert not normalized.auto_retry


def test_startup_preserves_queued_manual_schema_retry(tmp_path):
    database = tmp_path / "data" / "news.db"
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), Counter()),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())

    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
        assert db.claim_translation_task(
            conn,
            task.task_id,
            owner="worker-a",
            now=_at(1).isoformat(),
            lease_seconds=30,
        )
        db.finish_translation_task_failure(
            conn,
            task.task_id,
            owner="worker-a",
            now=_at(2).isoformat(),
            error_code="SCHEMA_VALIDATION_FAILED",
            error_category="schema",
            failure_stage="schema_validation",
            diagnostic_id="schema-failed",
            auto_retry=False,
        )
        queued = db.queue_translation_task_retry(
            conn, task.task_id, now=_at(3).isoformat(), actor="admin"
        )
        assert queued.status == "retry_wait"
        assert queued.manual_action_id

        assert db.recover_interrupted_translation_tasks(
            conn, now=_at(4).isoformat(), process_terminated=True
        ) == 0
        preserved = db.translation_task(conn, task.task_id)
        assert preserved is not None
        assert preserved.status == "retry_wait"
        assert preserved.auto_retry
        assert preserved.next_retry_at == _at(3).isoformat()
        action = conn.execute(
            "SELECT status FROM translation_admin_actions WHERE action_id = ?",
            (preserved.manual_action_id,),
        ).fetchone()
        assert action["status"] == "requested"
    finally:
        conn.close()


def test_startup_repairs_manual_schema_retry_already_normalized_by_old_worker(tmp_path):
    database = tmp_path / "data" / "news.db"
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), Counter()),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())

    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
        assert db.claim_translation_task(
            conn,
            task.task_id,
            owner="worker-a",
            now=_at(1).isoformat(),
            lease_seconds=30,
        )
        db.finish_translation_task_failure(
            conn,
            task.task_id,
            owner="worker-a",
            now=_at(2).isoformat(),
            error_code="SCHEMA_VALIDATION_FAILED",
            error_category="schema",
            failure_stage="schema_validation",
            diagnostic_id="schema-failed",
            auto_retry=False,
        )
        queued = db.queue_translation_task_retry(
            conn, task.task_id, now=_at(3).isoformat(), actor="admin"
        )
        conn.execute(
            "UPDATE translation_tasks SET status = 'failed', auto_retry = 0,"
            " next_retry_at = NULL WHERE task_id = ?",
            (task.task_id,),
        )
        conn.commit()

        assert db.recover_interrupted_translation_tasks(
            conn, now=_at(4).isoformat(), process_terminated=True
        ) == 0
        repaired = db.translation_task(conn, task.task_id)
        assert repaired is not None
        assert repaired.status == "retry_wait"
        assert repaired.auto_retry
        assert repaired.next_retry_at == _at(3).isoformat()
        assert repaired.manual_action_id == queued.manual_action_id

        result = runner.run_ready(now=_at(5), owner="worker-b")
        assert result.succeeded == 1
        completed = db.translation_task(conn, task.task_id)
        assert completed is not None
        assert completed.status == "succeeded"
        action = conn.execute(
            "SELECT status, result_code FROM translation_admin_actions WHERE action_id = ?",
            (queued.manual_action_id,),
        ).fetchone()
        assert tuple(action) == ("completed", "SUCCEEDED")
    finally:
        conn.close()


def test_missing_task_article_releases_claim_as_visible_failure(tmp_path):
    database = tmp_path / "data" / "news.db"
    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=FakeTranslator(set(), Counter()),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())
    conn = db.connect(database)
    try:
        conn.execute("DELETE FROM articles WHERE date = ?", ("2026-07-28",))
        conn.commit()
    finally:
        conn.close()

    result = runner.run_ready(now=_at(1), owner="worker-a")

    assert result.claimed == 1
    assert result.failed == 1
    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
    finally:
        conn.close()
    assert task.status == "failed"
    assert task.error_code == "TASK_DATA_MISSING"
    assert task.failure_stage == "saving_translation"
    assert not task.auto_retry
    assert task.lease_owner is None
    assert task.lease_expires_at is None


def test_runner_confirms_admin_cancel_only_after_provider_request_stops(tmp_path):
    database = tmp_path / "data" / "news.db"
    current = [_at(1)]

    class CancelledTranslator:
        label = "fake@automation"
        model = "fake"
        cache_identity = "fake-cancelled-request"

        def translate_with_cancel(self, article, *, cancel_requested):
            conn = db.connect(database)
            try:
                running = db.list_translation_tasks(conn, "2026-07-28", status="running")[0]
                db.request_translation_task_cancel(
                    conn, running.task_id, now=_at(5).isoformat(), actor="admin"
                )
            finally:
                conn.close()
            assert cancel_requested() is True
            current[0] = _at(6)
            raise TranslationError(
                "redacted",
                category="request_cancelled",
                termination_confirmed=True,
            )

    runner = TranslationAutomationRunner(
        database=database,
        provider_id="provider-default",
        translator=CancelledTranslator(),
        cache_dir=tmp_path / "cache",
        build_callback=lambda date: date,
        delivery_callback=lambda date, key: True,
        clock=lambda: current[0],
    )
    runner.seed_edition(DailyEdition(date="2026-07-28", articles=[_article(1)]), now=_at())

    result = runner.run_ready(now=_at(1), owner="worker-a")

    assert result.failed == 1
    conn = db.connect(database)
    try:
        task = db.list_translation_tasks(conn, "2026-07-28")[0]
    finally:
        conn.close()
    assert task.status == "retry_wait"
    assert task.error_code == "REQUEST_CANCELLED"
    assert task.failed_at == _at(6).isoformat()
    assert task.next_retry_at == _at(21).isoformat()
    assert task.cancel_requested_at is None
