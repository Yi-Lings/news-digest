"""邮件组装、.eml 落盘与 SMTP 投递。所有网络操作都必须由调用方显式触发。"""

import copy
import datetime as dt
import hashlib
import html
import queue
import smtplib
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from pathlib import Path
from typing import Literal

from news_digest.config import SmtpConfig, normalize_email_address, normalize_recipients

# SSL(465) 构造器和 STARTTLS 共用校验证书链与主机名的默认上下文。
_TLS_CONTEXT = ssl.create_default_context()
_SMTP_TOTAL_TIMEOUT_SECONDS = 30.0

ErrorCategory = Literal[
    "configuration",
    "dns",
    "timeout",
    "connection_refused",
    "tls",
    "starttls_unsupported",
    "authentication",
    "sender_rejected",
    "recipient_rejected",
    "rate_limited",
    "smtp_protocol",
    "network",
    "partial_refusal",
    "recipient_inactive",
]
ErrorStage = Literal[
    "configuration",
    "connect",
    "dns",
    "tls",
    "auth",
    "noop",
    "mail",
    "rcpt",
    "data_command",
    "data_write",
    "data_final_response",
]

_SAFE_ERROR_MESSAGES: dict[ErrorCategory, str] = {
    "configuration": "SMTP 配置不完整或无效",
    "dns": "SMTP 服务器 DNS 解析失败",
    "timeout": "SMTP 连接或操作超时",
    "connection_refused": "SMTP 服务器拒绝连接",
    "tls": "SMTP TLS 握手或证书校验失败",
    "starttls_unsupported": "SMTP 服务器不支持 STARTTLS",
    "authentication": "SMTP 认证失败",
    "sender_rejected": "SMTP 服务器拒绝发件人",
    "recipient_rejected": "SMTP 服务器拒绝收件人",
    "rate_limited": "SMTP 服务器暂时限制请求",
    "smtp_protocol": "SMTP 协议操作失败",
    "network": "SMTP 网络连接失败",
    "partial_refusal": "SMTP 投递仅部分成功",
    "recipient_inactive": "收件人在 SMTP DATA 前已停止订阅",
}


@dataclass(frozen=True)
class RecipientDeliveryResult:
    """One recipient outcome, identified only by a non-reversible log-safe reference."""

    recipient_ref: str
    status: Literal["sent", "failed", "unknown", "skipped"]
    error_category: ErrorCategory | None = None
    delivery_uncertain: bool = False
    accepted_possible: bool = False
    error_stage: ErrorStage | None = None


@dataclass(frozen=True)
class DeliveryReport:
    """Structured delivery result; it never contains mailbox addresses or SMTP responses."""

    results: tuple[RecipientDeliveryResult, ...]

    @property
    def sent_count(self) -> int:
        return sum(result.status == "sent" for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status == "failed" for result in self.results)

    @property
    def unknown_count(self) -> int:
        return sum(result.status == "unknown" for result in self.results)

    @property
    def outcome(self) -> Literal["sent", "failed", "partial", "unknown"]:
        if self.unknown_count and not self.sent_count and not self.failed_count:
            return "unknown"
        if not self.results or self.sent_count == 0:
            return "failed" if not self.unknown_count else "partial"
        if self.failed_count or self.unknown_count:
            return "partial"
        return "sent"


class MailError(RuntimeError):
    """A categorized SMTP error with a deliberately redacted public message."""

    def __init__(
        self,
        category: ErrorCategory,
        report: DeliveryReport | None = None,
        *,
        error_stage: ErrorStage | None = None,
    ) -> None:
        self.category = category
        self.report = report
        self.error_stage = error_stage
        super().__init__(_SAFE_ERROR_MESSAGES[category])


def recipient_reference(address: str) -> str:
    """Return a stable case-insensitive identifier suitable for status messages and logs."""
    return hashlib.sha256(address.casefold().encode()).hexdigest()[:12]


def unsubscribe_headers(url: str) -> dict[str, str]:
    """Return RFC 2369/8058 headers for one recipient-specific absolute HTTPS URL."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ValueError("unsubscribe URL must be absolute HTTPS") from error
    if (
        parts.scheme.lower() != "https"
        or not parts.netloc
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\r" in url
        or "\n" in url
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("unsubscribe URL must be absolute HTTPS")
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def inject_unsubscribe(message: EmailMessage, url: str) -> str:
    """Inject recipient-specific one-click headers and return URL for body integration."""
    headers = unsubscribe_headers(url)
    for name, value in headers.items():
        if name in message:
            message.replace_header(name, value)
        else:
            message[name] = value
    return url


def compose(
    subject: str,
    text: str | None,
    html: str | None,
    sender: str,
    recipients: tuple[str, ...],
) -> EmailMessage:
    normalized_sender = normalize_email_address(sender, "SMTP_FROM")
    normalized_recipients = normalize_recipients(list(recipients))
    if not normalized_recipients:
        raise ValueError("SMTP_RECIPIENTS must not be empty")
    if "\r" in subject or "\n" in subject:
        raise ValueError("email subject contains CR/LF")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = normalized_sender
    message["To"] = ", ".join(normalized_recipients)
    if text is None and html is None:
        raise ValueError("email content must include text or html")
    if text is None:
        message.set_content(html, subtype="html")
    else:
        message.set_content(text)
    if text is not None and html is not None:
        message.add_alternative(html, subtype="html")
    return message


def write_eml(message: EmailMessage, mail_dir: Path, date: str) -> Path:
    """Archive an EML file; callers record archive success/failure separately from delivery."""
    mail_dir.mkdir(parents=True, exist_ok=True)
    path = mail_dir / f"{date}.eml"
    path.write_bytes(bytes(message))
    return path


def validate_smtp(
    config: SmtpConfig,
    *,
    require_recipients: bool = True,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
    validate_target: bool = False,
) -> tuple[str, tuple[str, ...]] | None:
    missing = [
        name
        for name, value in (
            ("SMTP_HOST", config.host.strip()),
            ("SMTP_FROM", config.sender),
            ("SMTP_RECIPIENTS", ",".join(config.recipients) if require_recipients else "ok"),
        )
        if not value
    ]
    if missing:
        raise MailError("configuration")
    if not 1 <= config.port <= 65535:
        raise MailError("configuration")
    if config.security not in {"implicit_tls", "starttls"}:
        raise MailError("configuration")
    if bool(config.username) != bool(config.password):
        raise MailError("configuration")
    target = None
    try:
        normalize_email_address(config.sender, "SMTP_FROM")
        normalize_recipients(list(config.recipients))
        if validate_target:
            from news_digest.admin_email import validate_smtp_config_target

            target = validate_smtp_config_target(config, resolver)
    except ValueError as error:
        if getattr(error, "category", None) == "dns":
            raise MailError("dns", error_stage="dns") from None
        raise MailError("configuration", error_stage="configuration") from None
    return target


def _error_category(error: BaseException, stage: str) -> ErrorCategory:
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(error, (ssl.SSLError, ssl.CertificateError)):
        return "tls"
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "authentication"
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "sender_rejected"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "recipient_rejected"
    if isinstance(error, smtplib.SMTPNotSupportedError) and stage in {"starttls", "tls"}:
        return "starttls_unsupported"
    if isinstance(error, smtplib.SMTPResponseException):
        return "rate_limited" if 400 <= error.smtp_code < 500 else "smtp_protocol"
    if isinstance(error, (smtplib.SMTPException, OSError)):
        return "network" if stage == "connect" else "smtp_protocol"
    return "smtp_protocol"


def _check_reply(reply, stage: str) -> None:
    if isinstance(reply, tuple) and reply and not 200 <= int(reply[0]) < 300:
        code = int(reply[0])
        category: ErrorCategory = "rate_limited" if 400 <= code < 500 else "smtp_protocol"
        raise MailError(category, error_stage=stage)


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("SMTP hard deadline exceeded")
    return value


def _address_attempt_deadline(deadline: float, remaining_addresses: int) -> float:
    now = time.monotonic()
    remaining = deadline - now
    if remaining <= 0:
        raise TimeoutError("SMTP hard deadline exceeded")
    return now + remaining / remaining_addresses


def _validate_smtp_with_deadline(
    config: SmtpConfig,
    *,
    deadline: float,
    require_recipients: bool,
    resolver: Callable[[str, int], Iterable[str]] | None,
    validate_target: bool,
) -> tuple[str, tuple[str, ...]] | None:
    outcome: queue.SimpleQueue[tuple[bool, object]] = queue.SimpleQueue()

    def validate() -> None:
        try:
            outcome.put(
                (
                    True,
                    validate_smtp(
                        config,
                        require_recipients=require_recipients,
                        resolver=resolver,
                        validate_target=validate_target,
                    ),
                )
            )
        except BaseException as error:
            outcome.put((False, error))

    worker = threading.Thread(target=validate, daemon=True)
    worker.start()
    try:
        worker.join(_remaining(deadline))
    except TimeoutError:
        stage = "dns" if validate_target else "configuration"
        raise MailError("timeout", error_stage=stage) from None
    if worker.is_alive():
        stage = "dns" if validate_target else "configuration"
        raise MailError("timeout", error_stage=stage)
    succeeded, value = outcome.get()
    if not succeeded:
        if isinstance(value, BaseException):
            raise value
        raise MailError("configuration")
    return value  # type: ignore[return-value]


def _set_socket_deadline(smtp, deadline: float) -> None:
    timeout = _remaining(deadline)
    sock = getattr(smtp, "sock", None)
    if sock is not None:
        sock.settimeout(timeout)


def _abort_smtp_operation(smtp) -> None:
    sock = getattr(smtp, "sock", smtp)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    try:
        sock.close()
    except (AttributeError, OSError):
        pass


def _call_with_deadline(smtp, deadline: float, operation: Callable[[], object]):
    """Bound a compound SMTP operation, whose internal I/O may reset socket timeouts."""
    timeout = _remaining(deadline)
    expired = threading.Event()

    def abort() -> None:
        expired.set()
        _abort_smtp_operation(smtp)

    watchdog = threading.Timer(timeout, abort)
    watchdog.daemon = True
    watchdog.start()
    result = None
    error: BaseException | None = None
    try:
        result = operation()
    except BaseException as caught:
        error = caught
    finally:
        watchdog.cancel()
        watchdog.join()

    if expired.is_set() or time.monotonic() >= deadline:
        raise TimeoutError("SMTP hard deadline exceeded")
    if error is not None:
        raise error
    return result


def _quit_smtp(smtp, deadline: float) -> None:
    quit_operation = getattr(smtp, "quit", None)
    if not callable(quit_operation):
        return
    _set_socket_deadline(smtp, deadline)
    _check_reply(_call_with_deadline(smtp, deadline, quit_operation), "quit")


def _close_smtp(smtp) -> None:
    close_operation = getattr(smtp, "close", None)
    if not callable(close_operation):
        return
    try:
        close_operation()
    except (OSError, smtplib.SMTPException):
        pass


def _connect_pinned_socket(
    addresses: tuple[str, ...],
    port: int,
    deadline: float,
    source_address=None,
):
    last_error: OSError | None = None
    for index, address in enumerate(addresses):
        attempt_deadline = _address_attempt_deadline(deadline, len(addresses) - index)
        try:
            return socket.create_connection(
                (address, port),
                _remaining(attempt_deadline),
                source_address=source_address,
            )
        except OSError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise OSError("validated SMTP target has no connection address")


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        hostname: str,
        port: int,
        addresses: tuple[str, ...],
        *,
        deadline: float,
        context: ssl.SSLContext,
    ) -> None:
        self._pinned_addresses = addresses
        self._deadline = deadline
        super().__init__(hostname, port, timeout=_remaining(deadline), context=context)

    def _get_socket(self, host, port, timeout):
        sock = _connect_pinned_socket(
            self._pinned_addresses,
            port,
            self._deadline,
            source_address=self.source_address,
        )
        sock.settimeout(_remaining(self._deadline))
        return _call_with_deadline(
            sock,
            self._deadline,
            lambda: self.context.wrap_socket(sock, server_hostname=host),
        )


def _open_smtp(
    config: SmtpConfig,
    smtp_factory=None,
    *,
    deadline: float,
    pinned_addresses: tuple[str, ...] = (),
):
    implicit_tls = config.security == "implicit_tls"
    if smtp_factory is not None:
        kwargs = {"context": _TLS_CONTEXT} if implicit_tls else {}
        return smtp_factory(config.host, config.port, timeout=_remaining(deadline), **kwargs)
    if not pinned_addresses:
        raise OSError("SMTP target was not resolved and validated")
    if implicit_tls:
        return _PinnedSMTPSSL(
            config.host,
            config.port,
            pinned_addresses,
            deadline=deadline,
            context=_TLS_CONTEXT,
        )

    last_error: BaseException | None = None
    for index, address in enumerate(pinned_addresses):
        attempt_deadline = _address_attempt_deadline(
            deadline, len(pinned_addresses) - index
        )
        smtp = smtplib.SMTP(timeout=_remaining(attempt_deadline))
        try:
            _call_with_deadline(
                smtp,
                attempt_deadline,
                lambda smtp=smtp, address=address: smtp.connect(address, config.port),
            )
        except BaseException as error:
            last_error = error
            smtp.close()
            continue
        # STARTTLS must validate the certificate against the original hostname,
        # while connect() above is pinned to the validated numeric address.
        smtp._host = config.host
        return smtp
    if last_error is not None:
        raise last_error
    raise OSError("validated SMTP target has no connection address")


def _prepare_session(smtp, config: SmtpConfig, deadline: float) -> None:
    stage = "connect"
    try:
        _set_socket_deadline(smtp, deadline)
        _check_reply(_call_with_deadline(smtp, deadline, smtp.ehlo), stage)
        if config.security == "starttls":
            stage = "tls"
            _set_socket_deadline(smtp, deadline)
            _check_reply(
                _call_with_deadline(
                    smtp,
                    deadline,
                    lambda: smtp.starttls(context=_TLS_CONTEXT),
                ),
                stage,
            )
            stage = "connect"
            _set_socket_deadline(smtp, deadline)
            _check_reply(_call_with_deadline(smtp, deadline, smtp.ehlo), stage)
        if config.username:
            stage = "auth"
            _set_socket_deadline(smtp, deadline)
            _call_with_deadline(
                smtp,
                deadline,
                lambda: smtp.login(config.username, config.password),
            )
    except MailError:
        raise
    except Exception as error:
        raise MailError(_error_category(error, stage), error_stage=stage) from None


def test_connection(
    config: SmtpConfig,
    smtp_factory=None,
    *,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> None:
    """Explicitly connect, EHLO, upgrade TLS if selected, authenticate, and NOOP.

    No message command is issued. Saving/loading configuration must never call this function.
    """
    deadline = time.monotonic() + _SMTP_TOTAL_TIMEOUT_SECONDS
    target = _validate_smtp_with_deadline(
        config,
        deadline=deadline,
        require_recipients=False,
        resolver=resolver,
        validate_target=smtp_factory is None or resolver is not None,
    )
    try:
        smtp = _open_smtp(
            config,
            smtp_factory,
            deadline=deadline,
            pinned_addresses=target[1] if target else (),
        )
    except Exception as error:
        category = _error_category(error, "connect")
        stage = category if category in {"dns", "tls"} else "connect"
        raise MailError(category, error_stage=stage) from None
    try:
        _prepare_session(smtp, config, deadline)
        try:
            _set_socket_deadline(smtp, deadline)
            _check_reply(_call_with_deadline(smtp, deadline, smtp.noop), "noop")
        except MailError:
            raise
        except Exception as error:
            raise MailError(_error_category(error, "noop"), error_stage="noop") from None
        _quit_smtp(smtp, deadline)
    except MailError:
        raise
    except Exception as error:
        raise MailError(_error_category(error, "connect")) from None
    finally:
        _close_smtp(smtp)


def _message_for_recipient(
    message: EmailMessage, recipient: str, unsubscribe_url: str | None = None
) -> EmailMessage:
    private_message = copy.deepcopy(message)
    for header in ("To", "Cc", "Bcc", "List-Unsubscribe", "List-Unsubscribe-Post"):
        if header in private_message:
            del private_message[header]
    private_message["To"] = recipient
    if unsubscribe_url is not None:
        inject_unsubscribe(private_message, unsubscribe_url)
    if "Date" not in private_message:
        private_message["Date"] = format_datetime(dt.datetime.now(dt.UTC))
    if "Message-ID" not in private_message:
        sender = parseaddr(str(private_message["From"]))[1]
        private_message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2])
    return private_message


class _RecipientInactive(Exception):
    pass


def _send_message_with_stage(
    smtp,
    message: EmailMessage,
    *,
    deadline: float,
    pre_data_check: Callable[[], bool] | None = None,
):
    """Call smtplib while retaining the last SMTP envelope stage reached."""
    stage = "mail"
    originals = {}
    in_data = False
    data_ready_for_body = False
    data_accepted = False
    try:
        for name in ("mail", "rcpt", "data", "send", "getreply"):
            method = getattr(smtp, name, None)
            if callable(method):
                originals[name] = method

        def tracked_envelope(*args, _method, _stage, **kwargs):
            nonlocal stage
            _set_socket_deadline(smtp, deadline)
            stage = _stage
            return _call_with_deadline(
                smtp,
                deadline,
                lambda: _method(*args, **kwargs),
            )

        for name in ("mail", "rcpt"):
            method = originals.get(name)
            if method is not None:
                setattr(
                    smtp,
                    name,
                    lambda *args, _method=method, _stage=name, **kwargs: tracked_envelope(
                        *args, _method=_method, _stage=_stage, **kwargs
                    ),
                )

        data_method = originals.get("data")
        if data_method is not None:
            def tracked_data(*args, **kwargs):
                nonlocal stage, in_data, data_ready_for_body, data_accepted
                if pre_data_check is not None and not pre_data_check():
                    raise _RecipientInactive
                _set_socket_deadline(smtp, deadline)
                stage = "data_command"
                in_data = True
                data_ready_for_body = False
                data_accepted = False
                try:
                    return _call_with_deadline(
                        smtp,
                        deadline,
                        lambda: data_method(*args, **kwargs),
                    )
                finally:
                    in_data = False

            smtp.data = tracked_data

        send_method = originals.get("send")
        if send_method is not None:
            def tracked_send(*args, **kwargs):
                nonlocal stage
                if not in_data:
                    return send_method(*args, **kwargs)
                _set_socket_deadline(smtp, deadline)
                stage = "data_write" if data_ready_for_body else "data_command"
                result = _call_with_deadline(
                    smtp,
                    deadline,
                    lambda: send_method(*args, **kwargs),
                )
                if data_ready_for_body:
                    stage = "data_final_response"
                return result

            smtp.send = tracked_send

        getreply_method = originals.get("getreply")
        if getreply_method is not None:
            def tracked_getreply(*args, **kwargs):
                nonlocal stage, data_ready_for_body, data_accepted
                if not in_data:
                    return getreply_method(*args, **kwargs)
                _set_socket_deadline(smtp, deadline)
                stage = "data_final_response" if data_ready_for_body else "data_command"

                def receive_reply():
                    nonlocal data_accepted
                    reply = getreply_method(*args, **kwargs)
                    if (
                        data_ready_for_body
                        and isinstance(reply, tuple)
                        and reply
                        and 200 <= int(reply[0]) < 300
                    ):
                        data_accepted = True
                    return reply

                result = _call_with_deadline(
                    smtp,
                    deadline,
                    receive_reply,
                )
                if (
                    not data_ready_for_body
                    and isinstance(result, tuple)
                    and result
                    and int(result[0]) == 354
                ):
                    data_ready_for_body = True
                return result

            smtp.getreply = tracked_getreply

        try:
            return (
                _call_with_deadline(
                    smtp,
                    deadline,
                    lambda: smtp.send_message(message),
                ),
                None,
                stage,
                data_accepted,
            )
        except Exception as error:
            return None, error, stage, data_accepted
    finally:
        for name, method in originals.items():
            setattr(smtp, name, method)


def _deliver_one(
    message: EmailMessage,
    config: SmtpConfig,
    recipient: str,
    smtp_factory=None,
    *,
    pinned_addresses: tuple[str, ...] = (),
    unsubscribe_url: str | None = None,
    pre_send_check: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> RecipientDeliveryResult:
    recipient_ref = recipient_reference(recipient)
    deadline = deadline or time.monotonic() + _SMTP_TOTAL_TIMEOUT_SECONDS
    try:
        smtp = _open_smtp(
            config,
            smtp_factory,
            deadline=deadline,
            pinned_addresses=pinned_addresses,
        )
    except Exception as error:
        category = _error_category(error, "connect")
        stage = category if category in {"dns", "tls"} else "connect"
        return RecipientDeliveryResult(
            recipient_ref,
            "failed",
            category,
            error_stage=stage,
        )

    accepted = False
    try:
        _prepare_session(smtp, config, deadline)
        if pre_send_check is not None and not pre_send_check():
            return RecipientDeliveryResult(recipient_ref, "skipped", "recipient_inactive")
        try:
            _set_socket_deadline(smtp, deadline)
            refused, send_error, smtp_stage, data_accepted = _send_message_with_stage(
                smtp,
                _message_for_recipient(message, recipient, unsubscribe_url),
                deadline=deadline,
                pre_data_check=pre_send_check,
            )
            if send_error is not None:
                if data_accepted:
                    return RecipientDeliveryResult(recipient_ref, "sent")
                if isinstance(send_error, _RecipientInactive):
                    return RecipientDeliveryResult(
                        recipient_ref, "skipped", "recipient_inactive"
                    )
                category = _error_category(send_error, smtp_stage)
                uncertain = smtp_stage in {"data_write", "data_final_response"} and isinstance(
                    send_error,
                    (smtplib.SMTPServerDisconnected, TimeoutError, OSError),
                )
                if uncertain:
                    return RecipientDeliveryResult(
                        recipient_ref,
                        "unknown",
                        category,
                        delivery_uncertain=True,
                        accepted_possible=True,
                        error_stage=smtp_stage,
                    )
                return RecipientDeliveryResult(
                    recipient_ref,
                    "failed",
                    category,
                    error_stage=smtp_stage,
                )
        except Exception as error:
            return RecipientDeliveryResult(
                recipient_ref,
                "failed",
                _error_category(error, "mail"),
                error_stage="mail",
            )
        if refused:
            return RecipientDeliveryResult(
                recipient_ref,
                "failed",
                "recipient_rejected",
                error_stage="rcpt",
            )
        accepted = True
        _quit_smtp(smtp, deadline)
    except MailError as error:
        if accepted:
            return RecipientDeliveryResult(recipient_ref, "sent")
        return RecipientDeliveryResult(
            recipient_ref,
            "failed",
            error.category,
            error_stage=error.error_stage,
        )
    except Exception as error:
        if accepted:
            return RecipientDeliveryResult(recipient_ref, "sent")
        category = _error_category(error, "connect")
        stage = category if category in {"dns", "tls"} else "connect"
        return RecipientDeliveryResult(
            recipient_ref,
            "failed",
            category,
            error_stage=stage,
        )
    finally:
        _close_smtp(smtp)
    return RecipientDeliveryResult(recipient_ref, "sent")


def deliver_recipient(
    message: EmailMessage,
    config: SmtpConfig,
    recipient: str,
    *,
    unsubscribe_url: str,
    smtp_factory=None,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
    pre_send_check: Callable[[], bool] | None = None,
) -> RecipientDeliveryResult:
    """Deliver one private subscription message with its own one-click unsubscribe headers."""
    normalized = normalize_email_address(recipient, "recipient")
    deadline = time.monotonic() + _SMTP_TOTAL_TIMEOUT_SECONDS
    try:
        target = _validate_smtp_with_deadline(
            config,
            deadline=deadline,
            require_recipients=False,
            resolver=resolver,
            validate_target=smtp_factory is None or resolver is not None,
        )
    except MailError as error:
        return RecipientDeliveryResult(
            recipient_reference(normalized),
            "failed",
            error.category,
            error_stage=error.error_stage or "configuration",
        )
    return _deliver_one(
        message,
        config,
        normalized,
        smtp_factory,
        pinned_addresses=target[1] if target else (),
        unsubscribe_url=unsubscribe_url,
        pre_send_check=pre_send_check,
        deadline=deadline,
    )


def deliver(
    message: EmailMessage,
    config: SmtpConfig,
    smtp_factory=None,
    *,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> DeliveryReport:
    """Deliver one private message per configured recipient and return all outcomes."""
    validation_deadline = time.monotonic() + _SMTP_TOTAL_TIMEOUT_SECONDS
    target = _validate_smtp_with_deadline(
        config,
        deadline=validation_deadline,
        require_recipients=True,
        resolver=resolver,
        validate_target=smtp_factory is None or resolver is not None,
    )
    results = tuple(
        _deliver_one(
            message,
            config,
            recipient,
            smtp_factory,
            pinned_addresses=target[1] if target else (),
        )
        for recipient in config.recipients
    )
    return DeliveryReport(results)


def send(message: EmailMessage, config: SmtpConfig, smtp_factory=None) -> DeliveryReport:
    """Compatibility wrapper that raises on any failure while retaining a structured report."""
    report = deliver(message, config, smtp_factory)
    if report.outcome in {"partial", "unknown"}:
        raise MailError("partial_refusal", report)
    if report.outcome == "failed":
        categories = {result.error_category for result in report.results}
        category = categories.pop() if len(categories) == 1 else "smtp_protocol"
        raise MailError(category or "smtp_protocol", report)
    return report


def confirmation_message(config: SmtpConfig, recipient: str, confirmation_url: str) -> EmailMessage:
    """Build a private double-opt-in message without touching edition delivery state."""
    normalized = normalize_email_address(recipient, "subscription email")
    if "\r" in confirmation_url or "\n" in confirmation_url:
        raise ValueError("confirmation URL contains CR/LF")
    escaped_url = html.escape(confirmation_url, quote=True)
    return compose(
        "[确认] Cheapcoding News 邮件订阅",
        "请打开以下链接确认订阅（链接将在 24 小时后失效）：\n\n" + confirmation_url,
        (
            "<p>请点击下方链接确认订阅（链接将在 24 小时后失效）：</p>"
            f'<p><a href="{escaped_url}">确认订阅 Cheapcoding News</a></p>'
            "<p>如果这不是你的操作，请忽略此邮件。</p>"
        ),
        config.sender,
        (normalized,),
    )


def send_confirmation(
    config: SmtpConfig,
    recipient: str,
    confirmation_url: str,
    smtp_factory=None,
    *,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> DeliveryReport:
    """Send one confirmation message independently of edition idempotency records."""
    deadline = time.monotonic() + _SMTP_TOTAL_TIMEOUT_SECONDS
    target = _validate_smtp_with_deadline(
        config,
        deadline=deadline,
        require_recipients=False,
        resolver=resolver,
        validate_target=smtp_factory is None or resolver is not None,
    )
    result = _deliver_one(
        confirmation_message(config, recipient, confirmation_url),
        config,
        normalize_email_address(recipient, "subscription email"),
        smtp_factory,
        pinned_addresses=target[1] if target else (),
        deadline=deadline,
    )
    report = DeliveryReport((result,))
    if result.status == "failed":
        raise MailError(result.error_category or "smtp_protocol", report)
    return report


def send_test_email(
    config: SmtpConfig,
    smtp_factory=None,
    *,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> DeliveryReport:
    """Explicitly send a marked test message only to recipients already present in config."""
    message = compose(
        "[测试] Cheapcoding News SMTP 配置测试",
        "这是一封显式触发的 SMTP 测试邮件。",
        "<p>这是一封显式触发的 <strong>SMTP 测试邮件</strong>。</p>",
        config.sender,
        config.recipients,
    )
    return deliver(message, config, smtp_factory, resolver=resolver)
