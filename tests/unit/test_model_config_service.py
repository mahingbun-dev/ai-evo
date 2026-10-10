"""
ModelConfigService 单元测试
测试 parse_curl_command 方法和 ModelResponse 字段
"""

import pytest
from app.services.model_config_service import ModelConfigService
from app.api.model_config_api import ModelResponse, ParseCurlResponse
from app.api.evaluation_task_api import UpdateTaskRequest


class TestParseCurlCommand:
    """parse_curl_command 方法测试"""

    def test_parse_curl_command_standard_post_request(self):
        """测试解析标准 POST 请求"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X POST https://api.example.com/chat -H "Content-Type: application/json" -d \'{"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]}\''

        result = service.parse_curl_command(curl_cmd)

        assert result["endpoint"] == "https://api.example.com/chat"
        assert result["method"] == "POST"
        assert "Content-Type" in result["headers"]
        assert result["headers"]["Content-Type"] == "application/json"
        assert result["body"] is not None
        assert result["body"]["model"] == "gpt-4"
        assert result["body"]["messages"][0]["content"] == "hello"

    def test_parse_curl_command_bearer_token(self):
        """测试解析 Bearer token"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X POST https://api.example.com/chat -H "Authorization: Bearer sk-1234567890abcdef" -H "Content-Type: application/json"'

        result = service.parse_curl_command(curl_cmd)

        assert result["auth_config"]["type"] == "bearer"
        assert result["auth_config"]["token"] == "sk-1234567890abcdef"

    def test_parse_curl_command_basic_auth(self):
        """测试解析 Basic Auth"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X POST https://api.example.com/chat -u admin:secretpassword -H "Content-Type: application/json"'

        result = service.parse_curl_command(curl_cmd)

        assert result["auth_config"]["type"] == "basic"
        assert result["auth_config"]["username"] == "admin"
        assert result["auth_config"]["password"] == "secretpassword"

    def test_parse_curl_command_get_request(self):
        """测试解析 GET 请求"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X GET https://api.example.com/models -H "Authorization: Bearer token123"'

        result = service.parse_curl_command(curl_cmd)

        assert result["method"] == "GET"
        assert result["endpoint"] == "https://api.example.com/models"

    def test_parse_curl_command_empty_command(self):
        """测试处理空命令"""
        service = ModelConfigService(db=None)

        result = service.parse_curl_command("")

        assert result["endpoint"] == ""
        assert result["method"] == "POST"
        assert result["headers"] == {}
        assert result["body"] is None
        assert result["auth_config"] == {}

    def test_parse_curl_command_default_method(self):
        """测试默认方法为 POST"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl https://api.example.com/chat -H "Content-Type: application/json"'

        result = service.parse_curl_command(curl_cmd)

        assert result["method"] == "POST"

    def test_parse_curl_command_request_long_form(self):
        """测试解析 --request 长格式方法"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl --request GET https://api.example.com/models -H "Authorization: Bearer token123"'

        result = service.parse_curl_command(curl_cmd)

        assert result["method"] == "GET"
        assert result["endpoint"] == "https://api.example.com/models"

    def test_parse_curl_command_header_long_form(self):
        """测试解析 --header 长格式"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X POST https://api.example.com/chat --header "Content-Type: application/json" --header "X-Custom: value1"'

        result = service.parse_curl_command(curl_cmd)

        assert result["method"] == "POST"
        assert result["headers"]["Content-Type"] == "application/json"
        assert result["headers"]["X-Custom"] == "value1"

    def test_parse_curl_command_url_query_params(self):
        """测试解析 URL 查询参数"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X GET "https://api.example.com/search?q=hello&page=1&limit=10"'

        result = service.parse_curl_command(curl_cmd)

        assert result["endpoint"] == "https://api.example.com/search"
        assert result["params"]["q"] == "hello"
        assert result["params"]["page"] == "1"
        assert result["params"]["limit"] == "10"

    def test_parse_curl_command_get_with_long_form_all(self):
        """测试完整的 GET 请求（--request + --header + URL参数）"""
        service = ModelConfigService(db=None)

        curl_cmd = (
            "curl --location --request GET "
            "'https://api.example.com/api-detection/public/api/anomaly/publishTaskByCode"
            "?taskCodes=HOUSEHOLD_LOADING_SETTLEMENT_MODEL' "
            "--header 'Authorization: hnfU2ejWVT836u8KHVHE'"
        )

        result = service.parse_curl_command(curl_cmd)

        assert result["method"] == "GET"
        assert result["endpoint"] == (
            "https://api.example.com/api-detection/public/api/anomaly/publishTaskByCode"
        )
        assert result["params"]["taskCodes"] == "HOUSEHOLD_LOADING_SETTLEMENT_MODEL"
        assert result["headers"]["Authorization"] == "hnfU2ejWVT836u8KHVHE"
        assert result["body"] is None

    def test_parse_curl_command_bearer_token_long_header(self):
        """测试 --header 格式的 Bearer token"""
        service = ModelConfigService(db=None)

        curl_cmd = 'curl -X POST https://api.example.com/chat --header "Authorization: Bearer sk-1234567890abcdef"'

        result = service.parse_curl_command(curl_cmd)

        assert result["auth_config"]["type"] == "bearer"
        assert result["auth_config"]["token"] == "sk-1234567890abcdef"


class TestUpdateTaskRequest:
    """UpdateTaskRequest 模型测试"""

    def test_update_task_request_has_evaluation_type_field(self):
        """测试 UpdateTaskRequest 包含 evaluation_type 字段"""
        request = UpdateTaskRequest(
            name="Test Task",
            evaluation_type="judge",
        )
        assert request.evaluation_type == "judge"

    def test_update_task_request_evaluation_type_optional(self):
        """测试 evaluation_type 字段可选"""
        request = UpdateTaskRequest(name="Test Task")
        assert request.evaluation_type is None

    def test_update_task_request_all_fields(self):
        """测试 UpdateTaskRequest 所有字段"""
        request = UpdateTaskRequest(
            name="Test Task",
            evaluation_type="consistency",
            description="Test description",
            dataset_id="dataset-123",
        )
        assert request.name == "Test Task"
        assert request.evaluation_type == "consistency"
        assert request.description == "Test description"
        assert request.dataset_id == "dataset-123"


class TestModelResponse:
    """ModelResponse 模型测试"""

    def test_model_response_has_role_description_field(self):
        """测试 ModelResponse 包含 role_description 字段"""
        from datetime import datetime

        response = ModelResponse(
            id="model-1",
            name="Test Model",
            provider="openai",
            model_type="gpt-4",
            model_role="evaluation",
            endpoint="https://api.openai.com/v1/chat/completions",
            auth_type="bearer",
            supports_multimodal=False,
            description="Test model",
            role_description="待评测模型",
            is_active=True,
            is_preset=False,
            created_at=datetime.now(),
        )
        assert response.role_description == "待评测模型"

    def test_model_response_role_description_optional(self):
        """测试 role_description 字段可选"""
        from datetime import datetime

        response = ModelResponse(
            id="model-1",
            name="Test Model",
            provider="openai",
            model_type="gpt-4",
            model_role="evaluation",
            endpoint="https://api.openai.com/v1/chat/completions",
            auth_type="bearer",
            supports_multimodal=False,
            description="Test model",
            is_active=True,
            is_preset=False,
            created_at=datetime.now(),
        )
        assert response.role_description is None


class TestParseCurlResponse:
    """ParseCurlResponse 模型测试"""

    def test_parse_curl_response_model_exists(self):
        """测试 ParseCurlResponse 模型存在"""
        response = ParseCurlResponse(
            endpoint="https://api.example.com",
            method="POST",
            headers={"Content-Type": "application/json"},
            params={"q": "hello"},
            body={"key": "value"},
            auth_config={"type": "bearer", "token": "test"},
        )
        assert response.endpoint == "https://api.example.com"
        assert response.method == "POST"
        assert response.headers["Content-Type"] == "application/json"
        assert response.params["q"] == "hello"
        assert response.body["key"] == "value"
        assert response.auth_config["type"] == "bearer"

    def test_parse_curl_response_default_values(self):
        """测试 ParseCurlResponse 默认值"""
        response = ParseCurlResponse(
            endpoint="https://api.example.com",
            method="GET",
        )
        assert response.headers == {}
        assert response.params == {}
        assert response.body is None
        assert response.auth_config == {}
