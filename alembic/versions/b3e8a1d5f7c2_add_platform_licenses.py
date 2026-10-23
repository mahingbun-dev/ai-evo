"""M6 QE6-1: License 与治理墙——platform_licenses 表

- 超管上传的 license 文件原文（JSON 信封 payload+sig）与 sha256 指纹；
  最新一行即当前生效 license，验签/过期判定由 license_service 在启动与
  上传时计算并缓存（库里不存结论，只存可复核证据）；
- expand-only：仅新增一张表，不动存量数据；单 head（c9e4b7d2f6a3 之上）。
  见 plans/2026-10-22-Agent质量工程平台开发方案.md §M6 QE6-1。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3e8a1d5f7c2'
down_revision = 'c9e4b7d2f6a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "platform_licenses" not in inspector.get_table_names():
        op.create_table(
            'platform_licenses',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('license_text', sa.Text(), nullable=False,
                      comment='license 文件原文（JSON 信封）'),
            sa.Column('fingerprint', sa.String(64), nullable=False,
                      comment='sha256(license_text) hex'),
            sa.Column('customer', sa.String(200), nullable=True,
                      comment='授权客户名（验签通过后冗余存，列表展示用）'),
            sa.Column('tier', sa.String(20), nullable=True,
                      comment='授权档位 oss/enterprise（冗余存）'),
            sa.Column('uploaded_by', sa.String(), nullable=True, comment='上传人 user_code'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                      comment='上传时间'),
            sa.PrimaryKeyConstraint('id'),
            comment='平台 license 上传历史（最新一行生效；M6 QE6-1）',
        )
        op.create_index('ix_platform_licenses_id', 'platform_licenses', ['id'])


def downgrade() -> None:
    op.drop_index('ix_platform_licenses_id', table_name='platform_licenses')
    op.drop_table('platform_licenses')
