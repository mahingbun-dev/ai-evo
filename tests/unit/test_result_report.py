"""结果文档导出渲染测试（app/services/result_report.py）。

覆盖：
- build_markdown_export：judge 全量（多裁判 judge_details/dimension_feedbacks/
  超长 feedback 与 model_output 全文不截断）、consistency/rag/binary_classification/
  latency 的明细数组自适应、success_rate 无明细数组不出每题章节、
  failed/stopped 失败简报、头部元信息与配置摘要
- build_json_export：json roundtrip 无损（loads 后与库内 dict 深相等）
- export_filename：非法字符清洗、md/json 扩展名、completed_at 时间戳
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from app.services import result_report
from app.services.result_report import build_json_export, build_markdown_export, export_filename

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

LONG_TEXT = "这是一段超长裁判评语，必须全文不截断。" + "评" * 2000
LONG_MODEL_OUTPUT = "模型输出的长回答正文。" + "答" * 1500


def _record(**kwargs) -> SimpleNamespace:
    base = dict(
        id="exec_rec_1",
        execution_number=2,
        status="completed",
        result=None,
        total_cost=0.123,
        error_message=None,
        started_at=datetime(2026, 10, 16, 8, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 10, 16, 8, 5, 0, tzinfo=timezone.utc),
        config_snapshot={"model": "gpt-x", "temperature": 0.2},
        exported_files=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _task(**kwargs) -> SimpleNamespace:
    base = dict(id="task_x", name="冒烟任务", evaluation_type="judge")
    base.update(kwargs)
    return SimpleNamespace(**base)


def _judge_result() -> dict:
    return {
        "total": 3,
        "success_count": 2,
        "failed_count": 1,
        "success_rate": 2 / 3,
        "average_score": 8.33,
        "max_score": 10,
        "dimension_scores": {
            "准确性": {"average": 8.0, "weight": 1.0, "count": 3},
            "完整性": {"average": 8.67, "weight": 1.0, "count": 3},
        },
        "items": [
            {
                "idx": 0,
                "success": True,
                "row_status": "completed",
                "question": "第一题：什么是回归测试？",
                "ref_answer": "对已有功能重复执行验证的测试",
                "model_output": LONG_MODEL_OUTPUT,
                "judge_count": 2,
                "average_score": 8.5,
                "dimension_scores": {"准确性": 8.0, "完整性": 9.0},
                "judge_details": [
                    {
                        "evaluation_target_name": "裁判A",
                        "judge_index": 0,
                        "score": 9,
                        "feedback": LONG_TEXT,
                        "dimension_feedbacks": {"准确性": "理由充分", "完整性": "覆盖全面"},
                    },
                    {
                        "judge_index": 1,
                        "score": 8,
                        "feedback": "回答基本正确。",
                    },
                ],
            },
            {"idx": 2, "success": True, "row_status": "completed", "question": "第三题", "average_score": 9.0},
        ],
        "failed_items": [
            {
                "idx": 1,
                "success": False,
                "row_status": "failed",
                "question": "第二题",
                "error": "裁判调用超时（重试 3 次仍失败）",
            },
        ],
    }


# ── judge 全量渲染 ──────────────────────────────────────────────────


class TestJudgeMarkdown:
    def test_header_metadata(self):
        text = build_markdown_export(_record(), _task()).decode("utf-8")
        assert text.startswith("# 评测执行报告：冒烟任务")
        assert "- 任务 ID：`task_x`" in text
        assert "- 评测类型：judge" in text
        assert "- 执行序号：第 2 次" in text
        assert "- 状态：已完成" in text
        assert "- 开始时间：" in text
        assert "- 完成时间：" in text
        assert "- 耗时：0:05:00" in text
        assert "- 消耗成本：0.123" in text

    def test_summary_metrics_and_dimension_table(self):
        text = build_markdown_export(_record(result=_judge_result()), _task()).decode("utf-8")
        assert "## 汇总指标" in text
        assert "- success_rate：" in text
        assert "- average_score：8.33" in text
        # dimension_scores 表格
        assert "| 维度 | 平均分 | 权重 | 样本数 |" in text
        assert "| 准确性 | 8.0 | 1.0 | 3 |" in text
        assert "| 完整性 | 8.67 | 1.0 | 3 |" in text

    def test_config_summary_section(self):
        text = build_markdown_export(_record(), _task()).decode("utf-8")
        assert "## 配置摘要" in text
        assert "- model：gpt-x" in text
        assert "- temperature：0.2" in text

    def test_details_merged_and_sorted_by_idx(self):
        """items+failed_items 合并按 idx 排序：第 0 → 第 1（失败❌）→ 第 2 题。"""
        text = build_markdown_export(_record(result=_judge_result()), _task()).decode("utf-8")
        assert "## 每题明细（共 3 题）" in text
        i0 = text.index("### 第 0 题 ✅")
        i1 = text.index("### 第 1 题 ❌")
        i2 = text.index("### 第 2 题 ✅")
        assert i0 < i1 < i2
        assert "（failed）" in text  # 非 completed 的 row_status 进标题

    def test_judge_details_full_text_not_truncated(self):
        text = build_markdown_export(_record(result=_judge_result()), _task()).decode("utf-8")
        assert "**裁判评语：**" in text
        assert "- **裁判A：9 分**" in text
        assert LONG_TEXT in text  # 超长评语全文
        assert "- **裁判 1：8 分**" in text  # 无 name 用 judge_index 兜底
        assert "  - dimension_feedbacks：" in text

    def test_model_output_full_text_not_truncated(self):
        text = build_markdown_export(_record(result=_judge_result()), _task()).decode("utf-8")
        assert "**模型输出：**" in text
        assert LONG_MODEL_OUTPUT in text

    def test_input_fields_and_row_score(self):
        text = build_markdown_export(_record(result=_judge_result()), _task()).decode("utf-8")
        assert "**输入：**" in text
        assert "question：第一题：什么是回归测试？" in text
        assert "**行得分：8.5**" in text
        assert "**行级维度均分：**" in text

    def test_failed_row_error_block(self):
        text = build_markdown_export(_record(result=_judge_result()), _task()).decode("utf-8")
        assert "**错误：**" in text
        assert "裁判调用超时（重试 3 次仍失败）" in text


# ── 各评测类型明细数组自适应 ────────────────────────────────────────


class TestTypeAdaptiveDetails:
    def test_consistency_metrics_block_rendered(self):
        result = {
            "consistency_rate": 0.9,
            "metrics": {"pass_rate": 0.9, "std": 0.02},
            "items": [{"idx": 0, "success": True, "question": "Q"}],
        }
        text = build_markdown_export(_record(result=result), _task(evaluation_type="consistency")).decode("utf-8")
        assert "## 每题明细（共 1 题）" in text
        assert "**metrics**" in text  # 非明细数组/维度得分的 dict 进汇总 json 块
        assert '"pass_rate": 0.9' in text

    def test_rag_detail_results_rendered(self):
        result = {
            "hit_rate": 0.85,
            "detail_results": [
                {"idx": 1, "success": False, "question": "Q2", "retrieved": ["d3"]},
                {"idx": 0, "success": True, "question": "Q1", "retrieved": ["d1"]},
            ],
        }
        text = build_markdown_export(_record(result=result), _task(evaluation_type="rag")).decode("utf-8")
        assert "## 每题明细（共 2 题）" in text
        i0 = text.index("### 第 0 题 ✅")
        i1 = text.index("### 第 1 题 ❌")
        assert i0 < i1  # detail_results 也按 idx 排序

    def test_binary_classification_items_rendered(self):
        result = {
            "accuracy": 0.8,
            "items": [{"idx": 0, "success": True, "question": "Q", "feedback": "判断正确"}],
        }
        text = build_markdown_export(_record(result=result), _task(evaluation_type="binary_classification")).decode("utf-8")
        assert "## 每题明细（共 1 题）" in text
        assert "**评语：**" in text
        assert "判断正确" in text

    def test_latency_records_rendered(self):
        result = {
            "average_latency_ms": 120,
            "records": [{"idx": 0, "success": True, "question": "Q", "latency_ms": 120}],
        }
        text = build_markdown_export(_record(result=result), _task(evaluation_type="latency")).decode("utf-8")
        assert "## 每题明细（共 1 题）" in text
        assert "latency_ms：120" in text

    def test_success_rate_without_detail_array_no_details_section(self):
        result = {"success_rate": 0.95, "average_score": 8.0, "total": 100}
        text = build_markdown_export(_record(result=result), _task(evaluation_type="success_rate")).decode("utf-8")
        assert "## 汇总指标" in text
        assert "每题明细" not in text

    def test_feedback_long_string_not_truncated(self):
        """单裁判行（无 judge_details，只有 feedback 长文本）不截断。"""
        long_fb = "评" * 1300
        result = {"items": [{"idx": 0, "success": True, "question": "Q", "feedback": long_fb}]}
        text = build_markdown_export(_record(result=result), _task()).decode("utf-8")
        assert long_fb in text


# ── 失败/停止版本：失败简报 ─────────────────────────────────────────


class TestFailureBrief:
    def test_failed_version_renders_brief_only(self):
        record = _record(
            status="failed",
            error_message="裁判全部超时",
            result={"error": "judge timeout", "error_code": "JUDGE_TIMEOUT"},
        )
        text = build_markdown_export(record, _task()).decode("utf-8")
        assert "## 失败简报" in text
        assert "```text" in text
        assert "裁判全部超时" in text  # record.error_message 优先于 result.error
        # 不出正文章节
        assert "## 配置摘要" not in text
        assert "## 汇总指标" not in text
        assert "每题明细" not in text

    def test_failed_version_falls_back_to_result_error(self):
        record = _record(status="failed", error_message=None, result={"error": "来自 result 的错误"})
        text = build_markdown_export(record, _task()).decode("utf-8")
        assert "来自 result 的错误" in text

    def test_stopped_version_renders_brief(self):
        record = _record(status="stopped", error_message="人工停止", result={})
        text = build_markdown_export(record, _task()).decode("utf-8")
        assert "## 失败简报" in text
        assert "人工停止" in text

    def test_failed_with_detail_array_still_full_report(self):
        """failed 但 result 带明细数组（部分完成的批次）：仍出全量报告而非简报。"""
        record = _record(
            status="failed",
            error_message="第 3 题后中断",
            result={
                "average_score": 8.0,
                "items": [{"idx": 0, "success": True, "question": "Q", "average_score": 8.0}],
            },
        )
        text = build_markdown_export(record, _task()).decode("utf-8")
        assert "## 失败简报" not in text
        assert "## 每题明细（共 1 题）" in text


# ── json 无损导出 ──────────────────────────────────────────────────


class TestJsonExport:
    def test_roundtrip_lossless(self):
        record = _record(result=_judge_result())
        raw = build_json_export(record, _task())
        payload = json.loads(raw.decode("utf-8"))
        # loads 后与库内 dict 深相等（无损：不改结构、不转字符串、中文原样）
        assert payload["result"] == _judge_result()
        assert payload["config_snapshot"] == record.config_snapshot

    def test_outer_keys_contract(self):
        record = _record()
        payload = json.loads(build_json_export(record, _task()).decode("utf-8"))
        assert set(payload.keys()) == {
            "task_id", "task_name", "evaluation_type", "execution_number", "status",
            "started_at", "completed_at", "total_cost", "error_message",
            "config_snapshot", "result",
        }
        assert payload["task_id"] == "task_x"
        assert payload["task_name"] == "冒烟任务"
        assert payload["execution_number"] == 2
        assert payload["status"] == "completed"
        assert payload["total_cost"] == 0.123

    def test_datetime_serialized_isoformat(self):
        payload = json.loads(build_json_export(_record(), _task()).decode("utf-8"))
        assert payload["started_at"] == "2026-10-16T08:00:00+00:00"
        assert payload["completed_at"] == "2026-10-16T08:05:00+00:00"

    def test_chinese_not_escaped_and_utf8_bytes(self):
        record = _record(result={"note": "中文标点：「评语」"})
        raw = build_json_export(record, _task())
        assert isinstance(raw, bytes)
        assert "中文标点：「评语」".encode("utf-8") in raw  # ensure_ascii=False

    def test_failed_version_exportable(self):
        record = _record(status="failed", error_message="boom", result={"error": "boom"})
        payload = json.loads(build_json_export(record, _task()).decode("utf-8"))
        assert payload["status"] == "failed"
        assert payload["error_message"] == "boom"


# ── 文件名 ─────────────────────────────────────────────────────────


class TestExportFilename:
    def test_extension_mapping(self):
        name = export_filename("冒烟任务", 2, "markdown", completed_at=datetime(2026, 10, 16, 8, 30, 5, tzinfo=timezone.utc))
        assert name == "冒烟任务_exec2_20261016-083005.md"
        name = export_filename("冒烟任务", 2, "json", completed_at=datetime(2026, 10, 16, 8, 30, 5, tzinfo=timezone.utc))
        assert name.endswith(".json")

    def test_illegal_chars_sanitized(self):
        name = export_filename('a/b\\c:d*e?f"g<h>i|j', 1, "json")
        for ch in '\\/:*?"<>|':
            assert ch not in name
        assert name.startswith("b_c_d_e_f_g_h_i_j_exec1_")  # sanitize 先取 basename，"a/" 前缀被剥掉

    def test_completed_at_timestamp_used(self):
        name = export_filename("t", 3, "md", completed_at=datetime(2026, 10, 16, 8, 30, 5, tzinfo=timezone.utc))
        assert "20261016-083005" in name

    def test_empty_name_falls_back(self):
        name = export_filename("", 1, "md")
        assert name.startswith("task_exec1_")

    def test_path_separator_only_name(self):
        """纯路径分隔符的任务名：basename 后只剩后缀段，结果仍是合法文件名。"""
        name = export_filename("///", 1, "json")
        for ch in '\\/:*?"<>|':
            assert ch not in name
        assert name.startswith("_exec1_") and name.endswith(".json")

    def test_no_completed_at_uses_now(self):
        before = datetime.utcnow() - timedelta(seconds=5)
        name = export_filename("t", 1, "md")
        after = datetime.utcnow() + timedelta(seconds=5)
        ts = name.split("_exec1_")[1].rsplit(".", 1)[0]
        parsed = datetime.strptime(ts, "%Y%m%d-%H%M%S")
        assert before <= parsed <= after


def test_module_exports_documented_functions():
    """渲染入口与文件名函数是模块公开 API。"""
    assert callable(result_report.build_markdown_export)
    assert callable(result_report.build_json_export)
    assert callable(result_report.export_filename)
