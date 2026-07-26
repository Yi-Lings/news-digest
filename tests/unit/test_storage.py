"""storage.db 的离线单元测试:建库、round-trip、翻译保护、日期列表与持久化。

测试不直接写 SQL(SQL 仅允许出现在 storage.db 内),schema_version 的正确性
通过行为证明:版本一致时重复 connect 成功,篡改期望版本后 connect 必须报错。
"""

import pytest

from news_digest.models import (
    Article,
    ArticleImage,
    BriefItem,
    Collocation,
    Paragraph,
    SentenceNote,
    VocabularyItem,
)
from news_digest.storage import db


def _article(url: str, **overrides) -> Article:
    """构造必填字段齐全的测试文章,任意字段可用关键字覆盖。"""
    fields = {
        "slug": "test-article",
        "source": "Example Wire",
        "title_en": "Example Title",
        "summary_en": "Example summary.",
        "author": "Ada Writer",
        "published_at": "2026-07-26T08:00:00+00:00",
        "url": url,
        "reading_minutes": 4,
        "paragraphs": [Paragraph(en="First paragraph.", zh="第一段。")],
    }
    fields.update(overrides)
    return Article(**fields)


def test_connect_creates_db_idempotently_and_records_schema_version(tmp_path, monkeypatch):
    db_path = tmp_path / "state" / "digest.db"
    first = db.connect(db_path)  # 空目录:父目录与库文件自动创建
    first.close()
    assert db_path.exists()

    second = db.connect(db_path)  # 重复 connect 幂等,库可正常使用
    assert db.list_dates(second) == []
    second.close()

    # 库中已写入 schema_version 且等于 SCHEMA_VERSION:
    # 上面版本一致的 connect 成功,而换一个期望版本后必须因不匹配报错。
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="schema 版本不匹配"):
        db.connect(db_path)


def test_upsert_and_get_edition_round_trip(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    early = _article("https://example.com/early", published_at="2026-07-26T06:00:00+00:00")
    late = _article(
        "https://example.com/late",
        published_at="2026-07-26T09:30:00+00:00",
        title_zh="晚间标题",
        summary_zh="中文摘要。",
        translated_by="m@p1",
        paragraphs=[Paragraph(en="Body text.", zh="正文。")],
        vocabulary=[VocabularyItem("resilient", "/rɪˈzɪliənt/", "有韧性的", "A resilient grid.")],
        collocations=[Collocation("carry out", "执行", "They carry out repairs.")],
        sentence_notes=[SentenceNote("It holds.", "它撑得住。", "主谓结构,一般现在时。")],
        image=ArticleImage("https://example.com/a.jpg", "A power grid", "Example/Getty"),
        content_status="full",
    )
    briefs = [
        BriefItem(title_en="Zulu", source="Wire", url="https://example.com/z"),
        BriefItem(title_en="Alpha", source="Wire", url="https://example.com/a", title_zh="甲"),
    ]
    db.upsert_articles(conn, "2026-07-26", [early, late])
    db.upsert_briefs(conn, "2026-07-26", briefs)

    edition = db.get_edition(conn, "2026-07-26")
    assert edition is not None
    assert edition.date == "2026-07-26"
    # 文章按 published_at 降序;dataclass 相等性覆盖全部嵌套字段的完整还原
    assert edition.articles == [late, early]
    # 快讯按 url 升序
    assert edition.briefs == [briefs[1], briefs[0]]
    assert db.get_edition(conn, "2000-01-01") is None
    conn.close()


def test_untranslated_refetch_does_not_overwrite_translated_row(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    url = "https://example.com/protected"
    translated = _article(
        url, title_zh="已翻译标题", summary_zh="已翻译摘要。", translated_by="m@p2"
    )
    db.upsert_articles(conn, "2026-07-26", [translated])

    refetched = _article(url, title_zh="", translated_by="")  # 重新抓取的未翻译版本
    db.upsert_articles(conn, "2026-07-26", [refetched])

    edition = db.get_edition(conn, "2026-07-26")
    assert edition is not None
    assert edition.articles == [translated]  # 翻译成果原样保留
    conn.close()


def test_translated_version_overwrites_untranslated_row(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    url = "https://example.com/upgraded"
    db.upsert_articles(conn, "2026-07-26", [_article(url)])  # 未翻译旧行

    translated = _article(url, title_zh="新翻译标题", translated_by="m@p2")
    db.upsert_articles(conn, "2026-07-26", [translated])

    edition = db.get_edition(conn, "2026-07-26")
    assert edition is not None
    assert edition.articles == [translated]
    conn.close()


def test_list_dates_unions_both_tables_descending(tmp_path):
    conn = db.connect(tmp_path / "digest.db")
    db.upsert_articles(conn, "2026-07-25", [_article("https://example.com/one")])
    db.upsert_articles(conn, "2026-07-26", [_article("https://example.com/two")])
    db.upsert_briefs(
        conn, "2026-07-26", [BriefItem(title_en="C", source="Wire", url="https://example.com/c")]
    )
    # 2026-07-27 仅有快讯,也必须出现在日期列表中
    db.upsert_briefs(
        conn, "2026-07-27", [BriefItem(title_en="B", source="Wire", url="https://example.com/b")]
    )
    assert db.list_dates(conn) == ["2026-07-27", "2026-07-26", "2026-07-25"]
    conn.close()


def test_data_survives_reconnect(tmp_path):
    db_path = tmp_path / "digest.db"
    conn = db.connect(db_path)
    article = _article("https://example.com/persist", title_zh="持久标题", translated_by="m@p1")
    db.upsert_articles(conn, "2026-07-26", [article])
    db.upsert_briefs(
        conn, "2026-07-26", [BriefItem(title_en="B", source="Wire", url="https://example.com/b")]
    )
    conn.close()

    reopened = db.connect(db_path)
    edition = db.get_edition(reopened, "2026-07-26")
    assert edition is not None
    assert edition.articles == [article]
    assert [brief.url for brief in edition.briefs] == ["https://example.com/b"]
    reopened.close()
