"""Explicit loopback-only automation demo; never calls a provider or SMTP."""

import datetime as dt
import json
import threading
from collections import Counter
from pathlib import Path

from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.storage import db
from news_digest.translation.automation import (
    TranslationAutomationRunner,
    fail_translation_work,
)
from news_digest.translation.client import TranslationError
from news_digest.translation.schema import InvalidTranslation


def build_demo_edition() -> DailyEdition:
    """Return fixed local-only tasks for Admin automation acceptance."""
    articles = [
        Article(
            slug=f"automation-demo-{number}",
            source="Local fixture",
            title_en=title,
            summary_en="Local fixture used only for the automation status panel.",
            author="Fixture",
            published_at="2026-07-28T00:00:00+00:00",
            url=f"https://example.invalid/automation-demo-{number}",
            reading_minutes=1,
            paragraphs=[Paragraph(en="This local fixture never leaves the computer.")],
        )
        for number, title in enumerate(
            (
                "Schema validation failed and is waiting for a retry",
                "Translation request currently running",
                "Provider connection failed before the circuit opened",
                "Translated article already online",
            ),
            start=1,
        )
    ]
    return DailyEdition(date="2026-07-28", articles=articles)


class _DemoTranslator:
    label = "local-demo@phase8"
    model = "local-demo"
    cache_identity = "phase8-local-demo-v1"

    def __init__(self) -> None:
        self.calls = Counter()

    def translate(self, article: Article) -> str:
        self.calls[article.slug] += 1
        return json.dumps(
            {
                "title_zh": f"本地模拟译文 {article.slug}",
                "summary_zh": "这是一条不访问外部服务的本地模拟结果。",
                "paragraphs_zh": ["本地模拟段落。"] * len(article.paragraphs),
                "vocabulary": [
                    {
                        "word": "automation",
                        "phonetic": "/ˌɔːtəˈmeɪʃən/",
                        "meaning_zh": "自动化",
                        "example_en": "Automation processes one persistent task.",
                    },
                    {
                        "word": "retry",
                        "phonetic": "/ˌriːˈtraɪ/",
                        "meaning_zh": "重试",
                        "example_en": "The worker retries only the failed task.",
                    },
                    {
                        "word": "persist",
                        "phonetic": "/pərˈsɪst/",
                        "meaning_zh": "持久保存",
                        "example_en": "Task state persists across a restart.",
                    },
                ],
                "collocations": [
                    {
                        "phrase": "retry a task",
                        "meaning_zh": "重试任务",
                        "example_en": "An operator can retry a task.",
                    }
                ],
                "sentence_notes": [
                    {
                        "sentence_en": "Task state persists across a restart.",
                        "translation_zh": "任务状态会跨重启保留。",
                        "analysis_zh": "主谓结构，across a restart 作状语。",
                    }
                ],
            },
            ensure_ascii=False,
        )


class TranslationAutomationDemo:
    def __init__(self, database: Path, edition: DailyEdition, cache_dir: Path) -> None:
        self.database = database
        self.edition = edition
        self._process_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending = False
        self._worker_running = False
        self._timer: threading.Timer | None = None
        self.runner = TranslationAutomationRunner(
            database=database,
            provider_id="local-demo",
            translator=_DemoTranslator(),
            cache_dir=cache_dir,
            build_callback=lambda edition_date: edition_date,
            delivery_callback=lambda edition_date, delivery_key: True,
        )
        self._seed()

    @staticmethod
    def _iso(value: dt.datetime) -> str:
        return value.astimezone(dt.UTC).isoformat()

    def _seed(self) -> None:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        conn = db.connect(self.database)
        try:
            if db.latest_automation_edition(conn) is not None:
                return
        finally:
            conn.close()
        self.runner.seed_edition(self.edition, now=now)

        conn = db.connect(self.database)
        try:
            tasks_by_article = {
                task.article_id: task
                for task in db.list_translation_tasks(conn, self.edition.date)
            }
            tasks = [
                tasks_by_article[article.url]
                for article in self.edition.articles
                if article.url in tasks_by_article
            ]
            if not tasks:
                return
            first = tasks[0]
            db.claim_translation_task(
                conn,
                first.task_id,
                owner="demo-failed",
                now=self._iso(now),
                lease_seconds=120,
            )
            fail_translation_work(
                conn,
                first.task_id,
                owner="demo-failed",
                now=self._iso(now + dt.timedelta(seconds=1)),
                error=InvalidTranslation("redacted demo schema error"),
                stage="schema_validation",
            )
            if len(tasks) > 1:
                db.claim_translation_task(
                    conn,
                    tasks[1].task_id,
                    owner="demo-running",
                    now=self._iso(now),
                    lease_seconds=3600,
                )
            if len(tasks) > 2:
                third = tasks[2]
                db.claim_translation_task(
                    conn,
                    third.task_id,
                    owner="demo-provider-failed",
                    now=self._iso(now),
                    lease_seconds=120,
                )
                fail_translation_work(
                    conn,
                    third.task_id,
                    owner="demo-provider-failed",
                    now=self._iso(now + dt.timedelta(seconds=2)),
                    error=TranslationError(
                        "redacted demo network error", category="network"
                    ),
                    stage="connect_provider",
                )
            if len(tasks) > 3:
                fourth = tasks[3]
                db.claim_translation_task(
                    conn,
                    fourth.task_id,
                    owner="demo-online",
                    now=self._iso(now),
                    lease_seconds=120,
                )
                db.finish_translation_task_success(
                    conn,
                    fourth.task_id,
                    owner="demo-online",
                    now=self._iso(now + dt.timedelta(seconds=3)),
                )
                db.mark_translation_ready_for_build(
                    conn,
                    fourth.task_id,
                    now=self._iso(now + dt.timedelta(seconds=3)),
                    debounce_seconds=0,
                )
                db.claim_automation_build(
                    conn,
                    self.edition.date,
                    owner="demo-builder",
                    now=self._iso(now + dt.timedelta(seconds=3)),
                    lease_seconds=120,
                    force=True,
                )
                db.finish_automation_build(
                    conn,
                    self.edition.date,
                    owner="demo-builder",
                    now=self._iso(now + dt.timedelta(seconds=4)),
                    succeeded=True,
                )
            circuit = db.get_provider_circuit(conn, "local-demo")
            failures = circuit.consecutive_failures if circuit else 0
            for offset in range(failures, 5):
                db.record_provider_outcome(
                    conn,
                    "local-demo",
                    outcome="provider_failure",
                    now=self._iso(now + dt.timedelta(seconds=10 + offset)),
                )
        finally:
            conn.close()

    def wakeup(self) -> None:
        with self._state_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending = True
            if self._worker_running:
                return
            self._worker_running = True
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        restart = False
        try:
            while True:
                with self._state_lock:
                    if not self._pending:
                        return
                    self._pending = False
                self._process_one()
        finally:
            with self._state_lock:
                self._worker_running = False
                restart = self._pending
            if restart:
                self.wakeup()

    def _schedule_next_wakeup(self) -> None:
        now = dt.datetime.now(dt.UTC)
        conn = db.connect(self.database)
        try:
            tasks = db.list_translation_tasks(conn, self.edition.date)
            circuit = db.get_provider_circuit(conn, self.runner.provider_id)
        finally:
            conn.close()

        due: list[dt.datetime] = []
        if any(
            task.cancel_requested_at is not None
            or task.manual_retry_requested_at is not None
            or task.manual_probe_requested_at is not None
            for task in tasks
        ):
            due.append(now)
        elif circuit is None or circuit.state == "closed":
            for task in tasks:
                if task.status == "pending":
                    due.append(now)
                elif task.status == "retry_wait" and task.next_retry_at:
                    due.append(dt.datetime.fromisoformat(task.next_retry_at))
        if not due:
            return

        delay = max(0.0, (min(due) - now).total_seconds())
        timer = threading.Timer(delay, self._timer_wakeup)
        timer.daemon = True
        with self._state_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = timer
        timer.start()

    def _timer_wakeup(self) -> None:
        with self._state_lock:
            self._timer = None
        self.wakeup()

    def _process_one(self) -> None:
        with self._process_lock:
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            conn = db.connect(self.database)
            try:
                running = db.list_translation_tasks(
                    conn, self.edition.date, status="running"
                )
                for task in running:
                    if task.cancel_requested_at is not None and task.lease_owner:
                        db.confirm_translation_task_cancelled(
                            conn,
                            task.task_id,
                            owner=task.lease_owner,
                            now=self._iso(now),
                            request_terminated=True,
                        )
                tasks = db.list_translation_tasks(conn, self.edition.date)
                circuit = db.get_provider_circuit(conn, self.runner.provider_id)
                run_ready = (
                    circuit is None
                    or circuit.state != "open"
                    or any(task.manual_probe_requested_at is not None for task in tasks)
                )
            finally:
                conn.close()
            if run_ready:
                self.runner.run_ready(now=now, owner="demo-worker", max_tasks=1)
            self.runner.flush_build(now=now, owner="demo-builder", force=True)
            self.runner.flush_delivery(edition_date=self.edition.date, now=now)
            self._schedule_next_wakeup()
