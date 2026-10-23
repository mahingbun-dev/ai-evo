"""回合控制器（M7 QE7-2，仿真评测的 worker 内循环）

一场 trial 的执行序（plans §M7 7.1）：

1. **环境重置**——每 trial 重建工作区（``uploads/script_runs/{run_id}/
   trial-{n}/``：残留 rmtree 后经 build_run_dir 重建，staged 场景 env.files
   初始文件）；被测 agent 每轮经 ``execute_harness_in_container`` 在全新
   容器内拉起（docker run --rm，复用 script-runner 通道与 SandboxDriver），
   工作区在 trial 内跨轮持久（agent 第 N 轮写的文件第 N+1 轮可见）、
   trial 间隔离（重置）；
2. **回合循环**——模拟器发话（QE7-1 UserSimulator）→ agent 响应（经
   harness 沙箱，stdin 携带 user_message 与对话历史）→ 状态更新 →
   终止条件评估（goal_achieved/give_up/max_turns，QE7-1 check_termination）；
3. **保护**——回合间检查软超时（``trial_soft_timeout_seconds``，缺省
   1800s；不打断进行中的回合，故名「软」）与任务停止信号（复用
   RedisStopSignalChecker 的 is_set 协议，worker 侧传入）；agent 步骤
   基础设施失败（容器/超时）短路 trial（reason=agent_error，不烧完回合）；
   QE7-3 故障注入在 agent 通道边界拦截（fault_injection.wrap_with_faults，
   命中事件汇总进 trial 明细 faults_fired）；
4. **终态校验**——循环结束后对最终工作区与最终对话跑 M2 校验器
   （run_verifiers；file_assert 断言路径以 trial 工作区为根，agent cwd
   前缀 ``sim-agent/``）。

trial「通过」口径（与 M2 pass^k 的终态校验优先纪律一致）：终态校验
status == pass，且终止原因不是基础设施/人为中止（stopped/agent_error/
simulator_error——这些情形即使断言碰巧通过也判不过，避免把半途中止
计成成功；soft_timeout 时终态已可校验，断言通过即通过）。

多 trial（run_simulation_trials）串行执行，trial 通过序列经
pass_k.trial_pass_stats 出 pass@1 / pass^k（k 次 trial 全过），并聚合
平均回合数——供 simulation 类型结果页指标卡（任务成功率 = pass@1）。
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.services.user_simulator import UserSimulator, UserSimulatorError
from app.utils.path_safety import PathEscapeError, ensure_within

logger = logging.getLogger(__name__)

# 循环外追加的 trial 终止原因（QE7-1 三条件之外的保护性中止）
TRIAL_REASON_STOPPED = "stopped"
TRIAL_REASON_SOFT_TIMEOUT = "soft_timeout"
TRIAL_REASON_AGENT_ERROR = "agent_error"
TRIAL_REASON_SIMULATOR_ERROR = "simulator_error"

# 基础设施/人为中止：即使终态断言通过也判不过（见模块 docstring 口径）
NON_PASSED_REASONS = (TRIAL_REASON_STOPPED, TRIAL_REASON_AGENT_ERROR, TRIAL_REASON_SIMULATOR_ERROR)

# harness 工作区内被测 agent 的 cwd 目录名（script_id）——execute_harness_
# in_container 以 script_id 建 cwd；file_assert 断言路径以 trial 工作区为根，
# agent 产物即 ``sim-agent/...`` 前缀（回放/断言路径契约单一事实源）
SIM_AGENT_SCRIPT_KEY = "sim-agent"

# 停止信号协议：is_set() -> bool（RedisStopSignalChecker 兼容；测试注入替身）
StopSignal = Any

# agent 调用协议：(user_message, state, turn_no) -> pre_script_results 条目
AgentCall = Callable[[str, Any, int], Dict[str, Any]]


def tool_waste_rate(turns: List[Dict[str, Any]], termination_reason: str) -> Optional[float]:
    """工具浪费率（M7 §7.2 指标）：无进展回合的工具调用占比

    口径：agent 第 r 回合的进展判定取**下一次**用户消息的 progress 字段
    （QE7-1 协议——模拟器在第 r+1 条消息里评价第 r 回合 agent 回复是否
    推进目标）；末回合无后续用户消息，按终止原因兜底（goal_achieved =
    有效，其余视为无效）。progress 缺省（None，协议宽容口径）按有效计，
    不虚增浪费。无任何工具调用时返回 None（不虚构分母）。
    """
    user_by_round = {
        t.get("round"): t for t in turns if t.get("role") == "user"
    }
    total_tools = 0
    wasted_tools = 0
    for turn in turns:
        if turn.get("role") != "agent":
            continue
        tools = len(turn.get("tools") or [])
        if not tools:
            continue
        total_tools += tools
        nxt = user_by_round.get(int(turn.get("round", 0)) + 1)
        if nxt is not None:
            productive = nxt.get("progress") is not False
        else:
            productive = termination_reason == "goal_achieved"
        if not productive:
            wasted_tools += tools
    if total_tools == 0:
        return None
    return round(wasted_tools / total_tools, 4)


def reset_trial_environment(
    run_id: str, trial_data_id: str, env_files: Optional[Dict[str, str]] = None
) -> Path:
    """每 trial 环境重置：清残留 → 重建工作区 → staged 初始文件

    工作区 ``uploads/script_runs/{run_id}/{trial_data_id}/``（与
    verifier_runner.run_verifiers 的 build_run_dir 同根：终态校验直接
    断言该工作区）。上一 trial 的残留（agent 产物/日志/staged 三件套）
    rmtree 清空，保证 trial 间环境隔离；env.files 按 D5 工作区语义落盘
    （相对路径，越界拒绝）。
    """
    from app.services.pre_judge_runner import SCRIPT_RUN_BASE, build_run_dir

    run_dir_path = Path(ensure_within(SCRIPT_RUN_BASE, str(run_id), str(trial_data_id)))
    if run_dir_path.exists():
        shutil.rmtree(run_dir_path, ignore_errors=True)
    run_dir = Path(build_run_dir(str(run_id), str(trial_data_id)))
    for rel_path, content in (env_files or {}).items():
        target = Path(ensure_within(str(run_dir), str(rel_path)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return run_dir


def build_agent_script(agent_config: Dict[str, Any]) -> Dict[str, Any]:
    """把场景 agent 配置组装成 pre_judge_runner.execute_script 的 script 形态

    exec_type=claude_agent/deepseek_agent → 沙箱 harness 通道
    （_execute_harness_agent → execute_harness_in_container，全新容器）。
    """
    script: Dict[str, Any] = {
        "script_id": SIM_AGENT_SCRIPT_KEY,
        "name": "simulation-agent",
        "exec_type": agent_config["exec_type"],
        "entrypoint": agent_config["instruction"],
        "timeout_seconds": int(agent_config.get("timeout_seconds") or 300),
    }
    if agent_config.get("allowed_tools"):
        script["allowed_tools"] = agent_config["allowed_tools"]
    if agent_config.get("harness_extra"):
        script["harness_extra"] = agent_config["harness_extra"]
    return script


def extract_agent_reply(result: Dict[str, Any]) -> Dict[str, Any]:
    """从 harness 执行条目提取对话侧所需字段（content/tools/meta）

    content 优先 output（str），其次 summary；output 为结构体时 JSON 化
    截断；全空时给错误占位（模拟器需要看到 agent 的失败表态）。
    tools 取 files 产物键（工具轨迹摘要，QE7-3 故障注入后同口径回收）。
    """
    result = result or {}
    output = result.get("output")
    if isinstance(output, str) and output.strip():
        content = output
    elif str(result.get("summary") or "").strip():
        content = str(result["summary"])
    elif output is not None:
        content = json.dumps(output, ensure_ascii=False, default=str)[:2000]
    else:
        content = f"[agent 无输出] {result.get('error') or result.get('finish_reason') or '未知'}"
    return {
        "content": content,
        "tools": sorted(str(k) for k in (result.get("files") or {})),
        "meta": {
            "status": result.get("status"),
            "finish_reason": result.get("finish_reason"),
            "duration_ms": result.get("duration_ms"),
        },
    }


class _VerifierTaskStub:
    """run_verifiers 的任务替身：终态校验读 verifiers/id 两属性"""

    def __init__(self, run_id: str, verifiers: List[Dict[str, Any]]):
        self.id = run_id
        self.verifiers = verifiers


def default_agent_call(
    agent_config: Dict[str, Any], run_id: str, trial_data_id: str, run_dir: Path
) -> AgentCall:
    """默认 agent 通道：经 pre_judge_runner.execute_script 走沙箱 harness

    stdin 契约（容器内 harness_entry staged 为 stdin.json，instruction
    指导 agent 读取）：task_id/data_id/_run_dir（工作区挂载语义）、
    kind=simulation、user_message（本轮用户消息）、trial_turn、dialog
    （对话历史）。每轮一个全新容器（docker run --rm），工作区跨轮持久。
    """
    from app.services import pre_judge_runner

    script = build_agent_script(agent_config)

    def call(user_message: str, state: Any, turn_no: int) -> Dict[str, Any]:
        stdin: Dict[str, Any] = {
            "task_id": str(run_id),
            "data_id": str(trial_data_id),
            "_run_dir": str(run_dir),
            "kind": "simulation",
            "trial_turn": turn_no,
            "user_message": user_message,
            "dialog": state.to_dict()["turns"] if hasattr(state, "to_dict") else [],
        }
        return pre_judge_runner.execute_script(
            script, stdin, timeout_seconds=script["timeout_seconds"])

    return call


def _dialog_text(state: Any) -> str:
    """终态校验的 model_output：最终对话回放文本（script 校验器可断言）"""
    turns = state.to_dict().get("turns", []) if hasattr(state, "to_dict") else []
    lines = []
    for turn in turns:
        if turn.get("role") == "user":
            lines.append(f"用户·{turn.get('speaker', '')}: {turn.get('content', '')}")
        else:
            lines.append(f"Agent: {turn.get('content', '')}")
    return "\n".join(lines)


def run_simulation_trial(
    scenario: Dict[str, Any],
    task: Any,
    *,
    run_id: str,
    trial_index: int,
    stop_signal: Optional[StopSignal] = None,
    agent_call: Optional[AgentCall] = None,
    simulator: Optional[UserSimulator] = None,
    now_fn: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """执行单个 trial：环境重置 → 回合循环 → 终态校验

    Args:
        scenario: validate_scenario_config 归一化产物
        task: 任务对象（UserSimulator 经 JudgeClient 读裁判配置）
        run_id: 运行标识（工作区根目录名与校验 task.id）
        trial_index: 0 起 trial 序号
        stop_signal: 停止信号（is_set 协议，RedisStopSignalChecker；None 不检查）
        agent_call: agent 通道（默认经沙箱 harness；测试注入替身）
        simulator: 模拟器（默认按场景 personas 构建 UserSimulator；测试注入替身）
        now_fn: 时钟（软超时判定；测试注入受控时钟）

    Returns:
        trial 结果 dict：{trial, passed, termination:{reason,detail}, turns
        （完整对话）, agent:{calls,tool_count}, verification, duration_ms}。
    """
    from app.services.verifier_runner import STATUS_PASS, run_verifiers

    started_wall = time.time()
    trial_no = trial_index + 1
    trial_data_id = f"trial-{trial_no}"
    soft_timeout = scenario["trial_soft_timeout_seconds"]
    env_files = (scenario.get("env") or {}).get("files") or {}

    # 1) 环境重置（残留清空 + 初始文件 staged）
    run_dir = reset_trial_environment(run_id, trial_data_id, env_files)

    sim = simulator or UserSimulator(
        task, scenario["personas"], combo_mode=scenario["combo_mode"],
        max_turns=scenario["max_turns"],
    )
    agent = agent_call or default_agent_call(scenario["agent"], run_id, trial_data_id, run_dir)
    # QE7-3 故障注入：harness 调用层拦截（请求侧改写消息/响应侧改写结果），
    # 注入与默认通道同一生效面——注入的 agent 替身同样被包装
    faults = scenario.get("faults") or []
    if faults:
        from app.services.simulation.fault_injection import wrap_with_faults

        agent = wrap_with_faults(agent, faults)
    state = sim.initial_state()

    deadline = now_fn() + soft_timeout
    reason, detail = "", ""
    agent_calls = 0
    tool_count = 0
    faults_fired: List[Dict[str, Any]] = []

    while True:
        # 保护一：任务停止信号（回合间检查，复用 Redis 停止信号）
        if stop_signal is not None and stop_signal.is_set():
            reason, detail = TRIAL_REASON_STOPPED, "收到任务停止信号，trial 中止"
            break
        # 保护二：软超时（回合间检查，不打断进行中的回合）
        if now_fn() >= deadline:
            reason, detail = (
                TRIAL_REASON_SOFT_TIMEOUT,
                f"trial 软超时（>{soft_timeout:.0f}秒），回合间中止",
            )
            break

        # 模拟器发话（QE7-1；协议连续失败按基础设施错误短路）
        try:
            reply = sim.next_message(state)
        except UserSimulatorError as e:
            reason, detail = TRIAL_REASON_SIMULATOR_ERROR, str(e)
            break
        state.record_user_turn(reply)

        # agent 响应（经 harness 沙箱；每轮全新容器；faults 命中时被拦截改写）
        result = agent(reply["message"], state, state.user_turn_count)
        agent_calls += 1
        fired = result.pop("faults_fired", []) if isinstance(result, dict) else []
        if fired:
            faults_fired.extend(fired)
        extracted = extract_agent_reply(result)
        tool_count += len(extracted["tools"])
        if fired:
            extracted["meta"]["faults"] = fired
        state.record_agent_reply(
            extracted["content"], extracted["tools"], extracted["meta"])
        if str(result.get("status") or "") != "ok":
            # 基础设施失败（容器拉不起/超时/退出码非 0）短路 trial——
            # 模拟器继续对话只会烧无效回合，直接暴露给编排层重试
            reason = TRIAL_REASON_AGENT_ERROR
            detail = f"agent 步骤失败: {str(result.get('error') or result.get('finish_reason'))[:400]}"
            break

        # 终止条件（goal_achieved / give_up / max_turns，QE7-1）
        term = sim.check_termination(state)
        if term["terminated"]:
            reason, detail = term["reason"], term["detail"]
            break

    # 终态校验（对最终工作区与最终对话；场景发布已强制 verifiers 非空）
    verification = run_verifiers(
        _VerifierTaskStub(run_id, scenario["verifiers"]),
        payload={
            "_dataset_item": {"id": trial_data_id, "data": {}},
            "idx": trial_data_id,
            "model_output": _dialog_text(state),
        },
        model_output=_dialog_text(state),
    )
    verification_status = (verification or {}).get("status")
    passed = verification_status == STATUS_PASS and reason not in NON_PASSED_REASONS

    turns_snapshot = state.to_dict()
    return {
        "trial": trial_no,
        "passed": passed,
        "termination": {"reason": reason, "detail": detail},
        "turns": turns_snapshot,
        "agent": {"calls": agent_calls, "tool_count": tool_count},
        "tool_waste_rate": tool_waste_rate(turns_snapshot.get("turns", []), reason),
        "faults_fired": faults_fired,
        "verification": verification,
        "duration_ms": int((time.time() - started_wall) * 1000),
    }


def run_simulation_trials(
    scenario: Dict[str, Any],
    task: Any,
    *,
    run_id: str,
    stop_signal: Optional[StopSignal] = None,
    agent_call_factory: Optional[Callable[[int], AgentCall]] = None,
    simulator_factory: Optional[Callable[[int], UserSimulator]] = None,
    now_fn: Callable[[], float] = time.monotonic,
    on_trial_done: Optional[Callable[[int, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """串行执行全部 trial 并出 pass^k（worker 侧入口，QE7-x 接线调用）

    trial 间检查停止信号（收到即不再开新 trial，已完成的 trial 结果保留）；
    每 trial 独立环境重置（reset_trial_environment）。指标口径：
    任务成功率 = pass@1（trial 通过率均值）、pass^k = k 次 trial 全过
    （pass_k.trial_pass_stats，τ-bench 语义）、平均回合数 = 用户消息轮数均值、
    工具浪费率 = 逐 trial 浪费率均值（无工具调用的 trial 不进分母）、
    故障恢复率 = 注入命中的 trial 中通过（终态校验过）的占比（无命中时为
    None，不虚构分母）。

    agent_call_factory/simulator_factory：按 trial 序号（0 起）构建通道/
    模拟器（缺省逐 trial 新建——模拟器 JudgeClient 惰性持有可复用，但工厂
    注入让测试与 QE7-3 故障注入拦截有明确挂点）。on_trial_done：每完成
    一个 trial 回调 (完成数, 该 trial 结果)——QE7-4 进度推送挂点（WS 进度
    通道经 service._publish_run_progress）。
    """
    trials_result: List[Dict[str, Any]] = []
    for index in range(scenario["trials"]):
        if stop_signal is not None and stop_signal.is_set():
            logger.info("[仿真] run %s 收到停止信号，不再开启新 trial（已完成 %d）", run_id, index)
            break
        trial_result = run_simulation_trial(
            scenario, task, run_id=run_id, trial_index=index,
            stop_signal=stop_signal,
            agent_call=agent_call_factory(index) if agent_call_factory else None,
            simulator=simulator_factory(index) if simulator_factory else None,
            now_fn=now_fn,
        )
        trials_result.append(trial_result)
        if on_trial_done is not None:
            try:
                on_trial_done(len(trials_result), trial_result)
            except Exception as e:  # noqa: BLE001 - 进度回调失败不阻断仿真
                logger.warning("[仿真] run %s 进度回调失败（忽略）: %s", run_id, e)

    from app.services.pass_k import trial_pass_stats

    passes = [bool(t["passed"]) for t in trials_result]
    stats = trial_pass_stats(passes)
    turns = [t["turns"].get("user_turn_count", 0) for t in trials_result]
    # 故障恢复率（QE7-3）：注入命中的 trial 里终态仍达标的占比
    fired_trials = [t for t in trials_result if t.get("faults_fired")]
    fault_recovery_rate = (
        round(sum(1 for t in fired_trials if t["passed"]) / len(fired_trials), 4)
        if fired_trials else None
    )
    # 工具浪费率（M7 §7.2）：逐 trial 浪费率均值（无工具调用的 trial 不进分母）
    waste_rates = [t["tool_waste_rate"] for t in trials_result
                   if t.get("tool_waste_rate") is not None]
    return {
        "trials": trials_result,
        "pass_k": stats,
        "task_success_rate": stats["pass_at_1"],
        "average_turns": round(sum(turns) / len(turns), 2) if turns else 0.0,
        "tool_waste_rate": (
            round(sum(waste_rates) / len(waste_rates), 4) if waste_rates else None),
        "fault_recovery_rate": fault_recovery_rate,
    }
