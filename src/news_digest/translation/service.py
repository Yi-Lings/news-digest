"""翻译执行：缓存、重试与断点续跑。

缓存键 = sha256(文章内容哈希 : 模型 : prompt 版本)；
仅校验通过的结果写入缓存，非法响应绝不落盘、不覆盖既有有效结果。
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from news_digest.models import Article, DailyEdition
from news_digest.translation.schema import (
    PROMPT_VERSION,
    InvalidTranslation,
    TranslationResult,
    apply_translation,
    parse_translation,
    result_to_dict,
)


class Translator(Protocol):
    @property
    def label(self) -> str: ...
    @property
    def model(self) -> str: ...
    def translate(self, article: Article) -> str: ...


def article_content_hash(article: Article) -> str:
    payload = article.title_en + "\n" + "\n".join(p.en for p in article.paragraphs)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_key(article: Article, model: str) -> str:
    raw = f"{article_content_hash(article)}:{model}:{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class TranslateReport:
    total: int = 0
    already_done: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def _result_from_dict(data: dict, paragraph_count: int) -> TranslationResult:
    return parse_translation(json.dumps(data, ensure_ascii=False), paragraph_count)


def translate_edition(
    edition: DailyEdition,
    translator: Translator,
    cache_dir: Path,
    *,
    limit: int | None = None,
    max_attempts: int = 2,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[DailyEdition, TranslateReport]:
    """翻译一期内容；单篇失败不阻塞其余，已翻译文章直接跳过（断点续跑）。"""
    progress = on_progress or (lambda message: None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = TranslateReport(total=len(edition.articles))
    articles: list[Article] = []
    translated_count = 0

    for article in edition.articles:
        if article.translated_by:
            report.already_done += 1
            articles.append(article)
            continue
        if limit is not None and translated_count >= limit:
            articles.append(article)
            continue

        cache_file = cache_dir / f"{cache_key(article, translator.model)}.json"
        result: TranslationResult | None = None

        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                result = _result_from_dict(cached, len(article.paragraphs))
                report.cache_hits += 1
                progress(f"✓ {article.slug}（缓存命中）")
            except (InvalidTranslation, json.JSONDecodeError):
                result = None  # 缓存损坏：当作未命中，重新请求后覆盖

        if result is None:
            error_reason = ""
            for attempt in range(1, max_attempts + 1):
                try:
                    suffix = f"，第 {attempt} 次重试" if attempt > 1 else ""
                    progress(
                        f"→ {article.slug}（{len(article.paragraphs)} 段）翻译中{suffix}…"
                    )
                    report.api_calls += 1
                    raw = translator.translate(article)
                    result = parse_translation(raw, len(article.paragraphs))
                    cache_file.write_text(
                        json.dumps(result_to_dict(result), ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                    progress(f"✓ {article.slug}")
                    break
                except Exception as error:  # 接口错误与非法响应同等处理：重试后失败
                    error_reason = f"{error.__class__.__name__}: {error}"
                    result = None
            if result is None:
                report.failed += 1
                report.failures.append((article.slug, error_reason))
                progress(f"✗ {article.slug}: {error_reason[:100]}")
                articles.append(article)
                continue

        articles.append(apply_translation(article, result, translator.label))
        report.succeeded += 1
        translated_count += 1

    updated = DailyEdition(date=edition.date, articles=articles, briefs=edition.briefs)
    return updated, report
