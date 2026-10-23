"""
企业身份数据模型（M4 QE4-1 OIDC / QE4-2 LDAP）

- SsoOidcConfig：IdP 配置单例（issuer/client_id/client_secret/scopes/
  claims 映射），由超管在开发者设置页维护；client_secret 读接口脱敏，
  库内存明文（与 model_configs 存凭据的现状口径一致）；
- SsoTeamMapping：IdP groups claim / LDAP memberOf → 平台 team_code 映射表
  （OIDC 与 LDAP 共用），登录时首个命中的映射生效（按 id 升序，确定性），
  并即时 upsert Team；
- LdapConfig：LDAP/AD 只读认证配置单例（host/bind_dn/base_dn/user_filter/
  属性映射），search+bind 认证，写同步不做（QE4-2）。
"""
from sqlalchemy import Boolean, Column, Integer, String, DateTime
from sqlalchemy.sql import func

from app.db.session import Base


class SsoOidcConfig(Base):
    """OIDC IdP 配置（单例：服务层按 enabled 最新一条读写）"""
    __tablename__ = "sso_oidc_configs"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="配置ID")
    issuer = Column(String(500), nullable=False, comment="IdP issuer 基址（如 https://sso.corp.example）")
    client_id = Column(String(200), nullable=False, comment="OIDC client_id")
    client_secret = Column(String(500), nullable=False, comment="OIDC client_secret（读接口脱敏）")
    scopes = Column(String(300), nullable=False, default="openid profile email",
                    comment="授权 scopes（空格分隔，须含 openid）")
    # claims 映射：IdP userinfo 字段名 → 平台字段（标准 OIDC 字段为缺省）
    claim_email = Column(String(100), nullable=False, default="email", comment="email claim 字段名")
    claim_username = Column(String(100), nullable=False, default="preferred_username", comment="用户名 claim 字段名")
    claim_name = Column(String(100), nullable=False, default="name", comment="显示名 claim 字段名")
    claim_groups = Column(String(100), nullable=False, default="groups", comment="用户组 claim 字段名")
    # 完整回调地址（含 /api/v1/auth/oidc/callback）；留空按请求自动推导。
    # 反代部署（外部地址≠容器内地址）必须显式填写，IdP 侧要求精确匹配
    redirect_uri = Column(String(500), nullable=True, comment="回调地址（留空按请求推导）")
    enabled = Column(Boolean, nullable=False, default=True, comment="是否启用")
    updated_by = Column(String(64), nullable=True, comment="最后修改人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment="更新时间")


class SsoTeamMapping(Base):
    """IdP group → 平台团队映射（登录时按 id 升序取首个命中）"""
    __tablename__ = "sso_team_mappings"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="映射ID")
    idp_group = Column(String(200), nullable=False, unique=True, index=True,
                       comment="IdP groups claim 中的组名（精确匹配）")
    team_code = Column(String(100), nullable=False, comment="平台团队代码（team_code）")
    team_name = Column(String(200), nullable=True, comment="团队显示名（建团队时用，可空）")
    created_by = Column(String(64), nullable=True, comment="创建人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")


class LdapConfig(Base):
    """LDAP/AD 只读认证配置（单例：服务层按 enabled 最新一条读写）"""
    __tablename__ = "ldap_configs"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="配置ID")
    host = Column(String(300), nullable=False,
                  comment="LDAP 主机（裸主机名或 ldap:// ldaps:// 前缀，AD 如 dc01.corp.test）")
    port = Column(Integer, nullable=True, comment="端口（空=389/636 按 use_ssl 取默认）")
    use_ssl = Column(Boolean, nullable=False, default=False, comment="是否 LDAPS（SSL）")
    # 只读服务账号（search 用）：AD 必填；openldap 允许匿名读时留空
    bind_dn = Column(String(500), nullable=True, comment="只读服务账号 DN（search 用）")
    bind_password = Column(String(500), nullable=True, comment="服务账号密码（读接口脱敏）")
    base_dn = Column(String(500), nullable=False, comment="用户搜索基准 DN")
    user_filter = Column(String(500), nullable=False,
                         default="(&(objectClass=person)(sAMAccountName={username}))",
                         comment="用户搜索过滤器，{username} 占位（值做 RFC4515 转义）")
    # 属性映射（AD 缺省 sAMAccountName/mail/displayName/memberOf；openldap 常 uid/mail/cn/memberOf）
    attr_username = Column(String(100), nullable=False, default="sAMAccountName", comment="用户名属性")
    attr_email = Column(String(100), nullable=False, default="mail", comment="邮箱属性")
    attr_name = Column(String(100), nullable=False, default="displayName", comment="显示名属性")
    attr_groups = Column(String(100), nullable=False, default="memberOf", comment="用户组属性（组→团队映射用）")
    enabled = Column(Boolean, nullable=False, default=True, comment="是否启用")
    updated_by = Column(String(64), nullable=True, comment="最后修改人 user_code")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment="更新时间")
