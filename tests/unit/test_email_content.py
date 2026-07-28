"""Offline matrix tests for deterministic email content selection and building."""

import dataclasses
import email.policy

import pytest

from news_digest.delivery.email_content import (
    EmailContentConfig,
    build_email_message,
    select_email_content,
)
from news_digest.models import Article, BriefItem, DailyEdition, Paragraph
from news_digest.rendering.email import render_email, render_email_preview

SITE = "https://news.example.com"
DATE = "2026-07-26"


def _article(index: int, *, source: str | None = None, translated: bool = True) -> Article:
    suffix = " ".join([f"sentence-{index}"] * 80)
    return Article(
        slug=f"story-{index}",
        source=source or ("BBC News" if index % 2 else "NPR"),
        title_en=f"English headline {index}",
        title_zh=f"中文标题 {index}" if translated else "",
        summary_en=f"English summary {index} {suffix}",
        summary_zh=f"中文摘要 {index} {suffix}" if translated else "",
        author="Reporter",
        published_at=f"{DATE}T09:00:00+00:00",
        url=f"https://source.example.com/story-{index}",
        reading_minutes=index + 2,
        paragraphs=[Paragraph(en="One.", zh="一。" if translated else "")],
        translated_by="model@p2" if translated else "",
    )


def _brief(index: int, *, source: str | None = None, translated: bool = True) -> BriefItem:
    return BriefItem(
        title_en=f"English brief {index}",
        title_zh=f"中文简讯 {index}" if translated else "",
        source=source or ("BBC News" if index % 2 else "Reuters"),
        url=f"https://source.example.com/brief-{index}",
    )


def _edition() -> DailyEdition:
    return DailyEdition(
        date=DATE,
        articles=[_article(1), _article(2), _article(3)],
        briefs=[_brief(1), _brief(2), _brief(3)],
    )


@pytest.mark.parametrize("language", ["bi", "zh", "en"])
@pytest.mark.parametrize("layout", ["digest", "compact"])
@pytest.mark.parametrize("summary_length", ["short", "standard", "long"])
def test_language_layout_summary_matrix(language, layout, summary_length):
    config = EmailContentConfig(
        main_limit=2,
        brief_limit=2,
        language=language,
        layout=layout,
        summary_length=summary_length,
    )
    rendered = render_email_preview(_edition(), SITE, config)

    assert rendered.subject == f"Cheapcoding News 已更新｜{DATE}"
    assert rendered.metadata.main_count == 2
    assert rendered.metadata.brief_count == 2
    assert rendered.metadata.degraded is False
    assert SITE not in rendered.text
    assert SITE not in rendered.html
    assert "完整内容请访问 Cheapcoding News 官网" in rendered.text
    assert "完整内容请访问 Cheapcoding News 官网" in rendered.html
    assert 'name="viewport"' in rendered.html
    assert "max-width:600px" in rendered.html
    assert "<script" not in rendered.html.lower()
    assert "<form" not in rendered.html.lower()
    assert "<img" not in rendered.html.lower()

    if language == "en":
        assert "English headline 1" in rendered.text
        assert "中文标题 1" not in rendered.text
    elif language == "zh":
        assert "中文标题 1" in rendered.text
        assert "English headline 1" not in rendered.text
    else:
        assert "English headline 1" in rendered.text
        assert "中文标题 1" in rendered.text

    for body in (rendered.text, rendered.html):
        assert "English summary 1" not in body
        assert "中文摘要 1" not in body


def test_summary_lengths_are_deterministic_and_ordered():
    lengths = []
    for length in ("short", "standard", "long"):
        selected = select_email_content(
            _edition(),
            SITE,
            EmailContentConfig(
                briefs_enabled=False,
                main_limit=1,
                brief_limit=0,
                summary_length=length,
            ),
        )
        lengths.append(len(selected.mains[0].summary_en))
    assert lengths == sorted(lengths)
    assert len(set(lengths)) == 3
    assert 90 <= lengths[0] <= 96
    assert all(selected > 0 for selected in lengths)


@pytest.mark.parametrize(
    ("mains_enabled", "briefs_enabled", "main_limit", "brief_limit", "expected"),
    [
        (True, False, 0, 0, "empty"),
        (False, True, 0, 0, "empty"),
        (True, True, 4, 1, "main_limit"),
        (True, True, 1, 4, "brief_limit"),
        (False, False, 0, 0, "at least one"),
    ],
)
def test_content_and_count_boundaries(
    mains_enabled, briefs_enabled, main_limit, brief_limit, expected
):
    with pytest.raises(ValueError, match=expected):
        config = EmailContentConfig(
            mains_enabled=mains_enabled,
            briefs_enabled=briefs_enabled,
            main_limit=main_limit,
            brief_limit=brief_limit,
        )
        select_email_content(_edition(), SITE, config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"main_limit": -1},
        {"brief_limit": -1},
        {"language": "fr"},
        {"layout": "freeform"},
        {"summary_length": "huge"},
        {"topic_filters": ("technology",)},
        {"source_filters": ("",)},
    ],
)
def test_invalid_config_rejected(overrides):
    with pytest.raises(ValueError):
        EmailContentConfig(**overrides)


def test_source_filter_uses_only_source_and_preserves_publication_order():
    selected = select_email_content(
        _edition(),
        SITE,
        EmailContentConfig(
            main_limit=2,
            brief_limit=2,
            source_filters=(" bbc news ", "BBC NEWS"),
        ),
    )
    assert [item.title_en for item in selected.mains] == [
        "English headline 1",
        "English headline 3",
    ]
    assert [item.title_en for item in selected.briefs] == [
        "English brief 1",
        "English brief 3",
    ]


def test_unknown_or_empty_source_filter_blocks_selection():
    with pytest.raises(ValueError, match="absent"):
        select_email_content(
            _edition(),
            SITE,
            EmailContentConfig(main_limit=1, brief_limit=1, source_filters=("Unknown",)),
        )

    only_npr = EmailContentConfig(
        briefs_enabled=False,
        main_limit=0,
        brief_limit=0,
        source_filters=("NPR",),
    )
    with pytest.raises(ValueError, match="empty"):
        select_email_content(_edition(), SITE, only_npr)


def test_topic_filter_is_explicitly_unsupported_without_structured_model_field():
    assert "topic" not in DailyEdition.__dataclass_fields__
    assert "topic" not in Article.__dataclass_fields__
    with pytest.raises(ValueError, match="no structured topic field"):
        EmailContentConfig(topic_filters=("world",))


def test_partial_translation_is_degraded_and_falls_back_to_english():
    edition = dataclasses.replace(
        _edition(),
        articles=[_article(1, translated=False)],
        briefs=[_brief(1, translated=False)],
    )
    rendered = render_email_preview(
        edition,
        SITE,
        EmailContentConfig(main_limit=1, brief_limit=1, language="zh"),
    )
    assert rendered.metadata.degraded is True
    assert "译文暂缺" in rendered.text
    assert "English headline 1" in rendered.text
    assert "English brief 1" not in rendered.text
    for value in ("译文暂缺", "English headline 1"):
        assert value in rendered.html
    assert "English brief 1" not in rendered.html

    english = render_email_preview(
        edition,
        SITE,
        EmailContentConfig(main_limit=1, brief_limit=1, language="en"),
    )
    assert english.metadata.degraded is False
    assert "译文暂缺" not in english.text


def test_email_date_identity_has_no_news_links():
    rendered = render_email_preview(
        _edition(),
        SITE,
        EmailContentConfig(main_limit=3, brief_limit=0, briefs_enabled=False),
        expected_date=DATE,
    )
    assert DATE in rendered.subject
    for body in (rendered.text, rendered.html):
        assert "完整内容请访问 Cheapcoding News 官网" in body
        assert SITE not in body
        assert "https://source.example.com" not in body
        assert "/current" not in body


def test_old_release_expected_date_mismatch_and_empty_edition_rejected():
    with pytest.raises(ValueError, match="does not match"):
        render_email_preview(_edition(), SITE, expected_date="2026-07-27")
    with pytest.raises(ValueError, match="at least one|empty"):
        render_email(DailyEdition(date=DATE), SITE)


@pytest.mark.parametrize(
    "site_url",
    [
        "http://news.example.com",
        "https://127.0.0.1",
        "https://localhost",
        "https://news.example.com/path",
        "https://user:secret@news.example.com",
        "https://news.example.com?x=1",
        "https://news.example.com#x",
        "https://news.example.com\r\nBcc:x@example.com",
        "not-a-url",
    ],
)
def test_invalid_news_site_url_rejected(site_url):
    with pytest.raises(ValueError, match="NEWS_SITE_URL"):
        render_email(_edition(), site_url)


def test_invalid_release_date_and_article_link_block_building():
    with pytest.raises(ValueError, match="edition date"):
        render_email(dataclasses.replace(_edition(), date="2026-7-26"), SITE)
    broken = dataclasses.replace(_edition(), articles=[_article(1), _article(2)])
    broken.articles[0] = dataclasses.replace(broken.articles[0], slug="../escape")
    with pytest.raises(ValueError, match="slug"):
        render_email(broken, SITE)


def test_test_subject_and_subject_input_are_header_safe():
    subject, _, _ = render_email(_edition(), SITE, test=True)
    assert subject == f"[测试] Cheapcoding News 已更新｜{DATE}"
    assert "\r" not in subject and "\n" not in subject
    with pytest.raises(ValueError, match="edition date"):
        render_email(dataclasses.replace(_edition(), date=f"{DATE}\r\nBcc:x@example.com"), SITE)


def test_unified_builder_is_a_short_utf8_html_notice():
    config = EmailContentConfig(main_limit=2, brief_limit=2, language="bi")
    message = build_email_message(
        _edition(),
        SITE,
        "news@example.com",
        ("reader@example.com",),
        config,
        expected_date=DATE,
    )
    assert message.get_content_type() == "text/html"
    assert message.get_content_charset() == "utf-8"

    parsed = email.message_from_bytes(bytes(message), policy=email.policy.default)
    html = parsed.get_content()
    core = ["English headline 1", "English headline 2"]
    for value in core:
        assert value in html
    assert "English brief 1" not in html
    assert "English summary 1" not in html
    assert SITE not in html
    assert "https://source.example.com" not in html
    assert "完整内容请访问 Cheapcoding News 官网" in html
    assert message.get_body(preferencelist=("plain",)) is None
    assert message["List-Unsubscribe"] is None
    assert message["List-Unsubscribe-Post"] is None


def test_recipient_specific_unsubscribe_is_in_html_body_and_headers():
    url = f"{SITE}/unsubscribe/high-entropy-token"
    message = build_email_message(
        _edition(),
        SITE,
        "news@example.com",
        ("reader@example.com",),
        EmailContentConfig(main_limit=1, brief_limit=1),
        expected_date=DATE,
        unsubscribe_url=url,
    )
    assert str(message["List-Unsubscribe"]) == f"<{url}>"
    assert str(message["List-Unsubscribe-Post"]) == "List-Unsubscribe=One-Click"
    html = message.get_content()
    assert url in html
    assert f"{SITE}/issues/{DATE}/" not in html
    assert "https://source.example.com" not in html.replace(url, "")
    assert message.get_body(preferencelist=("html",)) is not None
    assert message.get_body(preferencelist=("plain",)) is None


def test_legacy_two_argument_call_uses_compatible_defaults():
    subject, text, html = render_email(_edition(), SITE)
    assert subject == f"Cheapcoding News 已更新｜{DATE}"
    assert SITE not in text
    assert "完整内容请访问 Cheapcoding News 官网" in text
    assert "English headline 3" in html
    assert "English brief 1" not in html
