"""仿真评测数据模型（M7 QE7-4，plans §M7 7.2）

- simulation_scenarios：仿真场景（被测 agent 定义 + 用户模拟器配置 + 环境
  与终态校验 + 故障注入 + 试验参数，全部在 config JSONB）。config 经
  simulation.scenario.validate_scenario_config 校验归一化后落库——创建/
  更新即发布门禁（无终态校验器的仿真不允许入库，end-state verification
  纪律）；
- simulation_runs：一次场景运行（trial 明细全量 JSONB——完整对话、工具
  轨迹、逐 trial 校验结果、pass^k/平均回合数/工具浪费率/故障恢复率指标）。
  生命周期 pending → running → completed/failed/stopped；运行互斥/停止
  信号/WS 进度/team 并发配额复用评测任务既有通道（ celery_tasks.
  run_simulation_run / redis_stop_signal / task_status:{id} 频道 /
  quota.TeamConcurrencyQuota）。

evaluation_type 白名单新增 ``simulation``（第 9 种类型）：该类型的运行
单元是场景而非评测任务（evaluation_tasks 不落 simulation 行，直建任务被
evaluation_task_service 显式拒绝并指向仿真入口）。
"""
from sqlalchemy import (
    Column, String, DateTime, Integer, Text, ForeignKey, JSON,
)
from sqlalchemy.sql import func

from app.db.session import Base


class SimulationScenario(Base):
    """仿真场景（config 经 validate_scenario_config 归一化；M7 QE7-4）"""

    __tablename__ = "simulation_scenarios"

    id = Column(String, primary_key=True, index=True)
    name = Column(String(200), nullable=False, comment="场景名称")
    description = Column(String(1000), nullable=True, comment="场景说明")
    # 归一化配置：agent/personas/combo_mode/max_turns/trial_soft_timeout_seconds/
    # env/verifiers/faults/trials（schema 见 simulation.scenario 模块头）
    config = Column(JSON, nullable=False, comment="场景配置（校验归一化后）")
    # verifiers 快照签发计数（verifiers 引用的脚本可能被删改，快照口径留待
    # 运行时展开；此列预留审计排障，不用作执行依据
    version = Column(Integer, nullable=False, default=1, server_default="1",
                     comment="配置修订版本（每次更新 +1）")
    created_by = Column(String, nullable=True, comment="创建人 user_code")
    team_code = Column(String, nullable=True, index=True, comment="所属团队 org_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), comment="更新时间")


class SimulationRun(Base):
    """仿真场景运行（trial 明细与指标全量落 result JSONB；M7 QE7-4）"""

    __tablename__ = "simulation_runs"

    id = Column(String, primary_key=True, index=True)
    scenario_id = Column(
        String, ForeignKey("simulation_scenarios.id", ondelete="CASCADE"),
        nullable=False, index=True, comment="场景ID")
    # 运行期配置快照：场景 config 在 run 创建时冻结（场景后续修订不影响在途 run）
    scenario_snapshot = Column(JSON, nullable=False,
                               comment="运行期场景配置快照（run 创建时冻结）")
    status = Column(String(20), nullable=False, default="pending",
                    server_default="pending", index=True,
                    comment="pending/running/completed/failed/stopped")
    trials_total = Column(Integer, nullable=False, default=0, server_default="0",
                          comment="计划 trial 数")
    trials_done = Column(Integer, nullable=False, default=0, server_default="0",
                         comment="已完成 trial 数（进度推送口径）")
    # {trials: [逐 trial 明细], pass_k, task_success_rate, average_turns,
    #  tool_waste_rate, fault_recovery_rate}（turn_controller.run_simulation_trials 产物）
    result = Column(JSON, nullable=True, comment="运行结果（trial 明细 + 指标）")
    error = Column(Text, nullable=True, comment="失败原因（status=failed）")
    celery_task_id = Column(String, nullable=True, comment="celery 任务ID（停止 revoke 用）")
    created_by = Column(String, nullable=True, comment="发起人 user_code")
    team_code = Column(String, nullable=True, index=True, comment="所属团队（配额账本键）")
    started_at = Column(DateTime(timezone=True), nullable=True, comment="开始时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="结束时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), comment="更新时间")
