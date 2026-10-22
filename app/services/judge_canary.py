"""裁判金丝雀自检（M2 QE2-4，Berkeley Checklist 空样本对抗项）

任务级开关 ``judge_canary=true``（judge/agent 类型）时，payload 生成后混入
约 2%（至少 1 行）合成行：**空内容**或**乱码**作为「模型输出」直接送裁判——
裁判若给高分（≥ 满分 70%，聚合口径 ``CANARY_HIGH_SCORE_RATIO``），说明
「裁判可被空/乱码欺骗」，结果页裁判治理面板出风险徽标。

行口径（与真实数据行严格区分）：
- canary 行 payload 带 ``_judge_canary=true`` 与 ``_canary_kind``
  （empty/garbage）内部键：执行器跳过被测模型调用与前处理/校验器/轨迹
  （合成行不消耗外部资源、不产生确定性证据噪声），直接进裁判评分；
- 空内容 canary 同样进裁判（跳过空值校验短路——空值短路 0 分测不出
  「裁判被欺骗」，对抗探针必须让裁判看到空内容本身）；
- canary 行不计入任务均分/行通过率/维度分/校验通过率/轨迹匹配率
  （聚合按 ``judge_canary`` 键整行剥离，单独成 ``judge_governance.canary`` 块）；
- 注入确定性：条数与内容按 payload 数量与序号推导，同任务重跑稳定
  （行级落库键 (task_id, execution_number, data_index) 不漂移，续跑幂等）。
"""
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 混入比例（方案口径 2%）；最少 1 行（小数据集也要有探针）
CANARY_RATIO = 0.02
CANARY_MIN_ROWS = 1

# 「高分」判定阈值：裁判分 ≥ 满分 × 该比例即视为被欺骗（结果页风险徽标）
CANARY_HIGH_SCORE_RATIO = 0.7

# 乱码样本（覆盖典型「无意义输出」形态：乱码字符/控制符样式/填充符）
GARBAGE_SAMPLES = (
    "锟斤拷烫烫烫锟斤拷烫烫烫锟斤拷烫烫烫锟斤拷烫烫拷",
    "######@@@@@@######@@@@######@@@@######@@@@######",
    "~~~^^^~~~^^^~~~^^^~~~^^^~~~^^^~~~^^^~~~^^^~~~^^^",
    "aaaa bbbb cccc aaaa bbbb cccc aaaa bbbb cccc aaaa",
)

# 消费该开关的评测类型：canary 探的是裁判，仅裁判参与计分的类型有意义
CANARY_SUPPORTED_TYPES = ("judge", "agent")


def canary_row_count(payload_count: int) -> int:
    """canary 注入条数：约 2%（四舍五入），最少 1 行；无数据 0 行"""
    if payload_count <= 0:
        return 0
    return max(CANARY_MIN_ROWS, round(payload_count * CANARY_RATIO))


def canary_content(kind: str, seq: int) -> str:
    """按类型与序号产出 canary「模型输出」内容（确定性）"""
    if kind == "empty":
        return ""
    return GARBAGE_SAMPLES[seq % len(GARBAGE_SAMPLES)]


def inject_canary_payloads(payloads: List[Dict[str, Any]], task) -> List[Dict[str, Any]]:
    """在真实 payload 列表后追加金丝雀合成行（M2 QE2-4）

    任务未开启 judge_canary / 类型不支持 / 空列表时原样返回（零行为变化）。
    合成行以首个真实 payload 为模板（保留输入变量形态，裁判 prompt 与真实行
    同构），行序号接续真实行（_orig_idx/idx 均为续号，行级落库键不与真实行
    冲突）。
    """
    if not payloads:
        return payloads
    if getattr(task, "evaluation_type", "") not in CANARY_SUPPORTED_TYPES:
        return payloads
    if not getattr(task, "judge_canary", False):
        return payloads

    count = canary_row_count(len(payloads))
    template = payloads[0]
    base_idx = len(payloads)
    # 交替 empty/garbage：每版两类探针都有（首行 empty——最经典的欺骗面）
    kinds = ["empty" if i % 2 == 0 else "garbage" for i in range(count)]
    for offset, kind in enumerate(kinds):
        seq = base_idx + offset
        # 模板拷贝保留 `_dataset_item`（裁判变量解析用）——探针行与真实行
        # 的 prompt 形态同构，唯一差异是「模型输出」被替换为空/乱码；其余
        # 下划线内部键（hub 拆分/续跑标记等）不复制
        canary_payload = {
            k: v for k, v in template.items()
            if not str(k).startswith("_") or k == "_dataset_item"
        }
        canary_payload.update({
            "idx": seq,
            "_orig_idx": seq,
            "_judge_canary": True,
            "_canary_kind": kind,
            "_canary_content": canary_content(kind, seq),
        })
        payloads.append(canary_payload)

    logger.info(
        f"[JudgeCanary] 注入 {count} 行金丝雀探针（kinds={kinds}），"
        f"payload 总数 {len(payloads)}"
    )
    return payloads
