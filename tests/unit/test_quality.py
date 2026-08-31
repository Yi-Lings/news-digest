"""质量门单元测试:数字抽取、值级匹配、硬/软门行为与 observe 模式。"""

from news_digest.translation import quality
from news_digest.translation.schema import TranslationResult


def _result(zh_sentences: list[list[str]]) -> TranslationResult:
    paragraphs = ["".join(sentences) for sentences in zh_sentences]
    return TranslationResult(
        title_zh="标题",
        summary_zh="摘要",
        paragraphs_zh=paragraphs,
        sentences_zh=zh_sentences,
    )


class TestExtraction:
    def test_en_values_with_scales_and_percent(self):
        got = quality.extract_en_values(
            "About 1.5 million people attended, up 45% from 2023, costing $2.3 billion."
        )
        assert sorted(got) == sorted([
            ("plain", 1_500_000.0),
            ("pct", 45.0),
            ("plain", 2023.0),
            ("plain", 2_300_000_000.0),
        ])

    def test_en_word_percent(self):
        got = quality.extract_en_values(
            "The 198-page report cites 3,000 cases and 12 percent cuts."
        )
        assert sorted(got) == sorted([("plain", 198.0), ("plain", 3000.0), ("pct", 12.0)])

    def test_zh_arabic_with_scales(self):
        got = quality.extract_zh_values("约150万人参加,比2023年增长45%,耗资23亿。")
        assert sorted(got) == sorted(
            [("plain", 1_500_000.0), ("plain", 2023.0), ("pct", 45.0), ("plain", 2_300_000_000.0)]
        )

    def test_zh_percent_prefix_and_numerals(self):
        got = quality.extract_zh_values("这份198页的报告引用了3000个案例和百分之十二的削减。")
        assert sorted(got) == sorted([("plain", 198.0), ("plain", 3000.0), ("pct", 12.0)])
        assert quality._parse_zh_int("三百") == 300.0
        assert quality._parse_zh_int("十二") == 12.0
        assert quality._parse_zh_int("一百五十") == 150.0
        assert quality._parse_zh_int("两千") == 2000.0


class TestHardGate:
    EN = "About 1.5 million people attended, up 45% from 2023."

    def test_matching_translation_passes(self):
        result = _result([["约150万人参加了活动,比2023年增长45%。"]])
        assert quality.check_numbers([self.EN], result) == []

    def test_missing_numbers_fail(self):
        result = _result([["许多人参加了活动,增长明显。"]])
        violations = quality.check_numbers([self.EN], result)
        assert len(violations) == 3

    def test_fraction_of_percent_expression(self):
        result = _result([["增长率为0.45。"]])
        assert quality.check_numbers(["The growth rate was 45%."], result) == []

    def test_scale_equivalence(self):
        result = _result([["该计划投资50亿元。"]])
        assert quality.check_numbers(["The program will invest 5 billion yuan."], result) == []

    def test_compact_m_physical_measurement_is_not_million(self):
        result = _result([["该物体长10米。"]])
        assert quality.extract_en_values("The object is 10m long.") == [("plain", 10.0)]
        assert quality.check_numbers(["The object is 10m long."], result) == []

    def test_compact_m_wide_measurement_is_not_million(self):
        result = _result([["该物体宽5米。"]])
        assert quality.check_numbers(["The object is 5m wide."], result) == []

    def test_year_in_positional_chinese_digits_passes(self):
        source = "The event happened in 2026."
        for translation in ("事件发生在二零二六年。", "事件发生在二〇二六年。"):
            assert quality.extract_zh_values(translation) == [("plain", 2026.0)]
            assert quality.check_numbers([source], _result([[translation]])) == []

    def test_completely_missing_year_still_fails(self):
        violations = quality.check_numbers(
            ["The event happened in 2026."], _result([["事件已经发生。"]])
        )
        assert len(violations) == 1

    def test_non_year_digit_sequences_are_not_positional_numbers(self):
        assert ("plain", 23.0) not in quality.extract_zh_values("大约两三次。")
        assert ("plain", 12.0) not in quality.extract_zh_values("可选一二个方案。")
        assert ("plain", 2026.0) not in quality.extract_zh_values("编号一二零二六年。")

    def test_compact_m_dimension_grammar(self):
        assert quality.extract_en_values("It is 10 m wide.") == [("plain", 10.0)]
        assert quality.extract_en_values("It measures 10m.") == [("plain", 10.0)]
        assert quality.extract_en_values("It is 10m by 5m.") == [
            ("plain", 10.0),
            ("plain", 5.0),
        ]

    def test_compact_m_business_compounds_remain_million(self):
        assert quality.extract_en_values("It signed 10m long-term contracts.") == [
            ("plain", 10_000_000.0)
        ]
        assert quality.extract_en_values("It serves 10m high-value customers.") == [
            ("plain", 10_000_000.0)
        ]
        assert quality.extract_en_values("Revenue will increase 10m by 2027.") == [
            ("plain", 10_000_000.0),
            ("plain", 2027.0),
        ]

    def test_compact_m_currency_remains_million(self):
        result = _result([["该基金筹集了1000万英镑。"]])
        assert quality.extract_en_values("The fund raised £10m.") == [
            ("plain", 10_000_000.0)
        ]
        assert quality.check_numbers(["The fund raised £10m."], result) == []

    def test_no_numbers_is_noop(self):
        result = _result([["这是在正常推进。"]])
        assert quality.check_numbers(["Things moved forward as planned."], result) == []

    def test_times_of_day_are_not_numbers(self):
        # 生产事故回放(2026-08 柏林报道):"22:00 local time (20:00 GMT)"
        # 曾被拆出幻影数值 0,译文漏掉时被硬门误杀。
        en = "The attack happened at about 22:00 local time (20:00 GMT) on Saturday."
        zh = "袭击发生在周六大约当地时间22点(格林尼治标准时间20点)。"
        result = _result([[zh]])
        assert quality.check_numbers([en], result) == []

    def test_lingering_zero_connector(self):
        # "一年零十个月"的"零"是连接符;EN 的 10 个月必须由 ZH 的"十"满足。
        en = "He had previously been sentenced to one year and 10 months in prison."
        zh = "他此前被判处一年零十个月监禁。"
        assert quality.check_numbers([en], _result([[zh]])) == []


class TestSoftGate:
    def test_short_translation_flagged(self):
        en = "One two three four five six seven eight nine ten eleven twelve thirteen."
        notes = quality.soft_signals([en], _result([["短。"]]))
        assert any("漏译" in note for note in notes)

    def test_normal_translation_clean(self):
        en = (
            "The government announced a new policy on Tuesday that will affect "
            "thousands of workers across the country and beyond."
        )
        zh = "政府周二宣布了一项将影响全国乃至境外数千名工人的新政策。"
        assert quality.soft_signals([en], _result([[zh]])) == []

    def test_negation_loss_flagged(self):
        en = (
            "He said he would not attend and did not explain, and the office "
            "refused to comment on why the plan was not approved."
        )
        zh = "他表示他将出席,并作出了解释,办公室也就计划获批的原因发表了评论。"
        notes = quality.soft_signals([en], _result([[zh]]))
        assert any("否定" in note for note in notes)


class TestMode:
    def test_default_is_enforce(self, monkeypatch):
        monkeypatch.delenv("TRANSLATION_QUALITY_MODE", raising=False)
        assert quality.mode() == "enforce"

    def _article(self, en: str):
        from news_digest.models import Article, Paragraph

        return Article(
            slug="test",
            source="BBC",
            title_en="Title",
            summary_en="Summary",
            author="Author",
            published_at="2026-08-30T08:00:00Z",
            reading_minutes=3,
            paragraphs=[Paragraph(en=en)],
            url="https://example.com/test",
        )

    def test_observe_mode_downgrades_hard(self, monkeypatch):
        from news_digest.translation.service import _content_gates

        monkeypatch.setenv("TRANSLATION_QUALITY_MODE", "observe")
        article = self._article("About 1.5 million people attended.")
        result = _result([["许多人参加了活动。"]])
        hard, soft = _content_gates(article, result)
        assert hard == []
        assert soft

    def test_enforce_mode_keeps_hard(self, monkeypatch):
        from news_digest.translation.service import _content_gates

        monkeypatch.setenv("TRANSLATION_QUALITY_MODE", "enforce")
        article = self._article("About 1.5 million people attended.")
        result = _result([["许多人参加了活动。"]])
        hard, _soft = _content_gates(article, result)
        assert hard

    def test_invalid_mode_falls_back_to_enforce(self, monkeypatch):
        monkeypatch.setenv("TRANSLATION_QUALITY_MODE", "chaos")
        assert quality.mode() == "enforce"
