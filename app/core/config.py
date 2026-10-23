from typing import Optional, Dict
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

import logging
import re
from functools import lru_cache

# 运行时档白名单同源引用（ARCH-3-4）：executor/sandbox_params 只依赖标准库
# （worker 镜像 COPY 全仓库，两侧均可导入），Settings 校验与沙箱参数构造共用
# 一份白名单——约束演进只改 sandbox_params.CONTAINER_RUNTIMES 一处
from executor.sandbox_params import CONTAINER_RUNTIMES

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

logger = logging.getLogger(__name__)

# SCRIPT_RUNNER_IMAGE 合法引用文法（ARCH-1-5 镜像 digest 钉扎）：
#   [registry[:port]/]*<name>[:tag][@sha256:<64 位十六进制>]
# - registry 前缀：0..n 个路径分量，首分量可带 :port（企业 LAN registry 形态，
#   如 harbor.internal:5000/ai-evo/）；
# - tag：docker tag 规范子集（与 executor/sandbox_params._IMAGE_TAG_RE 同宽）；
# - digest 钉扎：仅 sha256、恰好 64 位十六进制——截断/大写/其它算法一律拒绝
#   （digest 缺一位都是另一镜像，格式错必须 fail-fast 在配置解析层，不带病进容器通道）
_RUNNER_IMAGE_REF_RE = re.compile(
    r"^(?:(?:[A-Za-z0-9][A-Za-z0-9._-]*)(?::[0-9]+)?/)*"  # 可选 registry 前缀（可带端口）
    r"[A-Za-z0-9][A-Za-z0-9._-]*"                          # 镜像基名
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"              # 可选 tag
    r"(?:@sha256:[0-9a-f]{64})?$"                          # 可选 digest 钉扎（ARCH-1-5）
)


def validate_runner_image_ref(image: str) -> str:
    """SCRIPT_RUNNER_IMAGE 引用格式校验（ARCH-1-5，设计 §3 表 1-5/3-3 行）

    支持两类形态（均为 Settings 层校验，env/.env 同一道闸）：
    - 浮动引用 ``<base>[:<tag>]``（现状缺省 ai-evo-script-runner:latest）；
    - digest 钉扎 ``<repo>[@sha256:<64 位十六进制>]``（repo 可带 registry 前缀
      与 tag）——镜像内容寻址，防「同 tag 被覆盖/缓存投毒」，对应设计 3-3
      「script-runner 镜像 digest 钉扎进配置」的配置面预置。
    worker docker-CLI 通道原样把引用交给 docker run（daemon 原生支持 digest
    拉取校验）；executor 通道白名单（sandbox_params.validate_runner_image）
    尚未放开 digest（Phase 3-3），放开时两侧同步。

    返回剥离首尾空白后的引用；非法值抛 ValueError（pydantic 转 ValidationError，
    启动/导入配置即拒绝）。
    """
    ref = (image or "").strip()
    if not _RUNNER_IMAGE_REF_RE.match(ref):
        raise ValueError(
            f"SCRIPT_RUNNER_IMAGE 非法镜像引用 {image!r}：支持 <base>[:<tag>] 或 "
            "<repo>[@sha256:<64 位十六进制>]（digest 钉扎，ARCH-1-5），"
            "示例 ai-evo-script-runner:latest / "
            "harbor.internal:5000/ai-evo/script-runner@sha256:<64hex>"
        )
    return ref


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
    # 认证方式开关（M4 QE4-1/QE4-2）：逗号分隔子集 local,oidc,ldap——
    #   local —— 本地账号密码登录/注册（缺省，存量行为不变）；
    #   oidc  —— OIDC SSO 登录（须同时在超管设置里配置 IdP，见
    #            app/services/sso_service.py）；
    #   ldap  —— LDAP/AD 只读认证（QE4-2）：登录页账密表单旁路到
    #            search+bind 验证（须超管设置里配置目录，见
    #            app/services/ldap_service.py）。
    # 去掉 local（如 AUTH_METHODS=oidc）即关闭本地账密注册与直登；ldap
    # 与 local 可并存（本地有密码的账号走本地，其余旁路 LDAP）。
    # 校验见 _validate_auth_methods（非法值/空集启动即拒，不带病进登录面）。
    AUTH_METHODS: str = "local"

    @field_validator("AUTH_METHODS")
    @classmethod
    def _validate_auth_methods(cls, v: str) -> str:
        """认证方式白名单闸（M4 QE4-1/QE4-2）：非空子集 local,oidc,ldap"""
        methods = [m.strip().lower() for m in (v or "").split(",") if m.strip()]
        allowed = ("local", "oidc", "ldap")
        if not methods or any(m not in allowed for m in methods):
            raise ValueError(
                f"AUTH_METHODS 非法取值 {v!r}：仅允许 local/oidc/ldap 的非空逗号分隔组合"
                "（如 local,oidc,ldap 或 oidc）"
            )
        return ",".join(dict.fromkeys(methods))  # 去重保序

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

    # 前处理脚本执行模式（W15 容器化隔离灰度开关；ARCH-1-4 缺省切换）：
    #   local     —— 宿主子进程直跑（M1 现状；ARCH-1-4 起仅限开发机显式 env
    #                使用，企业部署禁止——同边界风险见 docs/build-pipeline.md
    #                第九节政策段）
    #   container —— docker run --rm / executor 沙箱容器内隔离执行（非 root/
    #                资源限制/只读根文件系统/无网络/仅挂本次 script_runs 工作
    #                目录；不注入 PYTHONPATH，脚本无法 import 平台代码）——
    #                ARCH-1-4（设计 §3 Phase 1 表 1-4 行）起为企业部署默认：
    #                合规态即缺省态，漏配不再落 local；灰度（内测团队 → 全量）
    #                与回退（改 env 重启）步骤见 docs/build-pipeline.md 第九节
    # 读取优先级：环境变量 SCRIPT_EXEC_MODE > .env/.env_local > 默认 container
    # （见 app.services.script_container.get_script_exec_mode：环境变量实时读，
    # 绕过 get_settings 的 lru_cache，改 env 重启 worker/api 即切换或回退）
    SCRIPT_EXEC_MODE: str = "container"

    # container 模式 runner 镜像（W15）：ai-evo-script-runner，构建方式见
    # docker/script-runner/README.md；部署机离线预载属 D-3 运维事项。
    # ARCH-1-5：支持镜像 digest 钉扎——除 <base>:<tag> 外可填
    # <repo>@sha256:<64 位十六进制>（repo 可带 registry 前缀），格式校验
    # 见 validate_runner_image_ref（Settings 解析层 fail-fast）；
    # worker docker-CLI 通道原样透传给 docker run（daemon 原生 digest 校验）
    SCRIPT_RUNNER_IMAGE: str = "ai-evo-script-runner:latest"

    @field_validator("SCRIPT_RUNNER_IMAGE")
    @classmethod
    def _validate_runner_image_ref(cls, v: str) -> str:
        """镜像引用格式闸（ARCH-1-5）：浮动 tag 与 digest 钉扎之外一律拒绝"""
        return validate_runner_image_ref(v)

    # container 模式容器内运行用户（非 root）：uid:gid。macOS Docker Desktop
    # 的 bind mount 对任意 uid 可写；Linux 部署机需与挂载目录属主匹配或预 chown
    SCRIPT_RUNNER_USER: str = "10001:10001"

    # 沙箱 bind 源宿主根（SANDBOX_WORKSPACE_HOST_ROOT）：docker daemon 按**宿主
    # 路径**解析 -v 挂载源，而 spec.workspace_host 是 worker 容器内视角路径
    # （/app/uploads/script_runs/...）。uploads 以 bind 且宿主/容器路径一致
    # 部署（./uploads:/app/uploads）时留空（缺省）即现状；命名卷部署
    # （compose 文档化的群晖形态 UPLOADS_VOLUME=uploads）下宿主不存在
    # /app/uploads/...，daemon 会静默新建空 root 目录——沙箱空工作区、
    # stdout/stderr 日志永远回不来，故该形态必填（如
    # /volume1/docker/ai-evo/uploads-data）。配置后 SandboxRunSpec.mount_source
    # = host_root + 工作区相对 /app/uploads/script_runs 的后缀（派生见
    # script_container.derive_mount_source）；executor 侧同名 env 校验白名单
    # （sandbox_params.resolve_mount_source，两侧单一事实源）
    SANDBOX_WORKSPACE_HOST_ROOT: str = ""

    # executor 沙箱服务调用地址（ARCH-1-2，设计 §2.3 D1）：worker → executor 的
    # compose 内网 HTTP 地址（如 http://executor:8300）。已配置时 container 模式
    # 走 HttpExecutorDriver（目标通道）；留空回退 docker-CLI 驱动（W15 既有单机
    # 通道，本机灰度/无 executor 部署形态）。读取同样环境变量实时优先
    # （见 app.services.script_container.get_sandbox_driver）
    EXECUTOR_URL: str = ""
    # executor 调用 token（ARCH-1-2，设计 D6：走 .env，不入库）：executor 侧
    # fail-closed 校验（未配置拒绝全部沙箱请求），worker 侧缺 token 同样不发起调用
    EXECUTOR_TOKEN: str = ""
    # 沙箱网络档位（ARCH-1-2，设计 D2 三档）：none（默认，--network none 完全
    # 离网）| egress（白名单域名出网，走沙箱出网 bridge，1-3 compose 建）| open
    # （默认 bridge 直出，超管审批流程属后续条目——executor 通道白名单未放开，
    # 请求即拒）。编排 step 的 network:true（W15「脚本要访问被测系统」）归一为
    # open 档。档位 → 容器网络参数映射见 script_container.WORKER_NET_PROFILE_TO_DOCKER
    # 与 executor/sandbox_params.NET_PROFILES；非法值 fail-safe 回退 none（告警）
    SCRIPT_NET_PROFILE: str = "none"

    # 沙箱容器运行时档（ARCH-3-4，设计 §3 Phase 3 表 3-4 行「gVisor 可选
    # 运行时档」）：runc（默认——docker daemon 缺省运行时，构造参数不拼
    # --runtime，默认行为与历史逐字节一致）| runsc（gVisor 加固档：应用层
    # 内核拦截系统调用，收窄容器内载荷触达宿主内核的面，高危脚本专用；
    # 宿主机须预装 runsc 并注册 docker daemon，安装步骤与适用场景见
    # docs/sandbox-runtime-gvisor.md）。白名单仅 runc|runsc（与
    # executor/sandbox_params.CONTAINER_RUNTIMES 同源），非法值启动即拒
    # （field_validator fail-fast，不带病进沙箱通道）；环境变量实时读取
    # 优先（见 app.services.script_container.sandbox_runtime：改 env 重启
    # worker 即切换或回退；HTTP executor 通道的客户端透传接线属后续批次，
    # 本批 docker-CLI 通道生效）
    SANDBOX_RUNTIME: str = "runc"

    @field_validator("SANDBOX_RUNTIME")
    @classmethod
    def _validate_sandbox_runtime(cls, v: str) -> str:
        """运行时档白名单闸（ARCH-3-4）：仅 runc|runsc（gVisor 加固档）"""
        runtime = (v or "").strip()
        if runtime not in CONTAINER_RUNTIMES:
            raise ValueError(
                f"SANDBOX_RUNTIME 非法运行时档 {v!r}：仅允许 {'|'.join(CONTAINER_RUNTIMES)}"
                "（runsc 为 gVisor 加固档，宿主机安装步骤见 docs/sandbox-runtime-gvisor.md）")
        return runtime

    # team 级评测并发配额（ARCH-2-2，设计 §2.3 决策 D4 / §3 Phase 2 表 2-2 行）：
    # 单团队同时在途（已提交、未到终态）的评测任务数上限，防止单团队打满全局
    # worker 并发（验收口径：单团队 100 并发任务不打满全局）。Redis 计数实现
    # 见 app/services/quota.py；超限提交被**拒绝**（不排队），任务按既有失败
    # 路径标记 failed，团队额度释放后重跑即可。必须为正整数（0 无「禁用」
    # 歧义语义，彻底放开请配一个大值）
    TEAM_MAX_CONCURRENT: int = Field(8, gt=0)

    # DeepEval 接入平台模型网关（W16，P0-D）：依赖 D-6 技术决策（f1518c1 revert
    # 根因未确认），决策前保守关闭。true 时 safety/agent 等 deepeval 指标判分
    # 经 PlatformLLMWrapper 走平台模型配置出真实分；false 时行为与未接线一致
    DEEPEVAL_USE_PLATFORM_LLM: bool = False
    # 指定 deepeval 判分用的平台模型配置 ID（model_configs.id）；留空时自动选取
    # 最新的启用中 evaluation 角色模型。仅 flag 开启时生效
    DEEPEVAL_PLATFORM_MODEL_ID: str = ""

    # M1 QE1-3 webhook SSRF 防护开关（默认关=拒绝内网目标）：webhook URL 校验
    # （app/services/webhook_url_guard.py）默认拒绝 localhost/私网/链路本地
    # 地址；企业内网接收端场景（本产品自托管部署常见）置 true 显式放行，
    # 由部署者自行承担内网目标面
    WEBHOOK_ALLOW_PRIVATE_TARGETS: bool = False

    # M2 http_assert 校验器内网目标放行的管理员总闸（默认关）：任务级
    # allow_private 只是任务侧意向，真正生效需本开关置 true（SSRF 控制面
    # 与 webhook 同口径——放行内网目标的决定权在部署管理员而非任务创建者）
    HTTP_ASSERT_ALLOW_PRIVATE_TARGETS: bool = False

    # M4 QE4-4 审计导出（xlsx/csv）企业功能位（默认关）：审计日志整表导出
    # 属企业治理诉求，非默认能力——开启后超管可在审计页导出（单次上限
    # AUDIT_EXPORT_MAX_ROWS 行，防全表拖库打爆内存）；关闭时端点 404
    AUDIT_EXPORT_ENABLED: bool = False
    AUDIT_EXPORT_MAX_ROWS: int = 10000

    # M5 QE5-3 周报邮件推送（可选，默认未配置=跳过邮件只走 webhook）：SMTP
    # 主机留空即整体跳过（email_status 落 skipped，不告警不阻断）；收件人
    # 逗号分隔。纯标准库 smtplib，无新增依赖
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM: str = ""
    WEEKLY_REPORT_EMAIL_TO: str = ""

    @property
    def auth_methods(self) -> set:
        """已启用认证方式集合（M4 QE4-1/QE4-2）：AUTH_METHODS 解析结果，

        供登录面（/auth/methods、/auth/login 本地/LDAP 闸、/auth/oidc/*
        SSO 闸）共用同一口径；validator 保证非空且 ⊆ {local, oidc, ldap}。
        """
        return {m.strip().lower() for m in self.AUTH_METHODS.split(",") if m.strip()}

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
