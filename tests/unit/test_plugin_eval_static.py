"""
插件评测静态层与复合分引擎单元测试
"""
import json

import pytest

from app.services.plugin_eval.engine import (
    DIMENSION_WEIGHTS,
    badge_from_score,
    build_composite,
    build_composite_from_static_overall,
    score_to_grade,
)
from app.services.plugin_eval.parser import parse_skill, parse_skill_content
from app.services.plugin_eval.static_layer import StaticAnalyzer, anti_pattern_penalty
from app.services.plugin_evaluator import (
    PluginEvaluator,
    _extract_and_parse,
    _measured_score,
    _resolve_judge_model,
)

pytestmark = pytest.mark.unit


GOOD_SKILL_MD = """---
name: demo-checker
description: Use when the user asks to check project demo files for broken links, missing references, or stale examples. Use proactively for demo hygiene tasks.
---

# Demo Checker

## Usage

Check the demo files step by step.

```bash
./check.sh --all
```

```python
print("example")
```

## References

See [details](references/details.md) for more.
"""


@pytest.fixture
def good_skill_dir(tmp_path):
    """最小合格 skill：frontmatter 完整、带触发短语、有 references/"""
    skill = tmp_path / "demo-checker"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
    (skill / "references" / "details.md").write_text("# details\n", encoding="utf-8")
    return skill


@pytest.fixture
def poor_skill_dir(tmp_path):
    """故意堆反模式的 skill：超量 MUST/短描述/无触发短语"""
    skill = tmp_path / "poor-skill"
    skill.mkdir()
    lines = ["---", "name: poor-skill", "description: short", "---", "# Poor"]
    for i in range(30):
        lines.append(f"You MUST always do step {i}. NEVER skip it.")
    (skill / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill


class TestStaticLayer:
    """静态层打分与反模式检测"""

    def test_good_skill_scores_and_no_antipatterns(self, good_skill_dir):
        result = StaticAnalyzer().analyze_skill(good_skill_dir)
        assert result["layer"] == "static"
        assert 0.0 <= result["score"] <= 1.0
        assert result["score"] > 0.5
        assert result["anti_patterns"] == []
        assert set(result["sub_scores"]) == {
            "frontmatter_quality", "orchestration_wiring", "progressive_disclosure",
            "structural_completeness", "token_efficiency", "ecosystem_coherence",
            "harness_portability",
        }
        assert all(0.0 <= v <= 1.0 for v in result["sub_scores"].values())

    def test_poor_skill_detects_antipatterns(self, poor_skill_dir):
        result = StaticAnalyzer().analyze_skill(poor_skill_dir)
        flags = {ap["flag"] for ap in result["anti_patterns"]}
        assert "OVER_CONSTRAINED" in flags
        assert "EMPTY_DESCRIPTION" in flags
        assert "MISSING_TRIGGER" in flags

    def test_anti_pattern_penalty_floor(self):
        assert anti_pattern_penalty(0) == 1.0
        assert anti_pattern_penalty(5) == 0.75
        assert anti_pattern_penalty(20) == 0.5  # 下限 0.5

    def test_inline_content_parse(self):
        skill = parse_skill_content(GOOD_SKILL_MD, name="inline-demo")
        assert skill.name == "demo-checker"
        assert "Use when" in skill.description
        assert skill.code_block_count == 2
        assert set(skill.code_block_languages) == {"bash", "python"}


class TestCompositeEngine:
    """复合分引擎：层混合、权重再归一化、徽章"""

    def _static_layer(self, score=0.8, anti_patterns=None):
        return {
            "layer": "static",
            "score": score,
            "sub_scores": {
                "frontmatter_quality": score,
                "orchestration_wiring": score,
                "progressive_disclosure": score,
                "structural_completeness": score,
                "token_efficiency": score,
                "ecosystem_coherence": score,
            },
            "anti_patterns": anti_patterns or [],
            "metadata": {},
        }

    def test_quick_depth_static_only(self):
        composite = build_composite([self._static_layer(0.8)], depth="quick")
        assert composite["confidence_label"] == "Estimated"
        assert 0.0 <= composite["score"] <= 100.0
        assert composite["anti_pattern_penalty"] == 1.0
        # 未测维度（judge-only 维度）记 None
        unmeasured = [d["name"] for d in composite["dimensions"] if d["score"] is None]
        assert "output_quality" in unmeasured
        assert "robustness" in unmeasured

    def test_standard_depth_blends_judge(self):
        judge_layer = {
            "layer": "judge",
            "score": 0.7,
            "sub_scores": {
                "triggering_accuracy": 0.9,
                "orchestration_fitness": 0.6,
                "output_quality": 0.5,
                "scope_calibration": 0.8,
            },
            "metadata": {},
        }
        composite = build_composite([self._static_layer(0.8), judge_layer], depth="standard")
        assert composite["confidence_label"] == "Assessed"
        unmeasured = [d["name"] for d in composite["dimensions"] if d["score"] is None]
        # code_template_quality 两层都不产分，永远 unmeasured
        assert "code_template_quality" in unmeasured

    def test_renormalization_when_judge_partially_measured(self):
        """judge 只测出部分维度时权重再归一化，不影响总分区间"""
        judge_layer = {
            "layer": "judge",
            "score": 0.5,
            "sub_scores": {"triggering_accuracy": 0.9},
            "metadata": {},
        }
        composite = build_composite([self._static_layer(0.8), judge_layer], depth="standard")
        assert 0.0 <= composite["score"] <= 100.0

    def test_anti_pattern_penalty_applied(self):
        ap = [{"flag": "OVER_CONSTRAINED", "description": "x", "severity": 0.1}]
        penalized = build_composite([self._static_layer(0.8, [ap[0]])], depth="quick")
        clean = build_composite([self._static_layer(0.8)], depth="quick")
        assert penalized["score"] < clean["score"]
        assert penalized["anti_pattern_penalty"] == pytest.approx(0.95)

    def test_plugin_level_composite(self):
        composite = build_composite_from_static_overall(0.8261, 0, depth="quick")
        assert composite["score"] > 80.0
        assert composite["badge"] in ("gold", "platinum")

    def test_badge_thresholds(self):
        assert badge_from_score(90) == "platinum"
        assert badge_from_score(85) == "gold"
        assert badge_from_score(75) == "silver"
        assert badge_from_score(65) == "bronze"
        assert badge_from_score(30) == "no_badge"

    def test_score_to_grade(self):
        assert score_to_grade(97) == "A+"
        assert score_to_grade(83) == "B"
        assert score_to_grade(59) == "F"

    def test_dimension_weights_sum_to_one(self):
        assert pytest.approx(sum(DIMENSION_WEIGHTS.values())) == 1.0


class TestJudgeHelpers:
    """judge 输出解析与度量"""

    def test_extract_plain_json(self):
        assert _extract_and_parse('{"score": 0.8}')["score"] == 0.8

    def test_extract_fenced_json(self):
        assert _extract_and_parse('```json\n{"score": 0.8}\n```')["score"] == 0.8

    def test_extract_non_json_degrades(self):
        result = _extract_and_parse("not json at all")
        assert result["unmeasured"] is True

    def test_extract_empty_degrades(self):
        assert _extract_and_parse("")["unmeasured"] is True

    def test_measured_score_bounds(self):
        assert _measured_score({"f1": 1.5}, "f1") == 1.0
        assert _measured_score({"f1": -0.2}, "f1") == 0.0
        assert _measured_score({"f1": "bad"}, "f1") is None
        assert _measured_score({"unmeasured": True}, "f1") is None


class TestResolveJudgeModel:
    """judge 模型分层：haiku 档固定映射，sonnet 档以 Claude Code 配置优先"""

    def test_haiku_tier_always_mapped(self):
        assert _resolve_judge_model("haiku", {"model": "gateway-model-x"}) == "claude-haiku-4-5-20251001"
        assert _resolve_judge_model("haiku", None) == "claude-haiku-4-5-20251001"

    def test_sonnet_tier_prefers_config(self):
        assert _resolve_judge_model("sonnet", {"model": "gateway-sonnet"}) == "gateway-sonnet"
        assert _resolve_judge_model("sonnet", {}) == "claude-sonnet-5"
        assert _resolve_judge_model("sonnet", None) == "claude-sonnet-5"

    def test_unknown_tier_falls_back_to_sonnet(self):
        assert _resolve_judge_model("other", {"model": "m"}) == "m"


class TestCCQueryDegrades:
    """_cc_query 降级路径（不真调 SDK）"""

    def test_unconfigured_degrades(self, monkeypatch):
        from app.services import plugin_evaluator as pe
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        monkeypatch.setattr(ClaudeCodeConfigService, "get_active_config", classmethod(lambda cls, *a, **k: None))
        result = pe._cc_query("prompt", "claude-sonnet-5")
        assert result["unmeasured"] is True
        assert "未配置" in result["error"]


class TestPluginEvaluator:
    """PluginEvaluator 行为（真临时目录，不调模型）

    本类校验**内置移植实现**（legacy 引擎）；market 引擎（实时加载市场版 plugin-eval）
    见 test_plugin_eval_market_engine.py。
    """

    @pytest.fixture(autouse=True)
    def legacy_engine(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_EVAL_ENGINE", "legacy")

    def test_quick_depth_end_to_end(self, good_skill_dir):
        evaluator = PluginEvaluator(depth="quick")
        row = evaluator.evaluate(plugin_path=str(good_skill_dir))
        assert row["success"] is True
        assert row["target_type"] == "skill"
        assert row["target_name"] == "demo-checker"
        assert 0.0 <= row["average_score"] <= 100.0
        assert row["badge"] in ("platinum", "gold", "silver", "bronze", "no_badge")
        assert row["plugin_eval"]["depth"] == "quick"
        assert row["plugin_eval"]["composite"]["confidence_label"] == "Estimated"
        # 快速深度零模型调用
        assert row["plugin_eval"]["model_usage"] == {}
        # 行结果可 JSON 序列化（落库要求）
        json.dumps(row, ensure_ascii=False)

    def test_missing_source_fails(self):
        row = PluginEvaluator(depth="quick").evaluate()
        assert row["success"] is False
        assert "plugin_path" in row["error"]

    def test_nonexistent_path_fails(self):
        row = PluginEvaluator(depth="quick").evaluate(plugin_path="/nonexistent/dir")
        assert row["success"] is False
        assert "不存在" in row["error"]

    def test_unknown_dir_fails(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        row = PluginEvaluator(depth="quick").evaluate(plugin_path=str(empty))
        assert row["success"] is False
        assert "SKILL.md" in row["error"]

    def test_standard_depth_without_cc_degrades(self, good_skill_dir, monkeypatch):
        """standard 深度未配置 Claude Code：judge 层降级 unmeasured，复合分重归一化仍可用"""
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        monkeypatch.setattr(ClaudeCodeConfigService, "get_active_config", classmethod(lambda cls, *a, **k: None))
        row = PluginEvaluator(depth="standard").evaluate(plugin_path=str(good_skill_dir))
        assert row["success"] is True
        assert row["plugin_eval"]["composite"]["confidence_label"] == "Assessed"
        judge_layers = [lr for lr in row["plugin_eval"]["layers"] if lr["layer"] == "judge"]
        assert len(judge_layers) == 1
        assert judge_layers[0]["sub_scores"] == {}
        assert any("Claude Code" in w for w in row["plugin_eval"]["warnings"])

    def test_standard_depth_with_judge_mock(self, good_skill_dir, monkeypatch):
        """standard 深度 + Claude Code 调用 mock：4 项评估计入复合分"""
        from app.services import plugin_evaluator as pe
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        monkeypatch.setattr(
            ClaudeCodeConfigService, "get_active_config",
            classmethod(lambda cls, *a, **k: {"model": "claude-sonnet-5", "base_url": "http://gw", "auth_token": "x"}),
        )

        responses = {
            "Generate 10 synthetic user prompts": '{"precision": 0.9, "recall": 0.8, "f1": 0.85}',
            "orchestration fitness": '{"score": 0.7, "reasoning": "ok"}',
            "Simulate 3 realistic tasks": '{"score": 0.6}',
            "scope calibration": '{"score": 0.9, "assessment": "ok"}',
        }
        captured_models = []

        def fake_cc_query(prompt, model, timeout=300.0, usage_sink=None):
            captured_models.append(model)
            for marker, resp in responses.items():
                if marker in prompt:
                    return json.loads(resp)
            return {"unmeasured": True, "error": "no match"}

        monkeypatch.setattr(pe, "_cc_query", fake_cc_query)
        evaluator = PluginEvaluator(depth="standard")
        row = evaluator.evaluate(plugin_path=str(good_skill_dir))

        assert row["success"] is True
        judge_layers = [lr for lr in row["plugin_eval"]["layers"] if lr["layer"] == "judge"]
        assert judge_layers[0]["sub_scores"] == {
            "triggering_accuracy": 0.85,
            "orchestration_fitness": 0.7,
            "output_quality": 0.6,
            "scope_calibration": 0.9,
        }
        assert judge_layers[0]["metadata"]["judge_engine"] == "claude_code"
        # 模型分层：triggering 走 haiku 档，其余走配置模型（sonnet 档）
        assert captured_models.count("claude-haiku-4-5-20251001") == 1
        assert captured_models.count("claude-sonnet-5") == 3
        # judge 全部测出后，仅 monte_carlo 独占的 robustness 与无层产出的
        # code_template_quality 保持未测
        unmeasured = [d["name"] for d in row["plugin_eval"]["composite"]["dimensions"] if d["score"] is None]
        assert sorted(unmeasured) == ["code_template_quality", "robustness"]

    def test_standard_depth_judge_non_json_degrades(self, good_skill_dir, monkeypatch):
        """judge 返回非 JSON：该维度 unmeasured，行仍成功"""
        from app.services import plugin_evaluator as pe
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        monkeypatch.setattr(
            ClaudeCodeConfigService, "get_active_config",
            classmethod(lambda cls, *a, **k: {"model": "claude-sonnet-5"}),
        )
        monkeypatch.setattr(
            pe, "_cc_query",
            lambda prompt, model, timeout=300.0, usage_sink=None: {"unmeasured": True, "error": "judge response was not valid JSON"},
        )
        evaluator = PluginEvaluator(depth="standard")
        row = evaluator.evaluate(plugin_path=str(good_skill_dir))
        assert row["success"] is True
        assert any("未测出" in w for w in row["plugin_eval"]["warnings"])
