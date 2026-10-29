"""W2 评分口径语义测试（P0-B）

对齐《整改开发计划-20261013》§2 W2「测试设计」：
- 6 裁判 1 失败 → avg = 5 个成功裁判均值（旧口径会偏低），failed_judge_count==1；
- 6 裁判全失败 → 该行 status=judge_failed、均分 None、不进任务均值；
- 维度分均值与总分用同一批裁判（1 失败 + 2 含维度 → 两者分母一致）；
- empty_input 行与 judge_failed 行在聚合里分别可辨。

另覆盖 W2-1 失败标记冒泡（真实 LangGraph 裁判图链路，mock 裁判 LLM）：
- 维度解析失败 → 裁判结果 judge_failed=True + fail_reason=parse_failed；
- 裁判 LLM 异常（超时，经 W2-3 重试耗尽）→ score=None + judge_failed=True + fail_reason=llm_error；
- 正常裁判 raw_output 保持旧 JSON 形态（不携带 judge_failed 字段）；
- run_parallel 混合成功/失败裁判的聚合行为。

旧口径对照：参照 657fc49 基线的 _aggregate_judge_scores 逻辑（失败裁判计 0 分入均分），
以 legacy_aggregate() 内联复现，仅用于断言新口径与旧口径的差异方向，不参与生产代码。
"""
import json
import re
import threading
import time

import pytest

from app.services.evaluation_executor import EvaluationExecutor
from app.services.judge_graph_state import build_state_input
from app.services.judge_langgraph_agent import JudgeLangGraphAgent
from app.services.model_caller import ModelCallerSync
from app.services.result_aggregator import ResultAggregator

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _failed_judge(reason="llm_error", feedback="裁判执行失败: mock"):
    """构造一个失败裁判结果（W2-1 新口径：score=None + 显式标记）"""
    return {
        "score": None,
        "feedback": feedback,
        "dimension_scores": {},
        "judge_failed": True,
        "fail_reason": reason,
    }


def _ok_judge(score, dims=None, feedback="好"):
    return {
        "score": score,
        "feedback": feedback,
        "dimension_scores": dims or {},
    }


def legacy_aggregate(judge_scores):
    """657fc49 基线旧口径聚合（对照用，非生产代码）

    总分：所有裁判（含失败裁判的 0 分）计入均值；
    维度分：仅对携带该维度分数的裁判取平均（与总分分母不一致——旧口径矛盾点）。
    """
    total = sum(s.get("score", 0) or 0 for s in judge_scores)
    avg = total / len(judge_scores)
    dim_accum = {}
    for s in judge_scores:
        for dim_name, dim_score in (s.get("dimension_scores") or {}).items():
            if isinstance(dim_score, (int, float)):
                dim_accum.setdefault(dim_name, []).append(float(dim_score))
    dims = {dim: sum(vals) / len(vals) for dim, vals in dim_accum.items()}
    return avg, dims


# ===========================================================================
# W2-2 聚合口径（_aggregate_judge_scores / ResultAggregator.aggregate）
# ===========================================================================
class TestAggregateJudgeScores:
    def test_six_judges_one_failed_average_excludes_failed(self):
        """6 裁判 1 失败 → 均值只含 5 个成功裁判，failed_judge_count==1"""
        judge_scores = [_ok_judge(8, {"A": 8, "B": 6}) for _ in range(5)] + [_failed_judge()]

        result = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, judge_scores)

        # 新口径：5 个 8 分裁判的均值 = 8.0
        assert result["average_score"] == 8.0
        assert result["failed_judge_count"] == 1
        assert result["judge_count"] == 6
        assert result["row_status"] == "completed"

        # 对照旧口径：失败裁判计 0 分入均分 → (5*8+0)/6 ≈ 6.6667（被拉低）
        legacy_avg, _ = legacy_aggregate(judge_scores)
        assert legacy_avg == pytest.approx(6.666667, abs=1e-5)
        assert result["average_score"] > legacy_avg

        # 维度分同样只吃 valid 裁判：A 均值 8（不是 8*5/6）
        assert result["dimension_scores"]["A"] == 8.0

    def test_all_six_judges_failed_marks_row_judge_failed(self):
        """6 裁判全失败 → 行级 average_score=None、row_status=judge_failed"""
        judge_scores = [_failed_judge() for _ in range(6)]

        result = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, judge_scores)

        assert result["average_score"] is None
        assert result["row_status"] == "judge_failed"
        assert result["failed_judge_count"] == 6
        assert result["judge_count"] == 6
        # 失败裁判的 feedback 保留（便于定位失败原因）
        assert result["feedback"].count("裁判执行失败") == 6

    def test_total_and_dimension_scores_share_same_denominator(self):
        """1 失败 + 2 含维度 → 总分与维度分用同一批裁判（分母一致）

        裁判总分取维度等权均值保持数据自洽（J1: (8+6)/2=7, J2: (4+8)/2=6），
        使得「总分 = (维度A均值 + 维度B均值)/2」这一分母一致性断言可精确成立。
        """
        judge_scores = [
            _ok_judge(7, {"A": 8, "B": 6}),
            _ok_judge(6, {"A": 4, "B": 8}),
            _failed_judge(),
        ]

        result = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, judge_scores)

        # 总分 = (7+6)/2 = 6.5，分母 2（valid 裁判数）
        assert result["average_score"] == 6.5
        # 维度 A = (8+4)/2 = 6.0，维度 B = (6+8)/2 = 7.0 —— 与总分同分母 2
        assert result["dimension_scores"]["A"] == 6.0
        assert result["dimension_scores"]["B"] == 7.0

        # 对照旧口径：总分分母 3（(7+6+0)/3≈4.33），维度分分母 2（A=6.0）→ 自相矛盾
        legacy_avg, legacy_dims = legacy_aggregate(judge_scores)
        assert legacy_avg == pytest.approx(13 / 3, abs=1e-9)
        assert legacy_dims["A"] == 6.0
        assert legacy_avg != pytest.approx(legacy_dims["A"], abs=1e-3)  # 旧口径总分≠维度分口径

        # 新口径下两者同分母：总分 = (维度A均值 + 维度B均值)/2（等权两维度）
        assert result["average_score"] == pytest.approx(
            (result["dimension_scores"]["A"] + result["dimension_scores"]["B"]) / 2
        )

    def test_all_valid_judges_keep_legacy_values(self):
        """无失败裁判 → 数值与旧口径一致（基础回归）"""
        judge_scores = [_ok_judge(8, {"A": 8, "B": 6}), _ok_judge(6, {"A": 4})]

        result = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, judge_scores)

        legacy_avg, legacy_dims = legacy_aggregate(judge_scores)
        assert result["average_score"] == 7
        assert result["average_score"] == pytest.approx(legacy_avg)
        assert result["dimension_scores"] == legacy_dims
        assert result["failed_judge_count"] == 0
        assert result["row_status"] == "completed"

    def test_aggregate_statistics_fields_present(self):
        judge_scores = [_ok_judge(5), _ok_judge(7), _failed_judge()]
        result = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, judge_scores)
        # result JSON 增量字段（只增不删）
        assert result["judge_count"] == 3
        assert result["failed_judge_count"] == 1


class TestResultAggregatorTaskLevel:
    """任务级聚合：judge_failed 行不进任务均值，empty_input/judge_failed 可辨"""

    @staticmethod
    def _task():
        return type("Task", (), {
            "max_score": 10,
            "evaluation_metrics": [{"name": "A", "weight": 1}, {"name": "B", "weight": 1}],
        })()

    @staticmethod
    def _normal_row(idx, avg, dims):
        return {
            "idx": idx, "success": True, "average_score": avg,
            "row_status": "completed", "judge_count": 6, "failed_judge_count": 0,
            "judge_scores": [_ok_judge(avg, dims)],
            "dimension_scores": dims, "model_output": "out",
        }

    @staticmethod
    def _judge_failed_row(idx):
        return {
            "idx": idx, "success": True, "average_score": None,
            "row_status": "judge_failed", "judge_count": 6, "failed_judge_count": 6,
            "judge_scores": [_failed_judge()],
            "dimension_scores": {}, "model_output": "out",
        }

    @staticmethod
    def _empty_input_row(idx):
        # executor 空值校验分支的输出形态（evaluation_executor.py:680-692）；
        # W4 起空输入行 average_score=None（与 judge_failed 同口径，不进任务均分）
        return {
            "idx": idx, "success": True, "average_score": None,
            "row_status": "empty_input", "judge_count": 0, "failed_judge_count": 0,
            "judge_scores": [], "dimension_scores": {},
            "feedback": "待评测内容为空，无法进行有效评估", "model_output": "",
        }

    def test_all_failed_row_excluded_from_task_average(self):
        rows = [self._normal_row(0, 6.0, {"A": 6, "B": 6}), self._judge_failed_row(1)]

        agg = ResultAggregator(self._task()).aggregate(rows)

        # 任务均分只含正常行的 6.0（judge_failed 行被排除，而不是以 0 分拉低到 3.0）
        assert agg["average_score"] == 6.0
        assert agg["failed_judge_rows"] == 1
        assert agg["total"] == 2
        assert agg["success_count"] == 2

    def test_empty_input_and_judge_failed_rows_distinguishable(self):
        """三种无分行在聚合里分别可辨：真低分 / judge_failed / empty_input"""
        rows = [
            self._normal_row(0, 0.0, {"A": 0, "B": 0}),   # 真低分（全部裁判给了 0 分）
            self._empty_input_row(1),                     # 空输入（W4：不进均分）
            self._judge_failed_row(2),                    # 裁判全失败
        ]

        agg = ResultAggregator(self._task()).aggregate(rows)

        by_idx = {item["idx"]: item for item in agg["items"]}
        # 真低分行：completed + average_score=0（真 0 分，进均分）
        assert by_idx[0]["row_status"] == "completed"
        assert by_idx[0]["average_score"] == 0
        # 空输入行：empty_input + average_score 字段缺省（None 不落 JSON，W4）
        assert by_idx[1]["row_status"] == "empty_input"
        assert "average_score" not in by_idx[1]
        # 裁判全失败行：judge_failed + average_score 字段缺省（None 不落 JSON）
        assert by_idx[2]["row_status"] == "judge_failed"
        assert "average_score" not in by_idx[2]
        # 三行 row_status 互不相同 → 可辨
        assert len({by_idx[i]["row_status"] for i in range(3)}) == 3

        # 任务均分（W4）：只有真低分的 0 分计入，empty_input/judge_failed 均排除
        assert agg["average_score"] == 0.0
        assert agg["failed_judge_rows"] == 1

    def test_judge_failed_and_empty_input_rows_excluded(self):
        """empty_input 的 None 不进任务均分（W4 与 judge_failed 同口径）"""
        rows = [self._empty_input_row(0), self._judge_failed_row(1)]

        agg = ResultAggregator(self._task()).aggregate(rows)
        # 两行均排除 → 无有效分，均分回退 0（分母为 0 而非被 None 拉低）
        assert agg["average_score"] == 0.0
        assert agg["failed_judge_rows"] == 1

        # 混合行进一步验证排除语义：
        rows2 = [self._normal_row(0, 8.0, {"A": 8, "B": 8}), self._empty_input_row(1),
                 self._judge_failed_row(2)]
        agg2 = ResultAggregator(self._task()).aggregate(rows2)
        # 8.0 / 1 = 8.0：empty_input 与 judge_failed 均排除（W2 旧口径为 (8+0)/2=4.0）
        assert agg2["average_score"] == 8.0
        assert agg2["failed_judge_rows"] == 1

    def test_items_judge_details_carry_failure_flags(self):
        """items[].judge_details 透传 judge_failed / fail_reason（result JSON 失败可见）"""
        rows = [{
            "idx": 0, "success": True, "average_score": 8.0,
            "row_status": "completed", "judge_count": 2, "failed_judge_count": 1,
            "judge_scores": [
                {"score": 8, "feedback": "好", "dimension_scores": {"A": 8}},
                _failed_judge("llm_error", "裁判执行失败: timeout"),
            ],
            "dimension_scores": {"A": 8.0}, "model_output": "out",
        }]

        agg = ResultAggregator(self._task()).aggregate(rows)

        details = agg["items"][0]["judge_details"]
        assert len(details) == 2
        assert details[0]["judge_failed"] is False
        assert details[0]["fail_reason"] is None
        assert details[1]["judge_failed"] is True
        assert details[1]["fail_reason"] == "llm_error"
        assert agg["items"][0]["failed_judge_count"] == 1


class TestExecutorEmptyInputBranch:
    """executor 空值校验分支显式标记 row_status=empty_input（evaluation_executor.py:680-692）"""

    def test_empty_model_output_marks_row_empty_input(self, monkeypatch):
        task = type("Task", (), {
            "evaluation_type": "judge",
            "model_config": {"provider": "http", "model_type": "mock", "api_key": "k"},
            "model_provider": None,
        })()
        monkeypatch.setattr(
            ModelCallerSync, "call", staticmethod(
                lambda *a, **k: {"success": True, "content": "   "}))

        executor = EvaluationExecutor()
        result = executor._evaluate_single({"idx": 3, "user_input": "问题"}, task)

        assert result["success"] is True
        # W4：空输入行 average_score=None（与 judge_failed 同口径，不进任务均分）
        assert result["average_score"] is None
        assert result["row_status"] == "empty_input"
        assert result["judge_count"] == 0
        assert result["failed_judge_count"] == 0
        assert "待评测内容为空" in result["feedback"]


# ===========================================================================
# W2-1 失败标记冒泡（真实 LangGraph 裁判图链路）
# ===========================================================================
class _JudgeLLMRouter:
    """mock ModelCallerSync._call_http：按 prompt 特征分派 analyze / 维度评分。

    - analyze prompt（「全面分析以下待评测内容」）→ 分配 thread-local 裁判号；
    - score prompt（「进行独立评分」）→ 按维度名返回打分表 / 非 JSON / 抛超时。
    经 ModelCallerSync.call 分发入口调用，保留 W2-3 重试链路（sleep 需外部 patch）。

    失败注入点设计（规避 mock 计数在重试下漂移）：
    - fail_all：所有调用抛超时（单裁判全失败场景——analyze 首调即败，无计数依赖）；
    - fail_judges：仅注入到该裁判的「维度评分」调用（analyze 正常返回，
      thread-local 裁判号已分配，重试 3 次判定稳定）。
    """

    ANALYZE_MARK = "全面分析以下待评测内容"
    DIM_NAME_RE = re.compile(r"名称: (\S+)")

    def __init__(self, fail_judges=(), parse_fail_dims=(), scores=None, fail_all=False):
        self.fail_judges = set(fail_judges)           # 这些裁判的维度评分调用抛超时
        self.parse_fail_dims = dict(parse_fail_dims)  # {裁判号: [维度名]} 返回非 JSON
        self.scores = scores or {}                    # {维度名: 分数}
        self.fail_all = fail_all                      # 全部调用抛超时
        self.local = threading.local()
        self.lock = threading.Lock()
        self.judge_counter = 0
        self.calls = {"n": 0}

    def __call__(self, messages, config, runtime_variables):
        self.calls["n"] += 1
        prompt = messages[-1]["content"]

        if self.fail_all:
            raise TimeoutError("mock judge llm read timeout")

        is_analyze = self.ANALYZE_MARK in prompt
        if is_analyze:
            with self.lock:
                self.judge_counter += 1
                judge_no = self.judge_counter - 1
            self.local.judge_no = judge_no
            # fail_judges 不注入 analyze：analyze 重试会重新分配裁判号导致计数漂移
        else:
            judge_no = getattr(self.local, "judge_no", 0)
            if judge_no in self.fail_judges:
                raise TimeoutError("mock judge llm read timeout")

        m = self.DIM_NAME_RE.search(prompt)
        dim = m.group(1) if m else None
        if dim is not None:
            if dim in self.parse_fail_dims.get(judge_no, []):
                return {"success": True, "content": "这不是 JSON 格式的评分回复", "raw": {}}
            score = self.scores.get(dim, 8)
            return {
                "success": True,
                "content": json.dumps({"score": score, "reasoning": f"{dim} 打分"}),
                "raw": {},
            }
        return {"success": True, "content": "整体分析文本", "raw": {}}


def _judge_task(task_id):
    return type("Task", (), {
        "id": task_id,
        "scene_desc": "W2 评分口径测试",
        "evaluation_metrics": [
            {"name": "维度A", "description": "A", "weight": 1},
            {"name": "维度B", "description": "B", "weight": 1},
        ],
        "max_score": 10,
        "score_details": "",
        "judge_prompt": "",
        "judge_model_config": {"provider": "http", "model_type": "mock", "api_key": "k"},
    })()


@pytest.fixture()
def judge_graph_env(monkeypatch):
    """隔离 graph 类级缓存 + mock 裁判 LLM provider 层（保留 model_caller 重试链路）"""
    JudgeLangGraphAgent.clear_cache()
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    yield sleeps
    JudgeLangGraphAgent.clear_cache()


class TestJudgeFailureBubbling:
    def test_parse_failure_bubbles_to_judge_failed_flag(self, monkeypatch, judge_graph_env):
        """维度评分返回非 JSON → 裁判结果 judge_failed=True + fail_reason=parse_failed"""
        router = _JudgeLLMRouter(parse_fail_dims={0: ["维度A"]}, scores={"维度A": 8, "维度B": 6})
        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(router))

        task = _judge_task("w2_sem_parse_fail")
        state = build_state_input(task, resolved_targets_content="待评测内容")
        result = JudgeLangGraphAgent(task).run(state, judge_index=0)

        assert result["judge_failed"] is True
        assert result["fail_reason"] == "parse_failed"
        # 解析失败维度以 0 分计入加权（(0+6)/2=3.0），但整裁判被标记失败、聚合时剔除
        assert result["score"] == pytest.approx(3.0)

    def test_llm_timeout_marks_judge_failed_llm_error(self, monkeypatch, judge_graph_env):
        """裁判 LLM 超时（经 W2-3 重试耗尽）→ score=None + judge_failed + llm_error"""
        router = _JudgeLLMRouter(fail_all=True)
        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(router))

        task = _judge_task("w2_sem_llm_error")
        state = build_state_input(task, resolved_targets_content="待评测内容")
        result = JudgeLangGraphAgent(task).run(state, judge_index=0)

        assert result["score"] is None
        assert result["judge_failed"] is True
        assert result["fail_reason"] == "llm_error"
        # 重试链路：analyze 首调即超时，经分发入口退避重试 2 次（1+2=3 次尝试）
        assert router.calls["n"] == 3
        assert judge_graph_env == [2.0, 8.0]

    def test_normal_judge_result_keeps_legacy_shape(self, monkeypatch, judge_graph_env):
        """正常裁判 → 结果不携带 judge_failed 字段（JSON 保持旧形态），分数正常"""
        router = _JudgeLLMRouter(scores={"维度A": 8, "维度B": 6})
        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(router))

        task = _judge_task("w2_sem_normal")
        state = build_state_input(task, resolved_targets_content="待评测内容")
        result = JudgeLangGraphAgent(task).run(state, judge_index=0)

        assert "judge_failed" not in result
        assert "fail_reason" not in result
        assert result["score"] == pytest.approx(7.0)
        assert result["dimension_scores"] == {"维度A": 8.0, "维度B": 6.0}

    def test_run_parallel_one_failed_others_valid(self, monkeypatch, judge_graph_env):
        """6 裁判并行（1 个超时）→ 恰好 1 个 judge_failed、5 个正常，均值只含正常裁判"""
        # 失败注入到裁判 3 的维度评分调用（analyze 正常，thread-local 裁判号分配稳定）
        router = _JudgeLLMRouter(fail_judges={3}, scores={"维度A": 8, "维度B": 8})
        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(router))

        task = _judge_task("w2_sem_parallel")
        state = build_state_input(task, resolved_targets_content="待评测内容")
        results = JudgeLangGraphAgent(task).run_parallel(state, judge_count=6)

        assert len(results) == 6
        failed = [r for r in results if r.get("judge_failed")]
        valid = [r for r in results if not r.get("judge_failed")]
        assert len(failed) == 1
        assert len(valid) == 5
        assert failed[0]["score"] is None
        assert failed[0]["fail_reason"] == "llm_error"
        # 5 个正常裁判全部 8 分 → 均值 8.0（旧口径为 8*5/6 ≈ 6.67）
        assert sum(r["score"] for r in valid) / len(valid) == pytest.approx(8.0)

        # 端到端聚合：行级 failed_judge_count==1、均值只含 valid
        row = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, results)
        assert row["average_score"] == pytest.approx(8.0)
        assert row["failed_judge_count"] == 1
        assert row["judge_count"] == 6

    def test_row_with_one_parse_failed_judge_aggregates_valid_only(
            self, monkeypatch, judge_graph_env):
        """1 正常 + 1 解析失败裁判 → 行均分只含正常裁判（旧口径为 (7+0)/2=3.5）"""
        # 裁判 0 维度A 解析失败；裁判 1 正常（A=6, B=8 → score 7.0）
        router = _JudgeLLMRouter(parse_fail_dims={0: ["维度A"]},
                                 scores={"维度A": 6, "维度B": 8})
        monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(router))

        task = _judge_task("w2_sem_mixed_parse")
        state = build_state_input(task, resolved_targets_content="待评测内容")
        results = JudgeLangGraphAgent(task).run_parallel(state, judge_count=2)

        assert len(results) == 2
        failed = [r for r in results if r.get("judge_failed")]
        assert len(failed) == 1
        assert failed[0]["fail_reason"] == "parse_failed"

        row = EvaluationExecutor._aggregate_judge_scores({"idx": 0}, results)
        # 新口径：只含正常裁判的 7.0；旧口径 (7+0)/2=3.5
        assert row["average_score"] == pytest.approx(7.0)
        assert row["failed_judge_count"] == 1
