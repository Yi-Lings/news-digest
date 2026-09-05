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
    # 站点样板句必须被过滤
    assert "terms & conditions" not in joined.lower()
    assert "sign up for our morning newsletter" not in joined.lower()


def test_video_placeholder_page_returns_none():
    placeholder = (
        "One of your browser extensions seems to be blocking the video player from loading."
    )
    html = "<html><body><article><h1>Clip</h1>" + f"<p>{placeholder}</p>" * 3 + (
        "</article></body></html>"
    )
    assert extract_body(html, "https://www.france24.com/en/demo") is None


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


def test_normal_short_paragraphs_survive_cleaning(monkeypatch):
    from news_digest.extractors import body

    monkeypatch.setattr(
        body.trafilatura, "extract", lambda *a, **kw: "It stopped.\nNobody left.\nShare this",
    )
    monkeypatch.setattr(body.trafilatura, "extract_metadata", lambda *a, **kw: None)
    result = extract_body("fixture", "https://example.test/story")
    assert result is not None
    assert result.paragraphs == ["It stopped.", "Nobody left."]
