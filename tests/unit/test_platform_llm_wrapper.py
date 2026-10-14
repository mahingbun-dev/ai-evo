"""
平台 LLM 包装器单元测试
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json

from app.services.platform_llm_wrapper import PlatformLLMWrapper, create_platform_llm

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestPlatformLLMWrapper:
    """PlatformLLMWrapper 测试类"""

    def test_init_with_model_config(self):
        """测试使用 model_config 初始化"""
        config = {
            "provider": "openai",
            "model_type": "gpt-4",
            "endpoint": "https://api.openai.com/v1/chat/completions",
            "api_key": "test-key",
        }
        wrapper = PlatformLLMWrapper(model_config=config, model_name="test-model")
        assert wrapper._model_name == "test-model"
        assert wrapper.model_config == config

    def test_init_without_model_name(self):
        """测试不传 model_name 时使用 model_type"""
        config = {
            "provider": "openai",
            "model_type": "gpt-4",
        }
        wrapper = PlatformLLMWrapper(model_config=config)
        assert wrapper._model_name == "gpt-4"

    def test_get_model_name(self):
        """测试获取模型名称"""
        config = {"provider": "openai", "model_type": "gpt-4"}
        wrapper = PlatformLLMWrapper(model_config=config, model_name="my-model")
        assert wrapper.get_model_name() == "my-model"

    def test_load_model_success(self):
        """测试加载模型成功"""
        config = {
            "provider": "openai",
            "model_type": "gpt-4",
            "endpoint": "https://api.openai.com/v1/chat/completions",
        }
        wrapper = PlatformLLMWrapper(model_config=config)
        result = wrapper.load_model()
        assert result == wrapper

    def test_load_model_missing_endpoint_custom(self):
        """测试自定义模型缺少 endpoint"""
        config = {
            "provider": "custom_http",
            "model_type": "my-model",
        }
        with pytest.raises(ValueError, match="缺少 endpoint"):
            PlatformLLMWrapper(model_config=config)

    @patch("app.services.platform_llm_wrapper.httpx.Client")
    def test_generate_openai(self, mock_client_class):
        """测试 OpenAI 模式生成"""
        config = {
            "provider": "openai",
            "model_type": "gpt-4",
            "endpoint": "https://api.openai.com/v1/chat/completions",
            "api_key": "test-key",
        }
        wrapper = PlatformLLMWrapper(model_config=config)

        # Mock HTTP 响应
        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Generated text"}}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__enter__.return_value = mock_client

        result = wrapper.generate("Test prompt")
        assert result == "Generated text"

    @patch("app.services.platform_llm_wrapper.httpx.Client")
    def test_generate_anthropic(self, mock_client_class):
        """测试 Anthropic 模式生成"""
        config = {
            "provider": "anthropic",
            "model_type": "claude-3-sonnet-20240229",
            "endpoint": "https://api.anthropic.com/v1/messages",
            "api_key": "test-key",
        }
        wrapper = PlatformLLMWrapper(model_config=config)

        # Mock HTTP 响应
        mock_response = Mock()
        mock_response.json.return_value = {
            "content": [{"text": "Generated text"}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__enter__.return_value = mock_client

        result = wrapper.generate("Test prompt")
        assert result == "Generated text"

    @patch("app.services.platform_llm_wrapper.httpx.Client")
    def test_generate_custom_http(self, mock_client_class):
        """测试自定义 HTTP 模式生成"""
        config = {
            "provider": "custom_http",
            "model_type": "my-model",
            "endpoint": "https://my-api.com/generate",
            "api_key": "test-key",
        }
        wrapper = PlatformLLMWrapper(model_config=config)

        # Mock HTTP 响应
        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Generated text"}}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__enter__.return_value = mock_client

        result = wrapper.generate("Test prompt")
        assert result == "Generated text"

    def test_generate_batch(self):
        """测试批量生成"""
        config = {"provider": "openai", "model_type": "gpt-4"}
        wrapper = PlatformLLMWrapper(model_config=config)

        with patch.object(wrapper, "_generate", return_value="text") as mock_generate:
            result = wrapper.generate(["prompt1", "prompt2"])
            assert result == ["text", "text"]
            assert mock_generate.call_count == 2

    def test_batch_generate(self):
        """测试 batch_generate 方法"""
        config = {"provider": "openai", "model_type": "gpt-4"}
        wrapper = PlatformLLMWrapper(model_config=config)

        with patch.object(wrapper, "_generate", return_value="text") as mock_generate:
            result = wrapper.batch_generate(["prompt1", "prompt2"])
            assert result == ["text", "text"]

    def test_supports_json_mode(self):
        """测试 JSON 模式支持"""
        config = {"provider": "openai", "model_type": "gpt-4"}
        wrapper = PlatformLLMWrapper(model_config=config)
        assert wrapper.supports_json_mode() is None

    def test_supports_log_probs(self):
        """测试 log probs 支持"""
        config = {"provider": "openai", "model_type": "gpt-4"}
        wrapper = PlatformLLMWrapper(model_config=config)
        assert wrapper.supports_log_probs() is None


class TestCreatePlatformLLM:
    """create_platform_llm 便捷函数测试"""

    def test_create_with_model_config(self):
        """测试使用 model_config 创建"""
        config = {
            "provider": "openai",
            "model_type": "gpt-4",
        }
        llm = create_platform_llm(config, model_name="test")
        assert isinstance(llm, PlatformLLMWrapper)
        assert llm.get_model_name() == "test"

    def test_create_without_model_name(self):
        """测试不传 model_name"""
        config = {
            "provider": "openai",
            "model_type": "gpt-4",
        }
        llm = create_platform_llm(config)
        assert llm.get_model_name() == "gpt-4"
