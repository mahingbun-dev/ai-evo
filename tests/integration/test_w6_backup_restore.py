"""W6-1 备份与恢复集成实跑测试（整改开发计划 §2 W6「测试设计」第一条）。

- backup.sh 对共享 dev 库实跑到临时目录（pg_dump -Fc，PGDMP 魔数校验）
- restore.sh 恢复到专用临时库 + alembic_version 与源库自动比对一致
- 防护分支：缺 DSN / 未知模式 / 非 PGDMP / 缺参 / 交互拒绝
- uploads 模式：hub_supplements 归档内容可解包还原

环境：共享 PG + pg_dump/pg_restore/psql/tar 命令（缺失即 skip 不假绿）；
恢复目标一律 w67t_ 前缀专用临时库（restore.sh --clean 会 DROP 重建目标库
对象，严禁指向共享库），自建自清。
"""
import os
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration  # W8 分类：integration（需本地 PG/Redis）

BACKUP_SH = REPO_ROOT / "scripts" / "backup.sh"
RESTORE_SH = REPO_ROOT / "scripts" / "restore.sh"


def _require_cmds():
    missing = [c for c in ("pg_dump", "pg_restore", "psql", "tar")
               if shutil.which(c) is None]
    if missing:
        pytest.skip(f"集成环境缺失命令 {missing}，跳过备份恢复实跑（不假绿）")


def _run_bash(script: Path, *args, env_extra=None, input_text=None, timeout=300):
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        input=input_text, timeout=timeout,
    )


def _make_dump(shared_dsn, tmp_path) -> Path:
    """对共享 dev 库实跑一次 backup.sh db，返回 dump 文件路径。"""
    _require_cmds()
    r = _run_bash(BACKUP_SH, "db", env_extra={
        "DATABASE_URL": shared_dsn, "BACKUP_DIR": str(tmp_path)})
    assert r.returncode == 0, f"backup.sh db 实跑失败: {r.stderr[-500:]}"
    dumps = list((tmp_path / "db").glob("db_*.dump"))
    assert len(dumps) == 1, f"应产出一个 dump，实得 {dumps}"
    return dumps[0]


# ---------------------------------------------------------------------------
# backup.sh
# ---------------------------------------------------------------------------

def test_backup_db_real_run_on_shared_dev_db(shared_dsn, tmp_path):
    """backup.sh db 对共享 dev 库实跑：exit 0 + PGDMP 魔数 + 非空产物。"""
    _require_cmds()
    r = _run_bash(BACKUP_SH, "db", env_extra={
        "DATABASE_URL": shared_dsn, "BACKUP_DIR": str(tmp_path)})
    assert r.returncode == 0, f"backup.sh db 实跑失败: stdout={r.stdout[-300:]} stderr={r.stderr[-300:]}"
    assert "数据库备份完成" in r.stdout

    dumps = list((tmp_path / "db").glob("db_*.dump"))
    assert len(dumps) == 1, f"应产出一个 db_*.dump，实得 {dumps}"
    dump = dumps[0]
    assert dump.stat().st_size > 0, "备份文件不得为空"
    assert dump.open("rb").read(5) == b"PGDMP", "必须是 pg_dump 自定义格式（PGDMP 魔数）"


def test_backup_uploads_archives_hub_supplements(tmp_path):
    """backup.sh uploads：hub_supplements 全量 tar 归档，内容可解包还原。"""
    _require_cmds()
    uploads_root = tmp_path / "uploads"
    supp = uploads_root / "hub_supplements"
    (supp / "pkg_a").mkdir(parents=True)
    (supp / "pkg_a" / "a.txt").write_text("w67-ut 补传文件A")
    (supp / "b.bin").write_bytes(b"\x00w67-ut-B")

    backup_dir = tmp_path / "backups"
    r = _run_bash(BACKUP_SH, "uploads", env_extra={
        "UPLOADS_DIR": str(uploads_root), "BACKUP_DIR": str(backup_dir)})
    assert r.returncode == 0, r.stderr[-400:]
    archives = list((backup_dir / "hub_supplements").glob("hub_supplements_*.tar.gz"))
    assert len(archives) == 1

    with tarfile.open(archives[0]) as tf:
        names = tf.getnames()
        assert "hub_supplements/pkg_a/a.txt" in names
        assert "hub_supplements/b.bin" in names
        content = tf.extractfile("hub_supplements/pkg_a/a.txt").read()
        assert content.decode() == "w67-ut 补传文件A"


def test_backup_db_without_dsn_rejected():
    """db 模式缺 DATABASE_URL → exit 1，明确报错。"""
    _require_cmds()
    r = _run_bash(BACKUP_SH, "db")
    assert r.returncode == 1
    assert "DATABASE_URL" in (r.stdout + r.stderr)


def test_backup_unknown_mode_rejected():
    """未知模式 → exit 1。"""
    r = _run_bash(BACKUP_SH, "w67-ut-bogus")
    assert r.returncode == 1
    assert "未知模式" in (r.stdout + r.stderr)


def test_backup_no_mode_prints_usage():
    """无参数 → exit 1 并打印用法。"""
    r = _run_bash(BACKUP_SH)
    assert r.returncode == 1
    assert "用法" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# restore.sh
# ---------------------------------------------------------------------------

def test_restore_to_temp_db_and_alembic_version_matches(shared_dsn, pg_admin, tmp_path):
    """恢复演练闭环：backup 实跑产物 → restore 到临时库 → alembic_version 比对一致。"""
    _require_cmds()
    dump = _make_dump(shared_dsn, tmp_path / "bk")

    shared_db = shared_dsn.rsplit("/", 1)[-1]
    shared_version, shared_tables = (
        pg_admin.scalar(shared_db, "SELECT version_num FROM alembic_version")[0],
        pg_admin.scalar(shared_db, "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")[0],
    )

    dbname = f"w67t_it_restore_{uuid.uuid4().hex[:8]}"
    pg_admin.create_db(dbname)
    try:
        r = _run_bash(RESTORE_SH, str(dump), pg_admin.dsn_for(dbname), shared_dsn,
                      env_extra={"ASSUME_YES": "1"}, timeout=600)
        assert r.returncode == 0, (
            f"restore 演练失败: stdout={r.stdout[-400:]} stderr={r.stderr[-600:]}"
        )
        assert "alembic_version 比对一致" in r.stdout, (
            f"脚本应输出比对一致，实得: {r.stdout[-400:]}"
        )

        # 不信任脚本自报，独立复核恢复库状态
        restored_version = pg_admin.scalar(dbname, "SELECT version_num FROM alembic_version")[0]
        restored_tables = pg_admin.scalar(
            dbname, "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")[0]
        assert restored_version == shared_version, (
            f"恢复库版本 {restored_version} != 源库 {shared_version}"
        )
        assert restored_tables == shared_tables, (
            f"恢复库表数 {restored_tables} != 源库 {shared_tables}"
        )
    finally:
        pg_admin.drop_db(dbname)


def test_restore_rejects_non_pgdmp(tmp_path):
    """非 pg_dump 自定义格式文件 → exit 1（PGDMP 魔数校验）。"""
    _require_cmds()
    bogus = tmp_path / "bogus.dump"
    bogus.write_text("w67-ut not a pgdump file")
    r = _run_bash(RESTORE_SH, str(bogus), "postgresql://w67ut:w67ut@127.0.0.1:59999/x")
    assert r.returncode == 1
    assert "PGDMP" in (r.stdout + r.stderr)


def test_restore_rejects_missing_file(tmp_path):
    """备份文件不存在 → exit 1。"""
    _require_cmds()
    r = _run_bash(RESTORE_SH, str(tmp_path / "nope.dump"),
                  "postgresql://w67ut:w67ut@127.0.0.1:59999/x")
    assert r.returncode == 1


def test_restore_missing_args_prints_usage():
    """缺参 → exit 1 并打印用法。"""
    _require_cmds()
    r = _run_bash(RESTORE_SH)
    assert r.returncode == 1
    assert "用法" in (r.stdout + r.stderr)


def test_restore_interactive_decline_stops(shared_dsn, tmp_path):
    """无 ASSUME_YES 且人工输入 no → 取消恢复 exit 1（防误恢复交互确认）。"""
    _require_cmds()
    dump = _make_dump(shared_dsn, tmp_path / "bk")
    r = _run_bash(RESTORE_SH, str(dump), "postgresql://w67ut:w67ut@127.0.0.1:59999/x",
                  input_text="no\n")
    assert r.returncode == 1
    assert "已取消" in (r.stdout + r.stderr)
