"""M5 QE5-2: 模板/Prompt 版本化——template_versions 表 + A/B 模板版本对照扩展

- 新表 template_versions：template_id/version_no（同模板内唯一，单调递增）/
  content JSON（打版时模板完整内容）/changelog/created_by；
- ab_test_sessions 增模板对照模式列（mode/source_task_id/template_id/
  template_version_a/b/task_a_id/task_b_id），model_a_id/model_b_id 放开
  NOT NULL（template 模式会话无模型对；存量 model 行不受影响，mode 空按
  model 兜底）。

expand-only：新表+可空列+放开约束，不动存量数据。单 head（e5a1c8f3b7d2 之上）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M5 QE5-2。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6b2d9e4c8a3'
down_revision = 'e5a1c8f3b7d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "template_versions" not in inspector.get_table_names():
        op.create_table(
            'template_versions',
            sa.Column('id', sa.String(), primary_key=True, comment='主键，格式：tplv_{uuid12}'),
            sa.Column('template_id', sa.String(),
                      sa.ForeignKey('evaluation_templates.id', ondelete='CASCADE'),
                      nullable=False, comment='模板ID'),
            sa.Column('version_no', sa.Integer(), nullable=False,
                      comment='版本号（同一模板内单调递增，从1开始）'),
            sa.Column('content', sa.JSON(), nullable=False, comment='打版时模板完整内容快照'),
            sa.Column('changelog', sa.String(), nullable=True, comment='版本变更说明'),
            sa.Column('created_by', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('template_id', 'version_no', name='uq_template_versions_template_no'),
            comment='评测模板版本表（M5 QE5-2）',
        )
        op.create_index('ix_template_versions_id', 'template_versions', ['id'])
        op.create_index('ix_template_versions_template_id', 'template_versions', ['template_id'])

    # A/B 模板版本对照模式列（全部可空；mode 空=存量手工会话式）
    op.execute(
        "ALTER TABLE ab_test_sessions ADD COLUMN IF NOT EXISTS mode VARCHAR(16) "
        "DEFAULT 'model'"
    )
    op.execute(
        "COMMENT ON COLUMN ab_test_sessions.mode IS "
        "'会话模式：model-双模型对比（存量默认）/template-模板版本对照（M5 QE5-2）'"
    )
    for col, comment in [
        ("source_task_id", "模板对照：源评测任务ID"),
        ("template_id", "模板对照：评测模板ID"),
        ("template_version_a", "模板对照：A 侧版本号"),
        ("template_version_b", "模板对照：B 侧版本号"),
        ("task_a_id", "模板对照：A 侧变体评测任务ID"),
        ("task_b_id", "模板对照：B 侧变体评测任务ID"),
    ]:
        col_type = "INTEGER" if col.startswith("template_version") else "VARCHAR"
        op.execute(
            f"ALTER TABLE ab_test_sessions ADD COLUMN IF NOT EXISTS {col} {col_type} NULL"
        )
        op.execute(f"COMMENT ON COLUMN ab_test_sessions.{col} IS '{comment}'")

    # template 模式会话无模型对：放开非空约束（存量 model 行不受影响）
    op.execute("ALTER TABLE ab_test_sessions ALTER COLUMN model_a_id DROP NOT NULL")
    op.execute("ALTER TABLE ab_test_sessions ALTER COLUMN model_b_id DROP NOT NULL")


def downgrade() -> None:
    # 先清 template 模式行（其变体任务保留，由任务自身生命周期管理），再恢复约束
    op.execute("DELETE FROM ab_test_sessions WHERE mode = 'template'")
    op.execute(
        "UPDATE ab_test_sessions SET mode = 'model' WHERE mode IS NULL"
    )
    op.execute("ALTER TABLE ab_test_sessions ALTER COLUMN model_a_id SET NOT NULL")
    op.execute("ALTER TABLE ab_test_sessions ALTER COLUMN model_b_id SET NOT NULL")
    for col in ["mode", "source_task_id", "template_id", "template_version_a",
                "template_version_b", "task_a_id", "task_b_id"]:
        op.execute(f"ALTER TABLE ab_test_sessions DROP COLUMN IF EXISTS {col}")
    op.drop_index('ix_template_versions_template_id', table_name='template_versions')
    op.drop_index('ix_template_versions_id', table_name='template_versions')
    op.drop_table('template_versions')
