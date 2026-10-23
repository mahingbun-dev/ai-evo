"""License 服务（M6 QE6-1）：Ed25519 验签 + 内存缓存 + 降级不阻断

职责边界（方案 §M6）：
- license 文件 = 自包含 JSON 信封（payload=base64url(canonical JSON claims)
  + sig=Ed25519 签名），格式与 tools/license/gen.py 同口径；
- 启动加载（main.py lifespan 调 load_from_db）+ 超管上传后即时刷新缓存
  （install_license），验签结果缓存在进程内（_state），读侧 current_state()
  零 DB/零验签开销；
- **失败只降级不阻断**：无 license / 验签失败 / 已过期 → 有效档位一律回落
  oss（核心评测不受影响），同时醒目告警（logger.warning 多行横幅）；
  任何异常都不向上抛——license 是商业治理面，不是可用性依赖；
- 企业功能位（features[]）只在此处解析；entitlements 门面（QE6-2）在
  本模块之上判 tier/features。

线程模型：_state 的读写用 _lock 保护（api 进程多协程 + lifespan 单点写入，
锁粒度小、无 IO，不构成竞争热点）。
"""
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ==================== 常量（tier 与企业功能位枚举） ====================

TIER_OSS = "oss"
TIER_ENTERPRISE = "enterprise"
LICENSE_TIERS = (TIER_OSS, TIER_ENTERPRISE)

# 企业功能位（与方案 §M6 QE6-2 对齐；预留位一并枚举，前端锁标渲染同源）：
# - audit_export        审计导出（M4 已有 AUDIT_EXPORT_ENABLED 环境位，QE6-2
#                       接线到 license 后环境位保留为总闸）
# - report_white_label  报告白标（去 logo/自定义 logo）
# - report_watermark    汇总页水印
# - project_rbac        项目级细粒度 RBAC（预留）
# - scim                SCIM 目录同步（预留）
# - admin_api           Admin API（预留）
ENTERPRISE_FEATURES = (
    "audit_export",
    "report_white_label",
    "report_watermark",
    "project_rbac",
    "scim",
    "admin_api",
)

# license 状态（status 字段取值；有效档位 effective_tier 仅在 valid 时为 enterprise）
STATUS_MISSING = "missing"            # 未上传任何 license
STATUS_VALID = "valid"                # 验签通过且未过期
STATUS_EXPIRED = "expired"            # 验签通过但已过期 → 降级 oss
STATUS_INVALID = "invalid_signature"  # 验签失败/信封不合法 → 降级 oss


# ==================== 信封编解码（与 tools/license/gen.py 同口径） ====================

def _b64u_decode(text: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class LicenseError(Exception):
    """license 解析/验签失败（中文消息可直接透出给超管上传端点）"""


def verify_license_text(text: str, public_key_b64: Optional[str] = None) -> Dict[str, Any]:
    """验签单个 license 文件原文，通过返回 claims，失败抛 LicenseError。

    只做密码学与结构校验，不做过期判定（过期是合法 license 的时间语义，
    由 _evaluate 区分 status=expired，与篡改类 invalid 分开上报）。
    """
    try:
        envelope = json.loads(text)
        if not isinstance(envelope, dict) or envelope.get("alg") != "Ed25519":
            raise LicenseError("license 信封不合法：缺 alg=Ed25519 字段")
        payload_b64 = envelope.get("payload") or ""
        sig_b64 = envelope.get("sig") or ""
        if not payload_b64 or not sig_b64:
            raise LicenseError("license 信封不合法：缺 payload/sig 字段")
        payload = _b64u_decode(payload_b64)
        signature = _b64u_decode(sig_b64)
    except LicenseError:
        raise
    except Exception as e:
        raise LicenseError(f"license 文件解析失败：{e}")

    key_b64 = (public_key_b64 or "").strip() or get_settings().LICENSE_PUBLIC_KEY
    try:
        import base64
        try:
            raw_key = base64.b64decode(key_b64, validate=True)
        except Exception:
            raw_key = _b64u_decode(key_b64)
        public_key = Ed25519PublicKey.from_public_bytes(raw_key)
    except Exception as e:
        raise LicenseError(f"LICENSE_PUBLIC_KEY 配置不合法（非 Ed25519 公钥）：{e}")

    try:
        public_key.verify(signature, payload)
    except InvalidSignature:
        raise LicenseError("license 验签失败：签名与内容/公钥不匹配（文件被篡改或非本平台签发）")
    except Exception as e:
        raise LicenseError(f"license 验签异常：{e}")

    try:
        claims = json.loads(payload.decode("utf-8"))
    except Exception as e:
        raise LicenseError(f"license claims 解析失败：{e}")

    # 结构校验：必填字段与取值域（tier 白名单进判定，防伪造 tier 字符串）
    if not isinstance(claims, dict):
        raise LicenseError("license claims 不合法：非 JSON 对象")
    customer = str(claims.get("customer") or "").strip()
    tier = str(claims.get("tier") or "").strip().lower()
    if not customer:
        raise LicenseError("license claims 不合法：缺 customer")
    if tier not in LICENSE_TIERS:
        raise LicenseError(f"license claims 不合法：tier 仅允许 {'/'.join(LICENSE_TIERS)}")
    expires_at = claims.get("expires_at")
    if expires_at is not None:
        try:
            datetime.fromisoformat(str(expires_at))
        except Exception:
            raise LicenseError("license claims 不合法：expires_at 非合法 ISO8601")
    for int_field in ("max_users", "max_monthly_runs"):
        if claims.get(int_field) is not None and not isinstance(claims.get(int_field), int):
            raise LicenseError(f"license claims 不合法：{int_field} 须为整数")
    features = claims.get("features")
    if features is not None and not isinstance(features, list):
        raise LicenseError("license claims 不合法：features 须为列表")
    return claims


# ==================== 缓存状态 ====================

class LicenseState:
    """进程内缓存的 license 生效态（value object，字段只读语义）"""

    __slots__ = ("status", "tier", "effective_tier", "customer", "expires_at",
                 "max_users", "max_monthly_runs", "features", "message",
                 "fingerprint", "loaded_at")

    def __init__(self, status: str, tier: str, effective_tier: str,
                 customer: str = "", expires_at: Optional[str] = None,
                 max_users: Optional[int] = None,
                 max_monthly_runs: Optional[int] = None,
                 features: Optional[List[str]] = None, message: str = "",
                 fingerprint: str = "", loaded_at: Optional[datetime] = None):
        self.status = status
        self.tier = tier
        self.effective_tier = effective_tier
        self.customer = customer
        self.expires_at = expires_at
        self.max_users = max_users
        self.max_monthly_runs = max_monthly_runs
        self.features = features or []
        self.message = message
        self.fingerprint = fingerprint
        self.loaded_at = loaded_at or datetime.now(timezone.utc)

    def to_view(self) -> Dict[str, Any]:
        """状态 API 视图（前端 License 卡渲染依据；含降级告警消息）"""
        return {
            "status": self.status,
            "tier": self.tier,
            "effective_tier": self.effective_tier,
            "enterprise": self.effective_tier == TIER_ENTERPRISE,
            "customer": self.customer,
            "expires_at": self.expires_at,
            "max_users": self.max_users,
            "max_monthly_runs": self.max_monthly_runs,
            "features": list(self.features),
            "message": self.message,
            "fingerprint": self.fingerprint,
            "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
        }


_lock = threading.Lock()
_state: Optional[LicenseState] = None

_FALLBACK_STATE = LicenseState(
    status=STATUS_MISSING, tier=TIER_OSS, effective_tier=TIER_OSS,
    message="未加载 license（进程启动后尚未初始化，按开源版口径运行）",
)


def current_state() -> LicenseState:
    """当前缓存态：未初始化时返回 oss 兜底（不触发 DB/验签，绝不抛错）"""
    with _lock:
        return _state or _FALLBACK_STATE


def _store(state: LicenseState) -> LicenseState:
    global _state
    with _lock:
        _state = state
    return state


def _evaluate(text: str) -> LicenseState:
    """对单条 license 原文做完整判定（验签 + 过期），产出生效态。

    验签失败/过期都产出降级态（effective_tier=oss）并醒目告警，不抛错——
    「降级不阻断」是本模块的硬约束（方案 §M6 风险对策：license 误伤核心
    功能 → 验签失败只降级不阻断）。
    """
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        claims = verify_license_text(text)
    except LicenseError as e:
        logger.warning(
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║ [LICENSE] ⚠ 验签失败，已降级为开源版（oss）——核心评测不受影响 ║\n"
            "║ 原因: %s\n"
            "║ 指纹: %s…\n"
            "║ 处置: 请联系厂商重新签发后，由超管在「开发者设置 → License」重新上传\n"
            "╚══════════════════════════════════════════════════════════════╝", str(e), fingerprint[:16])
        return LicenseState(
            status=STATUS_INVALID, tier=TIER_OSS, effective_tier=TIER_OSS,
            message=f"验签失败已降级开源版：{e}", fingerprint=fingerprint)

    tier = str(claims.get("tier"))
    expires_at = claims.get("expires_at")
    if expires_at:
        exp = datetime.fromisoformat(str(expires_at))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            logger.warning(
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║ [LICENSE] ⚠ license 已过期（%s），降级为开源版——核心评测不受影响 ║\n"
                "║ 客户: %s\n"
                "║ 处置: 续期后由超管重新上传（企业功能位在过期期间锁定）\n"
                "╚══════════════════════════════════════════════════════════════╝",
                expires_at, claims.get("customer"))
            return LicenseState(
                status=STATUS_EXPIRED, tier=tier, effective_tier=TIER_OSS,
                customer=str(claims.get("customer") or ""), expires_at=str(expires_at),
                max_users=claims.get("max_users"), max_monthly_runs=claims.get("max_monthly_runs"),
                features=[str(f) for f in (claims.get("features") or [])],
                message=f"license 已过期（{expires_at}），已降级开源版", fingerprint=fingerprint)

    effective = tier  # 未过期且验签通过：tier 即生效档位
    logger.info(
        "[LICENSE] license 加载成功：customer=%s tier=%s expires_at=%s features=%s",
        claims.get("customer"), tier, expires_at or "永久", claims.get("features") or [])
    return LicenseState(
        status=STATUS_VALID, tier=tier, effective_tier=effective,
        customer=str(claims.get("customer") or ""), expires_at=str(expires_at) if expires_at else None,
        max_users=claims.get("max_users"), max_monthly_runs=claims.get("max_monthly_runs"),
        features=[str(f) for f in (claims.get("features") or [])],
        message="license 有效", fingerprint=fingerprint)


# ==================== DB 入口（启动加载 / 超管上传） ====================

def load_from_db(db: Session) -> LicenseState:
    """启动加载：取最新一条上传记录验签缓存；无记录=天然开源版（不告警）。

    任何异常（表不存在/DB 抖动/内容坏）都降级不抛——lifespan 调用方仍有
    try/except，但本函数自身保证「license 永不阻断启动」。
    """
    try:
        from app.models.license_models import PlatformLicense
        row = (
            db.query(PlatformLicense)
            .order_by(PlatformLicense.id.desc())
            .first()
        )
        if row is None:
            logger.info("[LICENSE] 未上传 license：按开源版（oss）运行，核心评测不受影响")
            return _store(LicenseState(
                status=STATUS_MISSING, tier=TIER_OSS, effective_tier=TIER_OSS,
                message="未上传 license：按开源版（oss）运行"))
        return _store(_evaluate(row.license_text))
    except Exception as e:  # noqa: BLE001 —— 降级不阻断的兜底口径
        logger.warning("[LICENSE] 加载失败（%s）：降级开源版运行，核心评测不受影响", e)
        return _store(LicenseState(
            status=STATUS_MISSING, tier=TIER_OSS, effective_tier=TIER_OSS,
            message=f"license 加载异常已降级开源版：{e}"))


def install_license(db: Session, text: str, uploaded_by: str = "") -> LicenseState:
    """超管上传：验签前置（坏文件直接拒绝，不落库）→ 存最新行 → 刷新缓存。

    抛 LicenseError 由 API 层转 400（上传是管理员主动动作，给明确反馈；
    与启动加载的静默降级口径不同——那里不能打断服务）。
    """
    claims = verify_license_text(text)  # 坏信封/验签失败 → LicenseError
    from app.models.license_models import PlatformLicense
    row = PlatformLicense(
        license_text=text,
        fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        customer=str(claims.get("customer") or "") or None,
        tier=str(claims.get("tier") or "") or None,
        uploaded_by=uploaded_by or None,
    )
    db.add(row)
    db.commit()
    return _store(_evaluate(text))


# ==================== 读侧门面（QE6-2 entitlements 在此之上接线） ====================

def effective_tier() -> str:
    """当前生效档位（valid→license tier，其余→oss）；QE6-2/前端锁标共用"""
    return current_state().effective_tier


def has_feature(feature: str) -> bool:
    """企业功能位判定：仅 effective_tier=enterprise 且 features 声明时为真"""
    state = current_state()
    return state.effective_tier == TIER_ENTERPRISE and feature in state.features
