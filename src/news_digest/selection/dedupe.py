"""Cross-source deduplication: canonical URL, content hash, title similarity."""

import hashlib
from difflib import SequenceMatcher

from news_digest.models import Candidate

_TITLE_SIMILARITY_THRESHOLD = 0.92


def content_hash(candidate: Candidate) -> str:
    normalized = f"{candidate.title.lower()}\n{candidate.summary.lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _similar(title_a: str, title_b: str) -> bool:
    ratio = SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio()
    return ratio >= _TITLE_SIMILARITY_THRESHOLD


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Keep first occurrence; input order defines priority."""
    kept: list[Candidate] = []
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    for candidate in candidates:
        if candidate.url in seen_urls:
            continue
        digest = content_hash(candidate)
        if digest in seen_hashes:
            continue
        if any(_similar(candidate.title, existing.title) for existing in kept):
            continue
        kept.append(candidate)
        seen_urls.add(candidate.url)
        seen_hashes.add(digest)
    return kept
