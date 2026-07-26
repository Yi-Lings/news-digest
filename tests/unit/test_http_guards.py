"""Offline tests for the hardened HTTP layer (no DNS, no sockets)."""

import httpx
import pytest

from news_digest.sources.http import (
    FetchError,
    assert_public_host,
    host_allowed,
    proxy_active,
    safe_get,
)

ALLOWED = ("example.com",)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _no_dns(monkeypatch):
    monkeypatch.setattr("news_digest.sources.http.assert_public_host", lambda host: None)


def test_host_allowed_exact_and_subdomain():
    assert host_allowed("example.com", ALLOWED)
    assert host_allowed("www.example.com", ALLOWED)
    assert not host_allowed("evil-example.com", ALLOWED)
    assert not host_allowed("example.com.evil.net", ALLOWED)


def test_private_and_loopback_addresses_blocked():
    for host in ["127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.0.5", "0.0.0.0"]:
        with pytest.raises(FetchError):
            assert_public_host(host)


def test_public_literal_address_passes():
    assert_public_host("93.184.216.34")


def test_scheme_rejected(monkeypatch):
    _no_dns(monkeypatch)
    with _client(lambda request: httpx.Response(200)) as client:
        with pytest.raises(FetchError, match="协议"):
            safe_get(client, "ftp://example.com/feed", ALLOWED)


def test_domain_outside_allowlist_rejected(monkeypatch):
    _no_dns(monkeypatch)
    with _client(lambda request: httpx.Response(200)) as client:
        with pytest.raises(FetchError, match="allowlist"):
            safe_get(client, "https://other.net/feed", ALLOWED)


def test_redirect_offsite_rejected(monkeypatch):
    _no_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.net/x"})

    with _client(handler) as client:
        with pytest.raises(FetchError, match="allowlist"):
            safe_get(client, "https://example.com/feed", ALLOWED)


def test_redirect_followed_within_allowlist(monkeypatch):
    _no_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://example.com/new"})
        return httpx.Response(200, content=b"ok")

    with _client(handler) as client:
        assert safe_get(client, "https://example.com/old", ALLOWED) == b"ok"


def test_redirect_loop_capped(monkeypatch):
    _no_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    with _client(handler) as client:
        with pytest.raises(FetchError, match="重定向超过"):
            safe_get(client, "https://example.com/loop", ALLOWED)


def test_oversized_response_rejected(monkeypatch):
    _no_dns(monkeypatch)
    big = b"x" * (3_000_001)

    with _client(lambda request: httpx.Response(200, content=big)) as client:
        with pytest.raises(FetchError, match="大小上限"):
            safe_get(client, "https://example.com/big", ALLOWED)


def test_proxy_active_detection(monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    assert not proxy_active(None)
    assert proxy_active("http://127.0.0.1:2231")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    assert proxy_active(None)


def test_skip_ip_check_delegates_dns_to_proxy(monkeypatch):
    def boom(host):
        raise FetchError("本地 DNS 校验不应被调用")

    monkeypatch.setattr("news_digest.sources.http.assert_public_host", boom)
    with _client(lambda request: httpx.Response(200, content=b"ok")) as client:
        assert safe_get(client, "https://example.com/x", ALLOWED, skip_ip_check=True) == b"ok"
        with pytest.raises(FetchError, match="不应被调用"):
            safe_get(client, "https://example.com/x", ALLOWED)
