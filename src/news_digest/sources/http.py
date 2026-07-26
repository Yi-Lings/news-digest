"""Hardened HTTP fetching: domain allowlist, private-address blocking, limits.

已知残余风险：先解析校验、再由 httpx 连接存在 DNS TOCTOU 窗口；
对个人只读抓取工具属可接受范围，不为此引入自定义连接层。
"""

import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx

from news_digest import __version__

DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
USER_AGENT = f"news-digest/{__version__} (personal English-learning digest)"


class FetchError(RuntimeError):
    """Any fetch failure the pipeline should degrade on."""


def proxy_active(explicit_proxy: str | None) -> bool:
    """代理是否生效：显式配置或环境变量（HTTPS_PROXY 等）任一即视为生效。

    fake-ip 模式的代理（如 Clash 系）会让本地 DNS 返回 198.18.0.0/15 假地址，
    此时本地公网校验无意义且必然误拦，应交由代理侧解析；域名 allowlist 不受影响。
    """
    if explicit_proxy:
        return True
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        if os.environ.get(name) or os.environ.get(name.lower()):
            return True
    return False


def build_client(proxy: str | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
        follow_redirects=False,
        proxy=proxy,
        trust_env=True,
    )


def host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


def assert_public_host(host: str) -> None:
    """Reject hosts that resolve to loopback/private/link-local/reserved ranges."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as error:
        raise FetchError(f"DNS 解析失败：{host}") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise FetchError(f"目标解析到非公网地址，已阻断：{host} -> {address}")


def _validate_url(url: str, allowed_domains: tuple[str, ...], check_ip: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise FetchError(f"不允许的协议：{url}")
    if not parsed.hostname:
        raise FetchError(f"无效 URL：{url}")
    if not host_allowed(parsed.hostname, allowed_domains):
        raise FetchError(f"域名不在 allowlist：{parsed.hostname}")
    if check_ip:
        assert_public_host(parsed.hostname)


def safe_get(
    client: httpx.Client,
    url: str,
    allowed_domains: tuple[str, ...],
    *,
    skip_ip_check: bool = False,
) -> bytes:
    """GET with per-hop validation, redirect cap, and response size cap.

    skip_ip_check 仅应在代理生效时为 True（见 proxy_active）。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current, allowed_domains, check_ip=not skip_ip_check)
        try:
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(f"重定向缺少 Location：{current}")
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code != 200:
                    raise FetchError(f"HTTP {response.status_code}：{current}")
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > MAX_BYTES:
                        raise FetchError(f"响应超过大小上限 {MAX_BYTES}B：{current}")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as error:
            raise FetchError(f"请求失败：{current}（{error.__class__.__name__}）") from error
    raise FetchError(f"重定向超过 {MAX_REDIRECTS} 次：{url}")
