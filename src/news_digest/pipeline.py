"""Composition of the full build flow. The only module that wires stages together."""

import datetime
import json
import shutil
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from news_digest.config import BuildConfig, FetchConfig
from news_digest.delivery.publisher import publish
from news_digest.extractors.body import extract_body, reading_minutes
from news_digest.models import (
    Article,
    ArticleImage,
    BriefItem,
    Candidate,
    DailyEdition,
    Paragraph,
    edition_from_dict,
    edition_to_dict,
)
from news_digest.rendering.pages import (
    create_environment,
    render_archive,
    render_article,
    render_home,
)
from news_digest.selection.dedupe import dedupe
from news_digest.sources.feeds import parse_feed
from news_digest.sources.http import FetchError, build_client, safe_get
from news_digest.sources.registry import SOURCES, SourceConfig
from news_digest.textutil import slugify

_STATIC_DIR = Path(__file__).parent / "static"


# ── 真实抓取 ──────────────────────────────────────────────────────────────


@dataclass
class FetchReport:
    per_source: dict[str, str] = field(default_factory=dict)
    articles: int = 0
    briefs: int = 0
    degraded: int = 0

    @property
    def failed_sources(self) -> list[str]:
        return [key for key, status in self.per_source.items() if status.startswith("失败")]


def _within_window(candidate: Candidate, now: datetime.datetime, hours: int) -> bool:
    published = datetime.datetime.fromisoformat(candidate.published_at_utc)
    return published >= now - datetime.timedelta(hours=hours)


def _collect_candidates(
    client: httpx.Client,
    sources: tuple[SourceConfig, ...],
    now: datetime.datetime,
    window_hours: int,
    report: FetchReport,
) -> list[Candidate]:
    collected: list[Candidate] = []
    for source in sources:
        try:
            raw = safe_get(client, source.feed_url, source.allowed_domains)
            candidates = [
                candidate
                for candidate in parse_feed(raw, source)
                if _within_window(candidate, now, window_hours)
            ]
            candidates.sort(key=lambda c: c.published_at_utc, reverse=True)
            candidates = candidates[: source.max_articles]
            collected.extend(candidates)
            report.per_source[source.name] = f"正常，窗口内 {len(candidates)} 条"
        except FetchError as error:
            report.per_source[source.name] = f"失败：{error}"
    return collected


def _candidate_to_article(
    client: httpx.Client,
    candidate: Candidate,
    source: SourceConfig,
    report: FetchReport,
) -> Article:
    paragraphs: list[str] = []
    author = candidate.author
    image_url = candidate.image_url
    try:
        page = safe_get(client, candidate.url, source.allowed_domains)
        extracted = extract_body(page.decode("utf-8", errors="replace"), candidate.url)
    except FetchError:
        extracted = None
    if extracted is not None:
        paragraphs = extracted.paragraphs
        author = author or extracted.author
        image_url = image_url or extracted.image_url

    if paragraphs:
        content_status = "full"
        body = [Paragraph(en=p) for p in paragraphs]
        minutes = reading_minutes(paragraphs)
    else:
        content_status = "summary"
        report.degraded += 1
        body = [Paragraph(en=candidate.summary or candidate.title)]
        minutes = 1

    image = None
    if image_url:
        image = ArticleImage(
            src=image_url,
            alt_en=candidate.title,
            credit=f"图片来源：{candidate.source_name}",
        )
    return Article(
        slug=slugify(candidate.title),
        source=candidate.source_name,
        title_en=candidate.title,
        summary_en=candidate.summary or candidate.title,
        author=author,
        published_at=candidate.published_at_utc,
        url=candidate.url,
        reading_minutes=minutes,
        paragraphs=body,
        image=image,
        content_status=content_status,
    )


def fetch_daily(
    config: FetchConfig,
    *,
    client: httpx.Client | None = None,
    now: datetime.datetime | None = None,
) -> tuple[DailyEdition | None, FetchReport]:
    """Fetch feeds, extract bodies, and persist the day's edition to data_dir."""
    report = FetchReport()
    now = now or datetime.datetime.now(datetime.UTC)
    own_client = client is None
    client = client or build_client(config.proxy)
    try:
        full = tuple(s for s in SOURCES if s.kind == "full")
        brief = tuple(s for s in SOURCES if s.kind == "brief")
        by_key = {s.key: s for s in SOURCES}

        candidates = dedupe(
            _collect_candidates(client, full, now, config.window_hours, report)
        )
        articles = [
            _candidate_to_article(client, candidate, by_key[candidate.source_key], report)
            for candidate in candidates
        ]
        brief_candidates = dedupe(
            _collect_candidates(client, brief, now, config.window_hours, report)
        )
        briefs = [
            BriefItem(title_en=c.title, source=c.source_name, url=c.url)
            for c in brief_candidates
        ]
    finally:
        if own_client:
            client.close()

    report.articles = len(articles)
    report.briefs = len(briefs)
    if not articles and not briefs:
        return None, report

    local_date = now.astimezone(zoneinfo.ZoneInfo(config.timezone)).date().isoformat()
    edition = DailyEdition(date=local_date, articles=articles, briefs=briefs)

    fetched_dir = config.data_dir / "fetched"
    fetched_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now.isoformat(),
        "report": report.per_source,
        "edition": edition_to_dict(edition),
    }
    (fetched_dir / f"{local_date}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return edition, report


# ── 站点构建 ──────────────────────────────────────────────────────────────


def load_fixture_editions(fixtures_dir: Path) -> list[DailyEdition]:
    """Load demo editions (newest first) from *.json files in fixtures_dir."""
    paths = sorted(fixtures_dir.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"未在 {fixtures_dir} 找到演示数据（*.json）")
    editions = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        editions.append(edition_from_dict(data))
    editions.sort(key=lambda edition: edition.date, reverse=True)
    return editions


def load_fetched_editions(data_dir: Path) -> list[DailyEdition]:
    """Load previously fetched editions (newest first) from data_dir/fetched."""
    fetched_dir = data_dir / "fetched"
    paths = sorted(fetched_dir.glob("*.json"))
    if not paths:
        raise FileNotFoundError(
            f"未在 {fetched_dir} 找到抓取数据；请先运行 news-digest fetch"
        )
    editions = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        editions.append(edition_from_dict(data["edition"]))
    editions.sort(key=lambda edition: edition.date, reverse=True)
    return editions


def build_site(fixtures_dir: Path, config: BuildConfig) -> Path:
    """Render the demo site from fixture data, validate, then publish."""
    return build_editions(
        load_fixture_editions(fixtures_dir), config, fixture_images=fixtures_dir / "images"
    )


def build_editions(
    editions: list[DailyEdition],
    config: BuildConfig,
    fixture_images: Path | None = None,
) -> Path:
    output_root = config.output_root
    build_dir = output_root / ".build-tmp"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    env = create_environment()
    latest = editions[0]
    all_dates = [edition.date for edition in editions]

    for edition in editions:
        issue_dir = build_dir / "issues" / edition.date
        issue_dir.mkdir(parents=True)
        home_html = render_home(env, edition, is_today=edition is latest, all_dates=all_dates)
        (issue_dir / "index.html").write_text(home_html, encoding="utf-8")
        for article in edition.articles:
            article_html = render_article(env, edition, article)
            (issue_dir / f"{article.slug}.html").write_text(article_html, encoding="utf-8")

    root_html = render_home(env, latest, is_today=True, all_dates=all_dates)
    (build_dir / "index.html").write_text(root_html, encoding="utf-8")

    archive_dir = build_dir / "archive"
    archive_dir.mkdir()
    entries = [
        {
            "date": edition.date,
            "lead_title_en": edition.articles[0].title_en if edition.articles else "",
            "lead_title_zh": edition.articles[0].title_zh if edition.articles else "",
            "article_count": len(edition.articles),
            "brief_count": len(edition.briefs),
        }
        for edition in editions
    ]
    (archive_dir / "index.html").write_text(render_archive(env, entries), encoding="utf-8")

    shutil.copytree(_STATIC_DIR, build_dir / "assets")
    if fixture_images is not None and fixture_images.is_dir():
        shutil.copytree(fixture_images, build_dir / "assets" / "demo")

    _validate_build(build_dir)
    return publish(build_dir, output_root, _release_name(output_root, latest.date))


def _release_name(output_root: Path, date: str) -> str:
    releases = output_root / "releases"
    sequence = 1
    while (releases / f"{date}-{sequence:02d}").exists():
        sequence += 1
    return f"{date}-{sequence:02d}"


def _validate_build(build_dir: Path) -> None:
    required = [
        build_dir / "index.html",
        build_dir / "archive" / "index.html",
        build_dir / "assets" / "style.css",
        build_dir / "assets" / "app.js",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"构建产物缺失，已中止发布：{missing}")
