"""Render static pages from prepared edition data. No fetching, no model calls."""

import datetime
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, PackageLoader, select_autoescape

from news_digest.config import SITE_NAME, SITE_TAGLINE
from news_digest.models import Article, DailyEdition

WEEKDAY_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 与 sources.feeds.is_web_url 同策略，作为渲染层独立的第二道防线：入库白名单是
# 第一层，此处兜底历史入库数据与任何非 feed 来源的 URL。autoescape 只转义引号/
# 尖括号，挡不住 href="javascript:…" 这类 scheme 注入，必须在此过滤 scheme。
_SAFE_URL_SCHEMES = frozenset({"http", "https"})


def safe_url(url: str | None) -> str:
    """href/src 兜底：只放行 http/https，其余（javascript:/data:/vbscript: 等）→ 空串。"""
    if not url:
        return ""
    return url if urlparse(url).scheme.lower() in _SAFE_URL_SCHEMES else ""


def format_date_zh(date: str) -> str:
    """'2026-07-26' -> '2026 年 7 月 26 日'."""
    year, month, day = (int(part) for part in date.split("-"))
    return f"{year} 年 {month} 月 {day} 日"


def weekday_zh(date: str) -> str:
    year, month, day = (int(part) for part in date.split("-"))
    return WEEKDAY_ZH[datetime.date(year, month, day).weekday()]


def create_environment(*, demo: bool = False) -> Environment:
    """demo=True 时页面渲染「样张·预览数据」标识与演示声明。"""
    env = Environment(
        loader=PackageLoader("news_digest", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(site_name=SITE_NAME, site_tagline=SITE_TAGLINE, is_demo=demo)
    env.filters["date_zh"] = format_date_zh
    env.filters["weekday_zh"] = weekday_zh
    env.filters["safe_url"] = safe_url
    return env


def render_home(
    env: Environment,
    edition: DailyEdition,
    *,
    is_today: bool,
    all_dates: list[str],
) -> str:
    return env.get_template("home.html").render(
        edition=edition,
        is_today=is_today,
        all_dates=all_dates,
        page_title=f"{SITE_NAME} · {format_date_zh(edition.date)}",
    )


def render_article(env: Environment, edition: DailyEdition, article: Article) -> str:
    return env.get_template("article.html").render(
        edition=edition,
        article=article,
        page_title=f"{article.title_en} · {SITE_NAME}",
    )


def render_archive(env: Environment, entries: list[dict[str, Any]]) -> str:
    """entries: [{date, lead_title_en, lead_title_zh, article_count, brief_count}]."""
    return env.get_template("archive.html").render(
        entries=entries,
        page_title=f"往期归档 · {SITE_NAME}",
    )
