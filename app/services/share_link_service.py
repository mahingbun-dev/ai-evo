"""分享链接服务（M5 QE5-4，报告外发）

免登录只读报告的凭证管理：

- token 生成：``sh_`` 前缀 + ``secrets.token_urlsafe(24)``（192bit 熵 ≥ 方案
  的 128bit）；库内只存 sha256 hex，明文仅创建响应返回一次（与 API Key
  同款口径）；
- 读时校验：不存在/已吊销/已过期一律返回 None——公开端点对外**无差别
  404**（token 枚举拿不到任何区分信号），配 Redis IP 限流双保险；
- 报告解析：execution_number 空=跟随最新执行记录，整数=锁定该次；无执行
  记录时回退 task.result（与结果页兼容口径一致）；row_scope=summary 时
  从 result 摘除行级 ``items`` 数组只留聚合。
"""
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.share_models import ShareLink
from app.models.task_models import EvaluationTask, TaskExecutionRecord
from app.services.stats_service import BEIJING_TZ, beijing_now

logger = logging.getLogger(__name__)

# token 形态：sh_ 前缀 + 32 字节 urlsafe（192bit 熵，≥ 方案 128bit 下限）
TOKEN_PREFIX = "sh_"
_TOKEN_BYTES = 24

# 允许的有效期选项（天；None=永久）——API 层与前端弹窗共用同一集合
ALLOWED_EXPIRE_DAYS = (1, 7, 30, None)

# 行级可见范围
ROW_SCOPES = ("all", "summary")

# 公开端点限流（方案 §5.4：10 req/min/IP）
RATE_LIMIT_PER_MINUTE = 10
RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_KEY_PREFIX = "share_rate"


def generate_token() -> str:
    """生成分享 token（明文，仅创建响应返回一次；库内只存 sha256）"""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_share_rate_limit(redis_client: Any, ip: str) -> bool:
    """公开报告端点 IP 限流（固定窗口）：超限返回 False。

    与节流器同口径不计 X-Forwarded-For（可伪造）；窗口内首个请求设置
    TTL，窗口整体过期自动清零。
    """
    key = f"{_RATE_KEY_PREFIX}:{ip}"
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    return count <= RATE_LIMIT_PER_MINUTE


class ShareLinkService:
    """share_links 的创建/查询/吊销与只读报告解析"""

    def __init__(self, db: Session):
        self.db = db

    # ---------------- 创建 / 查询 / 吊销 ----------------

    def create_link(
        self,
        task_id: str,
        execution_number: Optional[int] = None,
        expires_days: Optional[int] = None,
        row_scope: str = "all",
        user_code: Optional[str] = None,
    ) -> Tuple[ShareLink, str]:
        """创建分享链接，返回 (链接行, 明文 token)。

        Raises:
            ValueError: 任务不存在/执行次数不存在/参数非法。
        """
        if expires_days not in ALLOWED_EXPIRE_DAYS:
            raise ValueError(f"有效期只支持 {list(ALLOWED_EXPIRE_DAYS)}（天，None=永久）")
        if row_scope not in ROW_SCOPES:
            raise ValueError(f"可见范围只支持 {list(ROW_SCOPES)}")

        task = self.db.query(EvaluationTask).filter(EvaluationTask.id == task_id).first()
        if not task or not task.is_active:
            raise ValueError("任务不存在或已删除")
        if execution_number is not None:
            record = (
                self.db.query(TaskExecutionRecord)
                .filter(TaskExecutionRecord.task_id == task_id,
                        TaskExecutionRecord.execution_number == execution_number)
                .first()
            )
            if record is None:
                raise ValueError(f"任务不存在第 {execution_number} 次执行记录")

        token = generate_token()
        link = ShareLink(
            id=f"shl_{uuid.uuid4().hex[:12]}",
            token_hash=hash_token(token),
            task_id=task_id,
            execution_number=execution_number,
            expires_at=(beijing_now() + timedelta(days=expires_days)
                        if expires_days is not None else None),
            row_scope=row_scope,
            created_by=user_code,
        )
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)
        return link, token

    def get_link_by_token(self, token: str) -> Optional[ShareLink]:
        """按明文 token 查有效链接；不存在/已吊销/已过期一律 None（无差别）。"""
        link = (
            self.db.query(ShareLink)
            .filter(ShareLink.token_hash == hash_token(token))
            .first()
        )
        if link is None or not self.is_active(link):
            return None
        return link

    @staticmethod
    def _as_beijing(dt):
        """naive datetime 补北京时区（sqlite 单测读回 naive；PG timestamptz 本就带）"""
        return dt if dt.tzinfo else dt.replace(tzinfo=BEIJING_TZ)

    @classmethod
    def is_active(cls, link: ShareLink) -> bool:
        if link.revoked_at is not None:
            return False
        if link.expires_at is not None and cls._as_beijing(link.expires_at) <= beijing_now():
            return False
        return True

    def list_links(self, task_id: str) -> List[Dict[str, Any]]:
        """任务的分享链接列表（新在前；明文 token 不存在也就不返回）"""
        links = (
            self.db.query(ShareLink)
            .filter(ShareLink.task_id == task_id)
            .order_by(ShareLink.created_at.desc())
            .all()
        )
        return [self.link_payload(link) for link in links]

    def link_payload(self, link: ShareLink) -> Dict[str, Any]:
        return {
            "id": link.id,
            "task_id": link.task_id,
            "execution_number": link.execution_number,
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
            "row_scope": link.row_scope,
            "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
            # 有效状态读时推导（吊销/过期不再可用）
            "active": self.is_active(link),
            "created_by": link.created_by,
            "created_at": link.created_at.isoformat() if link.created_at else None,
        }

    def revoke_link(self, task_id: str, link_id: str) -> Optional[ShareLink]:
        """吊销链接（限定在 task 范围内，防跨任务误吊/越权路径）。幂等返回行。"""
        link = (
            self.db.query(ShareLink)
            .filter(ShareLink.id == link_id, ShareLink.task_id == task_id)
            .first()
        )
        if link is None:
            return None
        if link.revoked_at is None:
            link.revoked_at = beijing_now()
            self.db.commit()
            self.db.refresh(link)
        return link

    # ---------------- 只读报告解析 ----------------

    def build_report(self, link: ShareLink) -> Dict[str, Any]:
        """解析只读报告 payload（公开端点返回体）。

        执行选择：execution_number 空=最新执行记录；无任何执行记录时回退
        task.result（与结果页 /history 兼容口径一致）。row_scope=summary
        摘除 result.items（行级明细）只留聚合。
        """
        task = (
            self.db.query(EvaluationTask)
            .filter(EvaluationTask.id == link.task_id)
            .first()
        )
        record = None
        query = self.db.query(TaskExecutionRecord).filter(
            TaskExecutionRecord.task_id == link.task_id)
        if link.execution_number is not None:
            record = query.filter(
                TaskExecutionRecord.execution_number == link.execution_number).first()
        else:
            record = (
                query.order_by(TaskExecutionRecord.execution_number.desc()).first())

        result: Optional[Dict[str, Any]] = None
        gate_result = None
        total_cost = None
        completed_at = None
        execution_number = link.execution_number
        if record is not None:
            result = record.result if isinstance(record.result, dict) else None
            gate_result = record.gate_result
            total_cost = record.total_cost
            completed_at = record.completed_at
            execution_number = record.execution_number
        elif task is not None and task.result:
            # 兼容：无执行记录但有任务级结果（与 get_result_history 同款回退）
            result = task.result if isinstance(task.result, dict) else None
            total_cost = task.total_cost
            completed_at = task.completed_at
            execution_number = task.execution_count

        if link.row_scope == "summary" and isinstance(result, dict):
            stripped = {k: v for k, v in result.items() if k != "items"}
            result = stripped

        return {
            "task": {
                "id": link.task_id,
                "name": task.name if task else "",
                "evaluation_type": task.evaluation_type if task else "",
                "status": (record.status if record else (task.status if task else "")),
            },
            "execution_number": execution_number,
            "row_scope": link.row_scope,
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
            "result": result,
            "gate_result": gate_result,
            "total_cost": total_cost,
            "completed_at": completed_at.isoformat() if completed_at else None,
        }
