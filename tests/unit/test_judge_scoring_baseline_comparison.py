"""W2 对照基线测试：固定 20 行 × 6 裁判，新旧评分口径逐行对照（P0-B）

对齐《整改开发计划-20261013》§2 W2「测试设计」对照基线项：
  固定 20 行 fixture × 6 裁判（mock 裁判响应固定打分表，注入 1 行裁判超时），
  新旧口径各跑一遍，断言新口径分数只在该行不同且更高；
  无失败裁判的行分数与旧口径完全一致（M1 出口门禁 §6.2 的判定表预期）。

链路为真实生产路径（仅 mock 裁判 LLM 的 HTTP provider 层）：
  build_state_input → JudgeLangGraphAgent.run_parallel（LangGraph 图，6 裁判并发）
  → EvaluationExecutor._aggregate_judge_scores → ResultAggregator.aggregate

旧口径参照（legacy_* 函数）按 657fc49 基线实现内联复现：
  失败裁判以 0 分入均分、维度分只对携带维度分数的裁判取平均。

打分表设计（保证新旧口径浮点可比性）：
  行 i 的维度 A 基准 base_a = 4.0 + (i % 5) * 0.5，维度 B 基准 base_b = 3.0 + (i % 7) * 0.5
  （基准 ∈ [3.0, 6.0]，叠加裁判偏移 ±2 后不触发 max_score=10 / 0 分钳制）；
  6 个裁判的维度分在基准上叠加 delta = [+2, +1, 0, 0, -1, -2]（Σ=0），
  因此无失败行的 6 裁判均值恰为半整数（二进制浮点精确），round(…, 4) 无损，
  新旧口径可作精确相等（==）断言，不受 round4 精度收敛影响。
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

N_ROWS = 20
N_JUDGES = 6
FAIL_ROW = 7       # 注入裁判超时的行（W2BL-R07）
FAIL_JUDGE = 2     # 该行内失败的裁判号（其维度评分调用恒抛超时，经 W2-3 重试耗尽）

# 6 个裁判的维度分在行基准上的偏移（Σ=0 → 无失败行的行均值恒等于基准）
_DELTA = [2, 1, 0, 0, -1, -2]


def _row_base(row: int):
    # 基准范围控制在 [3.0, 6.0]：叠加 delta(±2) 后不触发 max_score=10 / 0 分截断
    # （judge_graph_nodes.score_dimension_node 的 min(score, max_score) 钳制）
    base_a = 4.0 + (row % 5) * 0.5
    base_b = 3.0 + (row % 7) * 0.5
    return base_a, base_b


def _judge_dim_score(row: int, judge: int, dim: str) -> float:
    """固定打分表：裁判 judge 对行 row 的维度 dim 的分数"""
    base_a, base_b = _row_base(row)
    if dim == "维度A":
        return base_a + _DELTA[judge]
    return base_b + _DELTA[judge]


class _BaselineJudgeLLM:
    """mock ModelCallerSync._call_http：固定打分表 + 1 行 1 裁判超时注入。

    prompt 中携带行标记（W2BL-R07）与维度名（「名称: 维度A」）；
    analyze prompt 分配 thread-local 裁判号（重试只发生在维度评分调用上，
    thread-local 裁判号在重试间保持稳定）。
    """

    ROW_RE = re.compile(r"W2BL-R(\d{2})")
    DIM_NAME_RE = re.compile(r"名称: (\S+)")
    ANALYZE_MARK = "全面分析以下待评测内容"

    def __init__(self):
        self.local = threading.local()
        self.lock = threading.Lock()
        self.row_judge_counter = {}  # {row: 该行已开启的裁判数}
        self.llm_calls = 0
        self.retry_waits = []

    def __call__(self, messages, config, runtime_variables):
        self.llm_calls += 1
        prompt = messages[-1]["content"]
        row_m = self.ROW_RE.search(prompt)
        row = int(row_m.group(1)) if row_m else 0

        if self.ANALYZE_MARK in prompt:
            # 行内裁判号：每行独立从 0 计数（analyze 标志新裁判开始）
            with self.lock:
                judge_no = self.row_judge_counter.get(row, 0)
                self.row_judge_counter[row] = judge_no + 1
            self.local.judge_no = judge_no
            self.local.row = row
            return {"success": True, "content": "整体分析文本", "raw": {}}

        judge_no = getattr(self.local, "judge_no", 0)
        dim_m = self.DIM_NAME_RE.search(prompt)
        dim = dim_m.group(1) if dim_m else None

        # 超时注入：FAIL_ROW 行的 FAIL_JUDGE 号裁判维度评分恒超时
        if row == FAIL_ROW and judge_no == FAIL_JUDGE:
            raise TimeoutError("mock baseline judge timeout")

        score = _judge_dim_score(row, judge_no, dim) if dim else 8
        return {
            "success": True,
            "content": json.dumps({"score": score, "reasoning": f"{dim} 打分"}),
            "raw": {},
        }


# ---------------------------------------------------------------------------
# 旧口径参照（657fc49 基线逻辑内联复现，仅测试用）
# ---------------------------------------------------------------------------
def legacy_aggregate_row(judge_scores):
    """旧口径行级聚合：总分含失败裁判的 0 分；维度分只收携带维度分数的裁判"""
    total = sum(s.get("score", 0) or 0 for s in judge_scores)
    avg = total / len(judge_scores)
    dim_accum = {}
    for s in judge_scores:
        for dim_name, dim_score in (s.get("dimension_scores") or {}).items():
            if isinstance(dim_score, (int, float)):
                dim_accum.setdefault(dim_name, []).append(float(dim_score))
    dims = {dim: sum(vals) / len(vals) for dim, vals in dim_accum.items()}
    return avg, dims


def legacy_task_average(row_avgs):
    """旧口径任务均分：全部行（含失败裁判的行，其行均分已被 0 分拉低）取平均 + round2"""
    return round(sum(row_avgs) / len(row_avgs), 2)


@pytest.fixture()
def baseline_env(monkeypatch):
    JudgeLangGraphAgent.clear_cache()
    router = _BaselineJudgeLLM()
    monkeypatch.setattr(ModelCallerSync, "_call_http", staticmethod(router))
    # 退避等待记录器：patch 全局 time.sleep 时必须保留真实睡眠——前序用例
    # 遗留的后台线程（如 pubsub 监听线程的重连 sleep(1) 轮询）一旦被换成
    # no-op 记录器，会以 CPU 速度空转，把 waits 撑到千万级（GIL 饥饿拖慢
    # 全部并发裁判 + 断言 pformat 耗数分钟，CI 上表现为整段无输出直至超时）。
    waits = []
    _real_sleep = time.sleep

    def _recording_sleep(seconds):
        waits.append(seconds)
        _real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", _recording_sleep)
    yield router, waits
    JudgeLangGraphAgent.clear_cache()


def _baseline_task():
    return type("Task", (), {
        "id": "w2_baseline_task",
        "scene_desc": "W2 对照基线",
        "evaluation_metrics": [
            {"name": "维度A", "description": "A", "weight": 1},
            {"name": "维度B", "description": "B", "weight": 1},
        ],
        "max_score": 10,
        "score_details": "",
        "judge_prompt": "",
        "judge_model_config": {"provider": "http", "model_type": "mock", "api_key": "k"},
    })()


def _run_baseline():
    """跑 20 行 × 6 裁判全链路，返回（行级结果列表, 任务级聚合）"""
    task = _baseline_task()
    agent = JudgeLangGraphAgent(task)

    rows = []
    for i in range(N_ROWS):
        state = build_state_input(
            task, resolved_targets_content=f"W2BL-R{i:02d} 待评测内容文本")
        judge_scores = agent.run_parallel(state, judge_count=N_JUDGES)
        # 与 evaluation_executor._evaluate_single 的输出形态一致（judge_scores
        # 先写入 result 再聚合，executor.py:580-581）
        row = {"idx": i, "success": True, "model_output": f"输出{i}",
               "judge_scores": judge_scores}
        row = EvaluationExecutor._aggregate_judge_scores(row, judge_scores)
        rows.append(row)

    agg = ResultAggregator(task).aggregate(rows)
    return rows, agg


class TestJudgeScoringBaselineComparison:

    def test_baseline_20rows_6judges_new_vs_legacy(self, baseline_env):
        router, waits = baseline_env
        rows, agg = _run_baseline()

        assert len(rows) == N_ROWS

        # ── 行级：失败注入只影响 FAIL_ROW ──
        for row in rows:
            if row["idx"] == FAIL_ROW:
                assert row["failed_judge_count"] == 1
                assert row["judge_count"] == N_JUDGES
                failed = [s for s in row["judge_scores"] if s.get("judge_failed")]
                assert len(failed) == 1
                assert failed[0]["score"] is None
                assert failed[0]["fail_reason"] == "llm_error"
            else:
                assert row["failed_judge_count"] == 0
                assert row["row_status"] == "completed"

        # ── 逐行新旧口径对照 ──
        legacy_row_avgs = []
        for row in rows:
            legacy_avg, legacy_dims = legacy_aggregate_row(row["judge_scores"])
            legacy_row_avgs.append(legacy_avg)

            if row["idx"] == FAIL_ROW:
                # 注入行：新口径分数更高（失败裁判剔除出均分，旧口径以 0 分计入被拉低）
                base_a, base_b = _row_base(FAIL_ROW)
                expected_new = (base_a + base_b) / 2  # 5 个成功裁判的 delta 和为 0
                assert row["average_score"] == pytest.approx(expected_new, abs=1e-9)
                assert row["average_score"] > legacy_avg
                # 维度分：旧口径维度聚合本就不含失败裁判（其 dimension_scores 为空），
                # 新旧口径维度分一致——这正是旧口径「总分含失败/维度不含」的矛盾点，
                # 新口径将总分对齐到维度分的 valid 口径
                assert row["dimension_scores"]["维度A"] == pytest.approx(
                    legacy_dims["维度A"], abs=1e-9)
                assert row["dimension_scores"]["维度B"] == pytest.approx(
                    legacy_dims["维度B"], abs=1e-9)
            else:
                # 无失败裁判的行：行分与维度分与旧口径完全一致（精确相等）
                assert row["average_score"] == legacy_avg, (
                    f"row {row['idx']} 行分漂移: new={row['average_score']} "
                    f"legacy={legacy_avg}")
                for dim in ("维度A", "维度B"):
                    assert row["dimension_scores"][dim] == legacy_dims[dim], (
                        f"row {row['idx']} 维度 {dim} 漂移")

        # ── 任务级：failed_judge_rows 统计 + 新旧任务均分对照 ──
        # failed_judge_rows 只统计「全部裁判失败」的行（result_aggregator.py:152）；
        # 本场景 FAIL_ROW 仍有 5 个成功裁判（row_status=completed），计 0；
        # 全失败行 → failed_judge_rows==1 的场景在
        # test_judge_aggregate_semantics.py::test_all_failed_row_excluded_from_task_average 覆盖
        assert agg["failed_judge_rows"] == 0
        legacy_task = legacy_task_average(legacy_row_avgs)
        assert agg["average_score"] > legacy_task
        # 任务均分 = 20 行新行分的均值（FAIL_ROW 行以新口径进均分）
        expected_task = round(sum(r["average_score"] for r in rows) / N_ROWS, 2)
        assert agg["average_score"] == expected_task

        # ── 重试链路确实为注入裁判工作过：失败裁判的第一个维度评分节点（score_0）
        # 经 2 次退避重试（2s/8s）后耗尽，图在该节点中断 → 裁判标记 judge_failed ──
        # 全局 sleep 流会混入后台线程的轮询（pubsub 重连 sleep(1) 等），
        # 只断言注入裁判的退避子序列
        backoffs = [w for w in waits if w in (2.0, 8.0)]
        assert backoffs == [2.0, 8.0], (
            f"退避序列漂移: 前 20 项={waits[:20]}（共 {len(waits)} 项）"
        )

    def test_baseline_no_failure_rows_identical_to_legacy(self, baseline_env):
        """门禁 §6.2 判定表：无失败裁判的行分数与旧口径完全一致（单独固化）"""
        rows, agg = _run_baseline()

        drift = []
        for row in rows:
            if row["idx"] == FAIL_ROW:
                continue
            legacy_avg, legacy_dims = legacy_aggregate_row(row["judge_scores"])
            if row["average_score"] != legacy_avg or \
                    row["dimension_scores"] != legacy_dims:
                drift.append(row["idx"])
        assert drift == [], f"无失败裁判的行出现分数漂移: {drift}"

        # 19 个无失败行全部 completed 且可进任务均分
        assert all(r["row_status"] == "completed" for r in rows if r["idx"] != FAIL_ROW)
        assert sum(1 for r in rows if r.get("average_score") is not None) == N_ROWS
