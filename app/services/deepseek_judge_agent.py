"""DeepSeek Agent 裁判执行器（M8 QE8-2，judge_engine=deepseek_agent 任务流接通）

以 deepseek-harness-sdk（经 app/services/deepseek_adapter.DeepSeekAdapter）驱动
DeepSeek Harness 完成裁判评分。对外契约与 ClaudeCodeJudgeAgent 完全一致
（run_parallel 同签名、judge_scores 同结构），评测执行器按 task.judge_engine
无差别分发；适配器统一保证 JSON 输出协议、宽容解析 + 一次格式纠正重试与
结果归一化（见 harness_adapter.HarnessAdapter.run_prompt）。

与 CC 裁判的差异（本模块的职责边界）：
- 会话阅读：CC 裁判把超长会话落盘、用 Read 工具分段阅读；DSH 裁判把会话
  内容**内联**进 prompt（DSH 普通轮次无 CC 式文件阅读协议保证），超过
  SESSION_INLINE_LIMIT_CHARS 时截断并在 prompt 内标注——超长会话评测建议
  优先选 CC 引擎，本边界同步进 docs/user_guide 裁判引擎口径；
- 失败标记：与 CC 同口径（score=None + judge_failed=True + fail_reason，
  行级聚合据此剔除出均分），复用 W2-1 语义。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.metric_scoring import format_score_range, metric_max_score

logger = logging.getLogger(__name__)

# 内联会话上限（字符）：DSH 裁判把会话内容并入 prompt，超限截断并标注。
# CC 引擎无此限制（Read 工具分段阅读），超长会话任务建议选 CC 引擎
SESSION_INLINE_LIMIT_CHARS = 150_000

OUTPUT_PROTOCOL_TEMPLATE = """
---
## 输出要求
请先在心里完成整体分析与逐维度评分，然后最终回复【只包含一个 JSON 对象】，不要输出任何其他文字：
{{"score": <总分，0 到 {max_score} 的数字>, "feedback": "<整体评价与依据，200字以内>", "dimension_scores": {{<维度名>: <该维度得分数字>, ...}}, "dimension_feedbacks": {{"<维度名>": "<该维度评分理由，100字以内>", ...}}}}
参与评分的维度及各自最高分：{dimension_specs}。每个维度的得分不得超过该维度自己的最高分。
"""


class DeepSeekJudgeAgent:
    """DeepSeek Agent 裁判（多裁判并发与 ClaudeCodeJudgeAgent 对齐）"""

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
        """并发执行 N 个 DeepSeek Agent 裁判（签名与 ClaudeCodeJudgeAgent 一致）

        Args:
            system_prompt: PromptBuilder 构建的裁判 system prompt（DSH 无独立
                system 通道，适配器拼进任务 prompt 前部）
            user_prompt: PromptBuilder 构建的 user prompt（含评测变量内容）
            session_file: 会话 markdown 文件路径；内容内联进 prompt（见模块
                docstring 的边界说明）；None 表示内容已在 user_prompt 中
            judge_count: 裁判数量（每个裁判独立 harness 会话）
        """
        from app.services.deepseek_adapter import DeepSeekAdapter
        from app.services.deepseek_harness_config_service import (
            DeepSeekHarnessConfigService,
        )

        config = DeepSeekHarnessConfigService.get_active_config()
        if not config:
            raise RuntimeError(
                "未配置 DeepSeek Harness，请先在「模型配置」页保存 DeepSeek Harness 全局配置"
            )
        # 配置缺失凭据等硬伤在适配器构造期暴露（与 CC 裁判未配置即 RuntimeError 同口径）
        adapter = DeepSeekAdapter(config, task=self.task)

        judge_count = max(judge_count, 1)
        max_workers = min(judge_count, 4)
        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._run_single, adapter, config, system_prompt, user_prompt,
                    session_file, i,
                ): i
                for i in range(judge_count)
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error(
                        f"[DSHJudge] 并行执行中裁判 {futures[future]} 失败: {e}")
                    # W2-1 失败显式标记：score 置 None + judge_failed=True，
                    # 行级聚合据此剔除出均分（与 ClaudeCodeJudgeAgent 口径一致）
                    results.append({
                        "score": None,
                        "feedback": f"DeepSeek 裁判执行失败: {e}",
                        "dimension_scores": {},
                        "judge_failed": True,
                        "fail_reason": "llm_error",
                    })

        valid_scores = [
            r["score"] for r in results
            if not r.get("judge_failed") and isinstance(r.get("score"), (int, float))
        ]
        avg_text = (
            f"{sum(valid_scores) / len(valid_scores):.2f}" if valid_scores else "N/A")
        logger.info(
            f"[DSHJudge] {judge_count} 个 DeepSeek 裁判完成，"
            f"失败 {len(results) - len(valid_scores)} 个，平均分: {avg_text}"
        )
        return results

    def _run_single(
        self,
        adapter,
        config: Dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        session_file: Optional[str],
        judge_index: int,
    ) -> Dict[str, Any]:
        try:
            final_prompt = self._build_final_prompt(user_prompt, session_file)
            return adapter.run_prompt(
                system_prompt,
                final_prompt,
                timeout=config.get("timeout_seconds") or 300,
            )
        except Exception as e:
            logger.exception(f"[DSHJudge] 裁判 {judge_index} 执行失败")
            # 与 run_parallel 异常分支同口径；适配器两次 JSON 解析失败抛
            # "无法解析为 JSON" → parse_failed，其余为 llm_error
            fail_reason = "parse_failed" if "无法解析为 JSON" in str(e) else "llm_error"
            return {
                "score": None,
                "feedback": f"DeepSeek 裁判执行失败: {e}",
                "dimension_scores": {},
                "judge_failed": True,
                "fail_reason": fail_reason,
            }

    def _build_final_prompt(self, user_prompt: str, session_file: Optional[str]) -> str:
        """在 user prompt 基础上内联会话内容与输出协议

        会话文件内容直接并入 prompt（DSH 裁判不走 CC 的 Read 文件阅读机制），
        超过 SESSION_INLINE_LIMIT_CHARS 时截断并在 prompt 内显式标注。
        """
        metrics = getattr(self.task, "evaluation_metrics", []) or []
        max_score = getattr(self.task, "max_score", 10) or 10
        dimension_specs = "、".join(
            f"{m.get('name', '')}(最高分{format_score_range(metric_max_score(m, max_score))})"
            for m in metrics
            if m.get("name")
        ) or "（按提示词中的维度）"

        parts = [user_prompt.rstrip()]
        if session_file:
            try:
                session_text = Path(session_file).read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(f"[DSHJudge] 读取会话文件失败: {session_file} ({e})")
                session_text = ""
            if len(session_text) > SESSION_INLINE_LIMIT_CHARS:
                session_text = (
                    session_text[:SESSION_INLINE_LIMIT_CHARS]
                    + f"\n\n…（会话过长，已截断，原文 {len(session_text)} 字符；"
                      "超长会话建议使用 Claude Code Agent 引擎）"
                )
            if session_text:
                parts.append(f"\n---\n## 待评测会话记录（全文内联）\n{session_text}")
        parts.append(
            OUTPUT_PROTOCOL_TEMPLATE.format(
                max_score=max_score, dimension_specs=dimension_specs)
        )
        return "\n".join(parts)
