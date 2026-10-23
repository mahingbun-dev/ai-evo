"""A/B 模板版本对照服务（M5 QE5-2，任务化实验）

「同任务两模板版本」对照：从源评测任务复制出两个变体任务（除模板派生
字段外与源任务完全一致，模板派生字段分别取两版本 content 覆盖），落
ab_test_sessions（mode=template）作为对照记录。变体任务是普通评测任务
——运行走既有 run 入口（RBAC/配额/互斥全口径），执行历史天然留存可比。
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.ab_test_models import ABTestSession

logger = logging.getLogger(__name__)


class ABTemplateSessionError(ValueError):
    """对照实验创建参数错误（源任务/模板/版本不满足前提）"""


class ABTemplateSessionService:
    """A/B 模板版本对照（任务化实验）服务"""

    def __init__(self, db: Session):
        self.db = db

    # ---------------- 创建 ----------------

    def create_session(
        self,
        source_task_id: str,
        template_id: str,
        version_a: int,
        version_b: int,
        name: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> ABTestSession:
        """创建对照实验：两个模板版本 × 源任务 → 两个变体任务 + 对照记录

        变体任务经 EvaluationTaskService.create_task 创建（全量校验口径与
        手动建任务一致），模板派生字段（场景/场景描述/评测指令/维度/最高分/
        分值标准/裁判变量/完整指令模板）取版本 content 覆盖。
        """
        from app.models.task_models import EvaluationTask
        from app.models.template_models import EvaluationTemplate
        from app.services.evaluation_task_service import EvaluationTaskService
        from app.services.template_version_service import TemplateVersionService

        if version_a == version_b:
            raise ABTemplateSessionError("对照两版本不能相同")

        source = self.db.query(EvaluationTask).filter(
            EvaluationTask.id == source_task_id, EvaluationTask.is_active == True  # noqa: E712
        ).first()
        if not source:
            raise ABTemplateSessionError(f"源任务不存在: {source_task_id}")

        template = self.db.query(EvaluationTemplate).filter(
            EvaluationTemplate.id == template_id
        ).first()
        if not template:
            raise ABTemplateSessionError(f"评测模板不存在: {template_id}")

        version_service = TemplateVersionService(self.db)
        for version_no in (version_a, version_b):
            if not version_service.version_exists(template_id, version_no):
                raise ABTemplateSessionError(
                    f"模板不存在版本 {version_no}，请先在模板管理页打版"
                )

        task_service = EvaluationTaskService(self.db)
        task_ids: Dict[str, str] = {}
        for side, version_no in (("a", version_a), ("b", version_b)):
            content = version_service.get_version_content(template_id, version_no)
            variant = task_service.create_task(
                name=f"{source.name} [模板v{version_no}]",
                evaluation_type=source.evaluation_type,
                description=source.description,
                # ---- 与源任务一致（对照变量只有模板版本） ----
                model_config=source.model_config,
                model_provider=source.model_provider,
                model_endpoint=source.model_endpoint,
                model_output_source=source.model_output_source,
                model_output_field=source.model_output_field,
                dataset_id=source.dataset_id,
                dataset_version_no=getattr(source, "dataset_version_no", None),
                judge_model=source.judge_model,
                judge_engine=source.judge_engine,
                consistency_config=source.consistency_config,
                success_rate_config=source.success_rate_config,
                rag_config=source.rag_config,
                latency_config=source.latency_config,
                binary_metrics_config=source.binary_metrics_config,
                plugin_config=source.plugin_config,
                safety_config=source.safety_config,
                multi_judge_config=source.multi_judge_config,
                parallel_config=source.parallel_config,
                runtime_variables=source.runtime_variables,
                variable_sources=source.variable_sources,
                variable_types=source.variable_types,
                variable_mappings=source.variable_mappings,
                enable_user_import=source.enable_user_import,
                user_import_template=source.user_import_template,
                judge_count=source.judge_count,
                team_code=source.team_code,
                evaluation_targets=source.evaluation_targets,
                validation_enabled=source.validation_enabled,
                gate_config=source.gate_config,
                verifiers=source.verifiers,
                trajectory_config=source.trajectory_config,
                trials=source.trials,
                judge_guard_disabled=source.judge_guard_disabled,
                judge_canary=source.judge_canary,
                # ---- 模板派生字段：取版本 content 覆盖（与任务表单引用模板的
                # 填充口径一致，见 Tasks.vue handleTemplateChange） ----
                template_id=template_id,
                evaluation_scene=content.get("scene") or "",
                scene_desc=content.get("scene_description") or "",
                judge_prompt=content.get("judge_prompt"),
                evaluation_metrics=content.get("evaluation_metrics") or [],
                max_score=content.get("max_score") or 10,
                score_details=content.get("score_details"),
                judge_variable_config=content.get("judge_variable_config"),
                created_by=created_by,
            )
            task_ids[side] = variant.id

        session = ABTestSession(
            id=f"abtest_{uuid.uuid4().hex[:12]}",
            name=name,
            mode="template",
            model_a_id=None,
            model_b_id=None,
            source_task_id=source_task_id,
            template_id=template_id,
            template_version_a=version_a,
            template_version_b=version_b,
            task_a_id=task_ids["a"],
            task_b_id=task_ids["b"],
            status="pending",
            created_by=created_by,
        )
        self.db.add(session)
        self.db.commit()
        logger.info(
            "A/B 模板版本对照实验创建: session=%s, source_task=%s, template=%s, v%s vs v%s → %s / %s",
            session.id, source_task_id, template_id, version_a, version_b,
            task_ids["a"], task_ids["b"],
        )
        return session

    # ---------------- 查询 ----------------

    def get_session(self, session_id: str) -> Optional[ABTestSession]:
        return self.db.query(ABTestSession).filter(ABTestSession.id == session_id).first()

    def list_sessions(
        self,
        page: int = 1,
        page_size: int = 20,
        keyword: str = "",
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """模板对照会话列表（新→旧分页；W19 归属口径与模型会话一致）"""
        query = self.db.query(ABTestSession).filter(ABTestSession.mode == "template")
        if keyword:
            query = query.filter(ABTestSession.name.ilike(f"%{keyword}%"))
        if created_by is not None:
            query = query.filter(
                (ABTestSession.created_by == created_by)
                | (ABTestSession.created_by.is_(None))
            )
        total = query.count()
        items = (
            query.order_by(ABTestSession.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {"total": total, "page": page, "page_size": page_size, "items": items}

    # ---------------- 对照结果（读时计算，不落库） ----------------

    def comparison_payload(self, session: ABTestSession) -> Dict[str, Any]:
        """对照载荷：两侧变体任务当前状态 + 最新执行摘要（均分/执行号/完成时间）"""
        from app.models.task_models import EvaluationTask, TaskExecutionRecord

        def _side(task_id: Optional[str]) -> Optional[Dict[str, Any]]:
            if not task_id:
                return None
            task = self.db.query(EvaluationTask).filter(
                EvaluationTask.id == task_id
            ).first()
            if not task:
                return {"task_id": task_id, "missing": True}
            record = (
                self.db.query(TaskExecutionRecord)
                .filter(TaskExecutionRecord.task_id == task_id)
                .order_by(TaskExecutionRecord.execution_number.desc())
                .first()
            )
            summary: Dict[str, Any] = {
                "task_id": task_id,
                "task_name": task.name,
                "task_status": task.status,
                "execution_count": task.execution_count or 0,
            }
            if record:
                result = record.result if isinstance(record.result, dict) else {}
                summary["latest_execution"] = {
                    "execution_number": record.execution_number,
                    "status": record.status,
                    "completed_at": record.completed_at.isoformat() if record.completed_at else None,
                    "average_score": result.get("average_score"),
                    "pass_rate": result.get("pass_rate"),
                }
            else:
                summary["latest_execution"] = None
            return summary

        side_a = _side(session.task_a_id)
        side_b = _side(session.task_b_id)
        # 会话状态推导：两侧均有已完成执行 → completed；任一失败终态 → failed；
        # 否则 pending（未跑/进行中）
        statuses = [s["latest_execution"]["status"] for s in (side_a, side_b)
                    if s and s.get("latest_execution")]
        if statuses and all(s == "completed" for s in statuses):
            state = "completed"
        elif any(s in ("failed", "stopped") for s in statuses):
            state = "failed"
        else:
            state = "pending"
        return {"side_a": side_a, "side_b": side_b, "state": state}
