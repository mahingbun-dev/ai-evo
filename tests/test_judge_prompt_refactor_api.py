"""
Task 3 单元测试：API 层验证
"""
import pytest
import sys
import os

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.judge_config_api import router as judge_config_router


# 创建独立的测试 app，不含鉴权中间件
# W8 修复：router 自带 prefix="/judge-config"（app/api/judge_config_api.py:19），
# 生产挂载为 app.include_router(router, prefix=API_V1_STR + "/evaluation")
# （app/main.py:201），最终路径 /api/v1/evaluation/judge-config/*。
# 原测试又叠了一层 judge-config 前缀导致全部 404——旧「零执行」时代未暴露，
# W8 首次全量运行抓到；此处对齐生产挂载形态。
test_app = FastAPI()
test_app.include_router(judge_config_router, prefix="/api/v1/evaluation")
client = TestClient(test_app)


class TestJudgeConfigApi:
    """测试 JudgeConfig API"""

    def test_get_template_returns_default_structure(self):
        """测试 GET template 返回默认模板字段结构"""
        response = client.get("/api/v1/evaluation/judge-config/template")
        assert response.status_code == 200
        data = response.json()
        assert "system_prompt_template" in data
        assert "user_prompt_template" in data
        assert "evaluation_targets" in data

    def test_preview_renders_jinja2_template(self):
        """测试 POST preview 正确渲染 user prompt（judge_variable_config 驱动）

        W8 修复对齐现行契约：裁判 prompt 重构后 user prompt 不再取请求里的
        user_prompt_template 模板字段（该字段当前被端点接受但不参与渲染，
        见 concerns），而是渲染 judge_variable_config 中 prompt_type="user"
        的变量（与 tests/unit/test_prompt_builder_from_config.py 的契约一致）。
        """
        response = client.post(
            "/api/v1/evaluation/judge-config/preview",
            json={
                "system_prompt_template": "你是一个裁判，请评测：{% for t in targets %}{{ t.name }}{% endfor %}",
                "user_prompt_template": "{% for t in targets %}{{ t.name }}: {{ t.content }}\n{% endfor %}请评分",
                "targets": [
                    {
                        "id": "t1",
                        "name": "model_output",
                        "source": "model_output",
                        "display_name": "模型输出",
                        "required": True
                    }
                ],
                "dataset_item": {},
                "model_result": {"content": "模型输出内容"},
                "judge_variable_config": [
                    {"key": "model_out", "name": "模型输出", "source": "model_output", "prompt_type": "user"},
                ],
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "rendered_system_prompt" in data
        assert "rendered_user_prompt" in data
        assert "模型输出" in data["rendered_system_prompt"]
        assert "模型输出" in data["rendered_user_prompt"]
        assert "模型输出内容" in data["rendered_user_prompt"]

    def test_preview_with_empty_target_returns_validation_error(self):
        """测试 POST preview 检测到空必填目标时返回校验错误"""
        response = client.post(
            "/api/v1/evaluation/judge-config/preview",
            json={
                "system_prompt_template": "你是一个裁判",
                "user_prompt_template": "请评分",
                "targets": [
                    {
                        "id": "t1",
                        "name": "model_output",
                        "source": "model_output",
                        "display_name": "模型输出",
                        "required": True
                    }
                ],
                "dataset_item": {},
                "model_result": {"content": ""}  # 空内容
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_valid"] is False
        assert data["validation_results"] is not None
        assert len(data["validation_results"]) > 0

    def test_validate_returns_validation_results(self):
        """测试 POST validate 返回正确的校验结果列表"""
        response = client.post(
            "/api/v1/evaluation/judge-config/validate",
            json={
                "targets": [
                    {
                        "id": "t1",
                        "name": "model_output",
                        "source": "model_output",
                        "display_name": "模型输出",
                        "required": True
                    },
                    {
                        "id": "t2",
                        "name": "user_input",
                        "source": "dataset_field",
                        "source_config": {"field": "question"},
                        "display_name": "用户问题",
                        "required": True
                    }
                ],
                "dataset_item": {"question": "这是问题"},
                "model_result": {"content": "这是输出"}
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_valid"] is True
        assert len(data["validation_results"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])