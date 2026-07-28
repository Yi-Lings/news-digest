"""翻译执行：缓存、重试与断点续跑。

缓存键 = sha256(文章内容哈希 : 接口缓存身份 : prompt 版本)；
仅校验通过的结果写入缓存，非法响应绝不落盘、不覆盖既有有效结果。
"""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from news_digest.models import Article, DailyEdition
from news_digest.translation.client import TranslationError
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
    @property
    def cache_identity(self) -> str: ...
    def translate(self, article: Article) -> str: ...


def article_content_hash(article: Article) -> str:
    payload = (
        article.title_en
        + "\n"
        + article.summary_en
        + "\n"
        + "\n".join(p.en for p in article.paragraphs)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_key(article: Article, cache_identity: str) -> str:
    raw = f"{article_content_hash(article)}:{cache_identity}:{PROMPT_VERSION}"
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


_RETRYABLE_TIMEOUT_CATEGORIES = {
    "connection_timeout",
    "read_timeout",
}
_RECOVERABLE_PROVIDER_STATUSES = {500, 502, 503, 504}
_MAX_TRANSLATION_ATTEMPTS = 3
_MAX_RETRY_ELAPSED_SECONDS = 95.0
_MAX_RETRY_AFTER_SECONDS = 5.0
_DEFAULT_RETRY_DELAY_SECONDS = 0.25


def _should_retry_translation(error: Exception) -> bool:
    if not isinstance(error, TranslationError) or error.response_started:
        return False
    if error.category in _RETRYABLE_TIMEOUT_CATEGORIES:
        return True
    if error.category == "rate_limit":
        return error.status == 429
    if error.category == "provider":
        return error.status in _RECOVERABLE_PROVIDER_STATUSES
    return False


def _retry_delay(error: TranslationError, attempt: int) -> float:
    if (
        error.retry_after is not None
        and 0 <= error.retry_after <= _MAX_RETRY_AFTER_SECONDS
    ):
        return error.retry_after
    return min(_DEFAULT_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)), 2.0)


def translate_edition(
    edition: DailyEdition,
    translator: Translator,
    cache_dir: Path,
    *,
    limit: int | None = None,
    max_attempts: int = _MAX_TRANSLATION_ATTEMPTS,
    max_retry_elapsed_seconds: float = _MAX_RETRY_ELAPSED_SECONDS,
    on_progress: Callable[[str], None] | None = None,
    redo: frozenset[str] = frozenset(),
) -> tuple[DailyEdition, TranslateReport]:
    """翻译一期内容；单篇失败不阻塞其余，已翻译文章直接跳过（断点续跑）。

    redo 中的 slug 强制重新请求（跳过缓存读取、覆盖已有结果），不受 limit 约束。
    """
    progress = on_progress or (lambda message: None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = TranslateReport(total=len(edition.articles))
    articles: list[Article] = []
    translated_count = 0

    for article in edition.articles:
        force = article.slug in redo
        if article.translated_by and not force:
            report.already_done += 1
            articles.append(article)
            continue
        if not force and limit is not None and translated_count >= limit:
            articles.append(article)
            continue

        identity = getattr(translator, "cache_identity", translator.model)
        cache_file = cache_dir / f"{cache_key(article, identity)}.json"
        result: TranslationResult | None = None

        if not force and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                result = _result_from_dict(cached, len(article.paragraphs))
                report.cache_hits += 1
                progress(f"✓ {article.slug}（缓存命中）")
            except (InvalidTranslation, json.JSONDecodeError):
                result = None  # 缓存损坏：当作未命中，重新请求后覆盖

        if result is None:
            error_reason = ""
            started_at = time.monotonic()
            attempt_limit = min(max(1, max_attempts), _MAX_TRANSLATION_ATTEMPTS)
            retry_elapsed_limit = min(
                max(0.0, max_retry_elapsed_seconds),
                _MAX_RETRY_ELAPSED_SECONDS,
            )
            for attempt in range(1, attempt_limit + 1):
                remaining_retry_budget: float | None = None
                if attempt > 1:
                    remaining_retry_budget = retry_elapsed_limit - (
                        time.monotonic() - started_at
                    )
                    if remaining_retry_budget <= 0:
                        break
                try:
                    suffix = f"，第 {attempt} 次重试" if attempt > 1 else ""
                    progress(
                        f"→ {article.slug}（{len(article.paragraphs)} 段）翻译中{suffix}…"
                    )
                    report.api_calls += 1
                    translate_with_timeout = getattr(translator, "translate_with_timeout", None)
                    if remaining_retry_budget is not None and callable(translate_with_timeout):
                        raw = translate_with_timeout(
                            article,
                            timeout_seconds=remaining_retry_budget,
                        )
                    else:
                        raw = translator.translate(article)
                    result = parse_translation(raw, len(article.paragraphs))
                    cache_file.write_text(
                        json.dumps(result_to_dict(result), ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                    progress(f"✓ {article.slug}")
                    break
                except Exception as error:
                    error_reason = f"{error.__class__.__name__}: {error}"
                    result = None
                    if attempt >= attempt_limit or not _should_retry_translation(error):
                        break
                    delay = _retry_delay(error, attempt)
                    elapsed = time.monotonic() - started_at
                    if elapsed + delay > retry_elapsed_limit:
                        break
                    time.sleep(delay)
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
