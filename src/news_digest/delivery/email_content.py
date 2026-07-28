"""Pure email content selection and MIME message assembly.

The selector consumes only the just-published ``DailyEdition`` supplied by its caller. It
performs no database, model, filesystem, or network access.
"""

import datetime
import ipaddress
import re
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from news_digest.models import DailyEdition

EmailLanguage = Literal["bi", "zh", "en"]
EmailLayout = Literal["digest", "compact"]
SummaryLength = Literal["short", "standard", "long"]

_LANGUAGES = frozenset({"bi", "zh", "en"})
_LAYOUTS = frozenset({"digest", "compact"})
_SUMMARY_LENGTHS = frozenset({"short", "standard", "long"})
_SUMMARY_LIMITS: dict[str, int] = {"short": 96, "standard": 220, "long": 480}
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_NON_PUBLIC_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home")


@dataclass(frozen=True)
class EmailContentConfig:
    """One deterministic combination of published content and presentation options."""

    mains_enabled: bool = True
    briefs_enabled: bool = True
    main_limit: int = 6
    brief_limit: int = 5
    language: EmailLanguage = "bi"
    source_filters: tuple[str, ...] = ()
    topic_filters: tuple[str, ...] = ()
    layout: EmailLayout = "digest"
    summary_length: SummaryLength = "standard"

    def __post_init__(self) -> None:
        if type(self.mains_enabled) is not bool or type(self.briefs_enabled) is not bool:
            raise ValueError("content enabled flags must be boolean")
        if not self.mains_enabled and not self.briefs_enabled:
            raise ValueError("at least one of mains or briefs must be enabled")
        for name, value in (("main_limit", self.main_limit), ("brief_limit", self.brief_limit)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.language not in _LANGUAGES:
            raise ValueError("language must be bi, zh, or en")
        if self.layout not in _LAYOUTS:
            raise ValueError("layout must be digest or compact")
        if self.summary_length not in _SUMMARY_LENGTHS:
            raise ValueError("summary_length must be short, standard, or long")

        sources = _normalize_filters(self.source_filters, "source_filters")
        topics = _normalize_filters(self.topic_filters, "topic_filters")
        if topics:
            raise ValueError(
                "topic_filters are unsupported: DailyEdition has no structured topic field"
            )
        object.__setattr__(self, "source_filters", sources)
        object.__setattr__(self, "topic_filters", topics)


@dataclass(frozen=True)
class EmailMainContent:
    source: str
    title_en: str
    title_zh: str
    summary_en: str
    summary_zh: str
    reading_minutes: int
    article_url: str
    translation_missing: bool


@dataclass(frozen=True)
class EmailBriefContent:
    source: str
    title_en: str
    title_zh: str
    url: str
    translation_missing: bool


@dataclass(frozen=True)
class EmailPreviewMetadata:
    edition_date: str
    main_count: int
    brief_count: int
    degraded: bool

    @property
    def total_count(self) -> int:
        return self.main_count + self.brief_count


@dataclass(frozen=True)
class SelectedEmailContent:
    date: str
    issue_url: str
    mains: tuple[EmailMainContent, ...]
    briefs: tuple[EmailBriefContent, ...]
    language: EmailLanguage
    layout: EmailLayout
    summary_length: SummaryLength
    degraded: bool

    @property
    def metadata(self) -> EmailPreviewMetadata:
        return EmailPreviewMetadata(
            edition_date=self.date,
            main_count=len(self.mains),
            brief_count=len(self.briefs),
            degraded=self.degraded,
        )


def _normalize_filters(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{field} must be a sequence of values")
    try:
        raw_values = tuple(values)
    except TypeError as error:
        raise ValueError(f"{field} must be a sequence of values") from error

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            raise ValueError(f"{field} values must be strings")
        value = raw.strip()
        if not value or "\r" in value or "\n" in value:
            raise ValueError(f"{field} contains an empty or unsafe value")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(value)
    return tuple(normalized)


def _validate_date(value: str, field: str = "edition date") -> str:
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _public_site_url(site_url: str) -> str:
    if not isinstance(site_url, str):
        raise ValueError("NEWS_SITE_URL must be a public HTTPS base URL")
    raw = site_url.strip()
    if (
        raw != site_url.strip(" ")
        or not raw
        or "\\" in raw
        or any(character.isspace() for character in raw)
    ):
        raise ValueError("NEWS_SITE_URL must be a public HTTPS base URL")
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ValueError("NEWS_SITE_URL must be a public HTTPS base URL") from error
    if (
        parts.scheme.lower() != "https"
        or not parts.netloc
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("NEWS_SITE_URL must be a public HTTPS base URL")

    hostname = host.lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if (
            "." not in hostname
            or hostname == "localhost"
            or hostname.endswith(_NON_PUBLIC_SUFFIXES)
            or len(hostname) > 253
            or any(not _HOST_LABEL.fullmatch(label) for label in hostname.split("."))
        ):
            raise ValueError("NEWS_SITE_URL must be a public HTTPS base URL") from None
        netloc = hostname if port is None else f"{hostname}:{port}"
    else:
        if not address.is_global:
            raise ValueError("NEWS_SITE_URL must be a public HTTPS base URL")
        rendered_host = f"[{hostname}]" if address.version == 6 else hostname
        netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def _external_url(url: str) -> str:
    """Return a safe absolute source URL, or an empty string for legacy unsafe data."""
    if not isinstance(url, str) or not url or any(character.isspace() for character in url):
        return ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.netloc
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        return ""
    return url


def _summary(value: str, length: SummaryLength) -> str:
    collapsed = " ".join(value.split())
    limit = _SUMMARY_LIMITS[length]
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _translation_required(language: EmailLanguage) -> bool:
    return language in {"bi", "zh"}


def select_email_content(
    edition: DailyEdition,
    site_url: str,
    config: EmailContentConfig,
    *,
    expected_date: str | None = None,
) -> SelectedEmailContent:
    """Select from one supplied release while preserving its publication order."""
    date = _validate_date(edition.date)
    if expected_date is not None and date != _validate_date(expected_date, "expected date"):
        raise ValueError("edition date does not match the expected just-published release")
    base_url = _public_site_url(site_url)

    articles = tuple(edition.articles)
    briefs = tuple(edition.briefs)
    if config.mains_enabled and config.main_limit > len(articles):
        raise ValueError("main_limit exceeds the published edition")
    if config.briefs_enabled and config.brief_limit > len(briefs):
        raise ValueError("brief_limit exceeds the published edition")

    available_sources = {
        item.source.casefold(): item.source for item in (*articles, *briefs) if item.source.strip()
    }
    unknown_sources = [
        source for source in config.source_filters if source.casefold() not in available_sources
    ]
    if unknown_sources:
        raise ValueError("source_filters contains a source absent from the published edition")
    allowed_sources = {source.casefold() for source in config.source_filters}

    def source_allowed(source: str) -> bool:
        return not allowed_sources or source.casefold() in allowed_sources

    selected_articles = (
        tuple(article for article in articles if source_allowed(article.source))[
            : config.main_limit
        ]
        if config.mains_enabled
        else ()
    )
    selected_briefs = (
        tuple(brief for brief in briefs if source_allowed(brief.source))[: config.brief_limit]
        if config.briefs_enabled
        else ()
    )
    if not selected_articles and not selected_briefs:
        raise ValueError("email content selection is empty")

    issue_url = f"{base_url}/issues/{date}/"
    translation_required = _translation_required(config.language)
    mains: list[EmailMainContent] = []
    for article in selected_articles:
        if not isinstance(article.slug, str) or not _SLUG.fullmatch(article.slug):
            raise ValueError("published article has an invalid page slug")
        missing = translation_required and (
            not article.title_zh.strip() or not article.summary_zh.strip()
        )
        mains.append(
            EmailMainContent(
                source=article.source,
                title_en=article.title_en,
                title_zh=article.title_zh,
                summary_en=_summary(article.summary_en, config.summary_length),
                summary_zh=_summary(article.summary_zh, config.summary_length),
                reading_minutes=article.reading_minutes,
                article_url=f"{issue_url}{article.slug}.html",
                translation_missing=missing,
            )
        )

    email_briefs: list[EmailBriefContent] = []
    for brief in selected_briefs:
        missing = translation_required and not brief.title_zh.strip()
        email_briefs.append(
            EmailBriefContent(
                source=brief.source,
                title_en=brief.title_en,
                title_zh=brief.title_zh,
                url=_external_url(brief.url),
                translation_missing=missing,
            )
        )

    all_items = (*mains, *email_briefs)
    return SelectedEmailContent(
        date=date,
        issue_url=issue_url,
        mains=tuple(mains),
        briefs=tuple(email_briefs),
        language=config.language,
        layout=config.layout,
        summary_length=config.summary_length,
        degraded=any(item.translation_missing for item in all_items),
    )


def build_email_message(
    edition: DailyEdition,
    site_url: str,
    sender: str,
    recipients: tuple[str, ...],
    config: EmailContentConfig | None = None,
    *,
    test: bool = False,
    expected_date: str | None = None,
    unsubscribe_url: str | None = None,
) -> EmailMessage:
    """Build one short UTF-8 HTML update notice with the preview renderer."""
    from news_digest.delivery.mailer import compose
    from news_digest.rendering.email import render_email_preview

    rendered = render_email_preview(
        edition,
        site_url,
        config,
        test=test,
        expected_date=expected_date,
        unsubscribe_url=unsubscribe_url,
    )
    message = compose(rendered.subject, None, rendered.html, sender, recipients)
    if unsubscribe_url is not None:
        from news_digest.delivery.mailer import inject_unsubscribe

        inject_unsubscribe(message, unsubscribe_url)
    return message
