"""评测模板版本服务（M5 QE5-2）

打版（手动 / 编辑前自动留存）/列表/字段级 diff/恢复。版本事实源是
template_versions.content（打版时模板完整内容快照），不可变；A/B 模板
版本对照（任务化实验）以版本内容派生变体任务，见 ab_template_service。
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# diff 响应中单字段值的展示截断长度（模板正文/维度结构可很长，diff 面板
# 只需要可读对照；完整内容以版本 content 为准）
DIFF_VALUE_TRUNCATE_CHARS = 300

# 版本内容快照覆盖的模板字段（与恢复写回的字段一一对应）
CONTENT_FIELDS = (
    "name", "scene", "scene_description", "judge_prompt",
    "evaluation_metrics", "score_details", "max_score", "steps",
    "judge_variable_config", "instruction_template",
)


class TemplateVersionNotFound(ValueError):
    """版本不存在（或版本不属于该模板）"""


def _slim_value(value: Any) -> Any:
    """diff 展示用字段值瘦身：长标量/结构截断为可读摘要"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= DIFF_VALUE_TRUNCATE_CHARS:
            return value
        return value[:DIFF_VALUE_TRUNCATE_CHARS] + f"…（截断，共 {len(value)} 字符）"
    try:
        import json

        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= DIFF_VALUE_TRUNCATE_CHARS:
        return value
    return text[:DIFF_VALUE_TRUNCATE_CHARS] + f"…（截断，共 {len(text)} 字符）"


class TemplateVersionService:
    """评测模板版本服务"""

    def __init__(self, db: Session):
        self.db = db

    # ---------------- 打版 ----------------

    @staticmethod
    def snapshot_content(template) -> Dict[str, Any]:
        """模板完整内容快照（版本 content 的字段口径，恢复时按同字段写回）"""
        content: Dict[str, Any] = {}
        for field in CONTENT_FIELDS:
            content[field] = getattr(template, field, None)
        return content

    def create_version(
        self,
        template,
        changelog: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """以模板当前内容打版（content 全量快照 + 单调递增版本号）

        Args:
            template: EvaluationTemplate ORM 对象（调用方已查出校验）
        """
        from sqlalchemy import func
        from sqlalchemy.exc import IntegrityError

        from app.models.template_models import TemplateVersion

        content = self.snapshot_content(template)
        version = None
        for attempt in range(2):
            max_no = self.db.query(func.max(TemplateVersion.version_no)).filter(
                TemplateVersion.template_id == template.id
            ).scalar() or 0
            version = TemplateVersion(
                id=f"tplv_{uuid.uuid4().hex[:12]}",
                template_id=template.id,
                version_no=max_no + 1,
                content=content,
                changelog=changelog,
                created_by=created_by,
            )
            self.db.add(version)
            try:
                self.db.commit()
                break
            except IntegrityError:
                # 并发打版撞唯一约束：回滚后重取 max 再试一次
                self.db.rollback()
                if attempt == 1:
                    raise
        self.db.refresh(version)
        logger.info(
            "模板打版成功: template_id=%s, version_no=%s", template.id, version.version_no
        )
        return self._to_dict(version)

    def auto_version_before_update(
        self,
        template,
        changelog: str,
        created_by: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """编辑前自动留存：先把当前（改前）内容打版，再由调用方应用编辑

        语义：携带 version_changelog 的编辑是显式留档意图，改前状态必进版本链
        ——模板任何被显式版本化编辑覆盖的状态都可回溯。
        """
        return self.create_version(
            template, changelog=changelog or "编辑前自动留存", created_by=created_by
        )

    # ---------------- 查询 ----------------

    def list_versions(self, template_id: str) -> List[Dict[str, Any]]:
        """版本列表（新→旧，列表项不携带 content 大字段）"""
        from app.models.template_models import TemplateVersion

        versions = self.db.query(TemplateVersion).filter(
            TemplateVersion.template_id == template_id
        ).order_by(TemplateVersion.version_no.desc()).all()
        return [self._to_dict(v, include_content=False) for v in versions]

    def get_version(self, template_id: str, version_no: int) -> Dict[str, Any]:
        """单版本详情（含完整 content）"""
        return self._to_dict(self._get_version_or_raise(template_id, version_no))

    def get_version_content(self, template_id: str, version_no: int) -> Dict[str, Any]:
        """版本内容（A/B 模板对照派生变体任务用）"""
        return dict(self._get_version_or_raise(template_id, version_no).content or {})

    def version_exists(self, template_id: str, version_no: int) -> bool:
        from app.models.template_models import TemplateVersion

        return self.db.query(TemplateVersion.id).filter(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version_no == version_no,
        ).first() is not None

    def _get_version_or_raise(self, template_id: str, version_no: int):
        from app.models.template_models import TemplateVersion

        version = self.db.query(TemplateVersion).filter(
            TemplateVersion.template_id == template_id,
            TemplateVersion.version_no == version_no,
        ).first()
        if not version:
            raise TemplateVersionNotFound(f"模板 {template_id} 不存在版本 {version_no}")
        return version

    # ---------------- 字段级 diff ----------------

    def diff_versions(self, template_id: str, from_no: int, to_no: int) -> Dict[str, Any]:
        """两版本的字段级 diff：按 content 字段对齐，输出逐字段 from/to 对照

        Returns:
            {from/to 版本元信息, summary: {changed, unchanged},
             fields: [{field, status: changed, from, to}]（只含变更字段）}
        """
        from_version = self._get_version_or_raise(template_id, from_no)
        to_version = self._get_version_or_raise(template_id, to_no)

        from_content = from_version.content or {}
        to_content = to_version.content or {}

        fields: List[Dict[str, Any]] = []
        changed = unchanged = 0
        for field in CONTENT_FIELDS:
            from_value = from_content.get(field)
            to_value = to_content.get(field)
            if from_value == to_value:
                unchanged += 1
            else:
                changed += 1
                fields.append({
                    "field": field,
                    "status": "changed",
                    "from": _slim_value(from_value),
                    "to": _slim_value(to_value),
                })

        return {
            "template_id": template_id,
            "from": self._to_dict(from_version, include_content=False),
            "to": self._to_dict(to_version, include_content=False),
            "summary": {"changed": changed, "unchanged": unchanged},
            "fields": fields,
        }

    # ---------------- 恢复 ----------------

    def restore_version(
        self,
        template,
        version_no: int,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把模板活内容恢复到指定版本

        语义：先把恢复前活内容自动留存进版本链（改前状态不丢），再按版本
        content 覆盖活模板字段（content 覆盖口径与快照口径一致）。
        """
        version = self._get_version_or_raise(template.id, version_no)
        content = version.content or {}

        pre = self.create_version(
            template,
            changelog=f"恢复到 v{version_no} 前的自动留存",
            created_by=created_by,
        )

        for field in CONTENT_FIELDS:
            if field in content:
                setattr(template, field, content[field])
        self.db.commit()
        self.db.refresh(template)

        logger.info(
            "模板版本恢复完成: template_id=%s, version_no=%s（改前留存为 v%s）",
            template.id, version_no, pre["version_no"],
        )
        return {
            "template_id": template.id,
            "restored_version": version_no,
            "pre_restore_version": pre["version_no"],
        }

    # ---------------- 内部 ----------------

    @staticmethod
    def _to_dict(version, include_content: bool = True) -> Dict[str, Any]:
        result = {
            "id": version.id,
            "template_id": version.template_id,
            "version_no": version.version_no,
            "changelog": version.changelog,
            "created_by": version.created_by,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
        if include_content:
            result["content"] = version.content
        return result
