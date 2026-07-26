"""OpenAI 兼容客户端（MockTransport，离线）。"""

import json
from pathlib import Path

import httpx
import pytest

from news_digest.config import TranslationConfig
from news_digest.models import Article, Paragraph
from news_digest.translation.client import ApiTranslator, TranslationError
from news_digest.translation.schema import PROMPT_VERSION


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


def _sse(*chunks: str) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]})
        for chunk in chunks
    ]
    lines.append('data: {"choices": [], "usage": {"total_tokens": 1}}')  # 非内容块应被跳过
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


def test_successful_stream_returns_joined_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 8192  # Anthropic 兼容后端必填
        assert payload["stream"] is True  # 流式：避免反代读超时 504
        assert "temperature" not in payload  # 推理系模型会拒绝该参数
        return httpx.Response(200, content=_sse('{"ok"', ": true}"))

    translator = _translator(handler)
    assert translator.translate(_article()) == '{"ok": true}'
    assert translator.label == f"demo-model@{PROMPT_VERSION}"


def test_http_error_raises_without_leaking_key():
    translator = _translator(lambda request: httpx.Response(429))
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert "429" in str(excinfo.value)
    assert "test-key" not in str(excinfo.value)


def test_non_stream_body_yields_empty_and_raises():
    translator = _translator(lambda request: httpx.Response(200, json={"unexpected": 1}))
    with pytest.raises(TranslationError, match="无内容"):
        translator.translate(_article())
