"""仿真场景配置校验与归一化（M7 QE7-2）

仿真场景（simulation_scenario.config JSONB）的形态契约与发布门禁：

- agent：被测 Agent 定义——exec_type 白名单（claude_agent/deepseek_agent，
  复用既有 harness 拉起与镜像变体，见 harness_adapter.SANDBOX_HARNESS_VARIANTS）
  + 自然语言指令（entrypoint 契约）+ 单次调用超时；
- personas/combo_mode：用户模拟器配置（QE7-1 schema，见 user_simulator）；
- env：每 trial 重置时 staged 进工作区的初始文件（相对路径白名单）；
- verifiers：M2 终态校验器配置——**发布强制至少一个**（无终态校验的仿真
  不允许发布，强制 end-state verification 纪律，plans §M7 7.1）；形态校验
  复用 verifier_runner.validate_verifier_shape，script 存在性/active 查库
  校验留给发布服务层（与 evaluation_task_service._validate_verifiers 同分工）；
- faults：故障注入策略（QE7-3）——类型白名单 + 命中回合（config.turns，
  正整数数组、缺省第 2 回合）与类型参数边界归一化；执行拦截在 harness
  调用层（simulation.fault_injection.wrap_with_faults）；
- trials/max_turns/trial_soft_timeout_seconds：试验参数（trials 对齐
  pass_k 的 1..5 口径；max_turns 缺省 15 对齐方案 §7.1；软超时缺省
  1800s、上限 7200s 对齐 celery task_time_limit）。

``validate_scenario_config`` 为纯函数：非法配置抛 ValueError（API 发布
端点转 400），合法配置返回归一化 dict（越界值收敛到边界，缺省值补全），
回合控制器（turn_controller）按归一化形态消费。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from app.services.user_simulator import (
    COMBO_MODES,
    DEFAULT_MAX_TURNS,
    MAX_TURNS_LIMIT,
    validate_persona_configs,
)

# 试验参数边界
DEFAULT_TRIALS = 3
TRIALS_MIN = 1
TRIALS_MAX = 5  # 对齐 pass_k.TRIALS_MAX（行级多 trial 口径，plans §2.3）

# 单次 agent 调用超时（经 harness 沙箱执行；缺省对齐 harness_adapter 300s）
DEFAULT_AGENT_TIMEOUT_SECONDS = 300.0
AGENT_TIMEOUT_MIN = 30.0
AGENT_TIMEOUT_MAX = 3600.0

# trial 软超时（回合间检查，不打断进行中的回合；上限对齐 celery
# task_time_limit=7200s——整个任务含全部 trial 不得超 worker 硬限）
DEFAULT_TRIAL_SOFT_TIMEOUT_SECONDS = 1800
SOFT_TIMEOUT_MIN = 60
SOFT_TIMEOUT_MAX = 7200

# QE7-3 故障注入类型白名单（plans §M7 7.1：工具超时/错误响应/污染数据、
# 矛盾指令、上下文超长）。执行拦截在 harness 调用层
# （simulation.fault_injection.wrap_with_faults，回合控制器接线）。
FAULT_TYPES = ("tool_timeout", "tool_error", "dirty_data", "contradiction", "context_overflow")

# 场景配置顶层键（未知键拒绝，防配置面走私——与 verifiers 白名单纪律一致）
SCENARIO_KEYS = (
    "agent", "personas", "combo_mode", "max_turns", "trial_soft_timeout_seconds",
    "env", "verifiers", "faults", "trials",
)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    """数值收敛到边界（非法值回落缺省）"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(low, min(high, num))


def _validate_env(env: Any) -> Dict[str, str]:
    """校验 env.files：{相对路径: 文本内容}，路径不得越界（.. / 绝对路径拒绝）"""
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env 必须是对象")
    unknown = set(env) - {"files"}
    if unknown:
        raise ValueError(f"env 含未知键: {sorted(unknown)}（可用: files）")
    files = env.get("files") or {}
    if not isinstance(files, dict):
        raise ValueError("env.files 必须是 {相对路径: 文本内容} 对象")
    for path in files:
        text = str(path or "")
        if not text.strip() or text.startswith("/") or text.startswith("\\"):
            raise ValueError(f"env.files 路径必须是相对路径: {path!r}")
        parts = [p for p in text.replace("\\", "/").split("/") if p]
        if ".." in parts:
            raise ValueError(f"env.files 路径不得包含 ..: {path!r}")
    return {str(k): str(v) for k, v in files.items()}


def _normalize_fault_turns(value: Any, index: int) -> List[int]:
    """归一化故障命中回合：正整数或正整数列表 → 升序去重列表

    命中回合必须显式可枚举（缺省由 fault_injection.DEFAULT_FAULT_TURN
    兜底）——故障注入不引入随机源，固定 seed 可复现。
    """
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    turns: List[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or int(item) < 1:
            raise ValueError(
                f"faults 第 {index} 项 config.turns 必须是正整数（1 起回合号）数组: {value!r}")
        turns.append(int(item))
    return sorted(set(turns))


def _validate_faults(faults: Any) -> List[Dict[str, Any]]:
    """校验并归一化故障注入配置（形态 + 命中回合 + 类型参数边界，QE7-3 消费）"""
    from app.services.simulation.fault_injection import (
        DEFAULT_OVERFLOW_PAD_CHARS,
        OVERFLOW_PAD_MAX,
        OVERFLOW_PAD_MIN,
    )

    if faults is None:
        return []
    if not isinstance(faults, list):
        raise ValueError("faults 必须是故障策略数组")
    normalized: List[Dict[str, Any]] = []
    for i, fault in enumerate(faults):
        if not isinstance(fault, dict):
            raise ValueError(f"faults 第 {i} 项必须是对象")
        f_type = str(fault.get("type") or "").strip()
        if f_type not in FAULT_TYPES:
            raise ValueError(
                f"faults 第 {i} 项类型非法: {f_type!r}（可用: {', '.join(FAULT_TYPES)}）")
        config = fault.get("config")
        if config is not None and not isinstance(config, dict):
            raise ValueError(f"faults 第 {i} 项 config 必须是对象")
        config = dict(config or {})
        try:
            config["turns"] = _normalize_fault_turns(config.get("turns"), i)
        except ValueError as e:
            raise ValueError(f"faults 第 {i} 项配置非法: {e}") from e
        if f_type == "context_overflow":
            try:
                pad = int(config.get("pad_chars") or DEFAULT_OVERFLOW_PAD_CHARS)
            except (TypeError, ValueError):
                pad = DEFAULT_OVERFLOW_PAD_CHARS
            config["pad_chars"] = max(OVERFLOW_PAD_MIN, min(OVERFLOW_PAD_MAX, pad))
        normalized.append({"type": f_type, "config": config})
    return normalized


def validate_scenario_config(config: Any) -> Dict[str, Any]:
    """校验并归一化仿真场景配置（发布门禁；非法抛 ValueError）

    Returns:
        归一化配置 dict：agent（exec_type/instruction/timeout_seconds/
        allowed_tools?/harness_extra?）、personas（QE7-1 归一化 dict 列表）、
        combo_mode、max_turns、trial_soft_timeout_seconds、env.files、
        verifiers、faults、trials。
    """
    if not isinstance(config, dict):
        raise ValueError("场景配置必须是对象（simulation_scenario.config）")
    unknown = set(config) - set(SCENARIO_KEYS)
    if unknown:
        raise ValueError(f"场景配置含未知键: {sorted(unknown)}（可用: {', '.join(SCENARIO_KEYS)}）")

    # ---- agent：exec_type 白名单 + 指令文本（entrypoint 契约） ----
    from app.services.harness_adapter import SANDBOX_HARNESS_VARIANTS

    agent = config.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("场景配置缺少 agent（被测 Agent 定义）")
    exec_type = str(agent.get("exec_type") or "").strip()
    if exec_type not in SANDBOX_HARNESS_VARIANTS:
        raise ValueError(
            f"agent.exec_type 非法: {exec_type!r}（可用: {', '.join(sorted(SANDBOX_HARNESS_VARIANTS))}）")
    instruction = str(agent.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("agent.instruction 缺失（被测 agent 的自然语言指令，entrypoint 契约）")
    timeout = _clamp(
        agent.get("timeout_seconds"), AGENT_TIMEOUT_MIN, AGENT_TIMEOUT_MAX,
        DEFAULT_AGENT_TIMEOUT_SECONDS,
    )
    agent_out: Dict[str, Any] = {
        "exec_type": exec_type,
        "instruction": instruction,
        "timeout_seconds": timeout,
    }
    allowed_tools = agent.get("allowed_tools")
    if isinstance(allowed_tools, list) and allowed_tools:
        agent_out["allowed_tools"] = [str(t) for t in allowed_tools if str(t).strip()]
    harness_extra = agent.get("harness_extra")
    if isinstance(harness_extra, dict) and harness_extra:
        agent_out["harness_extra"] = harness_extra

    # ---- personas：QE7-1 引擎归一化（组合模式一并校验） ----
    combo_mode = str(config.get("combo_mode") or COMBO_MODES[0]).strip().lower()
    personas, error = validate_persona_configs(config.get("personas"), combo_mode)
    if error:
        raise ValueError(f"personas 配置非法: {error}")

    # ---- verifiers：发布强制至少一个终态校验器（end-state verification 纪律） ----
    verifiers = config.get("verifiers")
    if not isinstance(verifiers, list) or not verifiers:
        raise ValueError(
            "仿真场景必须至少声明一个终态校验器（verifiers 为空）——"
            "无终态校验的仿真不允许发布（end-state verification 纪律）")
    from app.services.verifier_runner import validate_verifier_shape

    try:
        validate_verifier_shape(verifiers)
    except ValueError as e:
        raise ValueError(f"verifiers 配置非法: {e}") from e

    # ---- 试验参数：越界收敛 ----
    max_turns = int(_clamp(config.get("max_turns"), 1, MAX_TURNS_LIMIT, DEFAULT_MAX_TURNS))
    soft_timeout = int(_clamp(
        config.get("trial_soft_timeout_seconds"),
        SOFT_TIMEOUT_MIN, SOFT_TIMEOUT_MAX, DEFAULT_TRIAL_SOFT_TIMEOUT_SECONDS,
    ))
    trials = int(_clamp(config.get("trials"), TRIALS_MIN, TRIALS_MAX, DEFAULT_TRIALS))

    return {
        "agent": agent_out,
        "personas": [asdict(p) for p in personas],
        "combo_mode": combo_mode,
        "max_turns": max_turns,
        "trial_soft_timeout_seconds": soft_timeout,
        "env": {"files": _validate_env(config.get("env"))},
        "verifiers": verifiers,
        "faults": _validate_faults(config.get("faults")),
        "trials": trials,
    }
