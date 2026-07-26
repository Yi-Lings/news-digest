"""Configuration loading and validation."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SITE_NAME = "Cheapcoding News"
SITE_TAGLINE = "每日双语新闻"


@dataclass(frozen=True)
class BuildConfig:
    """Settings needed by the static site build; no secrets involved."""

    output_root: Path
    site_url: str


def build_config_from_env(environ: Mapping[str, str] | None = None) -> BuildConfig:
    """Read build settings from the given mapping (defaults to os.environ).

    Missing values fall back to local-development defaults so that commands
    which do not need them are never blocked.
    """
    env = os.environ if environ is None else environ
    return BuildConfig(
        output_root=Path(env.get("NEWS_OUTPUT_PATH", "var/site")),
        site_url=env.get("NEWS_SITE_URL", "http://127.0.0.1:8000").rstrip("/"),
    )


@dataclass(frozen=True)
class FetchConfig:
    """Settings for the real-news fetch stage."""

    proxy: str | None
    window_hours: int
    timezone: str
    data_dir: Path


def fetch_config_from_env(environ: Mapping[str, str] | None = None) -> FetchConfig:
    env = os.environ if environ is None else environ
    return FetchConfig(
        proxy=env.get("NEWS_HTTP_PROXY") or None,
        window_hours=int(env.get("NEWS_FETCH_WINDOW_HOURS", "24")),
        timezone=env.get("NEWS_TIMEZONE") or "Asia/Shanghai",
        data_dir=Path(env.get("NEWS_DATA_DIR", "var/data")),
    )
