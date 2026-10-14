"""W1 RCE 链集成测试（整改开发计划 §2 W1「测试设计」集成项 + 模板包冒烟）

模拟完整攻击链（在整改前基线 657fc49 上应失败，红→绿）：
1. 恶意文件名上传（media / dataset 临时上传两处）→ 断言落盘路径在
   uploads 目标目录内、越界目标不存在；
2. 创建/更新 entrypoint 指向 `/etc/hosts`（非 .py、绝对路径）→ 服务端拒绝
   （service 层 ValueError；runner 层绝对路径/.. 穿越一律「脚本入口不存在」，
   绝不解析到脚本包之外）；
3. 普通用户调 `_ensure_admin` 保护的端点 → 403；超管 → 200；
4. 模板包冒烟：由 app/static/script_templates 脚手架模板（hub_package）
   生成的真实包在白名单 env 下经 runner 正常执行（PATH/LANG 足够）。

独立测试角色编写；只新增测试，不修改实现代码。
"""
import asyncio
import io
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.w9_direct_call import fake_request  # W9: 端点直调适配
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.dataset_models import EvaluationDataset, ImportTempFile
from app.services import pre_judge_runner, pre_judge_script_service
from app.services.media_service import MediaService
from app.services.pre_judge_script_service import PreJudgeScriptService
from app.api import dataset_api
from app.api.dataset_api import upload_temp_file
from app.api.pre_judge_script_api import (
    PreJudgeScriptDebugPayload,
    PreJudgeScriptPayload,
    create_script,
    debug_script,
    delete_script,
    update_script,
    upload_script_file,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

# 简单回显脚本（admin gate 超管 200 断言用；冒烟契约合法）
ECHO_SCRIPT = '''
import json

def run(stdin):
    return {"status": "ok", "output": {"echo": "hi"}, "files": {},
            "summary": "ok", "finish_reason": "completed", "error": None}
'''


# ---------------- 测试基建 ----------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def isolated_upload_paths(tmp_path, monkeypatch):
    upload_base = tmp_path / "pre_judge_scripts"
    upload_base.mkdir()
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")
    return upload_base


def _request(user):
    """构造最小 Request（模拟鉴权中间件写入 request.state.user）"""
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [], "query_string": b""})
    request.state.user = user
    return request


ADMIN_USER = {"user_code": "w1_admin", "super_admin_flag": 1}
NORMAL_USER = {"user_code": "w1_normal", "super_admin_flag": 0}
NO_FLAG_USER = {"user_code": "w1_noflag"}


def _admin_request():
    return _request(ADMIN_USER)


def _contained_in(resolved: Path, directory: Path) -> bool:
    """resolved 是否位于 directory 之内（含边界的下一级）"""
    dir_s = str(directory.resolve())
    return str(resolved) == dir_s or str(resolved).startswith(dir_s + os.sep)


# ==================== 1. 恶意文件名上传（W1-1） ====================

class TestMaliciousFilenameUpload:
    def test_media_upload_traversal_contained(self, db_session, tmp_path):
        """恶意文件名 ../../../../…/evil.py 上传媒体 → 落盘必须在 upload_dir 内"""
        upload_dir = tmp_path / "media_uploads"
        marker = uuid.uuid4().hex[:8]
        outside_path = Path("/tmp") / f"w1_rce_evil_{marker}.py"
        service = MediaService(db_session, upload_dir=str(upload_dir))
        file = UploadFile(
            file=io.BytesIO(b"evil-content"),
            filename=f"../../../../../../../../tmp/w1_rce_evil_{marker}.py")
        try:
            media = asyncio.run(service.upload_file(file))
        finally:
            # 防御清理：若实现仍把文件越界写到 /tmp（红验证场景），不留残留
            if outside_path.exists():
                outside_path.unlink()
        resolved = Path(media.file_path).resolve()
        assert _contained_in(resolved, upload_dir), \
            f"媒体落盘越出上传目录: {resolved}（upload_dir={upload_dir}）"
        # 越界目标不得存在（旧实现会把文件写到 uploads 之外）
        assert not (Path("/tmp") / f"w1_rce_evil_{marker}.py").exists(), \
            "恶意文件名把文件写到了上传目录之外"
        # DB 记录的文件名是清洗后的名字（不含路径成分）
        assert media.file_name == f"w1_rce_evil_{marker}.py"
        assert "/" not in media.file_name and ".." not in media.file_name

    def test_media_upload_empty_filename_falls_back(self, db_session, tmp_path):
        upload_dir = tmp_path / "media_uploads"
        service = MediaService(db_session, upload_dir=str(upload_dir))
        file = UploadFile(file=io.BytesIO(b"data"), filename="")
        media = asyncio.run(service.upload_file(file))
        assert media.file_name == "upload.bin"
        assert _contained_in(Path(media.file_path).resolve(), upload_dir)

    def test_dataset_temp_upload_traversal_contained(self, db_session, tmp_path,
                                                     monkeypatch):
        """数据集预览临时上传：恶意文件名 → 落盘必须在 temp 目录内"""
        monkeypatch.chdir(tmp_path)  # 端点内 temp_dir 为 ./uploads/datasets/temp
        # W9：upload_temp_file 对真实形态 dataset_id 做归属校验，先落数据集行
        db_session.add(EvaluationDataset(id="ds_w1_rce", name="rce-temp", is_active=True))
        db_session.commit()
        marker = uuid.uuid4().hex[:8]
        file = UploadFile(
            file=io.BytesIO(b"evil-content"),
            filename=f"../../../../w1_rce_evil_{marker}.txt")
        body = asyncio.run(upload_temp_file(
            dataset_id="ds_w1_rce", request=fake_request(), data_index=0, field="answer",
            file=file, db=db_session))
        temp_dir = tmp_path / "uploads" / "datasets" / "temp"
        assert temp_dir.is_dir()
        # 返回与 DB 记录都是清洗后的文件名
        assert body["file_name"] == f"w1_rce_evil_{marker}.txt"
        row = db_session.query(ImportTempFile).filter(ImportTempFile.id == body["id"]).one()
        assert row.file_name == f"w1_rce_evil_{marker}.txt"
        assert _contained_in(Path(row.file_path).resolve(), temp_dir), \
            f"临时上传落盘越出 temp 目录: {row.file_path}"
        # temp 目录内的全部落盘文件都在目录内（无越界残留）
        for path in temp_dir.iterdir():
            assert _contained_in(path.resolve(), temp_dir)
        # 上级（uploads/、tmp_path 根）无越界残留
        uploads_root = (tmp_path / "uploads").resolve()
        outside = [p for p in uploads_root.rglob(f"*{marker}*")
                   if not _contained_in(p.resolve(), temp_dir)]
        assert not outside, f"恶意文件名在 temp 目录外留下文件: {outside}"

    def test_media_upload_over_limit_413(self, db_session, tmp_path, monkeypatch):
        """上传超过 UPLOAD_MAX_BYTES → 413（读满上限即刻中止）"""
        from app.services import media_service as media_service_module

        monkeypatch.setattr(
            media_service_module, "get_settings",
            lambda: SimpleNamespace(UPLOAD_MAX_BYTES=8))
        service = MediaService(db_session, upload_dir=str(tmp_path / "m"))
        file = UploadFile(file=io.BytesIO(b"x" * 9), filename="big.bin")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(service.upload_file(file))
        assert exc.value.status_code == 413

    def test_dataset_temp_upload_over_limit_413(self, db_session, tmp_path,
                                                monkeypatch):
        monkeypatch.chdir(tmp_path)
        db_session.add(EvaluationDataset(id="ds_w1_rce", name="rce-temp-2", is_active=True))
        db_session.commit()
        monkeypatch.setattr(
            dataset_api, "get_settings",
            lambda: SimpleNamespace(UPLOAD_MAX_BYTES=8))
        file = UploadFile(file=io.BytesIO(b"x" * 9), filename="big.bin")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_temp_file(
                dataset_id="ds_w1_rce", request=fake_request(), data_index=0, field="answer",
                file=file, db=db_session))
        assert exc.value.status_code == 413


# ==================== 2. entrypoint 注入（W1-2） ====================

class TestEntrypointInjection:
    def test_update_entrypoint_etc_hosts_rejected(self, db_session,
                                                  isolated_upload_paths):
        """entrypoint 指向 /etc/hosts（绝对路径且非 .py）→ service 层拒绝"""
        service = PreJudgeScriptService(db_session)
        created = service.create_script(
            {"name": f"w1_rce_{uuid.uuid4().hex[:6]}", "exec_type": "script"})
        for evil in ("/etc/hosts", "../../../etc/hosts", "/etc/passwd"):
            with pytest.raises(ValueError):
                service.update_script(created["id"], {"entrypoint": evil})

    def test_runner_rejects_absolute_entrypoint(self, isolated_upload_paths):
        """运行器兜底：绝对路径 entrypoint 一律「脚本入口不存在」拒绝执行，
        绝不解析到脚本包之外（历史绝对路径直执分支已删除）"""
        script_def = {
            "script_id": "pjs_w1_abs", "name": "w1_abs",
            "exec_type": "script", "entrypoint": "/etc/hosts",
            "timeout_seconds": 30,
        }
        result = pre_judge_runner.execute_script(script_def, {"fields": {}}, 30)
        assert result["status"] == "fail"
        assert "绝对路径" in result["error"] or "脚本入口不存在" in result["error"]
        assert "/etc/hosts" in result["error"]

    def test_runner_rejects_absolute_entrypoint_to_real_py(
            self, isolated_upload_paths):
        """更狠的变体：绝对路径指向真实可执行 .py（有 run() 函数）
        也必须拒绝——旧实现会真的去执行包外文件"""
        target = pre_judge_script_service.SCRIPT_UPLOAD_BASE / "outside_target.py"
        target.write_text(
            "import json\n\ndef run(stdin):\n"
            "    return {'status': 'ok', 'output': {'executed_outside': True}}\n",
            encoding="utf-8")
        script_def = {
            "script_id": "pjs_w1_abs2", "name": "w1_abs2",
            "exec_type": "script", "entrypoint": str(target),
            "timeout_seconds": 30,
        }
        result = pre_judge_runner.execute_script(script_def, {"fields": {}}, 30)
        assert result["status"] == "fail"
        assert "绝对路径" in result["error"]
        # 关键：包外文件绝不能被执行（执行了就会输出 executed_outside）
        assert result.get("output") is None or "executed_outside" not in (result.get("output") or {})

    def test_runner_rejects_dotdot_traversal_entrypoint(self, isolated_upload_paths):
        """.. 穿越 entrypoint：解析结果不得越出脚本包目录"""
        base = pre_judge_script_service.SCRIPT_UPLOAD_BASE
        # 在包外（base 的上一级）放一个诱饵入口，穿越路径指向它
        bait = base.parent / "bait_outside.py"
        bait.write_text(
            "import json\n\ndef run(stdin):\n"
            "    return {'status': 'ok', 'output': {'escaped_package': True}}\n",
            encoding="utf-8")
        # 脚本包内放一个合法入口（不被使用）
        pkg = base / "pjs_w1_trav"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text("# legit entry\n", encoding="utf-8")
        script_def = {
            "script_id": "pjs_w1_trav", "name": "w1_trav",
            "exec_type": "script", "entrypoint": "../bait_outside.py",
            "timeout_seconds": 30,
        }
        result = pre_judge_runner.execute_script(script_def, {"fields": {}}, 30)
        assert result["status"] == "fail"
        assert result.get("output") is None or "escaped_package" not in (result.get("output") or {})

    def test_runner_missing_entry_file_fails_cleanly(self, isolated_upload_paths):
        """合法相对路径但文件不存在 → 明确失败（不静默执行别的东西）"""
        script_def = {
            "script_id": "pjs_w1_missing", "name": "w1_missing",
            "exec_type": "script", "entrypoint": "ghost.py",
            "timeout_seconds": 30,
        }
        result = pre_judge_runner.execute_script(script_def, {"fields": {}}, 30)
        assert result["status"] == "fail"
        assert "脚本入口不存在" in result["error"] or "入口" in result["error"]


# ==================== 3. 管理员门禁（W1-4） ====================

class TestAdminGate:
    """普通用户（flag=0 / 无标志）调写操作端点一律 403；超管正常 200"""

    def _create_script_row(self, db_session):
        return asyncio.run(create_script(
            PreJudgeScriptPayload(name=f"w1_gate_{uuid.uuid4().hex[:6]}",
                                  exec_type="script", timeout_seconds=30),
            request=_admin_request(), db=db_session))

    @pytest.mark.parametrize("user", [NORMAL_USER, NO_FLAG_USER])
    def test_create_script_denied_for_non_admin(self, db_session, user):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(create_script(
                PreJudgeScriptPayload(name=f"w1_gate_{uuid.uuid4().hex[:6]}",
                                      exec_type="script", timeout_seconds=30),
                request=_request(user), db=db_session))
        assert exc.value.status_code == 403

    def test_create_script_ok_for_admin(self, db_session):
        created = self._create_script_row(db_session)
        assert created["id"].startswith("pjs_")

    @pytest.mark.parametrize("user", [NORMAL_USER, NO_FLAG_USER])
    def test_update_script_denied_for_non_admin(self, db_session, user):
        created = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(update_script(
                created["id"],
                PreJudgeScriptPayload(name=created["name"], exec_type="script",
                                      description="tampered"),
                request=_request(user), db=db_session))
        assert exc.value.status_code == 403

    def test_update_script_ok_for_admin(self, db_session, isolated_upload_paths):
        created = self._create_script_row(db_session)
        updated = asyncio.run(update_script(
            created["id"],
            PreJudgeScriptPayload(name=created["name"], exec_type="script",
                                  description="legit-edit"),
            request=_admin_request(), db=db_session))
        assert updated["description"] == "legit-edit"

    @pytest.mark.parametrize("user", [NORMAL_USER, NO_FLAG_USER])
    def test_delete_script_denied_for_non_admin(self, db_session, user):
        created = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(delete_script(created["id"], request=_request(user),
                                      db=db_session))
        assert exc.value.status_code == 403
        # 记录仍在（未被删除）
        assert PreJudgeScriptService(db_session).get_script(created["id"]) is not None

    def test_delete_script_ok_for_admin(self, db_session, isolated_upload_paths):
        created = self._create_script_row(db_session)
        body = asyncio.run(delete_script(created["id"], request=_admin_request(),
                                         db=db_session))
        assert body == {"deleted": True, "script_id": created["id"]}

    @pytest.mark.parametrize("user", [NORMAL_USER, NO_FLAG_USER])
    def test_upload_script_file_denied_for_non_admin(self, db_session, user):
        created = self._create_script_row(db_session)
        file = UploadFile(file=io.BytesIO(ECHO_SCRIPT.encode("utf-8")),
                          filename="echo.py")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(upload_script_file(
                created["id"], request=_request(user), file=file,
                risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert exc.value.status_code == 403

    def test_upload_and_debug_ok_for_admin(self, db_session,
                                           isolated_upload_paths):
        """超管全链 200：上传 → debug 试跑真实子进程执行成功"""
        created = self._create_script_row(db_session)
        file = UploadFile(file=io.BytesIO(ECHO_SCRIPT.encode("utf-8")),
                          filename="echo.py")
        saved = asyncio.run(upload_script_file(
            created["id"], request=_admin_request(), file=file,
            risk_acknowledged=None, declared_requirements=None, db=db_session))
        assert saved["entrypoint"].endswith(".py")

        body = asyncio.run(debug_script(
            created["id"], PreJudgeScriptDebugPayload(fields={}),
            request=_admin_request(), db=db_session))
        assert body["result"]["status"] == "ok"
        assert body["result"]["output"]["echo"] == "hi"

    @pytest.mark.parametrize("user", [NORMAL_USER, NO_FLAG_USER])
    def test_debug_denied_for_non_admin(self, db_session, user):
        """debug 试跑与写操作同口径：非超管 403（试跑可执行任意脚本，同属高危）"""
        created = self._create_script_row(db_session)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(debug_script(
                created["id"], PreJudgeScriptDebugPayload(fields={}),
                request=_request(user), db=db_session))
        assert exc.value.status_code == 403


# ==================== 4. 模板包冒烟（白名单 env 下正常执行） ====================

class TestTemplatePackageSmoke:
    def test_hub_package_template_runs_under_whitelist_env(
            self, tmp_path, monkeypatch, sample_script_package):
        """由 app/static/script_templates 模板（hub_package）生成的真实包，
        经 runner 真实子进程在白名单 env 下执行成功（PATH/LANG 足够）：
        消费 32 文件执行包、提取 manifest 摘要、产物落 outputs/ 注册 files。

        模板设计意图：scripts/hub_package/ 为脚本包根（main.py + lib 兄弟
        import），inputs/ 为样例执行包数据——冒烟按该布局组包。
        ARCH-1-4 后缺省执行模式为 container：本用例断言的是 local 通道契约
        （白名单 env 下宿主侧产物路径可见），显式声明 local——与
        tests/unit/conftest.py 的套件级钉 local 同口径（tests/security 无
        套件 conftest，用例体内自声明）。"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "local")
        upload_base = tmp_path / "pre_judge_scripts"
        upload_base.mkdir()
        monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")

        script_id = f"pjs_smoke_{uuid.uuid4().hex[:6]}"
        script_dir = upload_base / script_id
        # 拷贝模板脚本包（等同 save_script_package 的落位形态：包根=脚本目录）
        import shutil
        shutil.copytree(sample_script_package.script_dir, script_dir)

        script_def = {
            "script_id": script_id,
            "name": "w1_hub_package_smoke",
            "exec_type": "script",
            "entrypoint": sample_script_package.entrypoint,
            "timeout_seconds": 120,
        }
        result = pre_judge_runner.execute_script(
            script_def, sample_script_package.stdin, timeout_seconds=120)
        assert result["status"] == "ok", f"模板包执行失败: {result.get('error')}"
        output = result["output"]
        # 消费了两个输入字段、共 32 个文件（样例索引全量）
        assert sorted(output["fields"].keys()) == ["hub_pkg", "hub_pkg_all"]
        assert output["fields"]["hub_pkg"]["file_count"] == 32
        # manifest 摘要真实提取（白名单 env 下文件/路径处理全部可用）
        assert output["manifest_summary"], "manifest 摘要不应为空"
        assert output["sample_rel_paths"], "样例 rel_path 不应为空"
        # 产物落 _run_dir/outputs/ 并注册 files
        summary_path = Path(result["files"]["manifest_summary.json"])
        assert summary_path.is_file()
        assert summary_path.parent.name == "outputs"

    def test_folder_scaffold_template_runs_via_service(
            self, db_session, tmp_path, isolated_upload_paths, monkeypatch):
        """from-template 脚手架生成的 folder 多文件工程（main.py 调 utils
        兄弟模块）经 runner 真实子进程在白名单 env 下执行成功——覆盖
        「现有模板脚本包跑通」的脚手架生成链路。
        ARCH-1-4 后缺省执行模式为 container：本用例断言 local 通道契约
        （宿主侧产物路径可见），显式声明 local（同上用例口径）"""
        monkeypatch.setenv("SCRIPT_EXEC_MODE", "local")
        service = PreJudgeScriptService(db_session)
        created = service.create_from_template("folder", f"w1_smoke_{uuid.uuid4().hex[:6]}")
        script_def = {
            "script_id": created["id"],
            "name": created["name"],
            "exec_type": "script",
            "entrypoint": created["entrypoint"],
            "timeout_seconds": 60,
        }
        stdin = {"task_id": "w1_scaffold_smoke", "data_id": "d1",
                 "fields": {}, "inputs": {}, "model_output": None,
                 "session": None, "params": {"upper": True}, "upstream": {},
                 "_run_dir": str(tmp_path / "scaffold_run_dir")}
        result = pre_judge_runner.execute_script(script_def, stdin, timeout_seconds=60)
        assert result["status"] == "ok", f"脚手架模板执行失败: {result.get('error')}"
        # 无 msg 输入文件 → 空文本（兄弟模块 utils 纯函数正常工作）
        assert result["output"]["text"] == ""
        assert result["output"]["chars"] == 0
        assert Path(result["files"]["result.json"]).is_file()
