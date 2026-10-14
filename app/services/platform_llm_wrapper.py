"""
平台 LLM 包装器

实现 DeepEvalBaseLLM 接口，使用平台配置的模型信息进行 LLM 调用，
而非硬编码 OpenAI API。

支持：
- OpenAI 兼容 API
- Anthropic API
- 自定义 HTTP API
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union

import httpx
from deepeval.models import DeepEvalBaseLLM

logger = logging.getLogger(__name__)


class PlatformLLMWrapper(DeepEvalBaseLLM):
    """
    平台 LLM 包装器

    封装平台配置的模型信息，实现 DeepEval 的 LLM 接口。
    支持 OpenAI、Anthropic 和自定义 HTTP API。
    """

    def __init__(
        self,
        model_config: Dict[str, Any],
        model_name: Optional[str] = None,
    ):
        """
        初始化平台 LLM 包装器

        Args:
            model_config: 模型配置字典，包含：
                - provider: 模型提供商 (openai/anthropic/custom_http)
                - model_type: 具体模型名称
                - endpoint: API 端点 URL
                - api_key: API Key
                - auth_type: 鉴权类型
                - auth_config: 鉴权配置
                - request_config: 请求配置
            model_name: 模型显示名称（可选）
        """
        self.model_config = model_config
        self._model_name = model_name or model_config.get("model_type", "platform-model")
        super().__init__(model=self._model_name)

    def load_model(self):
        """加载模型（此处为配置验证）"""
        provider = self.model_config.get("provider", "openai")
        endpoint = self.model_config.get("endpoint")

        if not endpoint and provider != "openai":
            raise ValueError(f"模型配置缺少 endpoint: {provider}")

        logger.info(f"加载平台模型: provider={provider}, model={self._model_name}")
        return self

    def _generate(self, prompt: str) -> str:
        """
        调用模型生成响应

        Args:
            prompt: 输入提示

        Returns:
            模型生成的文本
        """
        provider = self.model_config.get("provider", "openai")

        if provider == "openai":
            return self._call_openai(prompt)
        elif provider == "anthropic":
            return self._call_anthropic(prompt)
        elif provider in ("custom", "custom_http"):
            return self._call_custom(prompt)
        else:
            # 默认使用 OpenAI 兼容格式
            return self._call_openai(prompt)

    def _call_openai(self, prompt: str) -> str:
        """调用 OpenAI 兼容 API"""
        try:
            headers = {"Content-Type": "application/json"}

            api_key = self.model_config.get("api_key")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            model = self.model_config.get("model_type", "gpt-3.5-turbo")
            endpoint = self.model_config.get("endpoint", "https://api.openai.com/v1/chat/completions")

            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": self.model_config.get("temperature", 0.7),
                "max_tokens": self.model_config.get("max_tokens", 2000),
            }

            with httpx.Client(timeout=120.0) as client:
                response = client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                return result["choices"][0]["message"]["content"]

        except Exception as e:
            logger.exception("OpenAI API 调用失败")
            raise RuntimeError(f"模型调用失败: {str(e)}")

    def _call_anthropic(self, prompt: str) -> str:
        """调用 Anthropic API"""
        try:
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.model_config.get("api_key", ""),
                "anthropic-version": "2023-06-01",
            }

            model = self.model_config.get("model_type", "claude-3-sonnet-20240229")
            endpoint = self.model_config.get("endpoint", "https://api.anthropic.com/v1/messages")

            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.model_config.get("max_tokens", 2000),
                "temperature": self.model_config.get("temperature", 0.7),
            }

            with httpx.Client(timeout=120.0) as client:
                response = client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
                return result["content"][0]["text"]

        except Exception as e:
            logger.exception("Anthropic API 调用失败")
            raise RuntimeError(f"模型调用失败: {str(e)}")

    def _call_custom(self, prompt: str) -> str:
        """调用自定义 HTTP API"""
        try:
            endpoint = self.model_config.get("endpoint")
            if not endpoint:
                raise ValueError("未配置模型端点")

            headers = {"Content-Type": "application/json"}

            # 添加认证信息（W16 复核修复：显式 None 视同未配置，
            # dict.get 默认值不覆盖 key 存在但值为 None 的情况）
            auth_type = self.model_config.get("auth_type", "none")
            auth_config = self.model_config.get("auth_config", {}) or {}

            if auth_type in ("bearer", "api_key"):
                api_key = self.model_config.get("api_key")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
            elif auth_type == "custom_header":
                custom_header_name = auth_config.get("header_name", "X-API-Key")
                custom_header_value = auth_config.get("header_value", "")
                headers[custom_header_name] = custom_header_value

            # 构建请求
            request_config = self.model_config.get("request_config", {}) or {}
            request_template = request_config.get("template")

            if request_template:
                if isinstance(request_template, str):
                    payload = json.loads(request_template)
                else:
                    payload = request_template.copy()
                payload["messages"] = [{"role": "user", "content": prompt}]
            else:
                payload = {
                    "model": self.model_config.get("model_type", "default"),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.model_config.get("temperature", 0.7),
                    "max_tokens": self.model_config.get("max_tokens", 2000),
                }

            with httpx.Client(timeout=120.0) as client:
                response = client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()

                # 尝试从响应中提取内容
                content = result.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    content = result.get("content") or result.get("text") or str(result)

                return content

        except Exception as e:
            logger.exception("自定义 API 调用失败")
            raise RuntimeError(f"模型调用失败: {str(e)}")

    def generate(self, *args, **kwargs) -> str:
        """
        DeepEval 接口：生成文本

        支持两种调用方式：
        1. generate(prompts: List[str]) -> List[str]
        2. generate(prompt: str) -> str
        """
        if args and isinstance(args[0], list):
            # 批量生成
            prompts = args[0]
            return [self._generate(prompt) for prompt in prompts]
        elif args:
            return self._generate(args[0])
        elif "prompt" in kwargs:
            return self._generate(kwargs["prompt"])
        else:
            raise ValueError("generate() 需要 prompt 参数")

    def batch_generate(self, *args, **kwargs) -> List[str]:
        """
        DeepEval 接口：批量生成文本
        """
        return self.generate(*args, **kwargs)

    async def a_generate(self, *args, **kwargs) -> str:
        """
        DeepEval 接口：异步生成文本

        注意：当前实现使用同步 HTTP 调用，未来可优化为异步
        """
        return self.generate(*args, **kwargs)

    async def a_generate_with_schema(self, *args, schema=None, **kwargs):
        """
        DeepEval 接口：异步生成带 schema 的文本

        W16 接入 deepeval 指标（G-Eval 系）路径：与 DeepEvalBaseLLM 基类契约
        一致，返回 a_generate 的原始字符串，由 deepeval 的 trimAndLoadJson
        统一解析。此前实现返回 json.loads 后的 dict，会触发
        "'dict' object has no attribute 'find'"，导致所有 G-Eval 指标不可用。
        """
        return await self.a_generate(*args, **kwargs)

    def get_model_name(self) -> str:
        """获取模型名称"""
        return self._model_name

    def supports_json_mode(self) -> Union[bool, None]:
        """是否支持 JSON 模式"""
        return None

    def supports_log_probs(self) -> Union[bool, None]:
        """是否支持 log probs"""
        return None


def create_platform_llm(
    model_config: Dict[str, Any],
    model_name: Optional[str] = None,
) -> PlatformLLMWrapper:
    """
    创建平台 LLM 包装器的便捷函数

    Args:
        model_config: 模型配置字典
        model_name: 模型显示名称（可选）

    Returns:
        PlatformLLMWrapper 实例
    """
    return PlatformLLMWrapper(model_config=model_config, model_name=model_name)
