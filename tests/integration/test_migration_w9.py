"""W9 迁移集成测试：hub_files.team_code 列（对象级归属校验 IDOR 收口）

在本地 PG（127.0.0.1:55434）独立**探针临时库**（w9t_mig_ 前缀，测完 DROP）验证
整改计划 §3 W9 的 DDL 与迁移可执行/可回滚（共享库本体严禁变更，等价验证在
本临时库完成）：

1. 模拟 pre-W9 head 状态（create_all 全量表 → 摘除 W9 新增列 team_code →
   stamp c4e8f9a2b7d6，即 W3 迁移 head、W9 迁移的直接父）；
2. alembic upgrade head：核对 team_code 列（VARCHAR、可空、字面 'DEFAULT'
   服务端默认、COMMENT），并验证 expand-only（除新增列外无其他表/列变动）；
3. ADD COLUMN IF NOT EXISTS 幂等性；
4. alembic downgrade c4e8f9a2b7d6：列清零、版本回退；
5. 再次 upgrade head：干净重放。

注：用 create_all + stamp 构造基线，只验证 W9 迁移本身的正确性
（与 test_migration_w3.py 同口径）。原「从零全链 upgrade 因重复列缺陷
不可用」的限制已随 2026-10-15 修复（14f63ba90774 幂等化 + ab_test_sessions
补建迁移）解除，见 test_w6_alembic_env.py 的转正用例。
"""
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest

from tests.integration.conftest import BASE_DSN

WORKTREE_ROOT = Path(__file__).resolve().parents[2]

W3_HEAD = "c4e8f9a2b7d6"      # W9 迁移的直接父（stamp 基点，链上位置固定）
W9_REV = "a1b2c3d4e5f6"       # W9 迁移本身；与 W3 用例同口径钉住本迁移，不追 head（链头新增建表迁移会让 expand-only 断言假红）


_parts = urlsplit(BASE_DSN)
_ADMIN_DSN = urlunsplit(_parts._replace(path="/postgres"))
MIG_DB_NAME = f"w9t_mig_{secrets.token_hex(4)}"
MIG_DSN = urlunsplit(_parts._replace(path=f"/{MIG_DB_NAME}"))

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 探针库供给/清理（与 test_migration_w3.py 同模式）
# ---------------------------------------------------------------------------

def _connect_with_retry(dsn: str, attempts: int = 15, delay: float = 2.0):
    """管理连接重试：全量 suite 运行时，先序测试的瞬时连接峰值可能占满
    max_connections=100（实测 W4 e2e runtime 临时库单库峰值 ~89 连接），
    等待其释放后再建探针库管理连接。"""
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
def pre_w9_db():
    """构造 pre-W9 head 状态的探针库：全量表 → 摘除 W9 列 → stamp W3 head"""
    import os

    import subprocess
    import sys

    _drop_mig_db()
    _create_empty_db()
    # create_all 需在独立进程/环境变量下执行，避免污染本进程 app engine
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
    # 摘除 W9 对象 → 精确还原 pre-W9 head 状态（迁移 up 只加这一列）
    conn = psycopg2.connect(MIG_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE hub_files DROP COLUMN IF EXISTS team_code")
    finally:
        conn.close()
    _alembic(MIG_DSN, "stamp", W3_HEAD)
    yield MIG_DSN
    _drop_mig_db()


def _hub_file_columns(dsn):
    return {
        row[0]: row[1:]
        for row in _fetchall(
            dsn,
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = 'hub_files'",
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

    def test_repo_migration_chain_single_head(self):
        """迁移链必须单 head（原 _repo_head() 内的不变量，钉住 W9 revision 后保留）"""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory.from_config(
            Config(str(WORKTREE_ROOT / "alembic.ini"))
        ).get_heads()
        assert len(heads) == 1, f"迁移链应单 head，实得 {heads}"

    def test_upgrade_adds_team_code(self, pre_w9_db):
        before = _all_tables_columns(pre_w9_db)
        assert "team_code" not in before["hub_files"], "基线应无 team_code 列"

        # 钉住 W9 revision 本身（升级恰好建出 W9 对象）；不追 head——链头
        # 新增建表迁移（如 judge-pool）会让 expand-only 断言假红
        _alembic(pre_w9_db, "upgrade", W9_REV)

        # alembic 版本推进到 W9 revision
        assert _fetchall(pre_w9_db, "SELECT version_num FROM alembic_version")[0][0] == W9_REV

        after = _all_tables_columns(pre_w9_db)
        # expand-only：差异恰好 = hub_files.team_code 一列
        diff_tables = set(after) - set(before)
        assert not diff_tables, f"不应新增表：{diff_tables}"
        for table in set(before) & set(after):
            extra = after[table] - before[table]
            if table == "hub_files":
                assert extra == {"team_code"}, extra
            else:
                assert not extra, f"{table} 不应新增列：{extra}"
        for table in set(before) & set(after):
            removed = before[table] - after[table]
            assert not removed, f"{table} 不应删除/改名列（expand-only）：{removed}"

    def test_team_code_ddl_matches_plan(self, pre_w9_db):
        """逐项核对计划 §3 W9 DDL：VARCHAR、可空、字面 'DEFAULT' 默认、COMMENT"""
        cols = _hub_file_columns(pre_w9_db)
        assert "team_code" in cols
        data_type, is_nullable, col_default = cols["team_code"]
        assert data_type == "character varying", data_type
        assert is_nullable == "YES", "team_code 应可空（历史行为 NULL）"
        assert col_default is not None and "'DEFAULT'" in col_default, col_default

        # COMMENT 存在（导入时从数据集继承）
        comment = _fetchall(
            pre_w9_db,
            "SELECT col_description('hub_files'::regclass, "
            "(SELECT attnum FROM pg_attribute WHERE attrelid='hub_files'::regclass "
            "AND attname='team_code'))",
        )
        assert comment and "团队" in (comment[0][0] or ""), comment

    def test_existing_rows_get_default(self, pre_w9_db):
        """既有行读出 team_code 服务端默认 'DEFAULT'（历史包归属兜底口径）"""
        conn = psycopg2.connect(pre_w9_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO hub_files (id, file_id, filename, unpack_dir, unpack_status) "
                    "VALUES ('w9mig_row1', 'w9mig_fid1', 'x.zip', '/tmp/w9mig', 'unpacked')"
                )
                cur.execute("SELECT team_code FROM hub_files WHERE id = 'w9mig_row1'")
                assert cur.fetchone()[0] == "DEFAULT"
        finally:
            conn.close()

    def test_add_column_if_not_exists_idempotent(self, pre_w9_db):
        """迁移中的 ADD COLUMN IF NOT EXISTS 语句重复执行不报错"""
        conn = psycopg2.connect(pre_w9_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for _ in range(2):
                    cur.execute(
                        "ALTER TABLE hub_files ADD COLUMN IF NOT EXISTS "
                        "team_code VARCHAR NULL DEFAULT 'DEFAULT'"
                    )
        finally:
            conn.close()
        cols = _hub_file_columns(pre_w9_db)
        assert "team_code" in cols


class TestMigrationDowngrade:
    def test_downgrade_drops_team_code_and_reupgrade(self, pre_w9_db):
        _alembic(pre_w9_db, "downgrade", W3_HEAD)

        # 版本回退；列清零；其他表不受影响
        assert _fetchall(pre_w9_db, "SELECT version_num FROM alembic_version")[0][0] == W3_HEAD
        after_down = _all_tables_columns(pre_w9_db)
        assert "team_code" not in after_down["hub_files"]

        # 再次 upgrade：干净重放（回滚后可恢复）
        _alembic(pre_w9_db, "upgrade", W9_REV)
        assert _fetchall(pre_w9_db, "SELECT version_num FROM alembic_version")[0][0] == W9_REV
        assert "team_code" in _all_tables_columns(pre_w9_db)["hub_files"]
