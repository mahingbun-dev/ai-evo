"""
Webhook 服务（M1 QE1-3，门禁闭环的通知面）

职责三块：
1. 签名（HMAC-SHA256 + 时间戳防重放）：``X-Aievo-Signature: sha256=<hex>``，
   签名串为 ``{timestamp}.{body}``（GitHub 同款文法）；接收端用
   :func:`verify_signature` 验签 + ±5 分钟时间窗（重放窗口）；
2. 端点 CRUD 与事件分发（dispatch_event）：事件发生时按团队找活跃端点，
   建 deliveries 留痕行（pending）并派发 maint 队列投递任务——旁路能力，
   任何失败不抛出（与 alert_service 同口径，不拖挂业务主流程）；
3. 投递执行（execute_delivery）：每次发送前重验 URL（webhook_url_guard，
   防注册后 DNS 重绑定）；2xx 送达；失败指数退避重试 3 次（共 4 次尝试）。

事件（方案 §M1 指定三个）：
- task.execution.completed / task.execution.failed / gate.failed
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.webhook_models import WebhookDelivery, WebhookEndpoint
from app.services.webhook_url_guard import validate_webhook_url

logger = logging.getLogger(__name__)

# ---------------- 事件常量（方案 §M1 指定） ----------------

EVENT_TASK_COMPLETED = "task.execution.completed"
EVENT_TASK_FAILED = "task.execution.failed"
EVENT_GATE_FAILED = "gate.failed"

WEBHOOK_EVENTS: Tuple[str, ...] = (EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, EVENT_GATE_FAILED)

# ---------------- 投递参数 ----------------

HTTP_TIMEOUT_SECONDS = 10
# 重试 3 次（总尝试 4 次），指数退避 30s/60s/120s（celery countdown 承载）
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 30
# 防重放窗口：接收端校验 |now - timestamp| ≤ 该值（方案 ±5min）
REPLAY_TOLERANCE_SECONDS = 300

# 投递结果（celery 任务据此决定 self.retry 或终止）
OUTCOME_SUCCESS = "success"
OUTCOME_RETRY = "retry"
OUTCOME_FAILED = "failed"


# ---------------- 签名 / 验签 ----------------


def generate_secret() -> str:
    """生成端点签名 secret（whsec_ 前缀 + 32 字节 hex；明文仅创建时返回）"""
    return "whsec_" + secrets.token_hex(32)


def _sign(secret: str, body: str, timestamp: str) -> str:
    """HMAC-SHA256(f"{timestamp}.{body}")，返回 sha256=<hex> 文法"""
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode("utf-8"),
        hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def build_delivery_headers(event: str, secret: str, body: str,
                           timestamp: Optional[str] = None) -> Dict[str, str]:
    """构造投递请求头（签名 + 事件名 + 时间戳；接收端验签契约三件套）"""
    timestamp = timestamp or str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "X-Aievo-Event": event,
        "X-Aievo-Timestamp": timestamp,
        "X-Aievo-Signature": _sign(secret, body, timestamp),
    }


def verify_signature(secret: str, body: str, timestamp: str, signature: str,
                     *, now: Optional[float] = None,
                     tolerance: int = REPLAY_TOLERANCE_SECONDS) -> bool:
    """接收端验签：签名恒时比对 + 时间戳在容忍窗内（防重放）。

    任一不满足即 False（时间戳非数字/超窗/签名不符）；本函数同时是平台
    侧投递头的单测对照与文档示例。
    """
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance:
        return False
    return hmac.compare_digest(_sign(secret, body, timestamp), signature)


# ---------------- 端点 CRUD ----------------


def validate_events(events: Any) -> List[str]:
    """事件订阅白名单校验：非空、去重、全部 ∈ WEBHOOK_EVENTS"""
    if not isinstance(events, list) or not events:
        raise ValueError("events 不能为空：至少订阅一个事件")
    normalized = sorted({e for e in events if isinstance(e, str)})
    if not normalized:
        raise ValueError("events 含非法项：必须是非空字符串列表")
    unknown = [e for e in normalized if e not in WEBHOOK_EVENTS]
    if unknown:
        raise ValueError(f"未知事件 {unknown}，可选：{list(WEBHOOK_EVENTS)}")
    return normalized


def create_endpoint(db: Session, *, team_code: str, name: str, url: str,
                    events: List[str], created_by: Optional[str] = None,
                    allow_private: Optional[bool] = None) -> Tuple[WebhookEndpoint, str]:
    """创建端点：URL SSRF 校验（注册时第一道；投递时每次重验）→ 落库。

    Returns:
        (endpoint, secret 明文)——明文仅创建时返回一次（与 API Key 同口径）。
    """
    validate_webhook_url(url, allow_private=allow_private)
    normalized_events = validate_events(events)
    secret = generate_secret()
    endpoint = WebhookEndpoint(
        team_code=team_code,
        name=name,
        url=url.strip(),
        secret=secret,
        events=normalized_events,
        active=True,
        created_by=created_by,
    )
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    return endpoint, secret


def list_endpoints(db: Session, team_code: Optional[str]) -> List[WebhookEndpoint]:
    """端点列表：team_code 为 None（超管）看全部，否则按团队过滤"""
    query = db.query(WebhookEndpoint)
    if team_code is not None:
        query = query.filter(WebhookEndpoint.team_code == team_code)
    return query.order_by(WebhookEndpoint.id.desc()).all()


def delete_endpoint(db: Session, endpoint_id: int) -> Optional[WebhookEndpoint]:
    """删除端点（物理删；deliveries 经 ON DELETE CASCADE 级联清理）"""
    endpoint = db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        return None
    db.delete(endpoint)
    db.commit()
    return endpoint


def list_deliveries(db: Session, endpoint_id: int, limit: int = 50) -> List[WebhookDelivery]:
    """投递留痕（倒序，最新在前）"""
    return (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.id.desc())
        .limit(limit)
        .all()
    )


# ---------------- 事件分发（旁路：不抛出） ----------------


def dispatch_event(event: str, team_code: Optional[str], payload: Dict[str, Any],
                   db: Session = None) -> int:
    """向团队的全部活跃订阅端点派发事件：建留痕行 + 派发 maint 投递任务。

    旁路能力（与 alert_service 同口径）：查端点/落库/派发任一失败只记
    warning，绝不打断业务主流程（评测收尾路径调用）。返回创建的留痕行数。
    """
    created = 0
    own_session = db is None
    if own_session:
        from app.db.session import SessionLocal
        db = SessionLocal()
    try:
        endpoints = (
            db.query(WebhookEndpoint)
            .filter(
                WebhookEndpoint.team_code == (team_code or "DEFAULT"),
                WebhookEndpoint.active.is_(True),
            )
            .all()
        )
        targets = [ep for ep in endpoints if event in (ep.events or [])]
        if not targets:
            return 0
        deliveries = [
            WebhookDelivery(endpoint_id=ep.id, event=event, payload=payload, status="pending")
            for ep in targets
        ]
        db.add_all(deliveries)
        db.commit()  # commit 后各 delivery.id 已填充
        created = len(deliveries)

        from app.tasks.maintenance.webhook_delivery import deliver_webhook

        for delivery in deliveries:
            try:
                deliver_webhook.apply_async(args=[delivery.id])
            except Exception as exc:  # noqa: BLE001 - 单条派发失败不影响其余
                logger.warning("[WEBHOOK] 投递任务派发失败（delivery_id=%s）: %s",
                               delivery.id, exc)
        return created
    except Exception as exc:  # noqa: BLE001 - 旁路：绝不抛出
        logger.warning("[WEBHOOK] 事件派发失败（event=%s）: %s", event, exc)
        return created
    finally:
        if own_session:
            db.close()


# ---------------- 投递执行（celery 任务体委托到这里，便于单测） ----------------


def execute_delivery(
    delivery_id: int,
    *,
    http_post=None,
    resolver=None,
    allow_private: Optional[bool] = None,
    session_factory=None,
):
    """执行一次投递尝试（留痕状态机 + SSRF 重验 + 签名发送）。

    Args:
        http_post: 发送函数 ``(url, *, headers, body, timeout) -> status_code``，
            异常视为网络层失败；缺省用 httpx 真发（单测注入假体）。
        resolver / allow_private: SSRF 守卫注入（见 webhook_url_guard）。
        session_factory: DB 会话工厂（单测传 sqlite sessionmaker）。

    Returns:
        (outcome, countdown)：
        - ("success", None)：2xx 送达，留痕 status=success；
        - ("retry", N)：本次失败但重试未耗尽（总尝试 1+MAX_RETRIES 次），
          celery 侧以 N 秒指数退避重投；
        - ("failed", None)：重试耗尽 / SSRF 拒绝 / 端点已删停——终态留痕。
    """
    from app.db.session import SessionLocal

    if session_factory is None:
        session_factory = SessionLocal
    db = session_factory()
    try:
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            logger.warning("[WEBHOOK] 投递行不存在：delivery_id=%s", delivery_id)
            return OUTCOME_FAILED, None
        endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)

        # 端点已删/已停用：终态失败（不重试——重试无意义）
        if endpoint is None or not endpoint.active:
            delivery.status = "failed"
            delivery.attempts += 1
            delivery.last_error = "端点已删除或已停用"
            db.commit()
            return OUTCOME_FAILED, None

        # 每次发送前重验 URL（QE1-1 守卫；防注册后 DNS 重绑定到内网）。
        # 校验失败是配置态错误，重试无意义 → 终态失败。
        try:
            validate_webhook_url(endpoint.url, resolver=resolver, allow_private=allow_private)
        except ValueError as exc:
            delivery.status = "failed"
            delivery.attempts += 1
            delivery.last_error = f"URL 校验拒绝（SSRF 防护）：{exc}"
            db.commit()
            return OUTCOME_FAILED, None

        body = json.dumps(delivery.payload or {}, ensure_ascii=False)
        headers = build_delivery_headers(delivery.event, endpoint.secret, body)
        delivery.attempts += 1
        attempt = delivery.attempts
        try:
            sender = http_post or _httpx_post
            status_code = sender(
                endpoint.url, headers=headers, body=body.encode("utf-8"),
                timeout=HTTP_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 网络层异常按失败尝试处理
            status_code = None
            delivery.last_error = f"发送异常：{str(exc)[:500]}"
        else:
            delivery.response_status = status_code
            delivery.last_error = (
                None if 200 <= status_code < 300 else f"接收端返回 {status_code}")

        if status_code is not None and 200 <= status_code < 300:
            delivery.status = "success"
            db.commit()
            return OUTCOME_SUCCESS, None

        if attempt <= MAX_RETRIES:  # attempt 1..MAX_RETRIES 失败 → 仍有重试额度
            db.commit()  # 先留痕 attempts/last_error，再退避重投
            countdown = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.info("[WEBHOOK] 投递失败将退避重试（delivery_id=%s, 第 %s 次, %ss 后）",
                        delivery_id, attempt, countdown)
            return OUTCOME_RETRY, countdown

        delivery.status = "failed"
        db.commit()
        logger.warning("[WEBHOOK] 投递重试耗尽（delivery_id=%s, 共 %s 次）",
                       delivery_id, attempt)
        return OUTCOME_FAILED, None
    finally:
        db.close()


def _httpx_post(url, *, headers, body, timeout):
    """缺省发送通道（httpx 同步 POST，2xx/非 2xx 都返回状态码）"""
    import httpx

    resp = httpx.post(url, headers=headers, content=body, timeout=timeout)
    return resp.status_code


def build_execution_payload(*, task_id: str, execution_number: int, status: str,
                            team_code: Optional[str] = None,
                            gate_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """事件报文构造（completed/failed/gate.failed 共用同一信封）"""
    payload: Dict[str, Any] = {
        "task_id": task_id,
        "execution_number": execution_number,
        "status": status,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    if team_code is not None:
        payload["team_code"] = team_code
    if gate_result is not None:
        payload["gate_result"] = gate_result
    return payload
