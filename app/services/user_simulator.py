"""用户模拟器（M7 QE7-1，仿真评测的用户侧引擎）

仿真评测（simulation 类型，第 9 种评测类型）里扮演「真实用户」的引擎：
由裁判模型驱动，按 persona 配置与当前对话状态生成下一轮用户消息，并
给出目标/进展判定供终止条件消费。被测 Agent 的拉起、回合循环与环境
重置见 QE7-2 回合控制器，故障注入见 QE7-3——本模块只负责用户侧。

persona 配置（多 persona 组合，scenario config JSON 的一部分）::

    [
      {"name": "报销新人小王", "role": "首次使用报销系统的财务新人",
       "goal": "把 3580 元差旅报销单提交成功并拿到受理编号",
       "expertise": "novice",      # novice|intermediate|expert 或 0-1 数值
       "patience": 5,              # 可容忍的连续无进展回合数（1-15）
       "adversarial": 0.0,         # 对抗性 0-1（>0.6 会给矛盾指令）
       "style_hint": "口语化，爱打错别字"},   # 可选文风提示
    ]

组合模式（combo_mode）：

- ``single``（默认）：单 persona 用户；
- ``blend``：多 persona 合成同一个用户（多重身份），目标为全部子目标，
  目标判定回写所有 persona 的达成状态；
- ``rotate``：多个模拟用户轮流发言（如「报销员工 + 财务审核员」），
  目标已达成的 persona 退出轮换，全部达成才触发 goal_achieved。

终止条件（check_termination，优先级从高到低）：

1. ``goal_achieved``——全部 persona 目标达成（由模拟器逐轮判定
   goal_status，目标须实质达成，不接受客套性达成）；
2. ``give_up``——模拟器主动放弃（action=give_up），或确定性失控保护：
   连续无进展回合数 ≥ 全体 persona 最小耐心值；
3. ``max_turns``——用户消息轮数达到回合上限（默认 15，对齐方案 §7.1
   回合控制器缺省）。

调用协议：模拟器每轮必须输出单个 JSON 对象（message/goal_status/
progress/action），解析复用 ``extract_json_from_response``；解析失败
带纠偏提示重试一次，再失败或裁判通道异常抛 ``UserSimulatorError``
（由回合控制器按 trial 基础设施错误处理，不伪造成放弃/达成）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.services.response_parser import extract_json_from_response

logger = logging.getLogger(__name__)

# 回合上限：缺省 15（方案 §7.1 回合控制器 max_turns 默认 15），硬上限 50
DEFAULT_MAX_TURNS = 15
MAX_TURNS_LIMIT = 50

# persona 数量与耐心边界（防爆量：每轮每 persona 一次模型调用）
MAX_PERSONAS = 4
MIN_PATIENCE = 1
MAX_PATIENCE = 15
DEFAULT_PATIENCE = 5

# 组合模式白名单
COMBO_MODES = ("single", "blend", "rotate")

# 专业度档位（prompt 行为规则见 _EXPERTISE_RULES）
EXPERTISE_LEVELS = ("novice", "intermediate", "expert")
_DEFAULT_EXPERTISE = "intermediate"

# 终止原因
TERMINATION_GOAL = "goal_achieved"
TERMINATION_GIVE_UP = "give_up"
TERMINATION_MAX_TURNS = "max_turns"

#: 模拟器输出协议（写入 system prompt，与 parse_simulator_reply 对齐）
_SIMULATOR_PROTOCOL = (
    '{"message": "下一轮用户消息（用户口吻原文，不要旁白/引号包裹）", '
    '"goal_status": "achieved|pending（当前发言者目标是否已实质达成）", '
    '"progress": true/false（被测 Agent 上一轮回复是否让目标更接近）", '
    '"action": "speak|give_up（用户是否放弃）"}'
)

#: 专业度 → 模拟用户行为规则（写入 system prompt）
_EXPERTISE_RULES = {
    "novice": (
        "新手：只用大白话，术语不准确，不会一次性给全参数，关键信息（单号/"
        "金额/日期）会漏说或说错，需要被测 Agent 主动引导追问。"
    ),
    "intermediate": (
        "进阶用户：常规表达，能提供主要信息与参数，个别细节要被追问才补充。"
    ),
    "expert": (
        "专家：直接用领域术语，一次性给出精确参数与期望的处理方式，期望高效"
        "直接的回答，冗长客套会降低满意度。"
    ),
}

#: 对抗性分档规则（写入 system prompt；>0.6 档可给矛盾指令，QE7-3 复用）
def _adversarial_rule(level: float) -> str:
    if level <= 0:
        return "对抗性关闭：正常配合完成目标。"
    if level <= 0.3:
        return "轻度对抗：偶尔重复问题、要求解释为什么这么做。"
    if level <= 0.6:
        return "中度对抗：追问结论依据、质疑 Agent 的回答、要求二次确认细节。"
    return (
        "重度对抗：主动质疑与打断，可提出与先前消息矛盾的要求或信息（模拟"
        "难缠用户，检验 Agent 是否盲从），但不得要求违法/有害/越权操作。"
    )


class UserSimulatorError(Exception):
    """模拟器不可用（裁判通道异常/协议解析连续失败）"""


@dataclass
class SimulatedPersona:
    """单个模拟用户 persona（配置经归一化，可直接进 prompt）"""

    name: str
    role: str
    goal: str
    expertise: str = _DEFAULT_EXPERTISE
    patience: int = DEFAULT_PATIENCE
    adversarial: float = 0.0
    style_hint: str = ""

    @staticmethod
    def _normalize_expertise(value: Any) -> str:
        """归一化专业度：档位名（中英文）/0-1 数值 → 三档之一，非法回落 intermediate"""
        if isinstance(value, str) and value.strip():
            key = value.strip().lower()
            mapping = {
                "novice": "novice", "新手": "novice", "beginner": "novice",
                "intermediate": "intermediate", "进阶": "intermediate",
                "expert": "expert", "专家": "expert", "高级": "expert",
            }
            if key in mapping:
                return mapping[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            score = max(0.0, min(1.0, float(value)))
            if score < 0.34:
                return "novice"
            if score < 0.67:
                return "intermediate"
            return "expert"
        return _DEFAULT_EXPERTISE

    @classmethod
    def from_config(cls, config: Dict[str, Any], index: int = 0) -> "SimulatedPersona":
        """从 scenario config 的单个 persona dict 构建（越界值收敛到边界）"""
        name = str(config.get("name") or "").strip() or f"用户{index + 1}"
        patience_raw = config.get("patience", DEFAULT_PATIENCE)
        try:
            patience = int(patience_raw)
        except (TypeError, ValueError):
            patience = DEFAULT_PATIENCE
        patience = max(MIN_PATIENCE, min(MAX_PATIENCE, patience))
        adversarial_raw = config.get("adversarial", 0.0)
        try:
            adversarial = max(0.0, min(1.0, float(adversarial_raw)))
        except (TypeError, ValueError):
            adversarial = 0.0
        return cls(
            name=name,
            role=str(config.get("role") or "").strip() or "普通终端用户",
            goal=str(config.get("goal") or "").strip(),
            expertise=cls._normalize_expertise(config.get("expertise")),
            patience=patience,
            adversarial=adversarial,
            style_hint=str(config.get("style_hint") or "").strip(),
        )

    def describe(self) -> str:
        """persona 规格文本（写入 system prompt）"""
        lines = [
            f"- 姓名/称呼：{self.name}",
            f"- 角色设定：{self.role}",
            f"- 目标：{self.goal}",
            f"- 专业度：{self.expertise}——{_EXPERTISE_RULES[self.expertise]}",
            f"- 耐心：{self.patience} 回合（连续无进展超过该值将放弃）",
            f"- 对抗性：{self.adversarial:.1f}——{_adversarial_rule(self.adversarial)}",
        ]
        if self.style_hint:
            lines.append(f"- 文风提示：{self.style_hint}")
        return "\n".join(lines)


def validate_persona_configs(
    configs: Any, combo_mode: str = COMBO_MODES[0]
) -> Tuple[List[SimulatedPersona], str]:
    """校验并归一化 persona 配置列表

    Returns:
        (personas, error)：error 为空串表示通过。规则：非空列表、数量
        1..MAX_PERSONAS、每个 persona 必须有目标（无目标的模拟用户无法
        判定终止）、combo_mode 白名单。single 模式多于 1 个 persona 时
        只取第一个（宽容收敛，不阻断）。
    """
    mode = str(combo_mode or COMBO_MODES[0]).strip().lower()
    if mode not in COMBO_MODES:
        return [], f"不支持的 persona 组合模式: {combo_mode}，可选值: {list(COMBO_MODES)}"
    if not isinstance(configs, list) or not configs:
        return [], "persona 配置必须是非空列表"
    if len(configs) > MAX_PERSONAS:
        return [], f"persona 数量超过上限 {MAX_PERSONAS}"
    if not all(isinstance(c, dict) for c in configs):
        return [], "persona 配置项必须是对象"
    personas = [SimulatedPersona.from_config(c, i) for i, c in enumerate(configs)]
    missing = [p.name for p in personas if not p.goal]
    if missing:
        return [], f"persona 缺少目标（goal）配置: {', '.join(missing)}"
    if mode == "single" and len(personas) > 1:
        personas = personas[:1]
    return personas, ""


@dataclass
class SimulationDialogState:
    """一场仿真的对话状态（可序列化落 simulation_runs.trial 明细）

    回合口径：一条用户消息 + 一条 Agent 回复记 1 回合；``user_turn_count``
    即已发起的用户消息轮数。
    """

    turns: List[Dict[str, Any]] = field(default_factory=list)
    #: 各 persona 目标达成状态（rotate 按 persona 名分别记录；blend/single
    #: 判定结果写全部/单个键；终止要求全部为 True）
    goal_status: Dict[str, bool] = field(default_factory=dict)
    #: 连续无进展回合数（progress=False 连续计数，True/缺省清零）
    no_progress_rounds: int = 0
    #: 模拟器是否已宣布放弃（action=give_up）
    gave_up: bool = False
    user_turn_count: int = 0

    def record_user_turn(self, reply: Dict[str, Any]) -> None:
        """落一条模拟用户消息并更新目标/耐心账本

        reply 为 ``UserSimulator.next_message`` 的返回值（speaker/message/
        goal_status/goal_apply/progress/action）。goal_apply 声明本次
        goal_status 适用于哪些 persona（single/rotate=发言者，blend=全部，
        合成用户的整体判定）；缺省时按 speaker 归位。
        """
        speaker = str(reply.get("speaker") or "")
        achieved = bool(reply.get("goal_status"))
        self.turns.append({
            "role": "user",
            "speaker": speaker,
            "content": str(reply.get("message") or ""),
            "round": self.user_turn_count + 1,
            # QE7-4：progress/action 落回合明细——progress 描述「上一回合
            # agent 回复是否推进目标」（工具浪费率的判定输入），action 供回放
            "progress": reply.get("progress"),
            "action": reply.get("action") or "speak",
        })
        self.user_turn_count += 1
        keys = reply.get("goal_apply")
        if not isinstance(keys, list) or not keys:
            keys = [speaker] if speaker else list(self.goal_status)
        for key in keys:
            if key in self.goal_status:
                self.goal_status[key] = achieved
        if reply.get("action") == "give_up":
            self.gave_up = True
        if reply.get("progress") is False:
            self.no_progress_rounds += 1
        else:
            self.no_progress_rounds = 0

    def record_agent_reply(
        self,
        content: str,
        tools: Optional[List[str]] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """落被测 Agent 的回复（QE7-2 回合控制器在 harness 调用后写入）

        tools 为工具轨迹摘要（文件产物名等）；meta 携带执行元信息
        （status/finish_reason/duration_ms 等，供 trial 明细与回放展示）。
        """
        turn: Dict[str, Any] = {
            "role": "agent",
            "speaker": "agent",
            "content": str(content or ""),
            "round": self.user_turn_count,
        }
        if tools:
            turn["tools"] = [str(t) for t in tools]
        if isinstance(meta, dict) and meta:
            turn["meta"] = {k: v for k, v in meta.items() if v is not None}
        self.turns.append(turn)

    def pending_personas(self) -> List[str]:
        """目标未达成的 persona 名（rotate 轮换池）"""
        return [name for name, ok in self.goal_status.items() if not ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turns": self.turns,
            "goal_status": dict(self.goal_status),
            "no_progress_rounds": self.no_progress_rounds,
            "gave_up": self.gave_up,
            "user_turn_count": self.user_turn_count,
        }


def build_simulator_prompts(
    personas: List[SimulatedPersona],
    state: SimulationDialogState,
    active: SimulatedPersona,
    combo_mode: str = COMBO_MODES[0],
    max_turns: int = DEFAULT_MAX_TURNS,
) -> Tuple[str, str]:
    """构建模拟器 (system, user) 提示词（纯函数，固定输入 → 固定输出）"""
    if combo_mode == "blend":
        persona_text = "\n".join(p.describe() for p in personas)
        combo_text = (
            f"你同时兼具以上 {len(personas)} 个身份（同一个人），本轮消息要体现"
            "当前最相关的身份与目标；goal_status 只有当全部子目标实质达成才为 achieved。"
        )
    else:
        persona_text = active.describe()
        combo_text = "你只扮演上面这一个用户，不要扮演 Agent 或其他人。"

    system_prompt = (
        "你是 AI 仿真评测中的「模拟用户」引擎。你的任务不是帮忙答题，而是"
        "严格扮演指定 persona，与被测 Agent 多轮对话，推动（或对抗）其完成目标。\n"
        f"persona 设定：\n{persona_text}\n{combo_text}\n"
        "判定纪律：\n"
        "- goal_status 只在目标**实质达成**时为 achieved（拿到结果/单号/确认），"
        "Agent 只是客套、承诺、复述要求都不算；\n"
        "- progress 描述被测 Agent 上一轮回复是否让目标更接近（首条消息恒为 true）；\n"
        "- 目标无望或耐心耗尽时 action=give_up 并在 message 里以用户口吻告别。\n"
        "只输出一个 JSON 对象，不要输出其他内容："
        + _SIMULATOR_PROTOCOL
    )

    replay_lines = []
    for turn in state.turns:
        round_no = turn.get("round", "?")
        if turn.get("role") == "user":
            replay_lines.append(f"[第{round_no}轮][用户·{turn.get('speaker', '')}] {turn.get('content', '')}")
        else:
            replay_lines.append(f"[第{round_no}轮][Agent] {turn.get('content', '')}")
            for tool in turn.get("tools") or []:
                replay_lines.append(f"[第{round_no}轮][Agent 工具] {tool}")
    replay = "\n".join(replay_lines) if replay_lines else "（对话尚未开始——请发出第一条消息）"

    goals_text = "\n".join(
        f"- {name}: {'已达成' if ok else '未达成'}" for name, ok in state.goal_status.items()
    ) or "- 暂无目标状态"

    user_prompt = (
        f"【当前发言者】\n{active.name}\n\n"
        f"【对话回放】\n{replay}\n\n"
        f"【各 persona 目标状态】\n{goals_text}\n\n"
        f"【剩余预算】还可发起 {max(0, max_turns - state.user_turn_count)} 轮消息；"
        f"该 persona 耐心还剩 {max(0, active.patience - state.no_progress_rounds)} 回合。\n\n"
        "请以当前发言者口吻输出下一轮消息的 JSON。"
    )
    return system_prompt, user_prompt


def parse_simulator_reply(text: str) -> Dict[str, Any]:
    """解析模拟器回复为标准结构

    Returns:
        ``{"message": str, "goal_status": bool, "progress": Optional[bool],
        "action": "speak"|"give_up", "parse_error": Optional[str]}``；
        解析失败带 parse_error（调用方决定重试/报错）。speak 且 message
        为空视为解析失败；give_up 且 message 为空给默认告别语。
    """
    result: Dict[str, Any] = {
        "message": "",
        "goal_status": False,
        "progress": None,
        "action": "speak",
        "parse_error": None,
    }
    if not text or not str(text).strip():
        result["parse_error"] = "模拟器返回为空"
        return result

    data = extract_json_from_response(str(text))
    if not isinstance(data, dict):
        result["parse_error"] = "模拟器返回中未找到 JSON 对象"
        return result

    message = str(data.get("message") or "").strip()
    action = str(data.get("action") or "speak").strip().lower()
    if action not in ("speak", "give_up"):
        action = "speak"
    if not message:
        if action == "give_up":
            message = "算了，不需要了，谢谢。"
        else:
            result["parse_error"] = "message 字段缺失或为空"
            return result

    goal = data.get("goal_status")
    if isinstance(goal, bool):
        goal_status = goal
    elif isinstance(goal, str) and goal.strip().lower() == "achieved":
        goal_status = True
    else:
        goal_status = False

    progress = data.get("progress")
    if isinstance(progress, bool):
        result["progress"] = progress
    elif isinstance(progress, str) and progress.strip().lower() in ("true", "false"):
        result["progress"] = progress.strip().lower() == "true"

    result["message"] = message
    result["goal_status"] = goal_status
    result["action"] = action
    return result


def check_termination(
    state: SimulationDialogState, max_turns: int, min_patience: int = DEFAULT_PATIENCE
) -> Dict[str, Any]:
    """评估终止条件（纯函数）

    优先级：goal_achieved > give_up > max_turns。give_up 有两个来源：
    模拟器 action=give_up（state.gave_up）与确定性失控保护——连续无进展
    回合数 ≥ min_patience（全体 persona 最小耐心，防模拟器「永不放弃」）。

    Returns:
        ``{"terminated": bool, "reason": str, "detail": str}``；
        未终止时 reason 为空串。
    """
    if state.goal_status and all(state.goal_status.values()):
        achieved = "、".join(state.goal_status.keys())
        return {
            "terminated": True,
            "reason": TERMINATION_GOAL,
            "detail": f"全部 persona 目标已达成：{achieved}",
        }
    if state.gave_up:
        return {
            "terminated": True,
            "reason": TERMINATION_GIVE_UP,
            "detail": "模拟器主动放弃（action=give_up）",
        }
    if state.no_progress_rounds >= min_patience:
        return {
            "terminated": True,
            "reason": TERMINATION_GIVE_UP,
            "detail": (
                f"连续 {state.no_progress_rounds} 回合无进展，超过最小耐心 "
                f"{min_patience}（失控保护放弃）"
            ),
        }
    if state.user_turn_count >= max_turns:
        return {
            "terminated": True,
            "reason": TERMINATION_MAX_TURNS,
            "detail": f"用户消息轮数达到上限 {max_turns}",
        }
    return {"terminated": False, "reason": "", "detail": ""}


class UserSimulator:
    """裁判模型驱动的模拟用户（按 scenario persona 配置初始化）

    用法（QE7-2 回合控制器）::

        sim = UserSimulator(task, persona_configs, combo_mode="rotate", max_turns=15)
        state = sim.initial_state()
        while True:
            reply = sim.next_message(state)      # -> {speaker, message, ...}
            state.record_user_turn(reply)
            agent_reply = harness_call(reply["message"])
            state.record_agent_reply(agent_reply, tools)
            term = sim.check_termination(state)
            if term["terminated"]:
                break
    """

    def __init__(
        self,
        task: Any,
        persona_configs: List[Dict[str, Any]],
        combo_mode: str = COMBO_MODES[0],
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        personas, error = validate_persona_configs(persona_configs, combo_mode)
        if error:
            raise ValueError(error)
        self.task = task
        self.personas = personas
        self.combo_mode = combo_mode
        self.max_turns = max(1, min(MAX_TURNS_LIMIT, int(max_turns or DEFAULT_MAX_TURNS)))
        # 最小耐心：失控保护阈值（最没耐心的用户先放弃）
        self.min_patience = min(p.patience for p in self.personas)
        self._judge_client = None  # 惰性构建（首次发话时，mock 手法同 mutator）

    # ------------------------------------------------------------------
    # 状态与 persona 选择
    # ------------------------------------------------------------------
    def initial_state(self) -> SimulationDialogState:
        """构建初始对话状态（goal_status 按 persona 名初始化为未达成）"""
        state = SimulationDialogState()
        for persona in self.personas:
            state.goal_status[persona.name] = False
        return state

    def active_persona(self, state: SimulationDialogState) -> SimulatedPersona:
        """选出本轮发言 persona：single/blend 用首个；rotate 在未达成池轮换"""
        if self.combo_mode != "rotate" or len(self.personas) == 1:
            return self.personas[0]
        pending = [p for p in self.personas if not state.goal_status.get(p.name, False)]
        pool = pending or self.personas
        return pool[state.user_turn_count % len(pool)]

    def check_termination(self, state: SimulationDialogState) -> Dict[str, Any]:
        """按本 simulator 配置评估终止（透传纯函数）"""
        return check_termination(state, self.max_turns, self.min_patience)

    # ------------------------------------------------------------------
    # 下一轮用户消息
    # ------------------------------------------------------------------
    def next_message(self, state: SimulationDialogState) -> Dict[str, Any]:
        """按对话状态生成下一轮用户消息

        Returns:
            ``{"speaker", "message", "goal_status", "goal_apply", "progress",
            "action"}``（goal_apply 声明 goal_status 适用的 persona 键，
            blend 模式为全部）。解析失败带纠偏提示重试一次，仍失败抛
            ``UserSimulatorError``；裁判通道异常同样抛错（不伪装放弃）。
        """
        active = self.active_persona(state)
        system_prompt, user_prompt = build_simulator_prompts(
            self.personas, state, active, self.combo_mode, self.max_turns
        )
        parsed = self._call_and_parse(system_prompt, user_prompt)
        if parsed.get("parse_error"):
            # 纠偏重试：显式要求只输出协议 JSON
            retry_prompt = (
                f"{user_prompt}\n\n【纠偏】上一次输出无法解析（"
                f"{parsed['parse_error']}）。只输出一个 JSON 对象，格式：{_SIMULATOR_PROTOCOL}"
            )
            parsed = self._call_and_parse(system_prompt, retry_prompt)
            if parsed.get("parse_error"):
                raise UserSimulatorError(f"模拟器协议解析连续失败: {parsed['parse_error']}")
        if self.combo_mode == "blend":
            goal_apply = [p.name for p in self.personas]
        else:
            goal_apply = [active.name]
        reply = {
            "speaker": active.name,
            "message": parsed["message"],
            "goal_status": parsed["goal_status"],
            "goal_apply": goal_apply,
            "progress": parsed["progress"],
            "action": parsed["action"],
        }
        logger.info(
            "[仿真] 第 %d 轮用户消息（persona=%s, action=%s, goal=%s）",
            state.user_turn_count + 1, active.name, reply["action"], reply["goal_status"],
        )
        return reply

    def _call_and_parse(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """调用裁判模型并解析协议（异常上抛，由 next_message 统一处理）"""
        try:
            if self._judge_client is None:
                from app.services.judge_client import JudgeClient

                self._judge_client = JudgeClient(self.task)
            reply_text = self._judge_client.compress(system_prompt, user_prompt)
        except Exception as e:  # noqa: BLE001 - 通道不可用按 trial 错误上抛
            raise UserSimulatorError(f"模拟器裁判通道调用失败: {e}") from e
        return parse_simulator_reply(reply_text)
