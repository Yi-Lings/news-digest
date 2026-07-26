"""模型输出 schema、prompt 构造与严格校验。

版本约定：PROMPT_VERSION 变更即视为不同缓存命名空间；
非法或不完整响应一律拒绝（InvalidTranslation），绝不入缓存。
"""

import dataclasses
import json
import re
from dataclasses import dataclass, field

from news_digest.models import Article, Collocation, Paragraph, SentenceNote, VocabularyItem

PROMPT_VERSION = "p1"

SYSTEM_PROMPT = """你是一名服务于中文英语学习者的新闻翻译与教学助理。
对给定英文新闻，输出一个 JSON 对象（不要任何 JSON 以外的文字），字段如下：
- "title_zh": 中文标题，信达雅，不超过 40 字
- "summary_zh": 中文摘要，一到两句
- "paragraphs_zh": 字符串数组，逐段对应译文，数量必须与输入段落数完全一致，忠实通顺
- "vocabulary": 3 到 6 个值得学习的词，
  元素为 {"word","phonetic","meaning_zh","example_en"}，word 必须出自原文
- "collocations": 1 到 3 个固定搭配，元素为 {"phrase","meaning_zh","example_en"}
- "sentence_notes": 1 到 2 个长难句解析，
  元素为 {"sentence_en","translation_zh","analysis_zh"}，
  sentence_en 摘自原文，analysis_zh 讲清语法结构
不要在译文之外自创内容，不要输出 markdown。"""


class InvalidTranslation(ValueError):
    """模型响应非法或不完整。"""


@dataclass(frozen=True)
class TranslationResult:
    title_zh: str
    summary_zh: str
    paragraphs_zh: list[str]
    vocabulary: list[VocabularyItem] = field(default_factory=list)
    collocations: list[Collocation] = field(default_factory=list)
    sentence_notes: list[SentenceNote] = field(default_factory=list)


def build_user_prompt(article: Article) -> str:
    lines = [f"标题：{article.title_en}", f"摘要：{article.summary_en}", "正文段落："]
    for index, paragraph in enumerate(article.paragraphs, start=1):
        lines.append(f"{index}. {paragraph.en}")
    count = len(article.paragraphs)
    lines.append(f"（共 {count} 段，paragraphs_zh 必须恰好 {count} 项）")
    return "\n".join(lines)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidTranslation(f"字段 {key} 缺失或为空")
    return value.strip()


def _require_items(data: dict, key: str, fields: tuple[str, ...], factory) -> list:
    raw = data.get(key)
    if not isinstance(raw, list):
        raise InvalidTranslation(f"字段 {key} 缺失或不是数组")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise InvalidTranslation(f"{key} 元素必须是对象")
        kwargs = {}
        for name in fields:
            value = entry.get(name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidTranslation(f"{key} 元素缺少字段 {name}")
            kwargs[name] = value.strip()
        items.append(factory(**kwargs))
    return items


def parse_translation(raw_text: str, paragraph_count: int) -> TranslationResult:
    """解析并严格校验模型输出；任何缺失、错位、空值都视为非法。"""
    cleaned = _FENCE.sub("", raw_text.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise InvalidTranslation(f"响应不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise InvalidTranslation("响应顶层必须是 JSON 对象")

    paragraphs_zh = data.get("paragraphs_zh")
    if not isinstance(paragraphs_zh, list):
        raise InvalidTranslation("paragraphs_zh 缺失或不是数组")
    if len(paragraphs_zh) != paragraph_count:
        raise InvalidTranslation(
            f"paragraphs_zh 数量 {len(paragraphs_zh)} 与原文段落数 {paragraph_count} 不一致"
        )
    cleaned_paragraphs = []
    for index, item in enumerate(paragraphs_zh):
        if not isinstance(item, str) or not item.strip():
            raise InvalidTranslation(f"paragraphs_zh 第 {index + 1} 项为空")
        cleaned_paragraphs.append(item.strip())

    return TranslationResult(
        title_zh=_require_str(data, "title_zh"),
        summary_zh=_require_str(data, "summary_zh"),
        paragraphs_zh=cleaned_paragraphs,
        vocabulary=_require_items(
            data, "vocabulary", ("word", "phonetic", "meaning_zh", "example_en"), VocabularyItem
        ),
        collocations=_require_items(
            data, "collocations", ("phrase", "meaning_zh", "example_en"), Collocation
        ),
        sentence_notes=_require_items(
            data,
            "sentence_notes",
            ("sentence_en", "translation_zh", "analysis_zh"),
            SentenceNote,
        ),
    )


def result_to_dict(result: TranslationResult) -> dict:
    return {
        "title_zh": result.title_zh,
        "summary_zh": result.summary_zh,
        "paragraphs_zh": list(result.paragraphs_zh),
        "vocabulary": [vars(v) for v in result.vocabulary],
        "collocations": [vars(c) for c in result.collocations],
        "sentence_notes": [vars(s) for s in result.sentence_notes],
    }


def apply_translation(article: Article, result: TranslationResult, translated_by: str) -> Article:
    paragraphs = [
        Paragraph(en=paragraph.en, zh=zh)
        for paragraph, zh in zip(article.paragraphs, result.paragraphs_zh, strict=True)
    ]
    return dataclasses.replace(
        article,
        title_zh=result.title_zh,
        summary_zh=result.summary_zh,
        paragraphs=paragraphs,
        vocabulary=result.vocabulary,
        collocations=result.collocations,
        sentence_notes=result.sentence_notes,
        translated_by=translated_by,
    )
