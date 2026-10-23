"""故障注入单测（M7 QE7-3）

- 命中判定：turns 列表成员、缺省第 2 回合、非法 turns 拒绝（确定性纪律）；
- 请求侧拦截：矛盾指令附加（缺省话术/自定义）、上下文超长填充（字符量）；
- 响应侧拦截：工具超时/错误响应替换 output、污染数据确定性乱码；
  真实基础设施失败（status != ok）不改写；
- wrap_with_faults：命中回合记 faults_fired、未命中回合透传；
- 场景校验归一化：turns/pad_chars 收敛；
- 控制器接线：trial 明细 faults_fired + 回合 meta、会话不因注入短路、
  多 trial 故障恢复率聚合（含无命中时 None）。
"""
import pytest

from app.services.simulation import (
    DEFAULT_CONTRADICTION_TEXT,
    DEFAULT_FAULT_TURN,
    apply_request_faults,
    apply_response_faults,
    fault_fires,
    run_simulation_trial,
    run_simulation_trials,
    validate_scenario_config,
    wrap_with_faults,
)
from tests.unit.test_simulation_controller import (
    FakeSimulator,
    _agent,
    _reply,
    _valid_scenario,
    run_base,  # noqa: F401  (fixture 复用)
)

pytestmark = pytest.mark.unit


def _fault(f_type, **config):
    entry = {"type": f_type}
    if config:
        entry["config"] = config
    return entry


def _ok_result(output="本轮已处理完毕", **extra):
    return {"status": "ok", "output": output, "files": {"outputs/result.json": "…"},
            "finish_reason": "completed", "duration_ms": 8, **extra}


# ---------------------------------------------------------------- 命中判定
class TestFaultFires:
    def test_turns_membership_and_default(self):
        assert fault_fires(_fault("tool_error", turns=[1, 3]), 3) is True
        assert fault_fires(_fault("tool_error", turns=[1, 3]), 2) is False
        # 未配置 turns → 缺省第 2 回合命中
        assert fault_fires(_fault("tool_error"), DEFAULT_FAULT_TURN) is True
        assert fault_fires(_fault("tool_error"), 1) is False

    def test_scenario_rejects_bad_turns(self):
        with pytest.raises(ValueError, match="turns"):
            validate_scenario_config(_valid_scenario(
                faults=[{"type": "tool_error", "config": {"turns": [0, "x"]}}]))
        with pytest.raises(ValueError, match="turns"):
            validate_scenario_config(_valid_scenario(
                faults=[{"type": "tool_error", "config": {"turns": -1}}]))

    def test_scenario_normalizes_turns_and_pad(self):
        result = validate_scenario_config(_valid_scenario(faults=[
            {"type": "tool_error", "config": {"turns": [3, 1, 1]}},
            {"type": "context_overflow", "config": {"pad_chars": 99_999_999}},
        ]))
        assert result["faults"][0]["config"]["turns"] == [1, 3]  # 升序去重
        assert result["faults"][1]["config"]["pad_chars"] == 2_000_000  # 收敛上限


# ---------------------------------------------------------------- 请求侧
class TestRequestFaults:
    def test_contradiction_default_and_custom(self):
        message, fired = apply_request_faults(
            "帮我提交报销", [_fault("contradiction")], 2)
        assert DEFAULT_CONTRADICTION_TEXT in message
        assert "帮我提交报销" in message  # 原消息保留
        assert fired == [{"type": "contradiction", "turn": 2,
                          "detail": f"注入矛盾指令: {DEFAULT_CONTRADICTION_TEXT[:60]}"}]
        message2, _ = apply_request_faults(
            "x", [_fault("contradiction", turns=[1], text="改为撤销刚才的操作")], 1)
        assert "改为撤销刚才的操作" in message2

    def test_context_overflow_pads_message(self):
        pad_target = 5_000
        message, fired = apply_request_faults(
            "短消息", [_fault("context_overflow", turns=[1], pad_chars=pad_target)], 1)
        assert len(message) >= pad_target
        assert "超长填充" in message
        assert fired[0]["type"] == "context_overflow"

    def test_not_fired_passthrough(self):
        message, fired = apply_request_faults(
            "原消息", [_fault("contradiction", turns=[5])], 1)
        assert message == "原消息"
        assert fired == []


# ---------------------------------------------------------------- 响应侧
class TestResponseFaults:
    def test_tool_timeout_replaces_output(self):
        result, fired = apply_response_faults(
            _ok_result(), [_fault("tool_timeout", turns=[1], tool="上传发票", timeout_seconds=8)], 1)
        assert "[工具超时] 上传发票" in result["output"]
        assert "8 秒" in result["output"]
        assert fired[0]["detail"] == "注入工具超时: 上传发票"

    def test_tool_error_replaces_output(self):
        result, fired = apply_response_faults(
            _ok_result(), [_fault("tool_error", turns=[1], tool="查订单库", error_code="HTTP 503")], 1)
        assert "[工具错误] 查订单库" in result["output"]
        assert "HTTP 503" in result["output"]
        assert result["status"] == "ok"  # 故障模拟不改链路状态（会话存续）

    def test_dirty_data_pollutes_deterministically(self):
        result, fired = apply_response_faults(
            _ok_result(output="受理编号 BX-2026，金额 3580 元，状态已提交"), [_fault("dirty_data")], 2)
        assert "DATA_CORRUPTED" in result["output"]
        assert result["output"].startswith("受理编号")  # 头部 1/3 保留（部分可读）
        assert "状态已提交" not in result["output"]  # 尾段已乱序不可读
        again, _ = apply_response_faults(
            _ok_result(output="受理编号 BX-2026，金额 3580 元，状态已提交"), [_fault("dirty_data")], 2)
        assert again["output"] == result["output"]  # 确定性：同输入同输出

    def test_real_failure_not_rewritten(self):
        failure = {"status": "fail", "error": "容器启动失败", "finish_reason": "error"}
        result, fired = apply_response_faults(
            failure, [_fault("tool_error"), _fault("dirty_data")], 2)
        assert result == failure  # 基础设施失败原样上抛，不与故障模拟混淆
        assert fired == []


# ---------------------------------------------------------------- 包装层
class TestWrapWithFaults:
    def test_wrap_records_and_isolates_turns(self):
        seen = {}

        def raw(user_message, state, turn_no):
            seen[turn_no] = user_message
            return _ok_result()

        wrapped = wrap_with_faults(raw, [_fault("tool_error", turns=[2])])
        r1 = wrapped("第一轮", None, 1)
        assert r1["output"] == "本轮已处理完毕"
        assert "faults_fired" not in r1  # 未命中回合完全透传
        r2 = wrapped("第二轮", None, 2)
        assert r2["output"].startswith("[工具错误]")
        assert r2["faults_fired"] == [{"type": "tool_error", "turn": 2,
                                       "detail": "注入工具错误响应: 工具调用（HTTP 500）"}]

    def test_no_faults_returns_original(self):
        def raw(user_message, state, turn_no):
            return _ok_result()

        assert wrap_with_faults(raw, []) is raw

    def test_request_and_response_can_stack(self):
        """同一回合叠加请求侧+响应侧故障（矛盾指令 + 工具错误）"""
        seen = {}

        def raw(user_message, state, turn_no):
            seen[turn_no] = user_message
            return _ok_result()

        wrapped = wrap_with_faults(
            raw, [_fault("contradiction", turns=[1]), _fault("tool_error", turns=[1])])
        result = wrapped("先提交", None, 1)
        assert DEFAULT_CONTRADICTION_TEXT in seen[1]  # 请求侧改写已抵达底层通道
        assert result["output"].startswith("[工具错误]")  # 响应侧同时改写
        assert [f["type"] for f in result["faults_fired"]] == ["contradiction", "tool_error"]


# ---------------------------------------------------------------- 控制器接线
class TestControllerFaultWiring:
    def _scenario(self, faults, **kw):
        return validate_scenario_config(_valid_scenario(faults=faults, **kw))

    def test_fault_recorded_and_dialog_survives(self, run_base):
        """注入命中：会话不短路（区别于 agent_error）、明细与 meta 落故障事件"""
        scenario = self._scenario([_fault("tool_error", turns=[1])])
        sim = FakeSimulator([_reply("开始"), _reply("拿到了", goal=True)])
        result = run_simulation_trial(
            scenario, None, run_id="run-1", trial_index=0, simulator=sim,
            agent_call=_agent(1, "run-1", run_base))
        assert result["termination"]["reason"] == "goal_achieved"  # 注入未打断会话
        assert result["agent"]["calls"] == 2
        assert [f["type"] for f in result["faults_fired"]] == ["tool_error"]
        agent_turns = [t for t in result["turns"]["turns"] if t["role"] == "agent"]
        assert agent_turns[0]["content"].startswith("[工具错误]")
        assert agent_turns[0]["meta"]["faults"][0]["turn"] == 1
        assert "faults" not in agent_turns[1]["meta"]  # 未命中回合不带故障标记
        assert result["passed"] is True  # 注入后仍达成目标（终态校验过）

    def test_fault_recovery_rate_aggregation(self, run_base):
        """故障恢复率 = 注入命中的 trial 中通过占比；无命中为 None"""
        scenario = self._scenario([_fault("tool_error", turns=[1])], trials=2)
        agents = [lambda i=i: _agent(i + 1, "run-1", run_base, write=(i == 0))
                  for i in range(2)]  # trial 2 断言不过（未恢复）
        result = run_simulation_trials(
            scenario, None, run_id="run-1",
            agent_call_factory=lambda i: agents[i](),
            simulator_factory=lambda i: FakeSimulator([_reply("开始", goal=True)]))
        assert result["fault_recovery_rate"] == 0.5
        assert result["pass_k"]["pass_count"] == 1

        clean = validate_scenario_config(_valid_scenario(trials=2))
        no_fault = run_simulation_trials(
            clean, None, run_id="run-2",
            agent_call_factory=lambda i: _agent(i + 1, "run-2", run_base),
            simulator_factory=lambda i: FakeSimulator([_reply("开始", goal=True)]))
        assert no_fault["fault_recovery_rate"] is None  # 无命中不虚构分母
