"""裁判引擎对拍报告（M8 QE8-2，DeepSeek 裁判接通的回归验收物）

同一批评判样本（同一行输入 + 同一份既有模型输出）分别交 Claude Code Agent
与 DeepSeek Agent 两个裁判引擎评分，产出双引擎一致性对拍报告——DeepSeek
裁判接通后的回归验收证据（两引擎分数是否同口径），也是「3 裁判引擎」
表述的销售物料锚点。

口径：
- 样本 = 任务最新一次执行的行级结果（evaluation_results，model_output
  非空）× 该任务数据集重建的裁判 payload——两引擎拿到**逐字节相同**的
  system/user prompt（PromptBuilder 同一构建路径），分差才可归因于引擎；
- 每引擎每行 1 个裁判（对拍看引擎间差异，不看引擎内噪声；任务的多裁判
  均值口径不适用）；
- 无行级结果（任务从未执行过）不硬跑：对拍锚定既有输出，提示先执行任务；
- 引擎全局配置缺失 → 该引擎整批标 config_missing，报告仍返回（可看另一
  引擎侧失败原因），不 500。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.task_models import EvaluationResult, EvaluationTask

logger = logging.getLogger(__name__)

# 对拍样本上限：同步端点（HTTP 请求内跑完），双引擎 × N 行 × 单裁判
MAX_SAMPLE_COUNT = 10
DEFAULT_SAMPLE_COUNT = 5
# 分差容差带（|cc − dsh| ≤ 1 记「同口径」）：10 分制下 1 分以内视为一致
WITHIN_TOLERANCE = 1.0


class JudgeComparisonError(ValueError):
    """对拍前置校验失败（API 层映射 400）"""


def _uses_judge(task: EvaluationTask) -> bool:
    evaluation_type = getattr(task, "evaluation_type", "")
    consistency_config = getattr(task, "consistency_config", None) or {}
    return evaluation_type in ("judge", "agent") or (
        evaluation_type == "consistency"
        and isinstance(consistency_config, dict)
        and bool(consistency_config.get("use_judge"))
    )


def _engine_configs() -> Dict[str, Optional[Dict[str, Any]]]:
    """两引擎全局配置（脱敏摘要进报告；缺失为 None）"""
    configs: Dict[str, Optional[Dict[str, Any]]] = {}
    try:
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        cc = ClaudeCodeConfigService.get_active_config()
        configs["claude_agent"] = (
            {"model": cc.get("model"), "max_turns": cc.get("max_turns")} if cc else None
        )
    except Exception as e:
        logger.warning(f"[JudgeCompare] 加载 CC 全局配置失败: {e}")
        configs["claude_agent"] = None
    try:
        from app.services.deepseek_harness_config_service import (
            DeepSeekHarnessConfigService,
        )

        dsh = DeepSeekHarnessConfigService.masked(
            DeepSeekHarnessConfigService.get_active_config())
        configs["deepseek_agent"] = dsh
    except Exception as e:
        logger.warning(f"[JudgeCompare] 加载 DSH 全局配置失败: {e}")
        configs["deepseek_agent"] = None
    return configs


def _load_sample_rows(db: Session, task: EvaluationTask) -> Dict[int, Dict[str, Any]]:
    """最新一次执行中有模型输出的行级结果：{data_index: {model_output, row_status, score}}"""
    execution_number = getattr(task, "execution_count", None) or 1
    rows = (
        db.query(EvaluationResult)
        .filter(
            EvaluationResult.task_id == task.id,
            EvaluationResult.execution_number == execution_number,
        )
        .order_by(EvaluationResult.data_index)
        .all()
    )
    samples: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        if not row.model_output:
            continue
        agg = row.aggregate if isinstance(row.aggregate, dict) else {}
        samples[row.data_index] = {
            "model_output": row.model_output,
            "row_status": row.status,
            "existing_average_score": agg.get("average_score"),
        }
    return samples


def _score_of(judge_results: List[Dict[str, Any]]) -> Optional[float]:
    """单裁判结果的分数（judge_failed → None）"""
    if not judge_results:
        return None
    first = judge_results[0]
    if first.get("judge_failed") or not isinstance(first.get("score"), (int, float)):
        return None
    return float(first["score"])


def _run_engine(
    task: EvaluationTask, engine: str, system_prompt: str, user_prompt: str,
    langfuse_host: str,
) -> List[Dict[str, Any]]:
    """按引擎跑 1 个裁判（两引擎同签名 run_parallel，对拍即同参对调）"""
    if engine == "claude_agent":
        from app.services.claude_code_judge_agent import ClaudeCodeJudgeAgent

        return ClaudeCodeJudgeAgent(task=task, langfuse_host=langfuse_host).run_parallel(
            system_prompt=system_prompt, user_prompt=user_prompt,
            session_file=None, judge_count=1,
        )
    from app.services.deepseek_judge_agent import DeepSeekJudgeAgent

    return DeepSeekJudgeAgent(task=task, langfuse_host=langfuse_host).run_parallel(
        system_prompt=system_prompt, user_prompt=user_prompt,
        session_file=None, judge_count=1,
    )


def generate_judge_comparison(
    db: Session,
    task: EvaluationTask,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    langfuse_host: str = "",
) -> Dict[str, Any]:
    """生成双引擎对拍报告（同步执行；sample_count 1~10）"""
    if not _uses_judge(task):
        raise JudgeComparisonError("仅支持使用裁判的任务（judge/agent 或启用裁判的一致性任务）")
    sample_count = max(1, min(int(sample_count or DEFAULT_SAMPLE_COUNT), MAX_SAMPLE_COUNT))

    engine_configs = _engine_configs()
    missing = [e for e, c in engine_configs.items() if not c]
    if missing:
        labels = {"claude_agent": "Claude Code", "deepseek_agent": "DeepSeek Harness"}
        raise JudgeComparisonError(
            "对拍需要两套裁判引擎全局配置，缺少: "
            + "、".join(f"{labels.get(e, e)}（模型配置页）" for e in missing))

    # 样本：最新执行的既有模型输出（两引擎同输出，分差只归因引擎）
    sample_rows = _load_sample_rows(db, task)
    if not sample_rows:
        raise JudgeComparisonError(
            "该任务最新执行没有可对拍的行级结果（需先完整执行一次任务，"
            "对拍锚定既有模型输出保证两引擎输入一致）")

    # 重建裁判 payload：与任务执行同一数据集加载 + 变量注入路径
    from app.tasks.evaluation_tasks_async import _load_dataset
    from app.services.variable_injector import VariableInjector
    from app.services.prompt_builder import PromptBuilder

    injector = VariableInjector(task)
    prompt_builder = PromptBuilder(task)
    comparisons: List[Dict[str, Any]] = []
    for item in _load_dataset(task, db):
        idx = item.get("index")
        if idx not in sample_rows:
            continue
        payload = injector.generate_payload(item)
        payload["idx"] = idx
        payload["model_answer"] = sample_rows[idx]["model_output"]
        system_prompt, user_prompt = prompt_builder.build_judge_prompts(
            payload, sample_rows[idx]["model_output"])
        # 两引擎并发跑同一条（行间串行，控制并发占用为 2）
        with ThreadPoolExecutor(max_workers=2) as pool:
            cc_future = pool.submit(
                _run_engine, task, "claude_agent", system_prompt, user_prompt,
                langfuse_host)
            dsh_future = pool.submit(
                _run_engine, task, "deepseek_agent", system_prompt, user_prompt,
                langfuse_host)
            cc_result = cc_future.result()
            dsh_result = dsh_future.result()

        cc_score = _score_of(cc_result)
        dsh_score = _score_of(dsh_result)
        diff = (
            round(cc_score - dsh_score, 2)
            if cc_score is not None and dsh_score is not None else None
        )
        comparisons.append({
            "data_index": idx,
            "row_status": sample_rows[idx]["row_status"],
            "existing_average_score": sample_rows[idx]["existing_average_score"],
            "user_input": str(payload.get("user_input") or "")[:200],
            "claude_agent": {
                "score": cc_score,
                "feedback": (cc_result[0].get("feedback") if cc_result else None),
                "failed": cc_score is None,
            },
            "deepseek_agent": {
                "score": dsh_score,
                "feedback": (dsh_result[0].get("feedback") if dsh_result else None),
                "failed": dsh_score is None,
            },
            "score_diff": diff,
        })
        if len(comparisons) >= sample_count:
            break

    both = [c for c in comparisons
            if c["claude_agent"]["score"] is not None
            and c["deepseek_agent"]["score"] is not None]
    abs_diffs = [abs(c["score_diff"]) for c in both if c["score_diff"] is not None]
    summary = {
        "sample_count": len(comparisons),
        "both_scored": len(both),
        "claude_failed": sum(1 for c in comparisons if c["claude_agent"]["failed"]),
        "deepseek_failed": sum(1 for c in comparisons if c["deepseek_agent"]["failed"]),
        "mean_abs_diff": round(sum(abs_diffs) / len(abs_diffs), 2) if abs_diffs else None,
        "max_abs_diff": round(max(abs_diffs), 2) if abs_diffs else None,
        "within_tolerance_rate": (
            round(sum(1 for d in abs_diffs if d <= WITHIN_TOLERANCE) / len(abs_diffs), 4)
            if abs_diffs else None
        ),
        "tolerance": WITHIN_TOLERANCE,
    }

    return {
        "task_id": task.id,
        "task_name": task.name,
        "execution_number": getattr(task, "execution_count", None) or 1,
        "engines": {
            "claude_agent": engine_configs["claude_agent"],
            "deepseek_agent": engine_configs["deepseek_agent"],
        },
        "summary": summary,
        "rows": comparisons,
    }
