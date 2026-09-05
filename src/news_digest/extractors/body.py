"""Article body extraction and cleaning.

实现选择（阶段 2 决定）：trafilatura。理由：对新闻站点的正文/元数据抽取
准确率与维护活跃度俱佳，附带作者与主图元数据；相比 readability-lxml
（长期少维护）与 jusText（需自行拼装元数据）综合成本最低。
提取输出为纯文本段落，再经 nh3 双保险清洗；失败返回 None，由流水线降级为摘要。
"""

import re
from dataclasses import dataclass, field

import trafilatura

from news_digest.textutil import collapse_ws, html_to_text

_MIN_PARAGRAPH_COUNT = 2
_WORDS_PER_MINUTE = 200
EXTRACTOR_VERSION = f"trafilatura-{trafilatura.__version__}/clean-2"

# 常见站点样板句（页脚、分享、订阅、播放器占位）；仅对短段落生效，避免误伤正文。
_BOILERPLATE = re.compile(
    r"terms\s*&\s*conditions|privacy policy|sign up (for|to)|subscribe to"
    r"|follow us|share this|related topics|browser extensions?"
    r"|video player|stay with us for more|read more:|also read:",
    re.IGNORECASE,
)
_BOILERPLATE_MAX_CHARS = 140


@dataclass(frozen=True)
class ExtractedBody:
    paragraphs: list[str] = field(default_factory=list)
    author: str = ""
    image_url: str = ""


def extract_body(
    page_html: str, url: str, *, diagnostics: dict | None = None
) -> ExtractedBody | None:
    """Return cleaned paragraphs plus metadata, or None if not extractable."""
    details = diagnostics if diagnostics is not None else {}
    details.update(
        version=EXTRACTOR_VERSION,
        completeness="unverified",
        raw_paragraphs=0,
        removed_paragraphs=0,
        kept_paragraphs=0,
        fallback_reason=None,
    )
    text = trafilatura.extract(
        page_html,
        url=url,
        output_format="txt",
        favor_precision=True,
        include_comments=False,
        include_tables=True,
    )
    if not text:
        details["fallback_reason"] = "EXTRACT_EMPTY"
        return None
    paragraphs = [
        collapse_ws(html_to_text(line))
        for line in text.splitlines()
        if collapse_ws(line)
    ]
    details["raw_paragraphs"] = len(paragraphs)
    paragraphs = [
        p
        for p in paragraphs
        if not (len(p) <= _BOILERPLATE_MAX_CHARS and _BOILERPLATE.search(p))
    ]
    details["kept_paragraphs"] = len(paragraphs)
    details["removed_paragraphs"] = details["raw_paragraphs"] - len(paragraphs)
    if len(paragraphs) < _MIN_PARAGRAPH_COUNT:
        details["fallback_reason"] = "EXTRACT_TOO_FEW_PARAGRAPHS"
        return None
    if len(set(paragraphs)) == 1:
        # 全部段落相同：典型的播放器占位或反爬占位页
        details["fallback_reason"] = "EXTRACT_REPEATED_PLACEHOLDER"
        return None

    author = ""
    image_url = ""
    metadata = trafilatura.extract_metadata(page_html, default_url=url)
    if metadata is not None:
        author = collapse_ws(html_to_text(metadata.author or ""))
        image_url = metadata.image or ""
    return ExtractedBody(paragraphs=paragraphs, author=author, image_url=image_url)


def reading_minutes(paragraphs: list[str]) -> int:
    words = sum(len(paragraph.split()) for paragraph in paragraphs)
    return max(1, round(words / _WORDS_PER_MINUTE))
