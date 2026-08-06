"""翻译 schema 校验、缓存与断点续跑（全部离线）。"""

import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from news_digest.config import TranslationConfig
from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.translation.client import ApiTranslator, TranslationError
from news_digest.translation.schema import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    InvalidTranslation,
    apply_translation,
    parse_translation,
)
from news_digest.translation.service import translate_edition

FIXTURE = Path(__file__).parent.parent / "fixtures" / "translations" / "valid-response.json"


def _article(slug: str = "berlin-pride", paragraphs: int = 3) -> Article:
    texts = [
        "A police manhunt is under way in the German capital after a car was driven into a crowd.",
        "Investigators said the vehicle accelerated through a barrier that had been moved earlier.",
        "Officials appealed for witnesses and cautioned against sharing unverified claims.",
    ]
    return Article(
        slug=slug,
        source="BBC News",
        title_en="What we know so far about the Berlin Pride ramming attack",
        summary_en="A police manhunt is underway after a suspect rammed a car into a crowd.",
        author="Demo Writer",
        published_at="2026-07-26T09:18:33+00:00",
        url="https://www.bbc.co.uk/news/articles/cevmdxz4872o",
        reading_minutes=4,
        paragraphs=[Paragraph(en=text) for text in texts[:paragraphs]],
    )


def _valid_raw() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class FakeTranslator:
    """返回固定响应并计数调用。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls = 0
        self._cache_identity = "fake-openai:https://api.example.com/v1:fake-model"

    @property
    def label(self) -> str:
        return "fake-model@p1"

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def cache_identity(self) -> str:
        return self._cache_identity

    @cache_identity.setter
    def cache_identity(self, value: str) -> None:
        self._cache_identity = value

    def translate(self, article: Article) -> str:
        self.calls += 1
        return self.raw


def test_parse_valid_fixture():
    result = parse_translation(_valid_raw(), paragraph_count=3)
    assert result.title_zh.startswith("柏林")
    assert len(result.paragraphs_zh) == 3
    assert result.vocabulary[0].word == "manhunt"
    assert result.sentence_notes[0].analysis_zh


def test_parse_accepts_code_fences():
    fenced = "```json\n" + _valid_raw() + "\n```"
    result = parse_translation(fenced, paragraph_count=3)
    assert result.summary_zh


def test_parse_accepts_single_fenced_object_with_provider_preamble():
    wrapped = "Here is the requested JSON:\n```json\n" + _valid_raw() + "\n```"
    result = parse_translation(wrapped, paragraph_count=3)
    assert result.title_zh.startswith("柏林")


def test_parse_rejects_ambiguous_fenced_objects():
    wrapped = "```json\n{}\n```\n```json\n" + _valid_raw() + "\n```"
    with pytest.raises(InvalidTranslation):
        parse_translation(wrapped, paragraph_count=3)


def test_formal_prompt_uses_versioned_explicit_json_contract():
    assert PROMPT_VERSION == "p4"
    assert '"paragraphs_zh": ["逐段中文译文"]' in SYSTEM_PROMPT
    assert '"phonetic": "非空字符串"' in SYSTEM_PROMPT


def test_formal_prompt_requires_complete_non_summary_translation():
    assert "不是摘要、改写或评论" in SYSTEM_PROMPT
    assert "不得合并段落、跳过句子、删减事实" in SYSTEM_PROMPT
    assert "人物、机构、地点、时间、数字、比例、金额" in SYSTEM_PROMPT
    assert "summary_zh 只是补充摘要" in SYSTEM_PROMPT
    assert "逐段对照输入正文自检" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("title_zh"),
        lambda d: d.__setitem__("title_zh", "  "),
        lambda d: d.__setitem__("paragraphs_zh", d["paragraphs_zh"][:2]),
        lambda d: d["paragraphs_zh"].__setitem__(1, ""),
        lambda d: d["vocabulary"][0].pop("phonetic"),
        lambda d: d.__setitem__("sentence_notes", "not-a-list"),
    ],
)
def test_parse_rejects_invalid(mutate):
    data = json.loads(_valid_raw())
    mutate(data)
    with pytest.raises(InvalidTranslation):
        parse_translation(json.dumps(data, ensure_ascii=False), paragraph_count=3)


def test_parse_rejects_title_over_40_characters():
    data = json.loads(_valid_raw())
    data["title_zh"] = "标" * 41

    with pytest.raises(InvalidTranslation, match="title_zh 不得超过 40 字"):
        parse_translation(json.dumps(data, ensure_ascii=False), paragraph_count=3)


@pytest.mark.parametrize(
    ("key", "size"),
    [
        ("vocabulary", 0),
        ("vocabulary", 7),
        ("collocations", 0),
        ("collocations", 4),
        ("sentence_notes", 0),
        ("sentence_notes", 3),
    ],
)
def test_parse_rejects_learning_item_count_outside_prompt_contract(key, size):
    data = json.loads(_valid_raw())
    data[key] = [data[key][0] for _ in range(size)]

    with pytest.raises(InvalidTranslation, match=key):
        parse_translation(json.dumps(data, ensure_ascii=False), paragraph_count=3)


def test_parse_rejects_non_json():
    with pytest.raises(InvalidTranslation):
        parse_translation("抱歉，我无法完成该请求。", paragraph_count=3)


def test_apply_translation_fills_fields():
    article = _article()
    result = parse_translation(_valid_raw(), paragraph_count=3)
    translated = apply_translation(article, result, "fake-model@p1")
    assert translated.title_zh
    assert translated.paragraphs[0].zh
    assert translated.paragraphs[0].en == article.paragraphs[0].en
    assert translated.translated_by == "fake-model@p1"
    assert translated.vocabulary and translated.collocations and translated.sentence_notes


def test_cache_hit_avoids_second_api_call(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    translator = FakeTranslator(_valid_raw())
    events: list[str] = []

    updated, report = translate_edition(
        edition, translator, tmp_path, on_progress=events.append
    )
    assert report.succeeded == 1 and report.api_calls == 1 and translator.calls == 1
    assert updated.articles[0].translated_by == "fake-model@p1"
    assert any(event.startswith("→") for event in events)
    assert any(event.startswith("✓") for event in events)

    # 同一内容再翻一遍（模拟重新抓取后 translated_by 为空）：应命中缓存，不再调用 API
    fresh = DailyEdition(date="2026-07-26", articles=[_article()])
    updated2, report2 = translate_edition(fresh, translator, tmp_path)
    assert report2.cache_hits == 1 and report2.api_calls == 0 and translator.calls == 1
    assert updated2.articles[0].title_zh == updated.articles[0].title_zh


def test_summary_change_invalidates_translation_cache(tmp_path):
    translator = FakeTranslator(_valid_raw())
    first = DailyEdition(date="2026-07-26", articles=[_article()])
    _, first_report = translate_edition(first, translator, tmp_path)

    changed = DailyEdition(
        date="2026-07-26",
        articles=[replace(_article(), summary_en="A materially updated summary.")],
    )
    _, changed_report = translate_edition(changed, translator, tmp_path)

    assert first_report.api_calls == 1
    assert changed_report.cache_hits == 0 and changed_report.api_calls == 1
    assert translator.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_already_translated_articles_skipped(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    translator = FakeTranslator(_valid_raw())
    updated, _ = translate_edition(edition, translator, tmp_path)

    updated2, report2 = translate_edition(updated, translator, tmp_path)
    assert report2.already_done == 1 and report2.api_calls == 0


def test_translated_article_without_current_prompt_cache_is_retranslated(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    translator = FakeTranslator(_valid_raw())
    translated = replace(_article(), translated_by="old-model@p3")

    updated, report = translate_edition(
        DailyEdition(date=edition.date, articles=[translated]), translator, tmp_path
    )

    assert report.already_done == 0 and report.api_calls == 1
    assert updated.articles[0].translated_by == "fake-model@p1"


def test_invalid_response_not_cached_and_not_fatal(tmp_path):
    good = _article("good")
    bad = _article("bad")
    edition = DailyEdition(date="2026-07-26", articles=[bad, good])

    class MixedTranslator(FakeTranslator):
        def translate(self, article: Article) -> str:
            self.calls += 1
            if article.slug == "bad":
                return "not json at all"
            return self.raw

    translator = MixedTranslator(_valid_raw())
    updated, report = translate_edition(edition, translator, tmp_path)
    assert report.succeeded == 1 and report.failed == 1
    assert report.failures[0][0] == "bad"
    by_slug = {a.slug: a for a in updated.articles}
    assert not by_slug["bad"].translated_by
    assert by_slug["good"].translated_by
    # 非法响应不得写入缓存：目录中只有 good 的一个缓存文件
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_redo_forces_fresh_call_and_overwrites(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    first = FakeTranslator(_valid_raw())
    translated, _ = translate_edition(edition, first, tmp_path)
    assert translated.articles[0].translated_by == "fake-model@p1"

    second = FakeTranslator(_valid_raw())
    redone, report = translate_edition(
        translated, second, tmp_path, redo=frozenset({"berlin-pride"})
    )
    # 已翻译 + 缓存存在的情况下仍强制走 API
    assert second.calls == 1
    assert report.api_calls == 1 and report.already_done == 0
    assert redone.articles[0].translated_by == "fake-model@p1"


def test_cache_identity_isolates_provider_protocol_and_model(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    first = FakeTranslator(_valid_raw())
    _, first_report = translate_edition(edition, first, tmp_path)
    assert first_report.api_calls == 1

    second = FakeTranslator(_valid_raw())
    second.cache_identity = "fake-anthropic:https://api.example.com/v1:fake-model"
    _, second_report = translate_edition(edition, second, tmp_path)
    assert second_report.cache_hits == 0 and second_report.api_calls == 1

    third = FakeTranslator(_valid_raw())
    third.cache_identity = "fake-anthropic:https://api.example.com/v1:other-model"
    _, third_report = translate_edition(edition, third, tmp_path)
    assert third_report.cache_hits == 0 and third_report.api_calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 3


@pytest.mark.parametrize(
    ("api_type", "stream"),
    [
        ("openai_chat", True),
        ("openai_chat", False),
        ("anthropic_messages", True),
        ("anthropic_messages", False),
    ],
)
def test_real_adapter_formal_schema_round_trip_populates_and_reuses_cache(
    tmp_path, api_type, stream
):
    calls = 0
    raw = _valid_raw()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if api_type == "openai_chat":
            if stream:
                payload = json.dumps({"choices": [{"delta": {"content": raw}}]})
                return httpx.Response(200, content=f"data: {payload}\n\ndata: [DONE]\n\n")
            return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})
        if stream:
            payload = json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": raw},
                }
            )
            return httpx.Response(
                200,
                content=(
                    f"event: content_block_delta\ndata: {payload}\n\n"
                    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                ),
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": raw}]})

    translator = ApiTranslator(
        TranslationConfig(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="formal-model",
            timeout_seconds=10.0,
            max_tokens=8192,
            cache_dir=tmp_path,
            api_type=api_type,
            stream=stream,
        ),
        transport=httpx.MockTransport(handler),
    )
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    translated, first = translate_edition(edition, translator, tmp_path)
    cached, second = translate_edition(edition, translator, tmp_path)

    assert first.api_calls == 1 and first.succeeded == 1
    assert translated.articles[0].title_zh.startswith("柏林")
    assert second.api_calls == 0 and second.cache_hits == 1
    assert cached.articles[0].title_zh == translated.articles[0].title_zh
    assert calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_limit_controls_batch_size(tmp_path):
    import dataclasses

    # 标题各异 -> 内容哈希各异，避免互相命中缓存
    articles = [
        dataclasses.replace(_article(f"a{i}"), title_en=f"Story {i}: distinct headline")
        for i in range(3)
    ]
    edition = DailyEdition(date="2026-07-26", articles=articles)
    translator = FakeTranslator(_valid_raw())
    _, report = translate_edition(edition, translator, tmp_path, limit=2)
    assert report.succeeded == 2
    assert translator.calls == 2


@pytest.mark.parametrize(
    ("category", "expected_calls", "status"),
    [
        ("connection_timeout", 3, None),
        ("read_timeout", 3, None),
        ("rate_limit", 3, 429),
        ("provider", 3, 503),
        ("provider", 1, None),
        ("network", 1, None),
        ("authentication", 1, 401),
        ("endpoint", 1, 404),
        ("request", 1, 400),
        ("provider_permanent", 1, 501),
        ("response_format", 1, None),
    ],
)
def test_formal_translation_only_retries_recoverable_errors(
    tmp_path, monkeypatch, category, expected_calls, status
):
    monkeypatch.setattr("news_digest.translation.service.time.sleep", lambda seconds: None)

    class FailingTranslator(FakeTranslator):
        def translate(self, article: Article) -> str:
            self.calls += 1
            raise TranslationError("sanitized", category=category, status=status)

    translator = FailingTranslator(_valid_raw())
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
    )
    assert translator.calls == expected_calls
    assert report.api_calls == expected_calls
    assert report.failed == 1


def test_read_timeout_retry_budget_allows_three_bounded_attempts(tmp_path, monkeypatch):
    monotonic_values = iter([0.0, 30.0, 30.0, 60.25, 60.25])
    monkeypatch.setattr(
        "news_digest.translation.service.time.monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr("news_digest.translation.service.time.sleep", lambda seconds: None)

    class ReadTimeoutTranslator(FakeTranslator):
        def translate(self, article: Article) -> str:
            self.calls += 1
            raise TranslationError("sanitized", category="read_timeout")

    translator = ReadTimeoutTranslator(_valid_raw())
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
    )
    assert translator.calls == 3
    assert report.api_calls == 3 and report.failed == 1


@pytest.mark.parametrize("category", ["read_timeout", "total_timeout"])
def test_stream_started_failure_is_not_retried(tmp_path, category):
    class StartedTranslator(FakeTranslator):
        def translate(self, article: Article) -> str:
            self.calls += 1
            raise TranslationError(
                "sanitized",
                category=category,
                response_started=True,
            )

    translator = StartedTranslator(_valid_raw())
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
    )
    assert translator.calls == 1
    assert report.api_calls == 1


def test_invalid_schema_is_not_retried(tmp_path):
    translator = FakeTranslator("not json at all")
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
    )
    assert translator.calls == 1
    assert report.failed == 1


def test_retry_after_is_used_and_attempts_are_hard_capped(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monotonic_values = iter([0.0, 0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(
        "news_digest.translation.service.time.monotonic", lambda: next(monotonic_values)
    )
    monkeypatch.setattr("news_digest.translation.service.time.sleep", sleeps.append)

    class RateLimitedTranslator(FakeTranslator):
        def translate(self, article: Article) -> str:
            self.calls += 1
            raise TranslationError(
                "sanitized",
                category="rate_limit",
                status=429,
                retry_after=2.0,
            )

    translator = RateLimitedTranslator(_valid_raw())
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
        max_attempts=100,
        max_retry_elapsed_seconds=10.0,
    )
    assert translator.calls == 3
    assert report.api_calls == 3
    assert sleeps == [2.0, 2.0]


def test_retry_total_time_limit_stops_before_sleep(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("news_digest.translation.service.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("news_digest.translation.service.time.sleep", sleeps.append)

    class RateLimitedTranslator(FakeTranslator):
        def translate(self, article: Article) -> str:
            self.calls += 1
            raise TranslationError(
                "sanitized",
                category="rate_limit",
                status=429,
                retry_after=2.0,
            )

    translator = RateLimitedTranslator(_valid_raw())
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
        max_attempts=100,
        max_retry_elapsed_seconds=1.0,
    )
    assert translator.calls == 1
    assert report.api_calls == 1
    assert sleeps == []


def test_retry_deadline_waits_for_request_termination_confirmation(tmp_path, monkeypatch):
    raw = _valid_raw()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        time.sleep(0.3)
        return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})

    translator = ApiTranslator(
        TranslationConfig(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="formal-model",
            timeout_seconds=1.0,
            max_tokens=8192,
            cache_dir=tmp_path,
            api_type="openai_chat",
            stream=False,
        ),
        transport=httpx.MockTransport(handler),
    )
    started = time.monotonic()
    _, report = translate_edition(
        DailyEdition(date="2026-07-26", articles=[_article()]),
        translator,
        tmp_path,
        max_retry_elapsed_seconds=0.1,
    )
    elapsed = time.monotonic() - started

    assert calls == 2
    assert report.api_calls == 2 and report.failed == 1
    assert 0.28 <= elapsed < 0.5
