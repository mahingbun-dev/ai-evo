"""分享链接数据模型（M5 QE5-4，报告外发）

免登录只读报告的外发凭证：
- token：``sh_`` 前缀 + 32 字节 urlsafe 随机（256bit 熵 ≥ 方案要求的 128bit），
  **明文仅创建时返回一次**，库内只存 sha256 哈希（与 API Key 同款口径）；
- resource：task_id + execution_number（可空=跟随最新执行，整数=锁定该次）；
- row_scope：all-全部明细行 / summary-仅汇总（读时从 result 摘除行级数组）；
- 生命周期：expires_at 可空（空=永久）；revoked_at 非空即吊销——吊销/过期/
  不存在对外**无差别 404**（枚举防护），公开读端点另有 Redis IP 限流。
"""
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey, Index
from sqlalchemy.sql import func

from app.db.session import Base


class ShareLink(Base):
    """报告分享链接（M5 QE5-4）"""
    __tablename__ = "share_links"

    id = Column(String, primary_key=True, index=True)
    # sha256(token) hex——明文 token 仅创建响应返回一次，不落库
    token_hash = Column(String(64), nullable=False, unique=True, index=True,
                        comment="sha256(明文 token) hex；明文仅创建时返回一次")
    task_id = Column(String, ForeignKey("evaluation_tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True, comment="分享的评测任务ID")
    # 空=跟随最新执行（读时取最新执行记录）；整数=锁定该次执行报告
    execution_number = Column(Integer, nullable=True,
                              comment="锁定执行次数（空=跟随最新执行）")
    expires_at = Column(DateTime(timezone=True), nullable=True, comment="过期时间（空=永久）")
    # all-全部明细行 / summary-仅汇总（读时摘除 result.items 行级数组）
    row_scope = Column(String(10), nullable=False, default="all", server_default="all",
                       comment="可见范围：all-全部 / summary-仅汇总")
    revoked_at = Column(DateTime(timezone=True), nullable=True, comment="吊销时间（非空即失效）")
    created_by = Column(String, nullable=True, comment="创建人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
