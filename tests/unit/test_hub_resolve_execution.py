"""anc-hub 执行包解析与评测链路接入单元测试（阶段B：验收 B1 / B2）

- B1 resolve_hub_file_fields 重写：有持久解包区 → hardlink_tree 镜像到
  {cache_root}/{file_id}/ 并就地替换字段值；无持久区（未登记 / failed /
  目录被清理）→ 抛 HubFileNotAvailable（中文含「请重新导入」、可定位 file_id）
- B2 evaluation_tasks_async 接入：单条数据缺持久区 → 该数据项标记失败
  （_hub_resolve_error）并 continue，其余数据继续评测、任务不中断；
  失败 payload 在执行前短路（_split_hub_unresolved_payloads），不发起模型评测
- 缺陷B4 镜像目录竞态防御：hardlink_tree 对目标被并发清理「重建重试一次，
  仍失败明确报错」；resolve_hub_file_fields 镜像后校验非空，不放行半成品

DB 用 MagicMock/SimpleNamespace（不连真实库）；镜像落盘用 pytest tmp_path，
不污染 worktree uploads/。
"""
import errno
import os
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import hub_file_service
from app.services.hub_file_service import (
    HubFileError,
    HubFileNotAvailable,
    hardlink_tree,
    resolve_hub_file_fields,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------------------------------------------------------------------
# mock 工具：登记表 hub_files 查询链按 file_id 返回记录
# ---------------------------------------------------------------------------

def make_record(unpack_dir, status="unpacked"):
    return SimpleNamespace(unpack_dir=str(unpack_dir), unpack_status=status)


def make_db(records):
    """records: {file_id: record 或 None}；按 file_id 返回登记记录

    query(HubFileRecord).filter(HubFileRecord.file_id == <值>).first() 的
    比较值为 BindParameter，从 filter 调用参数中还原 file_id。
    """
    db = MagicMock()

    def _first():
        expr = db.query.return_value.filter.call_args_list[-1].args[0]
        fid = getattr(getattr(expr, "right", None), "value", None)
        return records.get(fid)

    db.query.return_value.filter.return_value.first.side_effect = _first
    return db


def make_unpack_root(tmp_path, name="fid-pkg", files=None):
    """构造持久解包区目录（files: {相对路径: 内容}）"""
    root = tmp_path / "persist" / name
    for rel, content in (files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# B1: resolve_hub_file_fields 重写
# ---------------------------------------------------------------------------

class TestResolveHubFileFieldsB1:
    PACKAGE = {
        "execution-manifest.json": '{"execution_id": "e-1"}',
        "logs/run.log": "line1\n",
        "allure/results.json": "[]",
    }

    def test_persistent_dir_mirrored_via_hardlink(self, tmp_path):
        """有持久解包区 → hardlink_tree 镜像到 cache_root/{file_id}/，字段值就地替换"""
        unpack = make_unpack_root(tmp_path, files=self.PACKAGE)
        db = make_db({"fid-100": make_record(unpack)})
        data = {"pkg": "fid-100", "note": "普通字段"}
        cache_root = tmp_path / "task-1"

        with patch("app.services.hub_file_service.hardlink_tree",
                   side_effect=hardlink_tree) as mock_link:
            resolved = resolve_hub_file_fields(
                data, ["pkg"], cache_root=str(cache_root), db=db)

        mirror = str(cache_root / "fid-100")
        assert data["pkg"] == mirror
        assert data["note"] == "普通字段"
        assert resolved == {"pkg": mirror}
        # 确实走了硬链接镜像，且源目录是持久解包区
        mock_link.assert_called_once_with(str(unpack), mirror)
        # 镜像内容与源一致
        assert (cache_root / "fid-100" / "logs" / "run.log").read_text(encoding="utf-8") == "line1\n"

    def test_hardlink_same_inode_on_same_volume(self, tmp_path):
        """同卷场景：镜像文件与持久区文件是同一 inode（硬链接零拷贝）"""
        unpack = make_unpack_root(tmp_path, files={"a.txt": "hello"})
        db = make_db({"fid-ino": make_record(unpack)})
        data = {"pkg": "fid-ino"}
        resolve_hub_file_fields(data, ["pkg"], cache_root=str(tmp_path / "task"), db=db)
        src = unpack / "a.txt"
        dst = tmp_path / "task" / "fid-ino" / "a.txt"
        assert dst.read_text(encoding="utf-8") == "hello"
        if dst.stat().st_nlink > 1:  # 硬链接生效的文件系统上校验 inode 同一
            assert os.path.samefile(src, dst)

    def test_no_record_raises_with_reimport_hint(self):
        """未登记 → HubFileNotAvailable，中文含 file_id、字段名与「请重新导入」"""
        db = make_db({"fid-gone": None})
        data = {"pkg": "fid-gone"}
        with pytest.raises(HubFileNotAvailable) as ei:
            resolve_hub_file_fields(data, ["pkg"], cache_root="/tmp/x", db=db)
        msg = str(ei.value)
        assert "fid-gone" in msg and "pkg" in msg and "请重新导入" in msg

    def test_failed_status_raises_not_available(self, tmp_path):
        """登记状态 failed（解包失败）→ 同样按无持久区处理"""
        unpack = make_unpack_root(tmp_path, files={"a.txt": "x"})
        db = make_db({"fid-bad": make_record(unpack, status="failed")})
        with pytest.raises(HubFileNotAvailable, match="failed"):
            resolve_hub_file_fields({"pkg": "fid-bad"}, ["pkg"], db=db)

    def test_unpack_dir_missing_raises_not_available(self, tmp_path):
        """登记 unpacked 但解包目录已被清理 → 无持久区"""
        ghost = tmp_path / "persist" / "ghost"  # 不创建
        db = make_db({"fid-ghost": make_record(ghost)})
        with pytest.raises(HubFileNotAvailable, match="fid-ghost"):
            resolve_hub_file_fields({"pkg": "fid-ghost"}, ["pkg"], db=db)

    def test_fail_fast_on_first_missing_field(self, tmp_path):
        """多字段：第一个字段缺持久区立即抛出，后续字段不处理"""
        unpack = make_unpack_root(tmp_path, files={"a.txt": "x"})
        db = make_db({"fid-ok": make_record(unpack), "fid-miss": None})
        data = {"pkg_a": "fid-miss", "pkg_b": "fid-ok"}
        with pytest.raises(HubFileNotAvailable, match="pkg_a"):
            resolve_hub_file_fields(data, ["pkg_a", "pkg_b"], db=db)
        assert data["pkg_b"] == "fid-ok"  # 未被处理

    def test_mirror_reused_within_same_root(self, tmp_path):
        """同任务同 file_id 二次解析：复用已有镜像，不重复 hardlink_tree"""
        unpack = make_unpack_root(tmp_path, files={"a.txt": "x"})
        db = make_db({"fid-100": make_record(unpack)})
        cache_root = str(tmp_path / "task")
        with patch("app.services.hub_file_service.hardlink_tree",
                   side_effect=hardlink_tree) as mock_link:
            resolve_hub_file_fields({"pkg": "fid-100"}, ["pkg"], cache_root=cache_root, db=db)
            data2 = {"pkg": "fid-100"}
            resolve_hub_file_fields(data2, ["pkg"], cache_root=cache_root, db=db)
        assert mock_link.call_count == 1
        assert data2["pkg"] == os.path.join(cache_root, "fid-100")

    def test_unsafe_file_id_sanitized_as_dirname(self, tmp_path):
        """file_id 含路径字符时目录名安全化（镜像不逃出 cache_root）"""
        unpack = make_unpack_root(tmp_path, name="safe", files={"a.txt": "x"})
        db = make_db({"../evil": make_record(unpack)})
        data = {"pkg": "../evil"}
        resolve_hub_file_fields(data, ["pkg"], cache_root=str(tmp_path / "task"), db=db)
        assert data["pkg"] == str(tmp_path / "task" / "evil")  # sanitize 后 basename


# ---------------------------------------------------------------------------
# B2: evaluation_tasks_async 数据项级失败与短路
# ---------------------------------------------------------------------------

class TestProcessMediaFilesItemFailure:
    """_process_media_files：缺持久区数据项标记失败、其余数据继续处理"""

    def _task(self, dataset_id="ds-1", task_id="task-1"):
        return SimpleNamespace(dataset_id=dataset_id, id=task_id)

    def _db(self, hub_fields):
        db = MagicMock()
        rows = [SimpleNamespace(field_name=n) for n in hub_fields]
        db.query.return_value.filter.return_value.all.return_value = rows
        return db

    def _run(self, task, items, db):
        from app.tasks.evaluation_tasks_async import _process_media_files
        with patch("app.services.media_processor.MediaProcessor") as mock_mp, \
                patch("app.services.oss_media_cache_service.OSSMediaCacheService") as mock_oss:
            mock_mp.return_value.process_content.side_effect = lambda v: v
            mock_oss.return_value.validate_oss_url.return_value = False
            return _process_media_files(task, items, db)

    def test_missing_item_marked_failed_others_continue(self, tmp_path):
        """数据1 缺持久区 → 标记失败 continue；数据2 正常镜像替换，流程不中断"""
        unpack = make_unpack_root(tmp_path, name="fid-ok", files={"a.txt": "x"})
        db = self._db(["pkg"])
        # first 依调用顺序返回：数据1（fid-gone 无记录）→ 数据2（fid-ok 有记录）
        db.query.return_value.filter.return_value.first.side_effect = [
            None, make_record(unpack)
        ]
        items = [
            {"id": "d1", "index": 0, "data": {"pkg": "fid-gone", "note": "1"}},
            {"id": "d2", "index": 1, "data": {"pkg": "fid-ok", "note": "2"}},
        ]
        # 镜像不真实 hardlink（避免污染 worktree uploads/），但需落占位文件通过
        # 缺陷B4 的镜像非空校验；仅断言替换值与调用参数。
        # 任务级镜像根在真实 uploads/hub_files/{task_id}/ 下（_process_media_files
        # 内部拼接、不可注入），测试前后自清理，保证跨轮次幂等
        mirror_root = os.path.join(os.getcwd(), "uploads", "hub_files", "task-9")
        shutil.rmtree(mirror_root, ignore_errors=True)

        def _fake_link(src_root, dst_root):
            os.makedirs(dst_root, exist_ok=True)
            with open(os.path.join(dst_root, "a.txt"), "w", encoding="utf-8") as f:
                f.write("x")
        try:
            with patch("app.services.hub_file_service.hardlink_tree",
                       side_effect=_fake_link) as mock_link:
                processed = self._run(self._task(task_id="task-9"), items, db)
        finally:
            shutil.rmtree(mirror_root, ignore_errors=True)

        assert len(processed) == 2  # 任务不中断，两条都在
        # 数据1：失败标记含 file_id，字段原值保留
        assert "fid-gone" in processed[0]["_hub_resolve_error"]
        assert processed[0]["data"]["pkg"] == "fid-gone"
        # 数据2：正常替换为任务级镜像目录，且以持久解包区为源
        assert processed[1]["data"]["pkg"] == os.path.join(
            os.getcwd(), "uploads", "hub_files", "task-9", "fid-ok")
        assert "_hub_resolve_error" not in processed[1]
        mock_link.assert_called_once_with(
            str(unpack), os.path.join(os.getcwd(), "uploads", "hub_files", "task-9", "fid-ok"))

    def test_cache_root_task_scoped_and_db_forwarded(self, tmp_path):
        """resolve 调用透传任务级 cache_root 与 db 会话（签名兼容阶段A接线）"""
        db = self._db(["pkg"])
        items = [{"id": "d1", "index": 0, "data": {"pkg": "fid-1"}}]
        with patch("app.services.hub_file_service.resolve_hub_file_fields") as mock_resolve:
            self._run(self._task(task_id="task-abc"), items, db)
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs["cache_root"] == os.path.join(
            os.getcwd(), "uploads", "hub_files", "task-abc")
        assert kwargs["db"] is db


class TestSplitHubUnresolvedPayloads:
    """_split_hub_unresolved_payloads：失败 payload 短路为失败结果，其余可执行"""

    def _payload(self, item_id, error=None):
        item = {"id": item_id, "index": 0, "data": {"note": "x"}}
        if error:
            item["_hub_resolve_error"] = error
        return {"user_input": "u", "_dataset_item": item}

    def test_split_failed_and_runnable(self):
        from app.tasks.evaluation_tasks_async import _split_hub_unresolved_payloads
        payloads = [
            self._payload("d1"),
            self._payload("d2", error="字段 pkg 的执行包（file_id=fid-gone）无持久解包区，请重新导入"),
            self._payload("d3"),
        ]
        failed, runnable = _split_hub_unresolved_payloads(payloads)
        assert len(runnable) == 2
        assert runnable[0]["_dataset_item"]["id"] == "d1"
        assert runnable[1]["_dataset_item"]["id"] == "d3"
        assert len(failed) == 1
        assert failed[0]["idx"] == 1
        assert failed[0]["success"] is False
        assert "fid-gone" in failed[0]["error"]

    def test_no_error_all_runnable(self):
        from app.tasks.evaluation_tasks_async import _split_hub_unresolved_payloads
        payloads = [self._payload("d1"), self._payload("d2")]
        failed, runnable = _split_hub_unresolved_payloads(payloads)
        assert failed == []
        assert runnable == payloads

    def test_non_dict_payload_tolerated(self):
        """非 dict 的畸形 payload 不炸分流（归入可执行列表，由执行器自行报错）"""
        from app.tasks.evaluation_tasks_async import _split_hub_unresolved_payloads
        ok = self._payload("d1")
        failed, runnable = _split_hub_unresolved_payloads([None, ok])
        assert failed == []
        assert runnable == [None, ok]


class TestRunEvaluationAsyncShortCircuit:
    """run_evaluation_async 步骤5 接线：失败数据项不进评测、其余继续、任务不中断"""

    def test_hub_failed_items_short_circuited(self):
        from app.tasks import evaluation_tasks_async as eta

        failed_item = {"id": "d1", "index": 0, "data": {"pkg": "fid-gone"},
                       "_hub_resolve_error": "字段 pkg 的执行包（file_id=fid-gone）无持久解包区，请重新导入"}
        ok_item = {"id": "d2", "index": 1, "data": {"pkg": "/mirror/fid-ok"}}
        payloads = [
            {"user_input": "u1", "_dataset_item": failed_item},
            {"user_input": "u2", "_dataset_item": ok_item},
        ]
        executed = {"payloads": None, "results": None}

        def _fake_execute(task, payloads, db, task_id, stop_event=None):
            executed["payloads"] = payloads
            return [{"idx": 0, "success": True, "user_input": "u2"}]

        task = SimpleNamespace(
            id="t-1", name="任务1", dataset_id="ds-1", evaluation_type="default",
            status="running",
        )
        with patch.object(eta, "get_db_session", return_value=MagicMock()), \
                patch("app.tasks.evaluation_tasks_async.EvaluationTaskService") as mock_task_svc_cls, \
                patch("app.tasks.evaluation_tasks_async.TaskExecutionLogService") as mock_log_svc_cls, \
                patch.object(eta, "_load_dataset", return_value=[failed_item, ok_item]), \
                patch.object(eta, "_process_media_files",
                             side_effect=lambda t, items, db: items), \
                patch.object(eta, "_generate_payloads", return_value=payloads), \
                patch.object(eta, "_execute_evaluation", side_effect=_fake_execute), \
                patch.object(eta, "_aggregate_results",
                             side_effect=lambda results, t: {"results": results}):
            mock_task_svc_cls.return_value.get_task.return_value = task
            result = eta.run_evaluation_async("t-1")

        assert result["status"] == "success"
        # 失败数据项未发起评测
        assert [p["_dataset_item"]["id"] for p in executed["payloads"]] == ["d2"]
        # 失败事实合入最终结果（含 file_id 可定位）
        final_results = result["result"]["results"]
        assert any(r.get("success") is False and "fid-gone" in r.get("error", "")
                   for r in final_results)
        # 记录了 warning 日志（数据项级失败对任务日志可见）
        add_log = mock_log_svc_cls.return_value.add_log
        assert any(
            call.kwargs.get("level") == "warning" or (call.args and call.args[1] == "warning")
            for call in add_log.call_args_list)


# ---------------------------------------------------------------------------
# 缺陷B4：镜像目录竞态防御（celery 孤儿消息重投：一个 worker rmtree 缓存目录
# vs 另一个 worker hardlink 镜像中 → 半成品镜像让脚本误报匹配 0 文件）
# ---------------------------------------------------------------------------

class TestMirrorRaceDefense:
    def test_hardlink_tree_retries_when_target_concurrently_removed(self, tmp_path, monkeypatch):
        """目标目录被并发清理：重建父目录重试一次后成功，镜像完整"""
        src = make_unpack_root(tmp_path, name="src-race",
                               files={"a.txt": "A", "sub/b.txt": "B"})
        dst = tmp_path / "mirror-race"
        real_copy2 = shutil.copy2
        copy_calls = {"n": 0}

        def _always_fail_link(src_path, dst_path):
            raise OSError(errno.EPERM, "模拟硬链接不可用（跨卷）")

        def _first_copy_fails(src_path, dst_path, **kw):
            copy_calls["n"] += 1
            if copy_calls["n"] == 1:
                raise FileNotFoundError(errno.ENOENT, "目标目录被并发清理（模拟）")
            return real_copy2(src_path, dst_path, **kw)

        monkeypatch.setattr(hub_file_service.os, "link", _always_fail_link)
        monkeypatch.setattr(hub_file_service.shutil, "copy2", _first_copy_fails)

        stats = hardlink_tree(str(src), str(dst))
        assert stats == {"linked": 0, "copied": 2}
        assert copy_calls["n"] == 3  # a.txt 失败重试成功 + sub/b.txt 成功
        assert (dst / "a.txt").read_text(encoding="utf-8") == "A"
        assert (dst / "sub" / "b.txt").read_text(encoding="utf-8") == "B"

    def test_hardlink_tree_raises_after_retry_exhausted(self, tmp_path, monkeypatch):
        """重试后仍失败：明确抛 HubFileError（中文），不静默产出半成品镜像"""
        src = make_unpack_root(tmp_path, name="src-dead", files={"a.txt": "A"})
        dst = tmp_path / "mirror-dead"

        def _dead_link(src_path, dst_path):
            raise OSError(errno.EPERM, "模拟硬链接不可用")

        def _dead_copy2(src_path, dst_path, **kw):
            raise FileNotFoundError(errno.ENOENT, "目标目录持续被并发清理（模拟）")

        monkeypatch.setattr(hub_file_service.os, "link", _dead_link)
        monkeypatch.setattr(hub_file_service.shutil, "copy2", _dead_copy2)

        with pytest.raises(HubFileError, match="并发清理"):
            hardlink_tree(str(src), str(dst))
        assert not (dst / "a.txt").exists()

    def test_resolve_keeps_original_when_mirror_empty(self, tmp_path):
        """镜像完成后目录为空（被并发清理）：保留原值，不放行空镜像路径"""
        unpack = make_unpack_root(tmp_path, name="fid-race", files={"a.txt": "x"})
        db = make_db({"fid-race": make_record(unpack)})
        data = {"pkg": "fid-race"}
        with patch("app.services.hub_file_service.hardlink_tree",
                   return_value={"linked": 0, "copied": 0}):  # 模拟镜像未落任何文件
            resolved = resolve_hub_file_fields(
                data, ["pkg"], cache_root=str(tmp_path / "task"), db=db)
        assert data["pkg"] == "fid-race"  # 保留原 file_id，不指向空目录
        assert resolved == {}

    def test_resolve_keeps_original_when_mirror_wiped_after_build(self, tmp_path):
        """镜像建成后目录被并发 worker 整体删除：同样保留原值（非空校验兜底）"""
        unpack = make_unpack_root(tmp_path, name="fid-wipe", files={"a.txt": "x"})
        db = make_db({"fid-wipe": make_record(unpack)})
        data = {"pkg": "fid-wipe"}

        def _link_then_wipe(src_root, dst_root):
            hardlink_tree(src_root, dst_root)
            shutil.rmtree(dst_root, ignore_errors=True)  # 模拟并发清理竞态窗口

        with patch("app.services.hub_file_service.hardlink_tree", side_effect=_link_then_wipe):
            resolved = resolve_hub_file_fields(
                data, ["pkg"], cache_root=str(tmp_path / "task"), db=db)
        assert data["pkg"] == "fid-wipe"
        assert resolved == {}
