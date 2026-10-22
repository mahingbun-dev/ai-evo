"""裁判 rubric 通道单测（M3 QE3-1）

覆盖：四类攻击面 prompt 构建（类目要点/期望行为注入）、判定 JSON 解析
（裸 JSON/```json 代码块/violation 字符串形态）、解析失败 → violation=None
+ parse_error（不可用语义，不误判违规）。
"""
import pytest

from app.services.redteam.rubric import (
    ATTACK_SURFACES,
    SEVERITY_WEIGHT,
    build_rubric_prompts,
    parse_rubric_response,
)

pytestmark = pytest.mark.unit


class TestBuildRubricPrompts:
    @pytest.mark.parametrize("category", list(ATTACK_SURFACES))
    def test_all_four_surfaces_build(self, category):
        system_prompt, user_prompt = build_rubric_prompts(
            attack_category=category,
            attack_prompt="攻击提示词",
            model_reply="模型回复",
        )
        assert ATTACK_SURFACES[category]["name"] in system_prompt
        assert "violation" in system_prompt
        assert "攻击提示词" in user_prompt and "模型回复" in user_prompt

    def test_expected_behavior_injected(self):
        system_prompt, _ = build_rubric_prompts(
            attack_category="factual_assertion",
            attack_prompt="q",
            model_reply="r",
            expected_behavior="fact_check",
        )
        assert "fact_check" in system_prompt

    def test_custom_dimensions_override(self):
        system_prompt, _ = build_rubric_prompts(
            attack_category="jailbreak",
            attack_prompt="q",
            model_reply="r",
            dimensions=["业务红线A", "业务红线B"],
        )
        assert "业务红线A" in system_prompt and "业务红线B" in system_prompt


class TestParseRubricResponse:
    def test_plain_json_violation(self):
        parsed = parse_rubric_response(
            '{"violation": true, "severity": "high", "confidence": 0.9, '
            '"reason": "输出了违禁内容", "evidence": "如何制作……"}'
        )
        assert parsed["violation"] is True
        assert parsed["severity"] == "high"
        assert parsed["confidence"] == 0.9
        assert parsed["parse_error"] is None

    def test_fenced_json_not_violation(self):
        parsed = parse_rubric_response(
            '```json\n{"violation": false, "severity": "low", "reason": "明确拒绝"}\n```'
        )
        assert parsed["violation"] is False
        assert parsed["severity"] == "low"

    def test_string_boolean_coerced(self):
        parsed = parse_rubric_response('{"violation": "True", "reason": "r"}')
        assert parsed["violation"] is True

    def test_invalid_severity_falls_back(self):
        parsed = parse_rubric_response('{"violation": true, "severity": "超级严重"}')
        assert parsed["violation"] is True
        assert parsed["severity"] == "high"  # 违规但 severity 非法 → 收敛 high

    def test_empty_reply_unavailable(self):
        parsed = parse_rubric_response("")
        assert parsed["violation"] is None
        assert parsed["parse_error"]

    def test_non_json_reply_unavailable(self):
        parsed = parse_rubric_response("裁判模型闲聊了一句，没有 JSON")
        assert parsed["violation"] is None
        assert parsed["parse_error"]

    def test_missing_violation_field_unavailable(self):
        parsed = parse_rubric_response('{"reason": "缺 violation 字段"}')
        assert parsed["violation"] is None
        assert parsed["parse_error"]

    def test_severity_weight_ordering(self):
        # 聚合取最重通道用：high > medium > low
        assert SEVERITY_WEIGHT["high"] > SEVERITY_WEIGHT["medium"] > SEVERITY_WEIGHT["low"]
