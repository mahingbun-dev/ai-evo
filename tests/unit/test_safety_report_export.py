"""安全评测报告导出（合规证据）+ deepeval 收敛测试（M3 QE3-2）

覆盖：
- markdown 安全报告：检查项（对齐《生成式人工智能服务安全基本要求》类目
  命名）→ 结果 → 证据（掩码原文/裁判理由/deepeval 摘录）→ 复测建议，
  含门禁结论与变异统计；
- xlsx 工作簿：检查项汇总/违规证据/复测建议三工作表，行数与列抽查；
- build_export_bytes 分发：safety 任务 markdown 自动切换安全报告模板、
  非 safety 任务走通用模板、json 不受影响；export_filename xlsx 扩展名；
- deepeval metric 开关：safety_config.metrics 校验、引擎逐指标通道
  （违规计入/不可用不计/未启用不产出）、聚合逐指标统计；
- 超管直调端点废弃标记（OpenAPI deprecated + Deprecation 响应头）。
"""
import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import result_report
from app.services.evaluators.safety import aggregate_safety_results
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.redteam.engine import RedTeamSafetyEngine

pytestmark = pytest.mark.unit


def _safety_task(metrics=None):
    config = {
        "categories": ["prompt_injection", "jailbreak", "sensitive_content", "factual_assertion"],
        "sample_source": "pack", "pack": "redteam_pack_v1", "pack_version": "1.0.0",
        "mutation": {"enabled": True, "count": 1},
        "channels": {"deterministic": {}, "judge_rubric": {}},
    }
    if metrics is not None:
        config["metrics"] = metrics
    return SimpleNamespace(
        id="task_s1", name="上线前安全评测", evaluation_type="safety", safety_config=config,
    )


def _safety_record(result=None, gate_result=None):
    return SimpleNamespace(
        execution_number=3, status="completed", started_at=None, completed_at=None,
        total_cost=None, error_message=None, config_snapshot=None,
        gate_result=gate_result,
        result=result if result is not None else _aggregate_fixture(),
    )


def _row(idx, category, violation, *, det_hit=False, judge_violation=False,
         deepeval=None, is_mutation=False):
    channels = {
        "deterministic": {
            "triggered": det_hit,
            "findings": ([{"kind": "pii_phone", "label": "手机号", "evidence": "13800138000",
                           "masked": "138***000"}] if det_hit else []),
        },
        "judge_rubric": {"violation": judge_violation, "unavailable": False,
                          "reason": "顺从攻击输出有害内容" if judge_violation else "已明确拒绝"},
    }
    if deepeval is not None:
        channels["deepeval"] = deepeval
    violation_flag = violation
    return {
        "idx": idx, "success": True, "user_input": f"攻击样本 {idx}", "model_output": f"模型回复 {idx}",
        "attack_category": category, "attack_sample_id": f"{category[:2]}-00{idx}",
        "is_mutation": is_mutation, "risk_point": "诱导泄露",
        "safety": {"passed": not violation_flag, "violation": violation_flag,
                    "severity": "high" if violation_flag else "",
                    "channels": channels},
    }


def _aggregate_fixture():
    """手算口径：6 行执行，2 违规；pi 3 行 1 违规（确定性命中）；
    jb 3 行 1 违规（裁判+deepeval bias 违规，1 条为变异体）"""
    rows = [
        _row(0, "prompt_injection", True, det_hit=True),
        _row(1, "prompt_injection", False),
        _row(2, "prompt_injection", False),
        _row(3, "jailbreak", True, judge_violation=True,
             deepeval={"bias": {"score": 0.3, "success": False, "metric_unavailable": False},
                        "toxicity": {"score": 0.8, "success": True, "metric_unavailable": False}}),
        _row(4, "jailbreak", False,
             deepeval={"bias": {"score": 0.9, "success": True, "metric_unavailable": False},
                        "toxicity": {"score": 0.9, "success": True, "metric_unavailable": False}}),
        _row(5, "jailbreak", False, is_mutation=True),
    ]
    return aggregate_safety_results(rows)


class TestSafetyMarkdownReport:
    def test_compliance_check_items_and_results(self):
        md = result_report.build_safety_markdown_export(
            _safety_record(gate_result={"verdict": "fail"}), _safety_task(metrics=["bias", "toxicity"])
        ).decode("utf-8")
        # 检查项命名对齐《生成式人工智能服务安全基本要求》类目口径
        for check_name in [
            "绕过安全防护类——提示注入攻击",
            "绕过安全防护类——越狱攻击",
            "侵犯个人权益类——个人信息与敏感词输出检测",
            "歧视性内容——偏见检测",  # deepeval bias 检查项
            "违法不良信息类——毒性与有害内容",  # deepeval toxicity 检查项
        ]:
            assert check_name in md, f"检查项缺失: {check_name}"
        # 结果表：样本数/问题数/防御率/结论
        assert "| 3 | 1 | 66.7% | 不通过 |" in md
        assert "通过" in md

    def test_evidence_with_masked_pii_and_judge_reason(self):
        md = result_report.build_safety_markdown_export(_safety_record(), _safety_task()).decode("utf-8")
        assert "证据 1｜样本 `pr-000`" in md
        assert "138***000" in md, "确定性证据必须掩码展示"
        assert "13800138000" not in md, "证据区不得泄露未掩码 PII 原文"
        assert "顺从攻击输出有害内容" in md
        # 违规行须标注攻击面与风险点；未违规变异体不进证据清单
        assert "提示注入" in md and "诱导泄露" in md

    def test_deepeval_summary_in_evidence(self):
        md = result_report.build_safety_markdown_export(
            _safety_record(), _safety_task(metrics=["bias", "toxicity"])).decode("utf-8")
        assert "bias: 违规(score=0.3)" in md
        assert "toxicity: 通过(score=0.8)" in md
        assert "deepeval 增强指标：bias、toxicity" in md

    def test_gate_verdict_and_retry_advice(self):
        md = result_report.build_safety_markdown_export(
            _safety_record(gate_result={"verdict": "fail"}), _safety_task()).decode("utf-8")
        assert "门禁结论：未通过（fail）" in md
        assert "## 四、复测建议" in md
        assert "全量重跑安全评测" in md
        # 变异体违规建议（fixtures 中变异体未违规——不应出现该建议）
        assert "换皮攻击" not in md or "变异体违规率高于" not in md

    def test_all_pass_report(self):
        record = _safety_record(result=aggregate_safety_results([
            _row(0, "prompt_injection", False),
        ]), gate_result={"verdict": "pass"})
        md = result_report.build_safety_markdown_export(record, _safety_task()).decode("utf-8")
        assert "未发现违规证据" in md
        assert "门禁结论：通过（pass）" in md
        assert "纳入回归基线" in md

    def test_mutation_stats_in_notes(self):
        md = result_report.build_safety_markdown_export(_safety_record(), _safety_task()).decode("utf-8")
        assert "变异增强：开启（每样本 1 个变体），本轮生成 1 个变体（违规 0）" in md


class TestSafetyXlsxReport:
    def test_workbook_sheets_and_counts(self):
        xlsx = result_report.build_safety_xlsx_export(
            _safety_record(gate_result={"verdict": "fail"}), _safety_task(metrics=["bias", "toxicity"]))
        import pandas as pd

        sheets = pd.read_excel(io.BytesIO(xlsx), sheet_name=None)
        assert list(sheets) == ["检查项汇总", "违规证据", "复测建议"]

        summary = sheets["检查项汇总"]
        # 手算：pi 检查项 3 样本 1 违规 + jb 3/1 + 确定性 6/1 + bias 6/1 + toxicity 6/0
        by_name = {row["检查项（合规类目口径）"]: row for _, row in summary.iterrows()}
        assert by_name["绕过安全防护类——提示注入攻击"]["问题数"] == 1
        assert by_name["歧视性内容——偏见检测"]["问题数"] == 1
        assert by_name["违法不良信息类——毒性与有害内容"]["问题数"] == 0
        assert by_name["侵犯个人权益类——个人信息与敏感词输出检测"]["问题数"] == 1

        evidence = sheets["违规证据"]
        assert len(evidence) == 2
        pii_cells = evidence[evidence["确定性通道"] == "命中"]["确定性证据（掩码）"].tolist()
        assert pii_cells and "138***000" in pii_cells[0]
        assert "13800138000" not in evidence.to_csv(), "xlsx 证据不得含未掩码 PII"

        advice = sheets["复测建议"]
        assert any("门禁结论" == row["检查项"] for _, row in advice.iterrows())
        assert any("门禁未通过" in str(row["复测建议"]) for _, row in advice.iterrows())

    def test_long_cell_truncated(self):
        long_row = _row(0, "prompt_injection", True, det_hit=True)
        long_row["user_input"] = "长" * 5000
        record = _safety_record(result=aggregate_safety_results([long_row]))
        xlsx = result_report.build_safety_xlsx_export(record, _safety_task())
        import pandas as pd

        evidence = pd.read_excel(io.BytesIO(xlsx), sheet_name="违规证据")
        assert len(evidence["攻击内容"].iloc[0]) <= result_report._XLSX_CELL_LIMIT + 20


class TestExportDispatcher:
    def test_safety_markdown_uses_safety_template(self):
        data = result_report.build_export_bytes(_safety_record(), _safety_task(), "markdown")
        assert data.decode("utf-8").startswith("# 安全评测报告（合规证据）")

    def test_non_safety_markdown_uses_generic_template(self):
        task = SimpleNamespace(id="t", name="裁判任务", evaluation_type="judge")
        record = _safety_record(result={"total": 1, "items": [
            {"idx": 0, "success": True, "average_score": 8, "user_input": "q", "model_output": "a"}]})
        data = result_report.build_export_bytes(record, task, "markdown")
        assert data.decode("utf-8").startswith("# 评测执行报告")

    def test_json_unchanged(self):
        import json

        data = result_report.build_export_bytes(_safety_record(), _safety_task(), "json")
        payload = json.loads(data.decode("utf-8"))
        assert payload["evaluation_type"] == "safety"
        assert "by_category" in payload["result"]

    def test_filename_xlsx_ext(self):
        name = result_report.export_filename("任务A", 2, "xlsx")
        assert name.endswith(".xlsx")


class TestDeepevalMetricSwitch:
    def test_validate_metrics_subset(self):
        service = EvaluationTaskService(MagicMock())
        normalized = service._validate_safety_config(
            {"metrics": ["bias", "toxicity"]}, dataset_id=None, model_provider="m")
        assert normalized["metrics"] == ["bias", "toxicity"]

    def test_validate_metrics_default_empty(self):
        service = EvaluationTaskService(MagicMock())
        normalized = service._validate_safety_config(None, dataset_id=None, model_provider="m")
        assert normalized["metrics"] == []

    def test_validate_metrics_unknown_rejected(self):
        service = EvaluationTaskService(MagicMock())
        with pytest.raises(ValueError, match="不支持的安全评测指标"):
            service._validate_safety_config(
                {"metrics": ["nope"]}, dataset_id=None, model_provider="m")

    def _engine_task(self, metrics):
        task = _safety_task(metrics=metrics)
        return RedTeamSafetyEngine(task)

    def test_metric_fail_counts_as_violation(self):
        engine = self._engine_task(["bias"])
        with patch.object(engine, "_judge") as mock_judge, \
             patch("app.services.safety_evaluator.SafetyEvaluator._evaluate_single_safety") as mock_eval:
            mock_judge.return_value = {"violation": False, "unavailable": False}
            mock_eval.return_value = MagicMock(score=0.3, success=False,
                                                metric_unavailable=False, reason="存在偏见", error=None)
            verdict = engine.evaluate_reply(
                {"attack_category": "jailbreak", "prompt": "p"}, "回复")
        assert verdict["violation"] is True
        assert verdict["channels"]["deepeval"]["bias"]["score"] == 0.3
        assert verdict["severity"] == "medium"

    def test_metric_unavailable_not_violation(self):
        engine = self._engine_task(["bias"])
        with patch.object(engine, "_judge") as mock_judge, \
             patch("app.services.safety_evaluator.SafetyEvaluator._evaluate_single_safety") as mock_eval:
            mock_judge.return_value = {"violation": False, "unavailable": False}
            mock_eval.return_value = MagicMock(score=None, success=False,
                                                metric_unavailable=True, reason=None, error="网关异常")
            verdict = engine.evaluate_reply({"attack_category": "jailbreak", "prompt": "p"}, "回复")
        assert verdict["violation"] is False
        assert verdict["channels"]["deepeval"]["bias"]["metric_unavailable"] is True

    def test_metric_exception_handled(self):
        engine = self._engine_task(["bias"])
        with patch.object(engine, "_judge") as mock_judge, \
             patch("app.services.safety_evaluator.SafetyEvaluator._evaluate_single_safety",
                   side_effect=RuntimeError("适配失败")):
            mock_judge.return_value = {"violation": False, "unavailable": False}
            verdict = engine.evaluate_reply({"attack_category": "jailbreak", "prompt": "p"}, "回复")
        assert verdict["violation"] is False
        assert verdict["channels"]["deepeval"]["bias"]["metric_unavailable"] is True

    def test_metrics_disabled_no_channel(self):
        engine = self._engine_task(None)
        with patch.object(engine, "_judge") as mock_judge:
            mock_judge.return_value = {"violation": False, "unavailable": False}
            verdict = engine.evaluate_reply({"attack_category": "jailbreak", "prompt": "p"}, "回复")
        assert "deepeval" not in verdict["channels"]

    def test_aggregate_deepeval_stats(self):
        agg = _aggregate_fixture()
        deepeval = agg["by_channel"]["deepeval"]
        # 手算：bias 违规 1（idx3）、toxicity 违规 0；行级违规 1、指标统计按指标独立
        assert deepeval["metrics"]["bias"] == {"violations": 1, "unavailable": 0}
        assert deepeval["metrics"]["toxicity"] == {"violations": 0, "unavailable": 0}
        assert deepeval["violations"] == 1


class TestSuperAdminEndpointDeprecated:
    def test_routes_marked_deprecated(self):
        from app.api.safety_evaluation_api import DEPRECATION_NOTICE, router

        assert "已废弃" in DEPRECATION_NOTICE
        deprecated_paths = [route.path for route in router.routes if getattr(route, "deprecated", False)]
        assert set(deprecated_paths) == {"/safety-evaluation/evaluate",
                                         "/safety-evaluation/bias",
                                         "/safety-evaluation/toxicity"}

    def test_deprecation_headers(self):
        from fastapi import Response

        from app.api.safety_evaluation_api import _mark_deprecated

        response = Response()
        request = MagicMock()
        _mark_deprecated(request, response)
        assert response.headers["Deprecation"] == "true"
        assert response.headers["Sunset"] == "deprecated"
        assert "successor-version" in response.headers["Link"]
