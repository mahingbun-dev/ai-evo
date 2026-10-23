"""用量计量服务（M6 QE6-3）：月度评测运行数与团队 token 用量聚合

口径（方案 §M6 QE6-3）：
- **评测运行数（runs）**：自然月内 task_execution_records 行数（每次执行一行，
  completed/failed/stopped 都算一次运行——失败运行也占用量）；
- **团队 token 用量**：task_execution_records.model_usage 按团队聚合
  （经 task.team_code 归属；NULL team_code 按 DEFAULT 口径，与团队隔离口径
  一致）。model_usage 的采集漏斗：被测与裁判 API 调用统一过 model_caller
  漏斗、Claude Code 裁判经 usage_sink 喂入（见 model_caller 模块注释）；
- **license 对账**：summary 附 tier/max_users/max_monthly_runs 与超额标记，
  供续约对账——**不设硬阻断**（方案明确口径），超额只告警：
  任务开始时 check_run_overage 对照当月 runs 与 max_monthly_runs，超了
  alert_service 告警（webhook + logs/alerts.log）+ 可选 SMTP 邮件
  （USAGE_ALERT_EMAIL_TO 配置收件人；未配置只走 webhook/log），Redis 日
  节流（每天至多一封），任何告警失败都不抛错不阻断任务。

时区：月度窗口按北京时间（与周报/统计口径一致，stats_service.BEIJING_TZ）。
"""
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services import license_service

logger = logging.getLogger(__name__)

# 超额告警 Redis 日节流 key（当天已告警则跳过邮件/告警重复发送）
_OVERAGE_ALERT_KEY = "aievo:usage_overage_alert:{day}"


def _month_window(month: Optional[str], now: Optional[datetime] = None) -> tuple[date, date, str]:
    """月度窗口（北京时间自然月）。month 形如 2026-10；缺省=当前月。

    返回 (start_date, end_date_exclusive, period_label)；非法 month 抛 ValueError
    由 API 层转 400。
    """
    from app.services.stats_service import beijing_now

    if month:
        # 严格 YYYY-MM（两位月份）：宽解析会让 2026-1 / 20261 混进来，对账口径必须唯一
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError(f"month 非法：{month!r}（格式 YYYY-MM，如 2026-10）")
        year, mon = month.split("-")
        start = date(int(year), int(mon), 1)
    else:
        now = now or beijing_now()
        start = date(now.year, now.month, 1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end, f"{start.year}-{start.month:02d}"


def _sum_model_usage(usage: Any) -> Dict[str, int]:
    """单条 model_usage JSON 求和（容忍旧形态 {model: int} 与缺维度）"""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not isinstance(usage, dict):
        return totals
    for value in usage.values():
        if isinstance(value, dict):
            totals["prompt_tokens"] += int(value.get("prompt_tokens") or 0)
            totals["completion_tokens"] += int(value.get("completion_tokens") or 0)
            totals["total_tokens"] += int(value.get("total_tokens") or 0)
        elif isinstance(value, (int, float)):
            # 旧形态（行级 model_usage 标量总量）按总量口径兼容
            totals["total_tokens"] += int(value)
    return totals


def monthly_usage_summary(db: Session, month: Optional[str] = None,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
    """月度用量汇总（GET /admin/usage/summary 数据源，超管续约对账用）。

    teams 之外附 totals 与 license 对账块（max_monthly_runs/max_users 与
    runs_over_quota 标记——超额是对账告警位，不是阻断位）。
    """
    from app.models.task_models import EvaluationTask, TaskExecutionRecord

    start, end, period = _month_window(month, now)

    # 拉原始行（团队 + 用量 JSON）在 Python 侧聚合——SQL count 与 JSON 列
    # 混选会被 SQLite 折成一行（无 GROUP BY 时裸列取任意行），用量即丢行；
    # 执行记录单表数据量=运行数（千级/月），全量拉行无压力
    rows = (
        db.query(
            sa_func.coalesce(EvaluationTask.team_code, "DEFAULT").label("team_code"),
            TaskExecutionRecord.model_usage,
        )
        .join(EvaluationTask, TaskExecutionRecord.task_id == EvaluationTask.id)
        .filter(TaskExecutionRecord.started_at >= start,
                TaskExecutionRecord.started_at < end)
        .all()
    )

    teams: Dict[str, Dict[str, Any]] = {}
    for team_code, usage in rows:
        slot = teams.setdefault(
            str(team_code),
            {"team_code": str(team_code), "runs": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        slot["runs"] += 1
        totals = _sum_model_usage(usage)
        for key in totals:
            slot[key] += totals[key]

    team_list: List[Dict[str, Any]] = sorted(
        teams.values(), key=lambda t: t["total_tokens"], reverse=True)
    totals = {
        "runs": sum(t["runs"] for t in team_list),
        "teams": len(team_list),
        "prompt_tokens": sum(t["prompt_tokens"] for t in team_list),
        "completion_tokens": sum(t["completion_tokens"] for t in team_list),
        "total_tokens": sum(t["total_tokens"] for t in team_list),
    }

    state = license_service.current_state()
    max_runs = state.max_monthly_runs
    return {
        "period": period,
        "totals": totals,
        "teams": team_list,
        "license": {
            "tier": state.effective_tier,
            "status": state.status,
            "customer": state.customer,
            "max_users": state.max_users,
            "max_monthly_runs": max_runs,
            # 对账告警位：True=当月运行数已超 license 上限（不阻断，只告警）
            "runs_over_quota": bool(max_runs is not None and totals["runs"] > max_runs),
        },
    }


def _month_runs_count(db: Session, now: datetime) -> int:
    """当月（北京时间）全平台运行数——超额对账的计数口径与 summary 一致"""
    from app.models.task_models import TaskExecutionRecord

    start, end, _ = _month_window(None, now)
    return int(
        db.query(sa_func.count(TaskExecutionRecord.id))
        .filter(TaskExecutionRecord.started_at >= start,
                TaskExecutionRecord.started_at < end)
        .scalar() or 0)


def check_run_overage(db: Session, now: Optional[datetime] = None) -> bool:
    """月度运行数超额检查（评测任务开始处调用；返回是否超额，供测试断言）。

    **不阻断**是硬约束：本函数任何路径都不抛异常（DB/Redis/SMTP 故障一律
    吞掉记 warning）。告警双通道：alert_service（webhook + logs/alerts.log）
    与可选 SMTP 邮件（USAGE_ALERT_EMAIL_TO 未配置=只走前者）；Redis 日节流
    保证同一天至多一轮告警（防每次任务开始都刷屏）。
    """
    from app.services.stats_service import beijing_now
    from app.services.alert_service import send_alert

    try:
        now = now or beijing_now()
        state = license_service.current_state()
        max_runs = state.max_monthly_runs
        if max_runs is None:
            return False  # 未设上限（oss/未声明）：无超额语义

        runs = _month_runs_count(db, now)
        if runs <= max_runs:
            return False

        day = now.strftime("%Y-%m-%d")
        month_label = f"{now.year}-{now.month:02d}"
        detail = {
            "month": month_label, "runs": runs, "max_monthly_runs": max_runs,
            "license_status": state.status, "customer": state.customer,
            "note": "已超额运行（不阻断，仅对账告警）——请与厂商续约或上调规模",
        }
        # 日节流：当天已告警则只留 debug 日志（Redis 不可用时不节流，宁可重复不漏报）
        throttled = False
        try:
            from app.services.redis_stop_signal import get_redis_client
            redis_client = get_redis_client()
            if redis_client is not None:
                throttled = not redis_client.set(
                    _OVERAGE_ALERT_KEY.format(day=day), "1", ex=86400, nx=True)
        except Exception as e:  # noqa: BLE001 — 节流失败不阻断告警
            logger.warning(f"[USAGE] 超额告警节流检查失败（照常告警）: {e}")

        if throttled:
            logger.debug("[USAGE] 月度运行数超额（今日已告警，节流跳过）: %s", detail)
            return True

        logger.warning("[USAGE] ⚠ 月度评测运行数超额：%s/%s（%s）——不阻断，核心评测继续",
                       runs, max_runs, month_label)
        try:
            send_alert("warning", "月度评测运行数超额（license 对账）", detail,
                       source="usage-metering")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[USAGE] 超额告警发送失败（不影响任务）: {e}")

        _send_overage_email(detail)
        return True
    except Exception as e:  # noqa: BLE001 — 超额检查绝不阻断评测
        logger.warning(f"[USAGE] 月度运行数超额检查异常（忽略，不阻断）: {e}")
        return False


def _send_overage_email(detail: Dict[str, Any]) -> str:
    """超额告警邮件（可选通道）。返回 sent/skipped/failed（与周报邮件同口径）。

    USAGE_ALERT_EMAIL_TO 未配置 → skipped（不告警不报错）；发送失败只记
    warning，绝不向上抛（告警是旁路能力）。
    """
    import smtplib
    from email.mime.text import MIMEText

    settings = get_settings()
    if not settings.SMTP_HOST or not settings.USAGE_ALERT_EMAIL_TO:
        return "skipped"
    try:
        body = (
            f"AgentEvo 月度评测运行数超额告警\n\n"
            f"月份：{detail['month']}\n当前运行数：{detail['runs']}\n"
            f"license 上限：{detail['max_monthly_runs']}\n"
            f"license 状态：{detail['license_status']}\n"
            f"授权客户：{detail['customer'] or '-'}\n\n"
            f"口径：超额不阻断（核心评测继续），本邮件为续约对账提醒；\n"
            f"用量明细见平台「GET /api/v1/admin/usage/summary」（超管）。"
        )
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[AgentEvo] 月度评测运行数超额（{detail['month']}：{detail['runs']}/{detail['max_monthly_runs']}）"
        msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
        msg["To"] = settings.USAGE_ALERT_EMAIL_TO
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as client:
            if settings.SMTP_USE_TLS:
                client.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            client.sendmail(msg["From"], [x.strip() for x in
                                          settings.USAGE_ALERT_EMAIL_TO.split(",") if x.strip()],
                            msg.as_string())
        return "sent"
    except Exception as e:  # noqa: BLE001 — 告警邮件失败不抛
        logger.warning(f"[USAGE] 超额告警邮件发送失败（不影响任务）: {e}")
        return "failed"
