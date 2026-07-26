"""翻译 schema 校验、缓存与断点续跑（全部离线）。"""

import json
from pathlib import Path

import pytest

from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.translation.schema import (
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

    @property
    def label(self) -> str:
        return "fake-model@p1"

    @property
    def model(self) -> str:
        return "fake-model"

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


def test_already_translated_articles_skipped(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_article()])
    translator = FakeTranslator(_valid_raw())
    updated, _ = translate_edition(edition, translator, tmp_path)

    updated2, report2 = translate_edition(updated, translator, tmp_path)
    assert report2.already_done == 1 and report2.api_calls == 0


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
