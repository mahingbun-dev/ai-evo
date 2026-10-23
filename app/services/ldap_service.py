"""
LDAP/AD 只读认证服务（M4 QE4-2）

企业内网 AD 最常见形态优先（search+bind 二段式）：

1. 只读服务账号（bind_dn/bind_password，AD 必填；openldap 匿名读可留空）
   绑定后按 base_dn + user_filter 搜索登录用户，要求**恰好一条**；
2. 用该用户的 DN + 输入密码二次绑定——绑定成功即认证通过（密码不经
   平台落库，标准 LDAP 认证语义）；写同步不做（方案 §4.2）。

认证通过后 claims（email/username/name/groups）交给
sso_service.provision_user 完成与 OIDC 相同的 JIT 开户 / email 映射 /
groups→team 归属（映射表 sso_team_mappings 两通道共用）。

安全要点：
- 滤值注入：username 进 filter 前做 RFC 4515 转义（escape_filter_value），
  `*`/`(`/`)`/反斜杠/NUL 一律转成 \\XX 十六进制形态，杜绝过滤器改写；
- 空密码直拒：LDAP 服务器对空密码可能按**匿名绑定成功**处理，必须在
  进任何 bind 之前拒绝（authenticate 前置校验）；
- 密码只出现在二次绑定的连接对象里，不进日志/审计/返回值。

测试注入缝：_make_connection（tests 用内嵌 mock 目录替换，零真实网络，
对应方案 §4.5「openldap 容器或内嵌 mock」二选一的内嵌路线）。
"""
import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.sso_models import LdapConfig
from app.services.sso_service import mask_secret

logger = logging.getLogger(__name__)

# 连接/读取超时（秒）：挂死的 DC 不能拖垮登录请求（本地优先账号不受影响，
# 仅 LDAP 旁路等待该上限）
LDAP_TIMEOUT_SECONDS = 5


class LdapError(Exception):
    """LDAP 认证/配置业务异常（消息中文，可直接透出登录页）"""


# ==================== RFC 4515 滤值转义 ====================

_FILTER_ESCAPE_MAP = {
    "\\": r"\5c",
    "*": r"\2a",
    "(": r"\28",
    ")": r"\29",
    "\x00": r"\00",
}


def escape_filter_value(value: str) -> str:
    """LDAP filter 值转义（RFC 4515 §3）：\\ * ( ) NUL → \\XX

    用户名直接拼进 user_filter 的 {username} 占位，不转义则
    `admin*)(objectClass=*)` 类输入可改写过滤器语义（绕过唯一性校验/
    枚举目录）。全量逐字符转义（无白名单捷径——白名单外的字符也可能
    携带合法域账号名，例如中文姓名拼音外的字符集）。
    """
    return "".join(_FILTER_ESCAPE_MAP.get(ch, ch) for ch in str(value or ""))


# ==================== 配置读写 ====================

def get_active_config(db: Session) -> Optional[LdapConfig]:
    """读取启用的 LDAP 配置（单例语义：enabled 最新一条）"""
    return (
        db.query(LdapConfig)
        .filter(LdapConfig.enabled == True)  # noqa: E712
        .order_by(LdapConfig.id.desc())
        .first()
    )


def config_view(row: LdapConfig) -> Dict[str, Any]:
    """配置 API 视图（bind_password 脱敏；提交脱敏值回读保留原值）"""
    return {
        "host": row.host,
        "port": row.port,
        "use_ssl": bool(row.use_ssl),
        "bind_dn": row.bind_dn or "",
        "bind_password": mask_secret(row.bind_password or ""),
        "base_dn": row.base_dn,
        "user_filter": row.user_filter,
        "attr_username": row.attr_username,
        "attr_email": row.attr_email,
        "attr_name": row.attr_name,
        "attr_groups": row.attr_groups,
        "enabled": bool(row.enabled),
        "updated_by": row.updated_by,
    }


def save_config(db: Session, payload: Dict[str, Any], updated_by: str) -> LdapConfig:
    """保存 LDAP 配置（单例 upsert；password 传脱敏回显值则保留原值）"""
    host = (payload.get("host") or "").strip()
    if not host:
        raise LdapError("host 不能为空")
    # 裸主机名合法（默认 ldap://389）；带 scheme 时只接受 ldap/ldaps
    scheme = host.split("://", 1)[0].lower() if "://" in host else ""
    if scheme and scheme not in ("ldap", "ldaps"):
        raise LdapError("host 仅支持 ldap:// 或 ldaps:// 前缀（或裸主机名）")
    base_dn = (payload.get("base_dn") or "").strip()
    if not base_dn:
        raise LdapError("base_dn 不能为空")
    user_filter = (payload.get("user_filter")
                   or "(&(objectClass=person)(sAMAccountName={username}))").strip()
    if "{username}" not in user_filter:
        raise LdapError("user_filter 必须包含 {username} 占位符")
    new_password = (payload.get("bind_password") or "").strip()

    row = get_active_config(db)
    if row is None:
        row = LdapConfig(host=host, base_dn=base_dn, user_filter=user_filter)
        db.add(row)
    else:
        row.host = host
        row.base_dn = base_dn
        row.user_filter = user_filter
    port = payload.get("port")
    row.port = int(port) if port else None
    row.use_ssl = bool(payload.get("use_ssl", False))
    row.bind_dn = (payload.get("bind_dn") or "").strip() or None
    if new_password and "****" not in new_password:
        row.bind_password = new_password
    elif not row.bind_dn:
        row.bind_password = None  # 匿名读场景不残留旧密码
    row.attr_username = (payload.get("attr_username") or "sAMAccountName").strip() or "sAMAccountName"
    row.attr_email = (payload.get("attr_email") or "mail").strip() or "mail"
    row.attr_name = (payload.get("attr_name") or "displayName").strip() or "displayName"
    row.attr_groups = (payload.get("attr_groups") or "memberOf").strip() or "memberOf"
    row.enabled = bool(payload.get("enabled", True))
    row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    return row


# ==================== 连接（测试注入缝） ====================

def _make_connection(config: LdapConfig, bind_dn: Optional[str] = None,
                     password: Optional[str] = None):
    """建立未绑定连接（真实 ldap3 通道；测试替换本函数注入 mock 目录）

    bind_dn/password 缺省取配置里的只读服务账号；二次绑定认证时传用户
    DN + 用户输入密码。连接/读取超时 LDAP_TIMEOUT_SECONDS。
    """
    from ldap3 import Connection, Server

    host = config.host
    server_kwargs: Dict[str, Any] = {
        "use_ssl": bool(config.use_ssl),
        "connect_timeout": LDAP_TIMEOUT_SECONDS,
        "get_info": None,  # 只读认证不需要 schema，省一次往返
    }
    if config.port:
        server_kwargs["port"] = int(config.port)
    server = Server(host, **server_kwargs)
    return Connection(
        server,
        user=bind_dn if bind_dn is not None else (config.bind_dn or None),
        password=password if password is not None else (config.bind_password or None),
        auto_bind=False,
        raise_exceptions=False,
        receive_timeout=LDAP_TIMEOUT_SECONDS,
    )


# ==================== search + bind 认证 ====================

def _first_rdn_value(dn: str) -> str:
    """memberOf 组 DN 的首 RDN 值（CN=qe,OU=.. → qe）

    映射表既可配完整 DN 也可配组短名（管理员两种习惯都常见），
    候选列表同时携带两种形态交 resolve_team 匹配。
    """
    first = dn.split(",", 1)[0]
    if "=" in first:
        first = first.split("=", 1)[1]
    return first.strip().strip('"').lower()


def _entry_attr(entry: Any, attr: str) -> List[str]:
    """ldap3 Entry 属性值列表（str 化、去空；无该属性返回 []）"""
    try:
        values = entry.entry_attributes_as_dict.get(attr) or []
    except Exception:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def authenticate(config: LdapConfig, username: str, password: str) -> Dict[str, Any]:
    """search+bind 只读认证；成功返回 claims（与 OIDC map_claims 同形态）

    异常统一 LdapError：凭据类错误消息与本地登录一致（「用户名或密码
    错误」），配置/目录类错误保留可排障的中文原因。
    """
    username = str(username or "").strip()
    if not username or not password:
        # 空密码前置直拒：LDAP 服务器对空密码可能按匿名绑定成功处理
        raise LdapError("用户名或密码错误")

    # ---- 1. 只读服务账号绑定 + 搜索登录用户 ----
    conn = _make_connection(config)
    try:
        if not conn.bind():
            raise LdapError("LDAP 服务账号绑定失败（检查 bind_dn/bind_password/host）")
        search_filter = config.user_filter.format(
            username=escape_filter_value(username))
        if not conn.search(config.base_dn, search_filter,
                           attributes=["*"]):
            raise LdapError("LDAP 用户搜索失败（检查 base_dn/user_filter）")
        entries = list(conn.entries or [])
        if len(entries) == 0:
            raise LdapError("用户名或密码错误")
        if len(entries) > 1:
            # 唯一性闸：多条匹配说明 filter 过宽，宁可拒绝不可猜（防枚举）
            raise LdapError("LDAP 过滤器命中多条用户，请联系管理员收窄 user_filter")
        entry = entries[0]
        user_dn = str(entry.entry_dn)
        if not user_dn:
            raise LdapError("LDAP 返回的用户条目缺少 DN")
    finally:
        try:
            conn.unbind()
        except Exception:
            pass

    # ---- 2. 用户 DN + 输入密码二次绑定（密码证明，不落库不进日志） ----
    user_conn = _make_connection(config, bind_dn=user_dn, password=password)
    try:
        if not user_conn.bind():
            raise LdapError("用户名或密码错误")
    finally:
        try:
            user_conn.unbind()
        except Exception:
            pass

    email = (_entry_attr(entry, config.attr_email) or [""])[0]
    if not email or "@" not in email:
        # 目录无邮箱属性：合成确定性占位（.invalid 域与真实企业邮箱不冲突），
        # 保证 JIT 的 email 主键语义仍成立
        email = f"{username.lower()}@ldap.invalid"
    name = (_entry_attr(entry, config.attr_name) or [""])[0]
    login_name = (_entry_attr(entry, config.attr_username) or [username])[0]
    group_dns = _entry_attr(entry, config.attr_groups)
    # 候选三种形态交 resolve_team 精确匹配：完整 DN 原样 / DN 小写（LDAP DN
    # 匹配大小写不敏感，映射表按管理员输入习惯可配任意大小写）/ 首 RDN 短名
    groups = group_dns + [dn.lower() for dn in group_dns] \
        + [_first_rdn_value(dn) for dn in group_dns]

    claims = {
        "email": email.lower(),
        "username": login_name,
        "name": name or login_name,
        "groups": groups,
    }
    logger.info(f"[LDAP] 认证成功: {login_name}（email={claims['email']}, "
                f"groups={len(group_dns)} 条）")
    return claims


# ==================== 连通性自检（超管） ====================

def test_connectivity(payload: Dict[str, Any], saved_password: str = "") -> Dict[str, Any]:
    """超管连通性自检：服务账号绑定 + base_dn 根对象搜索

    payload.bind_password 为空或脱敏值时回退 saved_password（先改后测/
    只测已存配置两种用法都成立）。返回 ok/latency_ms/error。
    """
    started = time.time()

    def _fail(msg: str) -> Dict[str, Any]:
        return {"ok": False, "latency_ms": int((time.time() - started) * 1000),
                "error": msg}

    host = (payload.get("host") or "").strip()
    if not host:
        return _fail("host 不能为空")
    probe = LdapConfig(
        host=host,
        port=int(payload["port"]) if payload.get("port") else None,
        use_ssl=bool(payload.get("use_ssl", False)),
        bind_dn=(payload.get("bind_dn") or "").strip() or None,
        bind_password=(
            (payload.get("bind_password") or "").strip()
            if (payload.get("bind_password") or "").strip() and "****" not in payload.get("bind_password", "")
            else (saved_password or None)
        ),
        base_dn=(payload.get("base_dn") or "").strip() or "",
        user_filter=payload.get("user_filter") or "",
    )
    conn = _make_connection(probe)
    try:
        if not conn.bind():
            return _fail("服务账号绑定失败（检查 host/端口/SSL 与 bind_dn/bind_password）")
        if probe.base_dn:
            # base 级根对象搜索：验证 base_dn 可达（常见错配：DN 打错层级）
            if not conn.search(probe.base_dn, "(objectClass=*)",
                               search_scope="BASE", attributes=["1.1"]):
                return _fail(f"base_dn 搜索失败：{probe.base_dn}")
        entries_hint = 1 if probe.base_dn else 0
        return {
            "ok": True,
            "latency_ms": int((time.time() - started) * 1000),
            "bind_ok": True,
            "base_dn_ok": bool(probe.base_dn),
            "searched_entries": entries_hint,
            "error": "",
        }
    except Exception as e:  # ldap3 网络异常等（raise_exceptions=False 时罕见，兜底）
        return _fail(f"连接异常：{e.__class__.__name__}")
    finally:
        try:
            conn.unbind()
        except Exception:
            pass
