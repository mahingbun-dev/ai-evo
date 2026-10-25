"""P0-D：演示面纯函数单测（mock LLM 应答分发 / 后端选型 / token 约束）

DB 相关路径（seed 幂等、权限、seed-and-run）由集成测试与浏览器验收覆盖，
此处只钉住零依赖的纯逻辑分支。
"""
import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.demo_api import _mock_reply
from app.services import demo_seed_service



pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）
def test_mock_reply_score_request_returns_judge_json():
    """裁判评分节点（prompt 要求只输出 JSON）必须拿到可解析的评分 JSON"""
    messages = [{"role": "user", "content": "请按维度打分，只输出 JSON：{\"score\",\"reasoning\"}"}]
    reply = _mock_reply(messages)
    assert reply.strip().startswith("{")
    assert "\"score\"" in reply and "\"reasoning\"" in reply


def test_mock_reply_plain_question_returns_demo_text():
    messages = [{"role": "user", "content": "什么是评测门禁？"}]
    reply = _mock_reply(messages)
    assert "演示回复" in reply
    assert "mock" in reply


def test_mock_reply_empty_messages_safe():
    assert "演示" in _mock_reply([]) or _mock_reply([]) == ""


def test_mock_endpoint_requires_demo_token():
    """固定演示 token 约束「按演示约定显式调用」：错 token 401、无 token 401"""
    from app.main import app

    client = TestClient(app)
    body = {"messages": [{"role": "user", "content": "hi"}]}
    r1 = client.post("/api/v1/demo/mock-llm/chat/completions", json=body)
    assert r1.status_code == 401
    r2 = client.post(
        "/api/v1/demo/mock-llm/chat/completions",
        json=body,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r2.status_code == 401
    r3 = client.post(
        "/api/v1/demo/mock-llm/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {demo_seed_service.DEMO_MOCK_TOKEN}"},
    )
    assert r3.status_code == 200
    assert r3.json()["choices"][0]["message"]["content"]


def test_resolve_demo_llm_backend_prefers_litellm(monkeypatch):
    """配置了 LiteLLM 网关时演示模型走网关（模型名固定 mimo-V2.6）"""
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.com/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "sk-demo")
    backend = demo_seed_service.resolve_demo_llm_backend()
    assert backend["endpoint"].startswith("https://litellm.example.com")
    assert backend["api_key"] == "sk-demo"
    assert backend["model_type"] == "mimo-V2.6"
    assert backend["mock"] is False


def test_resolve_demo_llm_backend_falls_back_to_mock(monkeypatch):
    """零凭据环境回退内置 mock 端点（固定演示 token）"""
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    backend = demo_seed_service.resolve_demo_llm_backend()
    assert backend["api_key"] == demo_seed_service.DEMO_MOCK_TOKEN
    assert backend["endpoint"].endswith("/api/v1/demo/mock-llm/chat/completions")



def test_demo_model_parse_config_shape_extracts_content():
    """E2E 回归钉：demo 模型的解析配置必须是平台正典结构（parse_type/parse_rule），
    否则 ResponseParser 拿整个响应 dict 当 content，裁判评分解析全挂。"""
    from app.services.response_parser import ResponseParser
    from app.services.demo_seed_service import resolve_demo_llm_backend

    mock_resp = {
        "id": "chatcmpl-aievo-demo",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "评分文本"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 0},
    }
    # 与 seed_demo_data 的 _model_config 相同形状
    parse_config = {
        "parse_type": "json_path",
        "parse_rule": {"content": "$.choices[0].message.content"},
    }
    assert ResponseParser.parse(mock_resp, parse_config) == "评分文本"
    # seed 源码不得再出现 ResponseParser 不读的错误 "path" 键
    import inspect
    from app.services import demo_seed_service
    assert '"path": "choices' not in inspect.getsource(demo_seed_service)


def test_demo_task_targets_satisfy_contract_v3():
    """E2E 回归钉：演示任务必须带 evaluation_targets（契约 v3 §五 judge 必填）。
    旧版种子漏掉该行——任务能跑，但任何 PUT 局部更新（如配门禁）都会被
    「必须配置待评测数据」校验拦成 400。"""
    from app.services.demo_seed_service import DEMO_EVALUATION_TARGETS

    assert isinstance(DEMO_EVALUATION_TARGETS, list) and DEMO_EVALUATION_TARGETS
    row = DEMO_EVALUATION_TARGETS[0]
    assert row["source"] in ("model_output", "dataset_field", "manual_input", "chain_result")
    assert isinstance(row["source_config"], dict)
    assert row.get("required") is True


def test_task_detail_response_exposes_gate_config():
    """E2E 回归钉：任务详情响应必须含 gate_config（编辑弹窗 applyGateConfig 回填
    依据）。之前字段缺失导致已保存的门禁配置在详情接口恒为 undefined。"""
    from app.api.evaluation_task_api import TaskDetailResponse

    assert "gate_config" in TaskDetailResponse.model_fields
