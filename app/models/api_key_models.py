"""
API Key 数据模型（M1 QE1-1，门禁闭环：评测结果可卡发布）

CI/CLI 侧用 Bearer aievo_sk_... 长期凭证替代用户 JWT 调用平台 API。
安全口径（plans/2026-10-22-Agent质量工程平台开发方案.md §M1「API Key 鉴权」）：
- 明文仅在创建时返回一次，库内只存 sha256；
- key_prefix 存随机段前 8 位（展示用 + 认证扫描索引，ix_api_keys_prefix）；
- 吊销 = revoked_at 置时间（软删，审计留痕），不做物理删除。
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON
from sqlalchemy.sql import func
from app.db.session import Base


class ApiKey(Base):
    """API Key 表（Bearer aievo_sk_... 凭证）"""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    user_code = Column(String, nullable=False, index=True, comment="所属用户（users.user_code）")
    team_code = Column(String, nullable=True, comment="所属团队（资源隔离口径与任务列表一致，空=DEFAULT）")
    name = Column(String(100), nullable=False, comment="Key 名称（展示用）")
    # 随机段前 8 位：认证时按前缀单查询扫描（ix_api_keys_prefix），列表页展示
    # 用。所有 Key 的 token 前缀 aievo_sk_ 相同，故前缀必须取随机段才有区分度。
    key_prefix = Column(String(8), nullable=False, index=True, comment="随机段前 8 位（展示 + 认证扫描索引）")
    key_hash = Column(String(64), nullable=False, unique=True, comment="完整 token 的 sha256（十六进制）")
    # 库列为 JSONB（见迁移），ORM 用通用 JSON 保持 SQLite 单测可建表（与
    # task_models.TaskExecutionRecord 同款写法）
    scopes = Column(JSON, nullable=True, comment="授权 scope 列表（app/core/scopes.py 常量）")
    last_used_at = Column(DateTime(timezone=True), nullable=True, comment="最近一次认证成功时间")
    expires_at = Column(DateTime(timezone=True), nullable=True, comment="过期时间（空=永不过期）")
    revoked_at = Column(DateTime(timezone=True), nullable=True, comment="吊销时间（空=未吊销）")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
