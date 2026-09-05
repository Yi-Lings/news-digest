"""模型输出 schema、prompt 构造与严格校验。

版本约定：PROMPT_VERSION 变更即视为不同缓存命名空间；
非法或不完整响应一律拒绝（InvalidTranslation），绝不入缓存。
"""

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field

from news_digest.models import Article, Collocation, Paragraph, SentenceNote, VocabularyItem

PROMPT_VERSION = "p7"
SENTENCE_REPAIR_PROTOCOL_VERSION = "sr1"
# 分句器独立版本:分句规则变更只升 SPLITTER_VERSION,不牵连 PROMPT_VERSION;
# 缓存读取时会校验该版本(见 service),防止正则热修静默按新规则重验旧缓存。
SPLITTER_VERSION = "s1"

SYSTEM_PROMPT = """你是一名服务于中文英语学习者的新闻翻译与教学助理。
文风要求：规范的新闻书面语（参照通讯社译文风格），正式、克制、准确；
避免口语化表达与网络用语；专有名词用通行译名，生僻者可在首次出现时括注英文。
核心任务是完整翻译新闻正文，不是摘要、改写或评论。必须逐句理解并逐段翻译，
不得合并段落、跳过句子、删减事实、弱化限定条件，或用概括性的一句话替代原文内容。
必须保留原文中的人物、机构、地点、时间、数字、比例、金额、因果关系、否定、条件、
不确定性、引语和归因；原文重复或表达谨慎时也不得擅自删改。summary_zh 只是补充摘要，
绝不能代替 sentences_zh 中的完整正文译文；词汇、搭配和长难句解析也不能代替正文翻译。
对给定英文新闻，严格按以下模板输出一个 JSON 对象：
{
  "title_zh": "非空字符串",
  "summary_zh": "非空字符串",
  "sentences_zh": [["逐句中文译文"]],
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
正文、标题和摘要必须为非空内容。教学字段无法完成时可返回空数组，不得影响正文完整性。
- "title_zh": 简洁准确的中文标题，信达雅
- "summary_zh": 中文摘要，一到两句
- "sentences_zh": 二维字符串数组，外层逐段、内层逐句对应；每个原文句子必须恰好对应
  一个同序中文句子，不得合并、拆分、跳过或新增句子
- 用户消息中的 [P#S#] 是不可变的原文句子编号；必须按编号逐句翻译，不能自行重新分句
- "vocabulary": 3 到 6 个值得学习的词，word 必须出自原文
- "collocations": 1 到 3 个固定搭配
- "sentence_notes": 1 到 2 个长难句解析，sentence_en 摘自原文，analysis_zh 讲清语法结构
输出 JSON 前逐段对照输入正文自检：确认段落数量、句子信息、事实细节和语气均未缺失；
如果某段较长，必须完整输出，不能为了控制长度而压缩、删句或只保留主旨。
不要在译文之外自创内容；不要输出 markdown、代码围栏、解释或 JSON 以外的文字。"""

SENTENCE_REPAIR_SYSTEM_PROMPT = """你是新闻翻译校对助理，只修复一个已定位的中文句子。
必须保留原句事实、时间、数字、单位、否定、因果、条件、引语和归因；不得摘要、扩写、评论，
不得修改上下文句子。只输出一个 JSON 对象，且只能包含以下字段：
{
  "paragraph_index": 1,
  "sentence_index": 1,
  "translation_zh": "完整的单句中文译文"
}
paragraph_index 和 sentence_index 必须原样回显输入编号；translation_zh 必须是非空字符串。
不得输出 Markdown、代码围栏、解释或任何额外字段。"""


class InvalidTranslation(ValueError):
    """模型响应非法或不完整。

    ``code`` 为可选的封闭错误码(如 CONTENT_NUMBER_MISSING),由质量门标注,
    供任务失败分类使用;缺省时按 SCHEMA_VALIDATION_FAILED 处理。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        candidate: dict | None = None,
        sentence_failures: tuple[tuple[int, int], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidate = candidate
        self.sentence_failures = sentence_failures


@dataclass(frozen=True)
class TranslationResult:
    title_zh: str
    summary_zh: str
    paragraphs_zh: list[str]
    sentences_zh: list[list[str]]
    vocabulary: list[VocabularyItem] = field(default_factory=list)
    collocations: list[Collocation] = field(default_factory=list)
    sentence_notes: list[SentenceNote] = field(default_factory=list)


@dataclass(frozen=True)
class SentenceRepair:
    paragraph_index: int
    sentence_index: int
    translation_zh: str


def build_user_prompt(article: Article, frozen_sentences: list[list[str]] | None = None) -> str:
    lines = [f"标题：{article.title_en}", f"摘要：{article.summary_en}", "正文按句编号："]
    sentence_counts: list[int] = []
    for index, paragraph in enumerate(article.paragraphs, start=1):
        sentences = (
            frozen_sentences[index - 1]
            if frozen_sentences is not None
            else split_sentences(paragraph.en)
        )
        sentence_counts.append(len(sentences))
        lines.append(f"段落 P{index}：")
        for sentence_index, sentence in enumerate(sentences, start=1):
            lines.append(f"[P{index}S{sentence_index}] {sentence}")
    count = len(article.paragraphs)
    counts = "、".join(f"P{index}={value}" for index, value in enumerate(sentence_counts, 1))
    lines.append(
        f"（共 {count} 段；sentences_zh 外层必须恰好 {count} 项；"
        f"内层句数必须严格匹配：{counts}）"
    )
    return "\n".join(lines)


def build_sentence_repair_prompt(
    *,
    title_en: str,
    paragraph_index: int,
    sentence_index: int,
    source_sentence: str,
    previous_translation: str,
    context_before: str,
    context_after: str,
    evidence: list[dict[str, object]],
) -> str:
    """Build a bounded prompt for replacing exactly one sentence."""
    evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return (
        f"文章标题：{title_en}\n"
        f"目标编号：P{paragraph_index}S{sentence_index}\n"
        f"原文句子：{source_sentence}\n"
        f"当前译文：{previous_translation}\n"
        f"前文只读上下文：{context_before or '无'}\n"
        f"后文只读上下文：{context_after or '无'}\n"
        f"结构化诊断 evidence：{evidence_json}\n"
        "请只修复目标句，并严格返回指定 JSON。"
    )


_FENCED_JSON = re.compile(
    r"```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n[ \t]*```",
    re.IGNORECASE | re.DOTALL,
)


def _load_response_json(raw_text: str):
    if len(raw_text) > 2 * 1024 * 1024:
        raise InvalidTranslation("Translation response is too large")
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


def parse_sentence_repair(
    raw_text: str, *, paragraph_index: int, sentence_index: int
) -> SentenceRepair:
    """Parse the closed three-field protocol for a single sentence repair."""
    if len(raw_text) > 65536:
        raise InvalidTranslation("Sentence repair response is too large")

    def unique_fields(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise InvalidTranslation("Duplicate sentence repair field")
            data[key] = value
        return data

    try:
        data = json.loads(raw_text, object_pairs_hook=unique_fields)
    except (ValueError, TypeError) as error:
        raise InvalidTranslation("Invalid sentence repair JSON") from error
    if not isinstance(data, dict):
        raise InvalidTranslation("句子修复响应顶层必须是 JSON 对象")
    if set(data) != {"paragraph_index", "sentence_index", "translation_zh"}:
        raise InvalidTranslation("句子修复响应包含缺失或额外字段")
    if (
        type(data["paragraph_index"]) is not int
        or paragraph_index < 1
        or data["paragraph_index"] != paragraph_index
    ):
        raise InvalidTranslation("句子修复段落编号不匹配")
    if (
        type(data["sentence_index"]) is not int
        or sentence_index < 1
        or data["sentence_index"] != sentence_index
    ):
        raise InvalidTranslation("句子修复句子编号不匹配")
    translation = data["translation_zh"]
    if not isinstance(translation, str) or not translation.strip():
        raise InvalidTranslation("句子修复译文缺失或为空")
    return SentenceRepair(paragraph_index, sentence_index, translation.strip())


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
) -> list:
    raw = data.get(key)
    if not isinstance(raw, list):
        return []
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kwargs = {}
        for name in fields:
            value = entry.get(name)
            if not isinstance(value, str) or not value.strip():
                break
            kwargs[name] = value.strip()
        else:
            items.append(factory(**kwargs))
    return items


_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[.!?])(?:[\"'”’\])}]*)(?=\s+(?:[A-Z0-9“‘\"(]))"
)
_ABBREVIATIONS = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e)\.$",
    re.IGNORECASE,
)
_INITIALISM = re.compile(r"(?:\b[A-Za-z]\.){2,}$")
_TIME_ABBREVIATION = re.compile(r"\b[ap]\.m\.$", re.IGNORECASE)


def split_sentences(text: str) -> list[str]:
    """Conservative deterministic English sentence segmentation for schema validation."""
    raw_parts = _SENTENCE_BOUNDARY.split(text.strip())
    parts: list[str] = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        if parts and (
            _ABBREVIATIONS.search(parts[-1])
            or (
                _INITIALISM.search(parts[-1])
                and not _TIME_ABBREVIATION.search(parts[-1])
            )
            or re.search(r"\b\d\.$", parts[-1])
        ):
            parts[-1] = f"{parts[-1]} {part}"
        else:
            parts.append(part)
    return parts or [text.strip()]


def expected_sentence_counts(paragraphs: list[str]) -> list[int]:
    """对每个段落分句并返回句数快照;seed 时冻结进任务,校验时复用。"""
    return [len(split_sentences(paragraph)) for paragraph in paragraphs]


def build_sentence_snapshot(paragraphs: list[str]) -> dict[str, object]:
    """Freeze source sentence text and its paragraph hash for an automation task."""
    return {
        "version": SPLITTER_VERSION,
        "paragraphs": [
            {
                "source_hash": hashlib.sha256(paragraph.encode("utf-8")).hexdigest(),
                "sentences": split_sentences(paragraph),
            }
            for paragraph in paragraphs
        ],
    }


def read_sentence_snapshot(snapshot: dict, paragraphs: list[str]) -> list[list[str]]:
    """Validate stored text without running a possibly changed splitter."""
    entries = snapshot.get("paragraphs")
    if not isinstance(snapshot.get("version"), str) or not isinstance(entries, list):
        raise ValueError("Invalid sentence snapshot")
    if len(entries) != len(paragraphs):
        raise ValueError("Sentence snapshot paragraph count mismatch")
    result = []
    for entry, paragraph in zip(entries, paragraphs, strict=True):
        if not isinstance(entry, dict):
            raise ValueError("Invalid sentence snapshot paragraph")
        sentences = entry.get("sentences")
        if entry.get("source_hash") != hashlib.sha256(paragraph.encode("utf-8")).hexdigest():
            raise ValueError("Sentence snapshot source hash mismatch")
        if (
            not isinstance(sentences, list)
            or not sentences
            or any(not isinstance(s, str) or not s.strip() for s in sentences)
        ):
            raise ValueError("Invalid sentence snapshot text")
        # The existing splitter may consume closing quotes at boundaries.
        cursor = 0
        for sentence in sentences:
            offset = paragraph.find(sentence, cursor)
            if offset < 0 or paragraph[cursor:offset].strip(" \t\r\n\"'”’])}"):
                raise ValueError("Sentence snapshot text mismatch")
            cursor = offset + len(sentence)
        if paragraph[cursor:].strip(" \t\r\n\"'”’])}"):
            raise ValueError("Sentence snapshot text mismatch")
        result.append(list(sentences))
    return result


def parse_translation(
    raw_text: str,
    paragraph_count: int,
    source_paragraphs: list[str] | None = None,
    frozen_counts: list[int] | None = None,
) -> TranslationResult:
    """解析并严格校验模型输出;任何缺失、错位、空值都视为非法。

    frozen_counts 是任务 seed 时冻结的逐段句数快照;提供时校验只与快照比对,
    不再对原文重新分句——分句规则变更不会让同一任务的验收标准漂移。
    """
    data = _load_response_json(raw_text)
    if not isinstance(data, dict):
        raise InvalidTranslation("响应顶层必须是 JSON 对象")

    sentences_zh = data.get("sentences_zh")
    if not isinstance(sentences_zh, list):
        raise InvalidTranslation("sentences_zh 缺失或不是数组")
    if len(sentences_zh) != paragraph_count:
        raise InvalidTranslation(
            f"sentences_zh 段落数量 {len(sentences_zh)} 与原文段落数 {paragraph_count} 不一致"
        )
    if source_paragraphs is not None and len(source_paragraphs) != paragraph_count:
        raise ValueError("source_paragraphs 数量必须与 paragraph_count 一致")
    if frozen_counts is not None and len(frozen_counts) != paragraph_count:
        raise ValueError("frozen_counts 数量必须与 paragraph_count 一致")
    cleaned_paragraphs = []
    failures = []
    cleaned_sentences: list[list[str]] = []
    for index, sentences in enumerate(sentences_zh):
        if not isinstance(sentences, list) or not sentences:
            raise InvalidTranslation(f"sentences_zh 第 {index + 1} 段为空或不是数组")
        if frozen_counts is not None:
            expected = frozen_counts[index]
        elif source_paragraphs is not None:
            expected = len(split_sentences(source_paragraphs[index]))
        else:
            expected = None
        if expected is not None and len(sentences) != expected:
            raise InvalidTranslation(f"sentences_zh 第 {index + 1} 段句子数量与原文不一致")
        invalid = [
            (index + 1, sentence_index + 1)
            for sentence_index, item in enumerate(sentences)
            if not isinstance(item, str) or not item.strip()
        ]
        if invalid:
            failures.extend(invalid)
            continue
        cleaned = [item.strip() for item in sentences]
        cleaned_sentences.append(cleaned)
        cleaned_paragraphs.append("".join(cleaned))

    title_zh = _require_str(data, "title_zh")
    summary_zh = _require_str(data, "summary_zh")
    if failures:
        raise InvalidTranslation(
            "Empty or non-string sentence: " + ", ".join(f"P{p}S{s}" for p, s in failures),
            code="CONTENT_FIELD_MISSING",
            candidate=data,
            sentence_failures=tuple(failures),
        )

    return TranslationResult(
        title_zh=title_zh,
        summary_zh=summary_zh,
        paragraphs_zh=cleaned_paragraphs,
        sentences_zh=cleaned_sentences,
        vocabulary=_require_items(
            data,
            "vocabulary",
            ("word", "phonetic", "meaning_zh", "example_en"),
            VocabularyItem,
        ),
        collocations=_require_items(
            data,
            "collocations",
            ("phrase", "meaning_zh", "example_en"),
            Collocation,
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
        "sentences_zh": [list(sentences) for sentences in result.sentences_zh],
        "vocabulary": [vars(v) for v in result.vocabulary],
        "collocations": [vars(c) for c in result.collocations],
        "sentence_notes": [vars(s) for s in result.sentence_notes],
        "splitter_version": SPLITTER_VERSION,
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
