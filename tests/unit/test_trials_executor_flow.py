"""judge 行级多 trial 执行链路与聚合测试（M2 QE2-3）

- trials>1（judge）：execute_batch 逐 trial 调 _evaluate_single 并合并为一行
  —— pass@1 / pass^k / 分数均值/标准差手算对照、trials_detail 完整、
  证据主体取首个成功 trial；
- trial 通过判定：配置校验器时以 verification_result.status==pass 为准
  （确定性口径），未配置时以「产出有效裁判分」为准；
- trials=1 / 非 judge 类型：直接单 trial（不多次调用、结果零变化）；
- 任务级聚合：aggregate() 对携带 pass_at_1 的行写 pass_k 块（旧行不写）；
- latency 聚合：同 idx 重复行（repeat_count 展开）→ pass_k 块手算对照。
"""
from unittest.mock import patch

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.result_aggregator import ResultAggregator

pytestmark = pytest.mark.unit


def _make_task(evaluation_type="judge", trials=None, verifiers=None):
    return type("Task", (), {
        "id": "task_trials",
        "evaluation_type": evaluation_type,
        "trials": trials,
        "verifiers": verifiers,
    })()


def _trial_result(idx, score, success=True, verification_status=None, error=None):
    result = {"idx": idx, "success": success, "model_output": f"out-{idx}"}
    if score is not None:
        result["average_score"] = score
        result["judge_scores"] = [{"score": score, "feedback": "ok"}]
    if verification_status:
        result["verification_result"] = {"status": verification_status, "results": []}
    if error:
        result["error"] = error
    return result


def _run_batch(task, payload_count=1):
    payloads = [{"idx": i, "user_input": f"q{i}"} for i in range(payload_count)]
    executor = EvaluationExecutor(max_workers=1, task_id="task_trials")
    return executor.execute_batch(payloads, task)


class TestJudgeTrialsExecution:
    def test_merge_hand_computed(self):
        # 手算：3 次 trial 分数 [8, 6, 7] 全成功 → 行分 = 7，
        # pass@1 = 1.0，pass^k = 1，std = 1，sem = 1/sqrt(3)
        task = _make_task(trials=3)
        scores = iter([(8, True), (6, True), (7, True)])
        with patch.object(
            EvaluationExecutor, "_evaluate_single",
            side_effect=lambda payload, t: _trial_result(payload["idx"], *next(scores)),
        ) as mock_single:
            results = _run_batch(task)

        assert mock_single.call_count == 3
        assert len(results) == 1
        merged = results[0]
        assert merged["trials_count"] == 3
        assert merged["average_score"] == 7
        assert merged["pass_at_1"] == 1.0
        assert merged["pass_pow_k"] == 1
        assert merged["score_std"] == 1.0
        assert merged["score_sem"] == round(1 / 3 ** 0.5, 4)
        assert merged["trial_scores"] == [8, 6, 7]
        assert [d["score"] for d in merged["trials_detail"]] == [8, 6, 7]

    def test_partial_failure_not_all_pass(self):
        # 手算：3 次 trial [成功(9), 失败, 成功(5)] → pass@1 = 2/3，
        # pass^k = 0；行分 = 成功 trial 均值 = 7；证据主体取首个成功 trial
        task = _make_task(trials=3)
        outcomes = iter([(9, True), (None, False), (5, True)])
        with patch.object(
            EvaluationExecutor, "_evaluate_single",
            side_effect=lambda payload, t: _trial_idx(payload["idx"], outcomes),
        ):
            results = _run_batch(task)
        merged = results[0]
        assert merged["pass_count"] == 2
        assert merged["pass_at_1"] == round(2 / 3, 4)
        assert merged["pass_pow_k"] == 0
        assert merged["average_score"] == 7  # (9+5)/2
        assert merged["model_output"] == "out-0"  # 首个成功 trial 的证据主体

    def test_verification_status_defines_trial_pass(self):
        # 配置校验器（trial 结果带 verification_result）时：裁判分有效但
        # 校验 fail 的 trial 不计通过 → pass@1 = 2/3、pass^k = 0
        task = _make_task(trials=3, verifiers=[{"type": "db_assert", "config": {}}])
        queue = [
            {"score": 9, "vs": "pass"},
            {"score": 8, "vs": "fail"},
            {"score": 7, "vs": "pass"},
        ]
        with patch.object(
            EvaluationExecutor, "_evaluate_single",
            side_effect=lambda payload, t: (
                lambda item: _trial_result(
                    payload["idx"], item["score"],
                    verification_status=item["vs"],
                )
            )(queue.pop(0)),
        ):
            results = _run_batch(task)
        merged = results[0]
        assert merged["pass_count"] == 2
        assert merged["pass_at_1"] == round(2 / 3, 4)
        assert merged["pass_pow_k"] == 0

    def test_single_trial_passthrough(self):
        # trials=None / 1：单 trial 直调，无多 trial 字段（行为与旧版一致）
        task = _make_task(trials=None)
        with patch.object(
            EvaluationExecutor, "_evaluate_single",
            return_value=_trial_result(0, 8),
        ) as mock_single:
            results = _run_batch(task)
        assert mock_single.call_count == 1
        assert "trials_count" not in results[0]
        assert results[0]["average_score"] == 8

    def test_non_judge_type_ignores_trials(self):
        # 非 judge 类型即便误配 trials>1 也恒单 trial（repeat_count 通道各自管）
        task = _make_task(evaluation_type="consistency", trials=5)
        with patch.object(
            EvaluationExecutor, "_evaluate_single",
            return_value=_trial_result(0, 8),
        ) as mock_single:
            results = _run_batch(task)
        assert mock_single.call_count == 1
        assert "trials_count" not in results[0]

    def test_stop_event_interrupts_after_current_trial(self):
        # 停止信号：跑完当前 trial 即止，pass^k 按实际 trial 数计算（不虚报）
        task = _make_task(trials=3)
        stop_event = __import__("threading").Event()
        executor = EvaluationExecutor(max_workers=1, task_id="task_trials", stop_event=stop_event)
        payloads = [{"idx": 0, "user_input": "q"}]
        # 第一次 trial 完成后置位停止信号
        original = EvaluationExecutor._evaluate_single

        def stop_after_first(payload, t):
            result = _trial_result(payload["idx"], 9)
            stop_event.set()
            return result

        with patch.object(EvaluationExecutor, "_evaluate_single", side_effect=stop_after_first):
            results = executor.execute_batch(payloads, task)
        merged = results[0]
        assert merged["trials_count"] == 1
        assert merged["pass_at_1"] == 1.0


def _trial_idx(idx, outcomes):
    score, success = next(outcomes)
    return _trial_result(idx, score, success=success, error=None if success else "模型调用失败")


class TestTrialsAggregation:
    def test_aggregate_writes_pass_k_block(self):
        # 手算：2 合并行 pass@1 = [1.0, 2/3]，pass^k = [1, 0]
        # → 平均 pass@1 = 5/6，平均 pass^k = 0.5
        results = [
            {"idx": 0, "success": True, "average_score": 8.0,
             "pass_at_1": 1.0, "pass_pow_k": 1, "trials_count": 3},
            {"idx": 1, "success": True, "average_score": 6.0,
             "pass_at_1": round(2 / 3, 4), "pass_pow_k": 0, "trials_count": 3},
        ]
        agg = ResultAggregator(task=type("T", (), {
            "evaluation_metrics": [], "max_score": 10})()).aggregate(results)
        assert agg["pass_k"]["rows"] == 2
        # 手算：(1.0 + 0.6667)/2 = 0.83335 → 4 位舍入约 0.8334
        assert agg["pass_k"]["average_pass_at_1"] == pytest.approx(5 / 6, abs=1e-4)
        assert agg["pass_k"]["average_pass_pow_k"] == 0.5
        assert agg["pass_k"]["all_pass_rows"] == 1

    def test_aggregate_legacy_rows_no_pass_k(self):
        results = [
            {"idx": 0, "success": True, "average_score": 8.0, "judge_scores": []},
        ]
        agg = ResultAggregator(task=type("T", (), {
            "evaluation_metrics": [], "max_score": 10})()).aggregate(results)
        assert "pass_k" not in agg

    def test_items_carry_trial_fields(self):
        results = [
            {"idx": 0, "success": True, "average_score": 7.0,
             "pass_at_1": 1.0, "pass_pow_k": 1, "trials_count": 2,
             "trial_scores": [6, 8], "trials_detail": [
                 {"trial": 1, "success": True, "score": 6, "error": None},
                 {"trial": 2, "success": True, "score": 8, "error": None},
             ]},
        ]
        agg = ResultAggregator(task=type("T", (), {
            "evaluation_metrics": [], "max_score": 10})()).aggregate(results)
        item = agg["items"][0]
        assert item["trials_count"] == 2
        assert item["pass_pow_k"] == 1
        assert item["trial_scores"] == [6, 8]
        assert len(item["trials_detail"]) == 2


class TestLatencyPassKAggregation:
    def test_repeated_sample_rows_hand_computed(self):
        # 手算：样本 0 三次重复 [过, 过, 败]（阈值 10）→ pass@1 = 2/3、
        # pass^k = 0；样本 1 三次全过 → pass@1 = 1、pass^k = 1
        # 任务级：平均 pass@1 = (2/3 + 1)/2 = 5/6，平均 pass^k = 0.5
        results = [
            {"idx": 0, "success": True, "duration": 5.0},
            {"idx": 0, "success": True, "duration": 8.0},
            {"idx": 0, "success": True, "duration": 12.0},
            {"idx": 1, "success": True, "duration": 3.0},
            {"idx": 1, "success": True, "duration": 4.0},
            {"idx": 1, "success": True, "duration": 5.0},
        ]
        agg = ResultAggregator.calculate_latency_metrics(results, threshold=10.0)
        assert agg["pass_k"]["trials"] == 3
        assert agg["pass_k"]["rows"] == 2
        assert agg["pass_k"]["average_pass_at_1"] == round(5 / 6, 4)
        assert agg["pass_k"]["average_pass_pow_k"] == 0.5
        assert agg["pass_k"]["all_pass_rows"] == 1

    def test_single_repeat_no_pass_k(self):
        # repeat_count=1（每样本一行，无重复 idx）：聚合结构不变
        results = [
            {"idx": 0, "success": True, "duration": 5.0},
            {"idx": 1, "success": True, "duration": 12.0},
        ]
        agg = ResultAggregator.calculate_latency_metrics(results, threshold=10.0)
        assert "pass_k" not in agg
