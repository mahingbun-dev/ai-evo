"""
插件市场数据模型

插件市场 = 一个 git 仓库（Claude Code marketplace），克隆到本地后
供插件评测从中选择插件/skill 作为评测对象。
"""
from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.db.session import Base


class PluginMarketplace(Base):
    """插件市场（git 仓库克隆）"""
    __tablename__ = "plugin_marketplaces"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    name = Column(String(100), nullable=False, comment="市场名称（展示用）")
    git_url = Column(String(500), nullable=False, unique=True, comment="市场 git 仓库地址")
    branch = Column(String(100), nullable=True, comment="分支（空=默认分支）")
    local_path = Column(String(500), nullable=True, comment="本地克隆目录绝对路径")
    sync_status = Column(String(20), nullable=False, default="pending", comment="同步状态：pending/syncing/ok/failed")
    sync_error = Column(Text, nullable=True, comment="最近一次同步失败原因")
    last_synced_at = Column(DateTime(timezone=True), nullable=True, comment="最近成功同步时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
