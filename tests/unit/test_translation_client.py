"""双协议翻译客户端（MockTransport，全离线）。"""

import json
import ssl
import threading
import time
from pathlib import Path

import httpcore
import httpx
import pytest

from news_digest.config import (
    TranslationConfig,
    normalize_translation_base_url,
    translation_config_from_env,
)
from news_digest.models import Article, Paragraph
from news_digest.translation.client import (
    ApiTranslator,
    TranslationError,
    _PinnedNetworkBackend,
    translation_cache_identity,
)
from news_digest.translation.schema import PROMPT_VERSION


def _config(**overrides) -> TranslationConfig:
    values = {
        "base_url": "https://api.example.com/v1",
        "api_key": "test-key",
        "model": "demo-model",
        "timeout_seconds": 10.0,
        "max_tokens": 8192,
        "cache_dir": Path("unused"),
        "api_type": "openai_chat",
        "stream": True,
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


def _translator(handler, **overrides) -> ApiTranslator:
    return ApiTranslator(_config(**overrides), transport=httpx.MockTransport(handler))


def test_pinned_backend_connects_only_validated_addresses(monkeypatch):
    calls = []
    stream = object()

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        calls.append((host, port))
        return stream

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    backend = _PinnedNetworkBackend(
        "api.example.com",
        443,
        ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
    )
    assert backend.connect_tcp("api.example.com", 443) is stream
    assert calls == [("93.184.216.34", 443)]
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("metadata.google.internal", 443)


def test_translator_explicitly_ignores_environment_proxies(monkeypatch):
    captured = {}
    real_client = httpx.Client

    def client(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    translator = ApiTranslator(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=_openai_sse("ok"))
        ),
    )
    assert translator.probe() == "ok"
    translator.close()
    assert captured["trust_env"] is False


def test_formal_request_uses_bounded_connect_and_read_timeouts():
    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == 10.0
        assert timeout["read"] == 30.0
        assert timeout["write"] == 180.0
        return httpx.Response(200, json=_openai_non_stream("ok"))

    translator = _translator(handler, stream=False, timeout_seconds=180.0)
    assert translator.probe() == "ok"


def _openai_sse(*chunks: str) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]})
        for chunk in chunks
    ]
    lines.append('data: {"choices": [], "usage": {"total_tokens": 1}}')
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _anthropic_sse(*chunks: str) -> bytes:
    events = [
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}},
        )
        for chunk in chunks
    ]
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(
        f"event: {event}\ndata: {json.dumps(payload)}\n\n" for event, payload in events
    ).encode()


def _openai_non_stream(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _anthropic_non_stream(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def test_translation_config_defaults_openai_stream_and_normalizes_base():
    config = translation_config_from_env(
        {
            "TRANSLATION_API_BASE_URL": "https://api.example.com/openai/",
            "TRANSLATION_API_KEY": "key",
            "TRANSLATION_MODEL": "model",
        }
    )
    assert config.api_type == "openai_chat"
    assert config.stream is True
    assert config.base_url == "https://api.example.com/openai/v1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/v1"),
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        ("https://gateway.example.com/openai", "https://gateway.example.com/openai/v1"),
        ("https://gateway.example.com/openai/v1", "https://gateway.example.com/openai/v1"),
    ],
)
def test_normalize_translation_base_url_preserves_safe_prefix(raw, expected):
    assert normalize_translation_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://api.example.com/v1",
        "https://user:secret@api.example.com/v1",
        "https://api.example.com/v1?key=secret",
        "https://api.example.com/v1#fragment",
        "https://api.example.com/v1/chat/completions",
        "https://api.example.com/messages",
        "https://api.example.com/v1/responses",
        "https://api.example.com/v1/v1",
        "https://api.example.com/v1/proxy",
        "https://api.example.com/openai//v1",
        "https://api.example.com/openai/v1//",
        "https://api.example.com/openai/%2e%2e/v1",
        "https://例子.example/v1",
    ],
)
def test_normalize_translation_base_url_rejects_non_base_or_unsafe_url(raw):
    with pytest.raises(ValueError, match="TRANSLATION_API_BASE_URL"):
        normalize_translation_base_url(raw)


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_translation_stream_env_is_strict_boolean(raw, expected):
    config = translation_config_from_env({"TRANSLATION_STREAM": raw})
    assert config.stream is expected


def test_translation_stream_env_rejects_other_values():
    with pytest.raises(ValueError, match="TRANSLATION_STREAM must be true or false"):
        translation_config_from_env({"TRANSLATION_STREAM": "1"})


def test_translation_config_rejects_invalid_api_type():
    with pytest.raises(ValueError, match="TRANSLATION_API_TYPE"):
        translation_config_from_env({"TRANSLATION_API_TYPE": "claude"})


def test_missing_config_raises():
    with pytest.raises(TranslationError, match="TRANSLATION_API_KEY"):
        ApiTranslator(_config(api_key=""))


def test_request_timeout_waits_until_worker_termination_is_confirmed():
    def slow(request):
        time.sleep(0.05)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    translator = _translator(slow, stream=False, timeout_seconds=0.01)
    started = time.monotonic()
    with pytest.raises(TranslationError) as excinfo:
        translator.probe()
    assert excinfo.value.category == "total_timeout"
    assert excinfo.value.termination_confirmed is True
    assert 0.04 <= time.monotonic() - started < 0.2


def test_unconfirmed_timeout_blocks_follow_up_request(monkeypatch):
    release = threading.Event()
    calls = 0

    class BlockingClient:
        def close(self):
            release.wait(1.0)

    translator = _translator(
        lambda request: httpx.Response(200, json=_openai_non_stream("ok")),
        stream=False,
        timeout_seconds=0.01,
    )
    monkeypatch.setattr(translator, "_new_client", BlockingClient)
    def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        release.wait(1.0)
        return "ok"

    monkeypatch.setattr(translator, "_request_text_blocking", request)
    monkeypatch.setattr("news_digest.translation.client._TERMINATION_GRACE_SECONDS", 0.01)

    try:
        with pytest.raises(TranslationError) as excinfo:
            translator.probe()
        assert excinfo.value.category == "termination_unconfirmed"
        assert excinfo.value.termination_confirmed is False
        with pytest.raises(TranslationError) as blocked:
            translator.probe()
        assert blocked.value.category == "termination_unconfirmed"
        assert calls == 1
    finally:
        release.set()


def test_completed_request_does_not_wait_for_blocking_client_close(monkeypatch):
    release = threading.Event()

    class BlockingClient:
        def close(self):
            release.wait(1.0)

    translator = _translator(
        lambda request: httpx.Response(200, json=_openai_non_stream("ok")),
        stream=False,
        timeout_seconds=0.1,
    )
    monkeypatch.setattr(translator, "_new_client", BlockingClient)
    monkeypatch.setattr(translator, "_request_text_blocking", lambda *args, **kwargs: "ok")

    started = time.monotonic()
    try:
        assert translator.probe() == "ok"
        assert time.monotonic() - started < 0.05
    finally:
        release.set()


def test_close_does_not_wait_for_blocking_active_client():
    release = threading.Event()

    class BlockingClient:
        def close(self):
            release.wait(1.0)

    translator = _translator(
        lambda request: httpx.Response(200, json=_openai_non_stream("ok")),
        stream=False,
    )
    with translator._state_lock:
        translator._active_clients.add(BlockingClient())

    started = time.monotonic()
    try:
        translator.close()
        assert time.monotonic() - started < 0.05
        with pytest.raises(TranslationError, match="翻译客户端已关闭"):
            translator.probe()
    finally:
        release.set()


def test_dns_timeout_waits_until_worker_termination_is_confirmed():
    def slow_resolver(hostname, port):
        time.sleep(0.05)
        return ["93.184.216.34"]

    started = time.monotonic()
    translator = ApiTranslator(_config(timeout_seconds=0.01), resolver=slow_resolver)
    with pytest.raises(TranslationError) as excinfo:
        translator.probe()
    assert excinfo.value.category == "total_timeout"
    assert excinfo.value.termination_confirmed is True
    assert 0.04 <= time.monotonic() - started < 0.2


def test_confirmed_hard_timeout_allows_next_request(monkeypatch):
    calls = 0
    release = threading.Event()

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait(1.0)
        return httpx.Response(200, json=_openai_non_stream("ok"))

    translator = _translator(handler, stream=False, timeout_seconds=0.01)
    original_new_client = translator._new_client

    class ClosingClient:
        def __init__(self):
            self.inner = original_new_client()

        def close(self):
            release.set()
            self.inner.close()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(translator, "_new_client", ClosingClient)
    with pytest.raises(TranslationError, match="硬总时限") as excinfo:
        translator.probe()
    assert excinfo.value.category == "total_timeout"
    assert excinfo.value.termination_confirmed is True
    assert translator.probe() == "ok"
    assert calls == 2


def test_cancelled_request_must_confirm_worker_termination(monkeypatch):
    release = threading.Event()

    def blocking(request):
        release.wait(1.0)
        return httpx.Response(200, json=_openai_non_stream("ok"))

    translator = _translator(blocking, stream=False, timeout_seconds=1.0)
    monkeypatch.setattr("news_digest.translation.client._TERMINATION_GRACE_SECONDS", 0.01)

    try:
        with pytest.raises(TranslationError) as excinfo:
            translator.translate_with_cancel(_article(), cancel_requested=lambda: True)
        assert excinfo.value.category == "termination_unconfirmed"
        assert excinfo.value.termination_confirmed is False
    finally:
        release.set()


def test_stream_hard_total_deadline_preserves_response_started():
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            time.sleep(0.05)
            yield b'data: {"choices": [{"delta": {"content": "late"}}]}\n\n'

    translator = _translator(
        lambda request: httpx.Response(200, stream=SlowStream()),
        timeout_seconds=0.01,
    )
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert excinfo.value.category == "total_timeout"
    assert excinfo.value.response_started is True


def test_tls_failure_has_distinct_safe_category():
    def fail(request):
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("tls", request=request) from cause

    translator = _translator(fail, stream=False)
    with pytest.raises(TranslationError) as excinfo:
        translator.probe()
    assert excinfo.value.category == "tls"
    assert "certificate verify failed" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("api_type", "stream", "expected_path"),
    [
        ("openai_chat", True, "/openai/v1/chat/completions"),
        ("openai_chat", False, "/openai/v1/chat/completions"),
        ("anthropic_messages", True, "/openai/v1/messages"),
        ("anthropic_messages", False, "/openai/v1/messages"),
    ],
)
def test_provider_modes_switch_path_headers_payload_and_parser(api_type, stream, expected_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected_path
        payload = json.loads(request.content)
        assert payload["stream"] is stream
        if api_type == "openai_chat":
            assert request.headers["authorization"] == "Bearer test-key"
            assert "x-api-key" not in request.headers
            assert "anthropic-version" not in request.headers
            assert payload["messages"][0]["role"] == "system"
            assert "system" not in payload
            if stream:
                return httpx.Response(200, content=_openai_sse('{"ok"', ": true}"))
            return httpx.Response(200, json=_openai_non_stream('{"ok": true}'))
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        assert isinstance(payload["system"], str)
        assert payload["messages"][0]["role"] == "user"
        if stream:
            return httpx.Response(200, content=_anthropic_sse('{"ok"', ": true}"))
        return httpx.Response(200, json=_anthropic_non_stream('{"ok": true}'))

    translator = _translator(
        handler,
        api_type=api_type,
        stream=stream,
        base_url="https://api.example.com/openai",
        model="claude-or-gpt-model",
    )
    assert translator.translate(_article()) == '{"ok": true}'
    assert translator.label == f"claude-or-gpt-model@{PROMPT_VERSION}"


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "request"),
        (401, "authentication"),
            (403, "upstream"),
        (404, "endpoint"),
        (429, "rate_limit"),
        (503, "provider"),
        (501, "provider_permanent"),
    ],
)
def test_http_errors_are_structured_without_leaking_response_or_key(status, category):
    secret_body = "upstream-secret-response test-key"
    translator = _translator(lambda request: httpx.Response(status, text=secret_body))
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    error = excinfo.value
    assert error.status == status
    assert error.status_code == status
    assert error.category == category
    assert error.response_started is False
    assert str(status) in str(error)
    assert secret_body not in str(error)
    assert "test-key" not in str(error)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2", 2.0), ("5", 5.0), ("6", None), ("999999", None), ("tomorrow", None)],
)
def test_retry_after_only_accepts_small_delta_seconds(raw, expected):
    translator = _translator(
        lambda request: httpx.Response(429, headers={"Retry-After": raw})
    )
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert excinfo.value.retry_after == expected


@pytest.mark.parametrize(
    ("api_type", "stream", "body"),
    [
        ("openai_chat", True, _anthropic_sse("wrong protocol")),
        ("anthropic_messages", True, _openai_sse("wrong protocol")),
        ("openai_chat", False, _anthropic_non_stream("wrong protocol")),
        ("anthropic_messages", False, _openai_non_stream("wrong protocol")),
    ],
)
def test_protocol_format_mismatch_is_rejected(api_type, stream, body):
    def handler(request: httpx.Request) -> httpx.Response:
        if stream:
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    translator = _translator(handler, api_type=api_type, stream=stream)
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert excinfo.value.category == "response_format"
    assert "wrong protocol" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("api_type", "stream"),
    [
        ("openai_chat", True),
        ("openai_chat", False),
        ("anthropic_messages", True),
        ("anthropic_messages", False),
    ],
)
def test_probe_is_fixed_hi_small_output_and_exactly_one_request(api_type, stream):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 8
        assert payload["messages"][-1]["content"] == "Hi"
        if api_type == "openai_chat":
            body = _openai_sse("Hello") if stream else _openai_non_stream("Hello")
        else:
            body = _anthropic_sse("Hello") if stream else _anthropic_non_stream("Hello")
        if stream:
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    translator = _translator(handler, api_type=api_type, stream=stream)
    assert translator.probe() == "Hello"
    assert calls == 1
    with pytest.raises(TypeError):
        translator.probe("arbitrary message")  # type: ignore[call-arg]
    assert calls == 1


def test_probe_does_not_retry_transient_failure():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    translator = _translator(handler)
    with pytest.raises(TranslationError) as excinfo:
        translator.probe()
    assert excinfo.value.category == "provider"
    assert calls == 1


def test_anthropic_stream_error_event_is_not_a_retryable_http_failure():
    body = b'event: error\ndata: {"type": "error", "error": {"message": "secret"}}\n\n'
    translator = _translator(
        lambda request: httpx.Response(200, content=body),
        api_type="anthropic_messages",
    )
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert excinfo.value.category == "response_format"
    assert "secret" not in str(excinfo.value)


def test_stream_failure_after_content_is_marked_started():
    class FailingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices": [{"delta": {"content": "partial"}}]}\n\n'
            raise httpx.ReadTimeout("secret upstream detail")

    translator = _translator(lambda request: httpx.Response(200, stream=FailingStream()))
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    error = excinfo.value
    assert error.category == "read_timeout"
    assert error.response_started is True
    assert "secret upstream detail" not in str(error)


def test_stream_failure_before_content_is_marked_started():
    class FailingStream(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("secret upstream detail")
            yield b""  # pragma: no cover

    translator = _translator(lambda request: httpx.Response(200, stream=FailingStream()))
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    error = excinfo.value
    assert error.category == "read_timeout"
    assert error.response_started is True
    assert "secret upstream detail" not in str(error)


def test_non_stream_failure_after_headers_is_marked_started():
    class FailingBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("secret upstream detail")
            yield b""  # pragma: no cover

    translator = _translator(
        lambda request: httpx.Response(200, stream=FailingBody()),
        stream=False,
    )
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    error = excinfo.value
    assert error.category == "read_timeout"
    assert error.response_started is True
    assert "secret upstream detail" not in str(error)


def test_redirect_is_not_followed_with_credentials():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "https://other.example.com/v1"})

    translator = _translator(handler)
    with pytest.raises(TranslationError) as excinfo:
        translator.translate(_article())
    assert excinfo.value.status == 307
    assert calls == 1


def test_cache_identity_is_safe_normalized_and_isolated_by_protocol_and_model():
    openai = translation_cache_identity(
        "openai_chat", "https://api.example.com/openai", "gpt-5"
    )
    same_openai = translation_cache_identity(
        "openai_chat", "https://api.example.com/openai/v1/", "gpt-5"
    )
    claude = translation_cache_identity(
        "anthropic_messages", "https://api.example.com/openai/v1", "claude-sonnet-4-5"
    )
    other_model = translation_cache_identity(
        "anthropic_messages", "https://api.example.com/openai/v1", "claude-opus-4-1"
    )
    other_gateway = translation_cache_identity(
        "openai_chat", "https://other.example.com/v1", "gpt-5"
    )
    assert openai == same_openai
    assert len({openai, claude, other_model, other_gateway}) == 4
    assert "api.example.com" not in openai
    assert "gpt-5" not in openai
