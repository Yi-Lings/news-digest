"""SQLite 存储:按日归档文章与快讯,payload 序列化为 JSON。全项目的 SQL 仅出现在本模块。"""

import json
import sqlite3
from pathlib import Path

from news_digest.models import (
    Article,
    BriefItem,
    DailyEdition,
    article_from_dict,
    article_to_dict,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    slug TEXT NOT NULL,
    source TEXT NOT NULL,
    translated_by TEXT NOT NULL DEFAULT '',
    content_status TEXT NOT NULL,
    published_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_date ON articles(date);
CREATE TABLE IF NOT EXISTS briefs (
    url TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_briefs_date ON briefs(date);
"""


def connect(path: Path) -> sqlite3.Connection:
    """打开数据库,父目录与表按需创建,并校验 schema 版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif row["value"] != str(SCHEMA_VERSION):
        found = row["value"]
        conn.close()
        raise RuntimeError(f"schema 版本不匹配:库中为 {found},代码期望 {SCHEMA_VERSION},需迁移")
    return conn


def upsert_articles(conn: sqlite3.Connection, date: str, articles: list[Article]) -> None:
    """按 url 写入文章,单个事务提交。

    覆盖规则:库中已翻译(translated_by 非空)的行不被未翻译的新版本覆盖,
    避免重新抓取刷掉翻译成果;其余情况一律用新数据整体覆盖。
    """
    with conn:
        for article in articles:
            if not article.translated_by:
                row = conn.execute(
                    "SELECT translated_by FROM articles WHERE url = ?", (article.url,)
                ).fetchone()
                if row is not None and row["translated_by"]:
                    continue
            conn.execute(
                "INSERT OR REPLACE INTO articles"
                " (url, date, slug, source, translated_by, content_status, published_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    article.url,
                    date,
                    article.slug,
                    article.source,
                    article.translated_by,
                    article.content_status,
                    article.published_at,
                    json.dumps(article_to_dict(article), ensure_ascii=False),
                ),
            )


def upsert_briefs(conn: sqlite3.Connection, date: str, briefs: list[BriefItem]) -> None:
    """按 url 写入快讯,直接覆盖,单个事务提交。"""
    with conn:
        for brief in briefs:
            conn.execute(
                "INSERT OR REPLACE INTO briefs (url, date, payload) VALUES (?, ?, ?)",
                (brief.url, date, json.dumps(vars(brief), ensure_ascii=False)),
            )


def get_edition(conn: sqlite3.Connection, date: str) -> DailyEdition | None:
    """组装指定日期的日刊;该日期完全无数据时返回 None。"""
    article_rows = conn.execute(
        "SELECT payload FROM articles WHERE date = ? ORDER BY published_at DESC", (date,)
    ).fetchall()
    brief_rows = conn.execute(
        "SELECT payload FROM briefs WHERE date = ? ORDER BY url", (date,)
    ).fetchall()
    if not article_rows and not brief_rows:
        return None
    return DailyEdition(
        date=date,
        articles=[article_from_dict(json.loads(row["payload"])) for row in article_rows],
        briefs=[BriefItem(**json.loads(row["payload"])) for row in brief_rows],
    )


def list_dates(conn: sqlite3.Connection) -> list[str]:
    """articles 与 briefs 两表出现过的日期并集,降序。"""
    rows = conn.execute(
        "SELECT date FROM articles UNION SELECT date FROM briefs ORDER BY date DESC"
    ).fetchall()
    return [row["date"] for row in rows]
