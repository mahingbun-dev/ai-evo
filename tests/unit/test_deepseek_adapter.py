"""
DeepSeekAdapter 单元测试（DSH 调用 mock，不真连）

覆盖：DeepSeekHarness 构造参数（独立 dsh_home + 一次性 workspace、
强制请求超时、profile/patches/env 透传）、RunResult 的 final_response 与
finish_reason 读取、JSON 解析重试、SDK 未安装与无配置的明确错误。
"""
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.deepseek_adapter import DeepSeekAdapter

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

VALID_JSON = json.dumps({"score": 9, "feedback": "优秀", "dimension_scores": {"完成度": 9}})

DSH_CONFIG = {
    "provider": "deepseek-official",
    "model": "deepseek-v4-flash",
    "profile": "sdk-minimal",
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-real-key-123456",
    "timeout_seconds": 120,
    "max_tokens": 4096,
    "extra_env": {"DSH_FLAG": "1"},
    "extra_patches": ["extra.patch.yml"],
}


def make_harness_cls(final_response=VALID_JSON, finish_reason="completed"):
    """构造 mock DeepSeekHarness 类：可调用、上下文管理器、记录构造 kwargs"""
    cls = MagicMock(name="DeepSeekHarness")
    run_result = SimpleNamespace(
        session_id="s-1", final_response=final_response, finish_reason=finish_reason, events=[], notifications=[]
    )
    cls.return_value.__enter__.return_value.run.return_value = run_result
    return cls


@pytest.fixture
def active_config():
    """构造时不再查库取全局配置"""
    with patch(
        "app.services.deepseek_harness_config_service.DeepSeekHarnessConfigService.get_active_config",
        return_value=None,
    ):
        yield


class TestDeepSeekAdapterInit:
    def test_no_config_raises(self):
        with patch(
            "app.services.deepseek_harness_config_service.DeepSeekHarnessConfigService.get_active_config",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="未配置 DeepSeek Harness"):
                DeepSeekAdapter()

    def test_explicit_config_no_db(self, active_config):
        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        assert adapter.config["model"] == "deepseek-v4-flash"


class TestExecuteRaw:
    """_execute_raw：DSH 构造参数、隔离目录、结果读取"""

    def _run(self, config=None, harness_cls=None, system_prompt="sys prompt", prompt="user prompt"):
        adapter = DeepSeekAdapter(config or dict(DSH_CONFIG))
        harness_cls = harness_cls or make_harness_cls()
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            raw, finish_reason = adapter._execute_raw(system_prompt, prompt, timeout=120)
        return adapter, harness_cls, raw, finish_reason

    def test_reads_final_response_and_finish_reason(self):
        _, _, raw, finish_reason = self._run()
        assert raw == VALID_JSON
        assert finish_reason == "completed"

    def test_finish_reason_passthrough(self):
        harness_cls = make_harness_cls(final_response=VALID_JSON, finish_reason="max_turns")
        _, _, raw, finish_reason = self._run(harness_cls=harness_cls)
        assert finish_reason == "max_turns"

    def test_system_prompt_concatenated(self):
        _, harness_cls, _, _ = self._run()
        run_arg = harness_cls.return_value.__enter__.return_value.run.call_args[0][0]
        assert "【系统指令】" in run_arg and "sys prompt" in run_arg and "user prompt" in run_arg

    def test_harness_kwargs_isolation_and_timeout(self):
        _, harness_cls, _, _ = self._run()
        kwargs = harness_cls.call_args[1]
        # 独立 dsh_home 与一次性 workspace（tempfile 临时目录）
        assert kwargs["dsh_home"].startswith("/tmp") or "dsh_home_" in kwargs["dsh_home"]
        assert "dsh_ws_" in kwargs["cwd"]
        assert kwargs["dsh_home"] != kwargs["cwd"]
        # 强制请求超时（DSH 普通轮次默认无上限）
        assert kwargs["request_timeout_seconds"] == 120.0
        assert kwargs["initialize_timeout_seconds"] == 30.0
        # 配置透传
        assert kwargs["provider"] == "deepseek-official"
        assert kwargs["model"] == "deepseek-v4-flash"
        assert kwargs["profile"] == "sdk-minimal"
        assert kwargs["patches"] == ("extra.patch.yml",)
        assert kwargs["env"] == {"DSH_FLAG": "1"}
        assert kwargs["max_tokens"] == 4096
        assert kwargs["base_url"] == "https://api.deepseek.com"
        assert kwargs["api_key"] == "sk-real-key-123456"

    def test_each_call_independent_dirs_and_cleaned(self, active_config):
        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        harness_cls = make_harness_cls()
        dirs_seen = []
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            for _ in range(2):
                adapter._execute_raw("sys", "user", 60)
                kwargs = harness_cls.call_args[1]
                dirs_seen.append((kwargs["dsh_home"], kwargs["cwd"]))
        # 两次调用目录不同（每次独立）
        assert dirs_seen[0] != dirs_seen[1]
        # 调用结束后目录均已清理（一次性）
        for dsh_home, cwd in dirs_seen:
            import os

            assert not os.path.exists(dsh_home)
            assert not os.path.exists(cwd)

    def test_empty_response_raises(self):
        harness_cls = make_harness_cls(final_response="")
        with pytest.raises(RuntimeError, match="无返回内容"):
            self._run(harness_cls=harness_cls)

    def test_dirs_cleaned_on_error(self, active_config):
        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        harness_cls = make_harness_cls(final_response="")
        seen = {}
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            with pytest.raises(RuntimeError):
                adapter._execute_raw("sys", "user", 60)
            seen = harness_cls.call_args[1]
        import os

        assert not os.path.exists(seen["dsh_home"])
        assert not os.path.exists(seen["cwd"])


class TestRunEndToEnd:
    """run 全链路：DSH mock → JSON 解析 → 归一化（含 finish_reason）"""

    def test_run_returns_judge_result(self, active_config):
        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        harness_cls = make_harness_cls(final_response=VALID_JSON, finish_reason="completed")
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            result = adapter.run("sys", "user prompt")
        assert result["score"] == 9
        assert result["feedback"] == "优秀"
        assert result["dimension_scores"] == {"完成度": 9}
        assert result["finish_reason"] == "completed"

    def test_run_retry_once(self, active_config):
        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        responses = iter(["我认为完成得不错", VALID_JSON])
        harness_cls = make_harness_cls()

        def fake_run(prompt, **kwargs):
            return SimpleNamespace(session_id="s", final_response=next(responses), finish_reason="completed", events=[])

        harness_cls.return_value.__enter__.return_value.run.side_effect = fake_run
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            result = adapter.run("sys", "user prompt", timeout=60)
        assert result["score"] == 9
        assert harness_cls.return_value.__enter__.return_value.run.call_count == 2

    def test_run_timeout_wrapped(self, active_config):
        """外层线程级超时兜底：invoke 卡死时抛 TimeoutError"""
        import time

        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        harness_cls = make_harness_cls()
        harness_cls.return_value.__enter__.return_value.run.side_effect = lambda *a, **k: time.sleep(3)
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            with pytest.raises(TimeoutError, match="超时"):
                adapter.run("sys", "user prompt", timeout=0.3)


class TestSdkNotInstalled:
    """SDK 未安装：明确的 RuntimeError 指引而不是 ImportError 堆栈"""

    def test_import_error_wrapped(self, active_config):
        adapter = DeepSeekAdapter(dict(DSH_CONFIG))
        with patch.dict(sys.modules, {"deepseek_harness": None}):
            with pytest.raises(RuntimeError, match="deepseek-harness-sdk 未安装"):
                adapter._execute_raw("sys", "user", 60)

    def test_no_credentials_still_attempts(self, active_config):
        """无凭据配置：不阻断（SDK 可用环境变量凭据），构造参数里不含 api_key"""
        config = dict(DSH_CONFIG)
        config.pop("api_key")
        config.pop("base_url")
        adapter = DeepSeekAdapter(config)
        harness_cls = make_harness_cls()
        with patch.object(adapter, "_load_sdk", return_value=harness_cls):
            raw, _ = adapter._execute_raw("sys", "user", 60)
        kwargs = harness_cls.call_args[1]
        assert "api_key" not in kwargs and "base_url" not in kwargs
        assert raw == VALID_JSON


class TestProtocolDispatch:
    """网关协议分派（deepseek 直连 / openai、anthropic 经 pi-ai provider patch）"""

    def _config(self, **overrides):
        cfg = {
            "provider": "custom",
            "model": "deepseek-flash",
            "base_url": "https://api.example.com/api-sse-anthropic",
            "api_key": "sk-test-123",
            "extra_patches": [],
            "extra_env": {},
        }
        cfg.update(overrides)
        return cfg

    def test_native_deepseek_keeps_direct_params(self):
        """deepseek 协议（含缺省）：base_url/api_key 直传，不生成 patch，env 不含 provider key"""
        for protocol in ("deepseek", None):
            config = self._config()
            if protocol is None:
                config.pop("protocol", None)
            else:
                config["protocol"] = protocol
            kwargs = DeepSeekAdapter(config=config)._build_harness_kwargs("/tmp/h", "/tmp/w", 60)
            assert kwargs["base_url"] == "https://api.example.com/api-sse-anthropic"
            assert kwargs["api_key"] == "sk-test-123"
            assert kwargs["patches"] == ()
            assert "DSH_PROVIDER_API_KEY" not in kwargs["env"]

    def test_anthropic_builds_pi_ai_patch(self, tmp_path):
        """anthropic 协议：生成 pi-ai patch（insert llm-pi-ai），key 走环境变量不落盘直传"""
        import os
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        config = self._config(protocol="anthropic")
        kwargs = DeepSeekAdapter(config=config)._build_harness_kwargs(str(tmp_path / "h"), workspace, 60)
        # base_url/api_key 不再直传（那是官方 DeepSeek 适配器的参数）
        assert "base_url" not in kwargs and "api_key" not in kwargs
        # patch 文件生成且内容完整
        patches = kwargs["patches"]
        assert len(patches) == 1 and os.path.isfile(patches[0])
        content = open(patches[0], encoding="utf-8").read()
        assert "llm-pi-ai" in content and "anthropic-messages" in content
        # provider 用固定标识声明（避开 sdk-minimal 内置 deepseek-official 的重复声明），
        # 并带 runtime 要求的 displayName/settingsNs
        assert "pi-ai-gateway" in content and "deepseek-flash" in content
        assert "displayName" in content and "settingsNs" in content
        assert "https://api.example.com/api-sse-anthropic" in content
        # key 不写入 patch 文件，只经环境变量引用
        assert "sk-test-123" not in content
        assert kwargs["env"]["DSH_PROVIDER_API_KEY"] == "sk-test-123"
        # provider 路由指向 patch 声明的固定标识，而非配置里的 provider 名
        assert kwargs["provider"] == "pi-ai-gateway"

    def test_openai_maps_to_completions_api(self, tmp_path):
        """openai 协议映射为 pi-ai 的 openai-completions"""
        import os
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        config = self._config(protocol="openai", base_url="https://gw.example/v1")
        kwargs = DeepSeekAdapter(config=config)._build_harness_kwargs(str(tmp_path / "h"), workspace, 60)
        content = open(kwargs["patches"][0], encoding="utf-8").read()
        assert "openai-completions" in content

    def test_extra_patches_preserved_with_provider_patch(self, tmp_path):
        """extra_patches 与协议 patch 叠加而非覆盖"""
        import os
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        config = self._config(protocol="anthropic", extra_patches=["my-own-patch.yml"])
        kwargs = DeepSeekAdapter(config=config)._build_harness_kwargs(str(tmp_path / "h"), workspace, 60)
        assert list(kwargs["patches"]) == ["my-own-patch.yml", kwargs["patches"][1]]

    def test_invalid_protocol_rejected(self, tmp_path):
        config = self._config(protocol="grpc")
        with pytest.raises(ValueError, match="不支持的网关协议"):
            DeepSeekAdapter(config=config)._build_harness_kwargs("/tmp/h", str(tmp_path), 60)

    def test_missing_required_fields_for_non_deepseek(self, tmp_path):
        """非 deepseek 协议下缺 provider/model/base_url 任一即报错"""
        config = self._config(protocol="anthropic", provider="")
        with pytest.raises(ValueError, match="必填"):
            DeepSeekAdapter(config=config)._build_harness_kwargs("/tmp/h", str(tmp_path), 60)
