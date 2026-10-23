"""M6 QE6-1 单测：license_service 验签/过期/降级路径 + gen.py 工具链路

覆盖口径（方案 §M6 测试要求「license 篡改/过期/降级路径」）：
- 有效 license：enterprise 生效、features 解析、缓存刷新；
- 过期：验签通过但过界 → status=expired、effective_tier=oss（降级不抛错）；
- 篡改：payload/sig 改字节、换公钥、alg 字段缺失、非 JSON → LicenseError
  / status=invalid_signature 降级；
- 无 license：missing → oss（天然开源态，不算告警级降级）；
- claims 结构校验：tier 白名单/必填 customer/数值字段类型；
- tools/license/gen.py 子进程链路：keygen → issue → 平台侧验签通过
  （工具与平台两侧 canonical JSON 口径一致性的最硬证据）。
"""
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import get_settings
from app.services import license_service
from app.services.license_service import (
    LicenseError, STATUS_EXPIRED, STATUS_INVALID, STATUS_MISSING, STATUS_VALID,
    TIER_ENTERPRISE, TIER_OSS, install_license, verify_license_text,
)

pytestmark = pytest.mark.unit  # W8 分类：临时密钥 + sqlite 替身，无网络

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PY = REPO_ROOT / "tools" / "license" / "gen.py"

# 复用工具侧 canonical 编码（两侧口径一致是本文件的核心断言对象之一）
_spec = importlib.util.spec_from_file_location("license_gen_tool", GEN_PY)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def key_pair(monkeypatch):
    """临时 Ed25519 密钥对 + 注入 Settings（monkeypatch 统一撤销，避免与
    测试内 setattr 的 LIFO 顺序打架——手工还原会被后执行的 monkeypatch
    teardown 再覆盖回临时值）"""
    private_key = Ed25519PrivateKey.generate()
    pub_b64 = _b64(private_key.public_key().public_bytes_raw())
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY", pub_b64)
    yield private_key, pub_b64


def _envelope(private_key: Ed25519PrivateKey, claims: dict) -> str:
    payload = gen.canonical_claims(claims)
    return json.dumps({
        "alg": "Ed25519",
        "payload": gen.b64u_encode(payload),
        "sig": gen.b64u_encode(private_key.sign(payload)),
    })


def _claims(**overrides) -> dict:
    base = {
        "customer": "ACME 科技",
        "tier": "enterprise",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "features": ["audit_export", "report_white_label"],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _reset_cache():
    """测试间清空进程内缓存（license_service 模块级单例）"""
    license_service._state = None


@pytest.fixture(autouse=True)
def _isolate_cache():
    _reset_cache()
    yield
    _reset_cache()


# ==================== 验签内核 ====================

class TestVerifyLicenseText:
    def test_valid_enterprise(self, key_pair):
        private_key, _ = key_pair
        claims = verify_license_text(_envelope(private_key, _claims()))
        assert claims["tier"] == "enterprise"
        assert claims["features"] == ["audit_export", "report_white_label"]

    def test_tampered_payload_rejected(self, key_pair):
        """篡改 payload（签名不动）→ 验签失败"""
        private_key, _ = key_pair
        text = _envelope(private_key, _claims())
        env = json.loads(text)
        claims = json.loads(gen.b64u_decode(env["payload"]))
        claims["tier"] = "enterprise"
        claims["max_users"] = 99999  # 越权改规模
        env["payload"] = gen.b64u_encode(gen.canonical_claims(claims))
        with pytest.raises(LicenseError, match="验签失败"):
            verify_license_text(json.dumps(env))

    def test_foreign_key_rejected(self, key_pair):
        """他钥签发（伪造厂商）→ 验签失败"""
        private_key, _ = key_pair
        rogue = Ed25519PrivateKey.generate()
        with pytest.raises(LicenseError, match="验签失败"):
            verify_license_text(_envelope(rogue, _claims()))

    def test_malformed_envelope(self, key_pair):
        for bad in ("not json", "{}", '{"alg":"RS256","payload":"e30","sig":"e30"}',
                    '{"alg":"Ed25519","payload":"","sig":""}'):
            with pytest.raises(LicenseError):
                verify_license_text(bad)

    def test_claims_schema(self, key_pair):
        """tier 白名单 / customer 必填 / 数值字段类型闸"""
        private_key, _ = key_pair
        with pytest.raises(LicenseError, match="tier"):
            verify_license_text(_envelope(private_key, _claims(tier="ultimate")))
        with pytest.raises(LicenseError, match="customer"):
            verify_license_text(_envelope(private_key, _claims(customer="")))
        with pytest.raises(LicenseError, match="max_users"):
            verify_license_text(_envelope(private_key, _claims(max_users="50")))
        with pytest.raises(LicenseError, match="expires_at"):
            verify_license_text(_envelope(private_key, _claims(expires_at="明天")))

    def test_invalid_public_key_config(self, key_pair, monkeypatch):
        """LICENSE_PUBLIC_KEY 非法 → 明确报配置错（不误报篡改）"""
        private_key, _ = key_pair
        text = _envelope(private_key, _claims())
        monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY", "!!!not-a-key!!!")
        with pytest.raises(LicenseError, match="LICENSE_PUBLIC_KEY"):
            verify_license_text(text)


# ==================== 生效态判定（降级路径） ====================

class TestEvaluate:
    def _evaluate(self, key_pair, **overrides):
        private_key, _ = key_pair
        return license_service._evaluate(_envelope(private_key, _claims(**overrides)))

    def test_valid_enterprise_state(self, key_pair):
        state = self._evaluate(key_pair, max_users=50, max_monthly_runs=5000)
        assert state.status == STATUS_VALID
        assert state.effective_tier == TIER_ENTERPRISE
        assert state.max_users == 50 and state.max_monthly_runs == 5000
        assert state.customer == "ACME 科技"

    def test_expired_degrades_to_oss(self, key_pair):
        """过期：tier=enterprise 留痕但 effective_tier=oss，不抛错"""
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        state = self._evaluate(key_pair, expires_at=expired)
        assert state.status == STATUS_EXPIRED
        assert state.tier == TIER_ENTERPRISE
        assert state.effective_tier == TIER_OSS
        assert "已过期" in state.message

    def test_tampered_degrades_to_oss(self, key_pair):
        private_key, _ = key_pair
        env = json.loads(_envelope(private_key, _claims()))
        env["sig"] = gen.b64u_encode(b"\x00" * 8 + gen.b64u_decode(env["sig"])[8:])
        state = license_service._evaluate(json.dumps(env))
        assert state.status == STATUS_INVALID
        assert state.effective_tier == TIER_OSS
        assert "降级" in state.message

    def test_naive_datetime_expires_handled(self, key_pair):
        """无时区 expires_at（按 UTC 口径）也能判定，不抛异常"""
        expired = (datetime.now(timezone.utc).replace(tzinfo=None)
                   - timedelta(hours=1)).isoformat()
        state = self._evaluate(key_pair, expires_at=expired)
        assert state.status == STATUS_EXPIRED

    def test_missing_state_defaults_oss(self):
        """未初始化缓存：current_state 返回 oss 兜底，不抛错、不触发验签"""
        state = license_service.current_state()
        assert state.effective_tier == TIER_OSS
        assert license_service.effective_tier() == TIER_OSS
        assert license_service.has_feature("audit_export") is False

    def test_has_feature_requires_enterprise(self, key_pair):
        state = self._evaluate(key_pair)
        license_service._store(state)
        assert license_service.has_feature("audit_export") is True
        assert license_service.has_feature("scim") is False  # 未授权位


# ==================== 上传/缓存刷新（sqlite 替身） ====================

class TestInstallAndCache:
    @pytest.fixture()
    def db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from app.db.session import Base
        from app.models.license_models import PlatformLicense

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine, tables=[PlatformLicense.__table__])
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        session = factory()
        yield session
        session.close()

    def test_install_valid_refreshes_cache(self, key_pair, db):
        from app.models.license_models import PlatformLicense
        private_key, _ = key_pair
        state = install_license(db, _envelope(private_key, _claims()), uploaded_by="u_admin")
        assert state.status == STATUS_VALID
        assert license_service.current_state().effective_tier == TIER_ENTERPRISE
        row = db.query(PlatformLicense).one()
        assert row.uploaded_by == "u_admin"
        assert row.customer == "ACME 科技"

    def test_install_invalid_rejected_no_row(self, key_pair, db):
        """坏文件上传直接拒绝且不落库（管理员拿 400 的服务层依据）"""
        with pytest.raises(LicenseError):
            install_license(db, '{"alg":"Ed25519"}')
        from app.models.license_models import PlatformLicense
        assert db.query(PlatformLicense).count() == 0

    def test_load_from_db_latest_row_wins(self, key_pair, db):
        """最新一行为生效 license（重传覆盖语义）"""
        from app.models.license_models import PlatformLicense
        private_key, _ = key_pair
        # 旧行：enterprise；新行：oss tier
        db.add(PlatformLicense(license_text=_envelope(private_key, _claims()),
                               fingerprint="a" * 64, customer="old", tier="enterprise"))
        db.add(PlatformLicense(
            license_text=_envelope(private_key, _claims(tier="oss", customer="new", features=[])),
            fingerprint="b" * 64, customer="new", tier="oss"))
        db.commit()
        state = license_service.load_from_db(db)
        assert state.status == STATUS_VALID
        assert state.effective_tier == TIER_OSS
        assert state.customer == "new"

    def test_load_from_db_empty_is_oss_not_error(self, db):
        state = license_service.load_from_db(db)
        assert state.status == STATUS_MISSING
        assert state.effective_tier == TIER_OSS

    def test_load_from_db_never_raises(self, key_pair, db, monkeypatch):
        """存量坏 license（上传后换了公钥）→ 降级不抛错（不阻断启动）"""
        from app.models.license_models import PlatformLicense
        private_key, _ = key_pair
        db.add(PlatformLicense(license_text=_envelope(private_key, _claims()),
                               fingerprint="a" * 64))
        db.commit()
        # 模换公钥：验签上下文变化
        monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY", _b64(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()))
        state = license_service.load_from_db(db)
        assert state.status == STATUS_INVALID
        assert state.effective_tier == TIER_OSS


# ==================== gen.py 工具链路（子进程，两侧口径一致性） ====================

class TestGenTool:
    def test_keygen_issue_platform_verify(self, tmp_path, monkeypatch):
        """keygen → issue → 平台 verify_license_text 全链路通

        工具签出的 license 必须能被平台服务验签——两侧 canonical JSON 与
        base64url 口径一致的最硬证据（任一侧漂移即红）。
        """
        key_dir = tmp_path / "keys"
        r = subprocess.run(
            [sys.executable, str(GEN_PY), "keygen", "--out-dir", str(key_dir),
             "--name", "prod_like_key"],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert r.returncode == 0, r.stderr
        assert (key_dir / "prod_like_key").exists()

        lic = tmp_path / "acme.json"
        r = subprocess.run(
            [sys.executable, str(GEN_PY), "issue",
             "--customer", "ACME", "--tier", "enterprise",
             "--days", "30", "--max-users", "50", "--max-monthly-runs", "5000",
             "--features", "audit_export,report_white_label",
             "--key", str(key_dir / "prod_like_key"), "--out", str(lic)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert r.returncode == 0, r.stderr

        monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEY",
                            (key_dir / "prod_like_key.pub").read_text().strip())
        claims = verify_license_text(lic.read_text())
        assert claims["customer"] == "ACME"
        assert claims["tier"] == "enterprise"
        assert claims["max_users"] == 50 and claims["max_monthly_runs"] == 5000
        assert claims["features"] == ["audit_export", "report_white_label"]

    def test_verify_subcommand_rejects_tampered(self, tmp_path):
        """verify 子命令对篡改文件返回非 0（工具侧自检口径）"""
        key_dir = REPO_ROOT / "tools" / "license" / "keys"
        lic = tmp_path / "t.json"
        r = subprocess.run(
            [sys.executable, str(GEN_PY), "issue", "--customer", "T", "--tier", "oss",
             "--key", str(key_dir / "test_signing_key"), "--out", str(lic)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert r.returncode == 0, r.stderr
        env = json.loads(lic.read_text())
        payload = json.loads(gen.b64u_decode(env["payload"]))
        payload["tier"] = "enterprise"
        env["payload"] = gen.b64u_encode(gen.canonical_claims(payload))
        lic.write_text(json.dumps(env))

        r = subprocess.run(
            [sys.executable, str(GEN_PY), "verify", "--file", str(lic)],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert r.returncode == 1
        assert "验签失败" in r.stdout

    def test_repo_test_keypair_usable(self):
        """入库测试密钥对可加载且公钥与 Settings 缺省一致（防误轮换漂移）"""
        key_dir = REPO_ROOT / "tools" / "license" / "keys"
        private_key = gen._load_private_key(key_dir / "test_signing_key")
        pub = _b64(private_key.public_key().public_bytes_raw())
        assert pub == get_settings().LICENSE_PUBLIC_KEY, (
            "仓库测试公钥与 Settings.LICENSE_PUBLIC_KEY 缺省值不一致——"
            "若确要轮换测试密钥，须同步 app/core/config.py 缺省值")
