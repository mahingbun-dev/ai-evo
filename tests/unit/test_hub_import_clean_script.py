"""清洗脚本导入链路单元测试（hub_import_job_service 的 script_id 分支）

覆盖：
- validate_clean_script：不存在 / 停用 / 类型不支持 / 按 id 与按名称命中
- create_import_job：script_id 校验通过落 job 行、校验失败不落库、不传兼容旧行为
- _run_clean_script：files 模式 stdin 组装、产物文件读行、fail 状态、无产物
- process_hub_import_job（挂清洗脚本）：逐包清洗落库 + CLEANSED_FIELD_METADATA
  注册、脚本缺失整体失败、单包清洗失败记 fail_detail 并补偿释放引用
- API：script_id 透传、非法脚本 400

mock 手法与 test_hub_import_job.py / test_hub_import_api.py 同构：
patch 一律打在源模块；db 用 MagicMock / FakeJobDb 式替身；文件用 tmp_path 真实落盘。
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.hub_import_job_service import (
    CLEANSED_FIELD_METADATA,
    HUB_FILE_ID_FIELD,
    _run_clean_script,
    create_import_job,
    process_hub_import_job,
    validate_clean_script,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

ROW_FILE_NAME = "hub_exec_cleaned.json"


def make_script_row(script_id="scr-clean01", name="hub_exec_clean",
                    exec_type="script", is_active=True):
    return SimpleNamespace(id=script_id, name=name, exec_type=exec_type,
                           is_active=is_active, entrypoint="main.py", timeout_seconds=120)


def make_script_db(row=None, by_name=None):
    """validate_clean_script 的 db 替身：query→filter→first 三级链
    第一次查询（按 id）命中 row，第二次（按名称）命中 by_name"""
    db = MagicMock()
    calls = {"n": 0}

    def query_side_effect(model):
        calls["n"] += 1
        chosen = row if calls["n"] == 1 else by_name
        filt = MagicMock()
        filt.filter.return_value.first.return_value = chosen
        return filt

    db.query.side_effect = query_side_effect
    return db


# ---------------------------------------------------------------------------
# validate_clean_script
# ---------------------------------------------------------------------------

class TestValidateCleanScript:
    def test_blank_returns_empty(self):
        assert validate_clean_script(MagicMock(), "") == ""
        assert validate_clean_script(MagicMock(), "   ") == ""

    def test_not_found_raises(self):
        db = make_script_db(row=None, by_name=None)
        with pytest.raises(ValueError) as exc:
            validate_clean_script(db, "scr-gone")
        assert "清洗脚本不存在" in str(exc.value)

    def test_inactive_raises(self):
        db = make_script_db(row=make_script_row(is_active=False))
        with pytest.raises(ValueError) as exc:
            validate_clean_script(db, "scr-off")
        assert "已停用" in str(exc.value)

    def test_non_script_type_raises(self):
        db = make_script_db(row=make_script_row(exec_type="claude_agent"))
        with pytest.raises(ValueError) as exc:
            validate_clean_script(db, "scr-agent")
        assert "仅支持 script" in str(exc.value)

    def test_valid_by_id(self):
        db = make_script_db(row=make_script_row(script_id="scr-1"))
        assert validate_clean_script(db, "scr-1") == "scr-1"

    def test_valid_by_name(self):
        # 按 id 未命中、按名称命中（与评测编排 _load_script 的兼容语义一致）
        db = make_script_db(row=None, by_name=make_script_row(script_id="scr-2", name="hub_exec_clean"))
        assert validate_clean_script(db, "hub_exec_clean") == "scr-2"


# ---------------------------------------------------------------------------
# create_import_job：script_id 校验与落库
# ---------------------------------------------------------------------------

class TestCreateImportJobWithScript:
    def _run(self, script_id, script_row=SimpleNamespace(
            id="scr-1", name="hub_exec_clean", exec_type="script", is_active=True)):
        db = MagicMock()
        added = []
        db.add.side_effect = lambda obj: added.append(obj)
        db.commit.return_value = None
        # validate_clean_script 内 db.query(...).filter(...).first() 三级链直接命中 script_row
        db.query.return_value.filter.return_value.first.return_value = script_row
        with patch("app.services.dataset_service.DatasetService") as mock_ds:
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-new")
            job = create_import_job(db, file_ids=["fid-1"], name="清洗集", script_id=script_id)
        return job, added

    def test_script_id_normalized_and_stored(self):
        job, added = self._run(script_id="hub_exec_clean")
        assert len(added) == 1
        assert job.script_id == "scr-1"  # 按名称命中后落规范化 id

    def test_no_script_id_stays_none(self):
        job, added = self._run(script_id="")
        assert len(added) == 1
        assert job.script_id is None

    def test_invalid_script_no_job_created(self):
        db = MagicMock()
        added = []
        db.add.side_effect = lambda obj: added.append(obj)
        with patch("app.services.dataset_service.DatasetService"), \
                patch("app.services.hub_import_job_service.validate_clean_script",
                      side_effect=ValueError("清洗脚本不存在: scr-gone")):
            with pytest.raises(ValueError) as exc:
                create_import_job(db, file_ids=["fid-1"], name="n", script_id="scr-gone")
        assert "清洗脚本不存在" in str(exc.value)
        assert added == []  # 校验失败不落 job 行


# ---------------------------------------------------------------------------
# _run_clean_script：stdin 组装与产物读取
# ---------------------------------------------------------------------------

class TestRunCleanScript:
    SCRIPT_DEF = {"script_id": "scr-1", "name": "hub_exec_clean", "exec_type": "script",
                  "entrypoint": "main.py", "timeout_seconds": 120}

    def test_ok_reads_row_from_files(self, tmp_path):
        row = {"schema_version": "hub_exec_cleaned_v1", "clean_summary": {"case_total": 3}}
        row_path = tmp_path / ROW_FILE_NAME
        row_path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        pkg_copy = tmp_path / "pkg"
        pkg_copy.mkdir()

        with patch("app.services.pre_judge_runner.build_run_dir", return_value=tmp_path), \
                patch("app.services.pre_judge_runner.copy_into_workspace",
                      return_value=str(pkg_copy)) as mock_copy, \
                patch("app.services.pre_judge_runner.execute_script") as mock_exec:
            mock_exec.return_value = {"status": "ok",
                                      "files": {ROW_FILE_NAME: str(row_path)}}
            result = _run_clean_script("/unpack/dir",
                                       [{"path": "attempt-1/execution-manifest.json",
                                         "size": 10, "type": "text"}],
                                       "fid-x", self.SCRIPT_DEF)

        assert result == row
        # 包拷贝进工作区（严格拷贝语义）
        assert mock_copy.call_args.args[:2] == ("/unpack/dir", tmp_path)
        # stdin files 模式：path=工作区内绝对路径、rel_path=索引 path
        stdin = mock_exec.call_args.args[1]
        assert stdin["task_id"] == "hub_import" and stdin["data_id"] == "fid-x"
        hub = stdin["inputs"]["hub_pkg"]
        assert hub["root"] == str(pkg_copy)
        assert hub["files"][0]["rel_path"] == "attempt-1/execution-manifest.json"
        assert hub["files"][0]["path"].startswith(str(pkg_copy))
        assert stdin["_run_dir"] == str(tmp_path)

    def test_no_file_index_omits_files_key(self, tmp_path):
        pkg_copy = tmp_path / "pkg"
        pkg_copy.mkdir()
        with patch("app.services.pre_judge_runner.build_run_dir", return_value=tmp_path), \
                patch("app.services.pre_judge_runner.copy_into_workspace",
                      return_value=str(pkg_copy)), \
                patch("app.services.pre_judge_runner.execute_script") as mock_exec:
            mock_exec.return_value = {"status": "ok", "output": {"schema_version": "v1"}}
            result = _run_clean_script("/unpack/dir", [], "fid-y", self.SCRIPT_DEF)
        # 索引为空时不带 files 键（脚本侧扫根兜底）
        assert "files" not in mock_exec.call_args.args[1]["inputs"]["hub_pkg"]
        assert result == {"schema_version": "v1"}  # 兜底 output

    def test_failed_status_raises(self, tmp_path):
        with patch("app.services.pre_judge_runner.build_run_dir", return_value=tmp_path), \
                patch("app.services.pre_judge_runner.copy_into_workspace",
                      return_value=str(tmp_path)), \
                patch("app.services.pre_judge_runner.execute_script") as mock_exec:
            mock_exec.return_value = {"status": "fail", "error": "no_cases_found"}
            with pytest.raises(RuntimeError) as exc:
                _run_clean_script("/unpack/dir", [], "fid-z", self.SCRIPT_DEF)
        assert "清洗脚本执行失败" in str(exc.value)
        assert "no_cases_found" in str(exc.value)

    def test_no_product_raises(self, tmp_path):
        with patch("app.services.pre_judge_runner.build_run_dir", return_value=tmp_path), \
                patch("app.services.pre_judge_runner.copy_into_workspace",
                      return_value=str(tmp_path)), \
                patch("app.services.pre_judge_runner.execute_script") as mock_exec:
            mock_exec.return_value = {"status": "ok", "files": {}, "output": None}
            with pytest.raises(RuntimeError) as exc:
                _run_clean_script("/unpack/dir", [], "fid-w", self.SCRIPT_DEF)
        assert "未产出可用的行内容" in str(exc.value)


# ---------------------------------------------------------------------------
# process_hub_import_job：挂清洗脚本的逐包分支
# ---------------------------------------------------------------------------

class FakeProcessDb:
    """process 流程用 db 替身：get_job 的 query→filter→first 三级链命中 job、
    EvaluationDataset 查询命中 dataset_row（W9：团队继承来源）、
    commit/rollback 计数"""

    def __init__(self, job):
        self.job = job
        # W9：目标数据集行替身（None = 查不到 → 团队按 DEFAULT 口径）
        self.dataset_row = None
        self.rolls = 0

    def query(self, model):
        filt = MagicMock()
        if getattr(model, "__name__", "") == "EvaluationDataset":
            filt.filter.return_value.first.return_value = self.dataset_row
        else:
            filt.filter.return_value.first.return_value = self.job
        return filt

    def commit(self):
        pass

    def rollback(self):
        self.rolls += 1


def make_job(file_ids, script_id=None, **kw):
    from app.models.hub_file_models import HubImportJob

    return HubImportJob(
        id="hubjob_clean001",
        dataset_id="ds-clean",
        file_ids=list(file_ids),
        script_id=script_id,
        status="pending",
        total_count=len(file_ids),
        success_count=0,
        fail_count=0,
        fail_detail={},
        succeeded_file_ids=[],
        created_by="tester",
    )


class TestProcessWithCleanScript:
    CLEAN_ROW = {"schema_version": "hub_exec_cleaned_v1",
                 "clean_summary": {"case_total": 18, "completion": {"completed": 18}}}

    def _run(self, tmp_path, monkeypatch, jobs, clean_side_effect=None,
             script_def=None or {"script_id": "scr-1", "name": "hub_exec_clean",
                                 "exec_type": "script", "entrypoint": "main.py",
                                 "timeout_seconds": 120}):
        rows = []
        meta = []
        released = []

        def fake_clean(unpack_dir, file_index, fid, sd):
            if clean_side_effect is not None:
                return clean_side_effect(unpack_dir, file_index, fid, sd)
            return dict(self.CLEAN_ROW)

        with patch("app.services.hub_file_service.ensure_unpacked") as mock_eu, \
                patch("app.services.hub_file_service.acquire_ref"), \
                patch("app.services.hub_file_service.release_ref",
                      side_effect=lambda db, fid: released.append(fid)), \
                patch("app.services.dataset_service.DatasetDataService") as mock_ds, \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs, \
                patch("app.services.pre_judge_runner._load_script",
                      return_value=script_def), \
                patch("app.services.hub_import_job_service._run_clean_script",
                      side_effect=fake_clean), \
                patch("app.services.hub_import_job_service._publish_job_progress"):
            mock_eu.return_value = {"unpack_dir": str(tmp_path / "unpacked"),
                                    "file_index": [], "summary": {}}
            mock_ds.return_value.add_data_v2.side_effect = \
                lambda dataset_id, data: rows.append((dataset_id, dict(data)))
            mock_fs.return_value.get_field_metadata.return_value = []
            mock_fs.return_value.save_field_metadata.side_effect = \
                lambda dataset_id, fields: meta.append((dataset_id, [dict(f) for f in fields]))
            results = []
            for job in jobs:
                db = FakeProcessDb(job)
                results.append((process_hub_import_job(db, job.id, datasets_root=str(tmp_path)),
                                db))
        return results, rows, meta, released

    def test_rows_from_clean_script(self, tmp_path, monkeypatch):
        job = make_job(["fid-1", "fid-2"], script_id="scr-1")
        [(result, _)], rows, meta, released = self._run(tmp_path, monkeypatch, [job])
        assert result["status"] == "completed" and result["success_count"] == 2
        # 数据行 = 清洗行内容 + hub_file 引用字段
        assert rows[0][1][HUB_FILE_ID_FIELD] == "fid-1"
        assert rows[0][1]["clean_summary"]["case_total"] == 18
        assert rows[1][1]["schema_version"] == "hub_exec_cleaned_v1"
        # 字段元数据注册为清洗字段集
        by_name = {f["name"]: f for f in meta[-1][1]}
        assert by_name[HUB_FILE_ID_FIELD]["type"] == "hub_file"
        assert by_name["session_messages"]["type"] == "json"
        assert by_name["review_material"]["type"] == "string"
        assert {m["name"] for m in CLEANSED_FIELD_METADATA} <= set(by_name)
        assert "hub_summary" not in by_name  # 原样导入字段不再注册
        assert released == []  # 全成功无补偿释放

    def test_script_load_failure_aborts(self, tmp_path, monkeypatch):
        job = make_job(["fid-1"], script_id="scr-gone")
        with pytest.raises(ValueError) as exc:
            self._run(tmp_path, monkeypatch, [job], script_def=None)
        assert "清洗脚本不可用" in str(exc.value)

    def test_single_package_clean_failure_recorded(self, tmp_path, monkeypatch):
        job = make_job(["fid-bad", "fid-ok"], script_id="scr-1")
        [(result, db)], rows, meta, released = self._run(
            tmp_path, monkeypatch, [job],
            clean_side_effect=lambda *a, **k: (
                (_ for _ in ()).throw(RuntimeError("清洗脚本执行失败: no_cases_found"))
                if a[2] == "fid-bad" else dict(self.CLEAN_ROW)))
        assert result["status"] == "failed"
        assert result["success_count"] == 1 and result["fail_count"] == 1
        assert "清洗脚本执行失败" in result["fail_detail"]["fid-bad"]
        assert len(rows) == 1 and rows[0][1][HUB_FILE_ID_FIELD] == "fid-ok"
        assert released == ["fid-bad"]  # 失败包补偿释放引用
        assert db.rolls >= 1
        # 成功项进幂等键，retry 只重跑失败项
        assert job.succeeded_file_ids == ["fid-ok"]

    def test_without_script_keeps_legacy_row(self, tmp_path, monkeypatch):
        job = make_job(["fid-1"], script_id=None)
        [(result, _)], rows, meta, released = self._run(tmp_path, monkeypatch, [job])
        assert result["status"] == "completed"
        # 旧行为：hub_file_id + hub_summary，且注册原样导入字段集
        assert rows[0][1] == {HUB_FILE_ID_FIELD: "fid-1", "hub_summary": {}}
        by_name = {f["name"]: f for f in meta[-1][1]}
        assert "hub_summary" in by_name
        assert "review_material" not in by_name


# ---------------------------------------------------------------------------
# API：script_id 透传与校验
# ---------------------------------------------------------------------------

class TestHubImportApiScriptId:
    def test_script_id_passthrough(self):
        from app.api.hub_import_api import HubFilesImportRequest, import_hub_files

        body = HubFilesImportRequest(file_ids=["fid-1"], name="n", script_id="scr-1")
        db = MagicMock()
        # create_import_job → validate_clean_script 的 db 三级链命中有效脚本行
        db.query.return_value.filter.return_value.first.return_value = make_script_row()
        db.add.side_effect = lambda obj: None
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.tasks.celery_tasks.run_hub_import_job"):
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-api")
            result = import_hub_files(body, request=None, db=db)
        assert result.script_id == "scr-clean01"  # 落库为规范化后的脚本 id
        assert result.status == "pending"

    def test_invalid_script_400(self):
        from fastapi import HTTPException

        from app.api.hub_import_api import HubFilesImportRequest, import_hub_files

        body = HubFilesImportRequest(file_ids=["fid-1"], name="n", script_id="scr-gone")
        db = MagicMock()
        # 按 id 与按名称都未命中 → ValueError → 400
        db.query.return_value.filter.return_value.first.return_value = None
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.tasks.celery_tasks.run_hub_import_job"):
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-api")
            with pytest.raises(HTTPException) as exc:
                import_hub_files(body, request=None, db=db)
        assert exc.value.status_code == 400
        assert "清洗脚本不存在" in exc.value.detail

    def test_blank_script_skips_validation(self):
        from app.api.hub_import_api import HubFilesImportRequest, import_hub_files

        body = HubFilesImportRequest(file_ids=["fid-1"], name="n", script_id="")
        db = MagicMock()
        db.add.side_effect = lambda obj: None
        query_spy = MagicMock()
        db.query.side_effect = query_spy
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.tasks.celery_tasks.run_hub_import_job"):
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-api")
            result = import_hub_files(body, request=None, db=db)
        assert result.script_id is None
        assert query_spy.call_count == 0  # 空 script_id 不触发脚本校验查询
