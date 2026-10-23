"""故障注入（M7 QE7-3，harness 调用层拦截）

五类可选故障策略（plans §M7 7.1），全部在回合控制器的 agent 通道边界
（``wrap_with_faults`` 包装 ``AgentCall``）确定性拦截——命中回合由
``config.turns``（1 起用户回合号列表，缺省 ``[DEFAULT_FAULT_TURN]``）显式
声明，不引入随机源（对齐「固定 seed 可复现」纪律）：

请求侧（拦截发给被测 agent 的用户消息）：

- ``contradiction`` 矛盾指令（simulator 行为）——在用户消息后附加与先前
  要求矛盾的补充指令（``config.text`` 可配），检验 agent 是否盲从反复
  横跳的用户；
- ``context_overflow`` 上下文超长——消息尾部追加 ``config.pad_chars``
  （缺省 10 万字符、上限 200 万）填充块，模拟超长上下文输入；

响应侧（拦截 harness 返回的结果条目）：

- ``tool_timeout`` 工具超时——agent 本轮回复替换为「工具挂死超时」错误
  表态（``config.tool``/``config.timeout_seconds`` 可配）；
- ``tool_error`` 错误响应——回复替换为「工具返回错误响应」表态；
- ``dirty_data`` 返回污染数据——回复内容注入确定性乱码/截断污染块
  （保留头部 1/3 + 乱码标记 + 尾段倒序），模拟工具返回脏数据。

设计口径（为什么响应侧故障不做成真基础设施失败）：真超时/容器失败会被
回合控制器按 ``agent_error`` 短路（QE7-2 的基础设施保护），会话即刻终止，
无法考察「注入后仍达成目标」的恢复能力——故障注入的目的是给被测 agent
制造可恢复的逆境，因此工具超时/错误/污染统一为**响应边界模拟**：底层
harness 调用真实执行，命中回合的返回内容被改写为故障形态，会话继续，
恢复与否由终态校验判定（故障恢复率 = 注入命中的 trial 中通过的占比，
turn_controller 聚合）。真实基础设施失败（status != ok）不做故障改写，
按原样上抛。

命中事件逐回合记入结果条目 ``faults_fired``（[{type, turn, detail}]），
回合控制器汇总进 trial 明细（回放展示与故障恢复率统计的输入）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from app.services.simulation.turn_controller import AgentCall

logger = logging.getLogger(__name__)

# 缺省注入回合：第 2 回合（首轮通常是背景建立，第 2 轮注入最能考察恢复）
DEFAULT_FAULT_TURN = 2

# context_overflow 填充块参数（字符数，收敛边界）
DEFAULT_OVERFLOW_PAD_CHARS = 100_000
OVERFLOW_PAD_MIN = 1_000
OVERFLOW_PAD_MAX = 2_000_000

# tool_timeout 缺省口径（仅用于错误表态文本，不产生真实等待）
DEFAULT_TOOL_TIMEOUT_SECONDS = 10

#: 矛盾指令缺省话术（config.text 覆盖）
DEFAULT_CONTRADICTION_TEXT = (
    "等等，刚才说的不算——现在要求完全相反：不要按之前的方式处理了，"
    "按新的要求重来一遍。"
)

# 请求侧 / 响应侧故障分派（单一事实源）
REQUEST_FAULTS = ("contradiction", "context_overflow")
RESPONSE_FAULTS = ("tool_timeout", "tool_error", "dirty_data")


def fault_fires(fault: Dict[str, Any], turn_no: int) -> bool:
    """故障是否在本回合命中（turns 列表成员判定；缺省 [DEFAULT_FAULT_TURN]）"""
    turns = (fault.get("config") or {}).get("turns")
    if not isinstance(turns, list) or not turns:
        turns = [DEFAULT_FAULT_TURN]
    return turn_no in [int(t) for t in turns if isinstance(t, (int, float)) and not isinstance(t, bool)]


def apply_request_faults(
    message: str, faults: List[Dict[str, Any]], turn_no: int
) -> Tuple[str, List[Dict[str, Any]]]:
    """请求侧拦截：改写发给 agent 的用户消息（contradiction/context_overflow）"""
    fired: List[Dict[str, Any]] = []
    for fault in faults:
        if not fault_fires(fault, turn_no):
            continue
        f_type = fault.get("type")
        config = fault.get("config") or {}
        if f_type == "contradiction":
            text = str(config.get("text") or "").strip() or DEFAULT_CONTRADICTION_TEXT
            message = f"{message}\n\n[用户补充] {text}"
            fired.append({"type": f_type, "turn": turn_no, "detail": f"注入矛盾指令: {text[:60]}"})
        elif f_type == "context_overflow":
            try:
                pad = int(config.get("pad_chars") or DEFAULT_OVERFLOW_PAD_CHARS)
            except (TypeError, ValueError):
                pad = DEFAULT_OVERFLOW_PAD_CHARS
            pad = max(OVERFLOW_PAD_MIN, min(OVERFLOW_PAD_MAX, pad))
            unit = "（超长填充数据行）"
            filler = unit * (pad // len(unit) + 1)  # 按实际单元长度凑足 pad 字符
            message = f"{message}\n\n{filler}"
            fired.append({"type": f_type, "turn": turn_no, "detail": f"注入上下文超长填充（约 {pad} 字符）"})
    return message, fired


def _pollute_text(text: str) -> str:
    """确定性污染：保留头部 1/3 + 乱码标记 + 尾段倒序（同输入同输出）"""
    head = text[: max(1, len(text) // 3)]
    tail = text[len(text) // 3: len(text) // 3 + 24]
    return f"{head}\ufffd\ufffd\ufffd[DATA_CORRUPTED]{tail[::-1]}\ufffd\ufffd\ufffd"


def apply_response_faults(
    result: Dict[str, Any], faults: List[Dict[str, Any]], turn_no: int
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """响应侧拦截：改写 harness 返回条目（tool_timeout/tool_error/dirty_data）

    只改写成功调用（status == ok）——真实基础设施失败原样上抛（由回合
    控制器按 agent_error 短路，不与故障模拟混淆）。
    """
    fired: List[Dict[str, Any]] = []
    if str(result.get("status") or "") != "ok":
        return result, fired
    result = dict(result)
    output = result.get("output")
    output_text = output if isinstance(output, str) and output.strip() else str(result.get("summary") or "（本轮处理结果）")
    for fault in faults:
        if not fault_fires(fault, turn_no):
            continue
        f_type = fault.get("type")
        config = fault.get("config") or {}
        if f_type == "tool_timeout":
            tool = str(config.get("tool") or "").strip() or "工具调用"
            seconds = config.get("timeout_seconds") or DEFAULT_TOOL_TIMEOUT_SECONDS
            result["output"] = (
                f"[工具超时] {tool} 调用超过 {seconds} 秒未返回（挂死），本轮未能完成处理。"
            )
            fired.append({"type": f_type, "turn": turn_no, "detail": f"注入工具超时: {tool}"})
        elif f_type == "tool_error":
            tool = str(config.get("tool") or "").strip() or "工具调用"
            code = str(config.get("error_code") or "HTTP 500")
            result["output"] = (
                f"[工具错误] {tool} 返回错误响应（{code}：内部错误），本轮处理失败。"
            )
            fired.append({"type": f_type, "turn": turn_no, "detail": f"注入工具错误响应: {tool}（{code}）"})
        elif f_type == "dirty_data":
            result["output"] = _pollute_text(output_text)
            fired.append({"type": f_type, "turn": turn_no, "detail": "注入返回污染数据（乱码/截断）"})
    return result, fired


def wrap_with_faults(agent_call: AgentCall, faults: List[Dict[str, Any]]) -> AgentCall:
    """给 agent 通道包上故障注入层（faults 为空时原样返回）

    拦截点即 harness 调用层：请求侧改写用户消息 → 真实调用 → 响应侧
    改写结果条目；命中事件汇总进 ``result["faults_fired"]``（回合控制
    器弹出记入 trial 明细，meta 同步给回放）。
    """
    if not faults:
        return agent_call

    def call(user_message: str, state: Any, turn_no: int) -> Dict[str, Any]:
        message, request_fired = apply_request_faults(user_message, faults, turn_no)
        result = agent_call(message, state, turn_no)
        result, response_fired = apply_response_faults(result, faults, turn_no)
        fired = request_fired + response_fired
        if fired:
            result["faults_fired"] = fired
            for event in fired:
                logger.info(
                    "[仿真] 第 %d 回合命中故障注入 %s: %s",
                    turn_no, event["type"], event["detail"])
        return result

    return call
