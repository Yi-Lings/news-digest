"""Shared data models. Pure data structures: no network, database, or file IO."""

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VocabularyItem:
    word: str
    phonetic: str
    meaning_zh: str
    example_en: str


@dataclass(frozen=True)
class Collocation:
    phrase: str
    meaning_zh: str
    example_en: str


@dataclass(frozen=True)
class SentenceNote:
    sentence_en: str
    translation_zh: str
    analysis_zh: str


@dataclass(frozen=True)
class Paragraph:
    en: str
    zh: str = ""


@dataclass(frozen=True)
class ArticleImage:
    src: str
    alt_en: str
    credit: str


@dataclass(frozen=True)
class Candidate:
    """Normalised feed entry, before body extraction and selection."""

    source_key: str
    source_name: str
    kind: str  # "full" | "brief"
    title: str
    url: str  # canonical: tracking params stripped
    summary: str  # plain text
    published_at_utc: str  # ISO 8601, e.g. "2026-07-26T09:18:33+00:00"
    author: str = ""
    image_url: str = ""
    updated_at_utc: str | None = None
    date_basis: str = "published"


@dataclass(frozen=True)
class Article:
    slug: str
    source: str
    title_en: str
    summary_en: str
    author: str
    published_at: str
    url: str
    reading_minutes: int
    paragraphs: list[Paragraph]
    title_zh: str = ""
    summary_zh: str = ""
    vocabulary: list[VocabularyItem] = field(default_factory=list)
    collocations: list[Collocation] = field(default_factory=list)
    sentence_notes: list[SentenceNote] = field(default_factory=list)
    image: ArticleImage | None = None
    content_status: str = "full"  # "full" | "summary"
    translated_by: str = ""  # 例如 "gpt-4o-mini@p1"；空表示未翻译
    source_key: str = ""
    fetched_at: str | None = None
    updated_at: str | None = None
    extraction: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BriefItem:
    title_en: str
    source: str
    url: str
    title_zh: str = ""
    published_at: str | None = None
    source_key: str = ""
    selection_reason: str = ""


@dataclass(frozen=True)
class DailyEdition:
    date: str
    articles: list[Article] = field(default_factory=list)
    briefs: list[BriefItem] = field(default_factory=list)
    generation: int = 0
    result_revisions: dict[str, int] = field(default_factory=dict)
    source_status: str = "ready"

    @property
    def sources(self) -> list[str]:
        """Distinct article sources in first-appearance order."""
        seen: dict[str, None] = {}
        for article in [*self.articles, *self.briefs]:
            seen.setdefault(article.source, None)
        return list(seen)


def article_source_hash(article: Article) -> str:
    text = article.title_en + "\n" + article.summary_en + "\n"
    text += "\n".join(paragraph.en for paragraph in article.paragraphs)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def article_from_dict(data: dict[str, Any]) -> Article:
    image_data = data.get("image")
    return Article(
        slug=data["slug"],
        source=data["source"],
        title_en=data["title_en"],
        title_zh=data.get("title_zh", ""),
        summary_en=data["summary_en"],
        summary_zh=data.get("summary_zh", ""),
        author=data.get("author", ""),
        published_at=data["published_at"],
        url=data["url"],
        reading_minutes=int(data["reading_minutes"]),
        content_status=data.get("content_status", "full"),
        translated_by=data.get("translated_by", ""),
        source_key=data.get("source_key", ""),
        fetched_at=data.get("fetched_at"),
        updated_at=data.get("updated_at"),
        extraction=data.get("extraction", {}),
        paragraphs=[Paragraph(en=p["en"], zh=p.get("zh", "")) for p in data["paragraphs"]],
        vocabulary=[
            VocabularyItem(
                word=v["word"],
                phonetic=v["phonetic"],
                meaning_zh=v["meaning_zh"],
                example_en=v["example_en"],
            )
            for v in data.get("vocabulary", [])
        ],
        collocations=[
            Collocation(
                phrase=c["phrase"],
                meaning_zh=c["meaning_zh"],
                example_en=c["example_en"],
            )
            for c in data.get("collocations", [])
        ],
        sentence_notes=[
            SentenceNote(
                sentence_en=s["sentence_en"],
                translation_zh=s["translation_zh"],
                analysis_zh=s["analysis_zh"],
            )
            for s in data.get("sentence_notes", [])
        ],
        image=(
            ArticleImage(
                src=image_data["src"],
                alt_en=image_data["alt_en"],
                credit=image_data["credit"],
            )
            if image_data
            else None
        ),
    )


def article_to_dict(article: Article) -> dict[str, Any]:
    data: dict[str, Any] = {
        "slug": article.slug,
        "source": article.source,
        "title_en": article.title_en,
        "title_zh": article.title_zh,
        "summary_en": article.summary_en,
        "summary_zh": article.summary_zh,
        "author": article.author,
        "published_at": article.published_at,
        "url": article.url,
        "reading_minutes": article.reading_minutes,
        "content_status": article.content_status,
        "translated_by": article.translated_by,
        "paragraphs": [{"en": p.en, "zh": p.zh} for p in article.paragraphs],
        "vocabulary": [vars(v) for v in article.vocabulary],
        "collocations": [vars(c) for c in article.collocations],
        "sentence_notes": [vars(s) for s in article.sentence_notes],
    }
    if article.image:
        data["image"] = vars(article.image)
    for key in ("source_key", "fetched_at", "updated_at", "extraction"):
        if value := getattr(article, key):
            data[key] = value
    return data


def edition_from_dict(data: dict[str, Any]) -> DailyEdition:
    return DailyEdition(
        date=data["date"],
        generation=data.get("generation", 0),
        result_revisions=data.get("result_revisions", {}),
        articles=[article_from_dict(a) for a in data["articles"]],
        briefs=[
            BriefItem(
                title_en=b["title_en"],
                title_zh=b.get("title_zh", ""),
                source=b["source"],
                url=b["url"],
                published_at=b.get("published_at"),
                source_key=b.get("source_key", ""),
                selection_reason=b.get("selection_reason", ""),
            )
            for b in data.get("briefs", [])
        ],
    )


def edition_to_dict(edition: DailyEdition) -> dict[str, Any]:
    data = {
        "date": edition.date,
        "articles": [article_to_dict(a) for a in edition.articles],
        "briefs": [
            {
                key: value
                for key, value in vars(b).items()
                if key not in {"published_at", "source_key", "selection_reason"} or value
            }
            for b in edition.briefs
        ],
    }
    if edition.generation or edition.result_revisions:
        data["generation"] = edition.generation
        data["result_revisions"] = edition.result_revisions
    return data
