"""Body extraction on realistic page fixtures."""

from pathlib import Path

from news_digest.extractors.body import extract_body, reading_minutes

PAGES = Path(__file__).parent.parent / "fixtures" / "pages"


def test_bbc_page_extracts_paragraphs_and_metadata():
    html = (PAGES / "bbc-article.html").read_text(encoding="utf-8")
    extracted = extract_body(html, "https://www.bbc.co.uk/news/articles/cevmdxz4872o")
    assert extracted is not None
    assert len(extracted.paragraphs) >= 3
    joined = " ".join(extracted.paragraphs)
    assert "police manhunt" in joined
    assert "SHOULD_NEVER_APPEAR_IN_OUTPUT" not in joined
    assert "Related" not in joined


def test_guardian_page_extracts():
    html = (PAGES / "guardian-article.html").read_text(encoding="utf-8")
    extracted = extract_body(html, "https://www.theguardian.com/world/2026/jul/26/demo")
    assert extracted is not None
    assert len(extracted.paragraphs) >= 3
    assert extracted.image_url.endswith("demo-lead-1200.jpg")


def test_garbage_returns_none():
    assert extract_body("<html><body><nav>x</nav></body></html>", "https://example.com") is None


def test_reading_minutes_floor():
    assert reading_minutes(["word " * 10]) == 1
    assert reading_minutes(["word " * 450]) == 2
