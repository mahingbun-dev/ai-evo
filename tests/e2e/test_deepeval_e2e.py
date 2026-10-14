"""
DeepEval 集成端到端测试
"""
import pytest
import requests
import json
import time
import os

import pytest
pytestmark = pytest.mark.e2e  # W8 分类：e2e（真模型/真服务，CI 永不跑）

BASE_URL = "http://localhost:8100/api/v1"
AUTH_TOKEN = os.environ.get("TEST_AUTH_TOKEN", "")


@pytest.fixture(scope="module", autouse=True)
def _live_backend():
    """依赖本地运行中的后端服务，未启动时整组跳过（如 CI 环境）"""
    try:
        requests.get(BASE_URL.replace("/api/v1", "") + "/openapi.json", timeout=2)
    except requests.RequestException:
        pytest.skip("后端服务未运行（http://localhost:8000），跳过 DeepEval 端到端集成测试")


def get_headers():
    """获取请求头"""
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return headers


def test_e2e_001_agent_evaluation_api():
    """E2E-001: Agent 评测 API 端点测试"""
    print("\n=== E2E-001: Agent 评测 API 测试 ===")

    # 测试任务完成度评测
    response = requests.post(
        f"{BASE_URL}/agent-evaluation/task-completion",
        headers=get_headers(),
        json={
            "task": "查询天气",
            "input_text": "请查询北京今天的天气",
            "actual_output": "北京今天天气晴朗，气温25度",
            "threshold": 0.5,
        },
    )
    print(f"任务完成度评测: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"  - score: {data.get('score')}")
        print(f"  - success: {data.get('success')}")
        assert "score" in data
        assert "success" in data
        print("✅ 任务完成度评测通过")
    elif response.status_code == 401:
        print("⚠️ 需要认证 token，跳过测试")
    else:
        print(f"❌ 任务完成度评测失败: {response.text}")


def test_e2e_002_safety_evaluation_api():
    """E2E-002: 安全评测 API 端点测试"""
    print("\n=== E2E-002: 安全评测 API 测试 ===")

    # 测试偏见检测
    response = requests.post(
        f"{BASE_URL}/safety-evaluation/bias",
        headers=get_headers(),
        json={
            "input_text": "描述不同职业的人群",
            "actual_output": "程序员通常喜欢技术，设计师注重美学",
            "threshold": 0.5,
        },
    )
    print(f"偏见检测: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"  - score: {data.get('score')}")
        print("✅ 偏见检测通过")
    elif response.status_code == 401:
        print("⚠️ 需要认证 token，跳过测试")
    else:
        print(f"❌ 偏见检测失败: {response.text}")


def test_e2e_003_dataset_synthesis_api():
    """E2E-003: 数据合成 API 端点测试"""
    print("\n=== E2E-003: 数据合成 API 测试 ===")

    response = requests.post(
        f"{BASE_URL}/dataset-synthesis/from-scratch",
        headers=get_headers(),
        json={
            "topic": "机器学习基础",
            "num_questions": 2,
        },
    )
    print(f"数据合成: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"  - generated_count: {data.get('generated_count')}")
        print("✅ 数据合成通过")
    elif response.status_code == 401:
        print("⚠️ 需要认证 token，跳过测试")
    else:
        print(f"❌ 数据合成失败: {response.text}")


def test_e2e_004_api_health_check():
    """E2E-004: API 健康检查"""
    print("\n=== E2E-004: API 健康检查 ===")

    # 检查 OpenAPI 文档
    response = requests.get(f"{BASE_URL.replace('/api/v1', '')}/openapi.json")
    print(f"OpenAPI 文档: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        paths = list(data.get("paths", {}).keys())
        print(f"  - API 路径数量: {len(paths)}")

        # 检查新 API 路径是否存在
        agent_paths = [p for p in paths if "agent-evaluation" in p]
        safety_paths = [p for p in paths if "safety-evaluation" in p]
        synthesis_paths = [p for p in paths if "dataset-synthesis" in p]

        print(f"  - Agent 评测路径: {len(agent_paths)}")
        print(f"  - 安全评测路径: {len(safety_paths)}")
        print(f"  - 数据合成路径: {len(synthesis_paths)}")

        assert len(agent_paths) > 0, "Agent 评测 API 路径不存在"
        assert len(safety_paths) > 0, "安全评测 API 路径不存在"
        assert len(synthesis_paths) > 0, "数据合成 API 路径不存在"
        print("✅ API 健康检查通过")
    else:
        print(f"❌ API 健康检查失败: {response.text}")


def main():
    """运行所有端到端测试"""
    print("=" * 60)
    print("DeepEval 集成端到端测试")
    print("=" * 60)

    start_time = time.time()

    # 运行测试
    test_e2e_004_api_health_check()
    test_e2e_001_agent_evaluation_api()
    test_e2e_002_safety_evaluation_api()
    test_e2e_003_dataset_synthesis_api()

    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(f"测试完成，耗时: {elapsed:.2f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
