"""
DeepSeek Harness 全局配置服务单元测试

覆盖：api_key 脱敏、SDK 环境变量构造、配置读取缓存与失效、
保存逻辑（脱敏值不覆盖原 api_key）、脱敏视图。
"""
from unittest.mock import MagicMock

import pytest

from app.services.deepseek_harness_config_service import DeepSeekHarnessConfigService

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


SAMPLE_CONFIG = {
    "id": "dsh_test000001",
    "name": "默认配置",
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-abcdefghijklmnop",
    "provider": "deepseek-official",
    "model": "deepseek-v4-flash",
    "profile": "sdk-minimal",
    "timeout_seconds": 300,
    "max_tokens": 49152,
    "extra_env": {"DSH_CUSTOM_FLAG": "1"},
    "extra_patches": [],
}


@pytest.fixture(autouse=True)
def reset_cache():
    """每个用例前后清空配置缓存，避免用例间串扰"""
    DeepSeekHarnessConfigService.invalidate_cache()
    yield
    DeepSeekHarnessConfigService.invalidate_cache()


class TestMaskToken:
    """脱敏复用 claude_code_config_service.mask_token（前 4 后 4）"""

    def test_mask_format(self):
        from app.services.claude_code_config_service import mask_token

        assert mask_token("sk-abcdefghijklmnop") == "sk-a****mnop"

    def test_masked_view(self):
        masked = DeepSeekHarnessConfigService.masked(dict(SAMPLE_CONFIG))
        assert masked["api_key"] == "sk-a****mnop"
        assert masked["model"] == "deepseek-v4-flash"  # 其他字段原样

    def test_masked_none(self):
        assert DeepSeekHarnessConfigService.masked(None) is None

    def test_masked_empty_key(self):
        masked = DeepSeekHarnessConfigService.masked(dict(SAMPLE_CONFIG, api_key=""))
        assert masked["api_key"] == "****"


class TestGetActiveConfig:
    def _patch_db(self, monkeypatch, row):
        fake_session = MagicMock()
        fake_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = row
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        monkeypatch.setattr(
            "app.services.deepseek_harness_config_service.SessionLocal",
            MagicMock(return_value=fake_session),
        )
        return fake_session

    @staticmethod
    def _make_row():
        row = MagicMock()
        row.id = "dsh_test000001"
        row.name = "默认配置"
        row.base_url = "https://api.deepseek.com"
        row.api_key = "sk-real-key-123456"
        row.provider = "deepseek-official"
        row.model = "deepseek-v4-flash"
        row.profile = "sdk-minimal"
        row.timeout_seconds = 300
        row.max_tokens = 49152
        row.extra_env = {"DSH_CUSTOM_FLAG": "1"}
        row.extra_patches = ["extra.patch.yml"]
        return row

    def test_read_config(self, monkeypatch):
        self._patch_db(monkeypatch, self._make_row())
        config = DeepSeekHarnessConfigService.get_active_config()
        assert config["api_key"] == "sk-real-key-123456"
        assert config["profile"] == "sdk-minimal"
        assert config["extra_patches"] == ["extra.patch.yml"]

    def test_defaults_filling(self, monkeypatch):
        row = self._make_row()
        row.profile = None
        row.timeout_seconds = None
        row.max_tokens = None
        row.extra_env = None
        row.extra_patches = None
        self._patch_db(monkeypatch, row)
        config = DeepSeekHarnessConfigService.get_active_config()
        assert config["profile"] == "sdk-minimal"
        assert config["timeout_seconds"] == 300
        assert config["max_tokens"] == 49152
        assert config["extra_env"] == {}
        assert config["extra_patches"] == []

    def test_no_config_returns_none_and_not_cached(self, monkeypatch):
        fake_session = self._patch_db(monkeypatch, None)
        assert DeepSeekHarnessConfigService.get_active_config() is None
        assert fake_session.query.call_count == 1
        # None 不缓存，再次调用仍查库
        assert DeepSeekHarnessConfigService.get_active_config() is None
        assert fake_session.query.call_count == 2

    def test_cache_hit_avoids_db(self, monkeypatch):
        fake_session = self._patch_db(monkeypatch, self._make_row())
        DeepSeekHarnessConfigService.get_active_config()
        DeepSeekHarnessConfigService.get_active_config()
        assert fake_session.query.call_count == 1  # 第二次命中缓存

    def test_invalidate_cache(self, monkeypatch):
        fake_session = self._patch_db(monkeypatch, self._make_row())
        DeepSeekHarnessConfigService.get_active_config()
        DeepSeekHarnessConfigService.invalidate_cache()
        DeepSeekHarnessConfigService.get_active_config()
        assert fake_session.query.call_count == 2

    def test_db_error_returns_none(self, monkeypatch):
        fake_session = MagicMock()
        fake_session.query.side_effect = RuntimeError("db down")
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        monkeypatch.setattr(
            "app.services.deepseek_harness_config_service.SessionLocal",
            MagicMock(return_value=fake_session),
        )
        assert DeepSeekHarnessConfigService.get_active_config() is None


class TestGetSdkEnv:
    def _patch_active(self, monkeypatch, config):
        monkeypatch.setattr(
            DeepSeekHarnessConfigService, "get_active_config", classmethod(lambda cls: config)
        )

    def test_env_from_extra_env(self, monkeypatch):
        self._patch_active(monkeypatch, dict(SAMPLE_CONFIG))
        env = DeepSeekHarnessConfigService.get_sdk_env()
        assert env == {"DSH_CUSTOM_FLAG": "1"}

    def test_env_values_stringified(self, monkeypatch):
        config = dict(SAMPLE_CONFIG, extra_env={"A": 1, "B": True})
        self._patch_active(monkeypatch, config)
        env = DeepSeekHarnessConfigService.get_sdk_env()
        assert env == {"A": "1", "B": "True"}

    def test_no_config_empty(self, monkeypatch):
        self._patch_active(monkeypatch, None)
        assert DeepSeekHarnessConfigService.get_sdk_env() == {}


class TestSaveConfigApiKeyHandling:
    """保存配置时 api_key 处理：脱敏回传值不覆盖原 api_key（与 CC 配置同规则）"""

    def _patch_db(self, monkeypatch, existing_row):
        fake_session = MagicMock()
        query_chain = fake_session.query.return_value.filter.return_value.order_by.return_value
        query_chain.first.return_value = existing_row
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        # save_config 末尾 get_active_config(use_cache=False) 会再开一个 session
        monkeypatch.setattr(
            "app.services.deepseek_harness_config_service.SessionLocal",
            MagicMock(return_value=fake_session),
        )
        return fake_session

    @staticmethod
    def _existing_row(api_key="sk-real-key-123456"):
        row = MagicMock()
        row.api_key = api_key
        return row

    def test_masked_key_unchanged_keeps_original(self, monkeypatch):
        row = self._existing_row("sk-real-key-123456")
        self._patch_db(monkeypatch, row)
        monkeypatch.setattr(
            DeepSeekHarnessConfigService, "get_active_config", classmethod(lambda cls, use_cache=True: {})
        )
        DeepSeekHarnessConfigService.save_config({
            "name": "默认配置",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-r****3456",  # mask_token 的输出格式
            "model": "deepseek-v4-flash",
        })
        assert row.api_key == "sk-real-key-123456"

    def test_short_masked_key_keeps_original(self, monkeypatch):
        row = self._existing_row("short")
        self._patch_db(monkeypatch, row)
        monkeypatch.setattr(
            DeepSeekHarnessConfigService, "get_active_config", classmethod(lambda cls, use_cache=True: {})
        )
        DeepSeekHarnessConfigService.save_config({"base_url": "https://x", "api_key": "****", "model": "m"})
        assert row.api_key == "short"

    def test_new_key_updates(self, monkeypatch):
        row = self._existing_row("old-key-value99")
        self._patch_db(monkeypatch, row)
        monkeypatch.setattr(
            DeepSeekHarnessConfigService, "get_active_config", classmethod(lambda cls, use_cache=True: {})
        )
        DeepSeekHarnessConfigService.save_config({"base_url": "https://x", "api_key": "brand-new-key111", "model": "m"})
        assert row.api_key == "brand-new-key111"

    def test_defaults_applied(self, monkeypatch):
        row = self._existing_row("sk-real")
        self._patch_db(monkeypatch, row)
        monkeypatch.setattr(
            DeepSeekHarnessConfigService, "get_active_config", classmethod(lambda cls, use_cache=True: {})
        )
        DeepSeekHarnessConfigService.save_config({"model": "deepseek-v4-flash"})
        assert row.provider == "deepseek-official"
        assert row.profile == "sdk-minimal"
        assert row.timeout_seconds == 300
        assert row.max_tokens == 49152
        assert row.extra_env == {}
        assert row.extra_patches == []
        assert row.base_url is None  # 空值落 None


class TestSaveConfigProtocol:
    """protocol 字段持久化与校验（支持非 DeepSeek 协议网关）"""

    def test_save_persists_protocol(self, monkeypatch):
        saved = {}
        monkeypatch.setattr(
            DeepSeekHarnessConfigService, "get_active_config",
            classmethod(lambda cls, use_cache=True: saved),
        )
        # 直连 sqlite 内存库跑真实 upsert
        import sqlalchemy as sa
        from app.db.session import Base
        from app.models.deepseek_harness_models import DeepSeekHarnessConfig
        engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[DeepSeekHarnessConfig.__table__])
        SessionLocal = sa.orm.sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.deepseek_harness_config_service.SessionLocal", SessionLocal)
        DeepSeekHarnessConfigService.save_config({
            "provider": "custom", "model": "deepseek-flash",
            "base_url": "https://gw.example/api-sse-anthropic", "api_key": "sk-x",
            "protocol": "anthropic",
        })
        with SessionLocal() as db:
            row = db.query(DeepSeekHarnessConfig).first()
            assert row.protocol == "anthropic"
        DeepSeekHarnessConfigService.invalidate_cache()

    def test_invalid_protocol_rejected(self, monkeypatch):
        import sqlalchemy as sa
        from app.db.session import Base
        from app.models.deepseek_harness_models import DeepSeekHarnessConfig
        engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[DeepSeekHarnessConfig.__table__])
        SessionLocal = sa.orm.sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.deepseek_harness_config_service.SessionLocal", SessionLocal)
        with pytest.raises(ValueError, match="不支持的网关协议"):
            DeepSeekHarnessConfigService.save_config({"model": "m", "protocol": "grpc"})

    def test_default_protocol_is_deepseek(self, monkeypatch):
        import sqlalchemy as sa
        from app.db.session import Base
        from app.models.deepseek_harness_models import DeepSeekHarnessConfig
        engine = sa.create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[DeepSeekHarnessConfig.__table__])
        SessionLocal = sa.orm.sessionmaker(bind=engine)
        monkeypatch.setattr("app.services.deepseek_harness_config_service.SessionLocal", SessionLocal)
        DeepSeekHarnessConfigService.save_config({"model": "deepseek-v4-flash"})
        with SessionLocal() as db:
            row = db.query(DeepSeekHarnessConfig).first()
            assert row.protocol == "deepseek"
