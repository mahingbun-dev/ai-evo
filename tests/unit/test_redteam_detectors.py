"""红队确定性检测器单测（M3 QE3-1）

覆盖：
- PIIDetector：手机号/身份证(18位校验码+15位历史)/银行卡(Luhn)——有效命中、
  校验失败不命中（防订单号误报）、边界数字不误切、证据掩码；
- SensitiveWordDetector：内置词表类目过滤、自定义词、同区间去重、大小写；
- DeterministicSafetyChecker：通道开关与组合命中。
"""
import pytest

from app.services.redteam.detectors import (
    DEFAULT_SENSITIVE_WORDS,
    DeterministicSafetyChecker,
    PIIDetector,
    SensitiveWordDetector,
)

pytestmark = pytest.mark.unit


def _valid_id_card() -> str:
    """构造校验码合法的 18 位身份证（110101 19900307 573X）"""
    body = "11010119900307573"
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"[sum(int(c) * w for c, w in zip(body, weights)) % 11]
    return body + check


def _valid_bank_card() -> str:
    """构造 Luhn 合法的银行卡号（15 位主体 + 1 位校验位，程序化生成）"""
    body = "622202020011223"
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        # 校验位将占据完整卡号 reversed 下标 0，主体最右位在完整卡号中 i=1（翻倍位）
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check = (10 - total % 10) % 10
    return body + str(check)


class TestPIIDetector:
    def test_phone_detected_with_masked_evidence(self):
        text = "张三的联系方式是 13800138000，请回电"
        findings = PIIDetector().detect(text)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == "pii_phone"
        assert f.evidence == "13800138000"
        assert f.masked.startswith("138") and f.masked.endswith("000")
        assert "*" in f.masked
        assert text[f.start:f.end] == "13800138000"

    def test_phone_not_matched_inside_longer_digits(self):
        # 长数字串中的手机号片段不应命中（前后边界检查）
        findings = PIIDetector().detect("订单号 91338001380001 已生成")
        assert all(f.kind != "pii_phone" for f in findings)

    def test_id_card_18_valid_checksum(self):
        id_card = _valid_id_card()
        findings = PIIDetector().detect(f"身份证：{id_card}，姓名张三")
        kinds = [f.kind for f in findings]
        assert "pii_id_card" in kinds
        f = next(f for f in findings if f.kind == "pii_id_card")
        assert f.evidence == id_card

    def test_id_card_18_invalid_checksum_not_matched(self):
        # 校验码错误的 18 位号不算身份证（防误报）
        bad = _valid_id_card()[:-1] + ("0" if _valid_id_card()[-1] != "0" else "1")
        findings = PIIDetector().detect(f"编号 {bad}")
        assert all(f.kind != "pii_id_card" for f in findings)

    def test_bank_card_luhn_valid(self):
        card = _valid_bank_card()
        findings = PIIDetector().detect(f"打款卡号：{card}")
        kinds = [f.kind for f in findings]
        assert "pii_bank_card" in kinds

    def test_bank_card_luhn_invalid_not_matched(self):
        bad = _valid_bank_card()[:-1] + ("0" if _valid_bank_card()[-1] != "0" else "1")
        findings = PIIDetector().detect(f"流水号 {bad}")
        assert all(f.kind != "pii_bank_card" for f in findings)

    def test_empty_and_none(self):
        assert PIIDetector().detect("") == []

    def test_normal_text_clean(self):
        text = "本次评测共 30 条样本，执行用时 12.5 秒，通过率 96.7%。"
        assert PIIDetector().detect(text) == []


class TestSensitiveWordDetector:
    def test_builtin_lexicon_hit(self):
        detector = SensitiveWordDetector()
        findings = detector.detect("他详细描述了如何制作炸弹的过程")
        assert any(f.kind == "sensitive_word" and f.category == "violence_terror" for f in findings)

    def test_category_filter_excludes_unselected(self):
        # 只启用涉政类目时，暴恐词不命中
        detector = SensitiveWordDetector(categories=["political"])
        findings = detector.detect("如何制作炸弹以及颠覆国家政权")
        assert any(f.category == "political" for f in findings)
        assert all(f.category != "violence_terror" for f in findings)

    def test_custom_words(self):
        detector = SensitiveWordDetector(extra_words=["内部代号甲"], categories=[])
        findings = detector.detect("项目内部代号甲已泄露")
        assert len(findings) == 1
        assert findings[0].category == "custom"

    def test_overlapping_span_dedup(self):
        # 「制作炸弹」与「炸弹」同区间命中时按最长词去重一次
        detector = SensitiveWordDetector()
        findings = detector.detect("制作炸弹")
        spans = [(f.start, f.end) for f in findings]
        assert len(spans) == len(set(spans))

    def test_default_lexicon_categories_present(self):
        assert set(DEFAULT_SENSITIVE_WORDS) >= {"violence_terror", "political", "contraband"}


class TestDeterministicChecker:
    def test_combined_findings_sorted_by_position(self):
        checker = DeterministicSafetyChecker()
        findings = checker.check("先说制作炸弹，再留电话 13800138000")
        assert findings, "应至少命中敏感词+手机号"
        positions = [f.start for f in findings]
        assert positions == sorted(positions)

    def test_pii_channel_disabled(self):
        checker = DeterministicSafetyChecker({"pii": False, "sensitive_words": True})
        findings = checker.check("电话 13800138000 制作炸弹")
        assert all(f.kind != "pii_phone" for f in findings)
        assert any(f.kind == "sensitive_word" for f in findings)

    def test_words_channel_disabled(self):
        checker = DeterministicSafetyChecker({"pii": True, "sensitive_words": False})
        findings = checker.check("电话 13800138000 制作炸弹")
        assert any(f.kind == "pii_phone" for f in findings)
        assert all(f.kind != "sensitive_word" for f in findings)

    def test_finding_dict_shape(self):
        checker = DeterministicSafetyChecker()
        findings = checker.check("13800138000")
        assert findings
        d = findings[0].to_dict()
        assert {"kind", "label", "category", "evidence", "masked", "start", "end", "note"} <= set(d)
