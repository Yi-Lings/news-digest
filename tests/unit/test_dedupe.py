"""Cross-source dedup behaviour."""

from news_digest.models import Candidate
from news_digest.selection.dedupe import dedupe


def _candidate(title: str, url: str, summary: str = "", source: str = "bbc") -> Candidate:
    return Candidate(
        source_key=source,
        source_name=source,
        kind="full",
        title=title,
        url=url,
        summary=summary or title,
        published_at_utc="2026-07-26T08:00:00+00:00",
    )


def test_exact_url_dupe_removed():
    kept = dedupe(
        [
            _candidate("A story", "https://example.com/a"),
            _candidate("A story again", "https://example.com/a"),
        ]
    )
    assert len(kept) == 1


def test_similar_titles_across_sources_removed_first_kept():
    kept = dedupe(
        [
            _candidate(
                "Wildfire evacuations expand across southern Europe",
                "https://example.com/1",
                source="bbc",
            ),
            _candidate(
                "Wildfire evacuations expand across southern  Europe ",
                "https://other.example.com/2",
                source="guardian",
            ),
        ]
    )
    assert len(kept) == 1
    assert kept[0].source_key == "bbc"


def test_distinct_stories_kept():
    kept = dedupe(
        [
            _candidate("Night trains stage a comeback in Europe", "https://example.com/t"),
            _candidate("Desert farms bet on fog harvesting", "https://example.com/f"),
            _candidate("Ocean atlas maps hidden seamounts", "https://example.com/o"),
        ]
    )
    assert len(kept) == 3
