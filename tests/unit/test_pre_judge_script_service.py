"""评测前处理脚本服务单元测试

覆盖：内置脚本迁移（builtin→script 统一子进程执行、幂等、保留用户编辑、
builtin:<name> 历史引用）、CRUD（SQLite 内存库走真实 SQL：唯一约束、软删除、
停用复活、builtin 类型拒绝）、任务编排配置校验、
API 层（列表含内置 / 创建更新删除流 / 管理员简化校验 / debug 试跑落盘）。

说明：API 用例按仓库既有模式（参照 test_hub_import_api.py）直接调用端点
协程函数，不经过 TestClient —— 规避存量测试引入 nest_asyncio 全局补丁后
对 anyio portal 的跨文件污染。
"""
import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.models.task_models import EvaluationTask
from app.services import pre_judge_runner
from app.services import pre_judge_script_service as script_service
from app.services.pre_judge_script_service import (
    BUILTIN_SCRIPT_DEFS,
    PreJudgeScriptService,
    ensure_builtin_scripts_migrated,
    get_builtin_def,
)
from app.api.pre_judge_script_api import (
    PreJudgeScriptDebugPayload,
    PreJudgeScriptPayload,
    create_script,
    debug_script,
    delete_script,
    get_script,
    list_scripts,
    update_script,
)
from fastapi import HTTPException

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def isolated_upload_dirs(tmp_path, monkeypatch):
    """脚本存储与试跑产物指向临时目录（迁移会拷贝内置源码、debug 会落盘产物）"""
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", tmp_path / "pre_judge_scripts")
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")
    return tmp_path


@pytest.fixture
def service(db_session):
    return PreJudgeScriptService(db_session)


def _request(user=None) -> Request:
    """构造最小 Request，模拟鉴权中间件写入的 request.state.user（dict）。

    W1-4 起写操作校验 super_admin_flag == 1：缺省注入超管用户。
    """
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})
    if user is None:
        user = {"user_code": "u_admin", "super_admin_flag": 1}
    request.state.user = user
    return request


def _payload(name=None, **overrides):
    base = {"name": name or f"api_script_{uuid.uuid4().hex[:6]}", "exec_type": "script",
            "timeout_seconds": 60}
    base.update(overrides)
    return PreJudgeScriptPayload(**base)


@pytest.fixture
def sample_files(tmp_path):
    """debug 用最小样例：case_ok passed 一致、case_bad failed 不一致、case_missing 缺失
    → 一致率 round(1/3, 4) = 0.3333"""
    cases = [
        {"name": "case_ok", "expected": "ok", "priority": "P1"},
        {"name": "case_bad", "expected": "bad", "priority": "P1"},
        {"name": "case_missing", "expected": "m", "priority": "P1"},
    ]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    results_dir = tmp_path / "allure-results"
    results_dir.mkdir()
    (results_dir / "0-result.json").write_text(
        json.dumps({"name": "case_ok", "fullName": "case_ok", "status": "passed"}), encoding="utf-8")
    (results_dir / "1-result.json").write_text(
        json.dumps({"name": "case_bad", "fullName": "case_bad", "status": "failed",
                    "statusDetails": {"message": "assert failed"}}), encoding="utf-8")
    return {"cases_file": str(cases_file), "results_dir": str(results_dir)}


class TestBuiltinMigration:
    def test_migrated_records_and_files(self, db_session, tmp_path):
        """迁移后 4 个内置脚本为 script 记录（entrypoint=main.py）且源码文件就位"""
        migrated = ensure_builtin_scripts_migrated(db_session)
        assert set(migrated) == set(BUILTIN_SCRIPT_DEFS)
        for name, script_id in migrated.items():
            assert script_id == f"pjs_builtin_{name}"
            row = db_session.query(PreJudgeScript).filter_by(id=script_id).first()
            assert row is not None and row.exec_type == "script"
            assert row.entrypoint == "main.py" and row.is_active is True
            assert (tmp_path / "pre_judge_scripts" / script_id / "main.py").is_file()

    def test_migration_idempotent(self, db_session):
        first = ensure_builtin_scripts_migrated(db_session)
        second = ensure_builtin_scripts_migrated(db_session)
        assert first == second
        count = db_session.query(PreJudgeScript).count()
        assert count == len(BUILTIN_SCRIPT_DEFS)

    def test_migration_from_legacy_builtin_row(self, db_session):
        """历史 exec_type=builtin 落库形态：迁移改写为 script + main.py + 拷贝源码"""
        db_session.add(PreJudgeScript(id="pjs_builtin_xml_diff", name="xml_diff",
                                      exec_type="builtin", entrypoint="xml_diff", is_active=True))
        db_session.commit()
        migrated = ensure_builtin_scripts_migrated(db_session)
        assert migrated["xml_diff"] == "pjs_builtin_xml_diff"
        row = db_session.query(PreJudgeScript).filter_by(name="xml_diff").first()
        assert row.exec_type == "script" and row.entrypoint == "main.py"

    def test_deleted_builtin_not_recreated_on_restart(self, db_session):
        """表非空时缺失的内置脚本不补种（用户删除即真删，重启/部署不再复活）"""
        ensure_builtin_scripts_migrated(db_session)
        victim = db_session.query(PreJudgeScript).filter_by(name="log_grep").first()
        db_session.delete(victim)
        db_session.commit()

        migrated = ensure_builtin_scripts_migrated(db_session)
        assert "log_grep" not in migrated
        remaining = {r.name for r in db_session.query(PreJudgeScript).all()}
        assert "log_grep" not in remaining
        assert set(migrated) == set(BUILTIN_SCRIPT_DEFS) - {"log_grep"}

    def test_recreate_missing_explicit_keeps_manual_migration_semantics(self, db_session):
        """recreate_missing=True（手动一键迁移入口）：缺失内置脚本重建"""
        ensure_builtin_scripts_migrated(db_session)
        victim = db_session.query(PreJudgeScript).filter_by(name="log_grep").first()
        db_session.delete(victim)
        db_session.commit()

        migrated = ensure_builtin_scripts_migrated(db_session, recreate_missing=True)
        assert migrated["log_grep"] == "pjs_builtin_log_grep"
        row = db_session.query(PreJudgeScript).filter_by(name="log_grep").first()
        assert row is not None and row.exec_type == "script" and row.is_active is True

    def test_nonempty_table_migrates_legacy_but_skips_missing(self, db_session):
        """表非空组合场景：legacy builtin 行仍迁移，缺失的其它内置不重建"""
        db_session.add(PreJudgeScript(id="pjs_builtin_xml_diff", name="xml_diff",
                                      exec_type="builtin", entrypoint="xml_diff", is_active=True))
        db_session.commit()
        migrated = ensure_builtin_scripts_migrated(db_session)
        assert migrated["xml_diff"] == "pjs_builtin_xml_diff"
        row = db_session.query(PreJudgeScript).filter_by(name="xml_diff").first()
        assert row.exec_type == "script"
        # 其余内置脚本缺失（表非空）→ 不补种
        assert db_session.query(PreJudgeScript).count() == 1
        assert set(migrated) == {"xml_diff"}

    def test_migration_keeps_user_edits(self, db_session):
        """同名已存在（含用户编辑过的记录）迁移不覆盖元数据（仅补文件）"""
        db_session.add(PreJudgeScript(id="pjs_user_log_grep", name="log_grep",
                                      exec_type="script", entrypoint="main.py",
                                      description="用户改过的描述", timeout_seconds=77, is_active=True))
        db_session.commit()
        migrated = ensure_builtin_scripts_migrated(db_session)
        assert migrated["log_grep"] == "pjs_user_log_grep"
        row = db_session.query(PreJudgeScript).filter_by(name="log_grep").first()
        assert row.description == "用户改过的描述" and row.timeout_seconds == 77

    def test_migrated_script_executes_via_subprocess(self, db_session, tmp_path):
        """迁移后的内置脚本经统一子进程 wrapper 真实执行（缺输入 → 标准输出 fail）"""
        ensure_builtin_scripts_migrated(db_session)
        result = pre_judge_runner.execute_script(
            {"script_id": "pjs_builtin_allure_vs_cases", "name": "allure_vs_cases",
             "exec_type": "script", "entrypoint": "main.py", "timeout_seconds": 30},
            {"fields": {}, "params": {}, "upstream": {},
             "_run_dir": str(tmp_path / "run")},
        )
        assert set(result) >= {"status", "output", "files", "summary", "finish_reason", "error"}
        assert result["status"] == "fail"

    def test_get_builtin_def(self):
        definition = get_builtin_def("allure_vs_cases")
        assert definition["exec_type"] == "script"
        assert definition["entrypoint"] == "main.py"
        assert "cases_file" in definition["input_fields"]
        # 返回副本，修改不影响注册表
        definition["name"] = "hacked"
        assert BUILTIN_SCRIPT_DEFS["allure_vs_cases"]["name"] == "allure_vs_cases"


class TestCrud:
    def test_create_with_defaults(self, service):
        script = service.create_script({"name": "my_script", "exec_type": "script"})
        assert script["id"].startswith("pjs_")
        assert script["timeout_seconds"] == 120
        assert script["is_active"] is True

    def test_create_invalid_exec_type(self, service):
        with pytest.raises(ValueError, match="不支持的执行类型"):
            service.create_script({"name": "s1", "exec_type": "shell"})

    def test_create_builtin_exec_type_rejected(self, service):
        """builtin 进程内类型已废除：创建一律拒绝"""
        with pytest.raises(ValueError, match="已废除"):
            service.create_script({"name": "legacy", "exec_type": "builtin",
                                   "entrypoint": "app.scripts_builtin.allure_vs_cases.run"})

    def test_update_builtin_exec_type_rejected(self, service):
        script = service.create_script({"name": "upd_legacy", "exec_type": "script"})
        with pytest.raises(ValueError, match="已废除"):
            service.update_script(script["id"], {"exec_type": "builtin"})

    def test_create_empty_name(self, service):
        with pytest.raises(ValueError, match="名称不能为空"):
            service.create_script({"name": "  "})

    def test_create_duplicate_active_name(self, service):
        service.create_script({"name": "dup", "exec_type": "script"})
        with pytest.raises(ValueError, match="已存在"):
            service.create_script({"name": "dup", "exec_type": "script"})

    def test_create_reactivates_stopped_same_name(self, service):
        first = service.create_script({"name": "recycle", "exec_type": "script"})
        # 停用（非删除）：删除已是物理删除，同名复活仅针对停用记录
        service.update_script(first["id"], {"is_active": False})
        # 停用后默认列表不可见，get 可见且 is_active=False
        assert service.get_script(first["id"])["is_active"] is False
        assert all(i["name"] != "recycle" for i in service.list_scripts())

        second = service.create_script({"name": "recycle", "exec_type": "script",
                                        "description": "复活的记录"})
        assert second["id"] == first["id"]
        assert second["is_active"] is True
        assert second["description"] == "复活的记录"

    def test_update_script(self, service):
        script = service.create_script({"name": "updatable", "exec_type": "script"})
        updated = service.update_script(script["id"], {"timeout_seconds": 30, "description": "new"})
        assert updated["timeout_seconds"] == 30
        assert updated["description"] == "new"

    def test_update_name_conflict(self, service):
        service.create_script({"name": "name_a", "exec_type": "script"})
        other = service.create_script({"name": "name_b", "exec_type": "script"})
        with pytest.raises(ValueError, match="已存在"):
            service.update_script(other["id"], {"name": "name_a"})

    def test_update_nonexistent(self, service):
        with pytest.raises(ValueError, match="不存在"):
            service.update_script("pjs_missing", {"timeout_seconds": 30})

    def test_delete_is_physical(self, service, db_session, tmp_path):
        """删除为物理删除：DB 记录与落盘目录一并清除（引用校验见 TestDeleteReferenceGuard）"""
        script = service.create_script({"name": "harddel", "exec_type": "script"})
        script_dir = tmp_path / "pre_judge_scripts" / script["id"]
        script_dir.mkdir(parents=True)
        (script_dir / "main.py").write_text("print('x')", encoding="utf-8")

        assert service.delete_script(script["id"]) is True
        assert db_session.query(PreJudgeScript).filter(PreJudgeScript.id == script["id"]).first() is None
        assert not script_dir.exists()
        assert service.delete_script("pjs_none") is False

    def test_list_db_only_after_migration(self, service, db_session):
        """统一落库：列表仅 DB 记录；迁移后内置脚本以普通 script 记录出现（无 is_builtin 字段）"""
        # 空表迁移：4 个内置脚本落库为普通记录（启动语义），再混入自建脚本
        ensure_builtin_scripts_migrated(db_session)
        service.create_script({"name": "mine", "exec_type": "script"})
        names = [i["name"] for i in service.list_scripts()]
        assert "allure_vs_cases" in names and "mine" in names
        assert len(names) == len(BUILTIN_SCRIPT_DEFS) + 1
        builtin_item = next(i for i in service.list_scripts() if i["name"] == "allure_vs_cases")
        assert "is_builtin" not in builtin_item
        assert builtin_item["id"] == "pjs_builtin_allure_vs_cases"
        assert builtin_item["exec_type"] == "script"

    def test_get_builtin_by_prefixed_id(self, service, db_session):
        """builtin:<name> 历史引用兼容：优先按名称解析 DB 记录；未迁移回落种子元数据"""
        script = service.get_script("builtin:allure_vs_cases")
        assert script is not None and script["name"] == "allure_vs_cases"  # 种子元数据兜底（无 DB 记录）
        ensure_builtin_scripts_migrated(db_session)
        script = service.get_script("builtin:allure_vs_cases")
        assert script["id"] == "pjs_builtin_allure_vs_cases"


class TestDeleteReferenceGuard:
    """物理删除的引用校验：被未删除评测任务引用时拒绝，提示先解除关联"""

    @staticmethod
    def _make_task(session, task_id: str, name: str, script_id: str, is_active: bool = True):
        task = EvaluationTask(
            id=task_id, name=name, evaluation_type="judge", is_active=is_active,
            evaluation_targets=[{
                "source": "script_output",
                "source_config": {"script_id": script_id, "on_fail": "continue",
                                   "input_mapping": {}},
            }],
        )
        session.add(task)
        session.commit()
        return task

    def test_referenced_by_active_task_rejected(self, service, db_session):
        script = service.create_script({"name": "guarded", "exec_type": "script"})
        self._make_task(db_session, "task_1", "回归任务A", script["id"])
        with pytest.raises(ValueError, match="无法删除.*解除关联.*回归任务A"):
            service.delete_script(script["id"])
        # 拒绝后脚本与目录仍在
        assert service.get_script(script["id"]) is not None

    def test_referenced_by_builtin_prefix_also_rejected(self, service, db_session):
        """builtin:<name> 历史引用形态同样拦截（与任务校验脚本视图口径一致）"""
        script = service.create_script({"name": "legacy_ref", "exec_type": "script"})
        self._make_task(db_session, "task_2", "存量任务B", f"builtin:{script['name']}")
        with pytest.raises(ValueError, match="存量任务B"):
            service.delete_script(script["id"])

    def test_deleted_task_reference_not_blocking(self, service, db_session):
        """已删除任务（is_active=False）的引用不可见不可操作，不拦截删除"""
        script = service.create_script({"name": "dead_ref", "exec_type": "script"})
        self._make_task(db_session, "task_3", "已删任务C", script["id"], is_active=False)
        assert service.delete_script(script["id"]) is True

    def test_unbind_then_delete_ok(self, service, db_session):
        """解除关联（任务不再引用）后可物理删除"""
        script = service.create_script({"name": "unbound", "exec_type": "script"})
        task = self._make_task(db_session, "task_4", "待解除任务D", script["id"])
        with pytest.raises(ValueError, match="无法删除"):
            service.delete_script(script["id"])
        task.evaluation_targets = []
        db_session.commit()
        assert service.delete_script(script["id"]) is True
        assert db_session.query(PreJudgeScript).filter_by(id=script["id"]).first() is None

    def test_unrelated_task_passes(self, service, db_session):
        """其他任务的 script_output 行不影响本脚本删除"""
        script = service.create_script({"name": "free", "exec_type": "script"})
        other = service.create_script({"name": "other", "exec_type": "script"})
        self._make_task(db_session, "task_5", "无关任务E", other["id"])
        assert service.delete_script(script["id"]) is True

    def test_api_delete_referenced_maps_to_400(self, db_session):
        """API 层：被引用删除 → 400，detail 为中文提示"""
        service = PreJudgeScriptService(db_session)
        script = service.create_script({"name": "api_guard", "exec_type": "script"})
        self._make_task(db_session, "task_6", "接口校验任务F", script["id"])
        with pytest.raises(HTTPException) as exc:
            asyncio.run(delete_script(script["id"], request=_request(), db=db_session))
        assert exc.value.status_code == 400
        assert "解除关联" in str(exc.value.detail)


class TestValidatePreScriptsConfig:
    def test_none_passes(self):
        assert PreJudgeScriptService.validate_pre_scripts_config(None) == []

    def test_valid_config(self):
        config = [{
            "script_id": "builtin:allure_vs_cases",
            "input_mapping": {"cases_file": {"field": "cases_file", "value_type": "local"}},
            "params": {"fuzzy_threshold": 0.8},
            "output_var": "allure_compare",
            "on_fail": "continue",
        }]
        assert PreJudgeScriptService.validate_pre_scripts_config(config) == []

    def test_missing_script_id(self):
        errors = PreJudgeScriptService.validate_pre_scripts_config([{"input_mapping": {}}])
        assert any("script_id" in e for e in errors)

    def test_bad_on_fail(self):
        errors = PreJudgeScriptService.validate_pre_scripts_config([{"script_id": "x", "on_fail": "retry"}])
        assert any("on_fail" in e for e in errors)

    def test_unknown_script_with_lookup(self):
        lookup = {"builtin:allure_vs_cases": {"exec_type": "script"}}
        errors = PreJudgeScriptService.validate_pre_scripts_config(
            [{"script_id": "pjs_gone"}], script_lookup=lookup)
        assert any("不存在" in e for e in errors)

    def test_not_a_list(self):
        assert PreJudgeScriptService.validate_pre_scripts_config({"a": 1}) == ["pre_scripts 必须是数组"]


class TestApi:
    """端点协程直调（仓库模式，规避 nest_asyncio × TestClient 冲突）"""

    def test_list_contains_migrated_builtin(self, db_session):
        ensure_builtin_scripts_migrated(db_session)
        body = asyncio.run(list_scripts(include_inactive=False, db=db_session))
        names = [i["name"] for i in body["items"]]
        assert body["total"] >= 1 and "allure_vs_cases" in names
        assert all("is_builtin" not in i for i in body["items"])

    def test_create_update_delete_flow(self, db_session):
        created = asyncio.run(create_script(_payload(), request=_request(), db=db_session))
        assert created["id"].startswith("pjs_") and created["timeout_seconds"] == 60

        updated = asyncio.run(update_script(created["id"], _payload(name=created["name"], timeout_seconds=45),
                                            request=_request(), db=db_session))
        assert updated["timeout_seconds"] == 45

        deleted = asyncio.run(delete_script(created["id"], request=_request(), db=db_session))
        assert deleted == {"deleted": True, "script_id": created["id"]}
        with pytest.raises(HTTPException) as exc:
            asyncio.run(get_script(created["id"], db=db_session))
        assert exc.value.status_code == 404

    def test_create_duplicate_maps_to_400(self, db_session):
        payload = _payload()
        asyncio.run(create_script(payload, request=_request(), db=db_session))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(create_script(payload, request=_request(), db=db_session))
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("user", [
        {"user_code": "u_normal", "super_admin_flag": 0},
        {"user_code": "u_plain"},  # 无超管标志（deny by default）
        {"user_code": "u_legacy", "is_admin": False},  # 旧 is_admin 字段不再是放行依据
    ])
    def test_write_rejected_for_non_super_admin(self, db_session, user):
        """W1-4：非超管（super_admin_flag != 1）写操作一律 403"""
        with pytest.raises(HTTPException) as exc:
            asyncio.run(create_script(_payload(), request=_request(user=user), db=db_session))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            asyncio.run(delete_script("pjs_any", request=_request(user=user), db=db_session))

    def test_write_rejected_without_user(self, db_session):
        """W1-4：未注入 user（未登录语义）写操作 403（deny by default）"""
        request = Request({"type": "http", "method": "POST", "path": "/",
                           "headers": [], "query_string": b""})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(create_script(_payload(), request=request, db=db_session))
        assert exc.value.status_code == 403

    def test_write_allowed_when_super_admin_flag_1(self, db_session):
        """W1-4：super_admin_flag == 1 放行（与前端 Scripts.vue isSuperAdmin 同口径）"""
        created = asyncio.run(create_script(
            _payload(),
            request=_request(user={"user_code": "u_admin", "super_admin_flag": 1}),
            db=db_session))
        assert created["id"].startswith("pjs_")

    def test_builtin_script_immutable(self, db_session):
        """W1-3：内置脚本记录不可编辑/删除（service 层守卫）"""
        ensure_builtin_scripts_migrated(db_session)
        service = PreJudgeScriptService(db_session)
        builtin_id = "pjs_builtin_db_assert"
        with pytest.raises(ValueError, match="不可编辑"):
            service.update_script(builtin_id, {"timeout_seconds": 60})
        with pytest.raises(ValueError, match="不可删除"):
            service.delete_script(builtin_id)
        with pytest.raises(ValueError, match="不可上传覆盖"):
            service.save_script_file(builtin_id, b"print('hijack')\n")

    def test_builtin_script_files_immutable(self, db_session):
        """W1-3：内置脚本目录文件不可在线增删改（403）"""
        from app.services.pre_judge_script_service import PreJudgeFileError

        ensure_builtin_scripts_migrated(db_session)
        service = PreJudgeScriptService(db_session)
        builtin_id = "pjs_builtin_db_assert"
        with pytest.raises(PreJudgeFileError) as exc:
            service.update_script_file(builtin_id, "main.py", "print('hijack')\n", None)
        assert exc.value.status_code == 403
        with pytest.raises(PreJudgeFileError) as exc:
            service.create_script_file(builtin_id, "evil.py", "print('hi')\n")
        assert exc.value.status_code == 403
        with pytest.raises(PreJudgeFileError) as exc:
            service.delete_script_file(builtin_id, "main.py")
        assert exc.value.status_code == 403

    def test_entrypoint_validation_on_update(self, service):
        """W1-2：update_script 的 entrypoint 走包内相对路径校验"""
        script = service.create_script({"name": "entry_upd", "exec_type": "script"})
        script_dir = script_service.SCRIPT_UPLOAD_BASE / script["id"]
        script_dir.mkdir(parents=True, exist_ok=True)
        (script_dir / "main.py").write_text("print('x')\n", encoding="utf-8")

        ok = service.update_script(script["id"], {"entrypoint": "main.py"})
        assert ok["entrypoint"] == "main.py"
        for evil in ("../outside.py", "/etc/hosts.py", "..\\evil.py", "ghost.py"):
            with pytest.raises(ValueError, match="entrypoint"):
                service.update_script(script["id"], {"entrypoint": evil})

    def test_debug_builtin_script(self, db_session, sample_files):
        """debug 端点：给输入样例试跑一次（子进程执行），产物落盘返回路径"""
        ensure_builtin_scripts_migrated(db_session)
        body = asyncio.run(debug_script(
            "builtin:allure_vs_cases",
            PreJudgeScriptDebugPayload(fields={"cases_file": sample_files["cases_file"],
                                               "allure_results": sample_files["results_dir"]}),
            request=_request(), db=db_session))
        assert body["result"]["status"] == "ok"
        assert body["result"]["output"]["consistency_rate"] == 0.3333
        assert os.path.isfile(body["result"]["files"]["per_case_full.json"])
        assert "debug" in body["run_dir"]

    def test_debug_with_inline_content(self, db_session, sample_files):
        """字段值不是路径时按 inline 内容写临时文件再执行"""
        ensure_builtin_scripts_migrated(db_session)
        cases_text = open(sample_files["cases_file"], encoding="utf-8").read()
        body = asyncio.run(debug_script(
            "builtin:allure_vs_cases",
            PreJudgeScriptDebugPayload(fields={"cases_file": cases_text,
                                               "allure_results": sample_files["results_dir"]}),
            request=_request(), db=db_session))
        assert body["result"]["status"] == "ok"

    def test_debug_unknown_script_404(self, db_session):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(debug_script("pjs_none", PreJudgeScriptDebugPayload(),
                                     request=_request(), db=db_session))
        assert exc.value.status_code == 404
