"""M5 QE5-1: 数据集版本化——dataset_versions 表 + 任务版本绑定列

- 新表 dataset_versions：dataset_id/version_no（同数据集内唯一，单调递增）/
  snapshot_id（复用既有 dataset_snapshot 机制）/changelog/row_count/created_by；
- dataset_snapshot.task_id 放开 NOT NULL：数据集打版的版本快照无任务归属
  （存量任务执行快照仍带 task_id，行为不变；FK 保留，NULL 不受约束影响）；
- evaluation_tasks.dataset_version_no：NULL=跟随最新（存量行为不变），
  整数=锁定版本运行。

expand-only：新表+可空列+放开约束，不动存量数据。单 head（d6f4a9c2e8b1 之上）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M5 QE5-1。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5a1c8f3b7d2'
down_revision = 'd6f4a9c2e8b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "dataset_versions" not in inspector.get_table_names():
        op.create_table(
            'dataset_versions',
            sa.Column('id', sa.String(), primary_key=True, comment='主键，格式：dsv_{uuid12}'),
            sa.Column('dataset_id', sa.String(),
                      sa.ForeignKey('evaluation_datasets.id', ondelete='CASCADE'),
                      nullable=False, comment='数据集ID'),
            sa.Column('version_no', sa.Integer(), nullable=False,
                      comment='版本号（同一数据集内单调递增，从1开始）'),
            sa.Column('snapshot_id', sa.String(), nullable=False,
                      comment='打版时的 dataset_snapshot 快照ID'),
            sa.Column('changelog', sa.String(), nullable=True, comment='版本变更说明'),
            sa.Column('row_count', sa.Integer(), nullable=True,
                      comment='版本行数（冗余登记，列表展示免解包快照）'),
            sa.Column('created_by', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('dataset_id', 'version_no', name='uq_dataset_versions_dataset_no'),
            comment='数据集版本表（M5 QE5-1）',
        )
        op.create_index('ix_dataset_versions_id', 'dataset_versions', ['id'])
        op.create_index('ix_dataset_versions_dataset_id', 'dataset_versions', ['dataset_id'])

    # 版本快照无任务归属：放开 dataset_snapshot.task_id 非空约束（幂等）
    op.execute("ALTER TABLE dataset_snapshot ALTER COLUMN task_id DROP NOT NULL")
    op.execute(
        "COMMENT ON COLUMN dataset_snapshot.task_id IS "
        "'任务ID（M5 QE5-1 起可空：数据集打版的版本快照无任务归属）'"
    )

    # 任务版本绑定列（可空，NULL=跟随最新）
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS dataset_version_no INTEGER NULL"
    )
    op.execute(
        "COMMENT ON COLUMN evaluation_tasks.dataset_version_no IS "
        "'数据集版本绑定（M5 QE5-1）：NULL=跟随最新，整数=锁定 dataset_versions.version_no'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS dataset_version_no")
    # 版本快照行 task_id 为 NULL，回滚前需已清理；存量任务快照不受影响
    op.execute("DELETE FROM dataset_versions")
    op.execute("DELETE FROM dataset_snapshot WHERE task_id IS NULL")
    op.execute("ALTER TABLE dataset_snapshot ALTER COLUMN task_id SET NOT NULL")
    op.drop_index('ix_dataset_versions_dataset_id', table_name='dataset_versions')
    op.drop_index('ix_dataset_versions_id', table_name='dataset_versions')
    op.drop_table('dataset_versions')
