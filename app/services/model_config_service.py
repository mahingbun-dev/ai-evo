"""
模型配置管理服务
"""
import uuid
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from app.models.model_config_models import ModelConfig, PresetModel, JudgeModelConfig
from app.services.model_caller import ModelCaller


class ModelConfigService:
    """模型配置服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_model(
        self,
        name: str,
        provider: str,
        model_type: str,
        model_role: str = "evaluation",
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        auth_type: str = "none",
        auth_config: Optional[Dict] = None,
        request_config: Optional[Dict] = None,
        response_parse_config: Optional[Dict] = None,
        supports_multimodal: bool = False,
        multimodal_config: Optional[Dict] = None,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        team_code: Optional[str] = None,
    ) -> ModelConfig:
        """创建模型配置"""
        model_id = f"model_{uuid.uuid4().hex[:12]}"

        config = ModelConfig(
            id=model_id,
            name=name,
            provider=provider,
            model_type=model_type,
            model_role=model_role,
            endpoint=endpoint,
            api_key=api_key,
            auth_type=auth_type,
            auth_config=auth_config or {},
            request_config=request_config or {},
            response_parse_config=response_parse_config or {},
            supports_multimodal=supports_multimodal,
            multimodal_config=multimodal_config or {},
            description=description,
            created_by=created_by,
            team_code=team_code or "DEFAULT",
        )

        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        return config

    def get_model(self, model_id: str) -> Optional[ModelConfig]:
        """获取模型配置"""
        return self.db.query(ModelConfig).filter(ModelConfig.id == model_id).first()

    def get_models(
        self,
        provider: Optional[str] = None,
        model_type: Optional[str] = None,
        model_role: Optional[str] = None,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
        team_code: Optional[str] = None,
    ) -> List[ModelConfig]:
        """获取模型配置列表"""
        query = self.db.query(ModelConfig)

        if provider:
            query = query.filter(ModelConfig.provider == provider)
        if model_type:
            query = query.filter(ModelConfig.model_type == model_type)
        if model_role:
            query = query.filter(ModelConfig.model_role == model_role)
        if is_active is not None:
            query = query.filter(ModelConfig.is_active == is_active)
        if team_code:
            query = query.filter(ModelConfig.team_code == team_code)

        return query.offset(skip).limit(limit).all()

    def update_model(
        self,
        model_id: str,
        **kwargs,
    ) -> Optional[ModelConfig]:
        """更新模型配置"""
        config = self.get_model(model_id)
        if not config:
            return None

        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)

        self.db.commit()
        self.db.refresh(config)
        return config

    def delete_model(self, model_id: str) -> bool:
        """删除模型配置"""
        config = self.get_model(model_id)
        if not config:
            return False

        # 检查是否有关联的评测任务
        from app.models.task_models import EvaluationTask
        from sqlalchemy import cast, String

        # 1. 检查 model_provider 字段（被评测模型ID）
        tasks_by_provider = self.db.query(EvaluationTask).filter(
            EvaluationTask.is_active == True,
            EvaluationTask.model_provider == model_id
        ).all()

        # 2. 检查 judge_model 字段（裁判模型ID）
        tasks_by_judge = self.db.query(EvaluationTask).filter(
            EvaluationTask.is_active == True,
            EvaluationTask.judge_model == model_id
        ).all()

        # 3. 检查 model_config JSON 字段中的 model_id
        tasks_by_config = self.db.query(EvaluationTask).filter(
            EvaluationTask.is_active == True,
            cast(EvaluationTask.model_config, String).like(f'%{model_id}%')
        ).all()

        # 验证 model_config 中的 model_id 确实匹配
        actual_related_tasks = set()
        for task in tasks_by_provider:
            actual_related_tasks.add(task)
        for task in tasks_by_judge:
            actual_related_tasks.add(task)
        for task in tasks_by_config:
            if task.model_config and isinstance(task.model_config, dict):
                if task.model_config.get("model_id") == model_id:
                    actual_related_tasks.add(task)

        if actual_related_tasks:
            task_names = [task.name for task in actual_related_tasks]
            raise ValueError(f"该模型被以下评测任务使用，无法删除：{', '.join(task_names)}。请先替换或删除这些评测任务。")

        self.db.delete(config)
        self.db.commit()
        return True

    def parse_curl_command(self, curl_command: str) -> Dict[str, Any]:
        """
        解析 curl 命令并转换为模型配置结构
        支持的格式：
        - -X POST/GET/PUT/DELETE -> method
        - -H "Key: Value" -> headers
        - -d '{"a":"b"}' -> body (JSON解析)
        - -u user:pass -> auth_config (basic)
        - -H "Authorization: Bearer xxx" -> auth_config (bearer)
        """
        import re
        import json

        result = {
            "endpoint": "",
            "method": "POST",
            "headers": {},
            "params": {},
            "body": None,
            "auth_config": {}
        }

        if not curl_command:
            return result

        # 提取 URL - 使用更简单的方法：找第一个看起来像 URL 的参数
        # curl 命令格式: curl [flags] [url] [flags]
        # URL 通常包含 :// 或以 / 开头
        tokens = curl_command.split()
        if len(tokens) >= 2:
            # 跳过 curl 本身，遍历剩余 token
            for i, token in enumerate(tokens[1:], start=1):
                # 跳过看起来像 flag 的 token
                if token.startswith('-'):
                    continue
                # 检查是否是 URL（包含 :// 或以 / 开头）
                if '://' in token or token.startswith('/'):
                    result["endpoint"] = token.strip('"\'')
                    break
                # 如果 token 是常见 HTTP 方法，跳过它
                if token.upper() in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'):
                    continue
                # 否则假设它是 URL
                result["endpoint"] = token.strip('"\'')
                break
            # 提取 URL query params
            if '?' in result["endpoint"]:
                base_url, query_string = result["endpoint"].split('?', 1)
                result["endpoint"] = base_url
                if query_string:
                    for param in query_string.split('&'):
                        if '=' in param:
                            k, v = param.split('=', 1)
                            result["params"][k.strip()] = v.strip()

        # 提取 method（同时支持 -X 和 --request 格式）
        method_match = re.search(r'-(?:X|-request)\s+(\w+)', curl_command)
        if method_match:
            result["method"] = method_match.group(1).upper()

        # 提取 headers（同时支持 -H 和 --header 格式，支持单引号和双引号）
        header_matches = re.findall(r'-(?:H|-header)\s+["\']([^"\']+)["\']', curl_command)
        for header in header_matches:
            if ':' in header:
                key, value = header.split(':', 1)
                result["headers"][key.strip()] = value.strip()

        # 提取 body (JSON) 支持 -d 和 --data 格式
        # 后面用 (?:\s+-) 匹配下一个参数开始或行尾
        body_match = re.search(r'-(?:d|-data)\s+["\']?(.+?)["\']?(?:\s+-|$)', curl_command, re.DOTALL)
        if body_match:
            body_str = body_match.group(1).strip()
            try:
                result["body"] = json.loads(body_str)
            except json.JSONDecodeError:
                result["body"] = {"raw": body_str}

        # 提取 basic auth (-u user:pass)
        auth_match = re.search(r'-u\s+["\']?([^"\'\s]+)["\']?', curl_command)
        if auth_match:
            user_pass = auth_match.group(1).strip()
            if ':' in user_pass:
                username, password = user_pass.split(':', 1)
                result["auth_config"] = {"type": "basic", "username": username, "password": password}

        # 提取 Bearer token（同时支持 -H 和 --header 格式，单引号和双引号）
        bearer_match = re.search(r'-(?:H|-header)\s+["\']Authorization:\s*Bearer\s+([^"\']+)["\']', curl_command)
        if bearer_match:
            result["auth_config"] = {"type": "bearer", "token": bearer_match.group(1).strip()}

        return result

    async def test_connection(self, model_id: str, runtime_variables: Dict[str, Any] = None) -> Dict[str, Any]:
        """测试模型连接"""
        config = self.get_model(model_id)
        if not config:
            return {"success": False, "error": "模型不存在"}

        # 处理 runtime_variables 中的 { value, type } 格式
        processed_runtime_vars = None
        if runtime_variables:
            processed_runtime_vars = {}
            for key, val in runtime_variables.items():
                if isinstance(val, dict) and 'value' in val and 'type' in val:
                    # 根据 type 转换值
                    var_type = val.get('type', 'string')
                    var_value = val.get('value', '')
                    if var_type == 'number':
                        try:
                            processed_runtime_vars[key] = float(var_value) if '.' in var_value else int(var_value)
                        except (ValueError, TypeError):
                            processed_runtime_vars[key] = 0
                    elif var_type == 'boolean':
                        processed_runtime_vars[key] = str(var_value).lower() in ('true', '1', 'yes')
                    else:
                        processed_runtime_vars[key] = str(var_value)
                else:
                    processed_runtime_vars[key] = val

        # 构建配置字典，使用 provider 决定调用方式，model_type 指定具体模型
        config_dict = {
            "provider": config.provider,  # openai/anthropic/custom_http
            "model_type": config.model_type,  # 具体模型名称如 gpt-4
            "api_key": config.api_key,
            "endpoint": config.endpoint,
            "auth_type": config.auth_type,
            "auth_config": config.auth_config,
            "request_config": config.request_config,
            "response_parse_config": config.response_parse_config,
            "model_id": config.id,
        }

        # 使用通用模型调用器，启用debug模式返回详细请求链信息
        messages = [{"role": "user", "content": "Hello"}]
        result = await ModelCaller.call(messages, config_dict, processed_runtime_vars, debug=True)

        if result.get("success"):
            return {
                "success": True,
                "message": "连接成功",
                "response": result.get("content", "")[:200],
                "request_details": result.get("request_details"),
            }
        else:
            error_msg = result.get("error", "未知错误")
            # 如果有 detail 也包含进来
            if result.get("detail"):
                error_msg = f"{error_msg} | Detail: {result.get('detail')}"
            # 如果有 all_results 也包含进来
            if result.get("all_results"):
                error_msg = f"{error_msg} | Results: {result.get('all_results')}"
            return {
                "success": False,
                "error": error_msg,
                "request_details": result.get("request_details"),
            }


class PresetModelService:
    """预置模型服务"""

    def __init__(self, db: Session):
        self.db = db

    def get_presets(self, provider: Optional[str] = None) -> List[PresetModel]:
        """获取预置模型列表"""
        query = self.db.query(PresetModel).filter(PresetModel.is_active == True)

        if provider:
            query = query.filter(PresetModel.provider == provider)

        return query.all()

    def create_preset(
        self,
        provider: str,
        model_name: str,
        display_name: str,
        default_config: Optional[Dict] = None,
        is_multimodal: bool = False,
        description: Optional[str] = None,
    ) -> PresetModel:
        """创建预置模型"""
        preset_id = f"preset_{uuid.uuid4().hex[:12]}"

        preset = PresetModel(
            id=preset_id,
            provider=provider,
            model_name=model_name,
            display_name=display_name,
            default_config=default_config or {},
            is_multimodal=is_multimodal,
            description=description,
        )

        self.db.add(preset)
        self.db.commit()
        self.db.refresh(preset)
        return preset


class JudgeModelService:
    """裁判模型配置服务"""

    def __init__(self, db: Session):
        self.db = db

    def create_judge_model(
        self,
        name: str,
        model_type: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        temperature: int = 0,
        max_tokens: int = 4096,
        system_prompt: Optional[str] = None,
        default_max_score: int = 10,
    ) -> JudgeModelConfig:
        """创建裁判模型配置"""
        judge_id = f"judge_{uuid.uuid4().hex[:12]}"

        judge = JudgeModelConfig(
            id=judge_id,
            name=name,
            model_type=model_type,
            endpoint=endpoint,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            default_max_score=default_max_score,
        )

        self.db.add(judge)
        self.db.commit()
        self.db.refresh(judge)
        return judge

    def get_judge_models(self, is_active: Optional[bool] = None) -> List[JudgeModelConfig]:
        """获取裁判模型列表"""
        query = self.db.query(JudgeModelConfig)

        if is_active is not None:
            query = query.filter(JudgeModelConfig.is_active == is_active)

        return query.all()

    def get_judge_model(self, judge_id: str) -> Optional[JudgeModelConfig]:
        """获取裁判模型"""
        return self.db.query(JudgeModelConfig).filter(JudgeModelConfig.id == judge_id).first()
