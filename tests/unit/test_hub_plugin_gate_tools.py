"""v1.4.0 新增门禁工具（M1 QE1-2）的分发测试（HTTP 客户端桩替换，
仿 test_hub_plugin_execution_tools）。

覆盖：
- run_task_with_gate：POST /evaluation/tasks/{task_id}/run 路径替换、
  缺 task_id 的 missing_param 卡、无审批标记（与 task_run 同策略）；
- get_gate_status：GET /evaluation/tasks/{task_id}/gate 路径替换、
  execution_number 可选 query 组装（缺省不带）、登录态透传；
- 登录态缺失 → login_required 卡（Bearer 透传策略不变）；
- manifest 声明与路由表接线。
"""
import pytest

import hubkit

from app.hub_plugin import manifest
from app.hub_plugin.manifest import TOOL_ROUTES, build_tool_decls
from app.hub_plugin.plugin import AievoHubPlugin
from tests.unit.test_hub_plugin_execution_tools import (
    FakeClient,
    make_env,
    make_plugin,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无真 DB）


def call_tool(tool: str, payload: dict, data=None, status=200):
    fake = FakeClient(status=status, data=data)
    plugin = make_plugin(fake)
    env = make_env(payload, {"hub.tool": tool, "hub.mas_token": "tok-gate"})
    out = hubkit.payload_json(plugin.handle(None, env))[0] or {}
    return out, fake


# ── run_task_with_gate ────────────────────────────────────────────


def test_run_task_with_gate_posts_run_endpoint():
    out, fake = call_tool(
        "run_task_with_gate", {"task_id": "t_gate"},
        data={"id": "t_gate", "status": "running"})
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/evaluation/tasks/t_gate/run"
    assert call["mas_token"] == "tok-gate"  # Bearer 透传
    assert call["json_body"] is None


def test_run_task_with_gate_missing_task_id():
    out, fake = call_tool("run_task_with_gate", {})
    assert out["success"] is False
    assert out["error_code"] == "missing_param"
    assert fake.calls == []


# ── get_gate_status ───────────────────────────────────────────────


def test_get_gate_status_default_latest_execution():
    payload = {"task_id": "t_gate", "status": "running",
               "gate_enabled": True, "verdict": None}
    out, fake = call_tool("get_gate_status", {"task_id": "t_gate"}, data=payload)
    assert out["success"] is True
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/evaluation/tasks/t_gate/gate"
    assert call["params"] == {}  # execution_number 缺省不拼 query


def test_get_gate_status_with_execution_number():
    # protobuf Struct 数字往返后 2.0 → _normalize_value 归一为 int（plugin.py）
    out, fake = call_tool(
        "get_gate_status", {"task_id": "t_gate", "execution_number": 2.0},
        data={"verdict": "error"})
    assert out["success"] is True
    call = fake.calls[0]
    assert call["params"] == {"execution_number": 2}


def test_get_gate_status_login_required():
    """无 mas_token → login_required 卡（登录闸门策略不变）"""
    fake = FakeClient()
    plugin = make_plugin(fake)
    env = make_env({"task_id": "t_gate"}, {"hub.tool": "get_gate_status"})
    out = hubkit.payload_json(plugin.handle(None, env))[0] or {}
    assert out["success"] is False
    assert out["error_code"] == "login_required"
    assert fake.calls == []


# ── manifest 接线 ─────────────────────────────────────────────────


def test_gate_tools_in_manifest():
    for name in ("run_task_with_gate", "get_gate_status"):
        assert name in TOOL_ROUTES
        decl = next(d for d in build_tool_decls() if d.name == name)
        assert decl.description  # 描述非空（agent 可发现门禁语义）
        # 与 task_run 同审批策略：触发执行/查询不要求人工审批
        assert decl.requires_approval is False
    # 审批策略不回归：既有需审批工具仍标记
    assert TOOL_ROUTES["task_delete"]["requires_approval"] is True
