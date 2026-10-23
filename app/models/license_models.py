"""平台 License 数据模型（M6 QE6-1）

- platform_licenses：超管上传的 license 文件原文（JSON 信封：payload+sig）。
  每次上传追加一行，**最新一行即当前生效 license**（重传坏文件会覆盖生效态、
  服务层降级 oss——上传端点已做验签前置拒绝，存储行的坏态只可能来自
  「上传后改公钥/时钟越界」这类环境变化）；
  不存解析后的 claims——验签结果由 license_service 启动/上传时计算并缓存，
  库里只保留可复核的原文与指纹（sha256），防「存结论不存证据」。
"""
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from app.db.session import Base


class PlatformLicense(Base):
    """license 文件上传历史（最新一行为当前生效 license；M6 QE6-1）"""
    __tablename__ = "platform_licenses"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    # license 文件原文（自包含 JSON 信封：payload=base64url(canonical JSON claims)+sig）
    license_text = Column(Text, nullable=False, comment="license 文件原文（JSON 信封）")
    # sha256(license_text) hex——状态页展示与上传去重判据（重传同文件幂等提示）
    fingerprint = Column(String(64), nullable=False, comment="sha256(license_text) hex")
    customer = Column(String(200), nullable=True, comment="授权客户名（验签通过后冗余存，列表展示用）")
    tier = Column(String(20), nullable=True, comment="授权档位 oss/enterprise（冗余存）")
    uploaded_by = Column(String, nullable=True, comment="上传人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="上传时间")
