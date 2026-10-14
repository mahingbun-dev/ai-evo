import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.api.endpoints import router as api_router
from app.api.health_api import router as health_router
from app.api.model_config_api import router as model_config_router, router_judge as judge_router
from app.api.instruction_config_api import router as instruction_router
from app.api.judge_config_api import router as judge_config_router
from app.api.media_api import router as media_router
from app.api.dataset_api import router as dataset_router
from app.api.evaluation_task_api import router as task_router, results_router
from app.api.batch_operations_api import router as batch_router, export_router
from app.api.ab_test_api import router as ab_test_router
from app.api.docs_api import router as docs_router
from app.api.user_api import router as user_router, auth_router as user_auth_router
from app.api.team_api import router as team_router
from app.api.audit_api import router as audit_router
from app.api.task_status_api import router as task_status_router
from app.api.stats_api import router as stats_router
from app.api.coverage_api import router as coverage_router
from app.api.efficiency_api import router as efficiency_router
from app.api.agent_evaluation_api import router as agent_evaluation_router
from app.api.safety_evaluation_api import router as safety_evaluation_router
from app.api.dataset_synthesis_api import router as dataset_synthesis_router
from app.api.claude_code_config_api import router as claude_code_config_router
from app.api.pre_judge_script_api import router as pre_judge_script_router
from app.api.deepseek_harness_config_api import router as deepseek_harness_config_router
from app.api.hub_file_api import router as hub_file_router
from app.api.hub_import_api import router as hub_import_router, dataset_router as hub_import_dataset_router
from app.middleware.auth_middleware import AuthMiddleware
from app.db.session import engine, Base
from app.core.config import get_settings, validate_runtime_settings
import uvicorn

logger = logging.getLogger(__name__)

# ============================================
# 日志配置：输出到控制台和文件（W13：文件改 RotatingFileHandler，
# 10MB×10 个备份——日志不再无限增长，占满磁盘上限 ≈ 110MB）
# ============================================
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 文件 Handler（轮转：单文件 10MB，保留 10 个历史）
file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "auth_middleware.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8"
)
# W13：auth 日志原为 DEBUG 级（每个请求刷 SSO 明细），降到 INFO
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(file_formatter)

# 控制台 Handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S"
)
console_handler.setFormatter(console_formatter)

# 配置 root logger 和 app.middleware logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(console_handler)

# W13：auth 两个 logger 由 DEBUG 降为 INFO（原本每个请求都把 SSO 请求/响应
# 明细刷进文件，既拖慢请求又占磁盘；DEBUG 排障需要时临时调回）
middleware_auth_logger = logging.getLogger("app.middleware.auth_middleware")
middleware_auth_logger.setLevel(logging.INFO)
middleware_auth_logger.addHandler(file_handler)
middleware_auth_logger.addHandler(console_handler)

logger.info(f"日志配置完成，日志目录: {LOG_DIR}")
# ============================================

# 获取配置实例
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时清理孤立任务，关闭时执行必要清理"""
    # W6-3：启动 fail-fast——缺 DATABASE_URL 直接拒绝启动（放在 lifespan 而非
    # 模块 import 时，保证 import app.main 可在无配置环境完成，导入不等于运行）
    validate_runtime_settings(settings)

    # === 启动阶段 ===
    # 清理上次服务重启遗留的 running 状态任务
    # W10-4：改为心跳判定——只有当前执行记录 heartbeat_at 超 15 分钟无进展的
    # running 任务才标 failed（真死收尸）；worker 仍在执行的任务（心跳新鲜）
    # 与排队中任务（执行记录尚无心跳）不再被部署重启误标。
    try:
        from app.db.session import SessionLocal
        from app.services.evaluation_task_service import EvaluationTaskService
        with SessionLocal() as db:
            svc = EvaluationTaskService(db)
            reaped = svc.fail_stale_running_tasks()
            if reaped:
                logger.warning(f"已收尸 {len(reaped)} 个心跳超时的 running 任务: {reaped}")
    except Exception as e:
        logger.warning(f"清理孤立任务失败: {e}")

    # 前处理脚本统一子进程执行：启动时幂等迁移内置脚本为 script 记录
    # （builtin 进程内类型已废除；失败告警不阻断启动）
    try:
        from app.db.session import SessionLocal
        from app.services.pre_judge_script_service import ensure_builtin_scripts_migrated
        with SessionLocal() as db:
            migrated = ensure_builtin_scripts_migrated(db)
            logger.info(f"前处理脚本迁移检查完成：{len(migrated)} 个内置脚本可用（子进程执行）")
    except Exception as e:
        logger.warning(f"迁移内置前处理脚本失败（不阻断启动）: {e}")

    # TaskScheduler 现在是无状态的，不需要显式启动
    # Celery Worker 由独立进程管理
    logger.info("TaskScheduler (Celery) 已初始化")

    yield

    # === 关闭阶段 ===
    # Celery 由独立进程管理，API 进程关闭时无需特殊处理
    logger.info("API 进程关闭")

# OpenAPI 标签说明，用于 /docs 分组展示
openapi_tags = [
    {"name": "Excel", "description": "Excel 文件上传与数据读取"},
    {"name": "DirectEval", "description": "直接评测接口"},
    {"name": "模型配置", "description": "待评测模型和裁判员模型配置管理"},
    {"name": "裁判模型", "description": "AI裁判员模型配置"},
    {"name": "评测管理", "description": "评测数据集、任务、模板管理"},
    {"name": "媒体资源", "description": "媒体文件上传与管理"},
    {"name": "批量操作", "description": "批量任务执行、删除、停止、导出"},
    {"name": "结果导出", "description": "评测结果导出"},
    {"name": "A/B测试", "description": "A/B 模型对比测试"},
    {"name": "用户文档", "description": "用户使用文档读取与更新"},
    {"name": "用户管理", "description": "用户信息查询与管理"},
    {"name": "用户认证", "description": "登录状态检查"},
    {"name": "团队管理", "description": "团队信息查询与成员管理"},
    {"name": "审计日志", "description": "操作审计日志查询"},
    {"name": "统计", "description": "任务执行统计分析"},
    {"name": "anc-hub 文件", "description": "anc-hub file-store 远端文件检索与导入"},
]

app = FastAPI(
    title=settings.PROJECT_NAME,
    description="AI模型评测平台 - 支持模型配置、评测任务管理、数据集管理、结果分析",
    version=settings.PROJECT_VERSION,
    openapi_tags=openapi_tags,
    lifespan=lifespan,
    # /docs 让给前端「使用文档」页（SPA 路由），Swagger 挪至 /api-docs
    docs_url="/api-docs",
    redoc_url=None,
)

# 添加鉴权中间件
app.add_middleware(AuthMiddleware)

# 添加 CORS 中间件
# 注意：当 allow_credentials=True 时，allow_origins 不能设置为 "*"
# 需要明确指定允许的源，或者在生产环境中配置正确的域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8100", "http://127.0.0.1:5173", "http://127.0.0.1:8100"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 前端 dist 路径
FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")

# 挂载前端静态文件（必须在路由之前）
if os.path.exists(FRONTEND_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets"), check_dir=False), name="assets")
    logger.info(f"前端静态文件已挂载: {FRONTEND_DIST}")

# 使用文档配图（user_guide.md 引用的截图；/docs 前缀在鉴权白名单内，与 Swagger 同放行策略）
DOCS_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "assets")
if os.path.exists(DOCS_ASSETS_DIR):
    app.mount("/docs/assets", StaticFiles(directory=DOCS_ASSETS_DIR), name="docs-assets")
    logger.info(f"文档配图目录已挂载: {DOCS_ASSETS_DIR}")

# 注册 API 路由
app.include_router(api_router, prefix=settings.API_V1_STR)
# 深健康检查（W13）：/api/v1/health/deep，白名单见 auth_middleware.WHITELIST_PREFIXES
app.include_router(health_router, prefix=settings.API_V1_STR)
app.include_router(model_config_router, prefix=settings.API_V1_STR)
app.include_router(judge_router, prefix=settings.API_V1_STR)
app.include_router(instruction_router, prefix=settings.API_V1_STR)
app.include_router(media_router, prefix=settings.API_V1_STR)
app.include_router(dataset_router, prefix=settings.API_V1_STR)
app.include_router(task_router, prefix=settings.API_V1_STR)
app.include_router(results_router, prefix=settings.API_V1_STR)
app.include_router(judge_config_router, prefix=settings.API_V1_STR + "/evaluation")
app.include_router(batch_router, prefix=settings.API_V1_STR)
app.include_router(export_router, prefix=settings.API_V1_STR)
app.include_router(ab_test_router, prefix=settings.API_V1_STR)
app.include_router(docs_router, prefix=settings.API_V1_STR)
app.include_router(user_router, prefix=settings.API_V1_STR)
app.include_router(user_auth_router, prefix=settings.API_V1_STR)
app.include_router(team_router, prefix=settings.API_V1_STR)
app.include_router(audit_router, prefix=settings.API_V1_STR)
app.include_router(stats_router, prefix=settings.API_V1_STR)
app.include_router(coverage_router, prefix=settings.API_V1_STR)
app.include_router(efficiency_router, prefix=settings.API_V1_STR)
app.include_router(agent_evaluation_router, prefix=settings.API_V1_STR)
app.include_router(safety_evaluation_router, prefix=settings.API_V1_STR)
app.include_router(dataset_synthesis_router, prefix=settings.API_V1_STR)
app.include_router(claude_code_config_router, prefix=settings.API_V1_STR)
app.include_router(pre_judge_script_router, prefix=settings.API_V1_STR)
app.include_router(hub_file_router, prefix=settings.API_V1_STR)
app.include_router(hub_import_dataset_router, prefix=settings.API_V1_STR)
app.include_router(hub_import_router, prefix=settings.API_V1_STR)
app.include_router(deepseek_harness_config_router, prefix=settings.API_V1_STR)

# WebSocket 路由（不需要 API_V1_STR 前缀）
app.include_router(task_status_router)


@app.get("/")
async def root():
    """根路径返回前端 index.html，支持前端路由"""
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Welcome to AI Evaluation Platform"}


@app.get("/{path:path}")
async def serve_frontend(path: str):
    """前端路由 fallback：未匹配的路径都返回 index.html（SPA 模式）"""
    # 排除 /assets 路径，由静态文件中间件处理
    if path.startswith("assets/"):
        from fastapi.responses import Response
        return Response(status_code=404)
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Welcome to AI Evaluation Platform"}

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8100, reload=True, limit_concurrency=1000)  # 50MB
