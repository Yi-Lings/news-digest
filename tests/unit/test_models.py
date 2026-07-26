"""Demo fixtures must load completely into the data models."""

import json
from pathlib import Path

from news_digest.models import edition_from_dict

FIXTURES = Path(__file__).parent.parent / "fixtures" / "demo"


def test_demo_fixtures_load_completely():
    paths = sorted(FIXTURES.glob("*.json"))
    assert paths, "缺少演示数据"
    for path in paths:
        edition = edition_from_dict(json.loads(path.read_text(encoding="utf-8")))
        assert edition.date == path.stem
        assert edition.articles
        for article in edition.articles:
            assert article.paragraphs, article.slug
            for paragraph in article.paragraphs:
                assert paragraph.en and paragraph.zh


def test_sources_are_distinct_and_in_first_appearance_order():
    data = json.loads((FIXTURES / "2026-07-26.json").read_text(encoding="utf-8"))
    edition = edition_from_dict(data)
    sources = edition.sources
    assert len(sources) == len(set(sources))
    assert sources[0] == edition.articles[0].source
