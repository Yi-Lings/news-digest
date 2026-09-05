import datetime as dt
from pathlib import Path

import pytest

from news_digest.extractors.body import EXTRACTOR_VERSION, extract_body
from news_digest.models import BriefItem, DailyEdition, edition_from_dict, edition_to_dict
from news_digest.sources.feeds import parse_feed
from news_digest.sources.registry import SOURCES

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_structured_text_retains_quotes_lists_and_table_cells():
    diagnostic = {}
    body = extract_body(
        (FIXTURES / "pages/structured-article.html").read_text(),
        "https://example.com/science",
        diagnostics=diagnostic,
    )
    assert body is not None
    text = "\n".join(body.paragraphs)
    for expected in ('"Not yet."', "calibration", "North Pier", "21.5", "20.8", "did not claim"):
        assert expected in text
    assert diagnostic["version"] == EXTRACTOR_VERSION
    assert diagnostic["completeness"] == "unverified"
    assert diagnostic["kept_paragraphs"] == len(body.paragraphs)


@pytest.mark.parametrize("key", ["bbc", "guardian", "dw", "france24", "aljazeera", "npr", "nyt"])
def test_fixed_source_samples_record_parse_accounting(key):
    source = next(source for source in SOURCES if source.key == key)
    diagnostic = {}
    candidates = parse_feed(
        (FIXTURES / f"feeds/{key}.xml").read_bytes(), source, diagnostics=diagnostic
    )
    assert diagnostic["raw"] == diagnostic["parsed"] + diagnostic["rejected"]
    assert diagnostic["parsed"] == len(candidates) > 0
    for candidate in candidates:
        assert dt.datetime.fromisoformat(candidate.published_at_utc).tzinfo is not None


def test_legacy_brief_serialization_and_new_timestamps_round_trip():
    old = {
        "date": "2026-09-05",
        "articles": [],
        "briefs": [
            {"title_en": "Brief", "title_zh": "", "source": "Example", "url": "https://example.com"}
        ],
    }
    assert edition_to_dict(edition_from_dict(old)) == old
    assert edition_from_dict(old).briefs[0].published_at is None
    edition = DailyEdition(
        "2026-09-05",
        briefs=[
            BriefItem(
                "Brief",
                "Example",
                "https://example.com",
                published_at="2026-09-05T00:00:00+00:00",
                source_key="example",
                selection_reason="brief_source",
            )
        ],
    )
    assert edition_from_dict(edition_to_dict(edition)) == edition
