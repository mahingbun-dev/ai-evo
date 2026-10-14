"""
Claude Code 全局配置服务单元测试

覆盖：token 脱敏、裁判 LLM 配置转换、SDK 环境变量构造、
配置读取缓存与失效、保存逻辑（脱敏值不覆盖原 token）、回落逻辑。
"""
from unittest.mock import MagicMock

import pytest

from app.services.claude_code_config_service import (
    ClaudeCodeConfigService,
    mask_token,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


SAMPLE_CONFIG = {
    "id": "ccc_test000001",
    "name": "默认配置",
    "base_url": "https://gw.example.com/anthropic/",
    "auth_token": "sk-abcdefghijklmnop",
    "model": "claude-sonnet-4-5",
    "timeout_seconds": 300,
    "max_turns": 30,
    "proxy_url": None,
    "extra_env": {"CUSTOM_FLAG": "1"},
}


@pytest.fixture(autouse=True)
def reset_cache():
    """每个用例前后清空配置缓存，避免用例间串扰"""
    ClaudeCodeConfigService.invalidate_cache()
    yield
    ClaudeCodeConfigService.invalidate_cache()


class TestMaskToken:
    def test_normal(self):
        assert mask_token("sk-abcdefghijklmnop") == "sk-a****mnop"

    def test_short_token(self):
        assert mask_token("short") == "****"

    def test_empty(self):
        assert mask_token("") == "****"


class TestGetJudgeLLMConfig:
    def _patch_active(self, monkeypatch, config):
        monkeypatch.setattr(
            ClaudeCodeConfigService, "get_active_config", classmethod(lambda cls: config)
        )

    def test_convert_to_anthropic_provider(self, monkeypatch):
        """转换为现有裁判配置形态：provider=anthropic，endpoint 去尾斜杠"""
        self._patch_active(monkeypatch, dict(SAMPLE_CONFIG))
        judge_config = ClaudeCodeConfigService.get_judge_llm_config()
        assert judge_config["provider"] == "anthropic"
        assert judge_config["endpoint"] == "https://gw.example.com/anthropic"
        assert judge_config["api_key"] == "sk-abcdefghijklmnop"
        assert judge_config["model_id"] == "claude-sonnet-4-5"
        assert judge_config["timeout_seconds"] == 300

    def test_no_config_returns_none(self, monkeypatch):
        """未配置时返回 None（调用方回落历史 judge_model）"""
        self._patch_active(monkeypatch, None)
        assert ClaudeCodeConfigService.get_judge_llm_config() is None


class TestGetSdkEnv:
    def _patch_active(self, monkeypatch, config):
        monkeypatch.setattr(
            ClaudeCodeConfigService, "get_active_config", classmethod(lambda cls: config)
        )

    def test_env_contains_anthropic_vars(self, monkeypatch):
        self._patch_active(monkeypatch, dict(SAMPLE_CONFIG))
        env = ClaudeCodeConfigService.get_sdk_env()
        assert env["ANTHROPIC_BASE_URL"] == "https://gw.example.com/anthropic"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-abcdefghijklmnop"
        assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"
        assert "HTTPS_PROXY" not in env
        # extra_env 合并
        assert env["CUSTOM_FLAG"] == "1"

    def test_proxy_env(self, monkeypatch):
        config = dict(SAMPLE_CONFIG, proxy_url="http://proxy.local:8080")
        self._patch_active(monkeypatch, config)
        env = ClaudeCodeConfigService.get_sdk_env()
        assert env["HTTPS_PROXY"] == "http://proxy.local:8080"
        assert env["HTTP_PROXY"] == "http://proxy.local:8080"

    def test_no_config_empty(self, monkeypatch):
        self._patch_active(monkeypatch, None)
        assert ClaudeCodeConfigService.get_sdk_env() == {}


class TestSaveConfigTokenHandling:
    """保存配置时 token 处理：脱敏回传值不覆盖原 token（P1 回归）"""

    def _patch_db(self, monkeypatch, existing_row):
        fake_session = MagicMock()
        query_chain = fake_session.query.return_value.filter.return_value.order_by.return_value
        query_chain.first.return_value = existing_row
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        monkeypatch.setattr(
            "app.services.claude_code_config_service.SessionLocal",
            MagicMock(return_value=fake_session),
        )
        return fake_session

    @staticmethod
    def _existing_row(token="sk-real-token-123456"):
        row = MagicMock()
        row.auth_token = token
        return row

    def test_masked_token_unchanged_keeps_original(self, monkeypatch):
        """回传 mask_token 格式的脱敏值 → 原 token 不被覆盖"""
        row = self._existing_row("sk-real-token-123456")
        self._patch_db(monkeypatch, row)
        ClaudeCodeConfigService.save_config({
            "name": "默认配置",
            "base_url": "https://gw.example.com",
            "auth_token": "sk-r****3456",  # mask_token 的输出格式
            "model": "claude-sonnet-4-5",
        })
        assert row.auth_token == "sk-real-token-123456"

    def test_short_masked_token_keeps_original(self, monkeypatch):
        """短 token 的脱敏值 **** 同样不覆盖"""
        row = self._existing_row("short")
        self._patch_db(monkeypatch, row)
        ClaudeCodeConfigService.save_config({
            "base_url": "https://gw.example.com",
            "auth_token": "****",
            "model": "m",
        })
        assert row.auth_token == "short"

    def test_new_token_updates(self, monkeypatch):
        """明文新 token → 更新"""
        row = self._existing_row("old-token-value99")
        self._patch_db(monkeypatch, row)
        ClaudeCodeConfigService.save_config({
            "base_url": "https://gw.example.com",
            "auth_token": "brand-new-token111",
            "model": "m",
        })
        assert row.auth_token == "brand-new-token111"


class TestCacheBehavior:
    def test_cache_hit_avoids_db(self, monkeypatch):
        """缓存命中时不查库：第二次调用不再触发 SessionLocal"""
        from unittest.mock import MagicMock

        fake_session = MagicMock()
        fake_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        monkeypatch.setattr(
            "app.services.claude_code_config_service.SessionLocal",
            MagicMock(return_value=fake_session),
        )
        # 第一次：查库返回 None（不缓存 None）
        assert ClaudeCodeConfigService.get_active_config() is None
        assert fake_session.query.call_count == 1
        # None 不缓存，再次调用仍查库
        assert ClaudeCodeConfigService.get_active_config() is None
        assert fake_session.query.call_count == 2

    def test_invalidate_cache(self):
        ClaudeCodeConfigService._cached_config = dict(SAMPLE_CONFIG)
        ClaudeCodeConfigService.invalidate_cache()
        assert ClaudeCodeConfigService._cached_config is None


class TestJudgeGraphStateFallback:
    """裁判图配置加载：CC 配置优先，无 CC 配置回落 task.judge_model"""

    def test_cc_config_first(self, monkeypatch):
        from app.services import judge_graph_state

        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_judge_llm_config",
            classmethod(lambda cls: {"provider": "anthropic", "api_key": "cc"}),
        )
        task = type("Task", (), {"judge_model": "model_x", "judge_model_config": {}})()
        config = judge_graph_state._load_judge_model_config(task)
        assert config["provider"] == "anthropic"
        assert config["api_key"] == "cc"

    def test_fallback_to_judge_model(self, monkeypatch):
        """无 CC 配置时回落 ModelConfig 查询"""
        from unittest.mock import MagicMock

        from app.services import judge_graph_state

        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_judge_llm_config",
            classmethod(lambda cls: None),
        )
        mock_row = MagicMock(
            provider="openai",
            model_type="gpt-4",
            endpoint="https://api.example.com",
            api_key="old-key",
            auth_type="none",
            auth_config={},
            request_config={},
            response_parse_config={},
            id="model_x",
        )
        fake_session = MagicMock()
        fake_session.query.return_value.filter.return_value.first.return_value = mock_row
        monkeypatch.setattr(
            "app.db.session.SessionLocal", MagicMock(return_value=fake_session)
        )
        task = type("Task", (), {"judge_model": "model_x", "judge_model_config": {}})()
        config = judge_graph_state._load_judge_model_config(task)
        assert config["provider"] == "openai"
        assert config["api_key"] == "old-key"
