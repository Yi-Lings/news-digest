"""Render configured email content from one supplied published edition."""

from dataclasses import dataclass
from html import escape

from news_digest.delivery.email_content import (
    EmailContentConfig,
    EmailPreviewMetadata,
    select_email_content,
)
from news_digest.models import DailyEdition
from news_digest.rendering.pages import create_environment


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str
    metadata: EmailPreviewMetadata


def _compatible_default(edition: DailyEdition) -> EmailContentConfig:
    """Preserve the original render_email default: all mains and up to five briefs."""
    return EmailContentConfig(
        mains_enabled=bool(edition.articles),
        briefs_enabled=bool(edition.briefs),
        main_limit=len(edition.articles),
        brief_limit=min(5, len(edition.briefs)),
    )


def render_email_preview(
    edition: DailyEdition,
    site_url: str,
    config: EmailContentConfig | None = None,
    *,
    test: bool = False,
    expected_date: str | None = None,
    unsubscribe_url: str | None = None,
) -> RenderedEmail:
    """Render a preview and metadata without IO or external calls."""
    selected = select_email_content(
        edition,
        site_url,
        config or _compatible_default(edition),
        expected_date=expected_date,
    )
    subject = f"Cheapcoding News 已更新｜{selected.date}"
    if test:
        subject = f"[测试] {subject}"
    env = create_environment()
    context = {"content": selected}
    text = env.get_template("email.txt").render(context)
    html = env.get_template("email.html").render(context)
    if unsubscribe_url is not None:
        text += f"\n\n退订：{unsubscribe_url}\n"
        html = html.replace(
            "</body>",
            '<p style="font-family:Arial,sans-serif;font-size:11px;line-height:1.5;">'
            f'<a href="{escape(unsubscribe_url, quote=True)}">退订邮件</a></p></body>',
        )
    return RenderedEmail(subject, text, html, selected.metadata)


def render_email(
    edition: DailyEdition,
    site_url: str,
    config: EmailContentConfig | None = None,
    *,
    test: bool = False,
    expected_date: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[str, str, str]:
    """Return ``(subject, text, html)``; the two-argument legacy call remains valid."""
    rendered = render_email_preview(
        edition,
        site_url,
        config,
        test=test,
        expected_date=expected_date,
        unsubscribe_url=unsubscribe_url,
    )
    return rendered.subject, rendered.text, rendered.html
