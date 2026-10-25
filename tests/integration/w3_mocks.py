"""W3 集成测试共享 mock —— 整改计划 §5.1「fake_judge_model」的落地

按「固定打分表」返回，让行级落库/断点续跑测试完全确定、零网关依赖：
- mock ModelCallerSync.call：从消息内容中的 row-NN 标记定位数据行，
  返回固定模型输出；
- mock JudgeLangGraphAgent.run_parallel：从 state_input 中的 row-NN 标记
  定位数据行，返回固定打分表（score/feedback/dimension_scores）。

固定打分表（judge_count=1 时行平均分即该值）：
    score(row)          = 5 + (row % 5)          # 5..9
    dimensions(row)     = {"准确性": score, "完整性": round(score/2, 1)}

行为钩子（通过 hooks 字典注入，全部可选）：
- model_calls：list，真实发起模型调用的行号记录（「续跑不重复计费」的证据）；
- row_delay_ms：每行模型调用前 sleep——**停止/被杀类场景必须设置**：
  真实评测每行是秒级 LLM 调用、行级落库 ~10ms，主线程总能跟上 as_completed
  循环；mock 行是微秒级，worker 会先于主线程跑完并置位停止信号，导致后续
  future 在主线程处理前就被跳过（时序与真实场景相反，断言不可复现）；
- call_log_path：每行模型调用后立刻追加一行 "row-NN" 到文件
  （子进程被 SIGKILL 后父进程仍可读取已评行）；
- stop_event + stop_after_rows：第 stop_after_rows 行裁判打分完成后置位
  停止信号（该行完整落库，后续行不再执行）；
- soft_limit_on_progress：主线程进度更新点注入 SoftTimeLimitExceeded
  （真实 celery 的软超时信号只在主线程抛出，worker 线程收不到；注入在
  EvaluationExecutor._update_progress——execute_batch 行循环主线程调用点，
  忠实模拟该信号）。

用法（pytest 与子进程共用同一入口，保证行为一致）：
    with install_w3_mocks(hooks):
        run_evaluation_async(task_id, stop_event)
"""
import re
import threading
import time
from contextlib import ExitStack
from typing import Any, Dict, Optional
from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded

_ROW_TAG_RE = re.compile(r"row-(\d{2})")
_thread_state = threading.local()


def expected_score(row: int) -> int:
    """固定打分表：行平均分（judge_count=1）"""
    return 5 + (row % 5)


def expected_feedback(row: int) -> str:
    return f"W3固定打分表反馈 row-{row:02d}"


def expected_dimensions(row: int) -> Dict[str, float]:
    score = expected_score(row)
    return {"准确性": float(score), "完整性": round(score / 2, 1)}


def _row_from_text(text: Any) -> Optional[int]:
    match = _ROW_TAG_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def _build_model_call_mock(hooks: Dict[str, Any]):
    def mock_call(messages, config, runtime_variables=None):
        content = " ".join(str(m.get("content", "")) for m in (messages or []))
        row = _row_from_text(content)
        if row is None:
            return {"success": False, "error": "W3 mock：消息中缺少 row-NN 标记"}
        hooks["model_calls"].append(row)
        _thread_state.current_row = row
        delay = hooks.get("row_delay_ms", 0)
        if delay:
            time.sleep(delay / 1000.0)
        log_path = hooks.get("call_log_path")
        if log_path:
            # 立即落盘：SIGKILL 后父进程仍能读到已评行
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"row-{row:02d}\n")
        return {
            "success": True,
            "content": f"W3 mock 模型输出 row-{row:02d}（确定性内容）",
            "chain_results": [],
        }

    return mock_call


def _build_judge_mock(hooks: Dict[str, Any], default_judge_count: int = 1):
    def mock_run_parallel(self, state_input, judge_count=None, **kwargs):
        row = getattr(_thread_state, "current_row", None)
        if row is None:
            row = _row_from_text(str(state_input))
        if row is None:
            raise AssertionError("W3 mock 裁判：无法定位数据行（缺 row-NN 标记）")

        # 停止钩子：本行打分完成后置位（该行会完整落库，后续行不再执行）
        stop_event = hooks.get("stop_event")
        stop_after = hooks.get("stop_after_rows")
        if stop_event is not None and stop_after is not None:
            hooks["judge_calls"].append(row)
            if len(hooks["judge_calls"]) >= stop_after:
                stop_event.set()

        count = judge_count or default_judge_count or 1
        score = expected_score(row)
        return [
            {
                "score": score,
                "feedback": expected_feedback(row),
                "dimension_scores": dict(expected_dimensions(row)),
            }
            for _ in range(max(int(count), 1))
        ]

    return mock_run_parallel


def install_w3_mocks(hooks: Optional[Dict[str, Any]] = None) -> ExitStack:
    """安装固定打分表 mock（ModelCallerSync.call + JudgeLangGraphAgent.run_parallel）

    返回 ExitStack，调用方 with 使用；pytest 内与子进程内行为完全一致。
    hooks 字典会被原地补充 model_calls / judge_calls 键（调用方可读取）。
    """
    hooks = hooks if hooks is not None else {}
    hooks.setdefault("model_calls", [])
    hooks.setdefault("judge_calls", [])

    stack = ExitStack()
    from app.services.model_caller import ModelCallerSync
    from app.services.judge_langgraph_agent import JudgeLangGraphAgent

    stack.enter_context(
        patch.object(ModelCallerSync, "call", staticmethod(_build_model_call_mock(hooks)))
    )
    stack.enter_context(
        patch.object(JudgeLangGraphAgent, "run_parallel", _build_judge_mock(hooks))
    )

    soft_limit_on_progress = hooks.get("soft_limit_on_progress")
    if soft_limit_on_progress is not None:
        from app.services.evaluation_executor import EvaluationExecutor

        original_update_progress = EvaluationExecutor._update_progress

        def update_progress_with_soft_limit(self, completed, total, force=False):
            # P0-4 给 _update_progress 加了 force 关键字（末行强制刷新），
            # mock 需按新签名透传，否则 TypeError 会让软超时路径失真
            if completed >= soft_limit_on_progress:
                hooks["soft_limit_raised"] = True
                raise SoftTimeLimitExceeded()
            return original_update_progress(self, completed, total, force=force)

        stack.enter_context(
            patch.object(EvaluationExecutor, "_update_progress", update_progress_with_soft_limit)
        )

    return stack


def read_call_log(path: str):
    """读取子进程模型调用日志（SIGKILL 后仍可读）"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return [
                int(m.group(1))
                for line in fh
                if (m := _ROW_TAG_RE.search(line))
            ]
    except FileNotFoundError:
        return []
