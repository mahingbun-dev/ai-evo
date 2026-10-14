"""深健康检查端点（W13 整改计划 §3）

GET /api/v1/health/deep —— 浅探活（进程活着）之上的四个真实依赖探针：

  1. database    ：DB ping（SELECT 1）
  2. redis       ：Redis ping
  3. migrations  ：alembic_version 表内容 与 本仓库迁移 heads 比对
                   （检测「代码升级了但忘了跑迁移」的版本漂移）
  4. uploads     ：uploads 目录可写探测（写删临时文件）

全部探针通过 → 200 {"status": "healthy"}；任一失败 → 503 {"status": "degraded"}
并逐探针给出 error。503 是给机器看的（build-service verify 的
urlopen 与 compose healthcheck 都以非 2xx/非零退出判定失败），
checks 里的明细是给人看的。

鉴权：/api/v1/health 在 AuthMiddleware 白名单内（auth_middleware.WHITELIST_PREFIXES），
探活请求不带业务 cookie，与 build-service 部署校验兼容。

测试注记：人为停 PG 的场景本地难做（§3 W13 测试约定），单测通过
monkeypatch 探针/引擎/heads 覆盖 degraded 分支；集成测试
tests/integration/test_health_deep_integration.py 用临时库 + 本地 Redis 验证全绿路径。
"""
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter(tags=["健康检查"])

# 单探针超时。四个探针并发执行，接口总耗时 ≈ 单探针耗时——必须小于
# build-service verify 的 urlopen timeout=5s，否则依赖宕机时探活会以
# 「超时」而非「503 degraded」呈现，丢掉 checks 明细。
PROBE_TIMEOUT_SECONDS = 3.0

# 与 media_processor / hub_file_service 等的落盘根一致（CWD 相对 uploads/，
# 容器内挂载 ./uploads:/app/uploads）
UPLOADS_DIRNAME = "uploads"


def _app_engine():
    """取全局引擎（隔离成函数便于测试 monkeypatch）"""
    from app.db.session import engine

    return engine


def probe_database(engine: Optional[Any] = None) -> Dict[str, Any]:
    """DB ping：SELECT 1"""
    try:
        eng = engine if engine is not None else _app_engine()
        if eng is None:
            return {"status": "down", "error": "数据库引擎未初始化（DATABASE_URL 未配置？）"}
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:  # noqa: BLE001 - 探针把异常翻译成结构化结果
        return {"status": "down", "error": str(e)[:500]}


def probe_redis() -> Dict[str, Any]:
    """Redis ping"""
    try:
        from app.services import redis_stop_signal

        redis_stop_signal.get_redis_client().ping()
        return {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        return {"status": "down", "error": str(e)[:500]}


def migration_heads() -> List[str]:
    """读本仓库 alembic 迁移脚本算出的 head 版本号（不连数据库）"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    project_root = Path(__file__).resolve().parents[2]
    cfg = Config()
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    return sorted(ScriptDirectory.from_config(cfg).get_heads())


def probe_migrations(engine: Optional[Any] = None) -> Dict[str, Any]:
    """alembic_version 表 与 迁移 heads 比对"""
    try:
        heads = migration_heads()
        eng = engine if engine is not None else _app_engine()
        if eng is None:
            return {"status": "down", "expected": heads, "error": "数据库引擎未初始化（DATABASE_URL 未配置？）"}
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        current = sorted(r[0] for r in rows)
        if set(current) == set(heads):
            return {"status": "ok", "current": current, "expected": heads}
        return {
            "status": "mismatch",
            "current": current,
            "expected": heads,
            "error": f"alembic_version={current} 与迁移 heads={heads} 不一致，请执行 alembic upgrade head",
        }
    except Exception as e:  # noqa: BLE001 - 含 alembic_version 表不存在（未执行过迁移）
        return {"status": "down", "error": str(e)[:500]}


def uploads_root() -> str:
    return os.path.join(os.getcwd(), UPLOADS_DIRNAME)


def probe_uploads(root: Optional[str] = None) -> Dict[str, Any]:
    """uploads 可写探测：写一个临时文件再删掉"""
    path = root or uploads_root()
    try:
        os.makedirs(path, exist_ok=True)
        probe_file = os.path.join(path, f".deep_health_{uuid.uuid4().hex}")
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe_file)
        return {"status": "ok", "path": path}
    except Exception as e:  # noqa: BLE001
        return {"status": "down", "path": path, "error": str(e)[:500]}


def _run_with_timeout(
    name: str, fn: Callable[[], Dict[str, Any]], timeout: float
) -> Dict[str, Any]:
    """给探针加超时护栏：探针内部连的是外部依赖，网络级悬挂（对端不回包）
    不能拖死整个健康检查。

    注：Python 线程无法强杀，超时后底层调用线程会留到自然结束——
    探针内所有 IO 都带超时参数，该残留是短暂且有界的。
    """
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        return executor.submit(fn).result(timeout=timeout)
    except FuturesTimeoutError:
        return {"status": "down", "error": f"探针 {name} 超时（>{timeout}s）"}
    except Exception as e:  # noqa: BLE001
        return {"status": "down", "error": str(e)[:500]}
    finally:
        executor.shutdown(wait=False)


def collect_checks(engine: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
    """并发执行全部探针，返回 {探针名: 结果}；engine 参数供测试注入"""
    probes: Dict[str, Callable[[], Dict[str, Any]]] = {
        "database": lambda: probe_database(engine),
        "redis": probe_redis,
        "migrations": lambda: probe_migrations(engine),
        "uploads": probe_uploads,
    }
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {
            name: pool.submit(_run_with_timeout, name, fn, PROBE_TIMEOUT_SECONDS)
            for name, fn in probes.items()
        }
    return {name: fut.result() for name, fut in futures.items()}


@router.get("/health/deep", summary="深健康检查（DB/Redis/迁移版本/uploads 可写）")
async def deep_health():
    """全绿 → 200 healthy；任一探针失败 → 503 degraded（明细在 checks）"""
    checks = collect_checks()
    healthy = all(c.get("status") == "ok" for c in checks.values())
    body = {
        "status": "healthy" if healthy else "degraded",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": checks,
    }
    return JSONResponse(content=body, status_code=200 if healthy else 503)
