"""邮件组装、.eml 落盘与 SMTP 投递。发送只由显式 send-email 命令触发。"""

import smtplib
from email.message import EmailMessage
from pathlib import Path

from news_digest.config import SmtpConfig


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
    """发送邮件。465 端口用隐式 SSL，其余端口在 use_tls 时 STARTTLS。

    smtp_factory 仅供测试注入；生产走 smtplib。
    """
    validate_smtp(config)
    if smtp_factory is None:
        smtp_factory = smtplib.SMTP_SSL if config.port == 465 else smtplib.SMTP
    try:
        with smtp_factory(config.host, config.port, timeout=30) as smtp:
            if config.use_tls and config.port != 465:
                smtp.starttls()
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        # SMTPResponseException 的 str 含服务器状态码与原话（不含凭据），是关键诊断信息
        raise MailError(f"SMTP 发送失败：{error.__class__.__name__}: {error}") from error
