"""v1.3.0 新增执行查询与导出工具的分发测试（HTTP 客户端桩替换，仿 test_hub_plugin_dispatch）。

覆盖：
- 5 个新工具（task_status/task_executions/task_execution_detail/
  task_execution_export/task_logs）的路径替换（含双 path_param）、
  query/body 组装、falsy 值保留、缺 path_param 的 missing_param 卡
- task_executions 后处理 _strip_execution_records：剥 result/config_snapshot
- task_execution_detail 后处理 paginate_detail：分页切片/钳制/排序/原样返回
- task_execution_export 的 route 级 timeout_seconds=180 覆盖插件级超时
- task_list 的 name 模糊过滤参数进 query
- manifest 工具路由与后处理接线
"""
import pytest

import hubkit

from app.hub_plugin import manifest
from app.hub_plugin.manifest import PLUGIN_VERSION, TOOL_ROUTES, postprocess_of
from app.hub_plugin.plugin import AievoHubPlugin
from app.hub_plugin.result_pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def make_env(payload: dict, meta: dict | None = None):
    env = hubkit.with_payload_json(None, payload)
    for key, value in (meta or {}).items():
        env.meta[key] = value
    return env


class FakeClient:
    """记录调用参数（含 timeout）的 ai-evo 客户端桩。"""

    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data if data is not None else {}
        self.calls = []

    def request(self, method, path, mas_token, params=None, json_body=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "mas_token": mas_token,
                "params": params,
                "json_body": json_body,
                "timeout": timeout,
            }
        )
        return {"status": self.status, "data": self.data}


def make_plugin(fake_client) -> AievoHubPlugin:
    plugin = AievoHubPlugin()
    plugin._client = fake_client
    return plugin


def call_tool(tool: str, payload: dict, data=None, status=200):
    """便捷分发：返回 (payload_of_env, fake_client)。"""
    fake = FakeClient(status=status, data=data)
    plugin = make_plugin(fake)
    env = make_env(payload, {"hub.tool": tool, "hub.mas_token": "tok"})
    out = hubkit.payload_json(plugin.handle(None, env))[0] or {}
    return out, fake


def payload_of_env(env) -> dict:
    return hubkit.payload_json(env)[0] or {}


# ── 路径替换与 query/body 组装 ──────────────────────────────────────


def test_task_status_path_and_no_body():
    out, fake = call_tool("task_status", {"task_id": "t1"}, data={"status": "running", "progress": 40})
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/evaluation/tasks/t1/status"
    assert call["params"] == {}
    assert call["json_body"] is None
    assert out["data"]["status"] == "running"


def test_task_executions_limit_in_query():
    out, fake = call_tool("task_executions", {"task_id": "t1", "limit": 5}, data=[])
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/evaluation/tasks/t1/executions"
    assert call["params"] == {"limit": 5}
    assert call["json_body"] is None


def test_task_executions_limit_absent_query_empty():
    _, fake = call_tool("task_executions", {"task_id": "t1"}, data=[])
    assert fake.calls[0]["params"] == {}


def test_task_executions_falsy_limit_preserved():
    """limit=0 是合法分页参数，用 `in` 判断必须保留（与 dataset_list 同口径）。"""
    _, fake = call_tool("task_executions", {"task_id": "t1", "limit": 0}, data=[])
    assert fake.calls[0]["params"] == {"limit": 0}


def test_task_execution_detail_double_path_param():
    """双 path_param：task_id 与 execution_number 都替换进 URL。

    注：execution_number 用字符串传——经 anc-hub 信封（protobuf Struct）时
    整数会被转成 float，见 test_task_execution_detail_int_number_becomes_float_path。
    """
    out, fake = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": "3"},
        data={"execution_number": 3, "status": "completed"},
    )
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/evaluation/results/t7/history/3"
    assert call["params"] == {}
    assert call["json_body"] is None


def test_task_execution_detail_int_number_becomes_float_path():
    """schema 声明 execution_number 为 integer：整数载荷经信封往返（Struct 只存 double，
    读回 3.0）后路径替换仍应产出干净整数路径——_build_request 的整值 float 归一。"""
    # make_env 走真实 hubkit 信封：整数在往返后确实变 float
    env = make_env(
        {"task_id": "t7", "execution_number": 3, "skip": 0, "limit": 20},
        {"hub.tool": "task_execution_detail", "hub.mas_token": "tok"},
    )
    rounded, _ = hubkit.payload_json(env)
    assert isinstance(rounded["execution_number"], float)  # 前置：信封确实整转浮
    fake = FakeClient(data={})
    plugin = make_plugin(fake)
    plugin.handle(None, env)
    call = fake.calls[0]
    assert call["path"] == "/api/v1/evaluation/results/t7/history/3"
    assert call["params"] == {"skip": 0, "limit": 20}


def test_task_execution_export_body_float_normalized():
    """导出 body 里的整值 float 同样归一为 int（json 序列化不再出 3.0）。"""
    _, fake = call_tool(
        "task_execution_export",
        {"task_id": "t7", "execution_number": 2.0, "format": "json"},
        data={"file_id": "f1", "cached": False},
    )
    call = fake.calls[0]
    assert call["path"] == "/api/v1/evaluation/results/t7/history/2/export"
    assert call["json_body"] == {"format": "json"}


def test_task_execution_detail_float_fraction_kept():
    """非整值 float 不动：limit=2.5 原样透传（后端自行校验）。"""
    _, fake = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 3, "limit": 2.5},
        data={},
    )
    assert fake.calls[0]["params"] == {"limit": 2.5}


def test_task_execution_detail_skip_limit_in_query():
    _, fake = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 3, "skip": 10, "limit": 20},
        data={},
    )
    assert fake.calls[0]["params"] == {"skip": 10, "limit": 20}


def test_task_execution_detail_falsy_skip_preserved():
    """skip=0/limit=0 保留（详情工具同样走「给了就拼」语义）。"""
    _, fake = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 3, "skip": 0, "limit": 0},
        data={},
    )
    assert fake.calls[0]["params"] == {"skip": 0, "limit": 0}


def test_task_execution_export_path_and_body():
    """POST 导出：双 path_param 进 URL，format 进 JSON body（execution_number 为字符串字面量；整值 float 归一见 test_task_execution_detail_int_number_becomes_float_path）。"""
    out, fake = call_tool(
        "task_execution_export",
        {"task_id": "t7", "execution_number": "2", "format": "json"},
        data={"file_id": "f-1"},
    )
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/evaluation/results/t7/history/2/export"
    assert call["json_body"] == {"format": "json"}
    assert call["params"] == {}


def test_task_execution_export_format_absent_body_empty():
    _, fake = call_tool(
        "task_execution_export",
        {"task_id": "t7", "execution_number": 2},
        data={"file_id": "f-1"},
    )
    assert fake.calls[0]["json_body"] == {}


def test_task_logs_level_limit_in_query():
    _, fake = call_tool(
        "task_logs",
        {"task_id": "t1", "level": "error", "limit": 30},
        data=[],
    )
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/evaluation/tasks/t1/logs"
    assert call["params"] == {"level": "error", "limit": 30}


def test_task_list_name_in_query():
    """task_list 的 name 模糊过滤参数进 query string。"""
    _, fake = call_tool("task_list", {"name": "冒烟", "limit": 20}, data=[])
    assert fake.calls[0]["params"] == {"name": "冒烟", "limit": 20}


def test_get_tools_extra_params_dropped():
    """GET 工具未声明的参数静默丢弃（不进 query 也不发 body）。"""
    _, fake = call_tool("task_status", {"task_id": "t1", "foo": "bar"}, data={})
    assert fake.calls[0]["params"] == {}
    assert fake.calls[0]["json_body"] is None


# ── 缺 path_param 错误卡 ────────────────────────────────────────────


def test_detail_missing_execution_number_card():
    out, fake = call_tool("task_execution_detail", {"task_id": "t7"}, data={})
    assert out["success"] is False
    assert out["error_code"] == "missing_param"
    assert "execution_number" in out["message"]
    assert out["plugin"] == "ai-evo"
    assert out["tool"] == "task_execution_detail"
    assert fake.calls == []


def test_export_missing_task_id_card():
    out, fake = call_tool("task_execution_export", {"execution_number": 1}, data={})
    assert out["success"] is False
    assert out["error_code"] == "missing_param"
    assert "task_id" in out["message"]
    assert fake.calls == []


def test_executions_blank_task_id_is_missing():
    out, fake = call_tool("task_executions", {"task_id": "   "}, data=[])
    assert out["success"] is False
    assert out["error_code"] == "missing_param"
    assert fake.calls == []


# ── task_executions 后处理：剥大列 ──────────────────────────────────


def test_executions_postprocess_strips_heavy_columns():
    data = [
        {
            "execution_number": 2, "status": "completed", "total_cost": 0.5,
            "result": {"items": [{"idx": 0}], "average_score": 9.0},
            "config_snapshot": {"model": "gpt"},
            "error_message": None,
        },
        {
            "execution_number": 1, "status": "failed",
            "result": None, "config_snapshot": None, "error_message": "boom",
        },
    ]
    out, _ = call_tool("task_executions", {"task_id": "t1"}, data=data)
    records = out["data"]
    for record in records:
        assert "result" not in record
        assert "config_snapshot" not in record
    assert records[0]["execution_number"] == 2
    assert records[0]["total_cost"] == 0.5
    assert records[1]["error_message"] == "boom"


def test_executions_postprocess_empty_and_non_list_safe():
    """空列表/非列表响应不炸（防御性后处理）。"""
    out, _ = call_tool("task_executions", {"task_id": "t1"}, data=[])
    assert out["data"] == []

    out, _ = call_tool("task_executions", {"task_id": "t1"}, data={"detail": "x"})
    assert out["data"] == {"detail": "x"}


def test_strip_execution_records_direct():
    rows = [{"result": 1, "config_snapshot": 2, "keep": 3}]
    assert manifest._strip_execution_records(rows, {"payload": {}}) == [{"keep": 3}]


# ── task_execution_detail 后处理：分页切片 ──────────────────────────


def _detail_result(n_items=14, n_failed=0):
    return {
        "execution_number": 1,
        "status": "completed",
        "average_score": 8.5,
        "success_rate": 0.9,
        "dimension_scores": {"准确性": {"average": 8.0, "weight": 1.0, "count": n_items}},
        "items": [{"idx": i, "success": True, "model_output": f"输出{i}"} for i in range(n_items)],
        "failed_items": [
            {"idx": 1000 + j, "success": False, "error": f"失败{j}"} for j in range(n_failed)
        ],
    }


def test_detail_postprocess_default_page_10():
    data = {"result": _detail_result(n_items=14)}
    out, _ = call_tool("task_execution_detail", {"task_id": "t7", "execution_number": 1}, data=data)
    result = out["data"]["result"]
    assert "items" not in result and "failed_items" not in result
    assert len(result["rows"]) == DEFAULT_PAGE_SIZE
    assert result["rows"][0]["idx"] == 0
    assert result["rows_page"] == {"skip": 0, "limit": DEFAULT_PAGE_SIZE, "returned": 10, "total": 14}
    assert "task_execution_export" in result["note"]


def test_detail_postprocess_explicit_skip_limit_and_page2():
    out, _ = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 1, "skip": 10, "limit": 10},
        data={"result": _detail_result(n_items=14)},
    )
    result = out["data"]["result"]
    assert [r["idx"] for r in result["rows"]] == [10, 11, 12, 13]
    assert result["rows_page"] == {"skip": 10, "limit": 10, "returned": 4, "total": 14}


def test_detail_postprocess_clamps_limit_and_negative_skip():
    out, _ = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 1, "skip": -5, "limit": 999},
        data={"result": _detail_result(n_items=120)},
    )
    result = out["data"]["result"]
    assert result["rows_page"]["skip"] == 0
    assert result["rows_page"]["limit"] == MAX_PAGE_SIZE
    assert len(result["rows"]) == MAX_PAGE_SIZE


def test_detail_postprocess_string_numeric_params_converted():
    """query 里的字符串数字（MCP 通道可能整包字符串）要能转换。"""
    out, _ = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 1, "skip": "2", "limit": "3"},
        data={"result": _detail_result(n_items=14)},
    )
    result = out["data"]["result"]
    assert [r["idx"] for r in result["rows"]] == [2, 3, 4]
    assert result["rows_page"] == {"skip": 2, "limit": 3, "returned": 3, "total": 14}


def test_detail_postprocess_merges_failed_items_sorted_by_idx():
    result_payload = _detail_result(n_items=2, n_failed=1)
    # items: idx 0,1；failed_items: idx 1000 → 排序后 [0, 1, 1000]
    out, _ = call_tool(
        "task_execution_detail",
        {"task_id": "t7", "execution_number": 1},
        data={"result": result_payload},
    )
    rows = out["data"]["result"]["rows"]
    assert [r["idx"] for r in rows] == [0, 1, 1000]
    assert rows[-1]["success"] is False
    assert result_payload["dimension_scores"]["准确性"]["average"] == 8.0  # 顶层汇总键原样保留


def test_detail_postprocess_non_int_idx_goes_last():
    data = {
        "result": {
            "items": [
                {"idx": "未知", "success": True, "model_output": "x"},
                {"idx": 1, "success": True, "model_output": "y"},
                {"model_output": "无idx"},  # idx 缺失排最后
            ],
        },
    }
    out, _ = call_tool("task_execution_detail", {"task_id": "t7", "execution_number": 1}, data=data)
    rows = out["data"]["result"]["rows"]
    assert [r.get("idx") for r in rows] == [1, "未知", None]


def test_detail_postprocess_failed_version_passthrough():
    """failed 版本无明细数组：原样返回（不注入 rows/rows_page/note）。"""
    failed_result = {"execution_number": 2, "status": "failed", "error": "裁判全部超时"}
    out, _ = call_tool(
        "task_execution_detail", {"task_id": "t7", "execution_number": 2}, data={"result": failed_result}
    )
    result = out["data"]["result"]
    assert "rows" not in result and "rows_page" not in result and "note" not in result
    assert result["error"] == "裁判全部超时"


def test_detail_postprocess_non_dict_result_passthrough():
    out, _ = call_tool(
        "task_execution_detail", {"task_id": "t7", "execution_number": 1}, data={"result": None}
    )
    assert out["data"]["result"] is None


def test_detail_postprocess_rag_detail_results_used():
    data = {"result": {"detail_results": [{"idx": 0, "retrieved": ["d1"]}, {"idx": 1, "retrieved": []}]}}
    out, _ = call_tool("task_execution_detail", {"task_id": "t7", "execution_number": 1}, data=data)
    result = out["data"]["result"]
    assert [r["idx"] for r in result["rows"]] == [0, 1]
    assert "detail_results" not in result


def test_detail_postprocess_latency_records_used():
    data = {"result": {"records": [{"idx": 5, "latency_ms": 100}]}}
    out, _ = call_tool("task_execution_detail", {"task_id": "t7", "execution_number": 1}, data=data)
    result = out["data"]["result"]
    assert [r["idx"] for r in result["rows"]] == [5]
    assert "records" not in result


def test_paginate_detail_direct_non_dict_data():
    """响应根不是 dict（如列表）：原样返回不炸。"""
    assert manifest.paginate_detail([1, 2], {"payload": {}}) == [1, 2]


# ── 超时策略 ────────────────────────────────────────────────────────


def test_export_route_timeout_overrides_plugin_default():
    """task_execution_export 慢操作：route timeout_seconds=180 覆盖插件级超时。"""
    fake = FakeClient(status=200, data={"file_id": "f-1"})
    plugin = make_plugin(fake)
    plugin._timeout = 12.5  # 显式钉死插件级缺省，排除环境变量干扰
    env = make_env(
        {"task_id": "t7", "execution_number": 1, "format": "markdown"},
        {"hub.tool": "task_execution_export", "hub.mas_token": "tok"},
    )
    plugin.handle(None, env)
    assert fake.calls[0]["timeout"] == 180.0
    assert isinstance(fake.calls[0]["timeout"], float)


def test_tools_without_route_timeout_use_plugin_default():
    """无 timeout_seconds 的工具用插件级缺省超时（不是 180）。"""
    fake = FakeClient(status=200, data={})
    plugin = make_plugin(fake)
    plugin._timeout = 12.5
    env = make_env({"task_id": "t1"}, {"hub.tool": "task_get", "hub.mas_token": "tok"})
    plugin.handle(None, env)
    assert fake.calls[0]["timeout"] == 12.5


# ── manifest 路由接线 ───────────────────────────────────────────────


def test_manifest_declares_new_tools():
    for name in (
        "task_status",
        "task_executions",
        "task_execution_detail",
        "task_execution_export",
        "task_logs",
    ):
        assert name in TOOL_ROUTES
        route = TOOL_ROUTES[name]
        assert route["method"] in ("GET", "POST")
        assert route["schema"]["required"], name
    assert PLUGIN_VERSION == "1.5.0"  # M8 QE8-5 新增版本查询/仿真触发工具升位


def test_manifest_export_route_timeout_declared():
    assert TOOL_ROUTES["task_execution_export"]["timeout_seconds"] == 180
    # 其余新工具不带 route 超时（走插件级缺省）
    for name in ("task_status", "task_executions", "task_execution_detail", "task_logs"):
        assert "timeout_seconds" not in TOOL_ROUTES[name]


def test_postprocess_wiring():
    """task_executions/task_execution_detail 声明的后处理即实现本体，防接线漂移。"""
    assert postprocess_of("task_executions") is manifest._strip_execution_records
    assert postprocess_of("task_execution_detail") is manifest.paginate_detail
    for name in ("task_status", "task_execution_export", "task_logs"):
        assert postprocess_of(name) is None
