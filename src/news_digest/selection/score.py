"""每日选题：按时效、全文与篇幅评分，贪心挑选首页文章与备选池。"""

import datetime
from dataclasses import dataclass
from difflib import SequenceMatcher

from news_digest.models import Article

# 时效窗口（小时）：距基准 0 小时得 1.0，线性衰减到 24 小时得 0.0，
# 超过 24 小时按 0——首页应以最近一天的新闻为主。
_FRESHNESS_WINDOW_HOURS = 24.0
# 全文加成：正文完整的文章阅读价值更高，摘要降级的少上首页。
_FULL_CONTENT_BONUS = 0.5
# 篇幅适中加成：3-8 分钟（含）适合每日阅读，太短没内容、太长读不完。
_MODERATE_LENGTH_BONUS = 0.2
_MODERATE_MIN_MINUTES = 3
_MODERATE_MAX_MINUTES = 8


@dataclass(frozen=True)
class Selection:
    mains: list[Article]
    overflow: list[Article]


def _score(article: Article, published: datetime.datetime, reference: datetime.datetime) -> float:
    """时效分 + 全文加成 + 篇幅适中加成；时效分钳制在 [0, 1]。"""
    age_hours = (reference - published).total_seconds() / 3600
    score = min(1.0, max(0.0, 1.0 - age_hours / _FRESHNESS_WINDOW_HOURS))
    if article.content_status == "full":
        score += _FULL_CONTENT_BONUS
    if _MODERATE_MIN_MINUTES <= article.reading_minutes <= _MODERATE_MAX_MINUTES:
        score += _MODERATE_LENGTH_BONUS
    return score


def _similar(title_a: str, title_b: str, threshold: float) -> bool:
    return SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio() >= threshold


def select_daily(
    articles: list[Article],
    *,
    reference_time: datetime.datetime,
    main_count: int = 6,
    per_source_cap: int = 2,
    similarity_threshold: float = 0.85,
) -> Selection:
    """贪心挑选 mains，其余进 overflow；两者合计恰为输入全集，不修改输入。"""
    published = [datetime.datetime.fromisoformat(a.published_at) for a in articles]
    scores = [
        _score(article, when, reference_time)
        for article, when in zip(articles, published, strict=True)
    ]
    # 分数降序；平分按 published_at 降序、slug 升序，保证确定性。
    ranked = sorted(
        range(len(articles)),
        key=lambda i: (-scores[i], -published[i].timestamp(), articles[i].slug),
    )
    mains: list[Article] = []
    picked: set[int] = set()
    per_source: dict[str, int] = {}
    for i in ranked:
        if len(mains) >= main_count:
            break
        article = articles[i]
        # 单一来源配额：避免首页被同一来源刷屏。
        if per_source.get(article.source, 0) >= per_source_cap:
            continue
        # 标题近似互斥：同题材只留最先选中的一篇。
        if any(_similar(article.title_en, m.title_en, similarity_threshold) for m in mains):
            continue
        mains.append(article)
        picked.add(i)
        per_source[article.source] = per_source.get(article.source, 0) + 1
    leftovers = sorted(
        (i for i in range(len(articles)) if i not in picked),
        key=lambda i: (-published[i].timestamp(), articles[i].slug),
    )
    return Selection(mains=mains, overflow=[articles[i] for i in leftovers])
