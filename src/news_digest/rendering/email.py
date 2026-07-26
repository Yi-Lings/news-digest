"""渲染双语摘要邮件（HTML + 纯文本），不涉及投递。"""

from news_digest.models import DailyEdition
from news_digest.rendering.pages import create_environment, format_date_zh


def render_email(edition: DailyEdition, site_url: str) -> tuple[str, str, str]:
    """返回 (subject, text, html)。"""
    env = create_environment()
    context = {"edition": edition, "site_url": site_url.rstrip("/")}
    subject = (
        f"Cheapcoding News · {format_date_zh(edition.date)} 双语简报"
        f"（{len(edition.articles)} 篇）"
    )
    text = env.get_template("email.txt").render(context)
    html = env.get_template("email.html").render(context)
    return subject, text, html
