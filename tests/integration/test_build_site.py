"""End-to-end fixture build: page content, internal links, local-only resources."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

from news_digest.config import BuildConfig
from news_digest.pipeline import build_site

FIXTURES = Path(__file__).parent.parent / "fixtures" / "demo"
RUNTIME_ROUTES = frozenset({"/admin/"})


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
    assert html.count('href="/admin/"') == 1
    assert 'class="admin-entry"' in html
    assert "本站完全用爱发电" in html
    assert "data-support-panel" in html
    assert 'src="/assets/alipay-support-qr.jpg"' in html
    assert 'href="#support"' in html


def test_subscription_form_is_limited_to_the_current_root_homepage(tmp_path):
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
    assert "data-subscribe-form" in root_html
    assert 'href="#subscribe"' in root_html
    dated_pages = sorted((current / "issues").glob("*/index.html"))
    assert dated_pages
    for page in dated_pages:
        assert "data-subscribe-form" not in page.read_text(encoding="utf-8"), page
        assert "data-support-panel" not in page.read_text(encoding="utf-8"), page


def test_privacy_page_documents_double_opt_in_and_one_click(site):
    output_root, _ = site
    html = (output_root / "current" / "privacy" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "double opt-in" in html
    assert "one-click" in html
    assert "不可逆摘要" in html


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
    assert 'href="/admin/"' not in html


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
