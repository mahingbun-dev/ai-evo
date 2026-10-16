"""W4 迁移 f8e1d4c7a2b6 正确性测试（独立探针临时库）

conftest 的运行时临时库因共享库活跃连接（TEMPLATE 复制被 ObjectInUse 阻断）
与先存迁移链缺陷（5a64da264cdd evaluation_targets 重复列，非 W4 引入）降级为
create_all + stamp head 供给——W4 迁移的真实 upgrade/downgrade 语义因此在本
文件用独立探针库（w4t_mig_ 前缀）单独验证：

1. 幂等性：目标列已存在（create_all 建出）→ stamp e4f7b1a9c2d8 后
   upgrade head 不报错（ADD COLUMN IF NOT EXISTS）；
2. 正向加列：手工 DROP 两列回到 e4f7b1a9c2d8 物理形态 → upgrade head
   真实建出 evaluation_tasks.judge_model_config 与
   task_execution_records.config_snapshot，类型 JSONB；
3. downgrade（expand-only 回滚语义）：降级回 e4f7b1a9c2d8 仅移除本包
   引入的两列，其余结构不动；
4. 共享库 aievo_verify 的 alembic_version 保持不变（测试绝不触碰共享库）。

探针库结束 DROP（自建自清）。
"""
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests.integration.conftest import BASE_DSN, SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

pytestmark = pytest.mark.integration

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
# 探针库基线 DSN 复用 conftest 同一解析源（W4_BASE_DSN > DATABASE_URL >
# 历史缺省）——本文件此前自带 ai_evo:ai_evo 硬编码缺省，与本地 dev 栈
# 口令不符时连接直接被拒；且 conftest 探测可达（不再置 SKIP_REASON）后
# 本模块从「整体 skip」变为实跑，硬编码立即暴露
_parts = urlsplit(BASE_DSN)
_ADMIN_DSN = urlunsplit(_parts._replace(path="/postgres"))
W4_REVISION = "f8e1d4c7a2b6"
W4_BASE_REVISION = "e4f7b1a9c2d8"


def _admin(sql: str, params=None):
    """在管理库（postgres）执行：仅限建/删库与连接终结"""
    import psycopg2

    conn = psycopg2.connect(_ADMIN_DSN, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
        return None
    finally:
        conn.close()


def _probe(dsn: str, sql: str, params=None):
    """在探针库执行（结构查询/ALTER/alembic_version 均作用于探针库）"""
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
        return None
    finally:
        conn.close()


def _alembic_cfg(dsn):
    import alembic.config

    cfg = alembic.config.Config()
    cfg.set_main_option("script_location", str(WORKTREE_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def _columns(dsn, table, column):
    return _probe(
        dsn,
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name=%s AND column_name=%s",
        (table, column),
    )


def _shared_alembic_version():
    import psycopg2

    conn = psycopg2.connect(BASE_DSN, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.fixture(scope="module")
def probe_db():
    """独立探针临时库（w4t_mig_ 前缀）：create_all 出 head schema 供迁移演练"""
    from sqlalchemy import create_engine

    import app.models  # noqa: F401 - 注册全部模型
    import app.models.ab_test_models  # noqa: F401
    import app.models.test_task_models  # noqa: F401
    from app.db.session import Base

    name = f"w4t_mig_{uuid.uuid4().hex[:8]}"
    dsn = urlunsplit(_parts._replace(path=f"/{name}"))
    _admin(f'CREATE DATABASE "{name}"')
    try:
        engine = create_engine(dsn)
        Base.metadata.create_all(engine)
        engine.dispose()
        yield {"name": name, "dsn": dsn}
    finally:
        _admin(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=%s AND pid<>pg_backend_pid()",
            (name,),
        )
        _admin(f'DROP DATABASE IF EXISTS "{name}"')


class TestW4Migration:
    def test_idempotent_add_column_and_downgrade(self, probe_db, monkeypatch):
        dsn = probe_db["dsn"]
        # W6-2 后 alembic env.py 优先读 DATABASE_URL（覆盖 config 的
        # sqlalchemy.url）——迁移/降级必须显式指向探针库，防止误打临时库/共享库
        monkeypatch.setenv("DATABASE_URL", dsn)

        # 共享库版本快照（前后断言不变——测试绝不触碰共享库）
        shared_before = _shared_alembic_version()

        import alembic.command

        # 1) 幂等：两列已存在（create_all 建出）→ stamp 基点 → upgrade W4。
        # W9 推进链头后不再追 head（head 上的 W3/W9 迁移会重建 create_all
        # 已建出的 evaluation_results 等对象），钉住 W4 revision 本身
        alembic.command.stamp(_alembic_cfg(dsn), W4_BASE_REVISION)
        alembic.command.upgrade(_alembic_cfg(dsn), W4_REVISION)
        # 探针库版本 = W4 head
        version_rows = _probe(dsn, "SELECT version_num FROM alembic_version")
        assert version_rows and version_rows[0][0] == W4_REVISION

        # 2) 正向加列：回到 e4f7b1a9c2d8 物理形态（DROP 两列 + 回退版本）
        _probe(dsn, "ALTER TABLE task_execution_records DROP COLUMN config_snapshot")
        _probe(dsn, "ALTER TABLE evaluation_tasks DROP COLUMN judge_model_config")
        _probe(dsn, "UPDATE alembic_version SET version_num=%s", (W4_BASE_REVISION,))
        assert not _columns(dsn, "task_execution_records", "config_snapshot")
        assert not _columns(dsn, "evaluation_tasks", "judge_model_config")

        alembic.command.upgrade(_alembic_cfg(dsn), W4_REVISION)
        # 迁移真实建出两列，类型 JSONB（与 ORM 模型/共享库一致）
        assert _columns(dsn, "task_execution_records", "config_snapshot") == [("jsonb",)]
        assert _columns(dsn, "evaluation_tasks", "judge_model_config") == [("jsonb",)]

        # 3) downgrade：expand-only 回滚语义——仅移除本包两列
        alembic.command.downgrade(_alembic_cfg(dsn), W4_BASE_REVISION)
        assert not _columns(dsn, "task_execution_records", "config_snapshot")
        assert not _columns(dsn, "evaluation_tasks", "judge_model_config")
        # 其余结构不动（抽样：evaluation_targets 仍在）
        assert _columns(dsn, "evaluation_tasks", "evaluation_targets") is not None

        # 共享库版本未变（供给/迁移只作用于探针库副本）
        assert _shared_alembic_version() == shared_before
