"""Source registry: the only origins the pipeline is allowed to touch.

Reuters 的公开 RSS 已停止提供；在确定合规聚合入口前不接入
（详见 tests/fixtures/feeds/SOURCES.md）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    feed_url: str
    kind: str  # "full": participates in main articles; "brief": headlines only
    allowed_domains: tuple[str, ...]
    max_articles: int = 8


SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        key="bbc",
        name="BBC News",
        feed_url="https://feeds.bbci.co.uk/news/world/rss.xml",
        kind="full",
        allowed_domains=("bbc.co.uk", "bbc.com", "bbci.co.uk", "bbcimg.co.uk"),
    ),
    SourceConfig(
        key="guardian",
        name="The Guardian",
        feed_url="https://www.theguardian.com/world/rss",
        kind="full",
        allowed_domains=("theguardian.com", "guim.co.uk"),
    ),
    SourceConfig(
        key="npr",
        name="NPR",
        feed_url="https://feeds.npr.org/1004/rss.xml",
        kind="full",
        allowed_domains=("npr.org",),
    ),
    SourceConfig(
        key="dw",
        name="DW English",
        feed_url="https://rss.dw.com/rdf/rss-en-all",
        kind="full",
        allowed_domains=("dw.com",),
    ),
    SourceConfig(
        key="aljazeera",
        name="Al Jazeera English",
        feed_url="https://www.aljazeera.com/xml/rss/all.xml",
        kind="full",
        allowed_domains=("aljazeera.com",),
    ),
    SourceConfig(
        key="france24",
        name="France 24 English",
        feed_url="https://www.france24.com/en/rss",
        kind="full",
        allowed_domains=("france24.com",),
    ),
    SourceConfig(
        key="nyt",
        name="The New York Times",
        feed_url="https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        kind="brief",
        allowed_domains=("nytimes.com", "nyt.com"),
        max_articles=10,
    ),
)


def full_sources() -> tuple[SourceConfig, ...]:
    return tuple(source for source in SOURCES if source.kind == "full")


def brief_sources() -> tuple[SourceConfig, ...]:
    return tuple(source for source in SOURCES if source.kind == "brief")
