"""OpenAI 兼容客户端（MockTransport，离线）。"""

import json
from pathlib import Path

import httpx
import pytest

from news_digest.config import TranslationConfig
from news_digest.models import Article, Paragraph
from news_digest.translation.client import ApiTranslator, TranslationError


def _config(**overrides) -> TranslationConfig:
    values = {
        "base_url": "https://api.example.com/v1",
        "api_key": "test-key",
        "model": "demo-model",
        "timeout_seconds": 10.0,
        "max_tokens": 8192,
        "cache_dir": Path("unused"),
    }
    values.update(overrides)
    return TranslationConfig(**values)


def _article() -> Article:
    return Article(
        slug="s",
        source="BBC News",
        title_en="T",
        summary_en="S",
        author="",
        published_at="2026-07-26T00:00:00+00:00",
        url="https://example.com/s",
        reading_minutes=1,
        paragraphs=[Paragraph(en="One paragraph of text.")],
    )


def _translator(handler) -> ApiTranslator:
    return ApiTranslator(_config(), transport=httpx.MockTransport(handler))


def test_missing_config_raises():
    with pytest.raises(TranslationError, match="TRANSLATION_API_KEY"):
        ApiTranslator(_config(api_key=""))


def test_successful_call_returns_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 8192  # Anthropic 兼容后端必填
        assert "temperature" not in payload  # 推理系模型会拒绝该参数
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"ok\": true}"}}]},
        )

    translator = _translator(handler)
    assert translator.translate(_article()) == '{"ok": true}'
    assert translator.label == "demo-model@p1"


def test_http_error_raises_without_leaking_key():
    translator = _translator(lambda request: httpx.Response(429))
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert "429" in str(excinfo.value)
    assert "test-key" not in str(excinfo.value)


def test_malformed_body_raises():
    translator = _translator(lambda request: httpx.Response(200, json={"unexpected": 1}))
    with pytest.raises(TranslationError, match="结构异常"):
        translator.translate(_article())
