"""每日选题 select_daily 的行为测试。"""

from datetime import UTC, datetime

from news_digest.models import Article, Paragraph
from news_digest.selection.score import Selection, select_daily

_REF = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _article(
    slug: str,
    *,
    title: str,
    source: str = "bbc",
    published_at: str = "2026-07-26T09:00:00+00:00",
    reading_minutes: int = 5,
    content_status: str = "full",
) -> Article:
    return Article(
        slug=slug,
        source=source,
        title_en=title,
        summary_en=f"Summary of {slug}",
        author="",
        published_at=published_at,
        url=f"https://example.com/{slug}",
        reading_minutes=reading_minutes,
        paragraphs=[Paragraph(en=f"Body of {slug}")],
        content_status=content_status,
    )


def test_empty_input():
    assert select_daily([], reference_time=_REF) == Selection(mains=[], overflow=[])


def test_deterministic_across_calls():
    # 含同一时刻发布的平分文章，验证确定性平局裁决
    articles = [
        _article(
            "night-trains",
            title="Night trains stage a comeback",
            published_at="2026-07-26T08:00:00+00:00",
        ),
        _article(
            "fog-farms",
            title="Desert farms bet on fog harvesting",
            source="dw",
            published_at="2026-07-26T08:00:00+00:00",
        ),
        _article(
            "ocean-atlas",
            title="Ocean atlas maps hidden seamounts",
            source="npr",
            published_at="2026-07-26T05:00:00+00:00",
        ),
    ]
    first = select_daily(articles, reference_time=_REF)
    second = select_daily(articles, reference_time=_REF)
    assert first == second


def test_per_source_cap_limits_mains():
    bbc_titles = [
        "Chip makers race for cheaper lithography",
        "Coral nurseries rebound after heatwave",
        "Old subway cars become artificial reefs",
        "Glacier monitors switch to solar power",
    ]
    articles = [
        _article(f"bbc-{i}", title=t, published_at=f"2026-07-26T{8 + i:02d}:00:00+00:00")
        for i, t in enumerate(bbc_titles)
    ]
    articles += [
        _article(
            "guardian-1",
            title="Night trains stage a comeback",
            source="guardian",
            published_at="2026-07-26T03:00:00+00:00",
        ),
        _article(
            "npr-1",
            title="Desert farms bet on fog harvesting",
            source="npr",
            published_at="2026-07-26T03:00:00+00:00",
        ),
    ]
    result = select_daily(articles, reference_time=_REF)
    bbc_in_mains = [a for a in result.mains if a.source == "bbc"]
    assert len(bbc_in_mains) == 2
    # 配额内保留分数最高（最新）的两篇，其余被挤到 overflow 而非丢弃
    assert {a.slug for a in bbc_in_mains} == {"bbc-3", "bbc-2"}
    assert len(result.mains) == 4
    assert {"bbc-0", "bbc-1"} <= {a.slug for a in result.overflow}


def test_similar_titles_only_one_selected():
    a = _article(
        "wildfire-a",
        title="Wildfire evacuations expand across southern Europe",
        published_at="2026-07-26T10:00:00+00:00",
    )
    b = _article(
        "wildfire-b",
        title="Wildfire evacuations expand across southern Europe.",
        source="guardian",
        published_at="2026-07-26T10:00:00+00:00",
    )
    result = select_daily([a, b], reference_time=_REF)
    assert result.mains == [a]
    assert result.overflow == [b]


def test_full_content_outranks_summary():
    full = _article("full-story", title="Night trains stage a comeback")
    summary = _article(
        "summary-story",
        title="Desert farms bet on fog harvesting",
        source="dw",
        content_status="summary",
    )
    result = select_daily([summary, full], reference_time=_REF, main_count=1)
    assert result.mains == [full]
    assert result.overflow == [summary]


def test_fresher_article_wins():
    fresh = _article(
        "fresh",
        title="Ocean atlas maps hidden seamounts",
        published_at="2026-07-26T11:00:00+00:00",
    )
    stale = _article(
        "stale",
        title="Chip makers race for cheaper lithography",
        source="npr",
        published_at="2026-07-25T16:00:00+00:00",
    )
    result = select_daily([stale, fresh], reference_time=_REF, main_count=1)
    assert result.mains == [fresh]


def test_partition_covers_all_articles():
    titles = [
        "Night trains stage a comeback",
        "Desert farms bet on fog harvesting",
        "Ocean atlas maps hidden seamounts",
        "Chip makers race for cheaper lithography",
        "Coral nurseries rebound after heatwave",
    ]
    sources = ["bbc", "dw", "npr", "guardian", "france24"]
    articles = [
        _article(
            f"story-{i}",
            title=t,
            source=s,
            published_at=f"2026-07-26T{5 + i:02d}:00:00+00:00",
        )
        for i, (t, s) in enumerate(zip(titles, sources, strict=True))
    ]
    snapshot = list(articles)
    result = select_daily(articles, reference_time=_REF, main_count=3)
    assert len(result.mains) == 3
    combined = result.mains + result.overflow
    assert sorted(a.slug for a in combined) == sorted(a.slug for a in articles)
    # 输入列表未被修改
    assert articles == snapshot
    # overflow 按 published_at 降序（同为 +00:00，字符串序即时间序）
    overflow_times = [a.published_at for a in result.overflow]
    assert overflow_times == sorted(overflow_times, reverse=True)


def test_all_selected_when_main_count_exceeds_candidates():
    articles = [
        _article("solo-1", title="Night trains stage a comeback"),
        _article("solo-2", title="Desert farms bet on fog harvesting", source="dw"),
        _article("solo-3", title="Ocean atlas maps hidden seamounts", source="npr"),
    ]
    result = select_daily(articles, reference_time=_REF)
    assert len(result.mains) == 3
    assert result.overflow == []
