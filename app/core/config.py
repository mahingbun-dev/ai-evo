from typing import Optional, Dict
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

from functools import lru_cache

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent


class Settings(BaseSettings):
    PROJECT_ROOT: Path = PROJECT_ROOT
    PROJECT_NAME: str = "AI Evaluation Platform"
    PROJECT_VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # 数据库配置（默认指向本机开发库，部署时通过 .env 覆盖）
    DATABASE_URL: str = "postgresql://postgres:postgres@127.0.0.1:5432/ai_evo"

    # Redis 配置
    REDIS_URL: str = "redis://localhost:6379/0"

    # 登录鉴权配置（JWT HS256）
    # AUTH_JWT_SECRET 为空时将基于 DATABASE_URL 派生稳定密钥（仅适合本地开发），
    # 生产环境务必在 .env 中显式配置随机高熵密钥
    AUTH_JWT_SECRET: str = ""
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
        """JWT 签名密钥：未显式配置时基于 DATABASE_URL 派生稳定密钥"""
        if self.AUTH_JWT_SECRET:
            return self.AUTH_JWT_SECRET
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
