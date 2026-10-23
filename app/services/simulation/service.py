"""仿真场景与运行服务（M7 QE7-4）

``SimulationService``：simulation_scenarios / simulation_runs 两表的读写
与运行生命周期，运行面复用评测任务既有通道——

- 配额：team 并发配额（``quota.TeamConcurrencyQuota``）提交前校验、超限
  拒绝；在途真值 = 该团队 running 的评测任务数 + running 的仿真 run 数。
  释放口径对齐评测链（单一释放点）：只有 ``submit_run`` 成功（拿到额度）
  的 run 才有释放义务，由 worker 侧 ``execute_run`` 收尾时释放**恰好一次**
  ——配额满路径（``acquire`` 内部已撤销占位）与 broker 异常路径
  （``submit_run`` except 已释放）**不再释放**；``request_stop`` 只发信号
  不释放（在途 worker 收尾释放；worker 已死时额度泄漏由对账「只裁不抬」
  + TTL 兜底回收——多释放会使账本低于 DB 真值且永不对账回来，宁可漏放）；
- 互斥：运行级互斥锁（``EvalTaskLock``，key 前缀 ``simrun_lock``）在
  celery 任务 ``run_simulation_run`` 内抢锁（acks_late 重投防护，口径同
  run_evaluation_task）；
- 停止：Redis 停止信号（``RedisStopSignalChecker(run_id)``）回合间生效，
  API ``request_stop`` 置位 + revoke celery 任务；
- 进度：每 trial 完成向 Redis ``task_status:{run_id}`` 频道发布
  ``simulation_progress``（best-effort），与评测任务/ hub 导入共用
  PubSub → WebSocket 通道。

发布门禁：create/update 即校验（``validate_scenario_config``——无终态
校验器的仿真不允许入库）。run 创建时冻结场景配置快照
（``scenario_snapshot``），场景后续修订不影响在途/历史 run 的复现口径。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.models.simulation_models import SimulationRun, SimulationScenario
from app.services.simulation.scenario import validate_scenario_config
from app.services.simulation.turn_controller import run_simulation_trials

logger = logging.getLogger(__name__)

# 运行状态机
RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_STOPPED = "stopped"

# 运行级互斥锁 key 前缀（celery 任务内抢锁；TTL 用 EvalTaskLock 缺省 3h
# ——覆盖 celery task_time_limit=2h 的同口径）
SIM_RUN_LOCK_PREFIX = "simrun_lock"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SimulationService:
    """仿真场景 CRUD 与运行生命周期（db 会话由调用方持有）"""

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # 场景 CRUD（create/update 即发布门禁）
    # ------------------------------------------------------------------
    def create_scenario(
        self,
        name: str,
        config: Dict[str, Any],
        *,
        description: str = "",
        created_by: Optional[str] = None,
        team_code: Optional[str] = None,
    ) -> SimulationScenario:
        name = str(name or "").strip()
        if not name:
            raise ValueError("场景名称不能为空")
        try:
            normalized = validate_scenario_config(config)
        except ValueError as e:
            raise ValueError(f"场景配置非法: {e}") from e
        scenario = SimulationScenario(
            id=_new_id("sim"),
            name=name,
            description=str(description or "").strip(),
            config=normalized,
            created_by=created_by,
            team_code=team_code or "DEFAULT",
        )
        self.db.add(scenario)
        self.db.commit()
        self.db.refresh(scenario)
        return scenario

    def update_scenario(
        self,
        scenario_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> SimulationScenario:
        scenario = self.get_scenario(scenario_id)
        if scenario is None:
            raise ValueError(f"仿真场景不存在: {scenario_id}")
        if name is not None:
            name = str(name).strip()
            if not name:
                raise ValueError("场景名称不能为空")
            scenario.name = name
        if description is not None:
            scenario.description = str(description).strip()
        if config is not None:
            try:
                scenario.config = validate_scenario_config(config)
            except ValueError as e:
                raise ValueError(f"场景配置非法: {e}") from e
            scenario.version = int(scenario.version or 1) + 1
        self.db.commit()
        self.db.refresh(scenario)
        return scenario

    def delete_scenario(self, scenario_id: str) -> bool:
        scenario = self.get_scenario(scenario_id)
        if scenario is None:
            return False
        running = (
            self.db.query(sa_func.count(SimulationRun.id))
            .filter(SimulationRun.scenario_id == scenario_id,
                    SimulationRun.status == RUN_STATUS_RUNNING)
            .scalar() or 0
        )
        if running:
            raise ValueError(
                f"场景有 {running} 个运行中的 run，停止后再删除"
                "（历史 run 随场景级联删除）")
        self.db.delete(scenario)
        self.db.commit()
        return True

    def get_scenario(self, scenario_id: str) -> Optional[SimulationScenario]:
        return (
            self.db.query(SimulationScenario)
            .filter(SimulationScenario.id == scenario_id)
            .first()
        )

    def list_scenarios(
        self, *, team_code: Optional[str] = None,
        skip: int = 0, limit: int = 50,
    ) -> List[SimulationScenario]:
        query = self.db.query(SimulationScenario)
        if team_code:
            query = query.filter(SimulationScenario.team_code == team_code)
        return (
            query.order_by(SimulationScenario.created_at.desc())
            .offset(skip).limit(min(limit, 200)).all()
        )

    # ------------------------------------------------------------------
    # 运行生命周期
    # ------------------------------------------------------------------
    def create_run(
        self, scenario_id: str, *, created_by: Optional[str] = None
    ) -> SimulationRun:
        scenario = self.get_scenario(scenario_id)
        if scenario is None:
            raise ValueError(f"仿真场景不存在: {scenario_id}")
        run = SimulationRun(
            id=_new_id("simrun"),
            scenario_id=scenario.id,
            scenario_snapshot=scenario.config,  # 冻结快照：场景修订不影响在途 run
            status=RUN_STATUS_PENDING,
            trials_total=int((scenario.config or {}).get("trials") or 0),
            created_by=created_by,
            team_code=scenario.team_code,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def get_run(self, run_id: str) -> Optional[SimulationRun]:
        return self.db.query(SimulationRun).filter(SimulationRun.id == run_id).first()

    def list_runs(
        self, *, scenario_id: Optional[str] = None,
        team_code: Optional[str] = None, limit: int = 50,
    ) -> List[SimulationRun]:
        query = self.db.query(SimulationRun)
        if scenario_id:
            query = query.filter(SimulationRun.scenario_id == scenario_id)
        if team_code:
            query = query.filter(SimulationRun.team_code == team_code)
        return (
            query.order_by(SimulationRun.created_at.desc())
            .limit(min(limit, 200)).all()
        )

    def start_run(self, run_id: str) -> bool:
        """pending → running（CAS：rowcount=0 表示已被启动/非 pending）"""
        from app.models.simulation_models import SimulationRun as _Run

        rowcount = (
            self.db.query(_Run)
            .filter(_Run.id == run_id, _Run.status == RUN_STATUS_PENDING)
            .update({_Run.status: RUN_STATUS_RUNNING, _Run.started_at: _utcnow()},
                    synchronize_session=False)
        )
        self.db.commit()
        return bool(rowcount)

    def submit_run(self, run_id: str) -> bool:
        """提交运行到 celery（team 配额校验 + 入队；口径同 TaskScheduler.submit_task）

        Returns:
            True 提交成功；False 配额超限/入队失败（调用方把 run 标 failed）
        """
        from app.celery_app import PRIORITY_HIGH
        from app.models.task_models import EvaluationTask
        from app.services.quota import TeamConcurrencyQuota
        from app.tasks.celery_tasks import run_simulation_run

        run = self.get_run(run_id)
        if run is None:
            logger.warning("仿真 run %s 不存在，提交跳过", run_id)
            return False
        quota = TeamConcurrencyQuota()
        # 在途真值 = running 评测任务 + running 仿真 run（本 run 已先行置 running）
        in_flight = (
            self.db.query(sa_func.count(EvaluationTask.id))
            .filter(EvaluationTask.team_code == run.team_code,
                    EvaluationTask.status == "running")
            .scalar() or 0
        ) + (
            self.db.query(sa_func.count(SimulationRun.id))
            .filter(SimulationRun.team_code == run.team_code,
                    SimulationRun.status == RUN_STATUS_RUNNING)
            .scalar() or 0
        )
        if not quota.acquire(team_code=run.team_code, db_in_flight=in_flight):
            logger.warning(
                "仿真 run %s 提交被拒：team %s 并发配额已满（DB 在途 %d）",
                run_id, run.team_code, in_flight)
            return False
        try:
            result = run_simulation_run.apply_async(
                args=[run_id], priority=PRIORITY_HIGH)
        except Exception:
            try:
                quota.release(run.team_code)
            except Exception as release_err:  # noqa: BLE001
                logger.warning("run %s 提交失败后释放配额异常: %s", run_id, release_err)
            raise
        run.celery_task_id = result.id
        self.db.commit()
        logger.info("仿真 run %s 已提交到 Celery，celery_task_id=%s", run_id, result.id)
        return True

    def fail_run(self, run_id: str, error: str) -> None:
        """标记失败（只改状态，不释放配额——口径对齐评测链 fail_task）

        配额释放的单一释放点在 worker 侧 ``execute_run`` 收尾（拿到额度
        的 run 恰好释放一次）。本方法的调用场景：配额满拒绝（额度从未
        占有——acquire 内部已撤销占位）与 broker 入队异常（submit_run
        的 except 已释放），两条路径都没有释放义务。
        """
        run = self.get_run(run_id)
        if run is None:
            return
        run.status = RUN_STATUS_FAILED
        run.error = str(error)[:4000]
        run.finished_at = _utcnow()
        self.db.commit()

    def execute_run(self, run_id: str, stop_signal: Any = None) -> Dict[str, Any]:
        """worker 侧执行入口（celery 任务调用；锁与停止信号由任务侧管理）

        逐 trial 经 ``run_simulation_trials`` 执行并发布进度；停止信号置位
        时状态落 stopped（已完成 trial 的结果保留在 result）。
        """
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"仿真 run 不存在: {run_id}")
        scenario = dict(run.scenario_snapshot or {})

        def on_trial_done(done: int, trial: Dict[str, Any]) -> None:
            run.trials_done = done
            self.db.commit()
            self._publish_progress(run, trial=trial)

        try:
            outcome = run_simulation_trials(
                scenario, _judge_task_stub(), run_id=run_id,
                stop_signal=stop_signal, on_trial_done=on_trial_done,
            )
        except Exception as e:  # noqa: BLE001 - 任何执行异常 → failed（单次配额释放）
            logger.exception("仿真 run %s 执行失败", run_id)
            self.fail_run(run_id, str(e))
            self._release_quota(run)  # 拿到额度的 run 收尾释放（单一释放点）
            raise

        stopped = stop_signal is not None and stop_signal.is_set()
        run.status = RUN_STATUS_STOPPED if stopped else RUN_STATUS_COMPLETED
        run.result = outcome
        run.finished_at = _utcnow()
        self.db.commit()
        self._publish_progress(run)
        self._release_quota(run)
        return outcome

    def request_stop(self, run_id: str) -> bool:
        """停止运行：置 Redis 停止信号 + revoke celery + 状态落 stopped

        不在此释放配额（单一释放点在 worker ``execute_run`` 收尾——在途
        worker 收到停止信号后正常收尾并释放；worker 已死时额度泄漏由
        配额对账「只裁不抬」+ TTL 兜底回收，与评测链 stop 语义一致，
        避免 API 侧 + worker 侧竞态双释放使账本低于 DB 真值）。
        """
        from app.celery_app import celery_app
        from app.services.redis_stop_signal import RedisStopSignalChecker

        run = self.get_run(run_id)
        if run is None:
            return False
        RedisStopSignalChecker(run_id).set()
        if run.celery_task_id:
            try:
                celery_app.control.revoke(run.celery_task_id, terminate=True, timeout=3)
            except Exception as e:  # noqa: BLE001 - revoke 失败靠停止信号兜底
                logger.warning("仿真 run %s revoke 失败（停止信号兜底）: %s", run_id, e)
        if run.status == RUN_STATUS_RUNNING:
            run.status = RUN_STATUS_STOPPED
            run.finished_at = _utcnow()
            self.db.commit()
        return True

    # ------------------------------------------------------------------
    # 内部：配额释放与进度发布
    # ------------------------------------------------------------------
    def _release_quota(self, run: SimulationRun) -> None:
        from app.services.quota import TeamConcurrencyQuota

        try:
            TeamConcurrencyQuota().release(run.team_code)
        except Exception as e:  # noqa: BLE001 - 释放失败靠对账/TTL 兜底
            logger.warning("仿真 run %s 释放 team 配额失败: %s", run.id, e)

    def _publish_progress(self, run: SimulationRun, trial: Optional[Dict[str, Any]] = None) -> None:
        """发布运行进度到 Redis ``task_status:{run_id}``（best-effort 不阻断）"""
        try:
            import redis

            from app.core.config import get_settings

            total = int(run.trials_total or 0)
            done = int(run.trials_done or 0)
            message = {
                "type": "simulation_progress",
                "run_id": run.id,
                "scenario_id": run.scenario_id,
                "status": run.status,
                "trials_done": done,
                "trials_total": total,
                "progress": int(done * 100 / total) if total > 0 else 100,
                "timestamp": _utcnow().isoformat(),
            }
            if trial is not None:
                message["last_trial"] = {
                    "trial": trial.get("trial"),
                    "passed": trial.get("passed"),
                    "termination": trial.get("termination"),
                }
            client = redis.from_url(get_settings().REDIS_URL)
            client.publish(f"task_status:{run.id}", json.dumps(message))
        except Exception as e:  # noqa: BLE001 - 进度发布失败只记日志
            logger.warning("仿真 run %s 进度发布失败: %s", run.id, e)


def _judge_task_stub() -> Any:
    """UserSimulator→JudgeClient 的任务替身：全空配置回落全局裁判配置

    JudgeClient（app/services/judge_client.py）在 task.judge_model_config
    为空时回落 ClaudeCodeConfigService 全局裁判配置——仿真模拟器不单独
    配裁判模型，与平台裁判面共用一套。
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        judge_model=None, judge_prompt="", max_score=10,
        evaluation_metrics=[], judge_model_config={},
    )
