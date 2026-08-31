"""内容级翻译质量门:纯函数,在 schema 结构校验之后运行。

设计原则(与 PLAN §12B 一致):
- 硬门只保留可确定性判定的"数字缺失"——原文数字在译文中值级缺失;
  专名/否定/长度只做软信号,永不单独阻断。
- 数值比较是值级的:"1.5 million"与"150万"相等,"45%"与"百分之45"同单位。
- observe 模式把硬违规降级为软信号,只记录不阻断,用于上线初期实测误报率。

所有函数只做检测,不发请求、不落盘;修复由 service 的既有 feedback 预算执行。
"""

import os
import re
from dataclasses import dataclass, field

from news_digest.translation.schema import TranslationResult

QUALITY_MODES = ("observe", "enforce")


def mode() -> str:
    value = os.environ.get("TRANSLATION_QUALITY_MODE", "enforce").strip().lower()
    return value if value in QUALITY_MODES else "enforce"


_EN_NUMBER = re.compile(
    r"(?:(?P<cur>[$€£])\s?)?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?P<pct>\s?%)?"
    r"(?:\s?(?P<scale>thousand|million|billion|trillion|[mbn](?=[\s.,;:!?)]|$)|k(?=[\s.,;:!?)]|$)))?",
    re.IGNORECASE,
)
_EN_WORD_PERCENT = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s+percent\b", re.IGNORECASE)
_EN_PHYSICAL_M_SUFFIX = re.compile(
    r"\s*(?:"
    r"(?:long|wide|high|deep|tall)"
    r"(?=\s*(?:$|[.,;:!?)]|(?:and|or|but|while|by)\b))"
    r"|in\s+(?:length|width|height|depth)\b"
    r"|by\s+\d[\d,]*(?:\.\d+)?\s?m(?=$|[\s.,;:!?)]))",
    re.IGNORECASE,
)
_EN_PHYSICAL_M_PREFIX = re.compile(
    r"(?:\b(?:measure|measures|measured|measuring)\s+|\d[\d,.]*\s?m\s+by\s+)$",
    re.IGNORECASE,
)
_ZH_PERCENT_PREFIX = re.compile(r"百分之\s*(?P<num>[\d一二三四五六七八九两零十点\.]+)")
_ZH_ARABIC = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)(?P<unit>万|亿|千)?\s*(?P<pct>%|％)?")
_ZH_DIGIT_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_ZH_SCALES = {"万": 10_000, "亿": 100_000_000}
_ZH_NUMERAL = re.compile(r"[零一二三四五六七八九两十百千万亿]+")

# 时刻(22:00、01:30、10.30am)不是可对账的数值:ZH 侧常写作"22时""晚上10时",
# 拆出的 0/30 只会制造幻影数值,两端都先行排除。
_EN_TIME = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?!\d)|(?<!\d)\d{1,2}\.\d{2}(?=\s?(?:am|pm)\b)",
    re.IGNORECASE,
)
_ZH_TIME = re.compile(
    r"(?<!\d)\d{1,2}:\d{2}(?!\d)|(?<!\d)\d{1,2}[时点]\d{1,2}分?|(?<!\d)\d{1,2}[时点](?=\s|[,，。;；!？?]|$)"
)

_SCALE_MULT = {
    "thousand": 1_000.0,
    "k": 1_000.0,
    "million": 1_000_000.0,
    "m": 1_000_000.0,
    "billion": 1_000_000_000.0,
    "bn": 1_000_000_000.0,
    "b": 1_000_000_000.0,
    "trillion": 1_000_000_000_000.0,
}


def _parse_zh_int(text: str) -> float | None:
    """解析不含小数的中文整数,如 一百五十/两千/三百二十万 的整数部分。"""
    if not text:
        return None
    total = 0.0
    section = 0.0
    number: float | None = None
    matched = False
    for ch in text:
        if ch in _ZH_DIGIT_MAP:
            if ch == "零" and number in (None, 0):
                # "一年零十个月"里的"零"是组间连接符,不是数值 0。
                matched = True
                continue
            number = float(_ZH_DIGIT_MAP[ch])
            matched = True
        elif ch in _ZH_SMALL_UNITS:
            unit = _ZH_SMALL_UNITS[ch]
            section += (number if number is not None else 1.0) * unit
            number = None
            matched = True
        elif ch in _ZH_SCALES:
            scale = _ZH_SCALES[ch]
            section = (section + (number or 0.0)) * scale
            total += section
            section = 0.0
            number = None
            matched = True
        else:
            return None
    if not matched:
        return None
    return total + section + (number or 0.0)


def extract_en_values(text: str) -> list[tuple[str, float]]:
    """抽取 (单位, 数值);单位 'pct' 表示百分比,plain 为普通数值。"""
    values: list[tuple[str, float]] = []
    consumed: list[tuple[int, int]] = []
    # 时刻整体排除;"N percent" 抽取为百分比并占位,防止主正则把 N 当普通数字。
    for match in _EN_TIME.finditer(text):
        consumed.append((match.start(), match.end()))
    for match in _EN_WORD_PERCENT.finditer(text):
        consumed.append((match.start(), match.end()))
        try:
            values.append(("pct", float(match.group("num").replace(",", ""))))
        except ValueError:
            pass

    def _consumed(start: int, end: int) -> bool:
        return any(start < end_ and start_ < end for start_, end_ in consumed)

    for match in _EN_NUMBER.finditer(text):
        if _consumed(match.start(), match.end()):
            continue
        raw = match.group("num")
        if not raw:
            continue
        pct = bool(match.group("pct"))
        scale = match.group("scale")
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if scale:
            scale_key = scale.lower().strip()
            physical_m = (
                scale == "m"
                and match.group("cur") is None
                and (
                    _EN_PHYSICAL_M_SUFFIX.match(text, match.end()) is not None
                    or _EN_PHYSICAL_M_PREFIX.search(text, 0, match.start()) is not None
                )
            )
            if not physical_m:
                value *= _SCALE_MULT.get(scale_key, 1.0)
        values.append(("pct" if pct else "plain", value))
    return values


def extract_zh_values(text: str) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    consumed: list[tuple[int, int]] = []

    def _overlap(start: int, end: int) -> bool:
        return any(start < end_ and start_ < end for start_, end_ in consumed)

    # 时刻表达(22:00、22时00分、晚上10时)整体排除,不产出数值证据。
    for match in _ZH_TIME.finditer(text):
        consumed.append((match.start(), match.end()))

    for match in _ZH_PERCENT_PREFIX.finditer(text):
        raw = match.group("num").replace("点", "").replace("。", "")
        value = _parse_float_mixed(raw)
        if value is not None:
            values.append(("pct", value))
            consumed.append((match.start(), match.end()))

    for match in _ZH_ARABIC.finditer(text):
        if _overlap(match.start(), match.end()):
            continue
        value = float(match.group("num").replace(",", ""))
        unit = match.group("unit")
        if unit in _ZH_SCALES:
            value *= _ZH_SCALES[unit]
        elif unit == "千":
            value *= 1_000.0
        values.append(("pct" if match.group("pct") else "plain", value))
        consumed.append((match.start(), match.end()))

    for match in _ZH_NUMERAL.finditer(text):
        if _overlap(match.start(), match.end()):
            continue
        raw = match.group(0)
        # 裸"万/千/亿"(如"数千""几十万")是约数而非精确值,绝不能作为
        # 数值证据:既不参与匹配,也不能被幻影满足。
        if len(raw) == 1 and raw in {"万", "千", "亿"}:
            continue
        value = _parse_zh_int(raw)
        if value is None:
            continue
        end = match.end()
        scale_mult = 1.0
        for token, mult in _ZH_SCALES.items():
            if text[end:].startswith(token):
                scale_mult = mult
                break
        values.append(("plain", value * scale_mult))
        consumed.append((match.start(), match.end()))
    return values


def _parse_float_mixed(raw: str) -> float | None:
    """解析 百分之X 的 X:可为阿拉伯数字、中文整数或混合(如 4十五 不合法则放弃)。"""
    if not raw:
        return None
    if re.fullmatch(r"[\d,]+(?:\.\d+)?", raw):
        return float(raw.replace(",", ""))
    value = _parse_zh_int(raw)
    return value


def _values_satisfied(
    expected: list[tuple[str, float]],
    present: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """返回 expected 中在 present 里找不到等值项的数值。"""

    def equal(unit: str, value: float, candidate: tuple[str, float]) -> bool:
        other_unit, other_value = candidate
        if unit == "pct" and other_unit == "plain" and other_value == value / 100.0:
            return True
        return unit == other_unit and abs(value - other_value) <= max(
            1e-9, abs(value) * 1e-9
        )

    remaining = list(present)
    missing: list[tuple[str, float]] = []
    for unit, value in expected:
        for index, candidate in enumerate(remaining):
            if equal(unit, value, candidate):
                remaining.pop(index)
                break
        else:
            missing.append((unit, value))
    return missing


def _format_value(unit: str, value: float) -> str:
    text = f"{value:g}"
    return f"{text}%" if unit == "pct" else text


_EN_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nor|without|cannot|hardly|barely|"
    r"do(?:es)?\s?not|did\s?not|isn'?t|aren'?t|wasn'?t|weren'?t|"
    r"don'?t|doesn'?t|didn'?t|won'?t|wouldn'?t|can'?t|couldn'?t|"
    r"shouldn'?t|mustn'?t|hasn'?t|haven'?t|hadn'?t)\b",
    re.IGNORECASE,
)
_ZH_NEGATION = re.compile(r"[不未无非没别勿]|难以|否认|拒绝|禁止|没有")


def check_numbers(source_paragraphs: list[str], result: TranslationResult) -> list[str]:
    """硬门:原文数字必须在译文中保值保留;返回违规描述列表。"""
    en_text = "\n".join(source_paragraphs)
    zh_text = "\n".join("".join(sentences) for sentences in result.sentences_zh)
    expected = extract_en_values(en_text)
    if not expected:
        return []
    present = extract_zh_values(zh_text)
    missing = _values_satisfied(expected, present)
    return [
        f"原文数值 {_format_value(unit, value)} 在译文中缺失或数值不一致"
        for unit, value in missing
    ]


def soft_signals(source_paragraphs: list[str], result: TranslationResult) -> list[str]:
    """软门:长度异常与否定词数量骤变;只提示,不阻断。

    长度基准:中文以字计、英文以词计,正常新闻译文约为每英文词 1.2-2.2 个汉字。
    """
    en_text = "\n".join(source_paragraphs)
    zh_text = "\n".join("".join(sentences) for sentences in result.sentences_zh)
    notes: list[str] = []
    en_words = max(len(re.findall(r"[A-Za-z0-9]+", en_text)), 1)
    zh_len = len(zh_text)
    ratio = zh_len / en_words
    if ratio < 0.8:
        notes.append(f"译文长度仅为常规水平的 {ratio:.0%},疑似漏译或压缩")
    elif ratio > 4.0:
        notes.append(f"译文长度达到常规水平的 {ratio:.0%},疑似增加了解释或重复")
    en_negations = len(_EN_NEGATION.findall(en_text))
    zh_negations = len(_ZH_NEGATION.findall(zh_text))
    if en_negations >= 3 and zh_negations == 0:
        notes.append(f"原文含 {en_negations} 处否定表达,译文未检出否定词")
    return notes


@dataclass(frozen=True)
class QualityReport:
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)


def check_translation(
    source_paragraphs: list[str], result: TranslationResult
) -> tuple[list[str], list[str]]:
    """对已通过 schema 的译文运行内容质量门,返回 (硬违规, 软违规)。"""
    return check_numbers(source_paragraphs, result), soft_signals(
        source_paragraphs, result
    )
