"""
Judge LangGraph Agent — 主类

提供:
1. Graph 实例缓存 (同一 task 的图只编译一次)
2. 单裁判执行 (run)
3. 多裁判并行执行 (run_parallel)
4. LangFuse 追踪集成
"""
import json
import logging
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from langgraph.graph.state import CompiledStateGraph
from app.services.judge_pool.context import capture_context, run_in_context
from app.services.judge_pool.errors import JudgePoolAborted, JudgePoolTimeout
from .judge_graph_builder import compile_judge_graph
from .judge_graph_state import JudgeAgentState
import uuid

logger = logging.getLogger(__name__)


class JudgeLangGraphAgent:
    """
    LangGraph 裁判 Agent。

    同一个 task 的 graph 实例可缓存复用，
    因为拓扑只由 evaluation_metrics 决定，不随数据变化。

    用法:
        agent = JudgeLangGraphAgent(task, langfuse_host="https://langfuse.example.com")
        results = agent.run_parallel(state_input, judge_count=6)
    """

    # 类级别缓存: {task_id: CompiledStateGraph}
    _graph_cache: Dict[str, CompiledStateGraph] = {}

    def __init__(
        self,
        task,
        langfuse_host: str = "",
    ):
        """
        Args:
            task: 评测任务对象
            langfuse_host: LangFuse 服务地址，用于构造 trace URL。为空时不创建 trace。
        """
        self.task = task
        self.langfuse_host = langfuse_host

        # 缓存 Graph
        cache_key = str(task.id)
        if cache_key not in self._graph_cache:
            self._graph_cache[cache_key] = compile_judge_graph(task)
        self.graph = self._graph_cache[cache_key]

    def run(
        self,
        state_input: JudgeAgentState,
        judge_index: int = 0,
    ) -> Dict[str, Any]:
        """
        执行单个裁判的完整评测流程。

        Args:
            state_input: 初始状态
            judge_index: 裁判索引 (用于 LangFuse 区分)

        Returns:
            裁判评分结果 (与 JudgeClient.judge() 兼容)
        """
        trace_id = None
        trace_url = None
        handler = None

        if self.langfuse_host:
            try:
                from langfuse.langchain import CallbackHandler

                trace_id = uuid.uuid4().hex  # 32位小写十六进制，LangFuse v4 要求的格式
                handler = CallbackHandler(trace_context={"trace_id": trace_id})
                trace_url = f"{self.langfuse_host.rstrip('/')}/trace/{trace_id}"
            except Exception as e:
                logger.warning(f"[JudgeAgent] 创建 LangFuse trace 失败: {e}")
                trace_id = None
                trace_url = None
                handler = None

        config = {
            "metadata": {
                "langfuse_session_id": f"{state_input['task_id']}_judge_{judge_index}",
                "langfuse_tags": [f"judge_{judge_index}", "langgraph_judge"],
            }
        }
        if handler:
            config["callbacks"] = [handler]

        try:
            result_state = self.graph.invoke(state_input, config)
        except JudgePoolAborted:
            # 池等待期间任务被停止：停止不是裁判失败，上抛走停止收尾
            # （不能被下面的宽 except 记成 judge_failed=True/llm_error）
            raise
        except JudgePoolTimeout:
            # 池满等待超时同样上抛：它要由行级逻辑落成「失败行」（携带额度耗尽的
            # 真实原因），被这里吞掉就只剩一个语义错误的 judge_failed/llm_error，
            # 运维据此判断不出是额度不够还是裁判模型报错
            raise
        except Exception as e:
            logger.exception(f"[JudgeAgent] 裁判 {judge_index} 执行失败")
            # W2-1 失败显式标记：score 置 None + judge_failed=True，
            # 由 _aggregate_judge_scores 剔除出均分（不再以 0 分拉低均值）
            result = {
                "score": None,
                "feedback": f"裁判执行失败: {str(e)}",
                "dimension_scores": {},
                "judge_failed": True,
                "fail_reason": "llm_error",
            }
            if trace_id:
                result["langfuse_trace_id"] = trace_id
                result["langfuse_trace_url"] = trace_url
            return result

        # format_output 将结果序列化为 raw_output
        raw_str = result_state.get("raw_output", "{}")
        try:
            result = json.loads(raw_str)
        except (json.JSONDecodeError, TypeError):
            result = {
                "score": result_state.get("final_score", 0),
                "feedback": result_state.get("final_feedback", ""),
                "dimension_scores": result_state.get("final_dimension_scores", {}),
            }
            # W2-1: 维度解析失败经 state 冒泡的失败标记（正常路径由 raw_output 携带）
            if result_state.get("final_judge_failed"):
                result["judge_failed"] = True
                result["fail_reason"] = result_state.get("final_fail_reason") or "parse_failed"

        if trace_id:
            result["langfuse_trace_id"] = trace_id
            result["langfuse_trace_url"] = trace_url
        return result

    def run_parallel(
        self,
        state_input: JudgeAgentState,
        judge_count: int,
    ) -> List[Dict[str, Any]]:
        """
        并发执行多个裁判。

        Args:
            state_input: 初始状态 (所有裁判共享)
            judge_count: 裁判数量

        Returns:
            裁判结果列表
        """
        judge_count = max(judge_count, 1)
        max_workers = min(judge_count, 4)  # 最多4线程
        results = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 每次 submit 单独复制任务上下文（同一 Context 不可被多线程并发进入）
            futures = {
                executor.submit(run_in_context, capture_context(), self.run, state_input, i): i
                for i in range(judge_count)
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except JudgePoolAborted:
                    # 停止信号导致的池中止不记裁判失败：上抛交给任务停止收尾
                    raise
                except JudgePoolTimeout:
                    # 池满等待超时上抛，由行级逻辑落失败行（口径同 run）
                    raise
                except Exception as e:
                    logger.error(f"[JudgeAgent] 并行执行中某个裁判失败: {e}")
                    # W2-1 失败显式标记（同 run 的异常分支口径）
                    results.append({
                        "score": None,
                        "feedback": f"执行失败: {e}",
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
            f"[JudgeAgent] {judge_count} 个裁判并行完成，"
            f"失败 {len(results) - len(valid_scores)} 个，平均分: {avg_text}"
        )
        return results

    @classmethod
    def clear_cache(cls, task_id: Optional[str] = None):
        """清理 Graph 缓存

        W12-1：图拓扑由 evaluation_metrics 决定并被类级缓存复用，任务编辑
        改动 evaluation_metrics / judge_prompt 后必须失效，否则重跑仍用
        旧评分标准的图（修「编辑维度后重跑用旧评分标准」）。

        Args:
            task_id: 指定任务 id 时仅清除该任务的缓存条目；缺省清空全部
                （保留无参调用兼容既有测试与兜底场景）
        """
        if task_id is None:
            cls._graph_cache.clear()
        else:
            cls._graph_cache.pop(str(task_id), None)
