"""周报聚合与推送服务（M5 QE5-3）

每周一 08:00（北京时间）聚合**上一个自然周**的三类指标生成 markdown 周报：

- 覆盖度（CoverageService：周计划 vs 实际成功执行）；
- 提效（EfficiencyService：人工预计耗时 − 系统实际耗时）；
- 门禁通过率（task_execution_records.gate_result 聚合，M1 门禁闭环口径）；
- 附执行统计概览（StatsService）。

生成幂等：weekly_reports.week_start 唯一——本周已生成（或兄弟进程并发
插入撞唯一约束）即跳过；api 停机错过 08:00 时首个扫描拍补生成（报告不丢）。
推送两路：webhook（既有事件面新事件 weekly.report.ready，按订阅了该事件的
活跃端点所在团队逐一派发）与邮件（SMTP 可选配置，未配置记 skipped）。
markdown 渲染口径对齐 app/services/result_report.py 的分节风格。
"""
import logging
import smtplib
import uuid
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.schedule_models import WeeklyReport
from app.models.webhook_models import WebhookEndpoint
from app.services.webhook_service import EVENT_WEEKLY_REPORT, dispatch_event

logger = logging.getLogger(__name__)

# 周报生成时刻：每周一 08:00（北京时间，方案 §5.3）
WEEKLY_REPORT_HOUR = 8


def _last_monday(now: datetime) -> date:
    """now（北京时间）所属周的**上一个**周一：周报覆盖的是刚结束的自然周。

    调用前提：now ≥ 本周一 08:00（generate_if_due 的到点判定已保证）。
    """
    this_monday = now.date() - timedelta(days=now.weekday())
    return this_monday - timedelta(days=7)


def _gate_stats(db: Session, start: date, end: date) -> Dict[str, Any]:
    """聚合周内执行的门禁判定（gate_result.verdict 计数；M1 口径）。"""
    from app.models.task_models import TaskExecutionRecord

    rows = (
        db.query(TaskExecutionRecord)
        .filter(
            TaskExecutionRecord.gate_result.isnot(None),
            TaskExecutionRecord.started_at >= datetime.combine(start, datetime.min.time()),
            TaskExecutionRecord.started_at < datetime.combine(
                end + timedelta(days=1), datetime.min.time()),
        )
        .all()
    )
    counts = {"passed": 0, "failed": 0, "error": 0}
    for row in rows:
        verdict = ((row.gate_result or {}).get("verdict") or "").lower()
        if verdict in counts:
            counts[verdict] += 1
    total = sum(counts.values())
    return {
        **counts,
        "total": total,
        "pass_rate": round(counts["passed"] / total, 4) if total else None,
    }


def build_weekly_report(db: Session, week_start: date) -> Dict[str, Any]:
    """聚合 week_start 所在周（周一~周日）的指标并渲染 markdown。

    Returns:
        {"week_start", "week_end", "payload", "content"}——content 为
        markdown 正文，payload 为指标快照（周报落库与 webhook 报文共用）。
    """
    from app.services.coverage_service import CoverageService
    from app.services.efficiency_service import EfficiencyService
    from app.services.stats_service import StatsService

    week_end = week_start + timedelta(days=6)
    start_str, end_str = week_start.isoformat(), week_end.isoformat()

    summary = StatsService(db).get_summary(start_date=start_str, end_date=end_str)
    coverage_rows = CoverageService(db).get_coverage(start_date=start_str, end_date=end_str)
    efficiency = EfficiencyService(db).get_total_efficiency(
        start_date=start_str, end_date=end_str)
    gate = _gate_stats(db, week_start, week_end)

    payload: Dict[str, Any] = {
        "week_start": start_str,
        "week_end": end_str,
        "summary": summary,
        "coverage": coverage_rows,
        "efficiency": {
            "total_efficiency_hours": efficiency.get("total_efficiency_hours"),
            "task_count": efficiency.get("task_count"),
            "avg_efficiency_hours": efficiency.get("avg_efficiency_hours"),
        },
        "gate": gate,
    }

    lines: List[str] = []
    lines.append(f"# 评测周报（{start_str} ~ {end_str}）")
    lines.append("")
    lines.append("## 执行概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 执行总数 | {summary.get('total_count', 0)} |")
    lines.append(f"| 成功 / 失败 | {summary.get('success_count', 0)} / "
                 f"{summary.get('failed_count', 0)} |")
    rate = summary.get("success_rate") or 0
    lines.append(f"| 成功率 | {rate * 100:.1f}% |")
    lines.append(f"| 平均耗时 | {summary.get('avg_duration_ms', 0) / 1000:.1f}s |")
    lines.append("")
    lines.append("## 覆盖度")
    lines.append("")
    if coverage_rows:
        lines.append("| 周 | 计划 | 实际 | 覆盖率 |")
        lines.append("| --- | --- | --- | --- |")
        for row in coverage_rows:
            lines.append(
                f"| {row['week_start_date']} ~ {row['week_end_date']} "
                f"| {row['planned_count']} | {row['actual_count']} "
                f"| {row['coverage_rate']}% |"
            )
    else:
        lines.append("本周无覆盖度计划录入。")
    lines.append("")
    lines.append("## 提效")
    lines.append("")
    total_eff = efficiency.get("total_efficiency_hours") or 0
    lines.append(f"- 总提效：{total_eff}h（{efficiency.get('task_count', 0)} 个评测任务）")
    lines.append(f"- 平均单任务提效：{efficiency.get('avg_efficiency_hours', 0)}h")
    lines.append("")
    lines.append("## 门禁通过率")
    lines.append("")
    if gate["total"]:
        pass_rate = (gate["pass_rate"] or 0) * 100
        lines.append(f"- 周内门禁判定 {gate['total']} 次：通过 {gate['passed']}、"
                     f"失败 {gate['failed']}、异常 {gate['error']}")
        lines.append(f"- 通过率 {pass_rate:.1f}%")
    else:
        lines.append("- 本周无门禁判定记录（未启用门禁的任务不计入）。")
    lines.append("")
    return {**payload, "content": "\n".join(lines)}


def _push_webhooks(db: Session, payload: Dict[str, Any]) -> int:
    """按订阅了周报事件的活跃端点所在团队逐一派发（尊重 webhook 团队隔离）。

    订阅过滤在 Python 侧做（与 dispatch_event 同口径）：events 是 JSON 列，
    通用 LIKE contains 在 PG 上编译为 ``'%' || events_param::json`` 拼接，
    无 || 运算符直接报错——订阅过滤不能下推 SQL。
    """
    rows = (
        db.query(WebhookEndpoint.team_code, WebhookEndpoint.events)
        .filter(WebhookEndpoint.active.is_(True))
        .distinct()
        .all()
    )
    teams = sorted({team for team, events in rows if EVENT_WEEKLY_REPORT in (events or [])})
    created = 0
    for team_code in teams:
        created += dispatch_event(
            EVENT_WEEKLY_REPORT, team_code,
            {"week_start": payload["week_start"], "week_end": payload["week_end"],
             "report": {k: payload[k] for k in ("summary", "coverage", "efficiency", "gate")}},
            db=db,
        )
    return created


def _send_email(content: str, week_start: date, week_end: date) -> str:
    """SMTP 发送周报（标准库实现，无新依赖）。返回 sent/skipped/failed。"""
    settings = get_settings()
    if not settings.SMTP_HOST or not settings.WEEKLY_REPORT_EMAIL_TO:
        return "skipped"
    recipients = [r.strip() for r in settings.WEEKLY_REPORT_EMAIL_TO.split(",") if r.strip()]
    if not recipients:
        return "skipped"
    subject = f"AI 评测平台周报 {week_start.isoformat()}~{week_end.isoformat()}"
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as client:
            if settings.SMTP_USE_TLS:
                client.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            client.sendmail(msg["From"], recipients, msg.as_string())
        return "sent"
    except Exception as e:  # noqa: BLE001 - 推送旁路：失败不阻断归档
        logger.warning(f"周报邮件发送失败（归档不受影响）: {e}")
        return "failed"


def generate_if_due(db: Session, now: Optional[datetime] = None,
                    *, push: bool = True) -> Optional[Dict[str, Any]]:
    """到点则生成上周周报并归档推送（lifespan 每个扫描拍调用）。

    幂等：本周已存在归档行（或并发插入撞唯一约束）返回 None；停机错过
    08:00 时由后续扫描拍补生成（报告不丢）。
    """
    from app.services.stats_service import BEIJING_TZ, beijing_now

    now = now or beijing_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING_TZ)
    # 上一个 08:00 已过的最近周一 = 报告应就绪的周期；未到点（周一 08:00 前）
    # 覆盖周还没结束，不生成
    ready_at = datetime.combine(now.date() - timedelta(days=now.weekday()),
                                datetime.min.time(), tzinfo=BEIJING_TZ
                                ).replace(hour=WEEKLY_REPORT_HOUR)
    if now < ready_at:
        return None
    week_start = _last_monday(now)
    week_end = week_start + timedelta(days=6)

    existing = (
        db.query(WeeklyReport)
        .filter(WeeklyReport.week_start == week_start)
        .first()
    )
    if existing:
        return None

    report = build_weekly_report(db, week_start)
    pushed = _push_webhooks(db, report) if push else 0
    email_status = _send_email(report["content"], week_start, week_end) if push else "skipped"

    row = WeeklyReport(
        id=f"wr_{uuid.uuid4().hex[:12]}",
        week_start=week_start,
        week_end=week_end,
        content=report["content"],
        payload={k: report[k] for k in ("summary", "coverage", "efficiency", "gate")},
        pushed_webhooks=pushed,
        email_status=email_status,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # 并发生成撞唯一约束：兄弟进程已归档，让位
        db.rollback()
        return None
    logger.info(f"周报已生成并归档：{week_start}~{week_end}，webhook 推送 {pushed} 端，"
                f"邮件 {email_status}")
    return {
        "id": row.id, "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(), "content": report["content"],
        "pushed_webhooks": pushed, "email_status": email_status,
    }


def list_reports(db: Session, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    """周报归档列表（Statistics 周报归档 Tab；新在前）。"""
    query = db.query(WeeklyReport)
    total = query.count()
    items = (
        query.order_by(WeeklyReport.week_start.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": r.id,
                "week_start": r.week_start.isoformat(),
                "week_end": r.week_end.isoformat(),
                "pushed_webhooks": r.pushed_webhooks,
                "email_status": r.email_status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in items
        ],
    }


def get_report_content(db: Session, report_id: str) -> Optional[Dict[str, Any]]:
    """取单份周报正文（前端查看弹窗渲染 markdown 原文）。"""
    row = db.query(WeeklyReport).filter(WeeklyReport.id == report_id).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "week_start": row.week_start.isoformat(),
        "week_end": row.week_end.isoformat(),
        "content": row.content,
        "payload": row.payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
