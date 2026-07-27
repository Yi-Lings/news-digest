"""Parse RSS/RDF feeds into normalised candidates. No body fetching here."""

import calendar
import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser

from news_digest.models import Candidate
from news_digest.sources.registry import SourceConfig
from news_digest.textutil import collapse_ws, html_to_text

# 实测确认的各来源跟踪参数（BBC: at_*；DW: maca），加上通用营销参数。
_TRACKING_PARAMS = {
    "maca",
    "cmp",
    "ns_campaign",
    "ns_mchannel",
    "ns_source",
    "ocid",
    "cmpid",
    "traffic_source",  # Al Jazeera 实测
}
_TRACKING_PREFIXES = ("utm_", "at_")

# 非文章类页面（直播贴、视频、音频），对文字学习无用且提取结果污染严重。
# 实测：BBC /news/videos/、DW /live-、AJE /news/liveblog/、France 24 视频页。
_EXCLUDED_PATH_PARTS = (
    "/live/",
    "/live-",
    "/liveblog",
    "/videos/",
    "/video/",
    "/av/",
    "/audio/",
    "/podcast",
)


# 只有 http/https 才是合法的对外链接。RSS 的 link 可含 javascript:/data:/vbscript:，
# 若原样落库并渲进 href 即为存储型 XSS——在入库前的这道白名单是第一层防线，
# 模板侧 safe_url 过滤器是第二层。
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def is_web_url(url: str) -> bool:
    return urlparse(url.strip()).scheme.lower() in _ALLOWED_URL_SCHEMES


def is_article_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not any(part in path for part in _EXCLUDED_PATH_PARTS)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower()
            + (f":{parsed.port}" if parsed.port else ""),
            parsed.path,
            parsed.params,
            urlencode(query),
            "",  # drop fragment
        )
    )


def _published_utc(entry) -> str:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr)
        if parsed:
            timestamp = calendar.timegm(parsed)
            moment = datetime.datetime.fromtimestamp(timestamp, tz=datetime.UTC)
            return moment.isoformat()
    return ""


def _image_url(entry) -> str:
    thumbnails = entry.get("media_thumbnail") or []
    for thumbnail in thumbnails:
        if thumbnail.get("url"):
            return thumbnail["url"]
    contents = entry.get("media_content") or []
    best_url, best_width = "", -1
    for content in contents:
        url = content.get("url", "")
        if not url:
            continue
        medium = content.get("medium", "")
        content_type = content.get("type", "")
        if medium and medium != "image" and not content_type.startswith("image/"):
            continue
        try:
            width = int(content.get("width", 0))
        except (TypeError, ValueError):
            width = 0
        if width > best_width:
            best_url, best_width = url, width
    if best_url:
        return best_url
    for enclosure in entry.get("enclosures") or []:
        if str(enclosure.get("type", "")).startswith("image/") and enclosure.get("href"):
            return enclosure["href"]
    return ""


def parse_feed(data: bytes, source: SourceConfig) -> list[Candidate]:
    """Normalise feed entries; entries without title/link/date are dropped."""
    parsed = feedparser.parse(data)
    candidates: list[Candidate] = []
    for entry in parsed.entries:
        title = collapse_ws(html_to_text(entry.get("title", "")))
        link = entry.get("link", "")
        published = _published_utc(entry)
        if not title or not link or not published:
            continue
        if not is_web_url(link):  # 丢弃 javascript:/data: 等非 http(s) 链接
            continue
        if source.kind == "full" and not is_article_url(link):
            continue
        candidates.append(
            Candidate(
                source_key=source.key,
                source_name=source.name,
                kind=source.kind,
                title=title,
                url=canonicalize_url(link),
                summary=html_to_text(entry.get("summary", "")),
                published_at_utc=published,
                author=collapse_ws(html_to_text(entry.get("author", ""))),
                image_url=_image_url(entry),
            )
        )
    return candidates
