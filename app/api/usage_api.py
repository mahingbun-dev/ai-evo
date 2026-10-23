"""用量计量 API（M6 QE6-3）：/api/v1/admin/usage/summary

- GET /admin/usage/summary：超管续约对账视图——月度评测运行数与团队 token
  用量聚合（模型 token 采集漏斗见 app/services/model_caller.py：被测 +
  裁判 API 调用统一收口、CC 裁判 usage_sink 喂入），附 license 规模
  （max_users/max_monthly_runs）与超额标记。可选 ?month=YYYY-MM 查历史月
  （北京时间自然月口径，与周报/统计一致）。

安全口径：超管专用（ensure_super_admin，deny by default）；不进 API Key
scope 注册表 → API Key 通道 default-deny（tests/security/test_usage_admin_api.py
专项）。超额不在此阻断（告警在任务链路 usage_service.check_run_overage）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import usage_service
from app.services.access_control import ensure_super_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/usage", tags=["用量计量"],
                   dependencies=[Depends(ensure_super_admin)])


@router.get("/summary", summary="月度用量汇总（超管，续约对账）")
def usage_summary(
    request: Request,
    month: str = Query("", description="查询月份 YYYY-MM（缺省=当前月，北京时间）"),
    db: Session = Depends(get_db),
):
    """月度评测运行数 + 团队 token 用量聚合 + license 对账（超管只读）。

    runs 口径：自然月内执行记录行数（completed/failed/stopped 都算一次运行）；
    token 口径：执行记录 model_usage 按团队（task.team_code，空按 DEFAULT）
    聚合，模型维度含被测与裁判调用。license.runs_over_quota=True 表示当月
    运行数已超上限——对账告警位，不阻断（告警走任务链路超额检查）。
    """
    try:
        return usage_service.monthly_usage_summary(db, month=month or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
