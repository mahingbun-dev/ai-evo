"""结果摘要裁剪测试：大明细不进信封、摘要信息密度可决策。"""
from app.hub_plugin.result_summary import (
    DEFAULT_MAX_FAILED_ITEMS,
    summarize_result,
)


def _full_result(n_items=90, n_failed=25) -> dict:
    return {
        "total": n_items + n_failed,
        "success_count": n_items,
        "failed_count": n_failed,
        "success_rate": n_items / (n_items + n_failed),
        "average_score": 8.5,
        "max_score": 10,
        "dimension_scores": {"准确性": {"average": 8.0, "count": 100, "weight": 1.0}},
        "items": [{"index": i, "score": 9} for i in range(n_items)],
        "failed_items": [
            {"index": i, "score": 3, "reason": "评" * 500} for i in range(n_failed)
        ],
        "completed_at": "2026-10-12T10:00:00",
    }


def test_summary_keeps_headline_metrics():
    out = summarize_result(_full_result())
    assert out["total"] == 115
    assert out["success_rate"] == 90 / 115
    assert out["average_score"] == 8.5
    assert out["dimension_scores"]["准确性"]["average"] == 8.0
    assert out["completed_at"] == "2026-10-12T10:00:00"


def test_summary_truncates_failed_items_and_drops_items():
    out = summarize_result(_full_result())
    assert len(out["failed_items"]) == DEFAULT_MAX_FAILED_ITEMS
    assert out["failed_items_omitted"] == 25 - DEFAULT_MAX_FAILED_ITEMS
    assert "items" not in out
    assert out["items_omitted"] == 90


def test_summary_truncates_long_strings():
    out = summarize_result(_full_result())
    reason = out["failed_items"][0]["reason"]
    assert "已截断" in reason
    assert len(reason) < 400


def test_summary_truncates_long_lists():
    result = {"failed_items": [{"logs": [f"line-{i}" for i in range(20)]}]}
    out = summarize_result(result)
    logs = out["failed_items"][0]["logs"]
    assert len(logs) == 4  # 3 条 + 1 条省略说明
    assert "共 20 项" in logs[-1]


def test_summary_passthrough_non_dict():
    assert summarize_result(None) is None
    assert summarize_result("running") == "running"
    assert summarize_result([]) == []


def test_summary_no_failed_items():
    out = summarize_result({"total": 10, "items": list(range(10))})
    assert out["failed_items"] == []
    assert "failed_items_omitted" not in out
    assert out["items_omitted"] == 10


def test_summary_custom_threshold():
    out = summarize_result(_full_result(n_failed=30), max_failed_items=5)
    assert len(out["failed_items"]) == 5
    assert out["failed_items_omitted"] == 25
