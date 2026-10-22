"""插件评测服务（评测类型 plugin）

评测动作**实时加载自插件市场**（默认）：市场里的 plugin-eval 是评测动作本身，它同样在
演进，所以每次评测都经 `plugin_eval_market_runner` 以子进程执行市场克隆里的最新版本
（评测器版本随市场懒加载更新），结果里记录评测器版本锚点。
可用 ``PLUGIN_EVAL_ENGINE=legacy`` 切回本仓库内置的移植实现（应急/对比用）。

- 评测动作（market）：市场内 plugin-eval CLI，quick 仅静态层、standard 追加 judge 层
  （经 claude-agent-sdk 驱动 Claude Code，模型分层 haiku/sonnet）
- 评测动作（legacy，移植自 agents/plugins/plugin-eval）：静态 lint（plugin_eval/
  static_layer.py）+ 4 项锚定 rubric judge；Layer 3 Monte Carlo 未移植

深度：
- quick：仅静态层，置信标签 Estimated，零模型调用
- standard：静态 + judge 层，置信标签 Assessed

深度守卫：**插件级目标只支持 quick**，standard（及蒙特卡洛）只支持 skill 级对象——
插件级的 standard 请求按 quick 执行并在 warnings 里写明降级原因，不产出假深度结果。

被测对象（每行一个）：
- plugin_path：本地插件/skill 目录路径（含 .claude-plugin/ 的目录按插件整体评；
  含 SKILL.md 的目录按单个 skill 评）
- skill_content：内联 SKILL.md 文本（按单个 skill 评，执行前落临时目录）
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from app.services.plugin_eval import engine as plugin_engine
from app.services.plugin_eval.parser import detect_target, parse_plugin, parse_skill, parse_skill_content
from app.services.plugin_eval.static_layer import StaticAnalyzer
from app.services.plugin_eval_market_runner import (
    DEFAULT_TRACE_MODE,
    TRACE_DEPTH,
    TRACE_MODES,
    MarketEvalError,
    resolve_evaluator,
    run_market_plugin_eval,
    run_market_plugin_trace_eval,
)
from app.services.plugin_eval_trace import (
    build_artifact_dir,
    detect_invoked_plugin,
    discover_rule_plugins,
    find_thread_file,
    resolve_plugin_dir,
)

logger = logging.getLogger(__name__)

VALID_DEPTHS = ("quick", "standard")

# 评测口径：trace=按数据集行里的会话 trace 做历史（真实数据）评测
TRACE_MODE = "trace"

# 逐检查项明细落库上限（超出只保留计数，避免 row_result JSONB 膨胀）
MAX_TRACE_CHECKS = 200
MAX_TRACE_KEY_STEPS = 300
# 失败项单独留一份且不随明细上限丢失（结论要能逐条复核）
MAX_TRACE_FAILED_CHECKS = 100
_MAX_TEXT_CHARS = 600
_MAX_EVIDENCE_IDS = 20
_MAX_CITATIONS = 10

# 评测动作引擎：market=实时加载市场版 plugin-eval（默认）；legacy=仓库内置移植实现
VALID_ENGINES = ("market", "legacy")

# 历史评测的「方向」名：规则文件里的 dimension 是抽取期的内部键
# （procedure_adherence 等），对外展示与导出统一用 plugin-eval 报告里的方向名。
# 逐字对齐 plugin-eval src/plugin_eval/reporter.py 的 _DIMENSION_NAMES。
HISTORY_DIMENSION_LABELS = {
    "reporting_accuracy": "报告准确性",
    "procedure_adherence": "流程遵循",
    "error_handling": "异常处理可信度",
    "source_alignment": "来源一致性",
    "coverage": "需求覆盖",
    "case_quality": "用例质量",
    "workflow_reporting": "生成流程与报告",
}

# 阶段名：metric → 报告的节标题（report.md「历史执行可信度」/「历史生成可信度」）
HISTORY_PHASE_LABELS = {
    "historical_execution_reliability": "历史执行可信度",
    "historical_generation_reliability": "历史生成可信度",
}


def plugin_eval_engine() -> str:
    """评测动作引擎开关（非法值回落 market）"""
    raw = (os.environ.get("PLUGIN_EVAL_ENGINE") or "").strip().lower()
    return raw if raw in VALID_ENGINES else "market"


def _normalize_dimension(dim: dict) -> dict:
    """市场维度对象 → 平台维度对象：补中文 label（市场只给英文 name），其余原样保留"""
    name = str(dim.get("name") or "")
    return {**dim, "name": name, "label": plugin_engine.DIMENSION_LABELS_ZH.get(name, name)}


def _sha256_file(path: Path) -> str:
    """线程文件内容哈希（取证锚点：同一份线程可复算）"""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trim_text(value, limit: int = _MAX_TEXT_CHARS) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text if len(text) <= limit else text[:limit] + f"…（截断，共 {len(text)} 字符）"


def _dimension_label(key) -> str:
    """抽取期方向键 → 报告里的方向名（未知键原样返回，规则文件可先于本表更新）"""
    text = str(key or "")
    return HISTORY_DIMENSION_LABELS.get(text, text)


def _dimension_rows(dimensions: dict) -> list:
    """报告「分项」表：方向 / 可信度 / 判定有效率 / 可判定·适用（口径与 reporter 同源）"""
    rows = []
    for key, value in (dimensions or {}).items():
        if not isinstance(value, dict):
            continue
        counts = value.get("counts") or {}
        measured = int(counts.get("passed", 0) or 0) + int(counts.get("failed", 0) or 0)
        rows.append({
            "key": key,
            "label": _dimension_label(key),
            "score": _round_rate(value.get("score")),
            "coverage": _round_rate(value.get("coverage")),
            "measured": measured,
            "applicable": measured + int(counts.get("insufficient", 0) or 0),
            "counts": counts,
        })
    return rows


def _trim_check(check: dict) -> dict:
    """逐检查项精简：保留判定要素，证据引用与实证文本限长"""
    if not isinstance(check, dict):
        return {}
    return {
        "id": check.get("id"),
        "rule_id": check.get("rule_id"),
        "dimension": check.get("dimension"),
        "dimension_label": _dimension_label(check.get("dimension")),
        "subject": check.get("subject"),
        "status": check.get("status"),
        "reason": _trim_text(check.get("reason")),
        "method": check.get("method"),
        "expectation": _trim_text(check.get("expectation")),
        "actual": _trim_text(check.get("actual")),
        "evidence_ids": (check.get("evidence_ids") or [])[:_MAX_EVIDENCE_IDS],
        "citations": (check.get("citations") or [])[:_MAX_CITATIONS],
        "reason_code": check.get("reason_code"),
    }


def _trim_key_step(step: dict) -> dict:
    if not isinstance(step, dict):
        return {}
    return {
        "id": step.get("id"),
        "step": step.get("step"),
        "name": step.get("name"),
        "signature": step.get("signature"),
        "task_epoch": step.get("task_epoch"),
        "line_start": step.get("line_start"),
        "line_end": step.get("line_end"),
        "attempts": step.get("attempts"),
        "achieved": step.get("achieved"),
        "check_count": step.get("check_count"),
        # 本步调用确认（确定性检查的判定；该步无 expected_calls 时为 None）
        "call_check": step.get("call_check"),
    }


def _round_rate(value):
    """比率保留两位小数：plugin-eval 出长浮点，前端明细与导出直接展示该值"""
    return round(float(value), 2) if isinstance(value, (int, float)) else value


def _trim_behavior_coverage(coverage: dict) -> dict:
    """去掉逐条未覆盖明细（体量大且不进判定），保留比率与分类汇总"""
    if not isinstance(coverage, dict):
        return {}
    trimmed = {k: v for k, v in coverage.items() if k != "uncovered"}
    if "rate" in trimmed:
        trimmed["rate"] = _round_rate(trimmed["rate"])
    return trimmed


def _trim_history_source(source: dict) -> dict:
    """历史来源锚点：产物清单体量大，只留计数"""
    if not isinstance(source, dict):
        return {}
    trimmed = {k: v for k, v in source.items() if k != "artifact_manifest"}
    manifest = source.get("artifact_manifest")
    if isinstance(manifest, list):
        trimmed["artifact_count"] = len(manifest)
    return trimmed


MAX_TRACE_EVIDENCE = 200


def _trim_evidence(items: list) -> list:
    """被引用证据：保留 id/来源/摘录（与报告「引用证据」同口径，单条 500 字）"""
    rows = []
    for item in (items or [])[:MAX_TRACE_EVIDENCE]:
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        rows.append({
            "id": item.get("id"),
            "kind": item.get("kind"),
            "source": {
                k: source.get(k)
                for k in ("line", "task_epoch", "tool_name", "role", "artifact_role")
                if source.get(k) is not None
            },
            "text": _trim_text(item.get("text") or "", 500),
        })
    return rows


def _design_snapshot(payload: dict) -> dict:
    """设计质量线快照：只含静态层 + judge 层，不含历史线

    同一插件（同版本、同市场路径、同判定方式）在任务内只评测一次，其余行复用本快照。
    模型用量按「总用量 − 两阶段历史用量」拆出设计侧用量。
    """
    if not isinstance(payload, dict):
        return {}
    history_usage: dict = {}
    for key in ("history", "history_generation"):
        for model, tokens in ((payload.get(key) or {}).get("model_usage") or {}).items():
            if isinstance(tokens, (int, float)):
                history_usage[model] = history_usage.get(model, 0) + int(tokens)
    design_usage = {}
    for model, tokens in (payload.get("model_usage") or {}).items():
        if not isinstance(tokens, (int, float)):
            continue
        remainder = int(tokens) - history_usage.get(model, 0)
        if remainder > 0:
            design_usage[model] = remainder
    return {
        "composite": payload.get("composite"),
        "layers": payload.get("layers") or [],
        "skill_results": payload.get("skill_results") or [],
        "config": payload.get("config") or {},
        "model_usage": design_usage,
        "duration_ms": payload.get("total_duration_ms"),
    }


def _history_phase_block(block: dict) -> dict:
    """一个历史阶段的报告要素（对应 report.md 的「历史执行/生成可信度」一节）

    plugin-eval 逐阶段各出一节：可信度得分、判定有效率、关键步骤覆盖率、
    可判定/适用、判定计数、方向分项、关键步骤、失败项、限制与规则锚点。
    本节即该节的结构化版本，页面与导出直接照着渲染。

    ``detected`` 来自引擎的 ``source.phase_detected``：false 表示本线程没有该阶段
    任务，四种判定计数与「可判定/适用」都是空集，页面必须显示未测而不是 0。
    """
    if not isinstance(block, dict) or not block:
        return {}
    metric = str(block.get("metric") or "")
    checks = block.get("checks") or []
    key_steps = [_trim_key_step(s) for s in (block.get("key_steps") or [])]
    failed = [
        {**_trim_check(c), "phase": metric}
        for c in checks
        if isinstance(c, dict) and c.get("status") == "failed"
    ]
    limitations = block.get("limitations") or []
    detected = (block.get("source") or {}).get("phase_detected")
    unmeasured_reason = ""
    if detected is False or block.get("status") == "unmeasured":
        unmeasured_reason = next(
            (str(x) for x in limitations if "未测" in str(x)), ""
        ) or next((str(x) for x in limitations if "未发现" in str(x)), "")
    return {
        "metric": metric,
        "label": HISTORY_PHASE_LABELS.get(metric, metric),
        "plugin": block.get("plugin"),
        "thread_id": block.get("thread_id"),
        "status": block.get("status"),
        "detected": detected,
        "unmeasured_reason": unmeasured_reason,
        "score": _round_rate(block.get("score")),
        "coverage": _round_rate(block.get("coverage")),
        "counts": block.get("counts") or {},
        "dimensions": _dimension_rows(block.get("dimensions") or {}),
        "behavior_coverage": _trim_behavior_coverage(block.get("behavior_coverage") or {}),
        "source": _trim_history_source(block.get("source") or {}),
        "rule": block.get("rule") or {},
        "limitations": limitations,
        "model_usage": block.get("model_usage") or {},
        "evidence": _trim_evidence(block.get("evidence") or []),
        "check_count": len(checks),
        "key_steps": key_steps[:MAX_TRACE_KEY_STEPS],
        "key_step_total": len(key_steps),
        # 失败项单独留一份：检查项明细有上限，失败结论不能被截掉
        "failed_checks": failed[:MAX_TRACE_FAILED_CHECKS],
        "failed_total": len(failed),
    }


def _static_row_fields(payload: dict) -> dict:
    """市场 JSON 的「设计质量」部分 → 行字段（综合分/徽章/维度分/各层得分/反模式）

    market（综合分口径）与 trace（合并口径）共用：trace 行同样带综合分与徽章，
    真实数据分单独放在 ``real_data``，两套分数互不并入（plugin-eval 口径）。
    """
    composite = dict(payload.get("composite") or {})
    raw_layers = payload.get("layers") or []
    dimensions = [_normalize_dimension(d) for d in (composite.get("dimensions") or [])]
    composite["dimensions"] = dimensions
    score = composite.get("score")
    return {
        "composite": composite,
        "layers": [
            {
                "layer": layer.get("layer"),
                "score": layer.get("score"),
                "sub_scores": layer.get("sub_scores") or {},
                "anti_patterns": layer.get("anti_patterns") or [],
            }
            for layer in raw_layers
        ],
        "anti_patterns": [
            ap for layer in raw_layers for ap in (layer.get("anti_patterns") or [])
        ],
        "average_score": round(float(score), 2) if isinstance(score, (int, float)) else None,
        "badge": composite.get("badge") or "",
        "dimension_scores": {
            d["name"]: d["score"]
            for d in dimensions
            # 未测维度（measured=false，市场给 0.0）必须剔除，否则会拉低聚合均分
            if d.get("measured", True) and isinstance(d.get("score"), (int, float))
        },
    }


def _skill_rows(payload: dict) -> list:
    """报告「Skill 明细」表：插件内逐 skill 的综合分与各层得分

    插件级目标在标准深度下由 plugin-eval 逐 skill 评审（静态 + judge），
    结果落在 ``skill_results``；逐条留全（只取分值与层分，体量小）。
    """
    rows = []
    for item in payload.get("skill_results") or []:
        if not isinstance(item, dict):
            continue
        composite = item.get("composite") or {}
        dimensions = [d for d in (composite.get("dimensions") or []) if isinstance(d, dict)]
        layers = [l for l in (item.get("layers") or []) if isinstance(l, dict)]
        rows.append({
            "name": Path(str(item.get("plugin_path") or "")).name,
            "score": _round_rate(composite.get("score")),
            "badge": composite.get("badge") or "",
            "confidence_label": composite.get("confidence_label") or "",
            "measured_dimensions": sum(1 for d in dimensions if d.get("measured")),
            "dimension_count": len(dimensions),
            "anti_pattern_count": sum(len(l.get("anti_patterns") or []) for l in layers),
            "layers": [
                {"layer": l.get("layer"), "score": _round_rate(l.get("score"))} for l in layers
            ],
        })
    return rows


def _trim_payload(payload: dict) -> dict:
    """审计用市场 JSON 摘要：保留判定要素与被引用证据，逐条未覆盖另存分类计数"""
    import json as _json

    history = payload.get("history") or {}
    composite = payload.get("composite") or {}
    return {
        "plugin_path": payload.get("plugin_path"),
        "timestamp": payload.get("timestamp"),
        "total_duration_ms": payload.get("total_duration_ms"),
        "model_usage": payload.get("model_usage") or {},
        "config": payload.get("config") or {},
        # 设计质量综合分（trace 合并口径下与真实数据分并列，两套独立）
        "composite": {
            "score": _round_rate(composite.get("score")),
            "badge": composite.get("badge"),
            "confidence_label": composite.get("confidence_label"),
            "anti_pattern_penalty": composite.get("anti_pattern_penalty"),
            "dimensions": [
                {
                    "name": d.get("name"),
                    "label": _normalize_dimension(d if isinstance(d, dict) else {})["label"],
                    "score": _round_rate((d or {}).get("score")),
                    "weight": (d or {}).get("weight"),
                    "measured": (d or {}).get("measured"),
                }
                for d in (composite.get("dimensions") or [])
                if isinstance(d, dict)
            ],
        },
        "layers": [
            {
                "layer": layer.get("layer"),
                "score": _round_rate(layer.get("score")),
                "sub_scores": layer.get("sub_scores") or {},
                "anti_pattern_count": len(layer.get("anti_patterns") or []),
            }
            for layer in payload.get("layers") or []
            if isinstance(layer, dict)
        ],
        "real_data": {
            "metric": (payload.get("real_data") or {}).get("metric"),
            "status": (payload.get("real_data") or {}).get("status"),
            "score": (payload.get("real_data") or {}).get("score"),
            "coverage": _round_rate((payload.get("real_data") or {}).get("coverage")),
            "counts": (payload.get("real_data") or {}).get("counts") or {},
            # 阶段小分（合并口径下报告「测评总览」的生成/执行两行）
            "phases": {
                metric: {
                    "label": HISTORY_PHASE_LABELS.get(metric, metric),
                    "status": (phase or {}).get("status"),
                    "score": _round_rate((phase or {}).get("score")),
                    "coverage": _round_rate((phase or {}).get("coverage")),
                    "counts": (phase or {}).get("counts") or {},
                }
                for metric, phase in ((payload.get("real_data") or {}).get("phases") or {}).items()
                if isinstance(phase, dict)
            },
        },
        "history": {
            "metric": history.get("metric"),
            "plugin": history.get("plugin"),
            "thread_id": history.get("thread_id"),
            "status": history.get("status"),
            "score": history.get("score"),
            "coverage": _round_rate(history.get("coverage")),
            "counts": history.get("counts") or {},
            "dimensions": _dimension_rows(history.get("dimensions") or {}),
            "rule": history.get("rule") or {},
            "source": _trim_history_source(history.get("source") or {}),
            "limitations": history.get("limitations") or [],
            "model_usage": history.get("model_usage") or {},
            "behavior_coverage": _trim_behavior_coverage(history.get("behavior_coverage") or {}),
            # 引用证据原文：报告「检查明细 / 引用证据」可离线复核，页面按 evidence_id 关联
            "evidence": _trim_evidence(history.get("evidence") or []),
            "output_dir": history.get("output_dir"),
            "check_count": len(history.get("checks") or []),
            "key_step_count": len(history.get("key_steps") or []),
        },
        "history_generation": _history_phase_block(payload.get("history_generation") or {}) or None,
        "raw_size_bytes": len(_json.dumps(payload, ensure_ascii=False, default=str)),
    }

# ---------------------------------------------------------------------------
# Judge 层锚定 rubric（逐字移植自 plugin-eval layers/judge.py）
# ---------------------------------------------------------------------------

ORCHESTRATION_RUBRIC = """
Score 0.0 — Poor: Skill acts as standalone agent; manages its own tool calls and sub-tasks.
Score 0.25 — Below average: Skill has some orchestration logic mixed with worker tasks.
Score 0.5 — Average: Skill delegates some tasks but still coordinates multi-step flows itself.
Score 0.75 — Good: Skill is mostly a worker; inputs/outputs documented, minimal coordination.
Score 1.0 — Excellent: Pure worker role; composable, clear contracts, no orchestration logic.
""".strip()

SCOPE_RUBRIC = """
Score 0.0 — Too thin: Stub or trivial wrapper with near-zero unique value.
Score 0.25 — Under-scoped: Covers only a narrow slice; misses obvious related tasks.
Score 0.5 — Average: Reasonable scope but either too broad or somewhat narrow.
Score 0.75 — Well-scoped: Covers one coherent domain; neither bloated nor sparse.
Score 1.0 — Perfectly calibrated: Minimal surface area, maximum cohesion, ideal composability.
""".strip()

_JUDGE_SYSTEM = (
    "You are an expert evaluator of Claude Code skills. "
    "Respond ONLY with valid JSON — no explanation, no markdown fences."
)


def _judge_prompt_triggering(description: str) -> str:
    return f"""Given this skill description:

<description>
{description}
</description>

Generate 10 synthetic user prompts: 5 that SHOULD trigger this skill and 5 that should NOT.
For each prompt, also predict whether a typical Claude model would trigger this skill.

Return JSON matching this schema:
{{
  "predictions": [
    {{"prompt": "...", "should_trigger": true, "would_trigger": true}},
    ...
  ],
  "precision": <float 0-1>,
  "recall": <float 0-1>,
  "f1": <float 0-1>
}}"""


def _judge_prompt_orchestration(skill_content: str) -> str:
    return f"""Evaluate this skill's orchestration fitness.

A skill should be a pure WORKER — it should NOT orchestrate other tools or agents.
It should accept clear inputs and produce clear outputs.

Rubric:
{ORCHESTRATION_RUBRIC}

Skill content:
<skill>
{skill_content[:3000]}
</skill>

Return JSON:
{{
  "score": <float 0.0-1.0 matching rubric>,
  "reasoning": "<one sentence>",
  "evidence": ["<quote or observation>", ...]
}}"""


def _judge_prompt_output_quality(skill_content: str) -> str:
    return f"""Simulate 3 realistic tasks this skill would handle, then evaluate the
expected output quality based on the skill's instructions.

Skill content:
<skill>
{skill_content[:3000]}
</skill>

Return JSON:
{{
  "score": <float 0.0-1.0>,
  "simulations": [
    {{"task": "...", "expected_output": "...", "quality_notes": "..."}}
  ]
}}"""


def _judge_prompt_scope(skill_content: str) -> str:
    return f"""Evaluate this skill's scope calibration.

Rubric:
{SCOPE_RUBRIC}

Skill content:
<skill>
{skill_content[:3000]}
</skill>

Return JSON:
{{
  "score": <float 0.0-1.0 matching rubric>,
  "assessment": "<one sentence>"
}}"""


def _extract_and_parse(text: str) -> dict:
    """剥 ```json 围栏后解析 judge 输出；非 JSON 降级为 unmeasured 标记。"""
    stripped = (text or "").strip()
    if not stripped:
        return {"unmeasured": True, "error": "judge LLM returned no text"}

    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {"unmeasured": True, "error": "judge response was not valid JSON", "raw": text}
    return parsed if isinstance(parsed, dict) else {
        "unmeasured": True, "error": "judge response was not a JSON object", "raw": text,
    }


def _measured_score(result: dict, key: str) -> float | None:
    """读取某项评估的数值分；unmeasured 返回 None。"""
    if not isinstance(result, dict) or result.get("unmeasured"):
        return None
    val = result.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return max(0.0, min(1.0, float(val)))


# ---------------------------------------------------------------------------
# Claude Code 调用（与 plugin-eval 本地评测同一条路径：claude-agent-sdk query()）
# ---------------------------------------------------------------------------

# 模型分层映射（与 plugin-eval layers/judge.py 的 _MODEL_MAP 逐字一致）
_JUDGE_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}


def _resolve_judge_model(tier: str, cc_config: dict | None) -> str:
    """按档位解析 judge 模型名

    - haiku 档固定用映射值（与本地 plugin-eval 分层一致）；网关不支持该模型名时
      该项评估降级 unmeasured，不影响其余维度
    - 其余档位以 Claude Code 全局配置的模型优先（服务器经网关调用，模型名以
      网关配置为准）；未配置时回退映射值
    """
    mapped = _JUDGE_MODEL_MAP.get(tier, _JUDGE_MODEL_MAP["sonnet"])
    configured = str((cc_config or {}).get("model") or "").strip()
    if tier != "haiku" and configured:
        return configured
    return mapped


def _usage_total_tokens(usage) -> int:
    """SDK usage dict → 总 token 数（与 plugin-eval _sdk.usage_total_tokens 同口径）"""
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    return sum(n for k, n in usage.items() if k.endswith("_tokens") and isinstance(n, int))


def _collect_sdk_output(messages: list) -> dict:
    """走查 SDK 消息流 → {text, result, errored, usage, model}

    与 plugin-eval _sdk.collect_sdk_output 同语义：拼接 assistant 文本块，
    取终态 ResultMessage 的 result / is_error / usage / model。
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    output = {"text": "", "result": None, "errored": False, "usage": None, "model": None}
    for message in messages:
        if isinstance(message, AssistantMessage):
            output["model"] = message.model
            for block in message.content:
                if isinstance(block, TextBlock):
                    output["text"] += block.text
        elif isinstance(message, ResultMessage):
            if message.is_error:
                output["errored"] = True
            if message.result:
                output["result"] = message.result
            if isinstance(message.usage, dict):
                output["usage"] = message.usage
    return output


def _cc_query(prompt: str, model: str, timeout: float = 300.0, usage_sink: dict | None = None) -> dict:
    """经 claude-agent-sdk 驱动 Claude Code CLI 执行一次 judge 评估

    与 plugin-eval query_llm 同一条调用路径：query(prompt=..., options=
    ClaudeAgentOptions(model=model, allowed_tools=[]))——system 约束拼进 prompt、
    禁用全部工具。返回解析后的 JSON dict；SDK 缺失/未配置/失败一律降级为
    {"unmeasured": True, ...}（judge 层是 best-effort，不阻断评测）。

    当 usage_sink 给出时，按 SDK 实际上报的模型累计 token 用量。
    """
    from app.services.claude_code_config_service import ClaudeCodeConfigService

    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError:
        return {"unmeasured": True, "error": "claude-agent-sdk 未安装（requirements: claude-agent-sdk==0.2.163）"}

    cc_config = ClaudeCodeConfigService.get_active_config()
    if not cc_config:
        return {"unmeasured": True, "error": "未配置 Claude Code 全局配置"}

    options = ClaudeAgentOptions(
        model=model,
        allowed_tools=[],
        env=ClaudeCodeConfigService.get_sdk_env(),
        # 不加载宿主机用户/项目 settings，保证评测环境纯净（与平台裁判引擎同口径）
        setting_sources=[],
    )

    async def _run():
        return [message async for message in query(prompt=prompt, options=options)]

    def _run_with_timeout() -> list:
        # 线程级超时（兼容生产环境 nest_asyncio patch asyncio.run，
        # 与 claude_code_judge_agent._run_async_with_timeout 同一套方案）
        box: dict = {}

        def runner():
            import asyncio

            try:
                box["value"] = asyncio.run(_run())
            except BaseException as e:  # noqa: BLE001 — 线程边界传递异常
                box["error"] = e

        thread = threading.Thread(target=runner, daemon=True, name="plugin-judge-query")
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"Claude Code judge 调用超时（>{timeout:.0f}秒）")
        if "error" in box:
            raise box["error"]
        return box.get("value") or []

    try:
        messages = _run_with_timeout()
    except Exception as exc:  # noqa: BLE001 — judge 降级不阻断
        return {"unmeasured": True, "error": f"judge Claude Code 调用失败: {exc}"}

    output = _collect_sdk_output(messages)
    if usage_sink is not None:
        tokens = _usage_total_tokens(output.get("usage"))
        if tokens:
            usage_model = output.get("model") or model
            usage_sink[usage_model] = usage_sink.get(usage_model, 0) + tokens

    if output.get("errored"):
        return {"unmeasured": True, "error": "judge Claude Code 调用返回错误结果"}

    raw = (output.get("text") or "").strip() or (output.get("result") or "").strip()
    return _extract_and_parse(raw)


class PluginEvaluator:
    """插件评测器：market 引擎（默认，实时加载市场版 plugin-eval）/ legacy 引擎（内置移植）。"""

    def __init__(
        self,
        depth: str = "quick",
        market_id: Optional[int] = None,
        engine: Optional[str] = None,
        trace_judgement: Optional[str] = None,
        trace_concurrency: Optional[int] = None,
    ):
        self.depth = depth if depth in VALID_DEPTHS else "quick"
        self.market_id = market_id
        # 历史评测判定方式（trace 口径用）：model=模型判定（默认）/ offline=仅确定性检查
        self.trace_judgement = (
            trace_judgement if trace_judgement in TRACE_MODES else DEFAULT_TRACE_MODE
        )
        # 任务级并发覆盖（None = 跟随 PLUGIN_EVAL_CONCURRENCY / 默认值）
        self.trace_concurrency = trace_concurrency
        self.engine = engine if engine in VALID_ENGINES else plugin_eval_engine()
        self._static = StaticAnalyzer()
        self._evaluator_info: Optional[dict] = None  # 评测动作锚点（同一任务内解析一次）

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def evaluate(self, plugin_path: str = "", skill_content: str = "") -> dict:
        """评测单个对象（目录或内联 SKILL.md），返回行结果 dict（不含 idx 等行标识）。"""
        if self.engine == "market":
            return self._evaluate_market(plugin_path, skill_content)
        return self._evaluate_legacy(plugin_path, skill_content)

    # ------------------------------------------------------------------
    # market 引擎：子进程执行市场克隆里的 plugin-eval
    # ------------------------------------------------------------------

    def _evaluate_market(self, plugin_path: str, skill_content: str) -> dict:
        started = time.perf_counter()
        if not plugin_path and not skill_content:
            return self._failure_row("未提供评测对象：需要 plugin_path 或 skill_content")

        warnings: list = []
        temp_dir: Optional[Path] = None
        try:
            if plugin_path:
                target_dir = Path(plugin_path).expanduser()
                if not target_dir.exists():
                    return self._failure_row(f"插件路径不存在: {plugin_path}")
                if not target_dir.is_dir():
                    return self._failure_row(f"插件路径不是目录: {plugin_path}")
                target_type = detect_target(target_dir)
                if target_type == "unknown":
                    return self._failure_row(
                        f"目录中未找到 SKILL.md 或 .claude-plugin/plugin.json: {plugin_path}"
                    )
            else:
                # 内联 SKILL.md：市场 CLI 只吃目录，落临时目录后评测
                skill = parse_skill_content(skill_content)
                temp_dir = Path(tempfile.mkdtemp(prefix="plugin-eval-content-"))
                (temp_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
                target_dir = temp_dir
                target_type = "skill"

            if target_type == "plugin":
                target_name = parse_plugin(target_dir).name
            elif plugin_path:
                target_name = parse_skill(target_dir).name
            else:
                target_name = skill.name

            depth = self.depth
            if target_type == "plugin" and depth != "quick":
                warnings.append(
                    "插件级评测只支持 quick（仅静态层），已按 quick 执行；"
                    "standard（含蒙特卡洛）只支持 skill 级对象"
                )
                depth = "quick"

            try:
                evaluator = self._resolve_evaluator()
                outcome = run_market_plugin_eval(str(target_dir), depth=depth, evaluator=evaluator)
            except MarketEvalError as e:
                return self._failure_row(f"评测动作不可用: {e}")

            warnings.extend(self._evaluator_warnings(evaluator))
            return self._row_from_market(outcome, target_type, target_name, depth, warnings, started)
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # trace 口径：按数据集行里的会话 trace 做历史（真实数据）评测
    # ------------------------------------------------------------------

    def evaluate_trace(self, package_dir: str, on_tick=None, design_cache: Optional[dict] = None) -> dict:
        """评测一行会话 trace：行提供 hub 执行包镜像目录，被测插件从线程调用识别

        结果分两条线：
        - **设计质量线**：静态层 + 逐 skill 的 rubric 评审。只与插件目录有关，与线程无关，
          因此同一插件在任务内**只评一次**，其余行通过 ``design_cache`` 复用（逐行只跑
          ``--history-only``），避免 N 行重复跑静态层与 judge。
        - **历史数据线**：本行线程的执行 + 生成两阶段，逐行独立。

        两套分数相互独立、不并入计算（与本地 ``score <plugin> --thread <jsonl> --depth
        standard`` 同口径）。可评性取决于两点：执行包里是否保留原始会话 jsonl、市场历史
        规则是否覆盖该插件——任一不满足按「未测」返回（不计失败），只有评测动作本身失败
        才走失败行。

        on_tick：评测单行可能跑几十分钟，任务层据此在评测动作执行期间刷新执行
        心跳并写进度日志（否则会被部署期的收尸 reaper 判为心跳超时）。
        """
        started = time.perf_counter()
        detail: dict = {"mode": TRACE_MODE}
        if not package_dir:
            return self._unmeasured_row(
                "本行未提供 anc-hub 执行包目录（数据集需含 hub_file 类型字段）", detail, started
            )
        if not Path(package_dir).is_dir():
            return self._unmeasured_row(f"本行执行包镜像目录不存在: {package_dir}", detail, started)

        try:
            evaluator = self._resolve_evaluator()
        except MarketEvalError as e:
            return self._trace_failure_row(f"评测动作不可用: {e}")

        detail["package_dir"] = package_dir
        work_dir = Path(tempfile.mkdtemp(prefix="plugin-eval-trace-"))
        try:
            source = find_thread_file(package_dir, str(work_dir))
            if source is None:
                return self._unmeasured_row(
                    "本行执行包里没有原始会话文件（需 claude-thread.zip 或 .jsonl），"
                    "截断的 session_messages 不能替代原始线程，无法做历史评测",
                    detail, started, evaluator,
                )
            thread_path = Path(source.jsonl_path)
            detail.update({
                "thread_file": thread_path.name,
                "thread_origin": source.origin,
                "thread_source": source.kind,
                "thread_size_bytes": thread_path.stat().st_size,
                "thread_sha256": _sha256_file(thread_path),
            })

            candidates = discover_rule_plugins(str(evaluator.get("package_root") or ""))
            if not candidates:
                return self._unmeasured_row(
                    "市场评测器未提供历史规则目录（rules/history），无法做历史评测",
                    detail, started, evaluator,
                )
            detected = detect_invoked_plugin(str(thread_path), candidates)
            detail["detected"] = detected
            if not detected["plugin"]:
                return self._unmeasured_row(
                    "线程里没有可评插件的技能调用（市场历史规则覆盖："
                    + "、".join(candidates) + "）",
                    detail, started, evaluator,
                )

            try:
                item = resolve_plugin_dir(int(evaluator["market_id"]), detected["plugin"])
            except Exception as e:  # 市场未同步/目录缺失：按失败行给明确原因
                return self._trace_failure_row(f"市场插件目录解析失败: {e}")
            if item is None:
                return self._unmeasured_row(
                    f"市场未收录插件「{detected['plugin']}」，无法加载其历史规则",
                    detail, started, evaluator,
                )
            detail.update({
                "plugin_source": item.get("source") or "",
                "plugin_version": item.get("version") or "",
                "plugin_path": item.get("path") or "",
            })

            try:
                artifact_dir = build_artifact_dir(package_dir, str(work_dir), source)
            except OSError as e:  # 产物镜像失败不阻断评测：退化为不带产物
                logger.warning(f"[插件评测] 产物镜像构建失败（本次不带产物）: {e}")
                artifact_dir = None
            # 镜像目录随 work_dir 清理，行里只留「带了多少份产物」这一事实
            detail["artifact_files"] = (
                sum(1 for p in Path(artifact_dir).rglob("*") if p.is_file())
                if artifact_dir else 0
            )

            # 设计质量线只与插件有关：同一插件（版本/路径/判定方式一致）任务内只评一次
            design_key = (
                str(detected["plugin"]),
                str(item.get("version") or ""),
                str(item.get("path") or ""),
                str(self.trace_judgement),
            )
            cached_design = (design_cache or {}).get(design_key)
            design_reused = cached_design is not None

            try:
                outcome = run_market_plugin_trace_eval(
                    str(item["path"]),
                    str(thread_path),
                    artifact_dirs=[artifact_dir] if artifact_dir else None,
                    evaluator=evaluator,
                    on_tick=on_tick,
                    judgement=self.trace_judgement,
                    concurrency=self.trace_concurrency,
                    history_only=design_reused,
                )
            except MarketEvalError as e:
                return self._trace_failure_row(f"trace 评测动作不可用: {e}")

            if design_reused:
                design = cached_design
            else:
                design = _design_snapshot(outcome["payload"])
                if design_cache is not None:
                    design_cache[design_key] = design

            warnings = self._evaluator_warnings(evaluator)
            warnings.append(
                "结果分两条独立线：设计质量线（静态 + LLM 评审，按插件评测一次）与"
                "历史数据线（本行线程的执行 + 生成阶段）；两条线不并入计算"
            )
            return self._row_from_trace(
                outcome, str(detected["plugin"]), detail, warnings, started,
                design=design, design_reused=design_reused,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _trace_failure_row(error: str) -> dict:
        """trace 口径的失败行：带口径标记，行全失败时聚合仍按 trace 渲染"""
        row = PluginEvaluator._failure_row(error)
        row["evaluation_mode"] = TRACE_MODE
        row["plugin_eval"] = {"mode": TRACE_MODE}
        return row

    @staticmethod
    def _unmeasured_row(
        reason: str, detail: dict, started: float, evaluator: Optional[dict] = None
    ) -> dict:
        """未测行：内容不足以判定（不计失败），原因随行结果落库"""
        trace = dict(detail or {})
        trace["unavailable_reason"] = reason
        detected = trace.get("detected") or {}
        return {
            "success": True,
            "evaluation_mode": TRACE_MODE,
            "target_type": "plugin",
            "target_name": str(detected.get("plugin") or ""),
            "plugin_eval": {
                "mode": TRACE_MODE,
                "depth": TRACE_MODE,
                "layers": [],
                "composite": None,
                "model_usage": {},
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "warnings": [],
                "evaluator": (
                    {k: v for k, v in evaluator.items() if k != "package_root"}
                    if evaluator else None
                ),
                "raw": None,
            },
            "average_score": None,
            "badge": "",
            "dimension_scores": {},
            "anti_patterns": [],
            "real_data": {
                "metric": "real_data_reliability",
                "status": "unmeasured",
                "score": None,
                "coverage": None,
                "counts": {},
                "limitations": [reason],
            },
            "checks": [],
            "trace": trace,
            "error": "",
        }

    @staticmethod
    def _row_from_trace(
        outcome: dict, plugin_name: str, detail: dict, warnings: list, started: float,
        design: Optional[dict] = None, design_reused: bool = False,
    ) -> dict:
        """市场 JSON → 平台行结果 schema（两条线：设计质量 + 历史数据）

        行结构照 plugin-eval 报告落库，页面与导出按同一份数据渲染：
        - 设计质量（``composite``/``badge``/``dimension_scores``/``anti_patterns``/``layers``）：
          静态层 + 逐 skill 的 rubric 评审，即报告「测评总览 / Dimension Scores / Skill 明细」。
          同一插件任务内只评一次，``design`` 为快照（复用时来自缓存，``design_reused=True``）。
        - ``real_data``（真实数据测评指标）+ ``trace.phases``（逐阶段一节，含方向分项/关键步骤/
          失败项/限制/引用证据）+ ``checks``（逐检查项明细，带方向名与所属阶段）。
        两套分数**相互独立**（plugin-eval 明确真实数据分不并入综合分/徽章/排名）：
        ``average_score`` 与其余来源一致地表示综合分，真实数据分只在 ``real_data.score``。
        """
        payload = outcome["payload"]
        raw_real = payload.get("real_data") or {}
        score = raw_real.get("score")
        design_payload = design if isinstance(design, dict) and design else payload
        static = _static_row_fields(design_payload)
        config = design_payload.get("config") or payload.get("config") or {}
        depth = str(config.get("depth") or TRACE_DEPTH)

        # 逐阶段（执行/生成）各出报告一节；phase=all 时两节都在
        phases = []
        checks = []
        for key in ("history", "history_generation"):
            block = _history_phase_block(payload.get(key) or {})
            if not block:
                continue
            phases.append(block)
            for check in (payload.get(key) or {}).get("checks") or []:
                item = _trim_check(check)
                item["phase"] = block["metric"]
                item["phase_label"] = block["label"]
                checks.append(item)
        execution = phases[0] if phases else {}

        trace = dict(detail)
        trace.update({
            "thread_id": execution.get("thread_id") or "",
            "rule": execution.get("rule") or {},
            "history": execution,
            "phases": phases,
        })
        if not isinstance(score, (int, float)) and not trace.get("unavailable_reason"):
            # 线程里没有该插件的任务入口（规则选不中）时 plugin-eval 出未测分，原因在此写明
            trace["unavailable_reason"] = (
                "plugin-eval 未在本次线程中定位到该插件的任务（规则选不中任务入口）"
            )

        # 复用设计质量快照时，raw 也要能离线复盘设计线（历史线 payload 的 composite 为 null）
        raw_source = payload
        if design and design_reused:
            raw_source = {
                **payload,
                "composite": design.get("composite"),
                "layers": design.get("layers") or [],
                "skill_results": design.get("skill_results") or [],
                "total_duration_ms": design.get("duration_ms"),
            }

        return {
            "success": True,
            "evaluation_mode": TRACE_MODE,
            "target_type": "plugin",
            "target_name": plugin_name,
            "plugin_eval": {
                "mode": TRACE_MODE,
                "depth": depth,
                "layers": static["layers"],
                "composite": static["composite"],
                "model_usage": payload.get("model_usage") or {},
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "warnings": warnings,
                "evaluator": {k: v for k, v in outcome["evaluator"].items() if k != "package_root"},
                "raw": _trim_payload(raw_source),
                # 报告「Skill 明细」表（插件内逐 skill 的综合分与各层得分）
                "skills": _skill_rows(design_payload),
                # 设计质量线：任务内只评一次，其余行复用（页面按插件展示一次）
                "design": {
                    "evaluated": bool(design),
                    "reused": bool(design_reused),
                    "model_usage": (design or {}).get("model_usage") or {},
                    "duration_ms": (design or {}).get("duration_ms"),
                    "depth": depth,
                },
                "line": "design_quality+history",
            },
            "average_score": static["average_score"],
            "badge": static["badge"],
            "dimension_scores": static["dimension_scores"],
            "anti_patterns": static["anti_patterns"],
            "design_reused": bool(design_reused),
            "real_data": {
                "metric": raw_real.get("metric") or "real_data_reliability",
                "status": raw_real.get("status") or "unmeasured",
                "score": round(float(score), 2) if isinstance(score, (int, float)) else None,
                "coverage": _round_rate(raw_real.get("coverage")),
                "counts": raw_real.get("counts") or {},
                "extraction_complete": raw_real.get("extraction_complete"),
                "limitations": raw_real.get("limitations") or [],
                "behavior_coverage": _trim_behavior_coverage(raw_real.get("behavior_coverage") or {}),
                "phases": raw_real.get("phases") or {},
            },
            "checks": checks[:MAX_TRACE_CHECKS],
            "checks_total": len(checks),
            "trace": trace,
            "error": "",
        }

    def _resolve_evaluator(self) -> dict:
        """评测动作锚点（每个 PluginEvaluator 实例解析一次，多行任务不重复解析）"""
        if self._evaluator_info is None:
            self._evaluator_info = resolve_evaluator(self.market_id)
        return self._evaluator_info

    @staticmethod
    def _evaluator_warnings(evaluator: dict) -> list:
        label = (
            f"{evaluator.get('name')} {evaluator.get('version') or '?'}"
            f"@{evaluator.get('commit') or '?'}（市场：{evaluator.get('market_name') or '?'}）"
        )
        warnings = [f"评测动作实时加载自插件市场：{label}"]
        if evaluator.get("stale"):
            warnings.append(
                f"评测器同步失败，本次用本地副本：{evaluator.get('refresh_error') or '原因未知'}"
            )
        return warnings

    @staticmethod
    def _row_from_market(
        outcome: dict, target_type: str, target_name: str, depth: str, warnings: list, started: float
    ) -> dict:
        """market JSON → 平台行结果 schema（下游聚合/导出/前端消费的字段保持同构）"""
        payload = outcome["payload"]
        static = _static_row_fields(payload)

        return {
            "success": True,
            "target_type": target_type,
            "target_name": target_name,
            "plugin_eval": {
                "depth": depth,
                "layers": static["layers"],
                "composite": static["composite"],
                "model_usage": payload.get("model_usage") or {},
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "warnings": warnings,
                "evaluator": {k: v for k, v in outcome["evaluator"].items() if k != "package_root"},
                "raw": payload,
            },
            "average_score": static["average_score"],
            "badge": static["badge"],
            "dimension_scores": static["dimension_scores"],
            "anti_patterns": static["anti_patterns"],
            "error": "",
        }

    # ------------------------------------------------------------------
    # legacy 引擎：仓库内置移植实现
    # ------------------------------------------------------------------

    def _evaluate_legacy(self, plugin_path: str, skill_content: str = "") -> dict:
        """评测单个对象（目录或内联 SKILL.md），返回行结果 dict（不含 idx 等行标识）。"""
        started = time.perf_counter()

        if not plugin_path and not skill_content:
            return self._failure_row("未提供评测对象：需要 plugin_path 或 skill_content")

        skill_content = skill_content or ""
        warnings = []

        if plugin_path:
            target_dir = Path(plugin_path).expanduser()
            if not target_dir.exists():
                return self._failure_row(f"插件路径不存在: {plugin_path}")
            if not target_dir.is_dir():
                return self._failure_row(f"插件路径不是目录: {plugin_path}")

            target_type = detect_target(target_dir)
            if target_type == "unknown":
                return self._failure_row(
                    f"目录中未找到 SKILL.md 或 .claude-plugin/plugin.json: {plugin_path}"
                )

            if target_type == "plugin":
                # 插件级评测：仅静态层（与 plugin-eval evaluate_plugin 一致）
                static_result = self._static.analyze_plugin(target_dir)
                layers = [static_result]
                target_name = static_result["metadata"].get("plugin_name") or target_dir.name
                warnings.append("插件级评测仅运行静态层，置信标签按 Estimated 处理")
                composite = plugin_engine.build_composite_from_static_overall(
                    static_result["score"],
                    len(static_result["anti_patterns"]),
                    depth="quick",
                )
                composite["confidence_label"] = plugin_engine.DEPTH_CONFIDENCE_LABELS["quick"]
                skill_for_judge = None
            else:
                skill = parse_skill(target_dir)
                static_result = self._static.analyze_skill(skill)
                layers = [static_result]
                target_name = skill.name
                skill_for_judge = skill
                composite = None  # judge 层之后统一组装
        else:
            skill = parse_skill_content(skill_content)
            static_result = self._static.analyze_skill(skill)
            layers = [static_result]
            target_name = skill.name
            target_type = "skill"
            skill_for_judge = skill
            composite = None

        judge_result = None
        if self.depth == "standard" and skill_for_judge is not None:
            judge_result = self._run_judge_layer(skill_for_judge)
            layers.append(judge_result["layer_result"])
            warnings.extend(judge_result["warnings"])

        if composite is None:
            composite = plugin_engine.build_composite(layers, depth=self.depth)

        row = {
            "success": True,
            "target_type": target_type,
            "target_name": target_name,
            "plugin_eval": {
                "depth": self.depth,
                "layers": layers,
                "composite": composite,
                "model_usage": plugin_engine.merge_model_usage(layers),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "warnings": warnings,
            },
            "average_score": round(composite["score"], 2),
            "badge": composite["badge"],
            "dimension_scores": {
                d["name"]: d["score"]
                for d in composite["dimensions"]
                if d["score"] is not None
            },
            "anti_patterns": static_result["anti_patterns"],
            "error": "",
        }
        return row

    # ------------------------------------------------------------------
    # Judge 层（Layer 2，经 Claude Code）
    # ------------------------------------------------------------------

    def _run_judge_layer(self, skill) -> dict:
        """并发跑 4 项 judge 评估（Claude Code 承载）；失败降级 unmeasured（与 plugin-eval 同口径）。"""
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        cc_config = ClaudeCodeConfigService.get_active_config()
        if not cc_config:
            return {
                "layer_result": self._judge_layer_result({}, ["未配置 Claude Code，judge 层跳过"]),
                "warnings": [
                    "未配置 Claude Code，judge 层跳过（请在「模型配置」页保存 Claude Code 全局配置；"
                    "复合分仅按静态层权重归一）"
                ],
            }

        timeout = float(cc_config.get("timeout_seconds") or 300)

        assessments = [
            ("triggering", "haiku", _judge_prompt_triggering(skill.description)),
            ("orchestration", "sonnet", _judge_prompt_orchestration(skill.raw_content)),
            ("output_quality", "sonnet", _judge_prompt_output_quality(skill.raw_content)),
            ("scope", "sonnet", _judge_prompt_scope(skill.raw_content)),
        ]

        model_usage: dict = {}
        errors: list = []

        def _call(item):
            key, tier, prompt = item
            model = _resolve_judge_model(tier, cc_config)
            # system 约束拼进 prompt（SDK query 无独立 system 概念，与 plugin-eval query_llm 同款）
            full_prompt = f"{_JUDGE_SYSTEM}\n\n{prompt}"
            parsed = _cc_query(full_prompt, model, timeout=timeout, usage_sink=model_usage)
            if parsed.get("unmeasured") and parsed.get("error"):
                errors.append(f"{key}: {parsed['error']}")
            return key, parsed

        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for key, parsed in pool.map(_call, assessments):
                results[key] = parsed

        raw_scores = {
            "triggering_accuracy": _measured_score(results.get("triggering"), "f1"),
            "orchestration_fitness": _measured_score(results.get("orchestration"), "score"),
            "output_quality": _measured_score(results.get("output_quality"), "score"),
            "scope_calibration": _measured_score(results.get("scope"), "score"),
        }
        sub_scores = {k: v for k, v in raw_scores.items() if v is not None}
        unmeasured = sorted(k for k, v in raw_scores.items() if v is None)

        warnings = []
        if unmeasured:
            warnings.append(f"judge 层部分维度未测出（{', '.join(unmeasured)}），已从复合分剔除重归一化")
        warnings.extend(errors)

        layer_result = self._judge_layer_result(
            sub_scores,
            warnings,
            metadata={
                "triggering": results.get("triggering"),
                "orchestration": results.get("orchestration"),
                "output_quality": results.get("output_quality"),
                "scope": results.get("scope"),
                "unmeasured": unmeasured,
                "model_usage": model_usage,
                "judge_engine": "claude_code",
            },
        )
        return {"layer_result": layer_result, "warnings": warnings}

    @staticmethod
    def _judge_layer_result(sub_scores: dict, warnings: list, metadata: dict | None = None) -> dict:
        """judge 层 LayerResult dict（layer score 仅展示用，取已测维度均值）。"""
        score = sum(sub_scores.values()) / len(sub_scores) if sub_scores else 0.0
        meta = dict(metadata or {})
        meta["warnings"] = warnings
        return {
            "layer": "judge",
            "score": score,
            "sub_scores": sub_scores,
            "anti_patterns": [],
            "metadata": meta,
        }

    # ------------------------------------------------------------------
    # 失败行
    # ------------------------------------------------------------------

    @staticmethod
    def _failure_row(error: str) -> dict:
        return {
            "success": False,
            "target_type": "",
            "target_name": "",
            "plugin_eval": None,
            "average_score": None,
            "badge": "",
            "dimension_scores": {},
            "anti_patterns": [],
            "error": error,
        }
