"""DSH 配置加 protocol 列：支持非 DeepSeek 协议网关（openai 兼容 / anthropic messages）

背景：网关形态各异（DeepSeek 官方 / OpenAI 兼容 / Anthropic Messages，如内部
LiteLLM UAT 网关的 /api-sse-anthropic）。原实现 HTTP 直调写死 OpenAI 协议、
Agent 链路把自定义 provider 直传 DeepSeekHarness（sdk-minimal 无该路由），
第三方协议网关完全不可用。protocol 用于分派 HTTP 探测方式，并在 Agent 链路
自动构造 pi-ai provider patch（详见 deepseek_adapter.build_provider_patch）。
"""
import sqlalchemy as sa
from alembic import op

revision = "a3f8c2d61b04"
down_revision = "b8e1f5c3a9d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deepseek_harness_configs",
        sa.Column("protocol", sa.String(), nullable=False, server_default="deepseek",
                  comment="网关 API 协议：deepseek|openai|anthropic"),
    )


def downgrade() -> None:
    op.drop_column("deepseek_harness_configs", "protocol")
