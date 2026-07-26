"""Small text helpers shared across modules. Pure functions only."""

import html
import re

import nh3

_WS = re.compile(r"\s+")


def html_to_text(value: str) -> str:
    """Strip all HTML tags, unescape entities, collapse whitespace."""
    stripped = nh3.clean(value, tags=set())
    return collapse_ws(html.unescape(stripped))


def collapse_ws(value: str) -> str:
    return _WS.sub(" ", value).strip()


def slugify(value: str, max_length: int = 60) -> str:
    """ASCII slug for file names: lowercase, hyphen-separated."""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if len(value) > max_length:
        value = value[:max_length].rstrip("-")
    return value or "article"
