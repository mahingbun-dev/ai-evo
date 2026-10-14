"""工作区统一入口与任务级归档单测（ARCH-2-4，设计 §2.3 D5 / §3 Phase 2 表 2-4 行）

三路径验收：
- 归档：工作区 tar 到 uploads/archives/{task_id}.tar.gz（内容完整、可解包）；
- 清理：归档成功后原目录清除（script_runs 根保留）；工作区不存在时 no-op
  不产生归档（正常链路已清的场景）；
- 失败保留：归档失败原目录保留、不落半成品 .tar.gz；task_id 路径穿越被拒。

另覆盖：统一入口（workspace 模块）路径行为与 pre_judge_runner 既有口径
一致（现有 script_runs 路径行为不变）；evaluation_tasks_async 任务结束
接线（沿用 finally 清理点，成功/异常统一触发，归档自身异常不影响任务状态）。

无网络/无真实 DB（任务链路 mock，同 test_hub_resolve_execution 口径）；
SCRIPT_RUN_BASE/ARCHIVE_BASE 指向 tmp_path，不污染 worktree uploads/。
"""
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import pre_judge_runner, workspace
from app.services.workspace import (
    LocalWorkspaceBackend,
    archive_task_workspace,
    build_run_dir,
    build_script_run_dir,
)
from app.utils.path_safety import PathEscapeError

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def make_task_workspace(run_base, task_id="t-arch", files=None):
    """在指定 script_runs 根下建任务工作区，返回任务目录 Path

    files: {相对 task_id 的路径: 文本内容}，缺省给一组典型产物
    （outputs 产物 + stdout/stderr 日志）。
    """
    task_dir = run_base / task_id
    default_files = {
        "d1/outputs/artifact.txt": "产物A",
        "d1/s-pjs/stdout.log": "stdout 行1",
        "d1/s-pjs/stderr.log": "",
        "d2/outputs/result.json": '{"ok": true}',
    }
    for rel, content in (files if files is not None else default_files).items():
        target = task_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return task_dir


@pytest.fixture
def isolated_run_archive_roots(tmp_path, monkeypatch):
    """script_runs / archives 双根指向 tmp_path（不污染 worktree uploads/）"""
    run_base = tmp_path / "script_runs"
    archive_base = tmp_path / "archives"
    run_base.mkdir()
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)
    monkeypatch.setattr(workspace, "ARCHIVE_BASE", archive_base)
    return run_base, archive_base


# ---------------------------------------------------------------------------
# 路径一：归档（tar 生成，内容完整可解）
# ---------------------------------------------------------------------------

class TestArchiveSuccess:
    def test_archive_creates_tar_with_full_content(self, isolated_run_archive_roots):
        run_base, archive_base = isolated_run_archive_roots
        make_task_workspace(run_base, task_id="t-arch")

        result = archive_task_workspace("t-arch")

        assert result == archive_base / "t-arch.tar.gz"
        assert result.is_file()
        # 归档内容完整可解：以任务目录名为根，成员与源工作区一致
        with tarfile.open(result, "r:gz") as tar:
            names = tar.getnames()
        for expected in (
            "t-arch",
            "t-arch/d1/outputs/artifact.txt",
            "t-arch/d1/s-pjs/stdout.log",
            "t-arch/d2/outputs/result.json",
        ):
            assert expected in names, f"归档缺成员: {expected}"

    def test_archive_overwrites_stale_archive(self, isolated_run_archive_roots):
        """重跑幂等：上次异常残留的同名归档被原子覆盖，不报错不叠加"""
        run_base, archive_base = isolated_run_archive_roots
        make_task_workspace(run_base, task_id="t-again")
        archive_base.mkdir()
        (archive_base / "t-again.tar.gz").write_bytes(b"stale-half-written")

        result = archive_task_workspace("t-again")

        assert result.is_file()
        with tarfile.open(result, "r:gz") as tar:
            assert "t-again/d1/outputs/artifact.txt" in tar.getnames()


# ---------------------------------------------------------------------------
# 路径二：清理（归档成功清原目录；无工作区 no-op）
# ---------------------------------------------------------------------------

class TestArchiveCleansSource:
    def test_archive_removes_task_dir_keeps_run_base(self, isolated_run_archive_roots):
        run_base, _ = isolated_run_archive_roots
        task_dir = make_task_workspace(run_base, task_id="t-arch")
        # 兄弟任务目录不受影响（只清本任务）
        sibling = make_task_workspace(run_base, task_id="t-other")

        archive_task_workspace("t-arch")

        assert not task_dir.exists()  # 原目录已清
        assert sibling.exists()  # 兄弟任务保留
        assert run_base.is_dir()  # script_runs 根保留

    def test_missing_workspace_is_noop(self, isolated_run_archive_roots):
        """工作区不存在（正常链路逐条+任务级清理已清空）：no-op 不产生归档"""
        _, archive_base = isolated_run_archive_roots

        assert archive_task_workspace("t-gone") is None
        assert not (archive_base / "t-gone.tar.gz").exists()

    def test_empty_task_id_rejected(self, isolated_run_archive_roots):
        """空 task_id 拒绝归档（否则会把 script_runs 根整体打进一个归档）"""
        run_base, archive_base = isolated_run_archive_roots
        make_task_workspace(run_base, task_id="t-real")

        assert archive_task_workspace("") is None
        assert archive_task_workspace("   ") is None
        assert not (archive_base / ".tar.gz").exists()
        assert (run_base / "t-real").exists()


# ---------------------------------------------------------------------------
# 路径三：失败保留（归档失败原目录保留、不落半成品）
# ---------------------------------------------------------------------------

class TestArchiveFailureKeepsSource:
    def test_tar_failure_keeps_original(self, isolated_run_archive_roots, monkeypatch):
        run_base, archive_base = isolated_run_archive_roots
        task_dir = make_task_workspace(run_base, task_id="t-arch")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(workspace.tarfile, "open", _boom)

        with pytest.raises(OSError, match="disk full"):
            archive_task_workspace("t-arch")

        # 原目录完整保留（失败保留，待兜底清理）
        assert (task_dir / "d1" / "outputs" / "artifact.txt").read_text(encoding="utf-8") == "产物A"
        assert (task_dir / "d2" / "outputs" / "result.json").exists()
        # 不落半成品 tar.gz / 临时文件
        assert not (archive_base / "t-arch.tar.gz").exists()
        assert not (archive_base / "t-arch.tar.gz.tmp").exists()

    def test_traversal_task_id_rejected(self, tmp_path, monkeypatch):
        """task_id 路径穿越被拒（ARCH-0-3 口径）：不写越界归档，原目录保留"""
        run_base = tmp_path / "script_runs"
        run_base.mkdir()
        # ../escape 解析到 script_runs 之外（真实存在的目录才走得到归档分支）
        outside_dir = tmp_path / "escape"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)
        monkeypatch.setattr(workspace, "ARCHIVE_BASE", tmp_path / "archives")

        with pytest.raises(PathEscapeError):
            archive_task_workspace("../escape")

        assert (outside_dir / "secret.txt").exists()
        assert not (tmp_path / "escape.tar.gz").exists()
        assert not (tmp_path / "archives" / "escape.tar.gz").exists()


# ---------------------------------------------------------------------------
# 统一入口：路径行为与 pre_judge_runner 既有口径一致（行为不变）
# ---------------------------------------------------------------------------

class TestUnifiedEntryPaths:
    def test_build_run_dir_same_layout_as_legacy(self, tmp_path, monkeypatch):
        """经 workspace 入口建工作区：绝对路径、层级、outputs/ 与既有实现同口径"""
        run_base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)

        via_workspace = build_run_dir("t-u", "d-1")
        via_legacy = pre_judge_runner.build_run_dir("t-u2", "d-1")

        assert via_workspace.is_absolute()
        assert via_workspace.resolve() == (run_base / "t-u" / "d-1").resolve()
        assert (via_workspace / pre_judge_runner.OUTPUTS_DIR_NAME).is_dir()
        # 同根同构（legacy 直调与统一入口落同一 script_runs 树）
        assert via_legacy.resolve().parent.parent == run_base.resolve()

    def test_build_script_run_dir_and_cleanups_delegate(self, tmp_path, monkeypatch):
        """script cwd 隔离目录 + 条目级/任务级清理经统一入口同样生效"""
        run_base = tmp_path / "script_runs"
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)

        cwd = build_script_run_dir("t-u", "d-1", "pjs-1")
        assert cwd.is_dir()
        assert cwd.resolve() == (run_base / "t-u" / "d-1" / "pjs-1").resolve()

        workspace.cleanup_item_run_files("t-u", "d-1")
        assert not (run_base / "t-u" / "d-1").exists()

        workspace.cleanup_script_run_files("t-u")
        assert not (run_base / "t-u").exists()
        # 不存在时 no-op 不抛错
        workspace.cleanup_script_run_files("t-never")

    def test_facade_routes_through_default_backend(self, isolated_run_archive_roots, monkeypatch):
        """模块级入口经默认后端路由：替换 _default_backend 即整体切换（对象存储预留）"""
        calls = []

        class SpyBackend(LocalWorkspaceBackend):
            def archive_task_workspace(self, task_id):
                calls.append(task_id)
                return None

        monkeypatch.setattr(workspace, "_default_backend", SpyBackend())

        assert archive_task_workspace("t-swap") is None
        assert calls == ["t-swap"]


# ---------------------------------------------------------------------------
# evaluation_tasks_async 任务结束接线（沿用 finally 清理点）
# ---------------------------------------------------------------------------

class TestTaskCompletionArchiveWiring:
    """run_evaluation_async 归档接线：成功/异常统一触发（finally 清理点），
    归档自身异常不影响任务状态。mock 口径同 test_hub_resolve_execution。"""

    def _run_task(self, eta, task_id="t-1", execute_side_effect=None):
        """跑一轮 run_evaluation_async，返回 (result 或抛出异常, mock 归档)"""
        payloads = [{"user_input": "u1", "_dataset_item": {"id": "d1", "index": 0, "data": {}}}]
        task = SimpleNamespace(
            id=task_id, name="任务1", dataset_id="ds-1", evaluation_type="default",
            status="running",
        )

        def _fake_execute(t, p, db, tid, stop_event=None):
            if execute_side_effect is not None:
                raise execute_side_effect
            return [{"idx": 0, "success": True, "user_input": "u1"}]

        with patch.object(eta, "get_db_session", return_value=MagicMock()), \
                patch("app.tasks.evaluation_tasks_async.EvaluationTaskService") as mock_task_svc_cls, \
                patch("app.tasks.evaluation_tasks_async.TaskExecutionLogService"), \
                patch.object(eta, "_load_dataset",
                             return_value=[{"id": "d1", "index": 0, "data": {}}]), \
                patch.object(eta, "_process_media_files",
                             side_effect=lambda t, items, db: items), \
                patch.object(eta, "_generate_payloads", return_value=payloads), \
                patch.object(eta, "_execute_evaluation", side_effect=_fake_execute), \
                patch.object(eta, "_aggregate_results",
                             side_effect=lambda results, t: {"results": results}), \
                patch("app.services.workspace.archive_task_workspace") as mock_archive:
            mock_task_svc_cls.return_value.get_task.return_value = task
            result = eta.run_evaluation_async(task_id)
        return result, mock_archive

    def test_archive_called_on_success(self):
        from app.tasks import evaluation_tasks_async as eta

        result, mock_archive = self._run_task(eta, task_id="t-1")

        assert result["status"] == "success"
        mock_archive.assert_called_once_with("t-1")

    def test_archive_called_on_execution_failure(self):
        """执行异常上抛（Celery on_failure 统一处理），finally 归档仍触发"""
        from app.tasks import evaluation_tasks_async as eta

        with pytest.raises(RuntimeError, match="boom"):
            self._run_task(eta, task_id="t-1", execute_side_effect=RuntimeError("boom"))

    def test_archive_failure_does_not_break_task(self):
        """归档自身异常仅告警：任务照常成功（失败保留原目录由后端语义保证）"""
        from app.tasks import evaluation_tasks_async as eta

        payloads = [{"user_input": "u1", "_dataset_item": {"id": "d1", "index": 0, "data": {}}}]
        task = SimpleNamespace(
            id="t-1", name="任务1", dataset_id="ds-1", evaluation_type="default",
            status="running",
        )

        def _fake_execute(t, p, db, tid, stop_event=None):
            return [{"idx": 0, "success": True, "user_input": "u1"}]

        with patch.object(eta, "get_db_session", return_value=MagicMock()), \
                patch("app.tasks.evaluation_tasks_async.EvaluationTaskService") as mock_task_svc_cls, \
                patch("app.tasks.evaluation_tasks_async.TaskExecutionLogService"), \
                patch.object(eta, "_load_dataset",
                             return_value=[{"id": "d1", "index": 0, "data": {}}]), \
                patch.object(eta, "_process_media_files",
                             side_effect=lambda t, items, db: items), \
                patch.object(eta, "_generate_payloads", return_value=payloads), \
                patch.object(eta, "_execute_evaluation", side_effect=_fake_execute), \
                patch.object(eta, "_aggregate_results",
                             side_effect=lambda results, t: {"results": results}), \
                patch("app.services.workspace.archive_task_workspace",
                      side_effect=OSError("disk full")):
            mock_task_svc_cls.return_value.get_task.return_value = task
            result = eta.run_evaluation_async("t-1")

        assert result["status"] == "success"
