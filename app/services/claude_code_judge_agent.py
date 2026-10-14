"""
Claude Code Agent 裁判执行器

以 claude-agent-sdk 驱动 Claude Code CLI 完成裁判评分：
- 支持超长会话：会话内容渲染为 markdown 文件，agent 用 Read/Grep 工具自行阅读
- 提示词参考现有裁判图结构（整体分析 → 逐维度评分 → 汇总），单次会话完成
- 返回格式与 JudgeLangGraphAgent 兼容：[{score, feedback, dimension_scores, langfuse_trace_*}]
"""
import asyncio
import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# 裁判 agent 允许使用的工具（只读，保证评分过程不产生副作用）
JUDGE_ALLOWED_TOOLS = ["Read", "Grep", "Glob"]

OUTPUT_PROTOCOL_TEMPLATE = """
---
## 待评测会话文件
待评测的完整会话记录已保存到本地文件：{session_file}
请使用 Read 工具阅读该文件（文件较长时分段阅读，直到读完），必要时可用 Grep 检索关键内容。

## 输出要求
请先在心里完成整体分析与逐维度评分，然后最终回复【只包含一个 JSON 对象】，不要输出任何其他文字：
{{"score": <总分，0 到 {max_score} 的数字>, "feedback": "<整体评价与依据，200字以内>", "dimension_scores": {{<维度名>: <该维度得分数字>, ...}}}}
必须评分的维度：{dimension_names}
"""


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从裁判回复文本中宽容提取 JSON 对象

    依次尝试：去代码块围栏后直接解析 → 扫描首个平衡的 {...} 片段。
    """
    if not text:
        return None
    cleaned = text.strip()
    # 剥离 markdown 代码块围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    # 扫描首个平衡的花括号片段
    depth = 0
    start = -1
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(cleaned[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, TypeError):
                        start = -1
    return None


class ClaudeCodeJudgeAgent:
    """Claude Code Agent 裁判（多裁判并发与 JudgeLangGraphAgent 对齐）"""

    def __init__(self, task, langfuse_host: str = ""):
        self.task = task
        self.langfuse_host = langfuse_host

    def run_parallel(
        self,
        system_prompt: str,
        user_prompt: str,
        session_file: Optional[str],
        judge_count: int,
    ) -> List[Dict[str, Any]]:
        """并发执行 N 个 Claude Code Agent 裁判

        Args:
            system_prompt: PromptBuilder 构建的裁判 system prompt
            user_prompt: PromptBuilder 构建的 user prompt（含评测变量内容）
            session_file: 会话 markdown 文件路径；None 表示内容已在 user_prompt 中
            judge_count: 裁判数量（每个裁判独立 agent 会话）
        """
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        config = ClaudeCodeConfigService.get_active_config()
        if not config:
            raise RuntimeError("未配置 Claude Code，请先在「模型配置」页保存 Claude Code 全局配置")

        judge_count = max(judge_count, 1)
        max_workers = min(judge_count, 4)
        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._run_single, config, system_prompt, user_prompt, session_file, i
                ): i
                for i in range(judge_count)
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error(f"[CCJudge] 并行执行中裁判 {futures[future]} 失败: {e}")
                    # W2-1 失败显式标记：score 置 None + judge_failed=True，
                    # 行级聚合据此剔除出均分（与 JudgeLangGraphAgent 口径一致）
                    results.append({
                        "score": None,
                        "feedback": f"Claude Code 裁判执行失败: {e}",
                        "dimension_scores": {},
                        "judge_failed": True,
                        "fail_reason": "llm_error",
                    })

        # W2-1: 失败裁判 score 为 None，日志均值只统计有效分数（与聚合口径一致）
        valid_scores = [
            r["score"] for r in results
            if not r.get("judge_failed") and isinstance(r.get("score"), (int, float))
        ]
        avg_text = f"{sum(valid_scores) / len(valid_scores):.2f}" if valid_scores else "N/A"
        logger.info(
            f"[CCJudge] {judge_count} 个 Claude Code 裁判完成，"
            f"失败 {len(results) - len(valid_scores)} 个，平均分: {avg_text}"
        )
        return results

    def _run_single(
        self,
        config: Dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        session_file: Optional[str],
        judge_index: int,
    ) -> Dict[str, Any]:
        trace_id, trace_url, trace = None, None, None
        if self.langfuse_host:
            trace_id = uuid.uuid4().hex
            trace_url = f"{self.langfuse_host.rstrip('/')}/trace/{trace_id}"
            trace = self._start_trace(trace_id, judge_index)

        try:
            final_prompt = self._build_final_prompt(user_prompt, session_file)
            result = self._query_agent(config, system_prompt, final_prompt)
            if trace:
                self._end_trace(trace, system_prompt, final_prompt, result)
            if trace_id:
                result["langfuse_trace_id"] = trace_id
                result["langfuse_trace_url"] = trace_url
            return result
        except Exception as e:
            logger.exception(f"[CCJudge] 裁判 {judge_index} 执行失败")
            if trace:
                try:
                    trace.update(output={"error": str(e)})
                except Exception:
                    pass
            # W2-1 失败显式标记（与 run_parallel 异常分支口径一致）；
            # harness 两次 JSON 解析失败抛 "无法解析为 JSON" → parse_failed，其余为 llm_error
            fail_reason = "parse_failed" if "无法解析为 JSON" in str(e) else "llm_error"
            result = {
                "score": None,
                "feedback": f"Claude Code 裁判执行失败: {e}",
                "dimension_scores": {},
                "judge_failed": True,
                "fail_reason": fail_reason,
            }
            if trace_id:
                result["langfuse_trace_id"] = trace_id
                result["langfuse_trace_url"] = trace_url
            return result

    def _build_final_prompt(self, user_prompt: str, session_file: Optional[str]) -> str:
        """在 user prompt 基础上拼接会话文件说明与输出协议"""
        metrics = getattr(self.task, "evaluation_metrics", []) or []
        dimension_names = [m.get("name", "") for m in metrics if m.get("name")]
        max_score = getattr(self.task, "max_score", 10) or 10
        parts = [user_prompt.rstrip()]
        if session_file:
            parts.append(
                OUTPUT_PROTOCOL_TEMPLATE.format(
                    session_file=session_file,
                    max_score=max_score,
                    dimension_names="、".join(dimension_names) or "（按提示词中的维度）",
                )
            )
        else:
            parts.append(
                f"\n---\n## 输出要求\n"
                f"请先完成整体分析与逐维度评分，最终回复【只包含一个 JSON 对象】，不要输出其他文字：\n"
                f'{{"score": <总分，0 到 {max_score} 的数字>, '
                f'"feedback": "<整体评价，200字以内>", '
                f'"dimension_scores": {{<维度名>: <得分数字>, ...}}}}\n'
                f"必须评分的维度：{'、'.join(dimension_names) or '（按提示词中的维度）'}"
            )
        return "\n".join(parts)

    def _query_agent(self, config: Dict[str, Any], system_prompt: str, final_prompt: str) -> Dict[str, Any]:
        """执行一次 agent 查询并解析结果（失败时带格式纠正重试一次）

        执行逻辑已收敛到 harness_adapter.ClaudeCodeAdapter（统一的 JSON 契约、
        重试策略与结果归一化），此处保留原有方法签名与行为，供 run_parallel
        与既有测试复用。
        """
        from app.services.harness_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter(config, task=self.task)
        return adapter.run_prompt(
            system_prompt,
            final_prompt,
            timeout=config.get("timeout_seconds") or 300,
        )

    def _normalize_result(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """归一化裁判输出（实现收敛到 harness_adapter.normalize_judge_result）"""
        from app.services.harness_adapter import normalize_judge_result

        return normalize_judge_result(parsed)

    def _start_trace(self, trace_id: str, judge_index: int):
        """Langfuse 上报（失败不影响评测）"""
        try:
            from langfuse import Langfuse

            lf = Langfuse()
            return lf.start_trace(
                id=trace_id,
                name=f"cc_judge_{judge_index}",
                metadata={
                    "task_id": str(getattr(self.task, "id", "")),
                    "judge_index": judge_index,
                    "judge_engine": "claude_agent",
                },
                tags=[f"judge_{judge_index}", "claude_code_agent"],
            )
        except Exception as e:
            logger.warning(f"[CCJudge] 创建 Langfuse trace 失败: {e}")
            return None

    def _end_trace(self, trace, system_prompt: str, final_prompt: str, result: Dict[str, Any]):
        try:
            generation = trace.start_generation(
                name="claude_code_agent_judge",
                input={
                    "system_prompt": system_prompt[:2000],
                    "user_prompt": final_prompt[:2000],
                },
                output=result,
            )
            generation.end()
            trace.update(output={"score": result.get("score")})
        except Exception as e:
            logger.warning(f"[CCJudge] Langfuse 上报失败: {e}")


def _run_async_with_timeout(coro_factory: Callable[[], Any], timeout: float) -> Any:
    """在独立线程执行 asyncio.run，主线程限时等待

    不能直接用 asyncio.wait_for：项目运行时会加载 nest_asyncio patch
    asyncio.run，此时 wait_for 内部的 timeouts.timeout 会抛
    "Timeout should be used inside a task"。线程级超时兼容该环境，
    超时后后台线程置为 daemon，不阻塞 worker 进程。
    """
    result_box: Dict[str, Any] = {}

    def runner():
        try:
            result_box["value"] = asyncio.run(coro_factory())
        except BaseException as e:  # noqa: BLE001 - 线程边界传递异常
            result_box["error"] = e

    thread = threading.Thread(target=runner, daemon=True, name="cc-judge-query")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"Claude Code 裁判调用超时（>{timeout:.0f}秒）")
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")


async def _run_with_prompt(options, prompt: str) -> str:
    """以指定 prompt 消费 SDK query 流，返回最终结果文本"""
    from claude_agent_sdk import query

    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if type(message).__name__ == "ResultMessage":
            final_text = str(getattr(message, "result", "") or "")
    return final_text


def _sanitize_path_segment(value: str) -> str:
    """路径段消毒：仅保留安全字符，防路径注入"""
    return re.sub(r"[^\w\-]", "_", str(value))[:50] or "id"


def write_session_file(task_id: str, data_id: str, session_markdown: str) -> str:
    """将会话 markdown 写入裁判会话临时目录，返回绝对路径

    目录：./uploads/judge_sessions/（与现有数据集上传目录同级约定）
    """
    base = Path("uploads/judge_sessions")
    base.mkdir(parents=True, exist_ok=True)
    safe_task_id = _sanitize_path_segment(task_id)
    safe_data_id = _sanitize_path_segment(data_id)
    path = base / f"{safe_task_id}_{safe_data_id}_{uuid.uuid4().hex[:6]}.md"
    path.write_text(session_markdown, encoding="utf-8")
    return str(path.resolve())


def cleanup_session_files(task_id: str):
    """评测结束后清理该任务的会话临时文件（execute_batch 结束兜底）"""
    base = Path("uploads/judge_sessions")
    if not base.exists():
        return
    safe_task_id = _sanitize_path_segment(task_id)
    for path in base.glob(f"{safe_task_id}_*.md"):
        try:
            path.unlink()
        except OSError:
            logger.warning(f"[CCJudge] 清理会话文件失败: {path}")


def cleanup_session_files_for_item(task_id: str, data_id: str):
    """单条数据评测完成后清理其会话临时文件（与 write_session_file 命名口径一致）"""
    base = Path("uploads/judge_sessions")
    if not base.exists():
        return
    pattern = f"{_sanitize_path_segment(task_id)}_{_sanitize_path_segment(data_id)}_*.md"
    for path in base.glob(pattern):
        try:
            path.unlink()
        except OSError:
            logger.warning(f"[CCJudge] 清理会话临时文件失败: {path}")
