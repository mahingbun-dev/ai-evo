"""
Harness 适配层：裁判引擎统一执行契约

设计：docs/design/pre-judge-scripts.md 第四节（四种插件类型）+ 第九节（9.2 裁判引擎架构）

- HarnessAdapter：抽象基类，统一「跑指令 → 收 JSON」的执行模板：
  输入文件物化为临时文件 → 最终 prompt 组装 → 子类执行 agent 拿原始回复 →
  宽容 JSON 提取（失败带格式纠正指令重试一次）→ 结果归一化
- ClaudeCodeAdapter：claude-agent-sdk 实现（执行逻辑收敛自 ClaudeCodeJudgeAgent，
  后者对外接口保持不变，见 app/services/claude_code_judge_agent.py）
- DeepSeekAdapter：deepseek-harness-sdk 实现（见 app/services/deepseek_adapter.py）

统一返回结构（与 JudgeLangGraphAgent 裁判结果兼容，另加 finish_reason）：
  {score, feedback, dimension_scores, finish_reason}
  finish_reason 语义对齐 DSH RunResult：completed | max_turns | timeout | error
"""
import logging
import shutil
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

# 宽容 JSON 提取与 CC SDK 调用辅助复用自既有 Claude Code 裁判模块（单向依赖，无循环导入）
from app.services.claude_code_judge_agent import (
    JUDGE_ALLOWED_TOOLS,
    _run_async_with_timeout,
    _sanitize_path_segment,
    extract_json_object,
)

logger = logging.getLogger(__name__)


def run_sync_with_timeout(callable_: Callable[[], Any], timeout: float, thread_name: str = "harness-call") -> Any:
    """在独立线程执行同步调用，主线程限时等待

    超时后后台线程置为 daemon 不阻塞 worker 进程；线程边界通过
    result_box 传递返回值与异常。与 claude_code_judge_agent._run_async_with_timeout
    同一套方案（兼容 nest_asyncio patch asyncio.run 的生产环境）。
    """
    result_box: Dict[str, Any] = {}

    def runner():
        try:
            result_box["value"] = callable_()
        except BaseException as e:  # noqa: BLE001 - 线程边界传递异常
            result_box["error"] = e

    thread = threading.Thread(target=runner, daemon=True, name=thread_name)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"Harness 调用超时（>{timeout:.0f}秒）")
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")


def normalize_judge_result(parsed: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
    """归一化裁判输出：score 数值化、dimension_scores 过滤非法值、finish_reason 兜底"""
    score = parsed.get("score", 0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    dim_scores: Dict[str, float] = {}
    for name, value in (parsed.get("dimension_scores") or {}).items():
        if isinstance(value, (int, float)):
            dim_scores[str(name)] = float(value)
        else:
            try:
                dim_scores[str(name)] = float(value)
            except (TypeError, ValueError):
                continue
    return {
        "score": score,
        "feedback": str(parsed.get("feedback", "")),
        "dimension_scores": dim_scores,
        "finish_reason": finish_reason or "completed",
    }


class HarnessAdapter(ABC):
    """Harness 抽象基类：跑指令 → 收 JSON（宽容解析 + 一次格式纠正重试）

    子类差异只在凭据来源与「一次 agent 调用」的实现（_execute_raw），
    JSON 输出协议、重试策略、临时文件读写与清理由基类统一保证。
    """

    # 首次解析失败时附加的格式纠正指令（只重试一次）
    OUTPUT_CORRECTION_SUFFIX = (
        "\n\n---\n注意：你上一次的回复无法解析为 JSON。"
        "请重新评分，最终回复必须只包含一个 JSON 对象。"
    )

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        task: Any = None,
        langfuse_host: str = "",
    ):
        self.config = config or {}
        self.task = task
        self.langfuse_host = langfuse_host
        self._run_dir: Optional[Path] = None

    # ---------- 对外入口 ----------

    def run(
        self,
        system_prompt: str,
        user_prompt: str,
        files: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """标准执行入口

        Args:
            system_prompt: 系统 prompt（约束角色与输出协议）
            user_prompt: 用户 prompt（含评测变量内容）
            files: {文件名: 文本内容}；物化为临时文件后把绝对路径附进 prompt，
                agent 用 Read 工具自行阅读（大文件不进上下文），run 结束即清理
            timeout: 单次调用超时（秒）；缺省取 config.timeout_seconds 或 300

        Returns:
            {score, feedback, dimension_scores, finish_reason}
        """
        timeout = float(timeout or self._default_timeout())
        file_refs = self._materialize_files(files)
        try:
            final_prompt = self._build_final_prompt(user_prompt, file_refs)
            return self.run_prompt(system_prompt, final_prompt, timeout)
        finally:
            self._cleanup_run_dir()

    def run_prompt(
        self, system_prompt: str, final_prompt: str, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """已拼接输出协议的 prompt 执行入口（宽容 JSON 提取 + 一次格式纠正重试）

        ClaudeCodeJudgeAgent 等已有「自行组装最终 prompt」的调用方走此入口。
        """
        timeout = float(timeout or self._default_timeout())
        raw, finish_reason = self._execute_raw(system_prompt, final_prompt, timeout)
        parsed = extract_json_object(raw)
        if parsed is None:
            logger.warning(f"[{self._log_tag}] 首次解析失败，附加格式纠正指令重试一次")
            retry_prompt = f"{final_prompt}{self.OUTPUT_CORRECTION_SUFFIX}"
            raw, finish_reason = self._execute_raw(system_prompt, retry_prompt, timeout)
            parsed = extract_json_object(raw)
        if parsed is None:
            raise RuntimeError(f"裁判回复无法解析为 JSON: {raw[:200]}")
        return normalize_judge_result(parsed, finish_reason)

    # ---------- 子类扩展点 ----------

    @abstractmethod
    def _execute_raw(self, system_prompt: str, prompt: str, timeout: float) -> Tuple[str, Optional[str]]:
        """执行一次 agent 调用

        Args:
            system_prompt: 系统 prompt（无独立 system 概念的 harness 自行拼接）
            prompt: 最终任务 prompt（已含输出协议与文件路径）
            timeout: 强制请求超时（秒）

        Returns:
            (原始回复文本, finish_reason)；失败抛异常（TimeoutError/其他）
        """

    def _default_timeout(self) -> float:
        return self.config.get("timeout_seconds") or 300

    @property
    def _log_tag(self) -> str:
        return type(self).__name__

    # ---------- 输入文件物化与清理 ----------

    def _build_final_prompt(self, user_prompt: str, file_refs: Optional[Dict[str, str]] = None) -> str:
        parts = [user_prompt.rstrip()]
        if file_refs:
            listing = "\n".join(f"- {name}: {path}" for name, path in file_refs.items())
            parts.append(
                f"\n---\n## 输入文件（本地绝对路径，使用 Read 工具阅读，较长时分段读完）\n{listing}"
            )
        return "\n".join(parts)

    def _materialize_files(self, files: Optional[Dict[str, str]]) -> Dict[str, str]:
        """把 {文件名: 内容} 写入本次运行的临时目录，返回 {文件名: 绝对路径}"""
        if not files:
            return {}
        base = Path("uploads/harness_runs") / uuid.uuid4().hex[:8]
        base.mkdir(parents=True, exist_ok=True)
        self._run_dir = base
        refs: Dict[str, str] = {}
        for name, content in files.items():
            safe_name = _sanitize_path_segment(name)
            path = base / f"{safe_name}_{len(refs)}"
            path.write_text(str(content), encoding="utf-8")
            refs[str(name)] = str(path.resolve())
        return refs

    def _cleanup_run_dir(self):
        if self._run_dir is not None:
            shutil.rmtree(self._run_dir, ignore_errors=True)
            self._run_dir = None


class ClaudeCodeAdapter(HarnessAdapter):
    """Claude Code Agent 适配器（claude-agent-sdk）

    执行逻辑收敛自 ClaudeCodeJudgeAgent._query_agent；ClaudeCodeJudgeAgent
    对外接口（run_parallel / _build_final_prompt / 会话文件机制）保持不变。
    """

    def _execute_raw(self, system_prompt: str, prompt: str, timeout: float) -> Tuple[str, Optional[str]]:
        from claude_agent_sdk import ClaudeAgentOptions

        from app.services.claude_code_config_service import ClaudeCodeConfigService

        options_kwargs: Dict[str, Any] = {}
        # 脚本 MCP 工具化（设计 9.3）：开关开启时把内置前处理脚本以 stdio MCP
        # server 注入裁判 agent（self.config 即 get_active_config() 的返回 dict，
        # 直接读开关，避免运行中二次查库；SDK 0.2.163 支持 mcp_servers 参数）
        if self.config.get("enable_script_mcp"):
            from app.services.script_mcp_server import MCP_SERVER_KEY, get_stdio_server_config

            options_kwargs["mcp_servers"] = {MCP_SERVER_KEY: get_stdio_server_config()}

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=JUDGE_ALLOWED_TOOLS,
            max_turns=self.config.get("max_turns") or 30,
            model=self.config["model"],
            env=ClaudeCodeConfigService.get_sdk_env(),
            # 不加载宿主机用户/项目 settings，保证评测环境纯净
            setting_sources=[],
            **options_kwargs,
        )
        # 函数级导入：保证既有测试对 claude_code_judge_agent._run_with_prompt 的 patch 生效
        from app.services.claude_code_judge_agent import _run_with_prompt

        raw = _run_async_with_timeout(lambda: _run_with_prompt(options, prompt), timeout)
        if not raw:
            raise RuntimeError("Claude Code 裁判无返回内容")
        return raw, "completed"
