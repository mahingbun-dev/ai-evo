"""M8 QE8-5 新增 MCP 工具测试：dataset_versions / simulation_run /
simulation_run_status（HTTP 客户端桩替换，仿 test_hub_plugin_execution_tools）。

覆盖：
- 路径替换与请求拼装（GET 版本列表 / POST 仿真触发 / GET 运行查询）；
- simulation_run_status 后处理 _summarize_simulation_run：剥逐 trial 对话
  回放留指标与 trial 概要（turns_omitted 计数口径）、running 态无 result
  原样返回、非 dict 响应不炸；
- manifest 接线：三工具进 TOOL_ROUTES 与 build_tool_decls、
  simulation_run_status 挂后处理、工具总数 28（v1.5.0 契约）。
"""
import pytest

import hubkit

from app.hub_plugin import manifest
from app.hub_plugin.manifest import (
    PLUGIN_VERSION,
    TOOL_ROUTES,
    build_tool_decls,
    postprocess_of,
)
from app.hub_plugin.plugin import AievoHubPlugin

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def make_env(payload: dict, meta: dict | None = None):
    env = hubkit.with_payload_json(None, payload)
    for key, value in (meta or {}).items():
        env.meta[key] = value
    return env


class FakeClient:
    """记录调用参数的 ai-evo 客户端桩。"""

    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data if data is not None else {}
        self.calls = []

    def request(self, method, path, mas_token, params=None, json_body=None, timeout=None):
        self.calls.append(
            {"method": method, "path": path, "params": params, "json_body": json_body}
        )
        return {"status": self.status, "data": self.data}


def make_plugin(fake_client) -> AievoHubPlugin:
    plugin = AievoHubPlugin()
    plugin._client = fake_client
    return plugin


def payload_of(env) -> dict:
    data, _ = hubkit.payload_json(env)
    return data or {}


# ── 请求拼装 ────────────────────────────────────────────────────────


def test_dataset_versions_get_path():
    fake = FakeClient(data=[{"version_no": 1, "row_count": 3, "changelog": "初版"}])
    plugin = make_plugin(fake)
    env = make_env(
        {"dataset_id": "ds-1"},
        {"hub.tool": "dataset_versions", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["path"] == "/api/v1/evaluation/datasets/ds-1/versions"
    assert fake.calls[0]["params"] in (None, {})  # 无 query 参数不拼值


def test_simulation_run_post_path():
    fake = FakeClient(data={"run_id": "run-1", "status": "running", "trials_total": 3})
    plugin = make_plugin(fake)
    env = make_env(
        {"scenario_id": "sc-1"},
        {"hub.tool": "simulation_run", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["success"] is True
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["path"] == "/api/v1/simulation/scenarios/sc-1/run"


def test_simulation_run_status_get_path():
    fake = FakeClient(data={"id": "run-1", "status": "running", "trials_done": 1})
    plugin = make_plugin(fake)
    env = make_env(
        {"run_id": "run-1"},
        {"hub.tool": "simulation_run_status", "hub.mas_token": "tok"})
    payload_of(plugin.handle(None, env))
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["path"] == "/api/v1/simulation/runs/run-1"


# ── simulation_run_status 后处理 ────────────────────────────────────


def _run_result() -> dict:
    return {
        "id": "run-1", "scenario_id": "sc-1", "status": "completed",
        "trials_total": 2, "trials_done": 2,
        "result": {
            "trials": [
                {
                    "trial": 0, "passed": True,
                    "termination": {"reason": "completed", "detail": ""},
                    "turns": {"turns": [
                        {"role": "user", "content": "帮我把服务起起来"},
                        {"role": "assistant", "content": "好的", "tool_calls": [{"name": "exec"}]},
                    ]},
                    "agent": {"calls": 4, "tool_count": 2},
                    "tool_waste_rate": 0.0,
                    "faults_fired": [{"name": "tool_timeout"}],
                    "verification": {"status": "pass", "results": [{"name": "http_assert"}]},
                    "duration_ms": 1200,
                },
                {
                    "trial": 1, "passed": False,
                    "termination": {"reason": "max_turns", "detail": "超过回合上限"},
                    "turns": {"turns": []},
                    "agent": {"calls": 9, "tool_count": 7},
                    "tool_waste_rate": 0.4,
                    "faults_fired": [],
                    "verification": {"status": "fail"},
                    "duration_ms": 3000,
                },
            ],
            "pass_k": {"pass_at_1": 0.5, "pass_pow_k": 0.0, "trials": 2},
            "task_success_rate": 0.5,
            "average_turns": 6.0,
            "tool_waste_rate": 0.2,
            "fault_recovery_rate": None,
        },
    }


def test_run_status_postprocess_strips_conversations():
    fake = FakeClient(data=_run_result())
    plugin = make_plugin(fake)
    env = make_env(
        {"run_id": "run-1"},
        {"hub.tool": "simulation_run_status", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    result = out["data"]["result"]
    # 指标保留（agent 轮询消费的终态面）
    assert result["pass_k"]["pass_pow_k"] == 0.0
    assert result["task_success_rate"] == 0.5
    assert result["tool_waste_rate"] == 0.2
    # trial 概要保留、对话/校验明细剥除
    trials = result["trials"]
    assert [t["trial"] for t in trials] == [0, 1]
    assert trials[0]["passed"] is True and trials[0]["turn_count"] == 2
    assert trials[0]["tool_count"] == 2
    assert trials[1]["termination"]["reason"] == "max_turns"
    for trial in trials:
        assert "turns" not in trial and "verification" not in trial
    assert "已省略" in result["note"]


def test_run_status_postprocess_running_without_result_passthrough():
    """running 态 result 为 None：原样返回（无 trials 可剥，不注入 note）。"""
    fake = FakeClient(data={"id": "run-1", "status": "running", "result": None})
    plugin = make_plugin(fake)
    env = make_env(
        {"run_id": "run-1"},
        {"hub.tool": "simulation_run_status", "hub.mas_token": "tok"})
    out = payload_of(plugin.handle(None, env))
    assert out["data"]["status"] == "running"
    assert out["data"]["result"] is None
    assert "note" not in (out["data"].get("result") or {})


def test_run_status_postprocess_direct_non_dict_data():
    """响应根不是 dict：后处理不炸（原样返回）。"""
    data = [1, 2]
    assert manifest._summarize_simulation_run(data, {"payload": {}}) == data


# ── manifest 接线 ──────────────────────────────────────────────────


def test_manifest_declares_m8_tools():
    for name in ("dataset_versions", "simulation_run", "simulation_run_status"):
        assert name in TOOL_ROUTES
        route = TOOL_ROUTES[name]
        assert route["method"] in ("GET", "POST")
        assert route["schema"]["required"], name
    # 后处理只挂在运行查询（触发与版本列表无大载荷面）
    assert postprocess_of("simulation_run_status") is manifest._summarize_simulation_run
    assert postprocess_of("simulation_run") is None
    assert postprocess_of("dataset_versions") is None


def test_manifest_tool_count_matches_version_contract():
    """v1.5.0 契约 = 28 个工具：声明与路由一一对应（中台同版本号契约不可变）。"""
    decls = {t.name for t in build_tool_decls()}
    assert decls == set(TOOL_ROUTES)
    assert len(TOOL_ROUTES) == 28
    assert PLUGIN_VERSION == "1.5.0"
