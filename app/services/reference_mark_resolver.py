"""引用标记解析器（@ 引用机制）

在全局评分标准（score_details）与维度等级描述（evaluation_metrics[].score_criteria
[等级].desc）中支持以引用标记嵌入外部内容，由执行链路在组装裁判 Prompt 前统一解析：

- 标记语法：
    {{ref:target:<target_id>|<显示名>}}   引用某个待评测数据（evaluation_targets 行）
    {{ref:var:<变量key>|<变量名>}}         引用某个裁判变量（judge_variable_config 解析结果）
- 解析顺序：优先按 id/key 精确匹配；未命中按显示名/变量名兜底（target 在上下文
  的 name 字段里找，var 直接把标记里的名称当 key 再查一次）；仍未命中→替换为
  空串并记 warning 日志（提交时由校验层拒绝失效引用，运行时兜底不抛错）。
- 注入格式：命中后替换为「【<显示名>】\\n<内容>」；显示名优先取标记内声明的名称，
  标记未写名称时回退用命中条目的名称（target）或 key（var）。

契约（共享设计契约 v3 第「一」节）：
- extract_reference_marks(text)：提取全部标记
- resolve_reference_marks(text, ctx)：就地解析替换，返回新字符串
- resolve_task_ref_overrides(task, ctx)：深拷贝任务配置并解析标记，产出
  {score_details, evaluation_metrics} 覆盖件（无标记时也返回副本，不回传引用）
"""
import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 引用标记正则：{{ref:target:<id>|<显示名>}} / {{ref:var:<key>|<变量名>}}
# 显示名/变量名段可整体省略（?:\|([^}]*))? 允许空名）
REFERENCE_MARK_PATTERN = re.compile(r"\{\{ref:(target|var):([^|}]+)(?:\|([^}]*))?\}\}")

# 标记种类常量（与正则第 1 捕获组对应）
KIND_TARGET = "target"
KIND_VAR = "var"

__all__ = [
    "REFERENCE_MARK_PATTERN",
    "KIND_TARGET",
    "KIND_VAR",
    "extract_reference_marks",
    "resolve_reference_marks",
    "resolve_task_ref_overrides",
]


def extract_reference_marks(text: Optional[str]) -> List[Dict[str, str]]:
    """提取文本中的全部引用标记

    Args:
        text: 待提取文本（None/非字符串返回空列表）

    Returns:
        标记列表，每项 {kind: 'target'|'var', key: id或变量key, name: 显示名, raw: 标记原文}，
        按出现顺序排列
    """
    if not text or not isinstance(text, str):
        return []

    marks = []
    for match in REFERENCE_MARK_PATTERN.finditer(text):
        kind, key, name = match.group(1), match.group(2), match.group(3)
        marks.append({
            "kind": kind,
            "key": key,
            "name": (name or "").strip(),
            "raw": match.group(0),
        })
    return marks


def resolve_reference_marks(text: Optional[str], ctx: Optional[Dict[str, Any]]) -> Any:
    """解析文本中的引用标记并就地替换

    Args:
        text: 待解析文本（None/非字符串原样返回）
        ctx: 解析上下文 {targets: {id: {name, content}}, vars: {key: value}}，
             两段均可缺省（按空处理）

    Returns:
        替换完成后的新字符串；未命中标记替换为空串（不抛错，记 warning 日志）
    """
    if not text or not isinstance(text, str):
        return text
    if not extract_reference_marks(text):
        return text

    ctx = ctx or {}
    targets_ctx = ctx.get("targets") or {}
    vars_ctx = ctx.get("vars") or {}

    def _replace(match: "re.Match") -> str:
        kind = match.group(1)
        key = match.group(2)
        name = (match.group(3) or "").strip()
        raw = match.group(0)

        content: Optional[str] = None
        resolved_name = name
        if kind == KIND_TARGET:
            # 优先按 id 精确匹配；未命中按显示名兜底
            entry = targets_ctx.get(key)
            if not isinstance(entry, dict):
                entry = _find_target_by_name(targets_ctx, name)
            if isinstance(entry, dict):
                content = _stringify_value(entry.get("content"))
                resolved_name = name or _stringify_value(entry.get("name")) or key
        else:
            # var：优先按 key 精确匹配；未命中把标记里的变量名当 key 再查一次
            if key in vars_ctx:
                content = _stringify_value(vars_ctx.get(key))
                resolved_name = name or key
            elif name and name in vars_ctx:
                content = _stringify_value(vars_ctx.get(name))
                resolved_name = name

        if content is None:
            # 运行时兜底：失效引用置空串，由提交时校验层负责拒绝
            logger.warning(
                f"[RefMarks] 引用标记未命中已置空: {raw}（kind={kind}, key={key}, name={name}）"
            )
            return ""

        # 注入格式：【显示名】\n内容
        return f"【{resolved_name}】\n{content}"

    return REFERENCE_MARK_PATTERN.sub(_replace, text)


def resolve_task_ref_overrides(task: Any, ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """深拷贝任务配置并解析其中的引用标记，产出执行链路覆盖件

    处理全局 score_details、每个 evaluation_metrics[].description（维度定义）
    与 evaluation_metrics[].score_criteria[等级].desc 三类字段。

    Args:
        task: 评测任务（EvaluationTask ORM 对象或 dict，取 score_details /
            evaluation_metrics 两个字段）
        ctx: 解析上下文，同 resolve_reference_marks

    Returns:
        {score_details: 深拷贝并解析后的文本, evaluation_metrics: 深拷贝并解析后的列表}；
        无标记时同样返回副本（下游可安全修改，不影响任务原对象）
    """
    score_details = copy.deepcopy(_get_task_field(task, "score_details"))
    evaluation_metrics = copy.deepcopy(_get_task_field(task, "evaluation_metrics"))

    new_score_details = resolve_reference_marks(score_details, ctx)

    if isinstance(evaluation_metrics, list):
        for metric in evaluation_metrics:
            if not isinstance(metric, dict):
                continue
            if isinstance(metric.get("description"), str):
                metric["description"] = resolve_reference_marks(metric["description"], ctx)
            score_criteria = metric.get("score_criteria")
            if not isinstance(score_criteria, dict):
                continue
            for level_info in score_criteria.values():
                if isinstance(level_info, dict) and isinstance(level_info.get("desc"), str):
                    level_info["desc"] = resolve_reference_marks(level_info["desc"], ctx)

    return {
        "score_details": new_score_details,
        "evaluation_metrics": evaluation_metrics,
    }


# ---------------- 内部工具 ----------------

def _find_target_by_name(targets_ctx: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """在目标上下文中按显示名兜底查找（返回首个 name 匹配的条目）"""
    if not name:
        return None
    for entry in targets_ctx.values():
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _stringify_value(value: Any) -> str:
    """把引用命中的内容安全转为字符串（风格对齐 JudgeVariableResolver._safe_string）

    dict/list 序列化为 JSON 文本，布尔统一小写，None 视为未命中交由上层置空。
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return _json_dump(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _json_dump(value: Any) -> str:
    """JSON 序列化（ensure_ascii=False + default=str 容错），失败回退 str()"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _get_task_field(task: Any, field_name: str) -> Any:
    """从任务对象取字段：ORM 对象走属性，dict 走键"""
    if isinstance(task, dict):
        return task.get(field_name)
    return getattr(task, field_name, None)
