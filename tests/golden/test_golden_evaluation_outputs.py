"""W17 评测域 golden 门禁：重构前后对固定 fixture 的输出逐字节比对（评测结论零漂移）。

覆盖面（评测计划 §4 W17）：
- 聚合：ResultAggregator.aggregate_for_task 对 6 个评测类型（judge / consistency /
  success_rate / rag / binary_classification / latency）的聚合输出；
- 导出三分支：export_to_excel / export_to_csv / export_to_json，且各覆盖
  「行级结果」与「聚合结果+evaluation_type」（batch-export API 的生产入参形态）两种入参。

基线文件：tests/golden/baseline/（本测试与基线逐字节比对）。
fixture：tests/golden/fixtures/<type>_rows.json（行级结果）+ <type>_task.json（任务口径）。

确定性前提（conftest.frozen_clock）：
- result_aggregator / latency_evaluator 的 now()（completed_at、export_time、行 timestamp）
- openpyxl docProps created/modified（xlsx 字节内嵌时间戳）

重生成基线：GOLDEN_UPDATE=1 pytest tests/golden -q
（仅在口径有意变更时使用；MR 描述必须附差异说明。）
"""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# 注意：app.* 一律延迟到测试/夹具内部导入——本目录按字母序先于
# tests/integration 收集，模块级导入会在 integration conftest 解析
# DATABASE_URL 之前把 app.db.session（engine=None）缓存进 sys.modules，
# 破坏其临时库供给（基线 worktree 实证）。

GOLDEN_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = GOLDEN_DIR / "fixtures"
BASELINE_DIR = GOLDEN_DIR / "baseline"
UPDATE = os.environ.get("GOLDEN_UPDATE") == "1"

# 评测计划点名的 5 类 + latency（分发/聚合/导出同样存在 latency 分支，一并收敛门禁）
EVALUATION_TYPES = [
    "judge",
    "consistency",
    "success_rate",
    "rag",
    "binary_classification",
    "latency",
]


class _TaskStub:
    """任务口径桩：aggregate_for_task / ResultAggregator 均按属性访问任务字段"""

    def __init__(self, spec: dict):
        self.__dict__.update(spec)


def _load_json(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _make_task(eval_type: str) -> _TaskStub:
    return _TaskStub(_load_json(f"{eval_type}_task.json"))


def _load_rows(eval_type: str) -> list:
    return _load_json(f"{eval_type}_rows.json")


def _aggregate(eval_type: str):
    """生产同口径聚合：aggregate_for_task 单点（与 complete/stop 聚合共用）"""
    from app.services.result_aggregator import ResultAggregator

    task = _make_task(eval_type)
    rows = _load_rows(eval_type)
    return ResultAggregator.aggregate_for_task(task, rows)


def _task_shape_export_data(eval_type: str) -> list:
    """batch-export API 的生产入参形态：聚合结果 + evaluation_type 合并"""
    aggregated = _aggregate(eval_type)
    return [dict(aggregated, evaluation_type=eval_type)]


def _canon_json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _exporter():
    """延迟导入导出器（app.* 不在收集期导入，见模块头注释）"""
    from app.services.result_aggregator import ResultExporter

    return ResultExporter


def _assert_bytes_equal(baseline: Path, actual: bytes) -> None:
    if UPDATE:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(actual)
        return
    assert baseline.exists(), (
        f"golden 基线缺失: {baseline.name} —— 运行 "
        f"GOLDEN_UPDATE=1 pytest tests/golden -q 生成后再提交"
    )
    expected = baseline.read_bytes()
    if actual == expected:
        return
    detail = f"字节长度 期望={len(expected)} 实际={len(actual)}"
    if baseline.suffix in (".json", ".csv"):
        exp_lines = expected.decode("utf-8", "replace").splitlines()
        act_lines = actual.decode("utf-8", "replace").splitlines()
        diffs = [
            f"  - {e!r}\n  + {a!r}"
            for e, a in zip(exp_lines, act_lines)
            if e != a
        ][:10]
        detail += "\n" + "\n".join(diffs)
    pytest.fail(
        f"golden 漂移: {baseline.name}\n{detail}\n"
        f"评测结论出现非预期变化——若为有意变更，GOLDEN_UPDATE=1 重生成并在 MR 说明"
    )


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eval_type", EVALUATION_TYPES)
def test_golden_aggregate(frozen_clock, eval_type):
    """聚合输出逐字节存档（judge/rag 的 completed_at 为冻结时刻）"""
    aggregated = _aggregate(eval_type)
    _assert_bytes_equal(
        BASELINE_DIR / f"{eval_type}_aggregate.json", _canon_json_bytes(aggregated)
    )


# ---------------------------------------------------------------------------
# 导出三分支（excel / csv / json）× 两种入参形态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eval_type", EVALUATION_TYPES)
def test_golden_export_excel_task_shape(frozen_clock, eval_type):
    """excel 分支 × 聚合形态（batch-export API 生产入参）"""
    data = _task_shape_export_data(eval_type)
    out = _exporter().export_to_excel(data, eval_type)
    assert isinstance(out, bytes) and out
    _assert_bytes_equal(BASELINE_DIR / f"{eval_type}_export_excel_task.xlsx", out)


@pytest.mark.parametrize("eval_type", EVALUATION_TYPES)
def test_golden_export_excel_rows_shape(frozen_clock, eval_type):
    """excel 分支 × 行级形态"""
    out = _exporter().export_to_excel(_load_rows(eval_type), eval_type)
    assert isinstance(out, bytes) and out
    _assert_bytes_equal(BASELINE_DIR / f"{eval_type}_export_excel_rows.xlsx", out)


@pytest.mark.parametrize("eval_type", EVALUATION_TYPES)
def test_golden_export_csv_task_shape(frozen_clock, eval_type):
    """csv 分支 × 聚合形态（batch-export API 生产入参）"""
    data = _task_shape_export_data(eval_type)
    out = _exporter().export_to_csv(data, eval_type)
    assert isinstance(out, str) and out
    _assert_bytes_equal(
        BASELINE_DIR / f"{eval_type}_export_csv_task.csv", out.encode("utf-8")
    )


@pytest.mark.parametrize("eval_type", EVALUATION_TYPES)
def test_golden_export_csv_rows_shape(frozen_clock, eval_type):
    """csv 分支 × 行级形态"""
    out = _exporter().export_to_csv(_load_rows(eval_type), eval_type)
    assert isinstance(out, str) and out
    _assert_bytes_equal(
        BASELINE_DIR / f"{eval_type}_export_csv_rows.csv", out.encode("utf-8")
    )


@pytest.mark.parametrize("eval_type", EVALUATION_TYPES)
def test_golden_export_json(frozen_clock, eval_type):
    """json 导出（export_time 为冻结时刻）"""
    data = _task_shape_export_data(eval_type)
    out = _exporter().export_to_json(data, eval_type)
    assert isinstance(out, str) and out
    _assert_bytes_equal(BASELINE_DIR / f"{eval_type}_export_json.json", out.encode("utf-8"))


# ---------------------------------------------------------------------------
# 门禁自校验：冻结时钟下 xlsx 生成必须字节确定（否则 golden 基线失效）
# ---------------------------------------------------------------------------


def test_excel_generation_is_byte_deterministic(frozen_clock):
    """同一冻结时钟下两次生成 xlsx 必须逐字节一致（openpyxl 时间戳内嵌）"""
    data = _task_shape_export_data("binary_classification")
    first = _exporter().export_to_excel(data, "binary_classification")
    second = _exporter().export_to_excel(data, "binary_classification")
    assert first == second
