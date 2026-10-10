"""
DeepSeek Harness 适配器（deepseek-harness-sdk）

设计：docs/design/pre-judge-scripts.md 第九节（9.2 裁判引擎架构 / 风险与边界）
- sdk-minimal profile 为 danger-full-access：每次调用使用独立 dsh_home 与
  一次性 workspace（tempfile 临时目录，run 结束清理），不接触任何生产路径
- DSH 普通轮次默认无超时：显式传 request_timeout_seconds 强制请求超时，
  外层再套线程级超时兜底（run_sync_with_timeout）
"""
import logging
import shutil
import tempfile
from typing import Any, Dict, Optional, Tuple

from app.services.harness_adapter import HarnessAdapter, run_sync_with_timeout

logger = logging.getLogger(__name__)

# 网关协议 → DSH pi-ai 适配器 api 标识（协议值为空/None 时按 deepseek 官方适配器直连）
PROTOCOL_TO_PI_AI_API = {
    "openai": "openai-completions",
    "anthropic": "anthropic-messages",
}
# 非 deepseek 协议下，pi-ai patch 通过该环境变量引用 api_key（key 不写进 patch 文件）
PROVIDER_API_KEY_ENV = "DSH_PROVIDER_API_KEY"
PI_AI_PACKAGE_NAME = "@deepseek-ai/dsh-llm-pi-ai"
# pi-ai provider 的固定声明标识：
# - 不能沿用配置里的 provider 名（如 deepseek-official）——sdk-minimal 已内置声明
#   同名 configurable provider，重复声明会报 "already declared" 导致插件树加载失败；
# - runtime 0.1.5rc1 起 configurable provider 强制要求 displayName 与 settingsNs。
PI_AI_PROVIDER_ID = "pi-ai-gateway"


def is_native_deepseek_protocol(config: Dict[str, Any]) -> bool:
    """配置是否走 DeepSeek 官方适配器直连（base_url/api_key 可直传 DeepSeekHarness）"""
    return (config.get("protocol") or "deepseek") == "deepseek"


def build_provider_patch(config: Dict[str, Any], workspace: str) -> str:
    """按配置构造 pi-ai provider patch 文件（写入 workspace，返回文件路径）

    非 deepseek 协议的网关需要经 dsh-llm-pi-ai 多协议适配器接入（DeepSeek 官方
    适配器只说 DeepSeek 协议，且 sdk-minimal 组合没有自定义 provider 路由）。
    patch 以 insert 形式挂载 llm-pi-ai，api_key 经 apiKeyEnv 环境变量引用而
    不落盘。返回的路径供 DeepSeekHarness(patches=(...)) 使用。
    """
    protocol = (config.get("protocol") or "").strip()
    api = PROTOCOL_TO_PI_AI_API.get(protocol)
    if not api:
        raise ValueError(f"不支持的网关协议: {protocol!r}（可选 deepseek/openai/anthropic）")
    provider = (config.get("provider") or "").strip()
    model = (config.get("model") or "").strip()
    base_url = (config.get("base_url") or "").strip()
    if not provider or not model or not base_url:
        raise ValueError("非 deepseek 协议下 provider、model、base_url 均为必填")
    patch = [
        {
            "insert": [
                {
                    "id": "llm-pi-ai",
                    "name": PI_AI_PACKAGE_NAME,
                    "config": {
                        "providers": {
                            PI_AI_PROVIDER_ID: {
                                "displayName": "PI-AI Gateway",
                                "settingsNs": PI_AI_PROVIDER_ID,
                                "apiKeyEnv": PROVIDER_API_KEY_ENV,
                                "api": api,
                                "baseURL": base_url.rstrip("/"),
                                "models": [{"id": model}],
                            }
                        }
                    },
                }
            ]
        }
    ]
    try:
        import yaml
        content = yaml.safe_dump(patch, allow_unicode=True, sort_keys=False)
    except ImportError:
        import json
        content = json.dumps(patch, ensure_ascii=False, indent=2)
    path = f"{workspace.rstrip('/')}/dsh-provider-patch.yml"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class DeepSeekAdapter(HarnessAdapter):
    """DeepSeek Harness 裁判适配器（每次调用独立隔离目录 + 强制请求超时）"""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        task: Any = None,
        langfuse_host: str = "",
    ):
        if config is None:
            from app.services.deepseek_harness_config_service import DeepSeekHarnessConfigService

            config = DeepSeekHarnessConfigService.get_active_config()
        if not config:
            raise RuntimeError(
                "未配置 DeepSeek Harness，请先在「模型配置」页保存 DeepSeek Harness 全局配置"
            )
        super().__init__(config=config, task=task, langfuse_host=langfuse_host)

    def _load_sdk(self):
        """延迟导入 SDK（未安装时给出明确安装指引，而不是 ImportError 堆栈）"""
        try:
            from deepseek_harness import DeepSeekHarness
        except ImportError as e:
            raise RuntimeError(
                "deepseek-harness-sdk 未安装，无法执行 DeepSeek Harness 调用"
                "（安装：pip install deepseek-harness-sdk，需捆绑平台运行时 wheel）"
            ) from e
        return DeepSeekHarness

    def _execute_raw(self, system_prompt: str, prompt: str, timeout: float) -> Tuple[str, Optional[str]]:
        DeepSeekHarness = self._load_sdk()
        # DSH run 无独立 system 概念，系统指令拼接进任务 prompt 前部
        final_prompt = (
            f"【系统指令】\n{system_prompt}\n\n---\n【任务】\n{prompt}" if system_prompt else prompt
        )
        dsh_home = tempfile.mkdtemp(prefix="dsh_home_")
        workspace = tempfile.mkdtemp(prefix="dsh_ws_")
        try:
            harness_kwargs = self._build_harness_kwargs(dsh_home, workspace, timeout)

            def _invoke():
                with DeepSeekHarness(**harness_kwargs) as harness:
                    return harness.run(final_prompt)

            result = run_sync_with_timeout(_invoke, timeout, thread_name="dsh-judge-call")
            final_response = str(getattr(result, "final_response", "") or "")
            if not final_response:
                raise RuntimeError("DeepSeek Harness 无返回内容")
            finish_reason = getattr(result, "finish_reason", None) or "completed"
            return final_response, finish_reason
        finally:
            # 一次性 workspace 与独立 dsh_home 用完即弃（danger-full-access 隔离要求）
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(dsh_home, ignore_errors=True)

    def _build_harness_kwargs(self, dsh_home: str, workspace: str, timeout: float) -> Dict[str, Any]:
        """构造 DeepSeekHarness 构造参数（隔离目录 + 凭据 + 强制超时）

        协议分派：deepseek 官方协议 base_url/api_key 直传 SDK；openai/anthropic
        协议经 pi-ai provider patch 接入（key 走环境变量，不落盘不直传）。
        """
        kwargs: Dict[str, Any] = {
            "provider": self.config.get("provider") or "deepseek-official",
            "model": self.config.get("model") or "deepseek-v4-flash",
            "profile": self.config.get("profile") or "sdk-minimal",
            # 独立 dsh_home 与一次性 workspace（sdk-minimal 为 danger-full-access，必须隔离）
            "cwd": workspace,
            "dsh_home": dsh_home,
            "patches": tuple(self.config.get("extra_patches") or ()),
            "env": self.get_sdk_env(),
            # DSH 普通轮次默认无超时，评测场景必须显式强制
            "request_timeout_seconds": float(timeout),
            "initialize_timeout_seconds": min(30.0, float(timeout)),
        }
        if self.config.get("max_tokens"):
            kwargs["max_tokens"] = int(self.config["max_tokens"])
        if is_native_deepseek_protocol(self.config):
            if self.config.get("base_url"):
                kwargs["base_url"] = self.config["base_url"].rstrip("/")
            if self.config.get("api_key"):
                kwargs["api_key"] = self.config["api_key"]
        else:
            # 非 deepseek 协议：不能把 base_url/api_key 直传（那是官方 DeepSeek 适配器的
            # 参数，且 provider 路由在 sdk-minimal 中不存在），改为 pi-ai patch 接入；
            # provider 路由同步指向 patch 里声明的固定标识（配置里的 provider 名可能
            # 与 sdk-minimal 内置声明冲突，不作路由用）
            patch_path = build_provider_patch(self.config, workspace)
            kwargs["patches"] = tuple(kwargs["patches"]) + (patch_path,)
            kwargs["provider"] = PI_AI_PROVIDER_ID
        return kwargs

    def get_sdk_env(self) -> Dict[str, str]:
        """DSH 子进程环境变量：config.extra_env 全量注入（SDK 继承调用者环境，仅覆盖）；
        非 deepseek 协议时额外注入 pi-ai patch 引用的 provider api_key"""
        env: Dict[str, str] = {}
        for key, value in (self.config.get("extra_env") or {}).items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                env[key] = str(value)
        if not is_native_deepseek_protocol(self.config) and self.config.get("api_key"):
            env[PROVIDER_API_KEY_ENV] = str(self.config["api_key"])
        return env
