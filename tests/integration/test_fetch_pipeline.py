"""Offline end-to-end fetch: MockTransport feeds/pages, degradation, build."""

import datetime
import json
from pathlib import Path

import httpx
import pytest

from news_digest.config import BuildConfig, FetchConfig
from news_digest.pipeline import build_editions, fetch_daily, load_fetched_editions

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime.datetime(2026, 7, 26, 12, 0, tzinfo=datetime.UTC)

_FEED_FILES = {
    "feeds.bbci.co.uk": "bbc.xml",
    "www.theguardian.com": "guardian.xml",
    "feeds.npr.org": "npr.xml",
    "rss.dw.com": "dw.xml",
    "www.france24.com": "france24.xml",
    "rss.nytimes.com": "nyt.xml",
    # aljazeera 故意缺席：模拟来源整体失败
}

_PAGE_FILES = {
    "/news/articles/cevmdxz4872o": "bbc-article.html",
    "/world/2026/jul/26/wildfire-evacuations-southern-europe-heatwave": "guardian-article.html",
}


def _handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "www.aljazeera.com":
        raise httpx.ConnectError("simulated outage", request=request)
    if host in _FEED_FILES and (path.endswith((".xml", "/rss", "rss-en-all")) or "rss" in path):
        data = (FIXTURES / "feeds" / _FEED_FILES[host]).read_bytes()
        return httpx.Response(200, content=data)
    if path in _PAGE_FILES:
        data = (FIXTURES / "pages" / _PAGE_FILES[path]).read_bytes()
        return httpx.Response(200, content=data)
    return httpx.Response(404, content=b"not found")


@pytest.fixture(scope="module")
def fetched(tmp_path_factory, module_no_dns):
    data_dir = tmp_path_factory.mktemp("data")
    config = FetchConfig(
        proxy=None, window_hours=24, timezone="Asia/Shanghai", data_dir=data_dir
    )
    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    with client:
        edition, report = fetch_daily(config, client=client, now=NOW)
    return data_dir, edition, report


@pytest.fixture(scope="module")
def module_no_dns():
    import unittest.mock

    with unittest.mock.patch(
        "news_digest.sources.http.assert_public_host", lambda host: None
    ):
        yield


def test_edition_produced_with_articles_and_briefs(fetched):
    _, edition, report = fetched
    assert edition is not None
    assert edition.date == "2026-07-26"  # UTC 12:00 -> Asia/Shanghai 同日
    assert report.articles >= 6
    assert report.briefs == 2
    sources = {article.source for article in edition.articles}
    assert {"BBC News", "The Guardian", "NPR", "DW English", "France 24 English"} <= sources


def test_failed_source_degrades_not_fatal(fetched):
    _, edition, report = fetched
    assert any(status.startswith("失败") for status in report.per_source.values())
    assert "Al Jazeera English" in report.failed_sources
    assert edition is not None


def test_window_filter_drops_old_entries(fetched):
    _, edition, _ = fetched
    urls = [article.url for article in edition.articles]
    # bbc.xml 中 07-25 06:34 的条目超出 24 小时窗口
    assert not any("cj9d27v70j1o" in url for url in urls)


def test_full_extraction_and_summary_degradation(fetched):
    _, edition, report = fetched
    by_url = {article.url: article for article in edition.articles}
    bbc = by_url["https://www.bbc.co.uk/news/articles/cevmdxz4872o"]
    assert bbc.content_status == "full"
    assert len(bbc.paragraphs) >= 3
    assert bbc.reading_minutes >= 1
    degraded = [a for a in edition.articles if a.content_status == "summary"]
    assert degraded, "404 页面应降级为摘要"
    assert report.degraded == len(degraded)
    for article in degraded:
        assert article.paragraphs[0].en


def test_briefs_come_from_nyt(fetched):
    _, edition, _ = fetched
    assert all(brief.source == "The New York Times" for brief in edition.briefs)
    assert all(brief.url.startswith("https://www.nytimes.com/") for brief in edition.briefs)


def test_persisted_json_and_rebuild(fetched, tmp_path):
    data_dir, edition, _ = fetched
    stored = data_dir / "fetched" / "2026-07-26.json"
    assert stored.is_file()
    payload = json.loads(stored.read_text(encoding="utf-8"))
    assert payload["edition"]["date"] == "2026-07-26"
    assert payload["report"]

    editions = load_fetched_editions(data_dir)
    output_root = tmp_path / "site"
    release = build_editions(editions, BuildConfig(output_root=output_root, site_url="http://x"))
    index = (output_root / "current" / "index.html").read_text(encoding="utf-8")
    assert "Cheapcoding News" in index
    assert edition.articles[0].title_en in index
    # 真实数据构建不得出现演示标识，页脚为正式来源声明
    assert "样张" not in index
    assert "预览数据" not in index
    assert "版权归原出版方" in index
    article_page = release / "issues" / "2026-07-26" / f"{edition.articles[0].slug}.html"
    assert article_page.is_file()
    page_html = article_page.read_text(encoding="utf-8")
    assert "原始来源" in page_html
    assert "演示占位链接" not in page_html
