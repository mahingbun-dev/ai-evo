"""插件分发与错误语义测试（HTTP 客户端全部用桩替换，不发真实请求）。

覆盖：
- validate：非 JSON 载荷 / 未知工具 / 缺工具名 拒绝；hub.tool 与 payload.op 双通道放行
- handle：登录态缺失 → login_required 卡；ai-evo 401 → login_invalid；
  2xx 透传 + 响应包装；超时/连接失败 → 结构化错误卡（不抛异常）；
  task_results/task_get 的结果摘要后处理生效；必填路径参数缺失 → missing_param
"""
from types import SimpleNamespace

import pytest

import hubkit
from hubkit.proto.hubv1 import envelope_pb2

from app.hub_plugin.plugin import AievoHubPlugin

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def make_env(payload: dict, meta: dict | None = None) -> envelope_pb2.Envelope:
    env = hubkit.with_payload_json(None, payload)
    for key, value in (meta or {}).items():
        env.meta[key] = value
    return env


class FakeClient:
    """记录调用参数的 ai-evo 客户端桩。"""

    def __init__(self, status=200, data=None, exc=None):
        self.status = status
        self.data = data if data is not None else {}
        self.exc = exc
        self.calls = []

    def request(self, method, path, mas_token, params=None, json_body=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "mas_token": mas_token,
                "params": params,
                "json_body": json_body,
            }
        )
        if self.exc is not None:
            raise self.exc
        return {"status": self.status, "data": self.data}


def make_plugin(fake_client) -> AievoHubPlugin:
    plugin = AievoHubPlugin()
    plugin._client = fake_client
    return plugin


def payload_of(env) -> dict:
    data, _ = hubkit.payload_json(env)
    return data or {}


# ── validate ────────────────────────────────────────────────────────


def test_validate_rejects_non_json_payload():
    plugin = AievoHubPlugin()
    env = envelope_pb2.Envelope()  # 空载荷，payload_json 返回非 JSON
    resp = plugin.validate(None, env)
    assert not resp.valid


def test_validate_rejects_unknown_op():
    plugin = AievoHubPlugin()
    env = make_env({"op": "no_such_tool"})
    resp = plugin.validate(None, env)
    assert not resp.valid
    assert any("未知" in i.message for i in resp.issues)


def test_validate_rejects_missing_op():
    plugin = AievoHubPlugin()
    resp = plugin.validate(None, make_env({"dataset_id": "x"}))
    assert not resp.valid


def test_validate_accepts_hub_tool_meta():
    plugin = AievoHubPlugin()
    resp = plugin.validate(None, make_env({"skip": 0}, {"hub.tool": "dataset_list"}))
    assert resp.valid


def test_validate_accepts_payload_op():
    plugin = AievoHubPlugin()
    resp = plugin.validate(None, make_env({"op": "dataset_list"}))
    assert resp.valid


# ── handle：登录态语义 ─────────────────────────────────────────────


def test_handle_login_required_card():
    """没有 mas_token：返回可行动的 login_required 卡（dc-dict 同口径）。"""
    plugin = make_plugin(FakeClient())
    env = make_env({"skip": 0}, {"hub.tool": "dataset_list"})  # 无 hub.mas_token
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is False
    assert out["error_code"] == "login_required"
    assert "anc-hub" in out["message"]


def test_handle_blank_token_treated_as_missing():
    """纯空白 token 视同缺失（与 hub 侧口径一致）。"""
    plugin = make_plugin(FakeClient())
    env = make_env({"skip": 0}, {"hub.tool": "dataset_list", "hub.mas_token": "   "})
    out = payload_of(plugin.handle(None, env))
    assert out["error_code"] == "login_required"


def test_handle_login_invalid_on_401():
    plugin = make_plugin(FakeClient(status=401, data={"detail": "x"}))
    env = make_env(
        {"skip": 0},
        {"hub.tool": "dataset_list", "hub.mas_token": "expired-token"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is False
    assert out["error_code"] == "login_invalid"
    assert out["http_status"] == 401


# ── handle：请求拼装与透传 ─────────────────────────────────────────


def test_handle_get_with_query_params_and_bearer_forwarding():
    """GET 分页参数进 query string；mas_token 以 Authorization: Bearer 转发。"""
    fake = FakeClient(status=200, data=[{"id": "d1", "name": "数据集"}])
    plugin = make_plugin(fake)
    env = make_env(
        {"skip": 5, "limit": 20},
        {"hub.tool": "dataset_list", "hub.mas_token": "tok-123"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert out["http_status"] == 200
    assert out["tool"] == "dataset_list"
    assert out["data"] == [{"id": "d1", "name": "数据集"}]

    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/evaluation/datasets"
    assert call["params"] == {"skip": 5, "limit": 20}
    assert call["mas_token"] == "tok-123"


def test_handle_path_param_substitution():
    fake = FakeClient(status=200, data={"id": "t1", "status": "running"})
    plugin = make_plugin(fake)
    env = make_env(
        {"task_id": "t1"},
        {"hub.tool": "task_run", "hub.mas_token": "tok"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/evaluation/tasks/t1/run"
    assert call["json_body"] is None  # run 无请求体


def test_handle_body_params_forwarded():
    fake = FakeClient(status=200, data={"id": "t9"})
    plugin = make_plugin(fake)
    env = make_env(
        {"name": "冒烟任务", "evaluation_type": "judge", "dataset_id": "d1"},
        {"hub.tool": "task_create", "hub.mas_token": "tok"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/evaluation/tasks"
    assert call["json_body"] == {
        "name": "冒烟任务",
        "evaluation_type": "judge",
        "dataset_id": "d1",
    }


def test_handle_payload_op_fallback_channel():
    """invoke_plugin 通用入口：payload.op 兜底分发（无 hub.tool meta）。"""
    fake = FakeClient(status=200, data={"status": "ok"})
    plugin = make_plugin(fake)
    env = make_env({"op": "task_stop", "task_id": "t1"}, {"hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert out["tool"] == "task_stop"
    assert fake.calls[0]["path"] == "/api/v1/evaluation/tasks/t1/stop"


def test_handle_missing_path_param_card():
    fake = FakeClient(status=200, data={})
    plugin = make_plugin(fake)
    env = make_env({"name": "x"}, {"hub.tool": "dataset_get", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is False
    assert out["error_code"] == "missing_param"
    assert "dataset_id" in out["message"]
    assert fake.calls == []


def test_handle_non_2xx_passthrough():
    fake = FakeClient(status=422, data={"detail": "name 不能为空"})
    plugin = make_plugin(fake)
    env = make_env({"name": ""}, {"hub.tool": "dataset_create", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is False
    assert out["http_status"] == 422
    assert out["data"]["detail"] == "name 不能为空"


def test_handle_infrastructure_errors_as_cards():
    """超时/连接失败是结构化错误卡，不是异常（中台不会记成插件故障）。"""
    from app.hub_plugin.client import AievoApiError

    for exc in (
        AievoApiError("timeout", "请求 ai-evo 超时"),
        AievoApiError("unreachable", "无法连接 ai-evo"),
    ):
        plugin = make_plugin(FakeClient(exc=exc))
        env = make_env({"task_id": "t1"}, {"hub.tool": "task_get", "hub.mas_token": "tok"})
        out = payload_of(plugin.handle(None, env))
        assert out["success"] is False
        assert out["error_code"] == exc.error_code
        assert out["message"] == exc.message


# ── handle：结果摘要后处理 ────────────────────────────────────────


def _full_result() -> dict:
    return {
        "total": 100,
        "success_count": 90,
        "failed_count": 10,
        "success_rate": 0.9,
        "average_score": 8.5,
        "max_score": 10,
        "dimension_scores": {"准确性": {"average": 8.0, "count": 100, "weight": 1.0}},
        "items": [{"index": i, "score": 9} for i in range(90)],
        "failed_items": [
            {"index": i, "score": 3, "reason": "回答与参考不符" * 80}
            for i in range(25)
        ],
        "completed_at": "2026-10-12T10:00:00",
    }


def test_handle_task_results_summarized():
    fake = FakeClient(status=200, data={"task_id": "t1", "status": "completed", "result": _full_result()})
    plugin = make_plugin(fake)
    env = make_env({"task_id": "t1"}, {"hub.tool": "task_results", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    result = out["data"]["result"]
    # 摘要字段保留
    assert result["success_rate"] == 0.9
    assert result["average_score"] == 8.5
    assert result["dimension_scores"]["准确性"]["average"] == 8.0
    # 失败样例截断到 10 条 + 省略计数；评语长文本被截断
    assert len(result["failed_items"]) == 10
    assert result["failed_items_omitted"] == 15
    assert len(result["failed_items"][0]["reason"]) < 400
    # 成功项明细整块省略只留计数
    assert "items" not in result
    assert result["items_omitted"] == 90
    # 结果页链接
    assert out["data"]["result_url"].endswith("/results?task_id=t1")
    assert out["data"]["result_url_task_id"] == "t1"


def test_handle_task_get_result_truncated():
    """task_get 的 result 同样裁成摘要，避免大响应撑爆信封。"""
    fake = FakeClient(
        status=200,
        data={"id": "t1", "status": "completed", "result": _full_result()},
    )
    plugin = make_plugin(fake)
    env = make_env({"task_id": "t1"}, {"hub.tool": "task_get", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    result = out["data"]["result"]
    assert result["items_omitted"] == 90
    assert len(result["failed_items"]) == 10
    assert out["data"]["status"] == "completed"


def test_handle_summary_safe_when_result_missing():
    """任务未完成（result=None）时摘要后处理不炸。"""
    fake = FakeClient(status=200, data={"task_id": "t1", "status": "running", "result": None})
    plugin = make_plugin(fake)
    env = make_env({"task_id": "t1"}, {"hub.tool": "task_results", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert out["data"]["result"] is None


# ── health ─────────────────────────────────────────────────────────


def test_health_reports_ai_evo_reachability():
    plugin = AievoHubPlugin()
    plugin._client = SimpleNamespace(
        health=lambda: True, request=None
    )
    assert plugin.health().healthy is True

    plugin._client = SimpleNamespace(health=lambda: False, request=None)
    assert plugin.health().healthy is False


# ── 盲区回归：由独立验证 agent 的审查清单补齐 ─────────────────────


def test_handle_import_hub_files_array_passthrough():
    """file_ids 数组必须原样透传给导入端点（数组字段不是对象 body 的特例）。"""
    fake = FakeClient(status=200, data={"id": "job-1", "status": "pending"})
    plugin = make_plugin(fake)
    env = make_env(
        {"file_ids": ["f1", "f2"], "name": "导入的数据集"},
        {"hub.tool": "dataset_import_hub_files", "hub.mas_token": "tok"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/evaluation/datasets/import/hub-files"
    assert call["json_body"] == {"file_ids": ["f1", "f2"], "name": "导入的数据集"}


def test_handle_job_status_path_substitution():
    fake = FakeClient(status=200, data={"id": "job-9", "status": "running"})
    plugin = make_plugin(fake)
    env = make_env(
        {"job_id": "job-9"},
        {"hub.tool": "hub_import_job_status", "hub.mas_token": "tok"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert fake.calls[0]["path"] == "/api/v1/hub-import-jobs/job-9"


def test_handle_falsy_query_params_preserved():
    """skip=0/limit=0 是合法分页参数，必须保留——用 `in` 而不是真值判断。"""
    fake = FakeClient(status=200, data=[])
    plugin = make_plugin(fake)
    env = make_env(
        {"skip": 0, "limit": 0},
        {"hub.tool": "dataset_list", "hub.mas_token": "tok"},
    )
    plugin.handle(None, env)
    assert fake.calls[0]["params"] == {"skip": 0, "limit": 0}


def test_handle_whitespace_path_param_is_missing():
    fake = FakeClient(status=200, data={})
    plugin = make_plugin(fake)
    env = make_env(
        {"task_id": "   "},
        {"hub.tool": "task_get", "hub.mas_token": "tok"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is False
    assert out["error_code"] == "missing_param"
    assert out["plugin"] == "ai-evo"
    assert out["tool"] == "task_get"
    assert fake.calls == []


def test_handle_get_extra_params_dropped():
    """GET 工具的多余参数静默丢弃，不拼 query 也不发 body。"""
    fake = FakeClient(status=200, data=[])
    plugin = make_plugin(fake)
    env = make_env(
        {"task_id": "t1", "name": "将被忽略"},
        {"hub.tool": "task_get", "hub.mas_token": "tok"},
    )
    plugin.handle(None, env)
    assert fake.calls[0]["params"] == {}
    assert fake.calls[0]["json_body"] is None


def test_handle_unknown_tool_defensive_card():
    """绕过 validate 直调 handle 的未知 op：结构化卡兜底，不抛 KeyError。"""
    plugin = make_plugin(FakeClient())
    env = make_env({"op": "no_such_tool"}, {"hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is False
    assert out["error_code"] == "unknown_tool"
    assert "no_such_tool" in out["message"]


def test_handle_task_get_without_result_key_no_injection():
    """result 键缺失的 task_get 响应不被塞进 result:null（截断护栏只动已有键）。"""
    fake = FakeClient(status=200, data={"id": "t1", "status": "pending"})
    plugin = make_plugin(fake)
    env = make_env({"task_id": "t1"}, {"hub.tool": "task_get", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert "result" not in out["data"]


def test_handle_import_hub_files_script_id_passthrough():
    """script_id（清洗脚本 id/名称）必须透传给导入端点。

    平台据此逐包执行清洗脚本、把脚本产出行作为数据集行；不传时只登记执行包引用，
    数据行停在 hub_file_id/hub_summary、清洗内容不展开。
    """
    fake = FakeClient(status=200, data={"id": "job-2", "status": "pending"})
    plugin = make_plugin(fake)
    env = make_env(
        {"file_ids": ["f1"], "name": "ds", "script_id": "anc_hub_row_passthrough"},
        {"hub.tool": "dataset_import_hub_files", "hub.mas_token": "tok"},
    )
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert fake.calls[0]["json_body"] == {
        "file_ids": ["f1"],
        "name": "ds",
        "script_id": "anc_hub_row_passthrough",
    }


def test_manifest_declares_hub_import_script_id():
    """工具 schema 必须声明 script_id，否则 MCP 客户端按契约传不了该参数。"""
    from app.hub_plugin.manifest import TOOL_ROUTES

    props = TOOL_ROUTES["dataset_import_hub_files"]["schema"]["properties"]
    assert "script_id" in props
    assert props["script_id"]["type"] == "string"
