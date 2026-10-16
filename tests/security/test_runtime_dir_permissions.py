"""ARCH-0-3 工作目录权限收紧安全测试（设计 §3 Phase 0 条目 0-3）

覆盖两块（沿用 tests/security/test_rce_chain.py 风格，unit 分类）：
1. 运行时目录显式权限：uploads 下 script_runs（build_run_dir /
   build_script_run_dir / inputs 落盘）与 hub 侧 hub_files 缓存、
   hub_datasets 解包区、hub_supplements 补传区的目录创建统一走
   secure_mkdir（0o750：属主 rwx、组 rx、其他无），且权限不受进程
   umask 收窄影响、逐级中间目录同样显式；
2. 越权路径读写拦截：符号链接（文件/目录两种形态）与相对路径
   （.. 穿越 / 绝对路径段）经 ensure_within 一律抛 PathEscapeError——
   覆盖直接调用、zip 解包链路（_unpack_zip / unpack_zip_safely 对
   解包根内预置 symlink 的写拦截）与 script_runs 运行目录创建链路，
   并断言根外零残留。
"""
import io
import os
import stat
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.services import hub_file_service, pre_judge_runner
from app.utils.path_safety import (
    RUNTIME_DIR_MODE,
    PathEscapeError,
    ensure_within,
    secure_mkdir,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- 测试基建 ----------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _dir_mode(path) -> int:
    """目录权限位（八进制）"""
    return stat.S_IMODE(os.stat(path).st_mode)


def _assert_tree_mode(root):
    """断言 root 子树内全部目录的权限位 == 显式 0o750"""
    for dirpath, dirnames, _files in os.walk(root):
        for name in dirnames:
            assert _dir_mode(os.path.join(dirpath, name)) == RUNTIME_DIR_MODE, (
                f"目录权限不是显式 {oct(RUNTIME_DIR_MODE)}: "
                f"{os.path.join(dirpath, name)}（实际 {oct(_dir_mode(os.path.join(dirpath, name)))}）"
            )


# ==================== 1. 目录创建权限显式化（ARCH-0-3） ====================

class TestSecureMkdir:
    def test_nested_levels_all_explicit(self, tmp_path):
        """多级创建：本次新建的每一级（含中间层级）都精确 0o750

        对照 os.makedirs 的 mode 只作用于叶子、中间层走 umask 缺省权限的缺陷
        """
        target = tmp_path / "uploads" / "script_runs" / "t1" / "d1"
        secure_mkdir(target)
        _assert_tree_mode(tmp_path / "uploads")
        assert _dir_mode(target) == RUNTIME_DIR_MODE

    def test_mode_immune_to_umask(self, tmp_path):
        """严格 umask（0o077）下目录权限仍为 0o750——显式位不靠 umask 运气"""
        old_umask = os.umask(0o077)
        try:
            target = tmp_path / "a" / "b" / "c"
            secure_mkdir(target)
        finally:
            os.umask(old_umask)
        assert _dir_mode(target) == RUNTIME_DIR_MODE
        assert _dir_mode(target.parent) == RUNTIME_DIR_MODE

    def test_idempotent_keeps_existing_mode(self, tmp_path):
        """已存在目录幂等跳过：不重建、不越权重设他人属主目录的权限"""
        existing = tmp_path / "keep"
        existing.mkdir(mode=0o700)
        secure_mkdir(existing)
        assert _dir_mode(existing) == 0o700

    def test_file_collision_rejected(self, tmp_path):
        """目标被同名文件占用 → FileExistsError（与 os.mkdir exist_ok 同语义）"""
        occupied = tmp_path / "occupied"
        occupied.write_text("x", encoding="utf-8")
        with pytest.raises(FileExistsError):
            secure_mkdir(occupied)


class TestScriptRunDirPermissions:
    """uploads/script_runs 运行时目录权限（pre_judge_runner 创建链路）"""

    def test_build_run_dir_tree_mode(self, tmp_path, monkeypatch):
        base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
        run_dir = pre_judge_runner.build_run_dir("task_m1", "data_m1")
        assert Path(run_dir).is_dir()
        assert (Path(run_dir) / "outputs").is_dir()
        # script_runs 本身 + task/data/outputs 全部显式 0o750
        _assert_tree_mode(base)

    def test_build_script_run_dir_mode(self, tmp_path, monkeypatch):
        base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
        cwd = pre_judge_runner.build_script_run_dir("task_m2", "data_m2", "pjs_m2")
        assert _dir_mode(cwd) == RUNTIME_DIR_MODE
        _assert_tree_mode(base)

    def test_workspace_inputs_dir_mode(self, tmp_path):
        """inline 值落盘与外部文件拷入工作区：inputs/ 目录显式 0o750"""
        run_dir = tmp_path / "run_m3"
        run_dir.mkdir()
        inline_path = pre_judge_runner.write_inline_value("answer", {"a": 1}, run_dir)
        assert _dir_mode(Path(inline_path).parent) == RUNTIME_DIR_MODE

        src = tmp_path / "src_m3.txt"
        src.write_text("payload", encoding="utf-8")
        copied = pre_judge_runner.copy_into_workspace(str(src), run_dir, "doc")
        assert _dir_mode(Path(copied).parent) == RUNTIME_DIR_MODE


class TestHubRuntimeDirPermissions:
    """hub 侧运行时目录权限（hub_datasets 解包区 / hub_files 缓存 / 补传区）"""

    def test_unpack_zip_safely_nested_dirs_mode(self, tmp_path):
        """执行包解包：多级成员目录与显式目录成员全部显式 0o750"""
        zip_path = tmp_path / "pkg.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a/b/c.txt", "x")
            zf.writestr("d/", "")  # 显式目录成员
        dest = tmp_path / "hub_datasets" / "fid_m1"
        stats = hub_file_service.unpack_zip_safely(str(zip_path), str(dest))
        assert stats["file_count"] == 1
        assert (dest / "a" / "b" / "c.txt").is_file()
        assert (dest / "d").is_dir()
        _assert_tree_mode(tmp_path / "hub_datasets")

    def test_unpack_zip_cache_tree_mode(self, tmp_path):
        """hub_files 缓存解包（download_and_extract 内层 _unpack_zip）权限"""
        zip_path = tmp_path / "z.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("x/y.txt", "hello")
        dest = tmp_path / "hub_files" / "fid_m2" / "unpacked"
        files = hub_file_service.unpack_zip(str(zip_path), str(dest))
        assert len(files) == 1
        _assert_tree_mode(tmp_path / "hub_files")

    def test_hardlink_tree_mirror_dirs_mode(self, tmp_path):
        """任务级镜像目录（hardlink_tree 建父目录链）权限显式"""
        src = tmp_path / "src_root"
        (src / "nested").mkdir(parents=True)
        (src / "nested" / "f.txt").write_text("x", encoding="utf-8")
        mirror = tmp_path / "mirror" / "fid_m3"
        stats = hub_file_service.hardlink_tree(str(src), str(mirror))
        assert stats["linked"] + stats["copied"] == 1
        _assert_tree_mode(tmp_path / "mirror")

    def test_add_supplements_dirs_mode(self, tmp_path, monkeypatch, db_session):
        """人工补传：补传区根目录与子目录（relative_paths 多级）权限显式"""
        from app.models.hub_file_models import HubFileRecord

        unpack_dir = tmp_path / "hub_datasets" / "fid_m4"
        unpack_dir.mkdir(parents=True)
        (unpack_dir / "orig.jsonl").write_text("{}", encoding="utf-8")
        record = HubFileRecord(
            id="hub_m4", file_id="fid_m4", unpack_status="unpacked",
            unpack_dir=str(unpack_dir), file_index=[{"path": "orig.jsonl"}],
        )
        db_session.add(record)
        db_session.commit()

        supp_root = tmp_path / "hub_supplements"
        monkeypatch.setattr(
            hub_file_service, "_hub_supplements_root",
            lambda root=None: str(supp_root),
        )
        body = hub_file_service.add_supplements(
            db_session, "fid_m4",
            files=[("extra.txt", io.BytesIO(b"more"))],
            relative_paths=["sub/dir/extra.txt"],
            uploaded_by="arch03",
        )
        assert body["added_count"] == 1
        assert (supp_root / "fid_m4" / "sub" / "dir" / "extra.txt").is_file()
        _assert_tree_mode(supp_root)

    def test_download_to_file_creates_cache_dir_mode(self, tmp_path, monkeypatch):
        """下载落盘目录在发起任何网络请求前已按显式权限创建

        HubMcpClient 打桩为立即失败：只验证目录创建口径，不发真实请求
        """
        class _BoomClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                raise hub_file_service.HubFileError("测试桩：不发起真实下载")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(hub_file_service, "HubMcpClient", _BoomClient)
        svc = hub_file_service.HubFileService(endpoint="http://stub/mcp")
        dest = tmp_path / "hub_files" / "fid_m5"
        with pytest.raises(hub_file_service.HubFileError):
            svc.download_to_file("fid_m5", dest_dir=str(dest))
        assert dest.is_dir()
        _assert_tree_mode(tmp_path / "hub_files")


# ==================== 2. 越权路径读写拦截（ARCH-0-3） ====================

class TestEnsureWithinEscape:
    """符号链接 / 相对路径越界一律 PathEscapeError（读拼路径与写落盘同口径）"""

    def test_symlink_file_escape_blocked(self, tmp_path):
        """根内 symlink 指向根外文件：沿它拼读路径 → 拦截"""
        root = tmp_path / "root"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("top-secret", encoding="utf-8")
        (root / "leak").symlink_to(secret)
        with pytest.raises(PathEscapeError):
            ensure_within(str(root), "leak")

    def test_symlink_dir_escape_blocked(self, tmp_path):
        """根内 symlink 目录指向根外目录：沿它拼子路径 → 拦截"""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PathEscapeError):
            ensure_within(str(root), "escape", "pwned.txt")

    def test_dotdot_traversal_blocked(self, tmp_path):
        """.. 穿越逃出根目录 → 拦截"""
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(PathEscapeError):
            ensure_within(str(root), "..", "evil.txt")

    def test_absolute_part_join_blocked(self, tmp_path):
        """绝对路径段拼接（os.path.join 会被整段替换）→ 拦截"""
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(PathEscapeError):
            ensure_within(str(root), "/etc/passwd")

    def test_normal_nested_path_passes(self, tmp_path):
        """合法嵌套相对路径放行且落在根内（不误伤正常流程）"""
        root = tmp_path / "root"
        root.mkdir()
        (root / "a").mkdir()
        target = ensure_within(str(root), "a", "b.txt")
        assert target.startswith(str(root) + os.sep)

    def test_unpack_zip_symlink_member_write_blocked(self, tmp_path):
        """解包根内预置指向根外的 symlink：zip 成员沿它写出越界文件 → 拦截

        写路径真实链路（_unpack_zip）：PathEscapeError 在任何字节落盘前抛出，
        根外目录零残留
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "unpacked"
        root.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("escape/pwned.txt", "gotcha")
        with pytest.raises(PathEscapeError):
            hub_file_service._unpack_zip(str(zip_path), str(root))
        assert list(outside.iterdir()) == [], "越界写未拦截：根外出现残留文件"

    def test_unpack_zip_safely_symlink_member_write_blocked(self, tmp_path):
        """安全解包入口（unpack_zip_safely）对预置 symlink 同口径拦截"""
        outside = tmp_path / "outside2"
        outside.mkdir()
        dest = tmp_path / "hub_datasets" / "fid_evil"
        dest.mkdir(parents=True)
        (dest / "escape").symlink_to(outside, target_is_directory=True)
        zip_path = tmp_path / "evil2.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("escape/pwned.txt", "gotcha")
        with pytest.raises(PathEscapeError):
            hub_file_service.unpack_zip_safely(str(zip_path), str(dest))
        assert list(outside.iterdir()) == [], "越界写未拦截：根外出现残留文件"


class TestScriptRunDirEscape:
    """script_runs 运行目录创建链路：越界 task/data 标识绝不越出根建目录"""

    def test_build_run_dir_dotdot_traversal_blocked(self, tmp_path, monkeypatch):
        base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
        with pytest.raises(PathEscapeError):
            pre_judge_runner.build_run_dir("../evil_task", "d")
        # 越界目标零残留（旧实现会把目录建到 script_runs 之外）
        assert not (tmp_path / "evil_task").exists()

    def test_build_script_run_dir_dotdot_traversal_blocked(self, tmp_path, monkeypatch):
        base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
        with pytest.raises(PathEscapeError):
            pre_judge_runner.build_script_run_dir("../evil_task", "d", "pjs_x")
        assert not (tmp_path / "evil_task").exists()

    def test_build_run_dir_symlink_escape_blocked(self, tmp_path, monkeypatch):
        """根内预置 symlink 任务目录指向根外：沿它建运行目录 → 拦截"""
        outside = tmp_path / "outside_runs"
        outside.mkdir()
        base = tmp_path / "script_runs"
        base.mkdir()
        (base / "leak_task").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
        with pytest.raises(PathEscapeError):
            pre_judge_runner.build_run_dir("leak_task", "d1")
        assert list(outside.iterdir()) == [], "越界建目录未拦截：根外出现残留目录"

    def test_normal_ids_unchanged(self, tmp_path, monkeypatch):
        """合法 task/data/script 标识路径与行为不变（正常流程回归锚点）"""
        base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
        run_dir = pre_judge_runner.build_run_dir("task_ok", "data_ok")
        cwd = pre_judge_runner.build_script_run_dir("task_ok", "data_ok", "pjs_ok")
        assert run_dir == (base / "task_ok" / "data_ok").absolute()
        assert cwd == (base / "task_ok" / "data_ok" / "pjs_ok").absolute()
        assert run_dir.is_dir() and cwd.is_dir()
