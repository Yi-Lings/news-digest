"""邮件组装、.eml 落盘与 SMTP 投递。发送只由显式 send-email 命令触发。"""

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from news_digest.config import SmtpConfig

# 校验证书的 TLS 上下文：默认上下文启用 CERT_REQUIRED + check_hostname，
# 杜绝 smtplib 默认 unverified 上下文——否则加密但不认证，login() 凭据可被
# 路径中间人截获。SSL(465) 传给构造器、STARTTLS 传给 starttls()，两处都用它。
_TLS_CONTEXT = ssl.create_default_context()


class MailError(RuntimeError):
    """投递前置条件不满足或发送失败；信息不含凭据。"""


def compose(
    subject: str, text: str, html: str, sender: str, recipients: tuple[str, ...]
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def write_eml(message: EmailMessage, mail_dir: Path, date: str) -> Path:
    mail_dir.mkdir(parents=True, exist_ok=True)
    path = mail_dir / f"{date}.eml"
    path.write_bytes(bytes(message))
    return path


def validate_smtp(config: SmtpConfig) -> None:
    missing = [
        name
        for name, value in (
            ("SMTP_HOST", config.host),
            ("SMTP_FROM", config.sender),
            ("SMTP_RECIPIENTS", ",".join(config.recipients)),
        )
        if not value
    ]
    if missing:
        raise MailError(f"SMTP 配置缺失：{', '.join(missing)}（写入 .env.local）")


def send(message: EmailMessage, config: SmtpConfig, smtp_factory=None) -> None:
    """发送邮件。465 端口用隐式 SSL，其余端口在 use_tls 时 STARTTLS；两种 TLS
    都传入校验证书的默认上下文（_TLS_CONTEXT）。

    smtp_factory 仅供测试注入；生产走 smtplib。
    """
    validate_smtp(config)
    use_ssl = config.port == 465
    if smtp_factory is None:
        smtp_factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    # SSL 上下文只对隐式 SSL(465) 的构造器有意义；明文 SMTP 构造器不接受 context
    connect_kwargs = {"context": _TLS_CONTEXT} if use_ssl else {}
    try:
        with smtp_factory(config.host, config.port, timeout=30, **connect_kwargs) as smtp:
            if config.use_tls and not use_ssl:
                smtp.starttls(context=_TLS_CONTEXT)
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        # SMTPResponseException 的 str 含服务器状态码与原话（不含凭据），是关键诊断信息
        raise MailError(f"SMTP 发送失败：{error.__class__.__name__}: {error}") from error
