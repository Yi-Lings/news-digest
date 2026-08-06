"""模型输出 schema、prompt 构造与严格校验。

版本约定：PROMPT_VERSION 变更即视为不同缓存命名空间；
非法或不完整响应一律拒绝（InvalidTranslation），绝不入缓存。
"""

import dataclasses
import json
import re
from dataclasses import dataclass, field

from news_digest.models import Article, Collocation, Paragraph, SentenceNote, VocabularyItem

PROMPT_VERSION = "p4"

SYSTEM_PROMPT = """你是一名服务于中文英语学习者的新闻翻译与教学助理。
文风要求：规范的新闻书面语（参照通讯社译文风格），正式、克制、准确；
避免口语化表达与网络用语；专有名词用通行译名，生僻者可在首次出现时括注英文。
核心任务是完整翻译新闻正文，不是摘要、改写或评论。必须逐句理解并逐段翻译，
不得合并段落、跳过句子、删减事实、弱化限定条件，或用概括性的一句话替代原文内容。
必须保留原文中的人物、机构、地点、时间、数字、比例、金额、因果关系、否定、条件、
不确定性、引语和归因；原文重复或表达谨慎时也不得擅自删改。summary_zh 只是补充摘要，
绝不能代替 paragraphs_zh 中的完整正文译文；词汇、搭配和长难句解析也不能代替正文翻译。
对给定英文新闻，严格按以下模板输出一个 JSON 对象：
{
  "title_zh": "非空字符串",
  "summary_zh": "非空字符串",
  "paragraphs_zh": ["逐段中文译文"],
  "vocabulary": [
    {
      "word": "原文词汇",
      "phonetic": "非空字符串",
      "meaning_zh": "非空字符串",
      "example_en": "非空字符串"
    }
  ],
  "collocations": [
    {"phrase": "原文搭配", "meaning_zh": "非空字符串", "example_en": "非空字符串"}
  ],
  "sentence_notes": [
    {
      "sentence_en": "原文句子",
      "translation_zh": "非空字符串",
      "analysis_zh": "非空字符串"
    }
  ]
}
以上六个顶层字段及数组元素内展示的字段全部必填；不得使用 null 或空字符串。
- "title_zh": 中文标题，信达雅，不超过 40 字
- "summary_zh": 中文摘要，一到两句
- "paragraphs_zh": 字符串数组，逐段对应完整译文，数量必须与输入段落数完全一致；
  每一项都必须覆盖对应英文段落的全部句子和信息，忠实、准确、通顺，不得摘要化或漏译
- "vocabulary": 3 到 6 个值得学习的词，word 必须出自原文
- "collocations": 1 到 3 个固定搭配
- "sentence_notes": 1 到 2 个长难句解析，sentence_en 摘自原文，analysis_zh 讲清语法结构
输出 JSON 前逐段对照输入正文自检：确认段落数量、句子信息、事实细节和语气均未缺失；
如果某段较长，必须完整输出，不能为了控制长度而压缩、删句或只保留主旨。
不要在译文之外自创内容；不要输出 markdown、代码围栏、解释或 JSON 以外的文字。"""


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


_FENCED_JSON = re.compile(
    r"```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n[ \t]*```",
    re.IGNORECASE | re.DOTALL,
)


def _load_response_json(raw_text: str):
    cleaned = raw_text.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as direct_error:
        matches = list(_FENCED_JSON.finditer(cleaned))
        if len(matches) != 1:
            raise InvalidTranslation(f"响应不是合法 JSON：{direct_error}") from direct_error
        match = matches[0]
        outside = cleaned[: match.start()] + cleaned[match.end() :]
        if "```" in outside:
            raise InvalidTranslation("响应包含多个或不完整的代码围栏") from direct_error
        try:
            return json.loads(match.group("body").strip())
        except json.JSONDecodeError as fenced_error:
            raise InvalidTranslation(f"响应不是合法 JSON：{fenced_error}") from fenced_error


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidTranslation(f"字段 {key} 缺失或为空")
    return value.strip()


def _require_items(
    data: dict,
    key: str,
    fields: tuple[str, ...],
    factory,
    *,
    min_count: int,
    max_count: int,
) -> list:
    raw = data.get(key)
    if not isinstance(raw, list):
        raise InvalidTranslation(f"字段 {key} 缺失或不是数组")
    if not min_count <= len(raw) <= max_count:
        raise InvalidTranslation(f"{key} 数量必须在 {min_count} 到 {max_count} 之间")
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
    data = _load_response_json(raw_text)
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

    title_zh = _require_str(data, "title_zh")
    if len(title_zh) > 40:
        raise InvalidTranslation("title_zh 不得超过 40 字")

    return TranslationResult(
        title_zh=title_zh,
        summary_zh=_require_str(data, "summary_zh"),
        paragraphs_zh=cleaned_paragraphs,
        vocabulary=_require_items(
            data,
            "vocabulary",
            ("word", "phonetic", "meaning_zh", "example_en"),
            VocabularyItem,
            min_count=3,
            max_count=6,
        ),
        collocations=_require_items(
            data,
            "collocations",
            ("phrase", "meaning_zh", "example_en"),
            Collocation,
            min_count=1,
            max_count=3,
        ),
        sentence_notes=_require_items(
            data,
            "sentence_notes",
            ("sentence_en", "translation_zh", "analysis_zh"),
            SentenceNote,
            min_count=1,
            max_count=2,
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
