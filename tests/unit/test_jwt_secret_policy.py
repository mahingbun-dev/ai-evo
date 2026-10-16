"""JWT 密钥独立化策略单测（ARCH-0-2，设计决策 D6：去 DATABASE_URL 派生）

覆盖：
- 企业部署档（默认）：AUTH_JWT_SECRET 缺失 → validate_runtime_settings
  启动 fail-fast，报错含 openssl 生成示例；auth_jwt_secret 取值同样抛错
  （覆盖不经应用 lifespan 的进程，如 celery worker）
- 本地开发档（显式开 AUTH_JWT_ALLOW_DERIVED）：派生回退可用、稳定、
  随 DATABASE_URL 变化；validate 放行且启动日志告警留痕
- 显式配置优先：配置了密钥时派生开关不产生任何影响

红→绿：改造前 auth_jwt_secret 无条件从 DATABASE_URL 派生（config.py 旧
auth_jwt_secret property），「企业档缺密钥抛错」「取值抛错」两组断言必红。
"""
import hashlib
import logging

import pytest

from app.core.config import Settings, validate_runtime_settings

pytestmark = pytest.mark.unit  # W8 分类门禁


def _settings(**overrides) -> Settings:
    """密闭构造：_env_file=None 隔离所在机器 .env，init kwargs 覆盖环境变量"""
    base = dict(
        _env_file=None,
        DATABASE_URL="postgresql://arch02:arch02@127.0.0.1:5432/arch02",
        AUTH_JWT_SECRET="",
        AUTH_JWT_ALLOW_DERIVED=False,
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 企业部署档（默认）：缺密钥 fail-fast
# ---------------------------------------------------------------------------

class TestEnterpriseFailFast:
    def test_missing_secret_rejected_at_startup(self):
        """缺 AUTH_JWT_SECRET 且未开派生开关 → 启动校验抛 RuntimeError"""
        with pytest.raises(RuntimeError) as exc:
            validate_runtime_settings(_settings())
        assert "AUTH_JWT_SECRET" in str(exc.value)

    def test_error_message_contains_openssl_example(self):
        """报错必须给出可复制的密钥生成示例（W6-3「报错即修复指引」风格）"""
        with pytest.raises(RuntimeError) as exc:
            validate_runtime_settings(_settings())
        assert "openssl rand -hex 32" in str(exc.value)
        # 指明禁止派生的原因与本地开发的出路
        assert "DATABASE_URL" in str(exc.value)
        assert "AUTH_JWT_ALLOW_DERIVED" in str(exc.value)

    def test_property_raises_without_secret_and_switch(self):
        """取值入口同样 fail-fast（覆盖不经 lifespan 的进程）"""
        with pytest.raises(RuntimeError) as exc:
            _settings().auth_jwt_secret
        assert "openssl rand -hex 32" in str(exc.value)

    def test_whitespace_secret_treated_as_missing(self):
        """纯空白密钥视同未配置（防止 .env 里只剩空格的假配置）"""
        with pytest.raises(RuntimeError):
            validate_runtime_settings(_settings(AUTH_JWT_SECRET="   "))

    def test_explicit_secret_passes_validation(self, caplog):
        """显式配置随机密钥 → 校验放行且无告警"""
        with caplog.at_level(logging.WARNING, logger="app.core.config"):
            validate_runtime_settings(
                _settings(AUTH_JWT_SECRET="a" * 64))
        assert "AUTH_JWT_SECRET" not in caplog.text

    def test_allow_derived_defaults_to_false(self):
        """派生开关默认关闭（企业部署档的默认语义），用字段默认值断言
        ——不受所在机器环境变量影响"""
        assert Settings.model_fields["AUTH_JWT_ALLOW_DERIVED"].default is False


# ---------------------------------------------------------------------------
# 本地开发档：显式开 AUTH_JWT_ALLOW_DERIVED，保留派生 + 启动告警
# ---------------------------------------------------------------------------

class TestLocalDevDerivation:
    def _local(self, dsn="postgresql://arch02:arch02@127.0.0.1:5432/arch02"):
        return _settings(
            DATABASE_URL=dsn, AUTH_JWT_SECRET="", AUTH_JWT_ALLOW_DERIVED=True)

    def test_derivation_allowed_passes_with_startup_warning(self, caplog):
        """本地档放行启动，但启动日志必须告警留痕"""
        with caplog.at_level(logging.WARNING, logger="app.core.config"):
            validate_runtime_settings(self._local())  # 不抛即放行
        assert "AUTH_JWT_SECRET" in caplog.text
        assert "openssl rand -hex 32" in caplog.text

    def test_derived_secret_is_stable_and_bound_to_dsn(self):
        """派生密钥稳定、绑定 DATABASE_URL（同 DSN 同值、换 DSN 即换钥）"""
        s1, s2 = self._local(), self._local()
        assert s1.auth_jwt_secret == s2.auth_jwt_secret
        expected = hashlib.sha256(
            f"ai-evo:{s1.DATABASE_URL}".encode()).hexdigest()
        assert s1.auth_jwt_secret == expected
        assert self._local("postgresql://other@127.0.0.1/other").auth_jwt_secret \
            != s1.auth_jwt_secret

    def test_derived_secret_usable_for_token_roundtrip(self, monkeypatch):
        """本地档派生密钥可正常签发/校验 JWT（登录功能不受影响）"""
        from app.services import jwt_service
        monkeypatch.setattr(jwt_service, "get_settings", lambda: self._local())
        token = jwt_service.create_token("u_arch02", "dev")
        payload = jwt_service.decode_token(token)
        assert payload is not None
        assert payload["sub"] == "u_arch02"

    def test_explicit_secret_wins_over_derivation(self):
        """显式配置优先：开了派生开关也不覆盖显式密钥"""
        s = _settings(AUTH_JWT_SECRET="explicit-secret",
                      AUTH_JWT_ALLOW_DERIVED=True)
        assert s.auth_jwt_secret == "explicit-secret"
