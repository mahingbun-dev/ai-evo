"""仿真评测 API（M7 QE7-4，plans §M7 7.2）

- ``/simulation/scenarios`` CRUD：创建/更新即发布门禁（无终态校验器的
  配置 400 拒绝，end-state verification 纪律）；
- ``POST /simulation/scenarios/{id}/run``：创建 run（冻结场景快照）→ 置
  running → team 并发配额校验 + celery 入队（复用评测任务配额/互斥/WS
  进度通道；配额满 429 语义用 400 返回明确错误信息）；
- ``GET /simulation/runs/{run_id}``：运行详情（trial 明细 + 指标）；
- ``POST /simulation/runs/{run_id}/stop``：停止（Redis 停止信号 + revoke）。

API Key 面（default-deny 注册表）：读走 ``tasks:read``、运行/停止走
``tasks:run``（与评测任务同 scope 族）；场景写操作页面态专用（不入
注册表，API Key 不可达）。

团队隔离与 RBAC（M4 口径，复审补收口）：列表按 ``filter_team_code``
过滤（超管看全部）；对象级读（场景详情/运行历史/运行详情）挂
``ensure_simulation_scenario_access`` / ``ensure_simulation_run_access``
（跨团队 403、不存在 404 不泄漏归属）；写操作（创建/更新/删除/发起
run/停止）另挂 ``ensure_permission(user, "tasks")``（viewer 只读）——
仿真属 tasks 资源族，RBAC 矩阵复用三角色权限。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import scopes
from app.core.scopes import require_scope
from app.db.session import get_db
from app.services.access_control import (
    ensure_permission,
    ensure_simulation_run_access,
    ensure_simulation_scenario_access,
    filter_team_code,
)
from app.services.audit_service import audited
from app.services.simulation.service import SimulationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/simulation", tags=["仿真评测"])

# API Key 可达路由注册（default-deny 注册表；scope 复用评测任务族）
scopes.register_api_key_route(
    "GET", "/api/v1/simulation/scenarios", scopes.TASKS_READ)
scopes.register_api_key_route(
    "GET", "/api/v1/simulation/scenarios/{scenario_id}", scopes.TASKS_READ)
scopes.register_api_key_route(
    "POST", "/api/v1/simulation/scenarios/{scenario_id}/run", scopes.TASKS_RUN)
scopes.register_api_key_route(
    "GET", "/api/v1/simulation/scenarios/{scenario_id}/runs", scopes.TASKS_READ)
scopes.register_api_key_route(
    "GET", "/api/v1/simulation/runs/{run_id}", scopes.TASKS_READ)
scopes.register_api_key_route(
    "POST", "/api/v1/simulation/runs/{run_id}/stop", scopes.TASKS_RUN)


# ---------------- 请求/响应模型 ----------------

class ScenarioCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="场景名称")
    description: Optional[str] = Field("", max_length=1000, description="场景说明")
    config: Dict[str, Any] = Field(..., description="场景配置（agent/personas/env/verifiers/faults/trials）")


class ScenarioUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    config: Optional[Dict[str, Any]] = None


class RunCreateResponse(BaseModel):
    run_id: str
    scenario_id: str
    status: str
    trials_total: int


def _user_code(request: Request) -> Optional[str]:
    user = request.state.user
    if isinstance(user, dict):
        return user.get("user_code")
    return getattr(user, "user_code", None)


def _team_code(request: Request) -> Optional[str]:
    user = request.state.user
    if isinstance(user, dict):
        return user.get("org_code")
    return getattr(user, "org_code", None)


def _serialize_scenario(scenario) -> Dict[str, Any]:
    return {
        "id": scenario.id,
        "name": scenario.name,
        "description": scenario.description or "",
        "config": scenario.config,
        "version": int(scenario.version or 1),
        "created_by": scenario.created_by,
        "team_code": scenario.team_code,
        "created_at": scenario.created_at.isoformat() if scenario.created_at else None,
        "updated_at": scenario.updated_at.isoformat() if scenario.updated_at else None,
    }


def _serialize_run(run, *, with_result: bool = True) -> Dict[str, Any]:
    payload = {
        "id": run.id,
        "scenario_id": run.scenario_id,
        "status": run.status,
        "trials_total": int(run.trials_total or 0),
        "trials_done": int(run.trials_done or 0),
        "error": run.error,
        "created_by": run.created_by,
        "team_code": run.team_code,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    if with_result:
        payload["result"] = run.result
    return payload


# ---------------- 场景 CRUD ----------------

@router.get("/scenarios", summary="列出仿真场景",
            dependencies=[Depends(require_scope(scopes.TASKS_READ))])
def list_scenarios(request: Request, skip: int = 0, limit: int = 50,
                   db: Session = Depends(get_db)):
    service = SimulationService(db)
    return [_serialize_scenario(s) for s in service.list_scenarios(
        team_code=filter_team_code(request.state.user), skip=skip, limit=limit)]


@router.post("/scenarios", summary="创建仿真场景（发布门禁：至少一个终态校验器）",
             dependencies=[Depends(audited("CREATE", "simulation_scenario"))])
def create_scenario(body: ScenarioCreateRequest, request: Request,
                    db: Session = Depends(get_db)):
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 只读
    service = SimulationService(db)
    try:
        scenario = service.create_scenario(
            body.name, body.config, description=body.description or "",
            created_by=_user_code(request), team_code=_team_code(request) or "DEFAULT",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_scenario(scenario)


@router.get("/scenarios/{scenario_id}", summary="获取仿真场景",
            dependencies=[Depends(require_scope(scopes.TASKS_READ))])
def get_scenario(scenario_id: str, request: Request, db: Session = Depends(get_db)):
    scenario = ensure_simulation_scenario_access(db, request.state.user, scenario_id)
    return _serialize_scenario(scenario)


@router.put("/scenarios/{scenario_id}", summary="更新仿真场景",
            dependencies=[Depends(audited("UPDATE", "simulation_scenario"))])
def update_scenario(scenario_id: str, body: ScenarioUpdateRequest,
                    request: Request, db: Session = Depends(get_db)):
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 只读
    ensure_simulation_scenario_access(db, request.state.user, scenario_id)
    service = SimulationService(db)
    try:
        scenario = service.update_scenario(
            scenario_id, name=body.name, description=body.description,
            config=body.config)
    except ValueError as e:
        status_code = 404 if "不存在" in str(e) else 400
        raise HTTPException(status_code=status_code, detail=str(e))
    return _serialize_scenario(scenario)


@router.delete("/scenarios/{scenario_id}", summary="删除仿真场景（运行中拒绝）",
               dependencies=[Depends(audited("DELETE", "simulation_scenario"))])
def delete_scenario(scenario_id: str, request: Request, db: Session = Depends(get_db)):
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 只读
    ensure_simulation_scenario_access(db, request.state.user, scenario_id)
    service = SimulationService(db)
    try:
        deleted = service.delete_scenario(scenario_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"仿真场景不存在: {scenario_id}")
    return {"deleted": True, "id": scenario_id}


# ---------------- 运行 ----------------

@router.post(
    "/scenarios/{scenario_id}/run", response_model=RunCreateResponse,
    summary="发起仿真运行（配额校验 + celery 入队）",
    dependencies=[Depends(require_scope(scopes.TASKS_RUN)),
                  Depends(audited("RUN", "simulation_scenario"))],
)
def run_scenario(scenario_id: str, request: Request, db: Session = Depends(get_db)):
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 不可触发运行
    ensure_simulation_scenario_access(db, request.state.user, scenario_id)
    service = SimulationService(db)
    try:
        run = service.create_run(scenario_id, created_by=_user_code(request))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # 先置 running（配额 DB 在途对账依赖该顺序，口径同 TaskScheduler.submit_task）
    if not service.start_run(run.id):
        raise HTTPException(status_code=409, detail=f"run {run.id} 已被启动或状态非 pending")
    try:
        submitted = service.submit_run(run.id)
    except Exception as e:  # noqa: BLE001 - broker 不可达等入队异常
        service.fail_run(run.id, f"提交失败: {e}")
        raise HTTPException(status_code=502, detail=f"仿真运行入队失败: {e}")
    if not submitted:
        service.fail_run(run.id, "team 并发配额已满（TEAM_MAX_CONCURRENT），请稍后重试")
        raise HTTPException(
            status_code=429,
            detail="team 并发配额已满（TEAM_MAX_CONCURRENT），请稍后重试")
    return RunCreateResponse(
        run_id=run.id, scenario_id=run.scenario_id,
        status=run.status, trials_total=run.trials_total)


@router.get("/scenarios/{scenario_id}/runs", summary="列出场景的运行历史",
            dependencies=[Depends(require_scope(scopes.TASKS_READ))])
def list_scenario_runs(scenario_id: str, request: Request, limit: int = 50,
                       db: Session = Depends(get_db)):
    ensure_simulation_scenario_access(db, request.state.user, scenario_id)
    service = SimulationService(db)
    return [_serialize_run(r, with_result=False)
            for r in service.list_runs(scenario_id=scenario_id, limit=limit)]


@router.get("/runs/{run_id}", summary="获取仿真运行详情（trial 明细 + 指标）",
            dependencies=[Depends(require_scope(scopes.TASKS_READ))])
def get_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    run = ensure_simulation_run_access(db, request.state.user, run_id)
    return _serialize_run(run)


@router.post("/runs/{run_id}/stop", summary="停止仿真运行",
             dependencies=[Depends(require_scope(scopes.TASKS_RUN)),
                           Depends(audited("STOP", "simulation_scenario"))])
def stop_run(run_id: str, request: Request, db: Session = Depends(get_db)):
    ensure_permission(request.state.user, "tasks")  # RBAC（QE4-3）：viewer 不可停止
    ensure_simulation_run_access(db, request.state.user, run_id)
    service = SimulationService(db)
    if not service.request_stop(run_id):
        raise HTTPException(status_code=404, detail=f"仿真运行不存在: {run_id}")
    return {"stopped": True, "run_id": run_id}
