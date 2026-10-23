"""M7 QE7-4: 仿真评测——simulation_scenarios / simulation_runs 两表

- simulation_scenarios：场景 config JSONB（agent/personas/env/verifiers/
  faults/trials，创建/更新经 validate_scenario_config 校验归一化——无终态
  校验器的仿真不允许入库，end-state verification 纪律）；
- simulation_runs：trial 明细与指标全量 JSONB（完整对话/工具轨迹/逐 trial
  校验结果 + pass^k/平均回合数/工具浪费率/故障恢复率）；运行互斥/停止信号/
  WS 进度/配额复用评测任务既有通道；
- evaluation_type 白名单新增 simulation（第 9 种类型，代码侧白名单，
  无 schema 变更）；expand-only 新表，不动存量数据；单 head
  （d8f2b6c4a9e1 之上）。
见 plans/2026-10-22-Agent质量工程平台开发方案.md §M7 7.2。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9d4c7e1f3b6'
down_revision = 'd8f2b6c4a9e1'
branch_labels = None
depends_on = None


def _tables_exist(inspector) -> set:
    return set(inspector.get_table_names())


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = _tables_exist(inspector)

    if "simulation_scenarios" not in existing:
        op.create_table(
            "simulation_scenarios",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False,
                      comment="场景名称"),
            sa.Column("description", sa.String(1000), nullable=True,
                      comment="场景说明"),
            sa.Column("config", sa.JSON(), nullable=False,
                      comment="场景配置（校验归一化后）"),
            sa.Column("version", sa.Integer(), nullable=False,
                      server_default="1", comment="配置修订版本（每次更新 +1）"),
            sa.Column("created_by", sa.String(), nullable=True,
                      comment="创建人 user_code"),
            sa.Column("team_code", sa.String(), nullable=True,
                      comment="所属团队 org_code"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), comment="创建时间"),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), comment="更新时间"),
        )
        op.create_index("ix_simulation_scenarios_id", "simulation_scenarios", ["id"])
        op.create_index("ix_simulation_scenarios_team_code", "simulation_scenarios", ["team_code"])

    if "simulation_runs" not in existing:
        op.create_table(
            "simulation_runs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("scenario_id", sa.String(),
                      sa.ForeignKey("simulation_scenarios.id", ondelete="CASCADE"),
                      nullable=False, comment="场景ID"),
            sa.Column("scenario_snapshot", sa.JSON(), nullable=False,
                      comment="运行期场景配置快照（run 创建时冻结）"),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="pending",
                      comment="pending/running/completed/failed/stopped"),
            sa.Column("trials_total", sa.Integer(), nullable=False,
                      server_default="0", comment="计划 trial 数"),
            sa.Column("trials_done", sa.Integer(), nullable=False,
                      server_default="0", comment="已完成 trial 数（进度推送口径）"),
            sa.Column("result", sa.JSON(), nullable=True,
                      comment="运行结果（trial 明细 + 指标）"),
            sa.Column("error", sa.Text(), nullable=True,
                      comment="失败原因（status=failed）"),
            sa.Column("celery_task_id", sa.String(), nullable=True,
                      comment="celery 任务ID（停止 revoke 用）"),
            sa.Column("created_by", sa.String(), nullable=True,
                      comment="发起人 user_code"),
            sa.Column("team_code", sa.String(), nullable=True,
                      comment="所属团队（配额账本键）"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True,
                      comment="开始时间"),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True,
                      comment="结束时间"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), comment="创建时间"),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), comment="更新时间"),
        )
        op.create_index("ix_simulation_runs_id", "simulation_runs", ["id"])
        op.create_index("ix_simulation_runs_scenario_id", "simulation_runs", ["scenario_id"])
        op.create_index("ix_simulation_runs_status", "simulation_runs", ["status"])
        op.create_index("ix_simulation_runs_team_code", "simulation_runs", ["team_code"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = _tables_exist(inspector)
    if "simulation_runs" in existing:
        op.drop_table("simulation_runs")
    if "simulation_scenarios" in existing:
        op.drop_table("simulation_scenarios")
