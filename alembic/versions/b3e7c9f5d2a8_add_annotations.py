"""M8 QE8-1: 标注闭环——annotations 表

行级人工金分标注：锚定 evaluation_results.id（一行结果最多一条标注，
幂等覆盖），task_id/execution_number/data_index 冗余投影供一致性报告
（Pearson/Spearman + 分带 Cohen's kappa）按任务/执行聚合。

expand-only 新表，不动存量数据；单 head（a9d4c7e1f3b6 之上）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M8。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3e7c9f5d2a8'
down_revision = 'a9d4c7e1f3b6'
branch_labels = None
depends_on = None


def _tables_exist(inspector) -> set:
    return set(inspector.get_table_names())


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = _tables_exist(inspector)

    if "annotations" not in existing:
        bigint = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
        op.create_table(
            "annotations",
            sa.Column("id", bigint, primary_key=True, autoincrement=True),
            sa.Column("result_row_id", bigint,
                      sa.ForeignKey("evaluation_results.id", ondelete="CASCADE"),
                      nullable=False,
                      comment="锚定的行级结果 evaluation_results.id"),
            sa.Column("task_id", sa.String(), nullable=False,
                      comment="任务ID（冗余投影，供一致性报告聚合）"),
            sa.Column("execution_number", sa.Integer(), nullable=False,
                      comment="执行序号（与 evaluation_results 同口径）"),
            sa.Column("data_index", sa.Integer(), nullable=False,
                      comment="数据行原始序号（展示用）"),
            sa.Column("gold_score", sa.Float(), nullable=True,
                      comment="人工金分（status=completed 必填；skipped 为空）"),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="completed",
                      comment="标注状态：completed-已打分 / skipped-跳过（无法判定）"),
            sa.Column("annotator", sa.String(), nullable=True,
                      comment="标注人 user_code"),
            sa.Column("notes", sa.Text(), nullable=True,
                      comment="标注备注（争议点/判定依据）"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), comment="创建时间"),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), comment="更新时间"),
            sa.UniqueConstraint("result_row_id", name="uq_annotations_result_row"),
        )
        op.create_index("ix_annotations_task_id", "annotations", ["task_id"])
        op.create_index("ix_annotations_result_row_id", "annotations", ["result_row_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = _tables_exist(inspector)
    if "annotations" in existing:
        op.drop_table("annotations")
