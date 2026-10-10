"""
本地开发启动入口

本地账号登录体系下无需任何绕过：直接启动标准 app，
首次使用在登录页注册账号（首个注册用户自动获得超级管理员权限）：
    .venv/bin/python scripts/run_local_dev.py [--port 8100] [--reload]

前置：
- .env 指向本地可达的 PostgreSQL / Redis（如本地容器）
- 数据库已执行 alembic upgrade head
    （连接串经 AIEVO_DATABASE_URL 环境变量或 alembic_local.ini 提供）
"""
import argparse
import logging
import sys
from pathlib import Path

# 直接运行脚本时把项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--reload", action="store_true",
                        help="代码改动自动重载（本地开发，仅 watch app/）")
    args = parser.parse_args()

    import uvicorn

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
