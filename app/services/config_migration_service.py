"""旧版任务/模板配置迁移服务（契约 v3 §六）

把旧版「评分步骤 steps + 任务级 pre_scripts」结构迁移到 v3 的
「evaluation_targets 单一事实源 + 维度顺序即步骤」结构：

- 规则1（任务/模板）：steps 非空 → 合并进 score_details（追加「评分步骤」编号块），steps 置 None
- 规则2（仅任务）：pre_scripts 非空 → 逐条转为 evaluation_targets 的 script_output 行，pre_scripts 置 None
- 规则3（仅任务，脚本统一落库改造）：evaluation_targets script_output 行与转换产物中的
  script_id=builtin:<name> 历史引用改写为 DB 记录 id

幂等：迁移完成后 steps/pre_scripts 均为 None 且无 builtin: 前缀引用，重复调用直接跳过（migrated=False）。
读取兼容：GET 侧不做服务端归一化（前端负责展示归一化）；执行链路保留双路径兜底，
未迁移任务的 pre_scripts 仍可执行，迁移不改变执行语义。
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.task_models import EvaluationTask
from app.models.template_models import EvaluationTemplate

logger = logging.getLogger(__name__)

# 迁移追加块的标题（编号格式与 instruction_builder 的步骤编号一致：{i+1}. {step}）
MIGRATED_STEPS_HEADER = "评分步骤："


class ConfigMigrationService:
    """旧版任务/模板配置迁移服务"""

    def __init__(self, db: Session):
        self.db = db

    # ---------- 单任务 ----------

    def migrate_task(self, task: EvaluationTask) -> Dict[str, Any]:
        """迁移单个任务（幂等），返回变更摘要

        Returns:
            {
              "task_id": 任务 id,
              "migrated": 是否发生变更（False=已迁移/无需迁移）,
              "steps_migrated": 合并进 score_details 的评分步骤行数,
              "pre_scripts_migrated": 转为 script_output 待评测数据行的脚本条数,
              "target_ids": 迁移生成的待评测数据行 id 列表,
              "changes": 变更摘要文案列表,
            }
        """
        changes: List[str] = []
        target_ids: List[str] = []
        script_lookup: Optional[Dict[str, Dict[str, Any]]] = None

        # 规则1：steps 非空 → 合并进 score_details，steps 置 None
        steps = _as_steps_list(task.steps)
        steps_migrated = len(steps)
        if steps:
            task.score_details = _append_steps_block(task.score_details, steps)
            task.steps = None
            changes.append(f"steps（{steps_migrated} 行）已合并进 score_details")

        # 规则2：pre_scripts 非空 → 逐条转为 script_output 待评测数据行，pre_scripts 置 None
        pre_scripts = task.pre_scripts
        pre_scripts_migrated = 0
        if pre_scripts:
            script_lookup = self._load_script_lookup()
            merged_targets = list(task.evaluation_targets or [])
            existing_ids = {
                row.get("id") for row in merged_targets if isinstance(row, dict)
            }
            for index, step in enumerate(pre_scripts):
                if not isinstance(step, dict):
                    logger.warning(f"[ConfigMigration] 任务 {task.id} pre_scripts[{index}] 非对象，跳过该条")
                    continue
                row = _build_script_target_row(task.id, index, step, script_lookup)
                if row["id"] in existing_ids:
                    continue
                merged_targets.append(row)
                existing_ids.add(row["id"])
                target_ids.append(row["id"])
                pre_scripts_migrated += 1
            task.evaluation_targets = merged_targets
            task.pre_scripts = None
            changes.append(f"pre_scripts（{pre_scripts_migrated} 条）已转为 script_output 待评测数据行")

        # 规则3：evaluation_targets script_output 行的 script_id=builtin:<name> 历史引用
        # 改写为 DB 记录 id（脚本统一落库改造；脚本不存在则保留原值，由校验层提示）
        targets = task.evaluation_targets
        if targets:
            builtin_refs = 0
            rewritten = 0
            new_targets: List[Any] = []
            for row in targets:
                if (
                    isinstance(row, dict)
                    and row.get("source") == "script_output"
                    and isinstance(row.get("source_config"), dict)
                    and str(row["source_config"].get("script_id") or "").startswith("builtin:")
                ):
                    builtin_refs += 1
                    if script_lookup is None:
                        script_lookup = self._load_script_lookup()
                    definition = script_lookup.get(row["source_config"].get("script_id"))
                    if definition:
                        row["source_config"]["script_id"] = definition["id"]
                        rewritten += 1
                new_targets.append(row)
            if rewritten:
                task.evaluation_targets = new_targets
                changes.append(
                    f"待评测数据 script 行的 builtin: 历史引用（{rewritten}/{builtin_refs} 处）已改写为脚本记录 id"
                )
            elif builtin_refs:
                logger.warning(
                    f"[ConfigMigration] 任务 {task.id} 存在 {builtin_refs} 处 builtin: 引用但未解析到脚本记录，保留原值"
                )

        migrated = bool(changes)
        if migrated:
            self.db.commit()
            logger.info(f"[ConfigMigration] 任务 {task.id} 迁移完成: {'；'.join(changes)}")
        return {
            "task_id": task.id,
            "migrated": migrated,
            "steps_migrated": steps_migrated,
            "pre_scripts_migrated": pre_scripts_migrated,
            "target_ids": target_ids,
            "changes": changes,
        }

    # ---------- 单模板 ----------

    def migrate_template(self, template: EvaluationTemplate) -> Dict[str, Any]:
        """迁移单个模板（仅规则1；模板 steps 为 Text 按 \\n 拆分），幂等"""
        changes: List[str] = []
        steps = _as_steps_list(template.steps)
        if steps:
            template.score_details = _append_steps_block(template.score_details, steps)
            template.steps = None
            changes.append(f"steps（{len(steps)} 行）已合并进 score_details")

        migrated = bool(changes)
        if migrated:
            self.db.commit()
            logger.info(f"[ConfigMigration] 模板 {template.id} 迁移完成: {'；'.join(changes)}")
        return {
            "id": template.id,
            "migrated": migrated,
            "steps_migrated": len(steps),
            "changes": changes,
        }

    # ---------- 批量 ----------

    def migrate_all_tasks(self, team_code: Optional[str] = None) -> Dict[str, Any]:
        """批量迁移全部未删除任务（可选团队过滤），返回 {total, migrated, skipped, errors}"""
        query = self.db.query(EvaluationTask).filter(EvaluationTask.is_active == True)  # noqa: E712
        if team_code:
            query = query.filter(EvaluationTask.team_code == team_code)
        return self._migrate_all(query.all(), self.migrate_task)

    def migrate_all_templates(self) -> Dict[str, Any]:
        """批量迁移全部未删除模板（仅规则1），返回 {total, migrated, skipped, errors}"""
        templates = self.db.query(EvaluationTemplate).filter(EvaluationTemplate.is_active == True).all()  # noqa: E712
        return self._migrate_all(templates, self.migrate_template)

    def _migrate_all(self, items: List[Any], migrate_one) -> Dict[str, Any]:
        """逐条迁移并汇总结果；单条失败回滚并记入 errors，不阻断其余条目"""
        total = migrated = skipped = 0
        errors: List[Dict[str, str]] = []
        for item in items:
            total += 1
            try:
                summary = migrate_one(item)
                if summary.get("migrated"):
                    migrated += 1
                else:
                    skipped += 1
            except Exception as e:  # noqa: BLE001 - 批量迁移边界：单条失败不影响整体
                self.db.rollback()
                errors.append({"id": item.id, "error": str(e)})
                logger.warning(f"[ConfigMigration] 迁移失败 {item.id}: {e}")
        return {"total": total, "migrated": migrated, "skipped": skipped, "errors": errors}

    # ---------- 内部 ----------

    def _load_script_lookup(self) -> Dict[str, Dict[str, Any]]:
        """DB 脚本记录视图（含停用）+ builtin:<name> 历史别名（先幂等迁移内置脚本）"""
        from app.services.pre_judge_script_service import ensure_builtin_scripts_migrated
        from app.models.pre_judge_models import PreJudgeScript

        ensure_builtin_scripts_migrated(self.db)
        lookup: Dict[str, Dict[str, Any]] = {}
        for row in self.db.query(PreJudgeScript).all():
            entry = {
                "id": row.id,
                "name": row.name,
                "exec_type": row.exec_type,
                "output_schema": row.output_schema,
            }
            lookup[row.id] = entry
            lookup.setdefault(f"builtin:{row.name}", entry)
            lookup.setdefault(row.name, entry)
        return lookup


# ---------------- 模块级工具 ----------------

def _as_steps_list(value: Any) -> List[str]:
    """把任务 steps（JSON 数组）或模板 steps（Text）归一为去空白的步骤行列表"""
    if not value:
        return []
    if isinstance(value, str):
        lines = [line.strip() for line in value.split("\n")]
        return [line for line in lines if line]
    if isinstance(value, list):
        return [str(line).strip() for line in value if str(line or "").strip()]
    return []


def _append_steps_block(score_details: Optional[str], steps: List[str]) -> str:
    """在评分标准末尾追加「评分步骤」编号块（契约 v3 §六：score_details += 编号步骤）"""
    numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    base = (score_details or "").rstrip()
    block = f"{MIGRATED_STEPS_HEADER}\n{numbered}"
    return f"{base}\n\n{block}" if base else block


def _build_script_target_row(
    task_id: str,
    index: int,
    step: Dict[str, Any],
    script_lookup: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """把旧版 pre_scripts 步骤转为 script_output 待评测数据行（契约 v3 §六 规则2）

    - id：target_mig_{index}_{task.id 前 8 位}
    - display_name：output_var 或脚本名（脚本未知时回退 script_id）
    - output_key：脚本 output_schema 首键（无 schema/未知脚本 → None，运行时整体 JSON 兜底）
    - required=False：脚本失败（on_fail=continue）时行内容为空，走既有空值为 0 分逻辑
    """
    script_id = str(step.get("script_id") or "")
    output_var = step.get("output_var")
    definition = script_lookup.get(script_id) or {}
    # builtin:<name> 历史引用改写为 DB 记录 id（规则3：转换产物直接落新 id）
    script_id = definition.get("id") or script_id

    if output_var:
        display_name = output_var
    else:
        display_name = definition.get("name") or script_id

    # output_schema 首键（dict 保序，首个键即首键）
    output_schema = definition.get("output_schema")
    first_key = None
    if isinstance(output_schema, dict) and output_schema:
        first_key = next(iter(output_schema.keys()))

    return {
        "id": f"target_mig_{index}_{str(task_id)[:8]}",
        "display_name": display_name,
        "source": "script_output",
        "required": False,
        "source_config": {
            "script_id": script_id,
            "output_key": first_key,
            "input_mapping": step.get("input_mapping") or {},
            "params": step.get("params") or {},
            "on_fail": step.get("on_fail") or "continue",
            "output_var": output_var,
        },
    }
