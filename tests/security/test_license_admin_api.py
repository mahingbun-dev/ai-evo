"""M6 QE6-1 安全测试：超管 License 管理 API 的对外攻击面

新增对外攻击面 = license 上传通道（换 license 即换企业档位/规模上限——
被越权利用等于白嫖企业位或把生产降级）。红→绿：

1. 越权：匿名 401、普通用户 403（上传与状态两端点同口径）；
2. API Key 通道不可达：scope 注册表 default-deny（未登记路由对 API Key
   全量 403，静态 + 运行时双断言）——CI 凭证不开 license 面；
3. 超管上传：坏信封/篡改签名 400 且不落库；合法 license 200 即生效
   （status 回 enterprise）；超限文件 413；非 UTF-8 400；
4. 降级演练（服务端证据）：过期 license 上传后 status=expired 且
   effective_tier=oss——企业位锁死但服务不报 5xx（不阻断口径）；
5. 状态端点不泄漏 license 原文（只回 claims 概要与指纹）。
"""
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import scopes
from app.core.config import get_settings
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.audit_models import OperationLog
from app.models.license_models import PlatformLicense
from app.models.user_models import User
from app.services import jwt_service, license_service
from app.api import license_api

pytestmark = pytest.mark.unit  # W8 分类：sqlite 替身 + 临时密钥，无网络


@pytest.fixture(autouse=True)
def _no_api_key_throttle(monkeypatch):
    """隔离 API Key 认证失败节流：本机/共享 Redis 会跨测试累计失败计数
    （ip+prefix 5 次/10 分钟锁 15 分钟），多文件合跑时把 401 变成 429；
    替成 no-op 保证无效 Key 断言确定性（CI 无 Redis 本就 fail-open 不受影响）"""
    from app.services.api_key_throttle import ApiKeyAuthThrottle
    monkeypatch.setattr(ApiKeyAuthThrottle, "is_locked", lambda self, ip, prefix: False)
    monkeypatch.setattr(ApiKeyAuthThrottle, "record_failure", lambda self, ip, prefix: None)

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PY = REPO_ROOT / "tools" / "license" / "gen.py"
UPLOAD, STATUS = "/api/v1/admin/license", "/api/v1/admin/license/status"
_REPO_TEST_PUBKEY = "AmIfRV9ImNO5nDUAQJv8gM67qoc9wH5EUdVDiP5irAQ="


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


def _b64u(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.fixture()
def platform(monkeypatch):
    """挂 license 路由 + AuthMiddleware 的最小平台（sqlite 替身）"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, PlatformLicense.__table__,
                                              OperationLog.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    for mod in ("app.db.session", "app.middleware.auth_middleware",
                "app.services.audit_service"):
        monkeypatch.setattr(f"{mod}.SessionLocal", factory)

    db = factory()
    db.add(User(user_code="u_admin", username="admin", super_admin_flag=1, is_active=True))
    db.add(User(user_code="u_user", username="nobody", super_admin_flag=0, is_active=True))
    db.commit()

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(license_api.router, prefix="/api/v1")
    # 测试间隔离：临时密钥 + 清空进程内 license 缓存
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY",
                        _b64(private_key.public_key().public_bytes_raw()))
    license_service._state = None
    yield {
        "client": TestClient(app),
        "db": db,
        "private_key": private_key,
        "admin_token": jwt_service.create_token("u_admin", "admin"),
        "user_token": jwt_service.create_token("u_user", "nobody"),
    }
    license_service._state = None
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY", _REPO_TEST_PUBKEY)
    db.close()


def _envelope(private_key: Ed25519PrivateKey, claims: dict) -> str:
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return json.dumps({"alg": "Ed25519", "payload": _b64u(payload),
                       "sig": _b64u(private_key.sign(payload))})


def _claims(**overrides) -> dict:
    base = {
        "customer": "ACME 科技", "tier": "enterprise",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "features": ["audit_export"],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _upload(client, token, text: str, filename: str = "license.json"):
    return client.post(
        UPLOAD, headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, text.encode("utf-8"), "application/json")})


# ==================== 越权与通道 ====================

class TestLicenseAccessControl:
    def test_anonymous_401(self, platform):
        assert platform["client"].get(STATUS).status_code == 401
        assert platform["client"].post(UPLOAD).status_code == 401

    def test_non_admin_403(self, platform):
        headers = {"Authorization": f"Bearer {platform['user_token']}"}
        assert platform["client"].get(STATUS, headers=headers).status_code == 403
        resp = platform["client"].post(
            UPLOAD, headers=headers,
            files={"file": ("l.json", b"{}", "application/json")})
        assert resp.status_code == 403

    def test_api_key_channel_default_deny(self, platform):
        """API Key 通道：scope 注册表未登记 license 路由 → default-deny"""
        assert scopes.match_api_key_route("POST", "/api/v1/admin/license") is None
        assert scopes.match_api_key_route("GET", "/api/v1/admin/license/status") is None
        # 运行时：无效 Key 先被鉴权闸拦（401）；合法 Key 也进不了（注册表 403）
        resp = platform["client"].post(
            UPLOAD, headers={"Authorization": "Bearer aievo_sk_invalid_token"},
            files={"file": ("l.json", b"{}", "application/json")})
        assert resp.status_code == 401


# ==================== 上传面 ====================

class TestLicenseUpload:
    def test_admin_upload_valid(self, platform):
        resp = _upload(platform["client"], platform["admin_token"],
                       _envelope(platform["private_key"], _claims(max_users=50)))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "valid"
        assert body["effective_tier"] == "enterprise"
        assert body["enterprise"] is True
        assert body["max_users"] == 50

        status = platform["client"].get(
            STATUS, headers={"Authorization": f"Bearer {platform['admin_token']}"}).json()
        assert status["effective_tier"] == "enterprise"

        # 上传落审计（QE4-4 覆盖面静态断言的运行时证据）：detail 只含概要不含原文
        audit = platform["db"].query(OperationLog).filter(
            OperationLog.resource_type == "license").one()
        assert audit.action == "CREATE"
        assert audit.user_code == "u_admin"
        assert "license_text" not in str(audit.detail)
        assert audit.detail["tier"] == "enterprise"

    def test_tampered_rejected_400_no_row(self, platform):
        env = json.loads(_envelope(platform["private_key"], _claims()))
        claims = json.loads(_b64u_decode(env["payload"]))
        claims["tier"] = "enterprise"
        claims["max_users"] = 999999
        env["payload"] = _b64u(json.dumps(
            claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        resp = _upload(platform["client"], platform["admin_token"], json.dumps(env))
        assert resp.status_code == 400
        assert "验签失败" in resp.json()["detail"]
        assert platform["db"].query(PlatformLicense).count() == 0

    def test_garbage_rejected(self, platform):
        resp = _upload(platform["client"], platform["admin_token"], "not a license")
        assert resp.status_code == 400

    def test_non_utf8_rejected(self, platform):
        resp = platform["client"].post(
            UPLOAD, headers={"Authorization": f"Bearer {platform['admin_token']}"},
            files={"file": ("l.bin", b"\xff\xfe\x00bad", "application/octet-stream")})
        assert resp.status_code == 400
        assert "UTF-8" in resp.json()["detail"]

    def test_oversize_413(self, platform, monkeypatch):
        monkeypatch.setattr(get_settings(), "LICENSE_MAX_BYTES", 64)
        big = _envelope(platform["private_key"], _claims(customer="x" * 200))
        resp = _upload(platform["client"], platform["admin_token"], big + " " * 100)
        assert resp.status_code == 413

    def test_status_never_leaks_license_text(self, platform):
        text = _envelope(platform["private_key"], _claims())
        _upload(platform["client"], platform["admin_token"], text)
        status = platform["client"].get(
            STATUS, headers={"Authorization": f"Bearer {platform['admin_token']}"}).json()
        assert "payload" not in json.dumps(status)
        assert status["fingerprint"] and len(status["fingerprint"]) == 64


# ==================== 降级演练（服务端证据） ====================

class TestDegradationDrill:
    def test_expired_license_degrades_not_blocks(self, platform):
        """过期 license：上传成功（结构合法）但生效态=oss，服务无 5xx"""
        expired = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        resp = _upload(platform["client"], platform["admin_token"],
                       _envelope(platform["private_key"], _claims(expires_at=expired)))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "expired"
        assert body["tier"] == "enterprise"        # 授权档位留痕
        assert body["effective_tier"] == "oss"     # 生效档位降级
        assert body["enterprise"] is False
        assert "已过期" in body["message"]

    def test_no_license_is_plain_oss(self, platform):
        status = platform["client"].get(
            STATUS, headers={"Authorization": f"Bearer {platform['admin_token']}"}).json()
        assert status["status"] == "missing"
        assert status["effective_tier"] == "oss"
        assert status["enterprise"] is False

    def test_stored_license_recheck_after_key_rotation(self, platform, monkeypatch):
        """上传后换公钥（模拟厂商轮换/环境错配）→ 启动加载降级不抛错"""
        _upload(platform["client"], platform["admin_token"],
                _envelope(platform["private_key"], _claims()))
        monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY",
                            _b64(Ed25519PrivateKey.generate().public_key().public_bytes_raw()))
        state = license_service.load_from_db(platform["db"])
        assert state.status == "invalid_signature"
        assert state.effective_tier == "oss"

    def test_gen_tool_license_accepted(self, platform, monkeypatch, tmp_path):
        """工具（仓库测试密钥）签出的 license 能被端到端上传生效——
        部署密钥对齐后管理面闭环可用（此处临时把平台公钥对齐测试密钥）"""
        lic = tmp_path / "l.json"
        r = subprocess.run(
            [sys.executable, str(GEN_PY), "issue", "--customer", "工具客户",
             "--tier", "enterprise", "--days", "7", "--features", "audit_export",
             "--out", str(lic)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert r.returncode == 0, r.stderr
        monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY",
                            (REPO_ROOT / "tools/license/keys/test_signing_key.pub").read_text().strip())
        resp = _upload(platform["client"], platform["admin_token"], lic.read_text())
        assert resp.status_code == 200
        assert resp.json()["effective_tier"] == "enterprise"
