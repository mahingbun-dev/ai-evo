"""M5 QE5-4: 报告外发——share_links 分享链接表

- 免登录只读报告的外发凭证：token_hash（sha256，明文仅创建时返回）、
  task_id + execution_number（空=跟随最新执行）、expires_at（空=永久）、
  row_scope（all/summary）、revoked_at；
- 对外安全口径：吊销/过期/不存在无差别 404 + 公开端点 Redis IP 限流
  （10 req/min/IP），见 app/api/share_api.py 与 tests/security/test_share_links.py。

expand-only：仅新增一张表，不动存量数据。单 head（b8d6f2a5c9e1 之上）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M5 QE5-4。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9e4b7d2f6a3'
down_revision = 'b8d6f2a5c9e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "share_links" not in inspector.get_table_names():
        op.create_table(
            'share_links',
            sa.Column('id', sa.String(), primary_key=True, comment='主键，格式：shl_{uuid12}'),
            sa.Column('token_hash', sa.String(64), nullable=False,
                      comment='sha256(明文 token) hex；明文仅创建时返回一次'),
            sa.Column('task_id', sa.String(),
                      sa.ForeignKey('evaluation_tasks.id', ondelete='CASCADE'),
                      nullable=False, comment='分享的评测任务ID'),
            sa.Column('execution_number', sa.Integer(), nullable=True,
                      comment='锁定执行次数（空=跟随最新执行）'),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True,
                      comment='过期时间（空=永久）'),
            sa.Column('row_scope', sa.String(10), nullable=False, server_default='all',
                      comment='可见范围：all-全部 / summary-仅汇总'),
            sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True,
                      comment='吊销时间（非空即失效）'),
            sa.Column('created_by', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('token_hash', name='uq_share_links_token_hash'),
            comment='报告分享链接表（M5 QE5-4）',
        )
        op.create_index('ix_share_links_id', 'share_links', ['id'])
        op.create_index('ix_share_links_task_id', 'share_links', ['task_id'])
        op.execute(
            "COMMENT ON COLUMN share_links.token_hash IS "
            "'sha256(明文 token) hex；明文仅创建时返回一次'"
        )


def downgrade() -> None:
    op.drop_index('ix_share_links_task_id', table_name='share_links')
    op.drop_index('ix_share_links_id', table_name='share_links')
    op.drop_table('share_links')
