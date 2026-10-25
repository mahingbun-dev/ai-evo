"""
根 conftest.py
"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
_root_dir = Path(__file__).parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

# ---------------------------------------------------------------------------
# ragas 0.4.3 在模块顶层硬 import langchain_community.chat_models.vertexai，
# 而锁定版 langchain-community（0.4.2）已把 vertexai 支持拆去独立包——
# 测试环境（本地/CI）不装 google 全家桶，ragas 导入即 ModuleNotFoundError。
# 产品代码（rag_evaluator）的 ragas 路径是 try/except 惰性可选导入，不受
# 影响；只有单测直接 import ragas（test_judge_pool_call_sites 等）。此处
# 注入自守桩：模块缺失时才生效，ChatVertexAI 只会在 ragas llm_factory
# 显式选用时实例化，测试从不走该分支。
# ---------------------------------------------------------------------------
try:
    import langchain_community.chat_models.vertexai  # noqa: F401
except ModuleNotFoundError:
    import types

    _vertexai_stub = types.ModuleType("langchain_community.chat_models.vertexai")

    class _ChatVertexAIStub:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("vertexai 桩：测试环境未安装 google-cloud-aiplatform")

    _vertexai_stub.ChatVertexAI = _ChatVertexAIStub
    sys.modules["langchain_community.chat_models.vertexai"] = _vertexai_stub