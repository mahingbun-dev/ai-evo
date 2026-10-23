"""
企业身份 API（M4 QE4-1/QE4-2）：/api/v1/auth/oidc/*、/api/v1/auth/ldap/*、
/api/v1/auth/methods

端点分两类：
- 匿名（进中间件白名单）：GET /auth/methods（登录页按返回值渲染登录方式）、
  GET /auth/oidc/login（302 到 IdP，state+PKCE）、GET /auth/oidc/callback
  （换 token→JIT→签发本站 JWT→302 回前端 /login?sso_token=...）；
  LDAP 无匿名端点——旁路挂在既有白名单 POST /auth/login 内
  （user_api._ldap_login_fallback：search+bind 成功即 JIT 签发）；
- 超管（开发者设置页）：GET/PUT /auth/oidc/config、POST /auth/oidc/test、
  GET/PUT/POST/DELETE sso_team_mappings（OIDC 与 LDAP 共用）、
  GET/PUT /auth/ldap/config、POST /auth/ldap/test——写操作落审计日志。

AUTH_METHODS 开关（local,oidc,ldap 子集）：不含 oidc 时 /auth/oidc/login
返回 403；不含 local 时 /auth/register 关闭且本地直登不可用（ldap 旁路
仍可经 /auth/login 认证企业目录账号）。
"""
import logging
import secrets
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services import sso_service
from app.services.access_control import ensure_super_admin
from app.services.audit_service import audited, log_operation
from app.services.jwt_service import create_token
from app.services.sso_service import SsoError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["SSO 认证"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _require_oidc_enabled() -> None:
    """AUTH_METHODS 闸：不含 oidc 即 403（与 local 闸同口径）"""
    if "oidc" not in get_settings().auth_methods:
        raise HTTPException(status_code=403, detail="SSO 登录未启用（AUTH_METHODS 未包含 oidc）")


# ==================== 登录方式公告（匿名，登录页渲染依据） ====================

@router.get("/methods", summary="已启用登录方式")
async def auth_methods(db: Session = Depends(get_db)):
    """登录页渲染依据（匿名）：methods 决定显示账密表单/注册页/SSO 按钮；
    oidc_configured/ldap_configured=false 而 methods 含对应项属「开了开关
    没配目录/IdP」的半配置态，登录页对应入口置灰并提示联系管理员。"""
    from app.models.sso_models import LdapConfig

    methods = sorted(get_settings().auth_methods)
    oidc_configured = False
    ldap_configured = False
    if "oidc" in methods:
        oidc_configured = sso_service.get_active_config(db) is not None
    if "ldap" in methods:
        ldap_configured = (
            db.query(LdapConfig)
            .filter(LdapConfig.enabled == True)  # noqa: E712
            .first() is not None
        )
    return {
        "methods": methods,
        "register_enabled": bool(get_settings().AUTH_REGISTER_ENABLED and "local" in methods),
        "oidc_configured": oidc_configured,
        "ldap_configured": ldap_configured,
    }


# ==================== OIDC 登录流（匿名） ====================

def _redirect_uri(request: Request, config) -> str:
    """回调地址：显式配置优先（反代部署外部地址≠容器内地址），否则按请求推导"""
    if config.redirect_uri:
        return config.redirect_uri
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/auth/oidc/callback"


@router.get("/oidc/login", summary="跳转 IdP 发起 SSO 登录")
async def oidc_login(request: Request, redirect: str = "/", db: Session = Depends(get_db)):
    """302 到 IdP 授权端点（state + PKCE S256）；登录事务写入 10 分钟
    HttpOnly Cookie（aievo_oidc_tx，HMAC 签名），回调时校验绑定。"""
    _require_oidc_enabled()
    config = sso_service.get_active_config(db)
    if config is None:
        raise HTTPException(status_code=403, detail="SSO 未配置：请联系超级管理员在开发者设置中完成 OIDC 配置")
    try:
        endpoints = await sso_service.discover(config.issuer)
    except SsoError as e:
        raise HTTPException(status_code=502, detail=str(e))

    state = secrets.token_urlsafe(24)
    verifier, challenge = sso_service.generate_pkce_pair()
    tx = sso_service.issue_tx_token(state=state, verifier=verifier, redirect=redirect)
    auth_url = sso_service.build_authorization_url(
        endpoints, config,
        redirect_uri=_redirect_uri(request, config),
        state=state, code_challenge=challenge,
    )
    resp = RedirectResponse(auth_url, status_code=302)
    # SameSite=Lax：IdP 302 回跳是顶层导航，Lax 放行且防跨站携带
    resp.set_cookie(
        sso_service.TX_COOKIE_NAME, tx,
        max_age=sso_service.TX_TTL_SECONDS, httponly=True, samesite="lax", path="/",
    )
    return resp


@router.get("/oidc/callback", summary="OIDC 回调：换 token/JIT/签发本站 JWT")
async def oidc_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """IdP 回跳端点。成功 302 到 /login?sso_token=<JWT>（前端登录页接管
    落袋与跳转），失败 302 到 /login?sso_error=<中文原因>——不向前端
    泄漏 token/代码细节，错误消息即排障入口。"""
    _require_oidc_enabled()

    def _fail(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/login?sso_error={quote(msg)}", status_code=302)

    if error:
        return _fail(f"IdP 返回错误：{error}")
    if not code or not state:
        return _fail("回调缺少 code/state 参数")

    import hmac as _hmac
    tx_token = request.cookies.get(sso_service.TX_COOKIE_NAME, "")
    tx = sso_service.verify_tx_token(tx_token) if tx_token else None
    if tx is None:
        return _fail("登录会话已过期，请重新发起 SSO 登录")
    if not _hmac.compare_digest(str(tx.get("state", "")), str(state)):
        logger.warning("[SSO] state 不匹配，疑似伪造回调（ip=%s）", _client_ip(request))
        return _fail("登录校验失败（state 不匹配），请重新发起 SSO 登录")

    config = sso_service.get_active_config(db)
    if config is None:
        return _fail("SSO 配置已被停用，请联系管理员")
    try:
        endpoints = await sso_service.discover(config.issuer)
        redirect_uri = _redirect_uri(request, config)
        token = await sso_service.exchange_code(
            config, endpoints, redirect_uri=redirect_uri,
            code=code, code_verifier=tx["verifier"],
        )
        userinfo = await sso_service.fetch_userinfo(endpoints, token["access_token"])
        claims = sso_service.map_claims(config, userinfo)
        user, created = sso_service.provision_user(db, claims)
    except SsoError as e:
        logger.warning("[SSO] 登录失败：%s（ip=%s）", e, _client_ip(request))
        return _fail(str(e))

    platform_token = create_token(user.user_code, user.username)
    # 审计：JIT 开户是安全相关写事件；每次 SSO 登录记 LOGIN 留痕
    log_operation(
        {"user_code": user.user_code, "user_name": user.user_name, "org_code": user.org_code},
        action="CREATE" if created else "LOGIN",
        resource_type="user" if created else "sso",
        resource_id=user.user_code,
        resource_name=user.username,
        detail={"via": "oidc", "email": claims["email"], "groups": claims["groups"]},
        ip_address=_client_ip(request),
    )

    # 回跳目标：仅平台内相对路径（防开放重定向——//evil、https://evil、
    # 反斜杠变体一律回落 /），redirect 由登录事务带回原访问页
    redirect = str(tx.get("redirect") or "/")
    if not redirect.startswith("/") or redirect.startswith("//") or "\\" in redirect:
        redirect = "/"
    resp = RedirectResponse(
        f"/login?sso_token={quote(platform_token)}&redirect={quote(redirect)}", status_code=302)
    resp.delete_cookie(sso_service.TX_COOKIE_NAME, path="/")
    return resp


# ==================== 超管配置面 ====================

class OidcConfigRequest(BaseModel):
    """IdP 配置保存请求（client_secret 传脱敏回显值=保留原值）"""
    issuer: str = Field(..., min_length=1, max_length=500, description="IdP issuer 基址")
    client_id: str = Field(..., min_length=1, max_length=200)
    client_secret: str = Field("", max_length=500, description="留空或脱敏回显值=保留原值")
    scopes: str = Field("openid profile email", max_length=300)
    claim_email: str = Field("email", max_length=100)
    claim_username: str = Field("preferred_username", max_length=100)
    claim_name: str = Field("name", max_length=100)
    claim_groups: str = Field("groups", max_length=100)
    redirect_uri: str = Field("", max_length=500, description="留空按请求推导；反代部署必填")
    enabled: bool = True


@router.get("/oidc/config", summary="读取 OIDC 配置（脱敏）")
async def get_oidc_config(request: Request, db: Session = Depends(get_db)):
    """超管读取当前 IdP 配置（client_secret 脱敏回显）；未配置返回 null 语义"""
    ensure_super_admin(request)
    row = sso_service.get_active_config(db)
    return {"configured": row is not None, "config": sso_service.config_view(row) if row else None}


@router.put("/oidc/config", summary="保存 OIDC 配置",
            dependencies=[Depends(audited("UPDATE", "sso_config"))])
async def put_oidc_config(body: OidcConfigRequest, request: Request, db: Session = Depends(get_db)):
    """保存 IdP 配置（单例 upsert，超管）。落审计（不含 secret 明文）。"""
    ensure_super_admin(request)
    try:
        row = sso_service.save_config(db, body.model_dump(), updated_by=request.state.user.get("user_code", ""))
    except SsoError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # QE4-4：审计改走 audited 路由依赖（detail 不含 secret 明文）
    request.state.audit_resource = {
        "id": str(row.id), "name": row.issuer,
        "detail": {"issuer": row.issuer, "client_id": row.client_id, "enabled": row.enabled},
    }
    return sso_service.config_view(row)


@router.post("/oidc/test", summary="OIDC 连通性自检")
async def test_oidc(body: OidcConfigRequest, request: Request, db: Session = Depends(get_db)):
    """对候选配置做 discovery 探测（保存前即可验证；secret 留空回退已存值）"""
    ensure_super_admin(request)
    saved = sso_service.get_active_config(db)
    saved_secret = saved.client_secret if saved else ""
    return await sso_service.test_connectivity(body.model_dump(), saved_secret=saved_secret)


# ==================== groups → team 映射 CRUD（超管） ====================

class TeamMappingRequest(BaseModel):
    idp_group: str = Field(..., min_length=1, max_length=200, description="IdP groups claim 中的组名")
    team_code: str = Field(..., min_length=1, max_length=100, description="平台团队代码")
    team_name: str = Field("", max_length=200, description="团队显示名（建团队时用，可空）")


class TeamMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idp_group: str
    team_code: str
    team_name: Optional[str] = None


@router.get("/oidc/team-mappings", response_model=List[TeamMappingResponse], summary="列出 groups 映射")
async def list_team_mappings(request: Request, db: Session = Depends(get_db)):
    ensure_super_admin(request)
    from app.models.sso_models import SsoTeamMapping
    return db.query(SsoTeamMapping).order_by(SsoTeamMapping.id.asc()).all()


@router.post("/oidc/team-mappings", response_model=TeamMappingResponse, summary="新增 groups 映射",
             dependencies=[Depends(audited("CREATE", "sso_team_mapping"))])
async def create_team_mapping(body: TeamMappingRequest, request: Request, db: Session = Depends(get_db)):
    ensure_super_admin(request)
    from app.models.sso_models import SsoTeamMapping
    exists = db.query(SsoTeamMapping).filter(SsoTeamMapping.idp_group == body.idp_group.strip()).first()
    if exists:
        raise HTTPException(status_code=409, detail=f"组 {body.idp_group} 已有映射（team_code={exists.team_code}）")
    row = SsoTeamMapping(
        idp_group=body.idp_group.strip(),
        team_code=body.team_code.strip(),
        team_name=(body.team_name or "").strip() or None,
        created_by=request.state.user.get("user_code", ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    # QE4-4：审计改走 audited 路由依赖
    request.state.audit_resource = {
        "id": str(row.id), "name": row.idp_group,
        "detail": {"idp_group": row.idp_group, "team_code": row.team_code},
    }
    return row


@router.delete("/oidc/team-mappings/{mapping_id}", summary="删除 groups 映射",
               dependencies=[Depends(audited("DELETE", "sso_team_mapping", id_param="mapping_id"))])
async def delete_team_mapping(mapping_id: int, request: Request, db: Session = Depends(get_db)):
    ensure_super_admin(request)
    from app.models.sso_models import SsoTeamMapping
    row = db.get(SsoTeamMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="映射不存在")
    request.state.audit_resource = {
        "id": str(mapping_id), "name": row.idp_group,
        "detail": {"idp_group": row.idp_group, "team_code": row.team_code},
    }
    db.delete(row)
    db.commit()
    return {"success": True}


# ==================== LDAP/AD 配置面（QE4-2，超管） ====================

class LdapConfigRequest(BaseModel):
    """LDAP 配置保存请求（bind_password 传脱敏回显值=保留原值）"""
    host: str = Field(..., min_length=1, max_length=300, description="主机名或 ldap(s):// 地址")
    port: Optional[int] = Field(None, ge=1, le=65535, description="端口（空=389/636 默认）")
    use_ssl: bool = Field(False, description="是否 LDAPS")
    bind_dn: str = Field("", max_length=500, description="只读服务账号 DN（openldap 匿名读可留空）")
    bind_password: str = Field("", max_length=500, description="留空或脱敏回显值=保留原值")
    base_dn: str = Field(..., min_length=1, max_length=500)
    user_filter: str = Field(
        "(&(objectClass=person)(sAMAccountName={username}))", max_length=500,
        description="用户搜索过滤器，{username} 占位（值做 RFC4515 转义）")
    attr_username: str = Field("sAMAccountName", max_length=100)
    attr_email: str = Field("mail", max_length=100)
    attr_name: str = Field("displayName", max_length=100)
    attr_groups: str = Field("memberOf", max_length=100)
    enabled: bool = True


@router.get("/ldap/config", summary="读取 LDAP 配置（脱敏）")
async def get_ldap_config(request: Request, db: Session = Depends(get_db)):
    """超管读取当前 LDAP 配置（bind_password 脱敏回显）；未配置返回 null 语义"""
    ensure_super_admin(request)
    from app.services import ldap_service
    row = ldap_service.get_active_config(db)
    return {"configured": row is not None,
            "config": ldap_service.config_view(row) if row else None}


@router.put("/ldap/config", summary="保存 LDAP 配置",
            dependencies=[Depends(audited("UPDATE", "ldap_config"))])
async def put_ldap_config(body: LdapConfigRequest, request: Request, db: Session = Depends(get_db)):
    """保存 LDAP 配置（单例 upsert，超管）。落审计（不含 bind_password）。"""
    ensure_super_admin(request)
    from app.services import ldap_service
    try:
        row = ldap_service.save_config(
            db, body.model_dump(), updated_by=request.state.user.get("user_code", ""))
    except ldap_service.LdapError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # QE4-4：审计改走 audited 路由依赖（detail 不含 bind_password）
    request.state.audit_resource = {
        "id": str(row.id), "name": row.host,
        "detail": {"host": row.host, "base_dn": row.base_dn, "enabled": row.enabled},
    }
    return ldap_service.config_view(row)


@router.post("/ldap/test", summary="LDAP 连通性自检")
async def test_ldap(body: LdapConfigRequest, request: Request, db: Session = Depends(get_db)):
    """对候选配置做「服务账号绑定 + base_dn 根对象搜索」探测（保存前可验证）"""
    ensure_super_admin(request)
    from app.services import ldap_service
    saved = ldap_service.get_active_config(db)
    return await _run_ldap_test(body, saved)


async def _run_ldap_test(body: LdapConfigRequest, saved_row):
    """同步包一层（ldap3 为同步客户端；探测本身 ≤2 次目录往返）"""
    from app.services import ldap_service
    saved_password = saved_row.bind_password if saved_row else ""
    return ldap_service.test_connectivity(body.model_dump(), saved_password=saved_password)
