"""add config snapshot columns (W4 模型/裁判配置快照)

W4（P0-C）：评测口径归因——创建任务时对模型与裁判配置各做深拷贝快照，
执行期「快照优先、实时回退」，执行记录留存每次执行的配置锚点。
- task_execution_records 追加 config_snapshot（执行开始写双快照、结束写
  实际使用配置摘要）；
- evaluation_tasks 追加 judge_model_config（裁判配置创建时快照；
  task.model_config 列本就存在，直接复用）。

两列均 ADD COLUMN IF NOT EXISTS 幂等：W3 分支的迁移（c4e8f9a2b7d6，
未合入本分支）同样会为 task_execution_records 建 config_snapshot，
两分支并行建列不冲突；MR 阶段需与 W3 统一迁移链（整改计划 §1.3 合并序：
W2 先合、W4 rebase）。

expand-only：upgrade 只加列；downgrade 仅回退本次新增对象。

Revision ID: f8e1d4c7a2b6
Revises: b7d2e8a4c651
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8e1d4c7a2b6'
down_revision: Union[str, None] = 'e4f7b1a9c2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 与 W3 的 c4e8f9a2b7d6 同用 IF NOT EXISTS，两分支任意先执行者建列、后者幂等跳过
    op.execute(
        "ALTER TABLE task_execution_records ADD COLUMN IF NOT EXISTS config_snapshot JSONB"
    )
    op.execute(
        "ALTER TABLE evaluation_tasks ADD COLUMN IF NOT EXISTS judge_model_config JSONB"
    )


def downgrade() -> None:
    # 仅回退本次新增列（expand-only 回滚语义：本迁移引入的对象才可被移除）
    op.execute("ALTER TABLE task_execution_records DROP COLUMN IF EXISTS config_snapshot")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN IF EXISTS judge_model_config")
