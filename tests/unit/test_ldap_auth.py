"""LDAP/AD 只读认证单元测试（M4 QE4-2）：内嵌 mock 目录全流程

按方案 §4.5「openldap 容器或内嵌 mock」二选一取内嵌路线：FakeLdapConn
复刻 ldap_service 用到的 ldap3 连接面（bind/search/entries/unbind），
内置一个 AD 形态目录（服务账号 + 两个用户 + memberOf 组），替换
ldap_service._make_connection 注入缝——零真实网络、零容器。

目录装配（FakeDirectory）：
- 服务账号 CN=svc-aievo,...（密码 svc-secret）——search 用只读账号
- 用户 alice（sAMAccountName=alice / mail / displayName / memberOf 两个组）
- 用户 bob（无 mail 属性——占位邮箱路径）
- 用户 off（密码正确但目录里禁用标记由平台侧账号状态表达，见用例）

覆盖：
- 认证全流程：search+bind → JIT 开户（email/姓名/组）→ 签发 JWT →
  与 OIDC 共用的 sso_team_mappings 归属（memberOf 首 RDN 与完整 DN 均可配）
- 登录旁路优先级：本地有密码账号不旁路（错密码不落 LDAP）、
  未知账号旁路 LDAP、二次登录不重复开户、email 映射既有账号
- 配置面：脱敏回显/保留语义、自检（服务账号绑定+base 搜索）、审计落痕
- 开关：AUTH_METHODS=ldap（无 local）时本地注册 403、LDAP 登录可用；
  未含 ldap 时旁路不生效
- 滤值转义（escape_filter_value）单元与目录侧注入面（详见 tests/security）
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.sso_models import SsoOidcConfig, SsoTeamMapping, LdapConfig
from app.models.team_models import Team
from app.models.user_models import User
from app.services import jwt_service, ldap_service, sso_service
from app.api import sso_api, user_api

pytestmark = pytest.mark.unit  # W8 分类门禁：全部替身（内嵌 mock 目录 + sqlite）

LDAP_HOST = "ldap.corp.test"
SVC_DN = "CN=svc-aievo,OU=Service,DC=corp,DC=test"
SVC_PWD = "svc-secret-123456"
BASE_DN = "OU=Users,DC=corp,DC=test"


# ==================== 内嵌 mock 目录 ====================

class FakeEntry:
    """ldap3.Entry 替身：entry_dn + entry_attributes_as_dict"""

    def __init__(self, dn: str, attrs: dict):
        self.entry_dn = dn
        self.entry_attributes_as_dict = {
            k: list(v) for k, v in attrs.items() if v
        }


class FakeDirectory:
    """AD 形态内存目录：DN 表 + 密码表；search 按 (attr=value) 对匹配"""

    def __init__(self):
        # DN → 属性 dict
        self.entries: dict = {}
        # DN → 密码（bind 校验）
        self.passwords: dict = {}
        # 服务账号匿名读场景：bind_dn 为 None 时 bind 视为成功
        self.anonymous_ok = True
        self.search_calls: list = []

    def add_user(self, dn, password, **attrs):
        self.entries[dn] = attrs
        self.passwords[dn] = password

    def make_connection(self, config=None, bind_dn=None, password=None):
        """与 ldap_service._make_connection 同参同回退语义：
        bind_dn/password 未显式给出时回退 config.bind_dn/bind_password"""
        user = bind_dn if bind_dn is not None else (getattr(config, "bind_dn", None) or None)
        pwd = password if password is not None else (getattr(config, "bind_password", None) or None)
        return FakeLdapConn(self, user, pwd)


class FakeLdapConn:
    """ldap3.Connection 替身：仅实现 ldap_service 用到的面"""

    def __init__(self, directory: FakeDirectory, bind_dn=None, password=None):
        self._dir = directory
        self._bind_dn = bind_dn
        self._password = password
        self.entries = []
        self.bound = False

    def bind(self) -> bool:
        if self._bind_dn is None:
            self.bound = self._dir.anonymous_ok  # 匿名绑定
            return self.bound
        pwd = self._dir.passwords.get(self._bind_dn)
        self.bound = pwd is not None and pwd == (self._password or "")
        return self.bound

    def search(self, base, filter_str, attributes=None, search_scope=None) -> bool:
        self._dir.search_calls.append((base, filter_str, search_scope))
        if not base.endswith("DC=corp,DC=test") and "DC=corp" in base:
            return False  # base_dn 错配形态
        # 解析 filter 里所有 (attr=value) 对，逐条目做包含匹配
        import re as _re
        pairs = _re.findall(r"\(([^()=]+)=([^()]*)\)", filter_str or "")
        matched = []
        for dn, attrs in self._dir.entries.items():
            if base and not dn.endswith(base.split(",", 1)[-1]) and base not in dn:
                # 简化 scope 判断：DN 需在 base 下（mock 目录全部同树，直接放行）
                pass
            ok = True
            for attr, value in pairs:
                values = [str(v).lower() for v in attrs.get(attr, [])]
                if value.lower() not in values:
                    ok = False
                    break
            if ok:
                matched.append(FakeEntry(dn, attrs))
        self.entries = matched
        return True

    def unbind(self):
        return True


def _directory():
    d = FakeDirectory()
    d.add_user(SVC_DN, SVC_PWD, objectClass=["user"], cn=["svc-aievo"])
    d.add_user(
        "CN=Alice Zhang,OU=Users,DC=corp,DC=test", "alice-pwd-123",
        objectClass=["person", "user"], sAMAccountName=["alice"],
        mail=["Alice@Corp.Test"], displayName=["张三"],
        memberOf=["CN=qe-engineers,OU=Groups,DC=corp,DC=test",
                  "CN=everyone,OU=Groups,DC=corp,DC=test"],
    )
    d.add_user(
        "CN=Bob Li,OU=Users,DC=corp,DC=test", "bob-pwd-123",
        objectClass=["person", "user"], sAMAccountName=["bob"],
        displayName=["Bob Li"],
        memberOf=[],
    )
    return d


# ==================== 平台装配件 ====================

def _settings_stub(monkeypatch, auth_methods="local,ldap"):
    from app.core.config import Settings

    stub = Settings(
        _env_file=None, DATABASE_URL="postgresql://unit-test",
        AUTH_JWT_SECRET="unit-test-jwt-secret", AUTH_METHODS=auth_methods,
    )
    # app.core.config.get_settings 覆盖 user_api 等函数内 from-import 取值；
    # 顶层绑定名字的模块逐一替换（user_api 无顶层绑定）
    monkeypatch.setattr("app.core.config.get_settings", lambda: stub)
    for mod in (jwt_service, sso_service, sso_api):
        monkeypatch.setattr(mod, "get_settings", lambda: stub)
    return stub


@pytest.fixture()
def ldap(monkeypatch):
    """平台 app + sqlite 内存库 + mock 目录注入缝"""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Team.__table__, SsoOidcConfig.__table__,
        SsoTeamMapping.__table__, LdapConfig.__table__, OperationLog.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.api.user_api", "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    directory = _directory()
    monkeypatch.setattr(ldap_service, "_make_connection", directory.make_connection)

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(user_api.auth_router, prefix="/api/v1")
    app.include_router(sso_api.router, prefix="/api/v1")
    return {
        "client": TestClient(app, follow_redirects=False),
        "db": factory,
        "dir": directory,
    }


def _seed_ldap_config(db, **overrides):
    row = LdapConfig(
        host=LDAP_HOST, bind_dn=SVC_DN, bind_password=SVC_PWD,
        base_dn=BASE_DN,
        user_filter="(&(objectClass=person)(sAMAccountName={username}))",
        enabled=True,
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    db.add(row)
    db.commit()
    return row


def _login(client, username, password):
    return client.post("/api/v1/auth/login",
                       json={"username": username, "password": password})


# ==================== 认证全流程（search+bind → JIT → JWT） ====================

class TestLdapLoginFlow:
    def test_first_login_jit_provisions_user(self, ldap, monkeypatch):
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.add(SsoTeamMapping(idp_group="qe-engineers", team_code="QE-TEAM",
                              team_name="质量工程组"))
        db.commit()

        resp = _login(ldap["client"], "alice", "alice-pwd-123")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["token"]

        # JIT：email 归一、无本地密码、姓名/组映射（memberOf 首 RDN 命中映射）
        user = db.query(User).filter(User.email == "alice@corp.test").first()
        assert user is not None
        assert user.password_hash is None
        assert user.user_name == "张三"
        assert user.username == "alice"
        assert user.org_code == "QE-TEAM"
        team = db.get(Team, "QE-TEAM")
        assert team is not None and team.team_name == "质量工程组"

        # JWT 可用（走一次受保护校验）
        payload = jwt_service.decode_token(body["token"])
        assert payload["sub"] == user.user_code

        # 审计：JIT 开户 CREATE（via ldap）
        log = db.query(OperationLog).filter(
            OperationLog.resource_type == "user").first()
        assert log is not None and log.action == "CREATE"
        assert log.detail.get("via") == "ldap"

    def test_mapping_matches_full_memberof_dn(self, ldap, monkeypatch):
        """映射表配完整组 DN 也能命中（两种管理员习惯都支持）"""
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.add(SsoTeamMapping(
            idp_group="cn=qe-engineers,ou=groups,dc=corp,dc=test",
            team_code="QE-FULL-DN"))
        db.commit()

        resp = _login(ldap["client"], "alice", "alice-pwd-123")
        assert resp.status_code == 200
        user = db.query(User).filter(User.email == "alice@corp.test").first()
        assert user.org_code == "QE-FULL-DN"

    def test_second_login_no_duplicate_and_login_audit(self, ldap, monkeypatch):
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.commit()
        assert _login(ldap["client"], "alice", "alice-pwd-123").status_code == 200
        assert _login(ldap["client"], "alice", "alice-pwd-123").status_code == 200
        assert db.query(User).filter(User.email == "alice@corp.test").count() == 1
        log = db.query(OperationLog).filter(
            OperationLog.resource_type == "ldap",
            OperationLog.action == "LOGIN").first()
        assert log is not None  # 二次登录记 LOGIN（非重复 CREATE）

    def test_no_mail_attribute_synthesizes_placeholder(self, ldap, monkeypatch):
        """目录无 mail 属性：确定性占位邮箱（.invalid 域），JIT 语义仍成立"""
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.commit()

        resp = _login(ldap["client"], "bob", "bob-pwd-123")
        assert resp.status_code == 200
        user = db.query(User).filter(User.username == "bob").first()
        assert user.email == "bob@ldap.invalid"

    def test_maps_existing_account_by_email(self, ldap, monkeypatch):
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.add(User(user_code="u_exist", username="alice_local", user_name="旧名",
                    email="alice@corp.test", super_admin_flag=0, is_active=True))
        db.commit()

        assert _login(ldap["client"], "alice", "alice-pwd-123").status_code == 200
        users = db.query(User).filter(User.email == "alice@corp.test").all()
        assert len(users) == 1 and users[0].user_code == "u_exist"
        assert users[0].user_name == "张三"  # 显示名按目录刷新

    def test_disabled_existing_account_403(self, ldap, monkeypatch):
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.add(User(user_code="u_off", username="off", email="alice@corp.test",
                    is_active=False))
        db.commit()
        resp = _login(ldap["client"], "alice", "alice-pwd-123")
        assert resp.status_code == 403


# ==================== 旁路优先级 ====================

class TestFallbackOrdering:
    def test_local_password_account_never_falls_through(self, ldap, monkeypatch):
        """本地有密码的账号不旁路：错密码直接 401（LDAP 同名账号不构成证明）"""
        _settings_stub(monkeypatch, auth_methods="local,ldap")
        db = ldap["db"]()
        from app.services.password_service import hash_password
        db.add(User(user_code="u_local", username="alice",
                    password_hash=hash_password("local-pwd"), email="local@corp.test",
                    is_active=True))
        db.commit()

        # LDAP 密码对本地账号无效
        resp = _login(ldap["client"], "alice", "alice-pwd-123")
        assert resp.status_code == 401
        # 本地密码正确
        assert _login(ldap["client"], "alice", "local-pwd").status_code == 200
        # 未走目录（无 LDAP 认证审计）
        assert db.query(OperationLog).filter(
            OperationLog.resource_type == "ldap").count() == 0

    def test_ldap_login_without_local_config(self, ldap, monkeypatch):
        """未配置 LDAP：本地不存在账号 → 常规 401（旁路静默不可用）"""
        _settings_stub(monkeypatch, auth_methods="local,ldap")
        db = ldap["db"]()
        db.commit()
        resp = _login(ldap["client"], "alice", "alice-pwd-123")
        assert resp.status_code == 401

    def test_wrong_ldap_password_401(self, ldap, monkeypatch):
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.commit()
        assert _login(ldap["client"], "alice", "wrong").status_code == 401
        assert _login(ldap["client"], "ghost", "whatever").status_code == 401

    def test_ldap_only_mode_blocks_register_keeps_login(self, ldap, monkeypatch):
        """AUTH_METHODS=ldap：注册关闭（403），账密表单仅走目录旁路"""
        _settings_stub(monkeypatch, auth_methods="ldap")
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.commit()

        reg = ldap["client"].post("/api/v1/auth/register",
                                  json={"username": "newbie", "password": "secret1"})
        assert reg.status_code == 403
        # 本地直登不可用（无本地账号时旁路目录）
        assert _login(ldap["client"], "alice", "alice-pwd-123").status_code == 200

    def test_methods_endpoint_reports_ldap(self, ldap, monkeypatch):
        _settings_stub(monkeypatch)
        db = ldap["db"]()
        _seed_ldap_config(db)
        db.commit()
        body = ldap["client"].get("/api/v1/auth/methods").json()
        # methods=local,ldap：注册随 local 开（ldap_only 场景 register 才关）
        assert body == {
            "methods": ["ldap", "local"], "register_enabled": True,
            "oidc_configured": False, "ldap_configured": True,
        }


# ==================== 滤值转义单元 ====================

class TestEscapeFilterValue:
    def test_exact_escape_table(self):
        esc = ldap_service.escape_filter_value
        assert esc("a*b") == "a\\2ab"
        assert esc("a(b") == "a\\28b"
        assert esc("a)b") == "a\\29b"
        assert esc("a\\b") == "a\\5cb"
        assert esc("a\x00b") == "a\\00b"
        assert esc("张三") == "张三"  # 普通字符原样（含中文姓名场景）
        assert esc("") == ""


# ==================== 超管配置面 ====================

def _admin_client(ldap, monkeypatch):
    _settings_stub(monkeypatch)
    db = ldap["db"]()
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1,
                is_active=True))
    db.commit()
    token = jwt_service.create_token("u_admin", "admin")
    ldap["client"].headers.update({"Authorization": f"Bearer {token}"})
    return db


class TestLdapConfigApi:
    def test_get_config_masked(self, ldap, monkeypatch):
        db = _admin_client(ldap, monkeypatch)
        _seed_ldap_config(db)
        body = ldap["client"].get("/api/v1/auth/ldap/config").json()
        assert body["configured"] is True
        assert "****" in body["config"]["bind_password"]
        assert SVC_PWD not in json.dumps(body)

    def test_put_config_save_keeps_password_and_audits(self, ldap, monkeypatch):
        db = _admin_client(ldap, monkeypatch)
        _seed_ldap_config(db)
        resp = ldap["client"].put("/api/v1/auth/ldap/config", json={
            "host": LDAP_HOST, "bind_dn": SVC_DN, "bind_password": "svc-****3456",
            "base_dn": BASE_DN,
            "user_filter": "(&(objectClass=person)(sAMAccountName={username}))",
            "enabled": True,
        })
        assert resp.status_code == 200, resp.text
        row = ldap_service.get_active_config(db)
        assert row.bind_password == SVC_PWD  # 脱敏回显值 → 保留原值
        log = db.query(OperationLog).filter(
            OperationLog.resource_type == "ldap_config").first()
        assert log is not None and log.action == "UPDATE"
        assert SVC_PWD not in json.dumps(log.detail)

    def test_put_config_validates(self, ldap, monkeypatch):
        _admin_client(ldap, monkeypatch)
        # 缺 {username} 占位
        resp = ldap["client"].put("/api/v1/auth/ldap/config", json={
            "host": LDAP_HOST, "base_dn": BASE_DN,
            "user_filter": "(objectClass=person)", "enabled": True,
        })
        assert resp.status_code == 400 and "{username}" in resp.json()["detail"]
        # 非 ldap scheme
        resp2 = ldap["client"].put("/api/v1/auth/ldap/config", json={
            "host": "http://dc01", "base_dn": BASE_DN, "enabled": True,
        })
        assert resp2.status_code == 400

    def test_connectivity_test_ok(self, ldap, monkeypatch):
        _admin_client(ldap, monkeypatch)
        resp = ldap["client"].post("/api/v1/auth/ldap/test", json={
            "host": LDAP_HOST, "bind_dn": SVC_DN, "bind_password": SVC_PWD,
            "base_dn": BASE_DN, "enabled": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["bind_ok"] is True and body["base_dn_ok"] is True
        assert body["latency_ms"] >= 0

    def test_connectivity_test_bad_credentials(self, ldap, monkeypatch):
        _admin_client(ldap, monkeypatch)
        resp = ldap["client"].post("/api/v1/auth/ldap/test", json={
            "host": LDAP_HOST, "bind_dn": SVC_DN, "bind_password": "wrong",
            "base_dn": BASE_DN, "enabled": True,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is False and "绑定失败" in resp.json()["error"]
