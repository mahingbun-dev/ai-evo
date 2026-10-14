"""W3-1 迁移集成测试：evaluation_results 表 + task_execution_records 新列

在本地 PG（127.0.0.1:55434）独立**探针临时库**（w3t_mig_ 前缀，测完 DROP）验证
整改计划 §2 W3-1 的 DDL 与迁移可执行/可回滚（验收标准：迁移在 dev 共享库
可执行可回滚——共享库本体严禁变更，等价验证在本临时库完成）：

1. 模拟 pre-W3 head 状态（create_all 全量表 → 摘除 W3/W4 对象 → stamp e4f7b1a9c2d8，即 W3/W4 迁移的共同父——两迁移在 main 上已串成一条线）；
2. alembic upgrade head：逐项核对计划 DDL（列/类型/非空/JSONB/timestamptz
   默认 now()/UNIQUE(task_id,execution_number,data_index)/两索引/外键），
   并验证 expand-only（除新增对象外无其他表/列变动）；
3. ADD COLUMN IF NOT EXISTS 幂等性（W4 并行建列安全）；
4. alembic downgrade f8e1d4c7a2b6（W4 head，W3 迁移的直接父）：表/列清零、版本回退；
5. 再次 upgrade head：干净重放。

注：从零全链 alembic upgrade 因先存迁移链缺陷（5a64da264cdd 与
14f63ba90774 重复添加 evaluation_targets 列，主仓同现）不可用，
故用 create_all + stamp 构造基线，只验证 W3 迁移本身的正确性。
"""
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest

from tests.integration.conftest import BASE_DSN

WORKTREE_ROOT = Path(__file__).resolve().parents[2]

_parts = urlsplit(BASE_DSN)
_ADMIN_DSN = urlunsplit(_parts._replace(path="/postgres"))
MIG_DB_NAME = f"w3t_mig_{secrets.token_hex(4)}"
MIG_DSN = urlunsplit(_parts._replace(path=f"/{MIG_DB_NAME}"))

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 探针库供给/清理
# ---------------------------------------------------------------------------

def _connect_with_retry(dsn: str, attempts: int = 15, delay: float = 2.0):
    """管理连接重试：全量 suite 运行时，先序测试的瞬时连接峰值可能占满
    max_connections=100（实测 W4 e2e runtime 临时库单库峰值 ~89 连接），
    等待其释放后再建探针库管理连接（与 test_migration_w9.py 同手法）。"""
    last_exc = None
    for _ in range(attempts):
        try:
            return psycopg2.connect(dsn, connect_timeout=3)
        except psycopg2.OperationalError as e:
            last_exc = e
            time.sleep(delay)
    raise last_exc


def _admin(sql: str, params=None):
    conn = _connect_with_retry(_ADMIN_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


def _create_empty_db():
    _admin(f'CREATE DATABASE "{MIG_DB_NAME}"')


def _drop_mig_db():
    _admin(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (MIG_DB_NAME,),
    )
    _admin(f'DROP DATABASE IF EXISTS "{MIG_DB_NAME}"')


def _alembic(dsn: str, command_name: str, revision: str):
    """显式 DSN 的 alembic 命令（绝不使用 alembic.ini 默认 DSN——其指向远程
    共享 dev 库，误用会改动共享库）。

    W6-2 后 env.py 优先读 DATABASE_URL（覆盖 config 的 sqlalchemy.url），
    故命令期间显式将 DATABASE_URL 指向探针库，防止误打临时库/共享库。
    """
    import os

    import alembic.command
    import alembic.config

    cfg = alembic.config.Config()
    cfg.set_main_option("script_location", str(WORKTREE_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        getattr(alembic.command, command_name)(cfg, revision)
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved


def _fetchall(dsn: str, sql: str, params=None):
    conn = _connect_with_retry(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def pre_w3_db():
    """构造 pre-W3 head 状态的探针库：全量表 → 摘除 W3/W4 对象 → stamp e4f7b1a9c2d8"""
    import os

    _drop_mig_db()
    _create_empty_db()
    # create_all 需在独立进程/环境变量下执行，避免污染本进程 app engine
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, {root!r});"
        "import os; os.environ['DATABASE_URL'] = {dsn!r};"
        "from app.db.session import Base, engine;"
        "import app.models, app.models.ab_test_models, app.models.test_task_models;"
        "Base.metadata.create_all(engine); print('create_all ok')"
    ).format(root=str(WORKTREE_ROOT), dsn=MIG_DSN)
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(WORKTREE_ROOT),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": MIG_DSN},
    )
    # 摘除 W3 对象 → 精确还原 pre-W3 head 状态（迁移 up 只加这些对象）
    conn = psycopg2.connect(MIG_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS evaluation_results")
            cur.execute(
                "ALTER TABLE task_execution_records "
                "DROP COLUMN IF EXISTS config_snapshot, DROP COLUMN IF EXISTS heartbeat_at"
            )
    finally:
        conn.close()
    _alembic(MIG_DSN, "stamp", "e4f7b1a9c2d8")
    yield MIG_DSN
    _drop_mig_db()


def _table_columns(dsn, table):
    return {
        row[0]: row[1:]
        for row in _fetchall(
            dsn,
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
    }


def _all_tables_columns(dsn):
    rows = _fetchall(
        dsn,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, column_name",
    )
    grouped = {}
    for table, column in rows:
        grouped.setdefault(table, set()).add(column)
    return grouped


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

class TestMigrationUpgrade:
    def test_upgrade_creates_table_and_columns(self, pre_w3_db):
        before = _all_tables_columns(pre_w3_db)
        assert "evaluation_results" not in before
        assert "config_snapshot" not in before["task_execution_records"]
        assert "heartbeat_at" not in before["task_execution_records"]

        # W9 推进链头后不再追 head：钉住 W3 revision 本身（升级恰好建出 W3 对象）
        _alembic(pre_w3_db, "upgrade", "c4e8f9a2b7d6")

        # alembic 版本推进到 W3 head
        assert _fetchall(pre_w3_db, "SELECT version_num FROM alembic_version")[0][0] == "c4e8f9a2b7d6"

        after = _all_tables_columns(pre_w3_db)
        # expand-only：差异恰好 = 新表 + 两个新列
        diff_tables = set(after) - set(before)
        assert diff_tables == {"evaluation_results"}, f"不应新增其他表：{diff_tables}"
        for table in set(before) & set(after):
            extra = after[table] - before[table]
            if table == "task_execution_records":
                assert extra == {"config_snapshot", "heartbeat_at"}, extra
            else:
                assert not extra, f"{table} 不应新增列：{extra}"
        for table in set(before) & set(after):
            removed = before[table] - after[table]
            assert not removed, f"{table} 不应删除/改名列（expand-only）：{removed}"

    def test_evaluation_results_ddl_matches_plan(self, pre_w3_db):
        """逐项核对计划 §2 W3-1 DDL"""
        cols = _table_columns(pre_w3_db, "evaluation_results")
        # 列集合（含计划外增列 row_result——开发偏差②，聚合/续跑重建事实源）
        assert set(cols) == {
            "id", "task_id", "execution_number", "data_index", "status",
            "input_data", "model_output", "model_raw_response", "judge_results",
            "aggregate", "row_result", "error", "created_at", "updated_at",
        }, set(cols)

        # 类型与非空
        assert cols["id"][0] == "bigint" and cols["id"][1] == "NO"
        assert cols["task_id"][0] == "character varying" and cols["task_id"][1] == "NO"
        assert cols["execution_number"][0] == "integer" and cols["execution_number"][1] == "NO"
        assert cols["data_index"][0] == "integer" and cols["data_index"][1] == "NO"
        assert cols["status"][0] == "character varying" and cols["status"][1] == "NO"
        # JSONB 列（计划：新表新列一律 JSONB）
        for col in ("input_data", "judge_results", "aggregate", "row_result"):
            assert cols[col][0] == "jsonb", f"{col} 应为 JSONB，实际 {cols[col][0]}"
        for col in ("model_output", "model_raw_response", "error"):
            assert cols[col][0] == "text"
        # timestamptz 默认 now()
        for col in ("created_at", "updated_at"):
            assert cols[col][0] == "timestamp with time zone", cols[col][0]
            assert cols[col][2] is not None and "now()" in cols[col][2], cols[col][2]
        # id 为 BIGSERIAL（默认 nextval 序列）
        assert "nextval" in (cols["id"][2] or ""), "id 应为 BIGSERIAL 自增主键"

        # 主键
        pk = _fetchall(
            pre_w3_db,
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'evaluation_results'::regclass AND i.indisprimary",
        )
        assert [r[0] for r in pk] == ["id"]

        # UNIQUE(task_id, execution_number, data_index)
        unique_cols = _fetchall(
            pre_w3_db,
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'evaluation_results'::regclass AND i.indisunique "
            "AND NOT i.indisprimary ORDER BY a.attnum",
        )
        assert [r[0] for r in unique_cols] == ["task_id", "execution_number", "data_index"]

        # 计划要求的两个索引
        indexes = {
            r[0]: r[1]
            for r in _fetchall(
                pre_w3_db,
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'evaluation_results'",
            )
        }
        assert "idx_eval_results_task_exec" in indexes, indexes.keys()
        assert "idx_eval_results_status" in indexes, indexes.keys()
        assert "USING btree (task_id, execution_number)" in indexes["idx_eval_results_task_exec"]
        assert "USING btree (task_id, execution_number, status)" in indexes["idx_eval_results_status"]

        # 外键 → evaluation_tasks.id
        fk = _fetchall(
            pre_w3_db,
            "SELECT confrelid::regclass::text FROM pg_constraint "
            "WHERE conrelid = 'evaluation_results'::regclass AND contype = 'f'",
        )
        assert fk and fk[0][0] == "evaluation_tasks"

    def test_task_execution_records_new_columns(self, pre_w3_db):
        cols = _table_columns(pre_w3_db, "task_execution_records")
        assert cols["config_snapshot"][0] == "jsonb"
        assert cols["config_snapshot"][1] == "YES"  # 备用列可空
        assert cols["heartbeat_at"][0] == "timestamp with time zone"
        assert cols["heartbeat_at"][1] == "YES"

    def test_add_column_if_not_exists_idempotent(self, pre_w3_db):
        """迁移中的 ADD COLUMN IF NOT EXISTS 语句重复执行不报错（W4 并行建列安全）"""
        conn = psycopg2.connect(pre_w3_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                # 重复执行两次（此时列已存在）
                for _ in range(2):
                    cur.execute(
                        "ALTER TABLE task_execution_records "
                        "ADD COLUMN IF NOT EXISTS config_snapshot JSONB"
                    )
                    cur.execute(
                        "ALTER TABLE task_execution_records "
                        "ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ"
                    )
        finally:
            conn.close()
        # 列仍各只有一份
        cols = _table_columns(pre_w3_db, "task_execution_records")
        assert "config_snapshot" in cols and "heartbeat_at" in cols


class TestMigrationDowngrade:
    def test_downgrade_drops_w3_objects_and_reupgrade(self, pre_w3_db):
        _alembic(pre_w3_db, "downgrade", "f8e1d4c7a2b6")

        # 版本回退；表/列清零；其他表不受影响
        assert _fetchall(pre_w3_db, "SELECT version_num FROM alembic_version")[0][0] == "f8e1d4c7a2b6"
        after_down = _all_tables_columns(pre_w3_db)
        assert "evaluation_results" not in after_down
        assert "config_snapshot" not in after_down["task_execution_records"]
        assert "heartbeat_at" not in after_down["task_execution_records"]

        # 再次 upgrade：干净重放（回滚后可恢复；钉 W3 revision，同上）
        _alembic(pre_w3_db, "upgrade", "c4e8f9a2b7d6")
        assert _fetchall(pre_w3_db, "SELECT version_num FROM alembic_version")[0][0] == "c4e8f9a2b7d6"
        assert "evaluation_results" in _all_tables_columns(pre_w3_db)
