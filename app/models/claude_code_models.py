"""
Claude Code 配置数据模型
全局单例配置：裁判引擎（Claude Code Agent / 裁判图 HTTP 直调）共用的
Anthropic 兼容网关连接参数
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean
from sqlalchemy.sql import func
from app.db.session import Base


class ClaudeCodeConfig(Base):
    """Claude Code 全局配置表（单例语义：仅一条 is_active=True 记录）"""
    __tablename__ = "claude_code_configs"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, default="默认配置", comment="配置名称")

    # Anthropic 兼容网关连接参数
    base_url = Column(String, nullable=False, comment="ANTHROPIC_BASE_URL，Anthropic 兼容网关地址")
    auth_token = Column(String, nullable=False, comment="ANTHROPIC_AUTH_TOKEN")
    model = Column(String, nullable=False, comment="模型名（ANTHROPIC_MODEL）")

    # 执行参数
    timeout_seconds = Column(Integer, default=300, comment="单次裁判调用超时（秒）")
    max_turns = Column(Integer, default=30, comment="Agent 裁判最大轮次")
    proxy_url = Column(String, nullable=True, comment="HTTPS_PROXY 代理地址（可选）")
    extra_env = Column(JSON, nullable=True, comment="附加环境变量（JSON 对象）")
    enable_script_mcp = Column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="是否把内置前处理脚本以 stdio MCP server 注入 claude-agent-sdk（默认关闭）",
    )

    is_active = Column(Boolean, default=True, comment="是否激活（单例）")
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
