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


def load_env_file(path: Path | None = None) -> None:
    """Merge KEY=VALUE lines from .env.local into os.environ; existing vars win.

    仅由 CLI 命令入口调用；测试不得触达（计划 §6 约束）。
    """
    path = path or Path(".env.local")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class TranslationConfig:
    """OpenAI 兼容翻译接口配置；缺省为空，真实调用前由 CLI 校验并提示。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    max_tokens: int
    cache_dir: Path


def translation_config_from_env(environ: Mapping[str, str] | None = None) -> TranslationConfig:
    env = os.environ if environ is None else environ
    return TranslationConfig(
        base_url=env.get("TRANSLATION_API_BASE_URL", "").rstrip("/"),
        api_key=env.get("TRANSLATION_API_KEY", ""),
        model=env.get("TRANSLATION_MODEL", ""),
        timeout_seconds=float(env.get("TRANSLATION_TIMEOUT_SECONDS", "60")),
        # Claude 系后端强制要求 max_tokens；长文译文需要充足余量
        max_tokens=int(env.get("TRANSLATION_MAX_TOKENS", "8192")),
        cache_dir=Path(env.get("NEWS_DATA_DIR", "var/data")) / "translations",
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
