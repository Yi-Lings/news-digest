"""翻译后的版次渲染：批注体译文、学习区与 AI 标识。"""

import json
from pathlib import Path

from news_digest.config import BuildConfig
from news_digest.models import Article, DailyEdition, Paragraph
from news_digest.pipeline import build_editions
from news_digest.translation.schema import apply_translation, parse_translation

FIXTURE = Path(__file__).parent.parent / "fixtures" / "translations" / "valid-response.json"


def _translated_article() -> Article:
    texts = [
        "A police manhunt is under way in the German capital after a car was driven into a crowd.",
        "Investigators said the vehicle accelerated through a barrier that had been moved earlier.",
        "Officials appealed for witnesses and cautioned against sharing unverified claims.",
    ]
    article = Article(
        slug="berlin-pride",
        source="BBC News",
        title_en="What we know so far about the Berlin Pride ramming attack",
        summary_en="A police manhunt is underway after a suspect rammed a car into a crowd.",
        author="Demo Writer",
        published_at="2026-07-26T09:18:33+00:00",
        url="https://www.bbc.co.uk/news/articles/cevmdxz4872o",
        reading_minutes=4,
        paragraphs=[Paragraph(en=text) for text in texts],
    )
    result = parse_translation(FIXTURE.read_text(encoding="utf-8"), paragraph_count=3)
    return apply_translation(article, result, "demo-model@p1")


def test_translated_page_renders_bilingual_and_labels(tmp_path):
    edition = DailyEdition(date="2026-07-26", articles=[_translated_article()])
    output_root = tmp_path / "site"
    build_editions([edition], BuildConfig(output_root=output_root, site_url="http://x"))

    page = (
        output_root / "current" / "issues" / "2026-07-26" / "berlin-pride.html"
    ).read_text(encoding="utf-8")
    assert "柏林骄傲游行冲撞事件" in page  # 中文标题
    assert "gloss-mark" in page  # 批注体译文
    assert "重点词汇" in page and "manhunt" in page
    assert "长难句解析" in page
    assert "由 AI 生成（demo-model@p1）" in page  # AI 内容标识
    assert "样张" not in page  # 真实构建无演示标识

    index = (output_root / "current" / "index.html").read_text(encoding="utf-8")
    assert "柏林骄傲游行冲撞事件" in index  # 首页双语标题


def test_translated_edition_roundtrips_through_json(tmp_path):
    from news_digest.models import edition_from_dict, edition_to_dict

    edition = DailyEdition(date="2026-07-26", articles=[_translated_article()])
    restored = edition_from_dict(json.loads(json.dumps(edition_to_dict(edition))))
    article = restored.articles[0]
    assert article.translated_by == "demo-model@p1"
    assert article.paragraphs[0].zh
    assert article.vocabulary[0].word == "manhunt"
