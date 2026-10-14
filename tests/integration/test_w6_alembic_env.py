"""W6-2 迁移链路集成测试（整改开发计划 §2 W6「测试设计」第二条）。

核心断言：设 DATABASE_URL 指向测试库跑 alembic upgrade，**打到 env 指向的库
而非 ini**（旧实现 env.py 只读 ini 硬编码共享库 URL，迁移打错库风险）。

迁移链上 5a64da264cdd 与 14f63ba90774 的重复 ALTER 缺陷已在后续
链路修复，空库 `alembic upgrade head` 现可全链成功（有独立用例覆盖）。

环境：共享 PG 实例（shared_dsn fixture，不可达即 skip）；临时库 w67t_ 前缀
自建自清，绝不触碰共享库既有对象。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration  # W8 分类：integration（需本地 PG/Redis）

PY = sys.executable
INIT_REV = "8eafcf0aba1d"  # init 迁移（链 root，建 21 张表）


def _alembic(*args, dsn=None, timeout=180):
    """在 worktree 跑 alembic；dsn=None 表示显式剔除 DATABASE_URL。"""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if dsn is not None:
        env["DATABASE_URL"] = dsn
    return subprocess.run(
        [PY, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=timeout,
    )


def _shared_state(pg_admin):
    return (
        pg_admin.scalar(pg_admin._base["dbname"], "SELECT version_num FROM alembic_version"),
        pg_admin.scalar(
            pg_admin._base["dbname"],
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
        ),
    )


def test_upgrade_targets_env_database_not_ini(pg_admin):
    """DATABASE_URL 指向临时库 → upgrade 落到该库；共享库前后无恙；current 跟随 env。"""
    dbname = f"w67t_it_env_{uuid.uuid4().hex[:8]}"
    pg_admin.create_db(dbname)
    try:
        shared_before = _shared_state(pg_admin)

        dsn = pg_admin.dsn_for(dbname)
        r = _alembic("upgrade", INIT_REV, dsn=dsn)
        assert r.returncode == 0, f"upgrade 应打到 env 指向的临时库: {r.stderr[-800:]}"

        # 临时库被建表落版本（init 建 21 张表 + alembic_version）
        assert pg_admin.scalar(dbname, "SELECT version_num FROM alembic_version") == (INIT_REV,), (
            "env 指向的临时库应被写入 alembic_version"
        )
        tables, = pg_admin.scalar(
            dbname, "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        )
        assert tables >= 20, f"临时库应被 init 建表（实得 {tables} 张）"

        # current 跟随 env 指向的库
        r_cur = _alembic("current", dsn=dsn)
        assert r_cur.returncode == 0, r_cur.stderr[-500:]
        assert INIT_REV in r_cur.stdout, "alembic current 应报告 env 指向库的版本"

        # 共享库全程无恙（版本与表数不变）
        assert _shared_state(pg_admin) == shared_before, "迁移期间共享库被意外改动"
    finally:
        pg_admin.drop_db(dbname)


def test_no_env_falls_back_to_ini_placeholder_cannot_connect(pg_admin):
    """未设 DATABASE_URL → 回退 ini 占位符 <host>，连接必然失败且绝不误打共享库。"""
    shared_before = _shared_state(pg_admin)

    r = _alembic("current")  # env 中 DATABASE_URL 已剔除
    assert r.returncode != 0, "ini 占位符不可连，必须失败"
    combined = r.stdout + r.stderr
    assert "could not translate host name" in combined, (
        f"应报占位符主机不可解析，实得: {combined[-400:]}"
    )
    assert "<host>" in combined, "报错应指向 ini 的占位符（证明目标是 ini 而非共享库）"
    assert "55434" not in combined, "不得连到共享开发库"

    assert _shared_state(pg_admin) == shared_before, "共享库不得被误迁移"


def test_upgrade_head_on_fresh_db(pg_admin):
    """空库 upgrade head 全链成功（5a64da264cdd/14f63ba90774 重复 ALTER 缺陷
    已在后续迁移链修复，原 strict xfail 标记按其约定移除）。"""
    """全新空库 upgrade head 应成功（当前因既有重复列缺陷失败）。"""
    dbname = f"w67t_it_head_{uuid.uuid4().hex[:8]}"
    pg_admin.create_db(dbname)
    try:
        r = _alembic("upgrade", "head", dsn=pg_admin.dsn_for(dbname))
        assert r.returncode == 0, f"空库 upgrade head 失败: {r.stderr[-800:]}"
        assert "DuplicateColumn" not in r.stderr
    finally:
        pg_admin.drop_db(dbname)
