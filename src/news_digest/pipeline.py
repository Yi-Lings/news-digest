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
from news_digest.selection.score import select_daily
from news_digest.sources.feeds import parse_feed
from news_digest.sources.http import FetchError, build_client, proxy_active, safe_get
from news_digest.sources.registry import SOURCES, SourceConfig
from news_digest.storage import db
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
    skip_ip_check: bool,
) -> list[Candidate]:
    collected: list[Candidate] = []
    for source in sources:
        try:
            raw = safe_get(
                client, source.feed_url, source.allowed_domains, skip_ip_check=skip_ip_check
            )
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
    skip_ip_check: bool,
) -> Article:
    paragraphs: list[str] = []
    author = candidate.author
    image_url = candidate.image_url
    try:
        page = safe_get(
            client, candidate.url, source.allowed_domains, skip_ip_check=skip_ip_check
        )
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
    skip_ip = proxy_active(config.proxy)
    try:
        full = tuple(s for s in SOURCES if s.kind == "full")
        brief = tuple(s for s in SOURCES if s.kind == "brief")
        by_key = {s.key: s for s in SOURCES}

        candidates = dedupe(
            _collect_candidates(client, full, now, config.window_hours, report, skip_ip)
        )
        articles = [
            _candidate_to_article(
                client, candidate, by_key[candidate.source_key], report, skip_ip
            )
            for candidate in candidates
        ]
        brief_candidates = dedupe(
            _collect_candidates(client, brief, now, config.window_hours, report, skip_ip)
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

    conn = db.connect(config.database)
    try:
        db.upsert_articles(conn, local_date, articles)
        db.upsert_briefs(conn, local_date, briefs)
    finally:
        conn.close()
    return edition, report


# ── 数据库版次组装与完整流水线 ────────────────────────────────────────────


def import_legacy_fetched(config: FetchConfig) -> int:
    """DB 为空时把 var/data/fetched/*.json 一次性导入；返回导入日期数。"""
    conn = db.connect(config.database)
    try:
        if db.list_dates(conn):
            return 0
        paths = sorted((config.data_dir / "fetched").glob("*.json"))
        for path in paths:
            edition = edition_from_dict(
                json.loads(path.read_text(encoding="utf-8"))["edition"]
            )
            db.upsert_articles(conn, edition.date, edition.articles)
            db.upsert_briefs(conn, edition.date, edition.briefs)
        return len(paths)
    finally:
        conn.close()


def _article_to_brief(article: Article) -> BriefItem:
    return BriefItem(
        title_en=article.title_en,
        title_zh=article.title_zh,
        source=article.source,
        url=article.url,
    )


def selected_edition(
    conn, date: str, now: datetime.datetime, main_count: int = 6
) -> DailyEdition | None:
    """从文章池选出主文章，未入选者与既有简讯合并为当日简讯。"""
    pool = db.get_edition(conn, date)
    if pool is None:
        return None
    selection = select_daily(pool.articles, reference_time=now, main_count=main_count)
    briefs = pool.briefs + [_article_to_brief(article) for article in selection.overflow]
    return DailyEdition(date=date, articles=selection.mains, briefs=briefs)


def load_db_editions(
    config: FetchConfig, now: datetime.datetime | None = None
) -> list[DailyEdition]:
    """全部日期的选题后版次（新在前）；DB 为空时尝试导入历史 JSON。"""
    import_legacy_fetched(config)
    now = now or datetime.datetime.now(datetime.UTC)
    conn = db.connect(config.database)
    try:
        editions = []
        for date in db.list_dates(conn):
            edition = selected_edition(conn, date, now)
            if edition is not None:
                editions.append(edition)
        if not editions:
            raise FileNotFoundError("数据库无内容；请先运行 news-digest fetch 或 run")
        return editions
    finally:
        conn.close()


def selected_mains_for_translation(
    config: FetchConfig, date: str, now: datetime.datetime | None = None
) -> DailyEdition | None:
    """translate 阶段的输入：当日选题后的主文章版次。"""
    import_legacy_fetched(config)
    now = now or datetime.datetime.now(datetime.UTC)
    conn = db.connect(config.database)
    try:
        return selected_edition(conn, date, now)
    finally:
        conn.close()


def store_translated(config: FetchConfig, date: str, articles: list[Article]) -> None:
    conn = db.connect(config.database)
    try:
        db.upsert_articles(conn, date, articles)
    finally:
        conn.close()


def latest_db_date(config: FetchConfig) -> str | None:
    import_legacy_fetched(config)
    conn = db.connect(config.database)
    try:
        dates = db.list_dates(conn)
        return dates[0] if dates else None
    finally:
        conn.close()


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
        load_fixture_editions(fixtures_dir),
        config,
        fixture_images=fixtures_dir / "images",
        demo=True,
    )


def build_editions(
    editions: list[DailyEdition],
    config: BuildConfig,
    fixture_images: Path | None = None,
    demo: bool = False,
) -> Path:
    output_root = config.output_root
    build_dir = output_root / ".build-tmp"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    env = create_environment(demo=demo)
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
