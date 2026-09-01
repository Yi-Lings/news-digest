"""End-to-end fixture build: page content, internal links, local-only resources."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

from news_digest.config import BuildConfig
from news_digest.pipeline import build_site

FIXTURES = Path(__file__).parent.parent / "fixtures" / "demo"
RUNTIME_ROUTES = frozenset({"/admin/", "/account", "/subscribe", "/contact"})


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    output_root = tmp_path_factory.mktemp("site")
    config = BuildConfig(output_root=output_root, site_url="http://127.0.0.1:8000")
    release = build_site(FIXTURES, config)
    return output_root, release


class RefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.resources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        elif tag in {"img", "script"} and attributes.get("src"):
            self.resources.append(attributes["src"])
        elif tag == "link" and attributes.get("href"):
            self.resources.append(attributes["href"])


def _collect(path: Path) -> RefCollector:
    collector = RefCollector()
    collector.feed(path.read_text(encoding="utf-8"))
    return collector


def test_release_layout(site):
    output_root, release = site
    current = output_root / "current"
    assert release.parent == output_root / "releases"
    for required in [
        current / "index.html",
        current / "archive" / "index.html",
        current / "privacy" / "index.html",
        current / "assets" / "style.css",
        current / "assets" / "app.js",
        current / "assets" / "alipay-support-qr.jpg",
    ]:
        assert required.is_file(), required


def test_home_shows_lead_and_demo_stamp(site):
    output_root, _ = site
    html = (output_root / "current" / "index.html").read_text(encoding="utf-8")
    assert "Coastal Cities Turn to" in html
    assert "海绵街道" in html
    assert "预览数据" in html
    assert "Cheapcoding News" in html
    assert "简讯" in html
    assert "data-subscribe-form" not in html
    assert 'href="/admin/"' not in html
    assert "<!--ADMIN_NAV-->" in html
    assert 'class="member-entry" href="/subscribe"' in html
    assert 'class="account-entry" href="/account"' in html
    assert "本站完全用爱发电" in html
    assert "data-support-panel" in html
    assert 'src="/assets/alipay-support-qr.jpg"' in html
    assert 'href="#support"' in html
    assert 'src="/assets/demo/demo-city.svg"' in html


def test_home_dateline_uses_two_semantic_mobile_rows(site):
    output_root, _ = site
    html = (output_root / "current" / "index.html").read_text(encoding="utf-8")
    css = (output_root / "current" / "assets" / "style.css").read_text(encoding="utf-8")
    assert 'class="dateline issue-meta"' in html
    assert html.count('class="issue-meta-line"') >= 2
    assert html.count('class="issue-meta-unit"') >= 4
    assert "篇，简讯" not in html
    assert ".issue-meta-unit { white-space: nowrap; }" in css
    assert ".issue-meta-divider { display: none; }" in css
    assert ".front > * { min-width: 0; }" in css


def test_home_uses_one_membership_and_newsletter_entry(tmp_path):
    output_root = tmp_path / "site"
    build_site(
        FIXTURES,
        BuildConfig(
            output_root=output_root,
            site_url="https://news.example.com",
            public_subscription_enabled=True,
        ),
    )
    current = output_root / "current"
    root_html = (current / "index.html").read_text(encoding="utf-8")
    assert "data-subscribe-form" not in root_html
    assert "会员订阅与每日简报" in root_html
    assert 'href="/subscribe"' in root_html
    dated_pages = sorted((current / "issues").glob("*/index.html"))
    assert dated_pages
    for page in dated_pages:
        assert "data-subscribe-form" not in page.read_text(encoding="utf-8"), page
        assert "data-support-panel" not in page.read_text(encoding="utf-8"), page


def test_privacy_page_documents_paid_member_newsletter_and_one_click(site):
    output_root, _ = site
    html = (output_root / "current" / "privacy" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "有效付费会员" in html
    assert "登录" in html
    assert "one-click" in html
    assert "不可逆摘要" in html
    assert "double opt-in" not in html
    assert "限时确认链接" not in html


def test_article_page_contains_learning_sections(site):
    output_root, _ = site
    page = output_root / "current" / "issues" / "2026-07-26" / "sponge-streets.html"
    html = page.read_text(encoding="utf-8")
    assert "重点词汇" in html
    assert "固定搭配" in html
    assert "长难句解析" in html
    assert "permeable" in html
    assert "朗读全文" in html
    assert '<option value="0.9" selected>正常</option>' in html


def test_every_article_has_a_page(site):
    output_root, _ = site
    current = output_root / "current"
    expected = {
        ("2026-07-26", "sponge-streets"),
        ("2026-07-26", "night-trains"),
        ("2026-07-26", "seafloor-atlas"),
        ("2026-07-26", "urban-mining"),
        ("2026-07-26", "fog-harvesting"),
        ("2026-07-26", "handwriting-classroom"),
        ("2026-07-25", "libraries-third-places"),
        ("2026-07-25", "container-shipping"),
    }
    for date, slug in expected:
        assert (current / "issues" / date / f"{slug}.html").is_file(), (date, slug)


def test_archive_lists_all_dates(site):
    output_root, _ = site
    html = (output_root / "current" / "archive" / "index.html").read_text(encoding="utf-8")
    assert "2026-07-26" in html
    assert "2026-07-25" in html
    assert "archive-calendar" in html
    assert "archive-calendar-wrap" in html
    assert 'href="/issues/2026-07-26/"' in html
    assert 'href="/issues/2026-07-25/"' in html
    assert 'id="back-to-top"' in html
    assert 'href="/admin/"' not in html


def test_home_page_links_to_archive_banner_and_has_back_to_top(site):
    output_root, _ = site
    current = output_root / "current"
    html = (current / "index.html").read_text(encoding="utf-8")
    script = (current / "assets" / "app.js").read_text(encoding="utf-8")
    assert "archive-banner" in html
    assert 'href="/archive/" class="archive-banner-btn"' in html
    assert 'id="back-to-top"' in html
    assert 'class="issue-link' not in html
    assert "全部归档 →" not in html
    assert 'getElementById("back-to-top")' in script


def test_internal_links_resolve_and_resources_are_local(site):
    output_root, _ = site
    current = output_root / "current"
    for page in sorted(current.rglob("*.html")):
        collector = _collect(page)
        for resource in collector.resources:
            assert resource.startswith("/"), f"{page}: 非本地资源 {resource}"
            assert (current / resource.lstrip("/")).is_file(), f"{page}: 资源缺失 {resource}"
        for href in collector.links:
            if href.startswith("#"):
                continue
            if href in RUNTIME_ROUTES:
                continue
            if href.startswith("/"):
                target = current / href.lstrip("/")
                if href.endswith("/"):
                    target = target / "index.html"
                assert target.is_file(), f"{page}: 死链 {href}"
            else:
                assert href.startswith("https://example.com/"), f"{page}: 非演示外链 {href}"


def test_second_build_creates_new_release_and_keeps_previous(site):
    output_root, first_release = site
    config = BuildConfig(output_root=output_root, site_url="http://127.0.0.1:8000")
    second_release = build_site(FIXTURES, config)
    assert second_release != first_release
    assert (first_release / "index.html").is_file()
    assert (output_root / "current" / "index.html").is_file()
