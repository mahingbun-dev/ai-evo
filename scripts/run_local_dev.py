"""
本地开发启动入口

本地账号登录体系下无需任何绕过：直接启动标准 app，
首次使用在登录页注册账号（首个注册用户自动获得超级管理员权限）：
    .venv/bin/python scripts/run_local_dev.py [--port 8100] [--reload]

前置：
- .env 指向本地可达的 PostgreSQL / Redis（如本地容器）
- 数据库已执行 alembic upgrade head
    （连接串经 AIEVO_DATABASE_URL 环境变量或 alembic_local.ini 提供）

P0-A：本入口不强制执行迁移（dev 直跑保持轻量），但启动前会检查
alembic_version 是否存在，未建表时在日志给出可复制的迁移命令。
"""
import argparse
import logging
import sys
from pathlib import Path

# 直接运行脚本时把项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO)


def check_db_migrated() -> None:
    """启动前轻量检查：库里有 alembic_version 表视为已迁移，否则打提示日志。

    连接串缺失/库不可达时无法判断——只提示不阻断（真正的 fail-fast 由
    app.main 的 validate_runtime_settings 与后续请求路径把关）。
    """
    logger = logging.getLogger("run_local_dev")
    try:
        from sqlalchemy import create_engine, inspect

        from app.core.config import get_settings

        url = get_settings().DATABASE_URL
        if not url.strip():
            return
        engine = create_engine(url)
        try:
            if not inspect(engine).has_table("alembic_version"):
                logger.warning(
                    "=" * 72
                )
                logger.warning(
                    "检测到数据库尚未执行迁移（无 alembic_version 表）："
                    "登录/注册等接口将 500，请先建表——"
                )
                logger.warning(
                    "    export AIEVO_DATABASE_URL=\"%s\" && alembic upgrade head",
                    url,
                )
                logger.warning(
                    "（或编辑 alembic_local.ini 提供连接串后执行 alembic upgrade head）"
                )
                logger.warning("=" * 72)
        finally:
            engine.dispose()
    except Exception as e:  # noqa: BLE001 - 检查失败不影响启动
        logger.info("跳过数据库迁移检查（连接不可达或未配置）: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--reload", action="store_true",
                        help="代码改动自动重载（本地开发，仅 watch app/）")
    args = parser.parse_args()

    import uvicorn

    check_db_migrated()

    if args.reload:
        # reload 模式必须传 import 字符串（app 对象无法跨进程传递），
        # 只 watch app/ 目录，避免前端 node_modules 等误触发重载
        uvicorn.run(
            "app.main:app",
            factory=False, host=args.host, port=args.port,
            log_level="info", reload=True, reload_dirs=["app"],
        )
    else:
        from app.main import app
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
