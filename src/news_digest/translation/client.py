"""双协议翻译客户端；配置驱动，支持流式与非流式响应。"""

import hashlib
import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpcore
import httpx

from news_digest.config import TranslationConfig, normalize_translation_base_url
from news_digest.models import Article
from news_digest.translation.schema import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

ApiType = Literal["openai_chat", "anthropic_messages"]
_MAX_RETRY_AFTER_SECONDS = 5.0
_PROBE_MAX_TOKENS = 8
_CONNECT_TIMEOUT_SECONDS = 10.0
_READ_TIMEOUT_SECONDS = 30.0
_TERMINATION_GRACE_SECONDS = 1.0
_CANCEL_POLL_SECONDS = 0.1


class TranslationError(RuntimeError):
    """安全且可供重试策略判断的接口错误。"""

    def __init__(
        self,
        message: str,
        *,
        category: str = "request",
        status: int | None = None,
        response_started: bool = False,
        retry_after: float | None = None,
        termination_confirmed: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status = status
        self.status_code = status  # 保留既有调用方兼容性
        self.response_started = response_started
        self.retry_after = retry_after
        self.termination_confirmed = termination_confirmed


@dataclass(frozen=True)
class _RequestSpec:
    path: str
    headers: dict[str, str]
    payload: dict[str, Any]


def _default_resolver(hostname: str, port: int) -> Iterable[str]:
    return {
        sockaddr[0]
        for _, _, _, _, sockaddr in socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    }


def _resolve_public_addresses(
    base_url: str,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> tuple[str, int, tuple[str, ...]]:
    parts = urlsplit(base_url)
    hostname = parts.hostname
    port = parts.port or 443
    if not hostname:
        raise TranslationError("翻译接口主机无效", category="configuration")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise TranslationError("翻译接口必须使用公网域名", category="configuration") from None
    try:
        raw_addresses = tuple(dict.fromkeys((resolver or _default_resolver)(hostname, port)))
    except (OSError, socket.gaierror) as error:
        raise TranslationError("翻译接口 DNS 解析失败", category="network") from error
    if not raw_addresses:
        raise TranslationError("翻译接口 DNS 无可用结果", category="network")
    try:
        parsed = tuple(ipaddress.ip_address(address) for address in raw_addresses)
    except ValueError as error:
        raise TranslationError("翻译接口 DNS 返回非法地址", category="network") from error
    if any(
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        for address in parsed
    ):
        raise TranslationError("翻译接口 DNS 结果不是公网地址", category="configuration")
    return hostname, port, tuple(str(address) for address in parsed)


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """Connect one HTTPS origin only to its already validated public addresses."""

    def __init__(self, hostname: str, port: int, addresses: tuple[str, ...]) -> None:
        self._hostname = hostname.casefold()
        self._port = port
        self._addresses = addresses

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        if host.casefold() != self._hostname or port != self._port:
            raise httpcore.ConnectError("connection target differs from validated origin")
        last_error: Exception | None = None
        for address in self._addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("validated origin has no connection address")


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, hostname: str, port: int, addresses: tuple[str, ...]) -> None:
        context = ssl.create_default_context()
        super().__init__(verify=context, trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=context,
            max_connections=10,
            max_keepalive_connections=10,
            network_backend=_PinnedNetworkBackend(hostname, port, addresses),
        )


def translation_cache_identity(api_type: ApiType, base_url: str, model: str) -> str:
    """Return a safe cache namespace hash without exposing gateway or model names."""
    normalized = normalize_translation_base_url(base_url)
    raw = f"{api_type}\n{normalized}\n{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request_spec(
    config: TranslationConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> _RequestSpec:
    if config.api_type == "openai_chat":
        return _RequestSpec(
            path="chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            payload={
                "model": config.model,
                "max_tokens": max_tokens,
                "stream": config.stream,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        )
    if config.api_type == "anthropic_messages":
        return _RequestSpec(
            path="messages",
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
            },
            payload={
                "model": config.model,
                "max_tokens": max_tokens,
                "stream": config.stream,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
        )
    raise TranslationError(
        "不支持的 TRANSLATION_API_TYPE",
        category="configuration",
    )


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if 0 <= seconds <= _MAX_RETRY_AFTER_SECONDS:
        return seconds
    return None


def _status_error(response: httpx.Response) -> TranslationError:
    status = response.status_code
    if status == 401:
        category, message = "authentication", "翻译接口鉴权失败"
    elif status == 403:
        category, message = "upstream", "翻译接口上游拒绝请求"
    elif status == 404:
        category, message = "endpoint", "翻译接口路径或模型不存在"
    elif status == 429:
        category, message = "rate_limit", "翻译接口请求受限"
    elif status in {500, 502, 503, 504}:
        category, message = "provider", "翻译接口服务暂时异常"
    elif status >= 500:
        category, message = "provider_permanent", "翻译接口服务拒绝请求"
    else:
        category, message = "request", "翻译接口拒绝请求"
    return TranslationError(
        f"{message}（HTTP {status}）",
        category=category,
        status=status,
        retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
    )


def _network_error(error: httpx.HTTPError, *, response_started: bool = False) -> TranslationError:
    cause: BaseException | None = error
    tls_error = False
    for _ in range(8):
        if isinstance(cause, (ssl.SSLError, ssl.CertificateError)):
            tls_error = True
            break
        cause = cause.__cause__ or cause.__context__
        if cause is None:
            break
    if tls_error:
        category, message = "tls", "翻译接口 TLS 握手或证书校验失败"
    elif isinstance(error, httpx.ConnectTimeout):
        category, message = "connection_timeout", "翻译接口连接超时"
    elif isinstance(error, httpx.ReadTimeout):
        category, message = "read_timeout", "翻译接口读取超时"
    else:
        category, message = "network", "翻译接口网络请求失败"
    return TranslationError(
        f"{message}（{error.__class__.__name__}）",
        category=category,
        response_started=response_started,
    )


class _SseParser:
    def __init__(self, api_type: ApiType, response_started: threading.Event) -> None:
        self.api_type = api_type
        self._response_started = response_started
        self.parts: list[str] = []
        self.recognized = False
        self.foreign_format = False
        self.completed = False

    @property
    def response_started(self) -> bool:
        return self._response_started.is_set()

    def feed(self, data: str, event: str) -> bool:
        if self.api_type == "openai_chat":
            return self._feed_openai(data)
        return self._feed_anthropic(data, event)

    def _feed_openai(self, data: str) -> bool:
        if data == "[DONE]":
            self.recognized = True
            self.completed = True
            return True
        payload = _json_object(data)
        if payload is None:
            return False
        if payload.get("type") == "error" or isinstance(payload.get("error"), dict):
            raise TranslationError(
                "翻译接口流式响应报错",
                category="provider",
                status=502,
                response_started=self.response_started,
            )
        if "content" in payload or str(payload.get("type", "")).startswith(
            ("message_", "content_block_")
        ):
            self.foreign_format = True
            return False
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return False
        self.recognized = True
        if not choices:
            return False
        first = choices[0]
        if not isinstance(first, dict):
            return False
        delta = first.get("delta")
        if not isinstance(delta, dict):
            return False
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.parts.append(content)
        return False

    def _feed_anthropic(self, data: str, event: str) -> bool:
        payload = _json_object(data)
        if payload is None:
            return False
        if "choices" in payload:
            self.foreign_format = True
            return False
        event_type = payload.get("type") or event
        if event_type == "error":
            raise TranslationError(
                "翻译接口流式响应报错",
                category="response_format",
                response_started=self.response_started,
            )
        if event_type == "message_stop":
            self.recognized = True
            return True
        if event_type == "content_block_delta":
            self.recognized = True
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    self.parts.append(text)
        elif event_type == "content_block_start":
            self.recognized = True
            block = payload.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    self.parts.append(text)
        elif event_type in {"message_start", "message_delta", "ping"}:
            self.recognized = True
        return False

    def result(self) -> str:
        content = "".join(self.parts)
        if content.strip():
            if self.api_type == "openai_chat" and not self.completed:
                raise TranslationError(
                    "翻译接口流式响应未正常结束",
                    category="provider",
                    status=502,
                    response_started=self.response_started,
                )
            return content
        if self.foreign_format:
            raise TranslationError(
                f"流式响应格式与 {self.api_type} 不匹配",
                category="response_format",
            )
        category = "empty_response" if self.recognized else "response_format"
        raise TranslationError("流式响应无文本内容", category=category)


def _json_object(data: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _non_stream_result(api_type: ApiType, response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as error:
        raise TranslationError(
            "非流式响应不是有效 JSON",
            category="response_format",
        ) from error
    if not isinstance(payload, dict):
        raise TranslationError("非流式响应格式不正确", category="response_format")

    if api_type == "openai_chat":
        if "content" in payload:
            raise TranslationError(
                "非流式响应格式与 openai_chat 不匹配",
                category="response_format",
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise TranslationError("OpenAI 非流式响应格式不正确", category="response_format")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise TranslationError("OpenAI 非流式响应格式不正确", category="response_format")
        content = message.get("content")
        if not isinstance(content, str):
            raise TranslationError("OpenAI 非流式响应格式不正确", category="response_format")
    else:
        if "choices" in payload:
            raise TranslationError(
                "非流式响应格式与 anthropic_messages 不匹配",
                category="response_format",
            )
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise TranslationError("Anthropic 非流式响应格式不正确", category="response_format")
        texts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                raise TranslationError(
                    "Anthropic 非流式响应格式不正确",
                    category="response_format",
                )
            texts.append(text)
        content = "".join(texts)

    if not content.strip():
        raise TranslationError("非流式响应无文本内容", category="empty_response")
    return content


class ApiTranslator:
    """正式翻译客户端；保留 translate(article) 对外接口。"""

    def __init__(
        self,
        config: TranslationConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str, int], Iterable[str]] | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("TRANSLATION_API_BASE_URL", config.base_url),
                ("TRANSLATION_API_KEY", config.api_key),
                ("TRANSLATION_MODEL", config.model),
            )
            if not value
        ]
        if missing:
            raise TranslationError(
                f"翻译接口配置缺失：{', '.join(missing)}（写入 .env.local）",
                category="configuration",
            )
        if config.api_type not in {"openai_chat", "anthropic_messages"}:
            raise TranslationError(
                "不支持的 TRANSLATION_API_TYPE",
                category="configuration",
            )
        if config.timeout_seconds <= 0:
            raise TranslationError(
                "TRANSLATION_TIMEOUT_SECONDS 必须大于 0",
                category="configuration",
            )
        self._config = config
        self._base_url = normalize_translation_base_url(config.base_url)
        self._transport = transport
        self._resolver = resolver
        self._state_lock = threading.Lock()
        self._active_clients: set[httpx.Client] = set()
        self._unresolved_workers: set[threading.Thread] = set()
        self._closed = False

    def _new_client(self) -> httpx.Client:
        transport = self._transport
        if transport is None:
            hostname, port, addresses = _resolve_public_addresses(self._base_url, self._resolver)
            transport = _PinnedHTTPTransport(hostname, port, addresses)
        return httpx.Client(
            timeout=httpx.Timeout(
                self._config.timeout_seconds,
                connect=min(self._config.timeout_seconds, _CONNECT_TIMEOUT_SECONDS),
                read=min(self._config.timeout_seconds, _READ_TIMEOUT_SECONDS),
            ),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def label(self) -> str:
        return f"{self._config.model}@{PROMPT_VERSION}"

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def cache_identity(self) -> str:
        return translation_cache_identity(
            self._config.api_type,
            self._base_url,
            self._config.model,
        )

    def translate(self, article: Article) -> str:
        """返回模型原始文本输出；解析与校验由 schema 层负责。"""
        return self._request_text(
            SYSTEM_PROMPT,
            build_user_prompt(article),
            max_tokens=self._completion_budget(article),
        )

    def translate_with_timeout(self, article: Article, *, timeout_seconds: float) -> str:
        """Translate one retry within the caller's remaining total budget."""
        return self._request_text(
            SYSTEM_PROMPT,
            build_user_prompt(article),
            max_tokens=self._completion_budget(article),
            timeout_seconds=timeout_seconds,
        )

    def translate_with_feedback(
        self,
        article: Article,
        feedback: str,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        previous_output: str | None = None,
    ) -> str:
        """Retry a malformed translation with the closed validation error as guidance.

        修复请求携带上一次的错误输出,让模型定向修正而不是从零重写;
        若未提供 previous_output(如旧调用方),行为退化为完整重生成。
        """
        repair_prompt = (
            f"{build_user_prompt(article)}\n\n"
            "上一份输出未通过严格校验。请完整重新生成 JSON，先修正以下问题：\n"
            f"{feedback}\n"
            "必须逐句对应原文，不能合并、拆分、跳过或新增句子。"
        )
        if previous_output:
            clipped = previous_output[:4000]
            repair_prompt += (
                "\n\n上一份完整输出如下（仅供修正参考，不要原样重复）：\n"
                f"{clipped}"
            )
        return self._request_text(
            SYSTEM_PROMPT,
            repair_prompt,
            max_tokens=self._completion_budget(article),
            cancel_requested=cancel_requested,
        )

    def translate_with_cancel(
        self,
        article: Article,
        *,
        cancel_requested: Callable[[], bool],
    ) -> str:
        """Translate while polling the durable task cancellation flag."""
        return self._request_text(
            SYSTEM_PROMPT,
            build_user_prompt(article),
            max_tokens=self._completion_budget(article),
            cancel_requested=cancel_requested,
        )

    def _completion_budget(self, article: Article) -> int:
        """按原文规模估算完成预算,下限为配置值,上限为配置值的 3 倍。

        固定 max_tokens 会让长文在流中途被截断,进而被误判为 schema 失败
        (1.2.12/1.2.15 家族);预算随文走才能让截断成为真正的异常路径。
        """
        en_chars = len(article.title_en) + len(article.summary_en)
        en_chars += sum(len(paragraph.en) for paragraph in article.paragraphs)
        estimated = int(en_chars * 1.2) + 1024
        ceiling = self._config.max_tokens * 3
        return max(self._config.max_tokens, min(estimated, ceiling))

    def probe(self) -> str:
        """恰好执行一次固定 ``Hi`` 的小输出协议探测，不重试。"""
        return self._request_text(
            "Reply briefly.",
            "Hi",
            max_tokens=min(self._config.max_tokens, _PROBE_MAX_TOKENS),
        )

    def _request_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int,
        timeout_seconds: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> str:
        total_timeout = self._config.timeout_seconds
        if timeout_seconds is not None:
            total_timeout = min(total_timeout, max(0.0, timeout_seconds))
        if total_timeout <= 0:
            raise TranslationError("翻译接口请求超过硬总时限", category="total_timeout")
        with self._state_lock:
            if self._closed:
                raise TranslationError("翻译客户端已关闭", category="configuration")
            self._unresolved_workers = {
                worker for worker in self._unresolved_workers if worker.is_alive()
            }
            if self._unresolved_workers:
                raise TranslationError(
                    "上一次翻译请求尚未确认终止",
                    category="termination_unconfirmed",
                    termination_confirmed=False,
                )

        outcome: queue.SimpleQueue[tuple[bool, str | BaseException]] = queue.SimpleQueue()
        response_started = threading.Event()
        cancelled = threading.Event()
        holder_lock = threading.Lock()
        holder: dict[str, httpx.Client] = {}

        def request() -> None:
            client: httpx.Client | None = None
            registered = False
            try:
                client = self._new_client()
                with self._state_lock:
                    if self._closed or cancelled.is_set():
                        return
                    self._active_clients.add(client)
                    registered = True
                with holder_lock:
                    holder["client"] = client
                if cancelled.is_set():
                    return
                outcome.put(
                    (
                        True,
                        self._request_text_blocking(
                            client,
                            system_prompt,
                            user_prompt,
                            max_tokens=max_tokens,
                            response_started=response_started,
                        ),
                    )
                )
            except BaseException as error:
                outcome.put((False, error))
            finally:
                if client is not None:
                    if registered:
                        with self._state_lock:
                            self._active_clients.discard(client)
                    client.close()

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        deadline = time.monotonic() + total_timeout
        while True:
            if cancel_requested is not None:
                try:
                    should_cancel = bool(cancel_requested())
                except Exception:
                    should_cancel = False
                if should_cancel:
                    confirmed = self._terminate_request(
                        worker, cancelled, holder, holder_lock
                    )
                    raise TranslationError(
                        "翻译请求已取消" if confirmed else "翻译请求终止状态无法确认",
                        category=(
                            "request_cancelled" if confirmed else "termination_unconfirmed"
                        ),
                        response_started=response_started.is_set(),
                        termination_confirmed=confirmed,
                    ) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    succeeded, value = outcome.get_nowait()
                    break
                except queue.Empty:
                    confirmed = self._terminate_request(
                        worker, cancelled, holder, holder_lock
                    )
                    raise TranslationError(
                        (
                            "翻译接口请求超过硬总时限"
                            if confirmed
                            else "翻译请求终止状态无法确认"
                        ),
                        category=("total_timeout" if confirmed else "termination_unconfirmed"),
                        response_started=response_started.is_set(),
                        termination_confirmed=confirmed,
                    ) from None
            try:
                succeeded, value = outcome.get(
                    timeout=min(remaining, _CANCEL_POLL_SECONDS)
                )
                break
            except queue.Empty:
                continue
        if succeeded:
            return str(value)
        raise value

    def _terminate_request(
        self,
        worker: threading.Thread,
        cancelled: threading.Event,
        holder: dict[str, httpx.Client],
        holder_lock: threading.Lock,
    ) -> bool:
        cancelled.set()
        with holder_lock:
            client = holder.get("client")
        if client is not None:
            threading.Thread(target=client.close, daemon=True).start()
        worker.join(_TERMINATION_GRACE_SECONDS)
        if worker.is_alive():
            with self._state_lock:
                self._unresolved_workers.add(worker)
            return False
        return True

    def _request_text_blocking(
        self,
        client: httpx.Client,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int,
        response_started: threading.Event,
    ) -> str:
        spec = _request_spec(
            self._config,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )
        endpoint = f"{self._base_url}/{spec.path}"
        if self._config.stream:
            return self._stream_text(client, endpoint, spec, response_started)
        return self._non_stream_text(client, endpoint, spec, response_started)

    def _stream_text(
        self,
        client: httpx.Client,
        endpoint: str,
        spec: _RequestSpec,
        response_started: threading.Event,
    ) -> str:
        parser = _SseParser(self._config.api_type, response_started)
        try:
            with client.stream(
                "POST",
                endpoint,
                headers=spec.headers,
                json=spec.payload,
            ) as response:
                if response.status_code != 200:
                    raise _status_error(response)
                response_started.set()
                event = ""
                for line in response.iter_lines():
                    if not line:
                        event = ""
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    if parser.feed(line[5:].strip(), event):
                        break
        except TranslationError:
            raise
        except httpx.HTTPError as error:
            raise _network_error(error, response_started=parser.response_started) from error
        return parser.result()

    def _non_stream_text(
        self,
        client: httpx.Client,
        endpoint: str,
        spec: _RequestSpec,
        response_started: threading.Event,
    ) -> str:
        try:
            with client.stream(
                "POST",
                endpoint,
                headers=spec.headers,
                json=spec.payload,
            ) as response:
                if response.status_code != 200:
                    raise _status_error(response)
                response_started.set()
                response.read()
        except httpx.HTTPError as error:
            raise _network_error(
                error,
                response_started=response_started.is_set(),
            ) from error
        return _non_stream_result(self._config.api_type, response)

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            clients = tuple(self._active_clients)
        for client in clients:
            threading.Thread(target=client.close, daemon=True).start()
