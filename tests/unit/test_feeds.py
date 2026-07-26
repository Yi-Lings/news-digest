"""Feed parsing against per-source fixtures (real samples for BBC/DW)."""

from pathlib import Path

from news_digest.sources.feeds import canonicalize_url, is_article_url, parse_feed
from news_digest.sources.registry import SOURCES

FEEDS = Path(__file__).parent.parent / "fixtures" / "feeds"
_BY_KEY = {source.key: source for source in SOURCES}


def _parse(key: str, filename: str):
    data = (FEEDS / filename).read_bytes()
    return parse_feed(data, _BY_KEY[key])


def test_canonicalize_strips_tracking_params():
    assert (
        canonicalize_url("https://www.bbc.co.uk/news/articles/x?at_medium=RSS&at_campaign=rss")
        == "https://www.bbc.co.uk/news/articles/x"
    )
    assert (
        canonicalize_url("https://www.dw.com/en/story/a-1?maca=en-rss-en-all-1573-rdf")
        == "https://www.dw.com/en/story/a-1"
    )
    assert (
        canonicalize_url("https://example.com/p?id=7&utm_source=x#frag")
        == "https://example.com/p?id=7"
    )


def test_non_article_urls_excluded():
    assert not is_article_url("https://www.bbc.co.uk/news/videos/c74gzknyyz2o")
    assert not is_article_url("https://www.dw.com/en/middle-east-live/live-78117732")
    assert not is_article_url("https://www.theguardian.com/world/live/2026/jul/26/blog")
    assert is_article_url("https://www.bbc.co.uk/news/articles/cevmdxz4872o")
    assert is_article_url("https://www.dw.com/en/olive-harvest-in-spain/a-78101422")


def test_bbc_real_sample():
    candidates = _parse("bbc", "bbc.xml")
    # fixture 含 4 条，其中 1 条 /news/videos/ 被排除
    assert len(candidates) == 3
    assert all("/videos/" not in candidate.url for candidate in candidates)
    first = candidates[0]
    assert first.source_name == "BBC News"
    assert first.title.startswith("What we know so far")
    assert first.url == "https://www.bbc.co.uk/news/articles/cevmdxz4872o"
    assert first.published_at_utc == "2026-07-26T09:18:33+00:00"
    assert first.image_url.startswith("https://ichef.bbci.co.uk/")
    assert first.summary.startswith("A police manhunt")


def test_dw_real_sample_rdf():
    candidates = _parse("dw", "dw.xml")
    assert len(candidates) == 2
    first = candidates[0]
    assert "maca" not in first.url
    assert first.published_at_utc == "2026-07-26T10:11:00+00:00"
    assert first.image_url == ""


def test_remaining_sources_parse():
    cases = {
        "guardian": "guardian.xml",
        "npr": "npr.xml",
        "aljazeera": "aljazeera.xml",
        "france24": "france24.xml",
        "nyt": "nyt.xml",
    }
    for key, filename in cases.items():
        candidates = _parse(key, filename)
        assert candidates, key
        for candidate in candidates:
            assert candidate.title
            assert candidate.url.startswith("https://")
            assert candidate.published_at_utc
            assert "<" not in candidate.summary, "摘要必须是纯文本"


def test_guardian_picks_largest_media_content():
    candidates = _parse("guardian", "guardian.xml")
    assert candidates[0].image_url.endswith("demo460.jpg")


def test_france24_enclosure_image():
    candidates = _parse("france24", "france24.xml")
    assert candidates[0].image_url.endswith("urban-mining.jpg")
