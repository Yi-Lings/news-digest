"""邮件渲染、组装、fake SMTP 投递与发送状态（全部离线）。"""

import email
import email.policy
from pathlib import Path

import pytest

from news_digest.config import SmtpConfig
from news_digest.delivery.mailer import MailError, compose, send, validate_smtp, write_eml
from news_digest.models import Article, BriefItem, DailyEdition, Paragraph
from news_digest.rendering.email import render_email
from news_digest.storage import db

SITE = "https://news.example.com"


def _edition() -> DailyEdition:
    article = Article(
        slug="berlin-pride",
        source="BBC News",
        title_en="What we know so far about the Berlin Pride ramming attack",
        title_zh="柏林骄傲游行冲撞事件：目前已知的情况",
        summary_en="A police manhunt is underway.",
        summary_zh="警方正在全城搜捕嫌疑人。",
        author="Demo Writer",
        published_at="2026-07-26T09:18:33+00:00",
        url="https://www.bbc.co.uk/news/articles/cevmdxz4872o",
        reading_minutes=4,
        paragraphs=[Paragraph(en="One.", zh="一。")],
        translated_by="m@p2",
    )
    brief = BriefItem(
        title_en="Glacier monitoring network doubles its stations",
        title_zh="冰川监测网络站点翻倍",
        source="The New York Times",
        url="https://www.nytimes.com/2026/07/26/world/glacier.html",
    )
    return DailyEdition(date="2026-07-26", articles=[article], briefs=[brief])


def _smtp_config(**overrides) -> SmtpConfig:
    values = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "user",
        "password": "secret",
        "sender": "news@example.com",
        "recipients": ("me@example.com", "you@example.com"),
        "delivery_enabled": True,
        "security": "starttls",
    }
    values.update(overrides)
    return SmtpConfig(**values)


def test_render_email_bilingual_without_news_links():
    subject, text, html = render_email(_edition(), SITE)
    assert subject == "Cheapcoding News 已更新｜2026-07-26"
    for body in (text, html):
        assert "What we know so far" in body
        assert "柏林骄傲游行冲撞事件" in body
        assert "完整内容请访问 Cheapcoding News 官网" in body
        assert "https://" not in body
        assert "AI 生成" in body
    assert "href=" not in html
    assert "冰川监测网络站点翻倍" not in html


def test_compose_and_eml_roundtrip(tmp_path):
    subject, text, html = render_email(_edition(), SITE)
    message = compose(subject, text, html, "news@example.com", ("me@example.com",))
    path = write_eml(message, tmp_path / "mail", "2026-07-26")
    assert path.name == "2026-07-26.eml"

    parsed = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    assert parsed["From"] == "news@example.com"
    assert "已更新" in parsed["Subject"]
    plain = parsed.get_body(preferencelist=("plain",))
    rich = parsed.get_body(preferencelist=("html",))
    assert plain is not None and "柏林骄傲游行冲撞事件" in plain.get_content()
    assert rich is not None and "完整内容请访问 Cheapcoding News 官网" in rich.get_content()


def test_compose_supports_a_single_html_part():
    message = compose(
        "Update",
        None,
        "<p>Published</p>",
        "news@example.com",
        ("me@example.com",),
    )
    assert message.get_content_type() == "text/html"
    assert message.get_body(preferencelist=("html",)) is not None
    assert message.get_body(preferencelist=("plain",)) is None
    with pytest.raises(ValueError, match="text or html"):
        compose("Empty", None, None, "news@example.com", ("me@example.com",))


class FakeSMTP:
    """记录调用的假 SMTP，兼容 with 协议。"""

    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float = 0, context=None) -> None:
        self.host, self.port = host, port
        self.ssl_context = context  # 465 隐式 SSL 时构造器收到的校验上下文
        self.starttls_context = None
        self.calls: list[str] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *args) -> None:
        pass

    def starttls(self, context=None) -> tuple[int, bytes]:
        self.starttls_context = context
        self.calls.append("starttls")
        return 220, b"ready"

    def ehlo(self) -> tuple[int, bytes]:
        self.calls.append("ehlo")
        return 250, b"ok"

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}")

    def send_message(self, message) -> dict:
        self.calls.append(f"send:{message['To']}")
        return {}


def test_send_uses_starttls_and_login(tmp_path):
    FakeSMTP.instances.clear()
    subject, text, html = render_email(_edition(), SITE)
    message = compose(subject, text, html, "news@example.com", ("me@example.com",))
    send(message, _smtp_config(recipients=("me@example.com",)), smtp_factory=FakeSMTP)
    smtp = FakeSMTP.instances[-1]
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login:user", "send:me@example.com"]
    # STARTTLS 必须传入校验证书的上下文（否则加密不认证，凭据可被中间人截获）
    assert smtp.starttls_context is not None


def test_send_port_465_skips_starttls():
    FakeSMTP.instances.clear()
    message = compose("s", "t", "<p>h</p>", "a@example.com", ("b@example.com",))
    send(
        message,
        _smtp_config(port=465, security="implicit_tls", recipients=("b@example.com",)),
        smtp_factory=FakeSMTP,
    )
    smtp = FakeSMTP.instances[-1]
    assert "starttls" not in smtp.calls
    # 465 隐式 SSL 必须把校验上下文传给构造器
    assert smtp.ssl_context is not None


def test_validate_smtp_missing_fields():
    with pytest.raises(MailError) as host_error:
        validate_smtp(_smtp_config(host=""))
    assert host_error.value.category == "configuration"
    with pytest.raises(MailError) as recipients_error:
        validate_smtp(_smtp_config(recipients=()))
    assert recipients_error.value.category == "configuration"


def test_sent_state_roundtrip(tmp_path):
    conn = db.connect(Path(tmp_path) / "news.db")
    assert db.sent_detail(conn, "2026-07-26") is None
    db.mark_sent(conn, "2026-07-26", "2026-07-26T10:00:00+00:00 -> 2 人")
    assert "2 人" in db.sent_detail(conn, "2026-07-26")
    conn.close()
