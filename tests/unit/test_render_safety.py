"""渲染安全：链接协议白名单（入库）+ safe_url 过滤器（渲染）+ autoescape。

弥补 PLAN 承诺但此前缺失的安全测试。覆盖存储型 XSS 的两道防线：
1. sources.feeds 入库时丢弃 javascript:/data: 等非 http(s) 链接；
2. 即便恶意 URL 绕过第一层（如历史入库数据），safe_url 过滤器在模板兜底；
以及 Jinja autoescape 对文本字段（标题含 <script>）的转义。
"""

from news_digest.models import Article, ArticleImage, BriefItem, DailyEdition, Paragraph
from news_digest.rendering.email import render_email
from news_digest.rendering.pages import (
    create_environment,
    render_article,
    render_home,
    safe_url,
)
from news_digest.sources.feeds import is_web_url, parse_feed
from news_digest.sources.registry import SOURCES

_BY_KEY = {source.key: source for source in SOURCES}
_XSS = "javascript:alert(document.cookie)"


# ---- 第一层：入库协议白名单 ----------------------------------------------

def test_is_web_url_only_allows_http_https():
    assert is_web_url("https://example.com/x")
    assert is_web_url("http://example.com/x")
    assert not is_web_url(_XSS)
    assert not is_web_url("data:text/html,<script>alert(1)</script>")
    assert not is_web_url("vbscript:msgbox(1)")
    assert not is_web_url("//example.com/protocol-relative")


def test_parse_feed_drops_non_http_links():
    rss = f"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Good story</title>
    <link>https://www.bbc.co.uk/news/articles/good</link>
    <pubDate>Sun, 26 Jul 2026 09:18:33 GMT</pubDate>
  </item>
  <item>
    <title>Evil story</title>
    <link>{_XSS}</link>
    <pubDate>Sun, 26 Jul 2026 09:18:33 GMT</pubDate>
  </item>
</channel></rss>""".encode()
    candidates = parse_feed(rss, _BY_KEY["bbc"])
    assert len(candidates) == 1
    assert candidates[0].title == "Good story"
    assert all(c.url.startswith("https://") for c in candidates)


# ---- 第二层：safe_url 过滤器 ---------------------------------------------

def test_safe_url_filter_neutralizes_dangerous_schemes():
    assert safe_url("https://example.com/x") == "https://example.com/x"
    assert safe_url("http://example.com/x") == "http://example.com/x"
    assert safe_url("/assets/demo/demo-city.svg") == "/assets/demo/demo-city.svg"
    assert safe_url("//example.com/protocol-relative") == ""
    assert safe_url("/\\example.com") == ""
    assert safe_url("/assets\\example.svg") == ""
    assert safe_url(_XSS) == ""
    assert safe_url("JavaScript:alert(1)") == ""  # 大小写
    assert safe_url("data:text/html;base64,PHNjcmlwdD4=") == ""
    assert safe_url("vbscript:msgbox(1)") == ""
    assert safe_url("") == ""
    assert safe_url(None) == ""
    assert safe_url("  javascript:alert(1)") == ""  # 前导空白使 scheme 失效→拒绝


# ---- 端到端：恶意 URL 绕过第一层后，渲染仍不产生可执行 href ----------------

def _edition_with_evil_urls() -> DailyEdition:
    """模拟历史入库数据：模型里直接塞恶意 URL（绕过 feeds 白名单）。"""
    article = Article(
        slug="evil",
        source="BBC News",
        title_en="<script>alert('xss')</script>Legit headline",
        title_zh="正常标题",
        summary_en="s",
        summary_zh="摘",
        author="a",
        published_at="2026-07-26T09:18:33+00:00",
        url=_XSS,
        reading_minutes=3,
        paragraphs=[Paragraph(en="One.", zh="一。")],
        image=ArticleImage(src=_XSS, alt_en="alt", credit="c"),
        translated_by="m@p2",
    )
    brief = BriefItem(
        title_en="Brief",
        title_zh="简讯",
        source="NYT",
        url=_XSS,
    )
    return DailyEdition(date="2026-07-26", articles=[article], briefs=[brief])


def test_rendered_pages_have_no_javascript_scheme():
    env = create_environment()
    edition = _edition_with_evil_urls()
    home = render_home(env, edition, is_today=True, all_dates=["2026-07-26"])
    article = render_article(env, edition, edition.articles[0])
    _, _, email_html = render_email(edition, "https://news.example.com")
    for html in (home, article, email_html):
        assert "javascript:" not in html.lower()


def test_rendered_pages_escape_script_in_title():
    env = create_environment()
    edition = _edition_with_evil_urls()
    article = render_article(env, edition, edition.articles[0])
    # autoescape 应把标题里的 <script> 转成实体，绝不能出现裸标签
    assert "<script>" not in article
    assert "&lt;script&gt;" in article


def test_home_links_the_unified_membership_page_without_anonymous_form():
    env = create_environment()
    edition = _edition_with_evil_urls()
    disabled = render_home(env, edition, is_today=True, all_dates=[edition.date])
    enabled = render_home(
        env,
        edition,
        is_today=True,
        all_dates=[edition.date],
        public_subscription_enabled=True,
    )
    assert "data-subscribe-form" not in disabled
    assert "data-subscribe-form" not in enabled
    assert "会员订阅与每日简报" in enabled
    assert 'href="/subscribe"' in enabled
    assert 'href="/privacy/"' in enabled
