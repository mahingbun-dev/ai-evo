"""M6 QE6-3: 用量计量——task_execution_records 增 model_usage 列

- 记录每次执行的模型 token 用量聚合 {model: {prompt_tokens, completion_tokens,
  total_tokens}}，采集漏斗为 model_caller（被测与裁判调用的单一通道）+
  Claude Code 裁判 usage_sink；供 GET /admin/usage/summary 做月度运行数/
  团队 token 聚合与 license 规模对账（超额只告警不阻断）；
- expand-only：新列可空，不动存量数据；单 head（b3e8a1d5f7c2 之上）。
  见 plans/2026-10-22-Agent质量工程平台开发方案.md §M6 QE6-3。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd8f2b6c4a9e1'
down_revision = 'b3e8a1d5f7c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("task_execution_records")}

    if "model_usage" not in columns:
        op.add_column(
            "task_execution_records",
            sa.Column("model_usage", sa.JSON(), nullable=True,
                      comment="模型 token 用量聚合（M6 QE6-3）：{model: {prompt_tokens, completion_tokens, total_tokens}}"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("task_execution_records")}
    if "model_usage" in columns:
        op.drop_column("task_execution_records", "model_usage")
