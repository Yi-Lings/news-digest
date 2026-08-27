"""阶段 4 完整流水线：入库、选题、幂等、翻译保护（全部离线）。"""

import datetime
from pathlib import Path

import httpx
import pytest

from news_digest.config import BuildConfig, FetchConfig
from news_digest.pipeline import (
    build_editions,
    fetch_daily,
    load_db_editions,
    selected_mains_for_translation,
    store_translated,
)
from news_digest.translation.schema import split_sentences
from news_digest.translation.service import translate_edition

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime.datetime(2026, 7, 26, 12, 0, tzinfo=datetime.UTC)

_FEED_FILES = {
    "feeds.bbci.co.uk": "bbc.xml",
    "www.theguardian.com": "guardian.xml",
    "feeds.npr.org": "npr.xml",
    "rss.dw.com": "dw.xml",
    "www.aljazeera.com": "aljazeera.xml",
    "www.france24.com": "france24.xml",
    "rss.nytimes.com": "nyt.xml",
}


def _handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host in _FEED_FILES:
        return httpx.Response(200, content=(FIXTURES / "feeds" / _FEED_FILES[host]).read_bytes())
    if request.url.path == "/news/articles/cevmdxz4872o":
        return httpx.Response(
            200, content=(FIXTURES / "pages" / "bbc-article.html").read_bytes()
        )
    return httpx.Response(404, content=b"not found")


@pytest.fixture
def no_dns(monkeypatch):
    monkeypatch.setattr("news_digest.sources.http.assert_public_host", lambda host: None)


def _config(tmp_path: Path) -> FetchConfig:
    return FetchConfig(
        proxy=None, window_hours=24, timezone="Asia/Shanghai", data_dir=tmp_path / "data"
    )


def _fetch(config: FetchConfig):
    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    with client:
        return fetch_daily(config, client=client, now=NOW)


class FakeTranslator:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def label(self) -> str:
        return "fake@p2"

    @property
    def model(self) -> str:
        return "fake"

    def translate(self, article) -> str:
        import json

        self.calls += 1
        return json.dumps(
            {
                "title_zh": "中文标题：测试",
                "summary_zh": "中文摘要。",
                "sentences_zh": [
                    ["中文句子。"] * len(split_sentences(paragraph.en))
                    for paragraph in article.paragraphs
                ],
                "vocabulary": [
                    {
                        "word": "report",
                        "phonetic": "/rɪˈpɔːrt/",
                        "meaning_zh": "报道",
                        "example_en": "The report describes the latest development.",
                    },
                    {
                        "word": "official",
                        "phonetic": "/əˈfɪʃəl/",
                        "meaning_zh": "官员",
                        "example_en": "An official commented on the event.",
                    },
                    {
                        "word": "public",
                        "phonetic": "/ˈpʌblɪk/",
                        "meaning_zh": "公众",
                        "example_en": "The public received an update.",
                    },
                ],
                "collocations": [
                    {
                        "phrase": "according to",
                        "meaning_zh": "根据",
                        "example_en": "According to the report, the situation changed.",
                    }
                ],
                "sentence_notes": [
                    {
                        "sentence_en": "The report describes the latest development.",
                        "translation_zh": "报道描述了最新进展。",
                        "analysis_zh": "主谓宾结构。",
                    }
                ],
            },
            ensure_ascii=False,
        )


def test_selection_applied_and_pool_preserved(tmp_path, no_dns):
    config = _config(tmp_path)
    edition, _ = _fetch(config)
    assert edition is not None

    editions = load_db_editions(config, now=NOW)
    assert len(editions) == 1
    today = editions[0]
    assert len(today.articles) <= 6
    for source in {a.source for a in today.articles}:
        assert sum(1 for a in today.articles if a.source == source) <= 2
    # 主文章 + 溢出简讯 = 全部入库全文文章；NYT 简讯另计
    pool_size = len(edition.articles)
    overflow_briefs = [b for b in today.briefs if b.source != "The New York Times"]
    assert len(today.articles) + len(overflow_briefs) == pool_size


def test_rerun_same_day_is_idempotent(tmp_path, no_dns):
    config = _config(tmp_path)
    first, _ = _fetch(config)
    second, _ = _fetch(config)
    assert first is not None and second is not None

    editions = load_db_editions(config, now=NOW)
    assert len(editions) == 1  # 不产生重复归档日期
    output_root = tmp_path / "site"
    build_editions(editions, BuildConfig(output_root=output_root, site_url="http://x"))
    manifest = output_root / "current" / "release.json"
    assert manifest.is_file()
    assert '"release_date": "2026-07-26"' in manifest.read_text(encoding="utf-8")
    build_editions(editions, BuildConfig(output_root=output_root, site_url="http://x"))
    releases = sorted(p.name for p in (output_root / "releases").iterdir())
    assert releases == ["2026-07-26-01", "2026-07-26-02"]
    archive = (output_root / "current" / "archive" / "index.html").read_text(encoding="utf-8")
    assert archive.count("2026-07-26</a>") == 1


def test_translation_survives_refetch_and_not_repeated(tmp_path, no_dns):
    config = _config(tmp_path)
    _fetch(config)
    mains = selected_mains_for_translation(config, "2026-07-26", now=NOW)
    assert mains is not None

    translator = FakeTranslator()
    updated, report = translate_edition(mains, translator, tmp_path / "cache")
    assert report.succeeded == len(mains.articles)
    store_translated(config, "2026-07-26", updated.articles)

    # 重新抓取（未翻译版本）不得覆盖已翻译成果
    _fetch(config)
    mains_after = selected_mains_for_translation(config, "2026-07-26", now=NOW)
    translated = [a for a in mains_after.articles if a.translated_by]
    assert len(translated) == len(updated.articles)

    # 再翻译一轮：全部跳过，不产生 API 调用
    translator2 = FakeTranslator()
    _, report2 = translate_edition(mains_after, translator2, tmp_path / "cache")
    assert translator2.calls == 0
    assert report2.already_done == len(mains_after.articles)

    # 构建出的页面含中文标题与溢出简讯
    editions = load_db_editions(config, now=NOW)
    output_root = tmp_path / "site"
    build_editions(editions, BuildConfig(output_root=output_root, site_url="http://x"))
    index = (output_root / "current" / "index.html").read_text(encoding="utf-8")
    assert "中文标题：" in index


def test_import_edition_cli(tmp_path, no_dns, monkeypatch):
    """import-edition：外部版次 JSON 併入库，译文保留，幂等。"""
    import json

    from news_digest.cli import main
    from news_digest.models import edition_to_dict
    from news_digest.storage import db

    config = _config(tmp_path)
    _fetch(config)
    mains = selected_mains_for_translation(config, "2026-07-26", now=NOW)
    translated, _ = translate_edition(mains, FakeTranslator(), tmp_path / "cache")

    payload = {"generated_at": "x", "edition": edition_to_dict(translated)}
    source = tmp_path / "founding.json"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    target = tmp_path / "server-data"
    monkeypatch.chdir(tmp_path)  # load_env_file 在空目录中安全无操作
    monkeypatch.setenv("NEWS_DATA_DIR", str(target))
    assert main(["import-edition", str(source)]) == 0
    assert main(["import-edition", str(source)]) == 0  # 幂等重跑

    conn = db.connect(target / "news.db")
    try:
        edition = db.get_edition(conn, "2026-07-26")
    finally:
        conn.close()
    assert edition is not None
    assert all(article.translated_by for article in edition.articles)


def test_legacy_fetched_json_auto_import(tmp_path, no_dns):
    config = _config(tmp_path)
    edition, _ = _fetch(config)
    # 模拟老布局：只有 fetched json、没有数据库
    db_file = config.database
    db_file.unlink()
    editions = load_db_editions(config, now=NOW)
    assert editions and editions[0].date == "2026-07-26"
    assert len(editions[0].articles) <= 6
