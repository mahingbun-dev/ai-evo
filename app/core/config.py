from typing import Optional, Dict
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

import logging
from functools import lru_cache

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    PROJECT_ROOT: Path = PROJECT_ROOT
    PROJECT_NAME: str = "AI Evaluation Platform"
    PROJECT_VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # 数据库配置（W6-3：默认空串，不再硬编码口令；必须通过环境变量或 .env 提供，
    # 应用启动入口会 fail-fast 校验，见 validate_runtime_settings）
    DATABASE_URL: str = ""

    # Redis 配置
    REDIS_URL: str = "redis://localhost:6379/0"

    # 登录鉴权配置（JWT HS256）
    # JWT 签名密钥（ARCH-0-2，设计决策 D6）：企业部署必须显式配置——留空且
    # 未开 AUTH_JWT_ALLOW_DERIVED 时启动 fail-fast（validate_runtime_settings）
    # 且 auth_jwt_secret 取值直接抛错，杜绝「从 DATABASE_URL 派生密钥」的
    # 凭证耦合（轮换 DB 口令不应作废全员登录态）
    AUTH_JWT_SECRET: str = ""
    # 派生回退显式开关（ARCH-0-2）：true 时允许 AUTH_JWT_SECRET 留空、回退
    # 「基于 DATABASE_URL 派生稳定密钥」——仅限本地开发机在 .env_local 显式
    # 打开（启动日志会告警留痕）；默认 false，企业部署档缺密钥即拒绝启动
    AUTH_JWT_ALLOW_DERIVED: bool = False
    # 登录态有效期（小时），默认 7 天
    AUTH_TOKEN_EXPIRE_HOURS: int = 168
    # 注册开关（关闭后仅已建账号可登录，登录页隐藏注册入口）
    AUTH_REGISTER_ENABLED: bool = True

    # 企业内网 OSS 媒体下载服务地址（可选功能：数据集媒体转存；留空则该功能不可用）
    OSS_BASE_URL: str = ""
    OSS_TENANT_CODE: str = ""

    # LangFuse 配置 (裁判评测追踪)
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    # 同时支持 LANGFUSE_HOST 和 LANGFUSE_BASE_URL 两种环境变量名
    LANGFUSE_HOST: str = Field("", alias="LANGFUSE_BASE_URL")

    # anc-hub MCP 端点（hub_file_service 运行时从 os.environ 读取，缺省时回退本字段）
    ANC_HUB_MCP_ENDPOINT: str = ""
    # anc-hub 调用凭证（静态服务 token，推荐；未配置时回退从浏览器 Cookie 提取）
    ANC_HUB_BEARER_TOKEN: str = ""
    # 与平台同域部署时承载登录态的 Cookie 名（其值作为 Bearer 凭证兜底通道；留空则禁用该通道）
    HUB_SSO_COOKIE_NAME: str = ""

    # 上传大小上限（W1-1：媒体上传与数据集临时上传共用，默认 50MB）
    UPLOAD_MAX_BYTES: int = 50 * 1024 * 1024

    # db_assert 内置脚本专用只读账号连接串（W1-3）：由平台编排时注入该脚本的
    # stdin payload，脚本自建 engine 执行只读断言。依赖 D-5（DBA 开立只读账号）；
    # 未配置时 db_assert 明确报「未配置只读连接」失败，不回退平台库
    DB_ASSERT_READONLY_DSN: str = ""

    # 前处理脚本执行模式（W15 容器化隔离灰度开关）：
    #   local     —— 宿主子进程直跑（M1 现状，默认；PYTHONPATH 注入保留）
    #   container —— docker run --rm 容器内隔离执行（非 root/资源限制/只读根
    #                文件系统/无网络/仅挂本次 script_runs 工作目录；不注入
    #                PYTHONPATH，脚本无法 import 平台代码）
    # 读取优先级：环境变量 SCRIPT_EXEC_MODE > .env/.env_local > 默认 local
    # （见 app.services.script_container.get_script_exec_mode：环境变量实时读，
    # 绕过 get_settings 的 lru_cache，dev 灰度切换无需重启）
    SCRIPT_EXEC_MODE: str = "local"

    # container 模式 runner 镜像（W15）：ai-evo-script-runner，构建方式见
    # docker/script-runner/README.md；部署机离线预载属 D-3 运维事项
    SCRIPT_RUNNER_IMAGE: str = "ai-evo-script-runner:latest"

    # container 模式容器内运行用户（非 root）：uid:gid。macOS Docker Desktop
    # 的 bind mount 对任意 uid 可写；Linux 部署机需与挂载目录属主匹配或预 chown
    SCRIPT_RUNNER_USER: str = "10001:10001"

    # DeepEval 接入平台模型网关（W16，P0-D）：依赖 D-6 技术决策（f1518c1 revert
    # 根因未确认），决策前保守关闭。true 时 safety/agent 等 deepeval 指标判分
    # 经 PlatformLLMWrapper 走平台模型配置出真实分；false 时行为与未接线一致
    DEEPEVAL_USE_PLATFORM_LLM: bool = False
    # 指定 deepeval 判分用的平台模型配置 ID（model_configs.id）；留空时自动选取
    # 最新的启用中 evaluation 角色模型。仅 flag 开启时生效
    DEEPEVAL_PLATFORM_MODEL_ID: str = ""

    @property
    def langfuse_config(self) -> Optional[Dict[str, str]]:
        """获取 LangFuse 配置字典，如果未启用则返回 None"""
        if not self.LANGFUSE_ENABLED:
            return None
        return {
            "public_key": self.LANGFUSE_PUBLIC_KEY,
            "secret_key": self.LANGFUSE_SECRET_KEY,
            "host": self.LANGFUSE_HOST,
        }

    @property
    def auth_jwt_secret(self) -> str:
        """JWT 签名密钥（ARCH-0-2，D6）。

        优先级：显式配置 AUTH_JWT_SECRET > 本地开发派生回退（须显式开
        AUTH_JWT_ALLOW_DERIVED）。两者皆无时抛 RuntimeError——不再静默从
        DATABASE_URL 派生，保证企业部署档缺密钥时任何签发/校验入口都
        fail-fast（含不经应用 lifespan 的进程，如 celery worker）。
        """
        if self.AUTH_JWT_SECRET.strip():
            return self.AUTH_JWT_SECRET
        if not self.AUTH_JWT_ALLOW_DERIVED:
            raise RuntimeError(_jwt_secret_missing_message())
        # 本地开发派生回退：与 DATABASE_URL 挂钩的稳定密钥（仅开发语义）
        import hashlib
        return hashlib.sha256(
            f"ai-evo:{self.DATABASE_URL}".encode()
        ).hexdigest()

    # 允许从 .env 文件读取配置，路径相对于项目根目录
    # .env 对应部署环境，.env_local 对应本地开发环境
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / ".env_local"),
        case_sensitive=True,
        extra="ignore"
    )

@lru_cache
def get_settings() -> Settings:
    """
    使用 lru_cache 缓存配置对象，避免重复读取环境变量
    """
    return Settings()


def _jwt_secret_missing_message() -> str:
    """JWT 密钥缺失的统一报错文案（ARCH-0-2）：校验入口与取值入口共用。

    报错信息必须给出可复制的密钥生成示例（沿用 W6-3「报错即修复指引」风格）。
    """
    return (
        "AUTH_JWT_SECRET 未配置：JWT 签名密钥不再从 DATABASE_URL 派生"
        "（ARCH-0-2，凭证解耦——轮换数据库口令不应作废全员登录态），"
        "请生成随机高熵密钥后写入部署目录 .env，示例：openssl rand -hex 32；"
        "仅本地开发机可显式设置 AUTH_JWT_ALLOW_DERIVED=true 保留派生回退"
        "（启动日志会告警），企业部署严禁开启"
    )


def validate_runtime_settings(settings: Settings) -> None:
    """运行入口 fail-fast 校验（W6-3；ARCH-0-2 扩展 JWT 密钥校验）。

    仅在应用启动入口（app.main 的 lifespan 开头）调用，不得在模块 import 时
    调用——保证 `import app.main` 在无 DATABASE_URL 的环境（如收集单测）仍可
    完成，导入不等于运行。缺 DATABASE_URL 时抛 RuntimeError 拒绝启动；
    缺 AUTH_JWT_SECRET 时（ARCH-0-2）：企业部署档抛 RuntimeError 拒绝启动，
    本地开发档（显式开 AUTH_JWT_ALLOW_DERIVED）放行但启动日志告警留痕。
    """
    if not settings.DATABASE_URL.strip():
        raise RuntimeError(
            "DATABASE_URL 未配置：服务启动需要数据库连接串，"
            "请通过环境变量或部署目录 .env 提供，"
            "格式 postgresql://<user>:<password>@<host>:5432/<database>"
        )
    # ARCH-0-2（D6）：JWT 密钥独立化——企业部署档缺失即拒绝启动
    if not settings.AUTH_JWT_SECRET.strip():
        if settings.AUTH_JWT_ALLOW_DERIVED:
            logger.warning(
                "AUTH_JWT_SECRET 未配置：当前为本地开发派生回退"
                "（AUTH_JWT_ALLOW_DERIVED=true，密钥由 DATABASE_URL 派生）——"
                "仅限开发机使用，企业部署请显式配置随机密钥（openssl rand -hex 32）"
            )
        else:
            raise RuntimeError(_jwt_secret_missing_message())
