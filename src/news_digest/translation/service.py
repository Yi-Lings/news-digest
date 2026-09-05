"""Translation execution with validated caches and bounded sentence repair."""

import copy
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from news_digest.models import Article, DailyEdition, article_source_hash
from news_digest.translation.client import TranslationError
from news_digest.translation.schema import (
    PROMPT_VERSION,
    SPLITTER_VERSION,
    InvalidTranslation,
    TranslationResult,
    apply_translation,
    parse_sentence_repair,
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
    return article_source_hash(article)


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


def _result_from_dict(
    data: dict, article: Article, frozen_counts: list[int] | None = None
) -> TranslationResult:
    if not isinstance(data, dict):
        raise InvalidTranslation("Invalid cached translation")
    cached_splitter = data.get("splitter_version")
    if cached_splitter is not None and cached_splitter != SPLITTER_VERSION:
        raise InvalidTranslation("Cached splitter version mismatch")
    return parse_translation(
        json.dumps(data, ensure_ascii=False),
        len(article.paragraphs),
        [paragraph.en for paragraph in article.paragraphs],
        frozen_counts,
    )


def _content_gates(article: Article, result: TranslationResult) -> tuple[list[str], list[str]]:
    from news_digest.translation import quality

    return quality.check_translation([p.en for p in article.paragraphs], result)


def _attempt_with_gates(
    article: Article,
    translator: Translator,
    raw: str,
    *,
    cancel_requested: Callable[[], bool] | None,
    frozen_counts: list[int] | None,
) -> TranslationResult:
    # Soft diagnostics are observational and never cause another provider request.
    return parse_translation(
        raw,
        len(article.paragraphs),
        [p.en for p in article.paragraphs],
        frozen_counts,
    )


def _write_json_cache(cache_file: Path, data: dict) -> None:
    temporary = cache_file.with_name(f".{cache_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(cache_file)
    finally:
        temporary.unlink(missing_ok=True)


def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise TranslationError("Translation cancelled", category="request_cancelled")


def translate_article_once(
    article: Article,
    translator: Translator,
    cache_dir: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    frozen_counts: list[int] | None = None,
    frozen_sentences: list[list[str]] | None = None,
    on_result: Callable[[TranslationResult], None] | None = None,
    on_request: Callable[[int, str | None, str, float], None] | None = None,
    force: bool = False,
) -> tuple[Article, bool]:
    """Repair invalid sentence slots within the article's single request/time budget."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    identity = getattr(translator, "cache_identity", translator.model)
    cache_file = cache_dir / f"{cache_key(article, identity)}.json"
    _check_cancel(cancel_requested)
    if not force and cache_file.is_file():
        try:
            result = _result_from_dict(
                json.loads(cache_file.read_text(encoding="utf-8")),
                article,
                frozen_counts,
            )
        except (InvalidTranslation, ValueError):
            pass
        else:
            if on_result is not None:
                on_result(result)
            return apply_translation(article, result, translator.label), True

    started = time.monotonic()
    budget = getattr(translator, "timeout_seconds", 600.0)
    last_error: InvalidTranslation | None = None
    raw = ""
    result = None
    for request_number in range(1, 4):
        _check_cancel(cancel_requested)
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            raise TranslationError(
                "Translation execution deadline exceeded",
                category="total_timeout",
            )
        repair = getattr(translator, "translate_sentence_repair", None)
        local = (
            last_error is not None
            and last_error.sentence_failures
            and frozen_sentences is not None
            and callable(repair)
        )
        target = last_error.sentence_failures[0] if local else None
        target_id = f"P{target[0]}S{target[1]}" if target else None
        if on_request is not None:
            on_request(request_number, target_id, "started", time.monotonic() - started)
        outcome = "failed"
        try:
            if target is not None:
                p, s = target
                sentences = frozen_sentences[p - 1]
                prior = last_error.candidate["sentences_zh"][p - 1][s - 1]
                response = repair(
                    title_en=article.title_en,
                    paragraph_index=p,
                    sentence_index=s,
                    source_sentence=sentences[s - 1],
                    previous_translation=prior if isinstance(prior, str) else "",
                    context_before=sentences[s - 2] if s > 1 else "",
                    context_after=sentences[s] if s < len(sentences) else "",
                    evidence=[{"code": "CONTENT_FIELD_MISSING", "target": target_id}],
                    cancel_requested=cancel_requested,
                    timeout_seconds=remaining,
                )
                fixed = parse_sentence_repair(response, paragraph_index=p, sentence_index=s)
                candidate = copy.deepcopy(last_error.candidate)
                candidate["sentences_zh"][p - 1][s - 1] = fixed.translation_zh
                raw = json.dumps(candidate, ensure_ascii=False)
            else:
                request = getattr(translator, "translate_request", None)
                feedback = getattr(translator, "translate_with_feedback", None)
                cancellable = getattr(translator, "translate_with_cancel", None)
                if callable(request):
                    raw = request(
                        article,
                        frozen_sentences=frozen_sentences,
                        cancel_requested=cancel_requested,
                        timeout_seconds=remaining,
                        feedback=str(last_error) if last_error else None,
                        previous_output=raw,
                    )
                elif last_error is not None and callable(feedback):
                    raw = feedback(
                        article,
                        str(last_error),
                        cancel_requested=cancel_requested,
                        previous_output=raw,
                    )
                elif last_error is not None:
                    raise last_error
                elif cancel_requested is not None and callable(cancellable):
                    raw = cancellable(article, cancel_requested=cancel_requested)
                else:
                    raw = translator.translate(article)
            _check_cancel(cancel_requested)
            if time.monotonic() - started >= budget:
                raise TranslationError(
                    "Translation execution deadline exceeded", category="total_timeout"
                )
            result = parse_translation(
                raw,
                len(article.paragraphs),
                [p.en for p in article.paragraphs],
                frozen_counts,
            )
            outcome = "succeeded"
        except InvalidTranslation as error:
            # A bad repair response must retain the candidate and its original target.
            if target is None or error.candidate is not None:
                last_error = error
            outcome = "invalid_sentence" if target else "invalid_structure"
            if target is not None and error.candidate is not None:
                if target not in error.sentence_failures:
                    outcome = "succeeded"
        except TranslationError as error:
            outcome = error.category
            raise
        finally:
            if on_request is not None:
                try:
                    on_request(request_number, target_id, outcome, time.monotonic() - started)
                except Exception:
                    # Recording must not replace cancellation or uncertain termination.
                    if outcome in {"request_cancelled", "termination_unconfirmed", "total_timeout"}:
                        logging.getLogger(__name__).warning("Translation request audit failed")
                    else:
                        raise
        if result is not None:
            break
    if result is None:
        raise last_error or InvalidTranslation("Translation request budget exhausted")
    _check_cancel(cancel_requested)
    if on_result is not None:
        on_result(result)
    _write_json_cache(cache_file, result_to_dict(result))
    return apply_translation(article, result, translator.label), False


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
        identity = getattr(translator, "cache_identity", translator.model)
        cache_file = cache_dir / f"{cache_key(article, identity)}.json"
        if article.translated_by and not force:
            # A prompt-version bump must invalidate previously persisted translations.
            # Current-version cache is the only safe signal that the article already
            # passed the current translation contract.
            if cache_file.is_file():
                try:
                    _result_from_dict(
                        json.loads(cache_file.read_text(encoding="utf-8")),
                        article,
                    )
                except (InvalidTranslation, json.JSONDecodeError):
                    pass
                else:
                    report.already_done += 1
                    articles.append(article)
                    continue
        if not force and limit is not None and translated_count >= limit:
            articles.append(article)
            continue

        result: TranslationResult | None = None

        if not force and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                result = _result_from_dict(cached, article)
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
                    result = _attempt_with_gates(
                        article,
                        translator,
                        raw,
                        cancel_requested=None,
                        frozen_counts=None,
                    )
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
