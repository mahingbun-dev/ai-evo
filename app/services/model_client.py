"""
被评测模型客户端

负责调用被评测模型获取输出
"""
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ModelClient:
    """被评测模型客户端"""

    def __init__(self, task, db=None):
        """
        初始化模型客户端

        Args:
            task: 任务对象，包含模型配置
            db: 数据库会话（可选）
        """
        self.task = task
        self.model_provider_id = task.model_provider  # 模型ID
        self.model_endpoint = task.model_endpoint

        # 优先使用任务中已有的 model_config
        self.model_config = task.model_config or {}

        # 如果 model_config 为空，尝试从数据库查询模型配置
        if not self.model_config and self.model_provider_id:
            self.model_config = self._load_model_config_from_db(db)

    def _load_model_config_from_db(self, db):
        """从数据库加载模型配置"""
        # 如果没有提供数据库会话，则创建一个
        if not db:
            try:
                from sqlalchemy import create_engine
                from sqlalchemy.orm import sessionmaker
                from app.core.config import get_settings

                settings = get_settings()
                engine = create_engine(settings.DATABASE_URL)
                SessionLocal = sessionmaker(bind=engine)
                db = SessionLocal()
                close_db = True
            except Exception as e:
                logger.warning(f"创建数据库会话失败: {e}")
                return {}
        else:
            close_db = False

        try:
            from app.models.model_config_models import ModelConfig
            model_config = db.query(ModelConfig).filter(
                ModelConfig.id == self.model_provider_id
            ).first()

            if not model_config:
                logger.warning(f"未找到模型配置: {self.model_provider_id}")
                return {}

            # 将数据库模型配置转换为字典
            config_dict = {
                "provider": model_config.provider,
                "model_type": model_config.model_type,
                "endpoint": model_config.endpoint,
                "api_key": model_config.api_key,
                "auth_type": model_config.auth_type,
                "auth_config": model_config.auth_config,
                "request_config": model_config.request_config,
                "response_parse_config": model_config.response_parse_config,
            }
            logger.debug(f"从数据库加载模型配置成功: provider={config_dict.get('provider')}, model_type={config_dict.get('model_type')}")
            return config_dict
        except Exception as e:
            logger.error(f"加载模型配置失败: {e}")
            return {}
        finally:
            if close_db and db:
                db.close()

    def invoke(self, payload: Dict[str, Any]) -> str:
        """
        调用被评测模型

        Args:
            payload: 请求Payload，包含user_input等

        Returns:
            模型输出文本
        """
        user_input = payload.get("user_input", "")

        if not user_input:
            raise ValueError("user_input不能为空")

        # 根据模型类型选择调用方式
        # 优先从 model_config 中获取 model_type/provider
        provider = self.model_config.get("provider") or self.model_config.get("model_type")
        if not provider:
            provider = "openai"

        if provider == "openai":
            return self._call_openai(user_input)
        elif provider == "anthropic":
            return self._call_anthropic(user_input)
        elif provider in ("custom", "custom_http"):
            # 传递完整payload，保留document_url, project_id等字段
            return self._call_custom(user_input, payload)
        else:
            return self._call_openai(user_input)

    def _call_openai(self, user_input: str) -> str:
        """调用OpenAI兼容API"""
        try:
            import httpx

            headers = {
                "Content-Type": "application/json",
            }

            # 添加API Key
            api_key = self.model_config.get("api_key")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            # 构建请求
            messages = [
                {"role": "user", "content": user_input}
            ]

            model = self.model_config.get("model_type", "gpt-3.5-turbo")
            # 优先使用 model_config 中的 endpoint，其次使用 task 的 model_endpoint
            endpoint = self.model_config.get("endpoint") or self.model_endpoint or "https://api.openai.com/v1/chat/completions"

            payload = {
                "model": model,
                "messages": messages,
                "temperature": self.model_config.get("temperature", 0.7),
                "max_tokens": self.model_config.get("max_tokens", 2000),
            }

            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

                result = response.json()
                return result["choices"][0]["message"]["content"]

        except Exception as e:
            logger.exception("OpenAI API调用失败")
            raise RuntimeError(f"模型调用失败: {str(e)}")

    def _call_anthropic(self, user_input: str) -> str:
        """调用Anthropic API"""
        try:
            import httpx

            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.model_config.get("api_key", ""),
                "anthropic-version": "2023-06-01",
            }

            model = self.model_config.get("model_type", "claude-3-sonnet-20240229")
            endpoint = self.model_config.get("endpoint") or "https://api.anthropic.com/v1/messages"

            payload = {
                "model": model,
                "messages": [{"role": "user", "content": user_input}],
                "max_tokens": self.model_config.get("max_tokens", 2000),
                "temperature": self.model_config.get("temperature", 0.7),
            }

            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

                result = response.json()
                return result["content"][0]["text"]

        except Exception as e:
            logger.exception("Anthropic API调用失败")
            raise RuntimeError(f"模型调用失败: {str(e)}")

    def _call_custom(self, user_input: str, full_payload: Dict[str, Any] = None) -> str:
        """
        调用自定义API

        Args:
            user_input: 用户输入文本
            full_payload: 完整的请求payload，包含document_url, project_id等额外字段
        """
        try:
            import httpx

            # 优先使用 model_config 中的 endpoint，其次使用 task 的 model_endpoint
            endpoint = self.model_config.get("endpoint") or self.model_endpoint
            if not endpoint:
                raise ValueError("未配置模型端点")

            headers = {
                "Content-Type": "application/json",
            }

            # 添加认证信息 - 优先使用 auth_config，其次使用 api_key
            auth_type = self.model_config.get("auth_type", "none")
            auth_config = self.model_config.get("auth_config", {})

            if auth_type == "bearer" or auth_type == "api_key":
                api_key = self.model_config.get("api_key")
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
            elif auth_type == "custom_header":
                # 从 auth_config 中获取自定义 header
                custom_header_name = auth_config.get("header_name", "X-API-Key")
                custom_header_value = auth_config.get("header_value", "")
                headers[custom_header_name] = custom_header_value
            # 其他鉴权类型可以继续扩展

            # 自定义请求格式 - 优先使用 request_config
            request_config = self.model_config.get("request_config", {})
            request_template = request_config.get("template") or self.model_config.get("request_template")

            if request_template:
                if isinstance(request_template, str):
                    payload = json.loads(request_template)
                else:
                    payload = request_template
                payload["messages"] = [{"role": "user", "content": user_input}]
            else:
                payload = {
                    "messages": [{"role": "user", "content": user_input}]
                }

            # ====== 新增：合并原始payload中的额外字段 ======
            # 解决TestCaseAgent作为被评测模型时，payload缺少document_url, project_id等问题
            if full_payload:
                excluded_keys = {"user_input", "ref_answer", "model_answer", "_dataset_item", "messages"}
                for key, value in full_payload.items():
                    if key not in excluded_keys and key not in payload:
                        payload[key] = value
                        logger.info(f"[ModelClient] 合并额外字段: {key}={value}")

            logger.info(f"调用自定义API: {endpoint}, payload: {payload}")

            # ====== 调试日志：记录实际发送的payload ======
            logger.info(f"========== [调试] ModelClient实际发送的Payload ==========")
            logger.info(f"endpoint: {endpoint}")
            logger.info(f"payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")
            logger.info(f"===========================================================")

            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()

                result = response.json()

                # 尝试从响应中提取内容
                content = result.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    content = result.get("content") or result.get("text") or str(result)

                return content

        except Exception as e:
            logger.exception("自定义API调用失败")
            raise RuntimeError(f"模型调用失败: {str(e)}")
