"""
OIDC SSO 服务（M4 QE4-1）

httpx 手写 Authorization Code + PKCE（S256）流（方案 §4.1 二选一取
httpx——authlib 不再新增依赖；httpx 已在 requirements.lock）：

- discovery：GET {issuer}/.well-known/openid-configuration（进程内 TTL
  缓存，配置保存时失效）；
- login：生成 state + PKCE verifier/challenge，302 到 IdP 授权端点；
  state 与 verifier 绑定进 HMAC 签名短时 Cookie（aievo_oidc_tx，10 分钟），
  回调时校验——不依赖服务端会话/Redis，多 api 副本天然可用；
- callback：换 token（client_secret_post）→ 拉 userinfo → claims 映射
  （email/username/name/groups 字段名可配）→ JIT 开户或按 email 映射
  既有账号 → groups 命中 sso_team_mappings 首条映射即 upsert 团队并
  归属 → 签发本站 JWT。

超管连通性自检（test_connectivity）：对候选配置做 discovery 探测并回带
端点/耗时/错误，保存前即可验证。

测试注入缝：_client_factory（tests/unit/test_oidc_sso.py 用
httpx.ASGITransport 挂 mock IdP，绝不打真实网络）。
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping
from app.models.user_models import User

logger = logging.getLogger(__name__)

# 登录事务 Cookie（state+PKCE verifier 载体）有效期：覆盖「跳 IdP→用户
# 输密码→跳回」的正常时长；过长放大重放窗口，过短长表单填完即失效
TX_COOKIE_NAME = "aievo_oidc_tx"
TX_TTL_SECONDS = 600

# discovery 缓存 TTL：OIDC 端点地址极少变化；配置保存即主动失效
_DISCOVERY_TTL_SECONDS = 300
_discovery_cache: Dict[str, Tuple[float, Dict[str, str]]] = {}


class SsoError(Exception):
    """SSO 流程业务异常（消息中文，可直接透出给登录页）"""


# ==================== http 注入缝 ====================

def _default_client_factory() -> httpx.AsyncClient:
    """默认真实网络客户端（10s 超时：IdP 探测/换 token 不拖死登录请求）"""
    return httpx.AsyncClient(timeout=10.0)


# 模块级可替换（测试注入 ASGITransport mock IdP）
_client_factory = _default_client_factory


# ==================== 配置读写 ====================

def get_active_config(db: Session) -> Optional[SsoOidcConfig]:
    """读取启用的 IdP 配置（单例语义：enabled 最新一条）"""
    return (
        db.query(SsoOidcConfig)
        .filter(SsoOidcConfig.enabled == True)  # noqa: E712
        .order_by(SsoOidcConfig.id.desc())
        .first()
    )


def mask_secret(secret: str) -> str:
    """client_secret 脱敏回显（与 claude_code_config_service.mask_token 同口径）"""
    if not secret or len(secret) <= 8:
        return "****"
    return f"{secret[:4]}****{secret[-4:]}"


def config_view(row: SsoOidcConfig) -> Dict[str, Any]:
    """配置 API 视图（client_secret 脱敏；提交脱敏值回读保留原值）"""
    return {
        "issuer": row.issuer,
        "client_id": row.client_id,
        "client_secret": mask_secret(row.client_secret),
        "scopes": row.scopes,
        "claim_email": row.claim_email,
        "claim_username": row.claim_username,
        "claim_name": row.claim_name,
        "claim_groups": row.claim_groups,
        "redirect_uri": row.redirect_uri or "",
        "enabled": bool(row.enabled),
        "updated_by": row.updated_by,
    }


def save_config(db: Session, payload: Dict[str, Any], updated_by: str) -> SsoOidcConfig:
    """保存 IdP 配置（单例 upsert；secret 传脱敏回显值则保留原值）"""
    issuer = (payload.get("issuer") or "").strip().rstrip("/")
    if not issuer.startswith(("http://", "https://")):
        raise SsoError("issuer 需以 http:// 或 https:// 开头")
    client_id = (payload.get("client_id") or "").strip()
    if not client_id:
        raise SsoError("client_id 不能为空")
    scopes = (payload.get("scopes") or "openid profile email").strip()
    if "openid" not in scopes.split():
        raise SsoError("scopes 必须包含 openid")
    new_secret = (payload.get("client_secret") or "").strip()

    row = get_active_config(db)
    if row is None:
        if not new_secret or "****" in new_secret:
            raise SsoError("首次配置必须填写 client_secret")
        row = SsoOidcConfig(
            issuer=issuer,
            client_id=client_id,
            client_secret=new_secret,
        )
        db.add(row)
    else:
        row.issuer = issuer
        row.client_id = client_id
        # 前端回显的脱敏值未修改时保留原 secret
        if new_secret and "****" not in new_secret:
            row.client_secret = new_secret
    row.scopes = scopes
    row.claim_email = (payload.get("claim_email") or "email").strip() or "email"
    row.claim_username = (payload.get("claim_username") or "preferred_username").strip() or "preferred_username"
    row.claim_name = (payload.get("claim_name") or "name").strip() or "name"
    row.claim_groups = (payload.get("claim_groups") or "groups").strip() or "groups"
    row.redirect_uri = (payload.get("redirect_uri") or "").strip() or None
    row.enabled = bool(payload.get("enabled", True))
    row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    invalidate_discovery_cache()
    return row


# ==================== discovery ====================

async def discover(issuer: str) -> Dict[str, str]:
    """拉取（并缓存）IdP discovery 文档，返回三个端点

    失败（网络/非 200/缺端点）抛 SsoError——登录入口与自检共用，
    错误消息面向超管/登录页可直接展示。
    """
    now = time.time()
    cached = _discovery_cache.get(issuer)
    if cached and now - cached[0] < _DISCOVERY_TTL_SECONDS:
        return cached[1]
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        async with _client_factory() as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise SsoError(f"无法访问 IdP discovery（{url}）：{e.__class__.__name__}")
    if resp.status_code != 200:
        raise SsoError(f"IdP discovery 返回 HTTP {resp.status_code}（{url}）")
    try:
        doc = resp.json()
        endpoints = {
            "authorization_endpoint": doc["authorization_endpoint"],
            "token_endpoint": doc["token_endpoint"],
            "userinfo_endpoint": doc["userinfo_endpoint"],
        }
    except (ValueError, KeyError) as e:
        raise SsoError(f"IdP discovery 文档不完整（缺 {e}）")
    _discovery_cache[issuer] = (now, endpoints)
    return endpoints


def invalidate_discovery_cache() -> None:
    """配置保存后失效 discovery 缓存"""
    _discovery_cache.clear()


async def test_connectivity(payload: Dict[str, Any], saved_secret: str = "") -> Dict[str, Any]:
    """超管连通性自检：对候选配置做 discovery 探测

    payload.client_secret 为空或脱敏值时回退 saved_secret（先改后测/
    只测已存配置两种用法都成立）。返回 ok/endpoints/latency_ms/error。
    """
    issuer = (payload.get("issuer") or "").strip().rstrip("/")
    if not issuer.startswith(("http://", "https://")):
        return {"ok": False, "error": "issuer 需以 http:// 或 https:// 开头"}
    # 候选 issuer 可能与已缓存的不同；探测前清缓存保证「改完立刻测」是真探测
    _discovery_cache.pop(issuer, None)
    started = time.time()
    try:
        endpoints = await discover(issuer)
        return {
            "ok": True,
            "issuer": issuer,
            "endpoints": endpoints,
            "latency_ms": int((time.time() - started) * 1000),
            "client_secret_provided": bool(
                (payload.get("client_secret") or "").strip() and "****" not in payload.get("client_secret", "")
            ) or bool(saved_secret),
            "error": "",
        }
    except SsoError as e:
        return {
            "ok": False,
            "issuer": issuer,
            "endpoints": {},
            "latency_ms": int((time.time() - started) * 1000),
            "client_secret_provided": False,
            "error": str(e),
        }


# ==================== PKCE 与登录事务 Cookie ====================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> Tuple[str, str]:
    """PKCE verifier/challenge（S256）。verifier 86 字符（43-128 合法域内）"""
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _tx_sign(data: bytes) -> str:
    key = get_settings().auth_jwt_secret.encode("utf-8")
    return _b64url(hmac.new(key, data, hashlib.sha256).digest())


def issue_tx_token(state: str, verifier: str, redirect: str) -> str:
    """登录事务签名令牌（state+verifier+redirect，10 分钟）

    与 jwt_service 同密钥（auth_jwt_secret）同 HMAC-SHA256 手法，但结构
    独立（sub 不存在），仅由本模块 verify_tx_token 消费。
    """
    payload = json.dumps(
        {"state": state, "verifier": verifier, "redirect": redirect,
         "exp": int(time.time()) + TX_TTL_SECONDS},
        separators=(",", ":"),
    ).encode("utf-8")
    body = _b64url(payload)
    return f"{body}.{_tx_sign(body.encode('ascii'))}"


def verify_tx_token(token: str) -> Optional[Dict[str, Any]]:
    """校验并解析登录事务令牌；篡改/过期返回 None"""
    try:
        body, sig = token.split(".")
        if not hmac.compare_digest(_tx_sign(body.encode("ascii")), sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


# ==================== 授权 URL / 换 token / userinfo ====================

def build_authorization_url(
    endpoints: Dict[str, str],
    config: SsoOidcConfig,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """构造 IdP 授权跳转 URL（response_type=code + PKCE S256 + state）"""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    # Request 构造器完成 URL 编码（中文/space 等按 RFC 3986 编码后拼接）
    return str(httpx.Request("GET", endpoints["authorization_endpoint"], params=params).url)


async def exchange_code(
    config: SsoOidcConfig,
    endpoints: Dict[str, str],
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> Dict[str, Any]:
    """授权码换 token（client_secret_post；错误统一抛 SsoError 中文消息）"""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code_verifier": code_verifier,
    }
    try:
        async with _client_factory() as client:
            resp = await client.post(endpoints["token_endpoint"], data=data)
    except httpx.HTTPError as e:
        raise SsoError(f"访问 IdP token 端点失败：{e.__class__.__name__}")
    if resp.status_code != 200:
        # RFC 6749 §5.2：error 字段尽量透出（invalid_grant 等），便于排障
        try:
            err = resp.json().get("error", "")
        except ValueError:
            err = ""
        raise SsoError(f"IdP 换取 token 失败（HTTP {resp.status_code}{(' ' + err) if err else ''}）")
    token = resp.json()
    if not token.get("access_token"):
        raise SsoError("IdP token 响应缺少 access_token")
    return token


async def fetch_userinfo(endpoints: Dict[str, str], access_token: str) -> Dict[str, Any]:
    """Bearer access_token 拉 userinfo（JSON dict）"""
    try:
        async with _client_factory() as client:
            resp = await client.get(
                endpoints["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as e:
        raise SsoError(f"访问 IdP userinfo 端点失败：{e.__class__.__name__}")
    if resp.status_code != 200:
        raise SsoError(f"IdP userinfo 返回 HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise SsoError("IdP userinfo 响应不是合法 JSON")


def map_claims(config: SsoOidcConfig, userinfo: Dict[str, Any]) -> Dict[str, Any]:
    """按配置的 claim 字段名提取 email/username/name/groups

    email 缺失即拒（JIT 与既有账号映射都依赖 email 主键）；groups 缺失
    视为空列表（团队映射可选功能，不强求 IdP 提供）。
    """
    email = str(userinfo.get(config.claim_email) or "").strip()
    if not email or "@" not in email:
        raise SsoError(f"IdP userinfo 缺少可用 email（claim: {config.claim_email}）")
    username = str(userinfo.get(config.claim_username) or "").strip()
    name = str(userinfo.get(config.claim_name) or "").strip()
    groups_raw = userinfo.get(config.claim_groups)
    groups = [str(g) for g in groups_raw if str(g).strip()] if isinstance(groups_raw, list) else []
    return {"email": email.lower(), "username": username, "name": name, "groups": groups}


# ==================== 团队映射与 JIT 开户 ====================

def resolve_team(db: Session, groups: List[str]) -> Optional[Tuple[str, str]]:
    """groups → (team_code, team_name)：id 升序首个命中（确定性）

    命中即 upsert Team（不存在的团队即时建立——企业 IdP 侧建组、平台侧
    映射即可开团，无需先去团队页手工建）；未命中返回 None（不动用户归属）。
    """
    if not groups:
        return None
    from app.services.team_service import upsert_team

    mapping = (
        db.query(SsoTeamMapping)
        .filter(SsoTeamMapping.idp_group.in_(groups))
        .order_by(SsoTeamMapping.id.asc())
        .first()
    )
    if mapping is None:
        return None
    upsert_team(db, mapping.team_code, mapping.team_name or mapping.team_code)
    return mapping.team_code, mapping.team_name or mapping.team_code


def _unique_username(db: Session, base: str) -> str:
    """JIT 用户名防撞：base 被占则追加 _oidc 序号（限定字符集，与注册规则一致）"""
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:24] or "sso_user"
    candidate = base
    seq = 2
    while db.query(User.user_code).filter(User.username == candidate).first():
        candidate = f"{base}_{seq}"
        seq += 1
    return candidate


def provision_user(db: Session, claims: Dict[str, Any], source: str = "oidc") -> Tuple[User, bool]:
    """JIT 开户 / 按 email 映射既有账号，返回 (user, 是否新建)

    OIDC（QE4-1）与 LDAP（QE4-2）共用：claims 形态一致
    {email, username, name, groups}，groups→team 映射共用
    sso_team_mappings（resolve_team）。

    - 既有账号：email 大小写不敏感匹配；本地禁用的账号拒绝登录（与
      /auth/login 同口径——禁用必须对 SSO/LDAP 通道同样有效）；
    - 新建账号：user_code=f"u_"+uuid、password_hash=None（本地登录不可用，
      与存量 SSO 用户同语义）、super_admin_flag=0；
    - groups 命中映射 → org_code/org_name 更新（团队 upsert 在
      resolve_team 内完成）；未命中保持原值（新用户则为空 → DEFAULT 兜底）。
    """
    from sqlalchemy import func

    email = claims["email"]
    user = (
        db.query(User)
        .filter(func.lower(User.email) == email)
        .first()
    )
    created = False
    if user is not None:
        if not user.is_active:
            raise SsoError("账号已被禁用，请联系管理员")
    else:
        base = claims.get("username") or email.split("@")[0]
        user = User(
            user_code=f"u_{uuid.uuid4().hex[:16]}",
            username=_unique_username(db, base),
            password_hash=None,
            user_name=claims.get("name") or base,
            email=email,
            role="engineer",
            super_admin_flag=0,
            application_admin=0,
            is_active=True,
        )
        db.add(user)
        created = True

    team = resolve_team(db, claims.get("groups") or [])
    if team is not None:
        user.org_code = team[0]
        user.org_name = team[1]
    if claims.get("name"):
        user.user_name = claims["name"]
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    logger.info(
        f"[SSO] JIT {'新建' if created else '映射既有'}用户: {user.username} "
        f"(via={source}, email={email}, groups={claims.get('groups')})"
    )
    return user, created
