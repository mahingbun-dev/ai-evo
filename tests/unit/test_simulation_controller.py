"""回合控制器与环境重置单测（M7 QE7-2）

- 场景配置校验：verifiers 发布门禁（强制非空）、agent exec_type/指令白名单、
  personas/env.files/faults 白名单、试验参数越界收敛与缺省；
- 环境重置：残留清空 + outputs/ 重建 + env.files staged、路径越界拒绝；
- 单 trial（mock 模拟器 + mock agent 通道，真实 file_assert 终态校验）：
  goal_achieved + 断言通过 → passed；断言失败 → not passed；agent 失败短路；
  模拟器协议错误短路（断言碰巧通过也判不过）；软超时；停止信号；
- 多 trial：pass@1 / pass^k 聚合、trial 间环境隔离；
- default_agent_call：stdin 契约与 execute_script 透传（patch 真实编排入口）。
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.pass_k import trial_pass_stats
from app.services.simulation import (
    build_agent_script,
    default_agent_call,
    extract_agent_reply,
    reset_trial_environment,
    run_simulation_trial,
    run_simulation_trials,
    validate_scenario_config,
)
from app.services.simulation.scenario import (
    DEFAULT_TRIALS,
    TRIALS_MAX,
)
from app.services.user_simulator import (
    SimulationDialogState,
    UserSimulatorError,
    check_termination,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- 场景夹具
def _valid_scenario(**overrides):
    config = {
        "agent": {
            "exec_type": "claude_agent",
            "instruction": "你是报销助手，按 stdin.json 里的用户消息处理报销",
            "timeout_seconds": 60,
        },
        "personas": [{"name": "小王", "goal": "拿到受理编号"}],
        "combo_mode": "single",
        "max_turns": 15,
        "trial_soft_timeout_seconds": 60,
        "env": {"files": {"README.md": "初始说明"}},
        "verifiers": [{
            "type": "file_assert", "name": "回执文件",
            "config": {"assertions": [
                {"name": "受理编号", "path": "outputs/result.json",
                 "mode": "jsonpath", "jsonpath": "status", "expected": "done"},
            ]},
        }],
        "trials": 3,
    }
    config.update(overrides)
    return config


@pytest.fixture
def run_base(tmp_path, monkeypatch):
    """工作区根切到 tmp（SCRIPT_RUN_BASE 单一事实源，pre_judge_runner 模块级）"""
    base = tmp_path / "script_runs"
    monkeypatch.setattr("app.services.pre_judge_runner.SCRIPT_RUN_BASE", base)
    return base


# ---------------------------------------------------------------- 替身
class FakeSimulator:
    """模拟器替身：按剧本回放 next_message；状态账本与终止用 QE7-1 真实逻辑"""

    def __init__(self, replies, personas=("小王",), max_turns=15, patience=5):
        self.replies = [dict(r) for r in replies]
        self.pos = 0
        self.personas = list(personas)
        self.max_turns = max_turns
        self.min_patience = patience

    def initial_state(self):
        state = SimulationDialogState()
        for name in self.personas:
            state.goal_status[name] = False
        return state

    def next_message(self, state):
        reply = dict(self.replies[min(self.pos, len(self.replies) - 1)])
        self.pos += 1
        return reply

    def check_termination(self, state):
        return check_termination(state, self.max_turns, self.min_patience)


def _reply(message="继续", goal=False, progress=True, action="speak", speaker="小王"):
    return {
        "speaker": speaker, "message": message, "goal_status": goal,
        "goal_apply": [speaker], "progress": progress, "action": action,
    }


def _agent(trial_no, run_id, base, write=True, fail=False):
    """agent 通道替身：往本 trial 工作区写产物文件（file_assert 断言目标）"""
    def call(user_message, state, turn_no):
        if fail:
            return {"status": "fail", "error": "容器启动失败",
                    "finish_reason": "error", "files": {}}
        if write:
            out_dir = base / run_id / f"trial-{trial_no}" / "outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "result.json").write_text(
                '{"status": "done", "turn": %d}' % turn_no, encoding="utf-8")
        return {
            "status": "ok", "output": f"第{turn_no}轮已处理",
            "files": {"outputs/result.json": "…"}, "finish_reason": "completed",
            "duration_ms": 12,
        }
    return call


class _FakeStop:
    def __init__(self, stopped=False):
        self.stopped = stopped

    def is_set(self):
        return self.stopped


# ---------------------------------------------------------------- 场景校验
class TestValidateScenarioConfig:
    def test_valid_config_normalized(self):
        result = validate_scenario_config(_valid_scenario())
        assert result["agent"]["exec_type"] == "claude_agent"
        assert result["max_turns"] == 15
        assert result["trials"] == 3
        assert result["env"]["files"] == {"README.md": "初始说明"}
        assert result["faults"] == []
        assert result["personas"][0]["name"] == "小王"

    def test_publish_gate_requires_verifier(self):
        with pytest.raises(ValueError, match="终态校验器"):
            validate_scenario_config(_valid_scenario(verifiers=[]))
        with pytest.raises(ValueError, match="终态校验器"):
            validate_scenario_config(_valid_scenario(verifiers=None))

    def test_agent_whitelist(self):
        with pytest.raises(ValueError, match="exec_type"):
            validate_scenario_config(_valid_scenario(
                agent={"exec_type": "gpt", "instruction": "x"}))
        with pytest.raises(ValueError, match="instruction"):
            validate_scenario_config(_valid_scenario(
                agent={"exec_type": "deepseek_agent", "instruction": "  "}))

    def test_personas_and_unknown_keys(self):
        with pytest.raises(ValueError, match="personas"):
            validate_scenario_config(_valid_scenario(personas=[{"role": "无目标"}]))
        with pytest.raises(ValueError, match="未知键"):
            validate_scenario_config(_valid_scenario(magic=True))

    def test_env_files_path_whitelist(self):
        with pytest.raises(ValueError, match="相对路径"):
            validate_scenario_config(_valid_scenario(
                env={"files": {"/etc/passwd": "x"}}))
        with pytest.raises(ValueError, match=r"\.\."):
            validate_scenario_config(_valid_scenario(
                env={"files": {"../escape.txt": "x"}}))

    def test_faults_whitelist_and_passthrough(self):
        with pytest.raises(ValueError, match="faults"):
            validate_scenario_config(_valid_scenario(faults=[{"type": "nuke"}]))
        result = validate_scenario_config(_valid_scenario(
            faults=[{"type": "tool_timeout", "config": {"tool": "upload", "delay_ms": 500}}]))
        # QE7-3：config 归一化补 turns（未配置 → 空列表，运行时按缺省回合兜底）
        assert result["faults"] == [
            {"type": "tool_timeout",
             "config": {"tool": "upload", "delay_ms": 500, "turns": []}}]

    def test_params_clamped_and_defaulted(self):
        result = validate_scenario_config(_valid_scenario(
            max_turns=999, trials=99, trial_soft_timeout_seconds=1,
            agent={"exec_type": "claude_agent", "instruction": "x",
                   "timeout_seconds": 99999},
        ))
        assert result["max_turns"] == 50
        assert result["trials"] == TRIALS_MAX
        assert result["trial_soft_timeout_seconds"] == 60
        assert result["agent"]["timeout_seconds"] == 3600
        # 缺省：trials=3 / max_turns=15 / 软超时 1800
        minimal = validate_scenario_config({
            "agent": {"exec_type": "deepseek_agent", "instruction": "x"},
            "personas": [{"goal": "g"}],
            "verifiers": [{"type": "http_assert",
                           "config": {"url": "https://sut.example.com/api"}}],
        })
        assert minimal["trials"] == DEFAULT_TRIALS
        assert minimal["max_turns"] == 15
        assert minimal["trial_soft_timeout_seconds"] == 1800

    def test_single_mode_extra_personas_dropped(self):
        result = validate_scenario_config(_valid_scenario(
            personas=[{"name": "A", "goal": "a"}, {"name": "B", "goal": "b"}]))
        assert [p["name"] for p in result["personas"]] == ["A"]


# ---------------------------------------------------------------- 环境重置
class TestResetTrialEnvironment:
    def test_resets_leftover_and_stages_files(self, run_base):
        run_dir = reset_trial_environment("run-1", "trial-1", {"README.md": "初始说明"})
        assert (run_dir / "README.md").read_text(encoding="utf-8") == "初始说明"
        assert (run_dir / "outputs").is_dir()  # build_run_dir 语义保留
        # 伪造上一 trial 残留 → 重置后清空
        (run_dir / "outputs" / "leftover.txt").write_text("脏数据", encoding="utf-8")
        reset_trial_environment("run-1", "trial-1", {"README.md": "初始说明"})
        assert not (run_dir / "outputs" / "leftover.txt").exists()
        assert (run_dir / "README.md").exists()  # 初始文件重新 staged

    def test_trial_workspaces_isolated(self, run_base):
        first = reset_trial_environment("run-1", "trial-1", {})
        (first / "outputs").mkdir(exist_ok=True)
        (first / "outputs" / "result.json").write_text("{}", encoding="utf-8")
        second = reset_trial_environment("run-1", "trial-2", {})
        assert (second / "outputs" / "result.json").exists() is False

    def test_path_escape_rejected(self, run_base):
        from app.utils.path_safety import PathEscapeError

        reset_trial_environment("run-1", "trial-1", {})  # 建根
        with pytest.raises(PathEscapeError):
            reset_trial_environment("run-1", "../../evil", {})


# ---------------------------------------------------------------- 单 trial
class TestRunSimulationTrial:
    def _run(self, scenario, sim, agent, run_id="run-1", trial_index=0, **kw):
        return run_simulation_trial(
            scenario, task=None, run_id=run_id, trial_index=trial_index,
            simulator=sim, agent_call=agent, **kw)

    def test_goal_achieved_with_passing_verifier(self, run_base):
        scenario = validate_scenario_config(_valid_scenario())
        sim = FakeSimulator([_reply("开始"), _reply("收到", goal=True)])
        result = self._run(scenario, sim, _agent(1, "run-1", run_base))
        assert result["passed"] is True
        assert result["termination"]["reason"] == "goal_achieved"
        assert result["agent"]["calls"] == 2
        assert result["verification"]["status"] == "pass"
        # 完整对话落 trial 明细（用户+agent 交替，含工具轨迹与 meta）
        turns = result["turns"]["turns"]
        assert [t["role"] for t in turns] == ["user", "agent", "user", "agent"]
        assert turns[1]["tools"] == ["outputs/result.json"]
        assert turns[1]["meta"]["status"] == "ok"

    def test_verifier_fail_not_passed(self, run_base):
        scenario = validate_scenario_config(_valid_scenario())
        sim = FakeSimulator([_reply("开始"), _reply("拿到了", goal=True)])
        result = self._run(scenario, sim, _agent(1, "run-1", run_base, write=False))
        assert result["termination"]["reason"] == "goal_achieved"
        assert result["verification"]["status"] == "fail"
        assert result["passed"] is False  # 终态校验纪律：断言不过即 trial 不过

    def test_agent_failure_short_circuits(self, run_base):
        scenario = validate_scenario_config(_valid_scenario())
        sim = FakeSimulator([_reply("开始")] * 10)  # 本可继续多轮
        result = self._run(scenario, sim, _agent(1, "run-1", run_base, fail=True))
        assert result["termination"]["reason"] == "agent_error"
        assert result["agent"]["calls"] == 1  # 短路，不烧完回合
        assert result["passed"] is False

    def test_simulator_error_not_passed_even_if_assert_passes(self, run_base):
        # 校验断言 env 初始文件（重置即 staged）——断言可通过，
        # 但模拟器协议失败属基础设施错误，trial 必须判不过
        scenario = validate_scenario_config(_valid_scenario(verifiers=[{
            "type": "file_assert", "name": "初始文件",
            "config": {"assertions": [{"name": "README", "path": "README.md"}]},
        }]))

        class BrokenSim(FakeSimulator):
            def next_message(self, state):
                raise UserSimulatorError("模拟器协议解析连续失败")

        result = self._run(scenario, BrokenSim([]), _agent(1, "run-1", run_base))
        assert result["termination"]["reason"] == "simulator_error"
        assert result["verification"]["status"] == "pass"
        assert result["passed"] is False

    def test_soft_timeout_between_turns(self, run_base):
        scenario = validate_scenario_config(_valid_scenario(trial_soft_timeout_seconds=60))
        clock = {"now": 0.0}

        def slow_agent(user_message, state, turn_no):
            clock["now"] += 100.0  # 单回合耗尽软超时预算
            return _agent(1, "run-1", run_base)(user_message, state, turn_no)

        sim = FakeSimulator([_reply("开始")] * 10)
        result = self._run(
            scenario, sim, slow_agent, now_fn=lambda: clock["now"])
        assert result["termination"]["reason"] == "soft_timeout"
        assert result["agent"]["calls"] == 1  # 第 1 回合完成、回合间被软超时截停

    def test_stop_signal_between_turns(self, run_base):
        scenario = validate_scenario_config(_valid_scenario())
        stop = _FakeStop()
        calls = {"n": 0}

        def agent(user_message, state, turn_no):
            calls["n"] += 1
            if calls["n"] >= 1:
                stop.stopped = True  # 第 1 回合后置位
            return _agent(1, "run-1", run_base)(user_message, state, turn_no)

        sim = FakeSimulator([_reply("开始")] * 10)
        result = self._run(scenario, sim, agent, stop_signal=stop)
        assert result["termination"]["reason"] == "stopped"
        assert result["agent"]["calls"] == 1
        assert result["passed"] is False

    def test_max_turns_termination(self, run_base):
        # 口径：max_turns 不属基础设施中止——终态断言通过即通过（校验器权威），
        # 断言不过则不过；模拟器 goal 判定只是终止信号，不覆盖确定性校验
        scenario = validate_scenario_config(_valid_scenario(max_turns=2))
        sim = FakeSimulator([_reply("还没好")] * 5, max_turns=2)
        result = self._run(scenario, sim, _agent(1, "run-1", run_base))
        assert result["termination"]["reason"] == "max_turns"
        assert result["turns"]["user_turn_count"] == 2
        assert result["passed"] is True  # 终态已达成（断言过），回合耗尽不翻案
        result_fail = self._run(scenario, sim, _agent(1, "run-1", run_base, write=False))
        assert result_fail["passed"] is False


# ---------------------------------------------------------------- 多 trial
class TestRunSimulationTrials:
    def test_pass_k_all_pass(self, run_base):
        scenario = validate_scenario_config(_valid_scenario(trials=3))
        result = run_simulation_trials(
            scenario, None, run_id="run-1",
            agent_call_factory=lambda i: _agent(i + 1, "run-1", run_base),
            simulator_factory=lambda i: FakeSimulator(
                [_reply("开始"), _reply("拿到", goal=True)]),
        )
        assert len(result["trials"]) == 3
        assert result["pass_k"] == trial_pass_stats([True, True, True])
        assert result["pass_k"]["pass_pow_k"] == 1
        assert result["task_success_rate"] == 1.0
        assert result["average_turns"] == 2.0

    def test_pass_k_partial(self, run_base):
        scenario = validate_scenario_config(_valid_scenario(trials=3))
        # trial 3 的 agent 不写产物 → 断言失败
        agents = [lambda i=i: _agent(i + 1, "run-1", run_base, write=(i != 2))
                  for i in range(3)]
        result = run_simulation_trials(
            scenario, None, run_id="run-1",
            agent_call_factory=lambda i: agents[i](),
            simulator_factory=lambda i: FakeSimulator(
                [_reply("开始"), _reply("拿到", goal=True)]),
        )
        assert result["pass_k"]["pass_count"] == 2
        assert result["pass_k"]["pass_at_1"] == 0.6667
        assert result["pass_k"]["pass_pow_k"] == 0  # 有一次翻车即非全过

    def test_trial_env_isolation_across_trials(self, run_base):
        scenario = validate_scenario_config(_valid_scenario(trials=2))
        seen = []

        def agent_factory(i):
            def call(user_message, state, turn_no):
                # 断言看不到前一个 trial 的产物（环境已重置）
                marker = run_base / "run-1" / f"trial-{i + 1}" / "outputs" / "result.json"
                if i > 0:
                    prev = run_base / "run-1" / f"trial-{i}" / "outputs" / "result.json"
                    seen.append(prev.exists())  # 前一 trial 目录仍在（审计），本 trial 不受污染
                out_dir = marker.parent
                out_dir.mkdir(parents=True, exist_ok=True)
                marker.write_text('{"status": "done"}', encoding="utf-8")
                return {"status": "ok", "output": "ok", "files": {}, "finish_reason": "completed"}
            return call

        run_simulation_trials(
            scenario, None, run_id="run-1",
            agent_call_factory=agent_factory,
            simulator_factory=lambda i: FakeSimulator([_reply("开始", goal=True)]),
        )
        assert seen == [True]  # trial-1 产物在原位，trial-2 工作区是全新树

    def test_stop_signal_prevents_new_trials(self, run_base):
        scenario = validate_scenario_config(_valid_scenario(trials=3))
        stop = _FakeStop(stopped=True)
        result = run_simulation_trials(
            scenario, None, run_id="run-1", stop_signal=stop,
            agent_call_factory=lambda i: _agent(i + 1, "run-1", run_base),
            simulator_factory=lambda i: FakeSimulator([_reply("开始", goal=True)]),
        )
        assert result["trials"] == []  # 开跑前即收到停止信号：不开新 trial
        assert result["pass_k"]["trials"] == 0


# ---------------------------------------------------------------- agent 通道
class TestDefaultAgentCall:
    def test_stdin_contract_and_passthrough(self, run_base, monkeypatch):
        from app.services import pre_judge_runner

        captured = {}

        def fake_execute_script(script, stdin, timeout_seconds=None, network=False):
            captured["script"] = script
            captured["stdin"] = stdin
            captured["timeout"] = timeout_seconds
            return {"status": "ok", "output": "done", "files": {},
                    "finish_reason": "completed"}

        monkeypatch.setattr(pre_judge_runner, "execute_script", fake_execute_script)
        scenario = validate_scenario_config(_valid_scenario(
            agent={"exec_type": "deepseek_agent", "instruction": "处理报销",
                   "timeout_seconds": 120, "allowed_tools": ["Read"],
                   "harness_extra": {"profile": "fast"}}))
        state = FakeSimulator([_reply("你好")]).initial_state()
        state.record_user_turn(_reply("你好"))

        call = default_agent_call(
            scenario["agent"], "run-9", "trial-1", Path("/ws/run-9/trial-1"))
        result = call("你好", state, 1)

        assert result["status"] == "ok"
        script = captured["script"]
        assert script["exec_type"] == "deepseek_agent"
        assert script["entrypoint"] == "处理报销"
        assert script["timeout_seconds"] == 120
        assert script["allowed_tools"] == ["Read"]
        assert script["harness_extra"] == {"profile": "fast"}
        assert captured["timeout"] == 120
        stdin = captured["stdin"]
        assert stdin["kind"] == "simulation"
        assert stdin["user_message"] == "你好"
        assert stdin["trial_turn"] == 1
        assert stdin["task_id"] == "run-9" and stdin["data_id"] == "trial-1"
        assert stdin["dialog"][0]["content"] == "你好"  # 对话历史随轮携带

    def test_build_agent_script_minimal(self):
        script = build_agent_script(
            {"exec_type": "claude_agent", "instruction": "x", "timeout_seconds": 90})
        assert script == {
            "script_id": "sim-agent", "name": "simulation-agent",
            "exec_type": "claude_agent", "entrypoint": "x", "timeout_seconds": 90,
        }
        assert "allowed_tools" not in script  # 未配置不下沉空键


class TestExtractAgentReply:
    def test_shapes(self):
        ok = extract_agent_reply(
            {"status": "ok", "output": "回复文本", "files": {"b": 1, "a": 1},
             "finish_reason": "completed", "duration_ms": 5})
        assert ok["content"] == "回复文本"
        assert ok["tools"] == ["a", "b"]  # 排序保证确定性
        assert ok["meta"] == {"status": "ok", "finish_reason": "completed", "duration_ms": 5}
        # 结构体 output JSON 化 / 空输出给错误占位（模拟器要看到失败表态）
        structured = extract_agent_reply({"status": "ok", "output": {"k": 1}})
        assert '"k"' in structured["content"]
        empty = extract_agent_reply({"status": "fail", "error": "超时"})
        assert empty["content"].startswith("[agent 无输出]")
