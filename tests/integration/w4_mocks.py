"""W4 集成测试共享 mock —— 整改计划 §5.1「fake_judge_model」精神的 W4 落地

在 W3 固定打分表 mock 的基础上增加**配置捕获**（W4 断言核心）：
- mock ModelCallerSync.call：捕获每次调用收到的完整 config 到
  hooks["model_configs"]（断言 executor 实际发出的口径字段来自快照、
  凭据来自实时配置），返回固定模型输出；
- mock JudgeLangGraphAgent.run_parallel：捕获 state_input 里的
  judge_model_config 到 hooks["judge_configs"]，返回固定打分表。

固定打分表与 W3 相同口径（score=5+row%5），让结果聚合可精确断言。
"""
import re
from contextlib import ExitStack
from typing import Any, Dict
from unittest.mock import patch

_ROW_TAG_RE = re.compile(r"row-(\d{2})")


def expected_score(row: int) -> int:
    return 5 + (row % 5)


def expected_dimensions(row: int) -> Dict[str, float]:
    score = expected_score(row)
    return {"准确性": float(score), "完整性": round(score / 2, 1)}


def _row_from_text(text: Any) -> int | None:
    match = _ROW_TAG_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def _build_model_call_mock(hooks: Dict[str, Any]):
    def mock_call(messages, config, runtime_variables=None):
        content = " ".join(str(m.get("content", "")) for m in (messages or []))
        row = _row_from_text(content)
        hooks["model_calls"].append(row)
        # W4 核心：捕获 executor 实际发出的完整调用配置（深拷贝防后续篡改）
        hooks["model_configs"].append(dict(config or {}))
        return {
            "success": True,
            "content": f"W4 mock 模型输出 row-{row:02d}",
            "chain_results": [],
        }

    return mock_call


def _build_judge_mock(hooks: Dict[str, Any]):
    def mock_run_parallel(self, state_input, judge_count=None, **kwargs):
        # W4 核心：捕获裁判链路实际拿到的调用配置
        judge_cfg = (state_input or {}).get("judge_model_config") or {}
        hooks["judge_configs"].append(dict(judge_cfg))
        row = _row_from_text(str(state_input)) or 0
        count = judge_count or 1
        score = expected_score(row)
        return [
            {
                "score": score,
                "feedback": f"W4固定打分表反馈 row-{row:02d}",
                "dimension_scores": dict(expected_dimensions(row)),
            }
            for _ in range(max(int(count), 1))
        ]

    return mock_run_parallel


def install_w4_mocks(hooks: Dict[str, Any]):
    """安装固定打分表 + 配置捕获 mock（上下文管理器）

    hooks 需含键：model_calls / model_configs / judge_configs（list）。
    """
    from app.services.judge_langgraph_agent import JudgeLangGraphAgent
    from app.services.model_caller import ModelCallerSync

    stack = ExitStack()
    stack.enter_context(
        patch.object(ModelCallerSync, "call", staticmethod(_build_model_call_mock(hooks)))
    )
    stack.enter_context(
        patch.object(JudgeLangGraphAgent, "run_parallel", _build_judge_mock(hooks))
    )
    return stack
