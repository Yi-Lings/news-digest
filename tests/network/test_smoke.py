"""真实公网连通性冒烟测试：默认跳过，运行 `uv run pytest -m network`。

用途：验证各来源 RSS 当前可用、窗口内有内容、格式仍能被解析。
需要可访问外网的环境；如走代理请设置 NEWS_HTTP_PROXY 或 HTTPS_PROXY。
"""

import pytest

from news_digest.config import fetch_config_from_env
from news_digest.sources.feeds import parse_feed
from news_digest.sources.http import build_client, safe_get
from news_digest.sources.registry import SOURCES

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def client():
    with build_client(fetch_config_from_env().proxy) as client:
        yield client


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.key)
def test_feed_reachable_and_parses(client, source):
    raw = safe_get(client, source.feed_url, source.allowed_domains)
    candidates = parse_feed(raw, source)
    assert candidates, f"{source.name} 无可解析条目"
    first = candidates[0]
    assert first.title and first.url and first.published_at_utc
