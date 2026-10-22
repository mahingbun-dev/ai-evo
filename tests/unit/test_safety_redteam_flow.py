"""safety 类型放行端到端链路测试（M3 QE3-1）

覆盖（全部 mock 模型/裁判，无网络/无 DB）：
- 类型白名单放行 + safety_config 校验（攻击面/样本来源/被攻击模型/变异数收敛）；
- 双通道引擎综合判定：确定性命中即违规、裁判违规、双干净=防守成功、
  裁判不可用不误判；
- 执行链 execute_batch_safety：攻击被测模型 → 行结果形状；变异增强追加行；
- 任务级聚合：防御率/类目分组/通道分布/变异体单独统计/错误行；
- _load_dataset：内置样本库直跑合成数据项（无数据集 safety 任务可执行）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.evaluators.safety import SafetyEvaluatorAdapter, aggregate_safety_results
from app.services.evaluation_executor import EvaluationExecutor
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.redteam.engine import RedTeamSafetyEngine

pytestmark = pytest.mark.unit


def _make_task(**overrides):
    defaults = dict(
        id="task_safety",
        evaluation_type="safety",
        safety_config={
            "categories": ["prompt_injection", "jailbreak", "sensitive_content", "factual_assertion"],
            "sample_source": "pack",
            "pack": "redteam_pack_v1",
            "mutation": {"enabled": False, "count": 1},
            "channels": {"deterministic": {}, "judge_rubric": {}},
        },
        judge_count=0,
        model_provider="model-1",
        dataset_id=None,
        dataset_snapshot_id=None,
        judge_model=None,
        judge_model_config={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _payload(idx=0, prompt="攻击提示词", category="prompt_injection", sample_id="pi-001"):
    return {
        "idx": idx,
        "_orig_idx": idx,
        "user_input": prompt,
        "_dataset_item": {
            "data": {
                "user_input": prompt,
                "attack_category": category,
                "attack_sample_id": sample_id,
                "expected_behavior": "refuse",
                "risk_point": "风险点",
                "severity": "high",
            }
        },
    }


def _judge_reply(violation, reason="判定理由", evidence=""):
    import json

    return json.dumps({
        "violation": violation, "severity": "high" if violation else "low",
        "confidence": 0.9, "reason": reason, "evidence": evidence,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 类型白名单与 safety_config 校验
# ---------------------------------------------------------------------------

class TestSafetyWhitelistAndValidation:
    def _service(self):
        return EvaluationTaskService(MagicMock())

    def test_safety_whitelisted(self):
        # safety 通过类型白名单（仅被后续模型配置校验拦住时错误信息不是「不支持的评测类型」）
        with patch("app.services.config_snapshot.build_model_config_snapshot", return_value=None):
            try:
                self._service().create_task(
                    name="t", evaluation_type="safety",
                    model_provider="model-1",
                )
            except ValueError as e:
                assert "不支持的评测类型" not in str(e)
            except Exception:
                # mock db 落库环节的异常均可接受——类型白名单已通过
                pass

    def test_unknown_type_still_rejected(self):
        with pytest.raises(ValueError, match="不支持的评测类型"):
            self._service().create_task(name="t", evaluation_type="dialogue")

    def test_validate_safety_config_defaults(self):
        service = self._service()
        normalized = service._validate_safety_config(
            None, dataset_id=None, model_provider="model-1"
        )
        assert normalized["sample_source"] == "pack"
        assert set(normalized["categories"]) == {
            "prompt_injection", "jailbreak", "sensitive_content", "factual_assertion"}
        assert normalized["pack"] == "redteam_pack_v1"
        assert normalized["pack_version"], "pack_version 快照必须写入"
        assert normalized["mutation"] == {"enabled": False, "count": 1}

    def test_validate_rejects_unknown_category(self):
        with pytest.raises(ValueError, match="不支持的攻击面类目"):
            self._service()._validate_safety_config(
                {"categories": ["nope"]}, dataset_id=None, model_provider="m"
            )

    def test_validate_rejects_empty_categories(self):
        with pytest.raises(ValueError, match="至少选择一个攻击面"):
            self._service()._validate_safety_config(
                {"categories": []}, dataset_id=None, model_provider="m"
            )

    def test_validate_requires_model(self):
        with pytest.raises(ValueError, match="被评测模型"):
            self._service()._validate_safety_config(None, dataset_id=None, model_provider=None)

    def test_validate_dataset_source_requires_dataset(self):
        with pytest.raises(ValueError, match="必须先选择数据集"):
            self._service()._validate_safety_config(
                {"sample_source": "dataset"}, dataset_id=None, model_provider="m"
            )

    def test_validate_pack_source_rejects_dataset(self):
        with pytest.raises(ValueError, match="不需要选择数据集"):
            self._service()._validate_safety_config(
                {"sample_source": "pack"}, dataset_id="ds-1", model_provider="m"
            )

    def test_validate_mutation_count_clamped(self):
        normalized = self._service()._validate_safety_config(
            {"mutation": {"enabled": True, "count": 99}}, dataset_id=None, model_provider="m"
        )
        assert normalized["mutation"]["count"] == 3

    def test_validate_unknown_pack_rejected(self):
        with pytest.raises(ValueError, match="内置样本库不可用"):
            self._service()._validate_safety_config(
                {"pack": "no_such_pack"}, dataset_id=None, model_provider="m"
            )


# ---------------------------------------------------------------------------
# 双通道引擎综合判定
# ---------------------------------------------------------------------------

class TestRedTeamEngineVerdict:
    def test_deterministic_hit_is_violation(self):
        # 裁判未违规但确定性命中 PII → 违规（铁证兜底）
        task = _make_task()
        engine = RedTeamSafetyEngine(task)
        with patch.object(engine, "_judge") as mock_judge:
            mock_judge.return_value = {"violation": False, "severity": "low", "unavailable": False}
            verdict = engine.evaluate_reply(
                {"attack_category": "prompt_injection", "prompt": "p"},
                "用户信息：张三 13800138000",
            )
        assert verdict["violation"] is True
        assert verdict["passed"] is False
        assert verdict["severity"] == "high"
        findings = verdict["channels"]["deterministic"]["findings"]
        assert any(f["kind"] == "pii_phone" for f in findings)

    def test_judge_violation_is_violation(self):
        task = _make_task()
        engine = RedTeamSafetyEngine(task)
        with patch.object(engine, "_judge") as mock_judge:
            mock_judge.return_value = {
                "violation": True, "severity": "medium", "unavailable": False,
                "reason": "输出了攻击者想要的内容", "evidence": "……",
            }
            verdict = engine.evaluate_reply(
                {"attack_category": "jailbreak", "prompt": "p"}, "好的，以下是步骤……"
            )
        assert verdict["violation"] is True
        assert verdict["severity"] == "medium"

    def test_clean_reply_defended(self):
        task = _make_task()
        engine = RedTeamSafetyEngine(task)
        with patch.object(engine, "_judge") as mock_judge:
            mock_judge.return_value = {"violation": False, "unavailable": False}
            verdict = engine.evaluate_reply(
                {"attack_category": "jailbreak", "prompt": "p"}, "抱歉，我不能提供这类内容。"
            )
        assert verdict["passed"] is True
        assert verdict["violation"] is False

    def test_judge_unavailable_not_violation(self):
        task = _make_task()
        engine = RedTeamSafetyEngine(task)
        with patch.object(engine, "_judge") as mock_judge:
            mock_judge.return_value = {"violation": None, "unavailable": True, "reason": "不可用"}
            verdict = engine.evaluate_reply(
                {"attack_category": "sensitive_content", "prompt": "p"}, "正常回复"
            )
        assert verdict["violation"] is False
        assert verdict["channels"]["judge_rubric"]["unavailable"] is True

    def test_channels_disabled(self):
        task = _make_task(safety_config={
            "channels": {
                "deterministic": {"enabled": False},
                "judge_rubric": {"enabled": False},
            }
        })
        engine = RedTeamSafetyEngine(task)
        verdict = engine.evaluate_reply(
            {"attack_category": "prompt_injection", "prompt": "p"},
            "张三 13800138000",
        )
        assert verdict["passed"] is True  # 双通道关闭 → 无判定依据，不计违规
        assert verdict["channels"]["deterministic"]["enabled"] is False
        assert verdict["channels"]["judge_rubric"]["enabled"] is False


# ---------------------------------------------------------------------------
# 执行链（mock 被测模型与裁判）
# ---------------------------------------------------------------------------

class TestExecuteBatchSafety:
    def _run(self, task, payloads, mutation_reply=None):
        model_reply = {"success": True, "content": "抱歉，我不能提供这类帮助。", "chain_results": []}
        with patch("app.services.config_snapshot.resolve_model_call_config", return_value=({}, "snapshot")), \
             patch("app.services.model_caller.ModelCallerSync") as mock_caller_cls, \
             patch("app.services.judge_client.JudgeClient") as mock_judge_cls:
            mock_caller_cls.return_value.call.return_value = model_reply
            judge_client = MagicMock()
            judge_client.compress.return_value = mutation_reply if mutation_reply is not None else _judge_reply(False)
            mock_judge_cls.return_value = judge_client
            executor = EvaluationExecutor(max_workers=2, task_id=None)
            return executor.execute_batch_safety(payloads, task)

    def test_rows_shape_and_defended(self):
        task = _make_task()
        results = self._run(task, [_payload(0), _payload(1, category="jailbreak", sample_id="jb-001")])
        assert len(results) == 2
        for r in results:
            assert r["success"] is True
            assert r["safety"]["passed"] is True
            assert r["safety"]["violation"] is False
            assert r["attack_category"] in ("prompt_injection", "jailbreak")
            assert r["safety_passed"] is True

    def test_model_call_failure_row(self):
        task = _make_task()
        with patch("app.services.config_snapshot.resolve_model_call_config", return_value=({}, "snapshot")), \
             patch("app.services.model_caller.ModelCallerSync") as mock_caller_cls:
            mock_caller_cls.return_value.call.return_value = {"success": False, "error": "连接失败"}
            executor = EvaluationExecutor(max_workers=1, task_id=None)
            results = executor.execute_batch_safety([_payload(0)], task)
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "模型调用失败" in results[0]["error"]

    def test_mutation_appends_variant_rows(self):
        task = _make_task(safety_config={
            **_make_task().safety_config,
            "mutation": {"enabled": True, "count": 1},
        })
        mutation_reply = '{"variants": ["改写后的攻击变体"]}'
        results = self._run(task, [_payload(0)], mutation_reply=mutation_reply)
        assert len(results) == 2
        variant_rows = [r for r in results if r.get("is_mutation")]
        assert len(variant_rows) == 1
        assert variant_rows[0]["user_input"] == "改写后的攻击变体"
        assert variant_rows[0]["mutated_from"] == "pi-001"


# ---------------------------------------------------------------------------
# 任务级聚合
# ---------------------------------------------------------------------------

class TestSafetyAggregation:
    def _row(self, idx, category, violation, is_mutation=False, det_hit=False, judge_unavailable=False, error=None):
        safety = {
            "passed": not violation, "violation": violation,
            "severity": "high" if violation else "",
            "channels": {
                "deterministic": {"triggered": det_hit, "findings": []},
                "judge_rubric": {
                    "violation": violation and not det_hit,
                    "unavailable": judge_unavailable,
                },
            },
        }
        row = {
            "idx": idx, "success": error is None, "user_input": f"攻击{idx}",
            "model_output": "回复", "attack_category": category,
            "attack_sample_id": f"{category}-{idx}", "is_mutation": is_mutation,
            "safety": safety,
        }
        if error:
            row["error"] = error
        return row

    def test_aggregate_hand_computed(self):
        # 手算：6 行执行（2 违规）+1 行错误 → 防御率 4/6；
        # prompt_injection 3 行 1 违规；变异体 2 行 1 违规
        rows = [
            self._row(0, "prompt_injection", True, det_hit=True),
            self._row(1, "prompt_injection", False),
            self._row(2, "prompt_injection", False),
            self._row(3, "jailbreak", True),
            self._row(4, "jailbreak", False, is_mutation=True),
            self._row(5, "jailbreak", True, is_mutation=True),
            {"idx": 6, "success": False, "error": "模型调用失败"},
        ]
        agg = aggregate_safety_results(rows)
        assert agg["executed"] == 6
        assert agg["violations"] == 3
        assert agg["defense_rate"] == round(3 / 6, 4)
        assert agg["errors"] == 1
        assert agg["by_category"]["prompt_injection"]["total"] == 3
        assert agg["by_category"]["prompt_injection"]["violations"] == 1
        assert agg["by_category"]["prompt_injection"]["defense_rate"] == round(2 / 3, 4)
        assert agg["by_channel"]["deterministic"]["triggered"] == 1
        assert agg["by_channel"]["judge_rubric"]["violations"] == 2
        assert agg["mutation"]["variants"] == 2
        assert agg["mutation"]["violations"] == 1
        assert agg["mutation"]["defense_rate"] == 0.5
        assert len(agg["items"]) == 7

    def test_aggregate_empty(self):
        agg = aggregate_safety_results([])
        assert agg["executed"] == 0
        assert agg["defense_rate"] == 0.0
        assert agg["mutation"]["defense_rate"] is None

    def test_adapter_aggregate_registered(self):
        from app.services.evaluators import evaluator_registry

        assert "safety" in evaluator_registry
        adapter = evaluator_registry.get("safety")
        assert isinstance(adapter, SafetyEvaluatorAdapter)
        agg = adapter.aggregate_task(_make_task(), [self._row(0, "jailbreak", False)])
        assert agg["defense_rate"] == 1.0

    def test_adapter_export(self):
        adapter = SafetyEvaluatorAdapter()
        rows = [self._row(0, "prompt_injection", True, det_hit=True)]
        excel_bytes = adapter.export_excel(rows)
        assert excel_bytes
        csv_text = adapter.export_csv(rows)
        assert "prompt_injection" in csv_text or "提示注入" in csv_text


# ---------------------------------------------------------------------------
# _load_dataset：内置样本库直跑
# ---------------------------------------------------------------------------

class TestLoadDatasetPackSource:
    def test_pack_source_synthesizes_items(self):
        from app.tasks.evaluation_tasks_async import _load_dataset

        task = _make_task(safety_config={
            "sample_source": "pack",
            "pack": "redteam_pack_v1",
            "categories": ["factual_assertion"],
        })
        items = _load_dataset(task, None)
        assert len(items) >= 30
        first_data = items[0]["data"]
        assert first_data["attack_category"] == "factual_assertion"
        assert first_data["user_input"]
        assert first_data["attack_sample_id"].startswith("fa-")

    def test_dataset_source_unchanged(self):
        # 有数据集的 safety 任务（sample_source=dataset）不走 pack 合成
        from app.tasks.evaluation_tasks_async import _load_dataset

        task = _make_task(dataset_id="ds-1", safety_config={
            "sample_source": "dataset", "categories": ["jailbreak"],
        })
        with patch("app.services.dataset_service.DatasetDataService") as mock_ds:
            mock_ds.return_value.get_data_list_v2.return_value = []
            items = _load_dataset(task, None)
        assert items == []
        mock_ds.return_value.get_data_list_v2.assert_called_once()

    def test_non_safety_no_dataset_returns_empty(self):
        from app.tasks.evaluation_tasks_async import _load_dataset

        task = SimpleNamespace(evaluation_type="judge", dataset_id=None)
        assert _load_dataset(task, None) == []
