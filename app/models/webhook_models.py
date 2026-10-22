"""
Webhook 数据模型（M1 QE1-3，门禁闭环的通知面）

- webhook_endpoints：团队级 webhook 接收端登记（URL + 签名 secret + 订阅事件）；
- webhook_deliveries：每次投递留痕（状态/响应码/尝试次数/最后错误），
  按方案「deliveries 留痕可查」——送达历史是排障与审计的事实源。

安全口径：secret 仅创建时明文返回一次（与 API Key 同款）；投递时按
app/services/webhook_url_guard.py 做 SSRF 校验（每次发送前重验，防注册后
DNS 重绑定）。
"""
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.db.session import Base


class WebhookEndpoint(Base):
    """webhook 接收端（团队级）"""
    __tablename__ = "webhook_endpoints"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    team_code = Column(String, nullable=False, index=True, comment="所属团队（资源隔离口径，空=DEFAULT）")
    name = Column(String(100), nullable=False, comment="端点名称（展示用）")
    url = Column(String(500), nullable=False, comment="接收端 URL（http/https，投递前做 SSRF 校验）")
    # 明文 secret 仅创建时返回一次；库存完整值（投递侧签名需要，不可像 API Key
    # 一样只存哈希——HMAC 签名要原值参与运算）
    secret = Column(String, nullable=False, comment="HMAC 签名 secret（whsec_ 前缀）")
    events = Column(JSON, nullable=True, comment="订阅事件列表（webhook_service.WEBHOOK_EVENTS 子集）")
    active = Column(Boolean, nullable=False, default=True, server_default="true",
                    comment="是否启用（停用后不再投递，保留历史留痕）")
    created_by = Column(String, nullable=True, comment="创建人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")


class WebhookDelivery(Base):
    """webhook 投递留痕（一次事件 × 一个端点 = 一行，重试不另起行）"""
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    endpoint_id = Column(Integer, ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    event = Column(String(100), nullable=False, comment="事件名（WEBHOOK_EVENTS 之一）")
    # 库列为 JSONB（见迁移），ORM 用通用 JSON 保持 SQLite 单测可建表
    payload = Column(JSON, nullable=True, comment="事件报文（投递原文，审计留痕）")
    status = Column(String(20), nullable=False, default="pending",
                    server_default="pending", comment="状态：pending/success/failed")
    response_status = Column(Integer, nullable=True, comment="接收端 HTTP 响应码（送达尝试的最后一次）")
    attempts = Column(Integer, nullable=False, default=0, server_default="0",
                      comment="总尝试次数（首次 + 重试）")
    last_error = Column(Text, nullable=True, comment="最近一次失败原因（重试耗尽/SSRF 拒绝/超时等）")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
                        comment="最近状态变更时间")
