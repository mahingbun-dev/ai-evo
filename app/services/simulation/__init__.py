"""仿真评测域包（M7）

simulation 类型（第 9 种评测类型）的组成部分：

- :mod:`app.services.simulation.scenario` 场景配置校验与发布门禁
  （agent/persona/env/verifiers/faults/trials；verifiers 强制非空）
- :mod:`app.services.simulation.turn_controller` 回合控制器与环境重置
  （worker 内循环、软超时、停止信号、每 trial 工作区重置、终态校验、pass^k）
- :mod:`app.services.simulation.fault_injection` 故障注入（QE7-3：工具超时/
  错误响应/污染数据/矛盾指令/上下文超长，harness 调用层拦截）
- 用户模拟器（QE7-1）在 :mod:`app.services.user_simulator`（persona 引擎、
  下一轮消息生成、终止条件）

API/表/前端接线见后续工作项（plans §M7 7.2/7.3）。
"""
from app.services.simulation.fault_injection import (
    DEFAULT_CONTRADICTION_TEXT,
    DEFAULT_FAULT_TURN,
    REQUEST_FAULTS,
    RESPONSE_FAULTS,
    apply_request_faults,
    apply_response_faults,
    fault_fires,
    wrap_with_faults,
)
from app.services.simulation.scenario import (
    DEFAULT_TRIALS,
    DEFAULT_TRIAL_SOFT_TIMEOUT_SECONDS,
    FAULT_TYPES,
    TRIALS_MAX,
    TRIALS_MIN,
    validate_scenario_config,
)
from app.services.simulation.turn_controller import (
    SIM_AGENT_SCRIPT_KEY,
    TRIAL_REASON_AGENT_ERROR,
    TRIAL_REASON_SIMULATOR_ERROR,
    TRIAL_REASON_SOFT_TIMEOUT,
    TRIAL_REASON_STOPPED,
    build_agent_script,
    default_agent_call,
    extract_agent_reply,
    reset_trial_environment,
    run_simulation_trial,
    run_simulation_trials,
    tool_waste_rate,
)

__all__ = [
    "DEFAULT_CONTRADICTION_TEXT",
    "DEFAULT_FAULT_TURN",
    "DEFAULT_TRIALS",
    "DEFAULT_TRIAL_SOFT_TIMEOUT_SECONDS",
    "FAULT_TYPES",
    "REQUEST_FAULTS",
    "RESPONSE_FAULTS",
    "SIM_AGENT_SCRIPT_KEY",
    "TRIALS_MAX",
    "TRIALS_MIN",
    "TRIAL_REASON_AGENT_ERROR",
    "TRIAL_REASON_SIMULATOR_ERROR",
    "TRIAL_REASON_SOFT_TIMEOUT",
    "TRIAL_REASON_STOPPED",
    "apply_request_faults",
    "apply_response_faults",
    "build_agent_script",
    "default_agent_call",
    "extract_agent_reply",
    "fault_fires",
    "reset_trial_environment",
    "run_simulation_trial",
    "run_simulation_trials",
    "tool_waste_rate",
    "validate_scenario_config",
    "wrap_with_faults",
]
