"""
Harness 适配层：裁判引擎统一执行契约

设计：docs/design/pre-judge-scripts.md 第四节（四种插件类型）+ 第九节（9.2 裁判引擎架构）

- HarnessAdapter：抽象基类，统一「跑指令 → 收 JSON」的执行模板：
  输入文件物化为临时文件 → 最终 prompt 组装 → 子类执行 agent 拿原始回复 →
  宽容 JSON 提取（失败带格式纠正指令重试一次）→ 结果归一化
- ClaudeCodeAdapter：claude-agent-sdk 实现（执行逻辑收敛自 ClaudeCodeJudgeAgent，
  后者对外接口保持不变，见 app/services/claude_code_judge_agent.py）
- DeepSeekAdapter：deepseek-harness-sdk 实现（见 app/services/deepseek_adapter.py）
- 沙箱被测 agent 执行（ARCH-3-2，设计 §2.3 D8 / §3 表 3-2 行）：exec_type=
  claude_agent/deepseek_agent 的前处理步骤经 SandboxDriver 在沙箱内拉起被测
  agent——本模块负责 harness 语义（变体解析/指令文本/凭据与参数组装），
  docker 编排在 app/services/script_container.execute_harness_in_container，
  调度接线在 app/services/pre_judge_runner._execute_harness_agent

统一返回结构（与 JudgeLangGraphAgent 裁判结果兼容，另加 finish_reason）：
  {score, feedback, dimension_scores, dimension_feedbacks?, finish_reason}
  dimension_feedbacks 为可选字段：裁判未输出分维度理由或理由全部非法时不出现在
  结果中（旧消费方无感知）；存在时为 {维度名: 评分理由} 的非空字符串映射。
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
from app.services.judge_pool import judge_pool_slot

logger = logging.getLogger(__name__)

# claude-agent-sdk 以 --system-prompt argv 传 system_prompt；Linux execve 单个
# argv 字符串上限 131072B（MAX_ARG_STRLEN=32*PAGE_SIZE），超限即报
# [Errno 7] Argument list too long、CLI 启动失败。取 100KB 保守阈值：超限时
# system_prompt 降级并入首条消息（走 stdin 传输无上限），system_prompt 置 None
SYSTEM_PROMPT_ARGV_LIMIT_BYTES = 100_000


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
    """归一化裁判输出：score 数值化、dimension_scores 过滤非法值、
    dimension_feedbacks 仅保留键值均为非空字符串的条目、finish_reason 兜底"""
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
    # 分维度理由透传：键与值均为非空字符串（按 strip 后判空）才保留；
    # 非 dict（裁判违反协议输出 list/str）按字段缺失处理，优雅丢弃而非抛错；
    # 裁判未输出或条目全部非法时不输出该键，结果结构保持旧形态
    raw_feedbacks = parsed.get("dimension_feedbacks")
    dim_feedbacks: Dict[str, str] = {
        name: value
        for name, value in (raw_feedbacks.items() if isinstance(raw_feedbacks, dict) else ())
        if isinstance(name, str) and name.strip() and isinstance(value, str) and value.strip()
    }
    result = {
        "score": score,
        "feedback": str(parsed.get("feedback", "")),
        "dimension_scores": dim_scores,
        "finish_reason": finish_reason or "completed",
    }
    if dim_feedbacks:
        result["dimension_feedbacks"] = dim_feedbacks
    return result


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
            {score, feedback, dimension_scores, dimension_feedbacks?, finish_reason}
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
        with judge_pool_slot(
            self._pool_engine(), meta={"call_site": f"harness:{type(self).__name__}"}
        ):
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

    def _pool_engine(self) -> str:
        # Claude Code 与其余 harness（DeepSeek 等）各自独立限额：
        # 只有 ClaudeCodeAdapter 归 claude_agent，非 CC 的 harness 一律归 harness
        return "claude_agent" if isinstance(self, ClaudeCodeAdapter) else "harness"

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

        # argv 安全兜底：system_prompt 超限时降级并入首条消息
        # （--system-prompt 走 argv 有单参数上限，首条消息走 stdin 无上限）
        system_prompt, prompt = self._argv_safe_prompts(system_prompt, prompt)

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

    @staticmethod
    def _argv_safe_prompts(system_prompt: str, prompt: str) -> Tuple[Optional[str], str]:
        """system_prompt 超 argv 单参数上限时降级：并入首条消息，system_prompt 置 None

        claude-agent-sdk 以 --system-prompt argv 传 system_prompt，Linux execve
        单参数超 131072B 直接报 [Errno 7] Argument list too long（生产 Errno 7
        裁判启动失败事故根因）；首条消息经 stdin 传输无此限制。返回的
        system_prompt 可能为 None（SDK 不再传 --system-prompt flag）。
        """
        sp_bytes = len(system_prompt.encode("utf-8")) if system_prompt else 0
        if sp_bytes > SYSTEM_PROMPT_ARGV_LIMIT_BYTES:
            logger.warning(
                f"[ClaudeCodeAdapter] system_prompt {sp_bytes}B "
                f"超 argv 安全阈值 {SYSTEM_PROMPT_ARGV_LIMIT_BYTES}B，降级并入首条消息"
            )
            return None, f"{system_prompt}\n\n---\n{prompt}"
        return system_prompt, prompt


# ---------------- 沙箱被测 agent 执行（ARCH-3-2，设计 §2.3 D8 / §3 表 3-2 行） ----------------

# exec_type → 沙箱 harness 变体键（镜像/入口脚本按变体选择）。
# D8：被测 agent 与前处理脚本同级对待，同一 executor 驱动、同一镜像族
# （ai-evo-script-runner 系，变体镜像见 docker/script-runner/Dockerfile.harness）。
SANDBOX_HARNESS_VARIANTS: Dict[str, str] = {
    "claude_agent": "claude",
    "deepseek_agent": "deepseek",
}


class HarnessConfigError(ValueError):
    """被测 agent 步骤组装失败（未配置凭据/缺指令文本），按该步骤 fail 处理"""


def harness_variant_for(exec_type: str) -> str:
    """exec_type → harness 变体（claude|deepseek）；未知类型明确拒绝"""
    variant = SANDBOX_HARNESS_VARIANTS.get(str(exec_type or ""))
    if not variant:
        raise HarnessConfigError(
            f"exec_type={exec_type!r} 不在沙箱 harness 支持清单: {sorted(SANDBOX_HARNESS_VARIANTS)}")
    return variant


def build_harness_instruction(script: Dict[str, Any]) -> str:
    """取 agent 步骤的自然语言指令文本（exec_type=*_agent 的 entrypoint 字段）

    契约见 PreJudgeScript.entrypoint 注释（agent 类型存指令文本而非文件路径）。
    空指令明确拒绝（沙箱内 agent 无事可做，不如 fail-fast 让编排方修正配置）。
    """
    text = str(script.get("entrypoint") or "").strip()
    if not text:
        raise HarnessConfigError(
            "claude_agent/deepseek_agent 步骤缺少指令文本（entrypoint 为空，"
            "agent 类型的 entrypoint 存自然语言指令）")
    return text


def build_harness_agent_config(
    variant: str,
    script: Optional[Dict[str, Any]] = None,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """组装沙箱内被测 agent 的运行配置（工作区 staged harness.json 的内容）

    凭据经全局配置服务收集（claude → ClaudeCodeConfigService / deepseek →
    DeepSeekHarnessConfigService），随 harness.json 落本次 script_runs 工作区：
    工作区 secure_mkdir 0o750、评完即清（D6 口径：不进镜像、不进日志、不进
    容器 env 白名单——容器内由 /opt/runner/harness_entry.py 加载后注入子进程）。

    - claude 变体：ANTHROPIC_BASE_URL/AUTH_TOKEN 必备（未配置 fail-fast）；
      model/max_turns 取 CC 全局配置，allowed_tools 取脚本记录声明（缺省不
      限制——被测 agent 的工具面由沙箱边界约束，不套用裁判的保守白名单）；
    - deepseek 变体：api_key 必备；官方协议 base_url/api_key 直传 SDK kwargs，
      网关协议 key 经 DSH_PROVIDER_API_KEY 环境变量引用（不落 patch 文件，
      与 DeepSeekAdapter.get_sdk_env 同口径）；脚本记录 harness_extra 覆盖
      全局默认（provider/model/profile/max_tokens/reasoning_effort）。

    Raises:
        HarnessConfigError: 全局配置缺失或必备凭据不全（调用方转 fail 条目）
    """
    if variant == "claude":
        from app.services.claude_code_config_service import ClaudeCodeConfigService

        config = ClaudeCodeConfigService.get_active_config() or {}
        env = ClaudeCodeConfigService.get_sdk_env()
        if not (env.get("ANTHROPIC_BASE_URL") and env.get("ANTHROPIC_AUTH_TOKEN")):
            raise HarnessConfigError(
                "claude_agent 步骤需要 Claude Code 全局配置（模型配置页），"
                "当前未配置或缺少 base_url/auth_token，无法在沙箱内拉起被测 agent")
        allowed_tools = (script or {}).get("allowed_tools")
        return {
            "variant": "claude",
            "env": env,
            "model": config.get("model"),
            "max_turns": config.get("max_turns") or 30,
            "allowed_tools": [str(t) for t in allowed_tools if str(t).strip()]
            if isinstance(allowed_tools, list) else None,
            "request_timeout_seconds": float(timeout),
        }

    if variant == "deepseek":
        from app.services.deepseek_adapter import (
            PROVIDER_API_KEY_ENV,
            is_native_deepseek_protocol,
        )
        from app.services.deepseek_harness_config_service import DeepSeekHarnessConfigService

        config = DeepSeekHarnessConfigService.get_active_config()
        if not config:
            raise HarnessConfigError(
                "deepseek_agent 步骤需要 DeepSeek Harness 全局配置（模型配置页），"
                "当前未配置，无法在沙箱内拉起被测 agent")
        api_key = str(config.get("api_key") or "").strip()
        if not api_key:
            raise HarnessConfigError("DeepSeek Harness 全局配置缺少 api_key")
        env = DeepSeekHarnessConfigService.get_sdk_env()
        merged = {
            "provider": config.get("provider"),
            "model": config.get("model"),
            "profile": config.get("profile"),
            "max_tokens": config.get("max_tokens"),
            "protocol": config.get("protocol") or "deepseek",
            "base_url": config.get("base_url"),
        }
        # 脚本级 harness_extra 覆盖全局默认（DSH 专用字段，见 PreJudgeScript 注释）
        extra = (script or {}).get("harness_extra")
        if isinstance(extra, dict):
            merged.update({k: v for k, v in extra.items() if v is not None})
        harness: Dict[str, Any] = {
            "variant": "deepseek",
            "env": env,
            "request_timeout_seconds": float(timeout),
            "initialize_timeout_seconds": min(30.0, float(timeout)),
            **{k: v for k, v in merged.items() if v is not None},
        }
        if is_native_deepseek_protocol(merged):
            # 官方协议：base_url/api_key 直传 SDK（与 DeepSeekAdapter._build_harness_kwargs 同口径）
            harness["api_key"] = api_key
        else:
            # 网关协议：key 不落 patch 文件，经环境变量引用（镜像内 harness_entry 生成 patch）
            env[PROVIDER_API_KEY_ENV] = api_key
        return harness

    raise HarnessConfigError(f"未知 harness 变体 {variant!r}（可选: {'/'.join(sorted(set(SANDBOX_HARNESS_VARIANTS.values())))}）")
