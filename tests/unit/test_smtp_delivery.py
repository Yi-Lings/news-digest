"""阶段 7A SMTP 配置、投递与状态机离线测试。"""

import smtplib
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import news_digest.delivery.mailer as mailer
from news_digest.config import SmtpConfig, encode_smtp_password, smtp_config_from_env
from news_digest.delivery.mailer import (
    MailError,
    compose,
    deliver,
    recipient_reference,
    send,
    send_test_email,
)
from news_digest.delivery.mailer import (
    test_connection as check_connection,
)
from news_digest.storage import db


def _config(**overrides) -> SmtpConfig:
    values = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "user",
        "password": "top-secret",
        "sender": "news@example.com",
        "recipients": ("Alice@example.com", "bob@example.com"),
        "delivery_enabled": True,
        "security": "starttls",
    }
    values.update(overrides)
    return SmtpConfig(**values)


def _message():
    return compose(
        "Digest",
        "Plain body",
        "<p>HTML body</p>",
        "news@example.com",
        ("Alice@example.com", "bob@example.com"),
    )


def test_smtp_config_repr_is_fully_redacted():
    config = _config()

    assert repr(config) == "SmtpConfig(<redacted>)"
    for secret in (
        config.host,
        config.username,
        config.password,
        config.sender,
        *config.recipients,
    ):
        assert secret not in repr(config)


def test_pinned_smtp_socket_uses_only_validated_addresses(monkeypatch):
    calls = []
    connected = object()

    def create_connection(address, timeout, source_address=None):
        calls.append(address)
        if len(calls) == 1:
            raise ConnectionRefusedError()
        return connected

    monkeypatch.setattr(mailer.socket, "create_connection", create_connection)
    result = mailer._connect_pinned_socket(
        ("93.184.216.34", "142.250.72.14"),
        465,
        time.monotonic() + 5.0,
    )
    assert result is connected
    assert calls == [("93.184.216.34", 465), ("142.250.72.14", 465)]


def test_pinned_smtp_socket_recomputes_remaining_deadline_per_address(monkeypatch):
    clock = [0.0]
    timeouts = []
    connected = object()

    def create_connection(address, timeout, source_address=None):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            clock[0] += 2.0
            raise ConnectionRefusedError()
        return connected

    monkeypatch.setattr(mailer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mailer.socket, "create_connection", create_connection)
    result = mailer._connect_pinned_socket(
        ("93.184.216.34", "142.250.72.14"),
        465,
        5.0,
    )
    assert result is connected
    assert timeouts == [2.5, 3.0]


def test_pinned_implicit_tls_falls_back_after_first_address_timeout(monkeypatch):
    clock = [0.0]
    attempts = []

    class Connected:
        def settimeout(self, timeout):
            self.timeout = timeout

    connected = Connected()

    def create_connection(address, timeout, source_address=None):
        attempts.append((address, timeout))
        if len(attempts) == 1:
            clock[0] += timeout
            raise TimeoutError
        return connected

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            assert sock is connected
            assert server_hostname == "smtp.example.com"
            return sock

    monkeypatch.setattr(mailer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mailer.socket, "create_connection", create_connection)
    smtp = mailer._PinnedSMTPSSL.__new__(mailer._PinnedSMTPSSL)
    smtp._pinned_addresses = ("93.184.216.34", "142.250.72.14")
    smtp._deadline = 10.0
    smtp.source_address = None
    smtp.context = Context()

    assert smtp._get_socket("smtp.example.com", 465, 10.0) is connected
    assert attempts == [
        (("93.184.216.34", 465), 5.0),
        (("142.250.72.14", 465), 5.0),
    ]


def test_pinned_starttls_falls_back_after_first_address_timeout(monkeypatch):
    clock = [0.0]
    instances = []

    class SMTP:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self._host = ""
            instances.append(self)

        def connect(self, host, port):
            self.host = host
            self.port = port
            if len(instances) == 1:
                clock[0] += self.timeout
                raise TimeoutError
            return 220, b"ready"

        def close(self):
            pass

    monkeypatch.setattr(mailer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mailer.smtplib, "SMTP", SMTP)

    smtp = mailer._open_smtp(
        _config(security="starttls"),
        deadline=10.0,
        pinned_addresses=("93.184.216.34", "142.250.72.14"),
    )

    assert smtp is instances[1]
    assert smtp._host == "smtp.example.com"
    assert [(item.host, item.timeout) for item in instances] == [
        ("93.184.216.34", 5.0),
        ("142.250.72.14", 5.0),
    ]


def test_pinned_implicit_tls_keeps_original_hostname(monkeypatch):
    class Connected:
        def settimeout(self, timeout):
            pass

    connected = Connected()
    wrapped = object()
    observed = {}

    monkeypatch.setattr(
        mailer,
        "_connect_pinned_socket",
        lambda addresses, port, deadline, source_address=None: connected,
    )

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            observed.update(sock=sock, server_hostname=server_hostname)
            return wrapped

    smtp = mailer._PinnedSMTPSSL.__new__(mailer._PinnedSMTPSSL)
    smtp._pinned_addresses = ("93.184.216.34",)
    smtp._deadline = time.monotonic() + 5.0
    smtp.source_address = None
    smtp.context = Context()
    assert smtp._get_socket("smtp.example.com", 465, 5.0) is wrapped
    assert observed == {"sock": connected, "server_hostname": "smtp.example.com"}


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    refusals: dict[str, tuple[int, bytes]] = {}
    raised: BaseException | None = None

    def __init__(self, host, port, timeout=0, context=None):
        self.host = host
        self.port = port
        self.context = context
        self.calls = []
        self.messages = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def ehlo(self):
        self.calls.append("ehlo")
        return 250, b"ok"

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.starttls_context = context
        return 220, b"ready"

    def login(self, username, password):
        self.calls.append(("login", username, password))
        if self.raised:
            raise self.raised
        return 235, b"ok"

    def noop(self):
        self.calls.append("noop")
        return 250, b"ok"

    def send_message(self, message):
        self.calls.append("send_message")
        self.messages.append(message)
        address = str(message["To"])
        return {address: self.refusals[address]} if address in self.refusals else {}


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances.clear()
    FakeSMTP.refusals = {}
    FakeSMTP.raised = None


def test_smtp_config_security_and_legacy_migration():
    base = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "2525",
        "SMTP_FROM": "news@example.com",
        "SMTP_RECIPIENTS": " Alice@Example.com,alice@example.com, Bob@example.com ",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "secret",
        "EMAIL_DELIVERY_ENABLED": "true",
    }
    config = smtp_config_from_env({**base, "SMTP_SECURITY": "starttls"})
    assert config.security == "starttls"
    assert config.delivery_enabled is True
    assert config.recipients == ("Alice@Example.com", "Bob@example.com")

    assert smtp_config_from_env({**base, "SMTP_USE_TLS": "true"}).security == "implicit_tls"
    assert smtp_config_from_env({**base, "SMTP_USE_TLS": "false"}).security == "starttls"
    encoded = encode_smtp_password(" ${SMTP_HOST} # 中文 ")
    config = smtp_config_from_env({**base, "SMTP_PASSWORD": encoded})
    assert config.password == " ${SMTP_HOST} # 中文 "
    with pytest.raises(ValueError, match="conflicts"):
        smtp_config_from_env(
            {**base, "SMTP_SECURITY": "starttls", "SMTP_USE_TLS": "true"}
        )


@pytest.mark.parametrize(
    "password",
    [
        "nd-b64-v1:",
        "nd-b64-v1:not-base64!",
        "nd-b64-v1:/w==",
        "nd-b64-v1:c2 VjcmV0",
    ],
)
def test_invalid_managed_password_suffix_remains_legacy_plaintext(password):
    config = smtp_config_from_env(
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_FROM": "news@example.com",
            "SMTP_RECIPIENTS": "reader@example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": password,
            "SMTP_SECURITY": "starttls",
        }
    )
    assert config.password == password


@pytest.mark.parametrize(
    "override,match",
    [
        ({"SMTP_PORT": "0"}, "1 to 65535"),
        ({"SMTP_PORT": "65536"}, "1 to 65535"),
        ({"SMTP_PORT": "abc"}, "integer"),
        ({"SMTP_SECURITY": "plain"}, "implicit_tls or starttls"),
        ({"SMTP_USE_TLS": "perhaps"}, "true or false"),
        ({"SMTP_USE_TLS": ""}, "true or false"),
        ({"EMAIL_DELIVERY_ENABLED": ""}, "true or false"),
        ({"SMTP_USERNAME": "user", "SMTP_PASSWORD": ""}, "both be set"),
        ({"SMTP_RECIPIENTS": "a@example.com,,b@example.com"}, "empty item"),
        ({"SMTP_RECIPIENTS": "bad address"}, "valid email"),
        ({"SMTP_RECIPIENTS": "a@example.com\r\nBcc:x@example.com"}, "CR/LF"),
    ],
)
def test_smtp_config_rejects_invalid_values(override, match):
    env = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "news@example.com",
        "SMTP_RECIPIENTS": "a@example.com",
        "SMTP_SECURITY": "starttls",
    }
    env.update(override)
    with pytest.raises(ValueError, match=match):
        smtp_config_from_env(env)


def test_smtp_config_allows_delivery_without_legacy_recipients():
    config = smtp_config_from_env(
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM": "news@example.com",
            "SMTP_RECIPIENTS": "",
            "SMTP_SECURITY": "starttls",
            "EMAIL_DELIVERY_ENABLED": "true",
        }
    )

    assert config.delivery_enabled is True
    assert config.recipients == ()


def test_connection_checks_protocol_without_sending():
    check_connection(_config(), smtp_factory=FakeSMTP)
    smtp = FakeSMTP.instances[0]
    assert smtp.calls == [
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "user", "top-secret"),
        "noop",
    ]
    assert smtp.starttls_context is not None
    assert smtp.messages == []


def test_connection_deadline_includes_blocking_dns_resolution(monkeypatch):
    release = threading.Event()

    def blocked_resolver(host, port):
        release.wait(1.0)
        return ["93.184.216.34"]

    monkeypatch.setattr(mailer, "_SMTP_TOTAL_TIMEOUT_SECONDS", 0.01)
    started = time.monotonic()
    try:
        with pytest.raises(MailError) as raised:
            check_connection(_config(), smtp_factory=FakeSMTP, resolver=blocked_resolver)
    finally:
        release.set()
    assert raised.value.category == "timeout"
    assert time.monotonic() - started < 0.2


class BlockingQuitSMTP(FakeSMTP):
    class Socket:
        def __init__(self):
            self.closed = threading.Event()

        def settimeout(self, timeout):
            pass

        def shutdown(self, how):
            self.closed.set()

        def close(self):
            self.closed.set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sock = self.Socket()
        self.close_calls = 0
        self.quit_interrupted = False

    def __exit__(self, *args):
        try:
            self.quit()
        finally:
            self.close()

    def quit(self):
        self.calls.append("quit")
        self.quit_interrupted = self.sock.closed.wait(1.0)
        return 221, b"bye"

    def close(self):
        self.close_calls += 1
        self.sock.close()


def test_connection_quit_obeys_shared_wall_clock_deadline(monkeypatch):
    monkeypatch.setattr(mailer, "_SMTP_TOTAL_TIMEOUT_SECONDS", 0.01)
    started = time.monotonic()
    with pytest.raises(MailError) as raised:
        check_connection(
            _config(
                port=465,
                security="implicit_tls",
                username="",
                password="",
            ),
            smtp_factory=BlockingQuitSMTP,
        )

    smtp = BlockingQuitSMTP.instances[0]
    assert raised.value.category == "timeout"
    assert time.monotonic() - started < 0.05
    assert smtp.quit_interrupted is True
    assert smtp.close_calls == 1


def test_successful_data_is_sent_when_quit_times_out():
    deadline = time.monotonic() + 0.1
    result = mailer._deliver_one(
        _message(),
        _config(
            port=465,
            security="implicit_tls",
            username="",
            password="",
            recipients=(),
        ),
        "reader@example.com",
        BlockingQuitSMTP,
        deadline=deadline,
    )

    smtp = BlockingQuitSMTP.instances[0]
    assert result.status == "sent"
    assert result.error_stage is None
    assert smtp.quit_interrupted is True
    assert smtp.close_calls == 1


def test_compound_smtp_operation_is_interrupted_at_wall_clock_deadline():
    class InterruptibleSocket:
        def __init__(self):
            self.closed = threading.Event()

        def shutdown(self, how):
            self.closed.set()

        def close(self):
            self.closed.set()

    class SMTP:
        def __init__(self):
            self.sock = InterruptibleSocket()

    smtp = SMTP()
    threads_before = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        mailer._call_with_deadline(
            smtp,
            started + 0.01,
            lambda: smtp.sock.closed.wait(1.0),
        )

    assert time.monotonic() - started < 0.2
    assert smtp.sock.closed.is_set()
    assert {thread.ident for thread in threading.enumerate()} <= threads_before


@pytest.mark.parametrize("slow_stage", ["starttls", "authentication"])
def test_compound_session_stage_shares_wall_clock_deadline(slow_stage):
    class InterruptibleSocket:
        def __init__(self):
            self.closed = threading.Event()

        def settimeout(self, timeout):
            pass

        def shutdown(self, how):
            self.closed.set()

        def close(self):
            self.closed.set()

    class CompoundSessionSMTP(FakeSMTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sock = InterruptibleSocket()

        def _two_steps(self):
            self.sock.closed.wait(0.006)
            self.sock.closed.wait(0.006)

        def starttls(self, context=None):
            if slow_stage == "starttls":
                self._two_steps()
            return super().starttls(context)

        def login(self, username, password):
            if slow_stage == "authentication":
                self._two_steps()
            return super().login(username, password)

    smtp = CompoundSessionSMTP("smtp.example.com", 587)
    threads_before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(MailError) as raised:
        mailer._prepare_session(smtp, _config(), time.monotonic() + 0.01)

    assert raised.value.category == "timeout"
    assert {thread.ident for thread in threading.enumerate()} <= threads_before


def test_recipient_dns_timeout_before_data_is_failed_not_unknown(monkeypatch):
    release = threading.Event()

    def blocked_resolver(host, port):
        release.wait(1.0)
        return ["93.184.216.34"]

    monkeypatch.setattr(mailer, "_SMTP_TOTAL_TIMEOUT_SECONDS", 0.01)
    try:
        result = mailer.deliver_recipient(
            _message(),
            _config(recipients=()),
            "reader@example.com",
            unsubscribe_url="https://news.example.com/unsubscribe/token",
            smtp_factory=FakeSMTP,
            resolver=blocked_resolver,
        )
    finally:
        release.set()
    assert result.status == "failed"
    assert result.error_category == "timeout"
    assert result.error_stage == "dns"
    assert result.delivery_uncertain is False


def test_recipient_connect_failure_reports_connect_stage():
    def refused_factory(*args, **kwargs):
        raise ConnectionRefusedError("redacted")

    result = mailer._deliver_one(
        _message(),
        _config(recipients=()),
        "reader@example.com",
        refused_factory,
    )

    assert result.status == "failed"
    assert result.error_category == "connection_refused"
    assert result.error_stage == "connect"


@pytest.mark.parametrize(
    "failure_stage,error,expected_category,expected_stage",
    [
        ("tls", smtplib.SMTPNotSupportedError("redacted"), "starttls_unsupported", "tls"),
        (
            "auth",
            smtplib.SMTPAuthenticationError(535, b"redacted"),
            "authentication",
            "auth",
        ),
    ],
)
def test_recipient_session_failure_reports_precise_stage(
    failure_stage, error, expected_category, expected_stage
):
    class SessionFailureSMTP(FakeSMTP):
        def starttls(self, context=None):
            if failure_stage == "tls":
                raise error
            return super().starttls(context)

        def login(self, username, password):
            if failure_stage == "auth":
                raise error
            return super().login(username, password)

    result = mailer._deliver_one(
        _message(),
        _config(recipients=()),
        "reader@example.com",
        SessionFailureSMTP,
    )

    assert result.status == "failed"
    assert result.error_category == expected_category
    assert result.error_stage == expected_stage


def test_connection_noop_failure_reports_noop_stage():
    class NoopFailureSMTP(FakeSMTP):
        def noop(self):
            raise TimeoutError("redacted")

    with pytest.raises(MailError) as raised:
        check_connection(_config(), smtp_factory=NoopFailureSMTP)

    assert raised.value.category == "timeout"
    assert raised.value.error_stage == "noop"


def test_envelope_stages_share_one_hard_deadline(monkeypatch):
    clock = [0.0]

    class SlowEnvelopeSMTP(FakeSMTP):
        data_calls = 0

        def mail(self, *args, **kwargs):
            clock[0] += 0.006
            return 250, b"ok"

        def rcpt(self, *args, **kwargs):
            clock[0] += 0.006
            return 250, b"ok"

        def data(self, *args, **kwargs):
            type(self).data_calls += 1
            return 250, b"ok"

        def send_message(self, message):
            self.mail("news@example.com")
            self.rcpt("alice@example.com")
            self.data(message.as_bytes())
            return {}

    monkeypatch.setattr(mailer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mailer, "_SMTP_TOTAL_TIMEOUT_SECONDS", 0.01)
    report = deliver(
        _message(),
        _config(recipients=("alice@example.com",)),
        SlowEnvelopeSMTP,
    )
    result = report.results[0]
    assert result.status == "failed"
    assert result.error_category == "timeout"
    assert SlowEnvelopeSMTP.data_calls == 0


@pytest.mark.parametrize(
    "slow_stage,expected_status",
    [("mail", "failed"), ("rcpt", "failed"), ("data", "failed")],
)
def test_compound_envelope_stage_timeout_preserves_delivery_certainty(
    slow_stage, expected_status
):
    class InterruptibleSocket:
        def __init__(self):
            self.closed = threading.Event()

        def settimeout(self, timeout):
            pass

        def shutdown(self, how):
            self.closed.set()

        def close(self):
            self.closed.set()

    class CompoundEnvelopeSMTP(FakeSMTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sock = InterruptibleSocket()

        def _stage(self, name):
            if slow_stage == name:
                self.sock.closed.wait(0.006)
                self.sock.closed.wait(0.006)
            return 250, b"ok"

        def mail(self, *args, **kwargs):
            return self._stage("mail")

        def rcpt(self, *args, **kwargs):
            return self._stage("rcpt")

        def data(self, *args, **kwargs):
            return self._stage("data")

        def send_message(self, message):
            self.mail("news@example.com")
            self.rcpt("reader@example.com")
            return self.data(message.as_bytes())

    threads_before = {thread.ident for thread in threading.enumerate()}
    result = mailer._deliver_one(
        _message(),
        _config(
            username="",
            password="",
            recipients=(),
            security="implicit_tls",
            port=465,
        ),
        "reader@example.com",
        CompoundEnvelopeSMTP,
        deadline=time.monotonic() + 0.01,
    )

    assert result.status == expected_status
    assert result.error_category == "timeout"
    assert result.delivery_uncertain is False
    assert {thread.ident for thread in threading.enumerate()} <= threads_before


def test_implicit_tls_receives_verified_context_without_starttls():
    check_connection(_config(port=465, security="implicit_tls"), smtp_factory=FakeSMTP)
    smtp = FakeSMTP.instances[0]
    assert smtp.context is not None
    assert "starttls" not in smtp.calls


def test_send_is_one_connection_and_private_message_per_recipient():
    report = send(_message(), _config(), smtp_factory=FakeSMTP)
    assert report.sent_count == 2
    assert len(FakeSMTP.instances) == 2
    all_messages = [instance.messages[0] for instance in FakeSMTP.instances]
    assert [str(message["To"]) for message in all_messages] == [
        "Alice@example.com",
        "bob@example.com",
    ]
    for message in all_messages:
        assert message["Date"]
        assert str(message["Message-ID"]).endswith("@example.com>")
        serialized = message.as_string()
        other = "bob@example.com" if "Alice@example.com" in serialized else "Alice@example.com"
        assert other not in serialized


def test_partial_refusal_is_structured_and_redacted():
    FakeSMTP.refusals = {"bob@example.com": (550, b"bob@example.com mailbox secret")}
    report = deliver(_message(), _config(), smtp_factory=FakeSMTP)
    assert report.outcome == "partial"
    assert report.sent_count == 1
    assert report.failed_count == 1
    assert report.results[1].error_category == "recipient_rejected"
    assert report.results[1].recipient_ref == recipient_reference("bob@example.com")
    assert "bob@example.com" not in repr(report)

    with pytest.raises(MailError) as raised:
        send(_message(), _config(), smtp_factory=FakeSMTP)
    assert raised.value.category == "partial_refusal"
    assert "bob@example.com" not in str(raised.value)
    assert "mailbox secret" not in str(raised.value)


@pytest.mark.parametrize(
    "data_error",
    [
        smtplib.SMTPServerDisconnected("after DATA"),
        TimeoutError("after DATA"),
        ConnectionResetError("after DATA"),
        BrokenPipeError("after DATA"),
    ],
)
def test_data_method_disconnect_before_observable_write_is_failed(data_error):
    class UnknownSMTP(FakeSMTP):
        def mail(self, *args, **kwargs):
            return 250, b"ok"

        def rcpt(self, *args, **kwargs):
            return 250, b"ok"

        def data(self, *args, **kwargs):
            raise data_error

        def send_message(self, message):
            self.mail("news@example.com")
            self.rcpt("alice@example.com")
            self.data(message.as_bytes())

    report = deliver(_message(), _config(recipients=("alice@example.com",)), UnknownSMTP)
    result = report.results[0]
    assert result.status == "failed"
    assert result.error_stage == "data_command"
    assert result.delivery_uncertain is False
    assert result.accepted_possible is False
    assert report.unknown_count == 0


class DataLifecycleSMTP(FakeSMTP):
    def __init__(self, *args, failure_stage, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_stage = failure_stage
        self.reply_count = 0
        self.debuglevel = 0

    def mail(self, *args, **kwargs):
        return 250, b"ok"

    def rcpt(self, *args, **kwargs):
        return 250, b"ok"

    def putcmd(self, *args, **kwargs):
        self.send(b"data\r\n")

    def getreply(self):
        self.reply_count += 1
        if self.failure_stage == "data_command" and self.reply_count == 1:
            raise smtplib.SMTPServerDisconnected("redacted")
        if self.failure_stage == "data_final_response" and self.reply_count == 2:
            raise smtplib.SMTPServerDisconnected("redacted")
        return (354, b"continue") if self.reply_count == 1 else (250, b"queued")

    def send(self, data):
        if self.failure_stage == "data_write" and data != b"data\r\n":
            raise BrokenPipeError("redacted")

    def data(self, message):
        return smtplib.SMTP.data(self, message)

    def send_message(self, message):
        self.mail("news@example.com")
        self.rcpt("reader@example.com")
        self.data(message.as_bytes())
        return {}


@pytest.mark.parametrize(
    "failure_stage,expected_status,expected_uncertain",
    [
        ("data_command", "failed", False),
        ("data_write", "unknown", True),
        ("data_final_response", "unknown", True),
    ],
)
def test_data_lifecycle_reports_precise_failure_stage(
    failure_stage, expected_status, expected_uncertain
):
    def factory(*args, **kwargs):
        return DataLifecycleSMTP(*args, failure_stage=failure_stage, **kwargs)

    result = mailer._deliver_one(
        _message(),
        _config(username="", password="", security="implicit_tls", port=465),
        "reader@example.com",
        factory,
    )

    assert result.status == expected_status
    assert result.error_stage == failure_stage
    assert result.delivery_uncertain is expected_uncertain
    assert result.accepted_possible is expected_uncertain


def test_data_2xx_is_accepted_before_deadline_post_check(monkeypatch):
    clock = [0.0]

    class AcceptedAtDeadlineSMTP(DataLifecycleSMTP):
        def getreply(self):
            reply = super().getreply()
            if self.reply_count == 2:
                clock[0] = 1.0
            return reply

    def factory(*args, **kwargs):
        return AcceptedAtDeadlineSMTP(*args, failure_stage=None, **kwargs)

    monkeypatch.setattr(mailer.time, "monotonic", lambda: clock[0])
    result = mailer._deliver_one(
        _message(),
        _config(username="", password="", security="implicit_tls", port=465),
        "reader@example.com",
        factory,
        deadline=1.0,
    )

    assert result.status == "sent"
    assert result.error_stage is None
    assert result.delivery_uncertain is False


@pytest.mark.parametrize("failed_stage", ["mail", "rcpt"])
def test_disconnect_before_data_is_failed_not_unknown(failed_stage):
    class PreDataSMTP(FakeSMTP):
        def mail(self, *args, **kwargs):
            if failed_stage == "mail":
                raise smtplib.SMTPServerDisconnected("before DATA")
            return 250, b"ok"

        def rcpt(self, *args, **kwargs):
            if failed_stage == "rcpt":
                raise smtplib.SMTPServerDisconnected("before DATA")
            return 250, b"ok"

        def data(self, *args, **kwargs):
            return 250, b"ok"

        def send_message(self, message):
            self.mail("news@example.com")
            self.rcpt("alice@example.com")
            return self.data(message.as_bytes())

    report = deliver(_message(), _config(recipients=("alice@example.com",)), PreDataSMTP)
    result = report.results[0]
    assert result.status == "failed"
    assert result.delivery_uncertain is False
    assert result.accepted_possible is False


@pytest.mark.parametrize(
    "error,category",
    [
        (TimeoutError("secret host"), "timeout"),
        (ConnectionRefusedError("secret host"), "connection_refused"),
        (smtplib.SMTPAuthenticationError(535, b"user secret"), "authentication"),
    ],
)
def test_connection_error_categories_are_redacted(error, category):
    FakeSMTP.raised = error
    with pytest.raises(MailError) as raised:
        check_connection(_config(), smtp_factory=FakeSMTP)
    assert raised.value.category == category
    assert "secret" not in str(raised.value)
    assert "example.com" not in str(raised.value)


def test_test_email_uses_only_saved_config_recipients():
    report = send_test_email(_config(recipients=("saved@example.com",)), FakeSMTP)
    assert report.sent_count == 1
    message = FakeSMTP.instances[0].messages[0]
    assert str(message["To"]) == "saved@example.com"
    assert "测试" in str(message["Subject"])


def test_delivery_state_retry_and_archive_are_independent(tmp_path):
    conn = db.connect(Path(tmp_path) / "news.db")
    date = "2026-07-27"
    now = "2026-07-27T00:00:00+00:00"
    db.ensure_delivery_recipients(conn, date, ("a@example.com", "b@example.com"), now)

    assert db.claim_delivery(conn, date, "a@example.com", now)
    db.finish_delivery(conn, date, "a@example.com", "sent", now)
    assert not db.claim_delivery(conn, date, "a@example.com", now)

    assert db.claim_delivery(conn, date, "b@example.com", now)
    db.finish_delivery(conn, date, "b@example.com", "failed", now, "recipient_rejected")
    assert db.claim_delivery(conn, date, "b@example.com", now)
    db.finish_delivery(conn, date, "b@example.com", "unknown", now, "commit_uncertain")
    assert not db.claim_delivery(conn, date, "b@example.com", now)

    summary = db.delivery_summary(conn, date)
    assert summary.sent == 1 and summary.unknown == 1 and summary.failed == 0
    assert db.reset_unknown_delivery(conn, date, "b@example.com", now)
    assert db.claim_delivery(conn, date, "b@example.com", now)

    db.mark_archive(conn, date, "failed", now, "disk_error")
    archive = db.archive_state(conn, date)
    assert archive is not None and archive.status == "failed"
    assert db.delivery_summary(conn, date).sent == 1
    conn.close()


def test_interrupted_sending_is_recovered_as_unknown(tmp_path):
    conn = db.connect(Path(tmp_path) / "news.db")
    now = "2026-07-27T00:00:00+00:00"
    db.ensure_delivery_recipients(conn, "2026-07-27", ("a@example.com",), now)
    assert db.claim_delivery(conn, "2026-07-27", "a@example.com", now, run_id="crashed")
    assert db.recover_interrupted_deliveries(
        conn,
        "2026-07-27T00:05:00+00:00",
        stale_before="2026-07-26T23:55:00+00:00",
    ) == 0
    assert db.recover_interrupted_deliveries(
        conn,
        "2026-07-27T00:11:00+00:00",
        stale_before="2026-07-27T00:01:00+00:00",
    ) == 1
    state = db.delivery_states(conn, "2026-07-27")[0]
    assert state.status == "unknown"
    assert state.error_category == "worker_interrupted"
    assert not db.claim_delivery(conn, "2026-07-27", "a@example.com", now)
    conn.close()


def test_delivery_claim_is_atomic_between_connections(tmp_path):
    path = Path(tmp_path) / "news.db"
    first = db.connect(path)
    second = db.connect(path)
    date = "2026-07-27"
    now = "2026-07-27T00:00:00+00:00"
    db.ensure_delivery_recipients(first, date, ("a@example.com",), now)
    assert db.claim_delivery(first, date, "a@example.com", now)
    assert not db.claim_delivery(second, date, "a@example.com", now)
    first.close()
    second.close()


def test_legacy_sent_meta_remains_readable(tmp_path):
    conn = db.connect(Path(tmp_path) / "news.db")
    db.mark_sent(conn, "2026-07-26", "legacy detail")
    summary = db.delivery_summary(conn, "2026-07-26")
    assert summary.legacy_sent_detail == "legacy detail"
    assert summary.sent == 0
    conn.close()


def test_finish_requires_a_claim(tmp_path):
    conn = db.connect(Path(tmp_path) / "news.db")
    db.ensure_delivery_recipients(
        conn, "2026-07-27", ("a@example.com",), "2026-07-27T00:00:00+00:00"
    )
    with pytest.raises(RuntimeError, match="not claimed"):
        db.finish_delivery(
            conn,
            "2026-07-27",
            "a@example.com",
            "sent",
            "2026-07-27T00:00:01+00:00",
        )
    conn.close()


def test_connect_schema_is_usable_by_plain_sqlite_reader(tmp_path):
    path = Path(tmp_path) / "news.db"
    conn = db.connect(path)
    conn.close()
    reader = sqlite3.connect(path)
    assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reader.close()
