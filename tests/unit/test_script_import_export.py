"""脚本导入导出单元测试（设计文档 §十二 二期：契约 JSON 打包/再导入）

覆盖：
1. 服务层 export_contract：DB 记录契约字段（环境字段不导出）、builtin 元数据导出
   （携带 is_builtin）、脚本不存在返回 None
2. 服务层 import_script：新建、同名活跃冲突（overwrite 开关）、同名停用复活、
   缺名称报错、builtin entrypoint 可解析校验、契约无关字段（is_builtin）不落库
   （import_script 为服务层内部方法：契约 JSON 的 HTTP /import 端点已下线，
   import_package 建脚本记录复用本方法）
3. API 层：GET /{id}/export（404 / Content-Disposition 附件头）
4. 导出→导入回环：导出契约在干净库导入得到同契约脚本，可再导出且契约一致
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.pre_judge_script_api import export_script
from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.services.pre_judge_script_service import (
    EXTENSION_POINT_PRE_JUDGE,
    PreJudgeScriptService,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

CONTRACT_FIELDS = (
    "name", "description", "exec_type", "entrypoint", "allowed_tools",
    "harness_extra", "input_fields", "output_schema", "timeout_seconds", "is_active",
    # 阶段E：依赖声明与风险声明随契约导出（package_dir 为环境相关路径不导出）
    "declared_requirements", "risk_acknowledged",
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def service(db_session):
    return PreJudgeScriptService(db_session)


def _make_script(service, name=None, **overrides) -> dict:
    payload = {
        "name": name or f"exp_script_{uuid.uuid4().hex[:6]}",
        "description": "导出测试脚本",
        "exec_type": "script",
        "entrypoint": "main.py",
        "input_fields": {"cases_file": {"value_type": "local"}},
        "output_schema": {"consistency_rate": "number"},
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return service.create_script(payload, created_by="tester")


# ==================== 1. 服务层导出 ====================

class TestExportContract:
    def test_export_db_script_contract(self, service):
        created = _make_script(service)
        contract = service.export_contract(created["id"])
        assert contract is not None
        assert contract["export_version"] == "1.0"
        assert contract["extension_point"] == EXTENSION_POINT_PRE_JUDGE
        assert contract["exported_at"]
        script = contract["script"]
        assert script["name"] == created["name"]
        assert script["entrypoint"] == "main.py"
        assert script["timeout_seconds"] == 60
        # 环境相关字段不导出
        for env_field in ("id", "created_by", "created_at", "updated_at", "is_builtin"):
            assert env_field not in script
        assert set(script) == set(CONTRACT_FIELDS)

    def test_export_builtin_metadata(self, service):
        """builtin: 前缀历史引用导出：统一契约（种子元数据兜底，迁移后 script/main.py 形态）"""
        contract = service.export_contract("builtin:allure_vs_cases")
        assert contract is not None
        script = contract["script"]
        assert script["name"] == "allure_vs_cases"
        assert "is_builtin" not in script
        assert script["exec_type"] == "script"
        assert script["entrypoint"] == "main.py"

    def test_export_missing_script_returns_none(self, service):
        assert service.export_contract("pjs_ghost") is None
        assert service.export_contract("builtin:no_such_builtin") is None


# ==================== 2. 服务层导入 ====================

class TestImportScript:
    def test_import_new_script(self, service):
        contract = {
            "name": "imported_xml_diff",
            "description": "导入的比对脚本",
            "exec_type": "script",
            "entrypoint": "main.py",
            "input_fields": {"xml_a": {"value_type": "local"}},
            "timeout_seconds": 90,
        }
        imported = service.import_script(contract, created_by="importer")
        assert imported["id"].startswith("pjs_")
        assert imported["name"] == "imported_xml_diff"
        assert imported["entrypoint"] == "main.py"
        assert imported["timeout_seconds"] == 90
        assert imported["is_active"] is True
        assert imported["created_by"] == "importer"

    def test_import_conflict_without_overwrite(self, service):
        _make_script(service, name="dup_script")
        contract = {"name": "dup_script", "exec_type": "script",
                    "entrypoint": "main.py"}
        with pytest.raises(ValueError, match="overwrite"):
            service.import_script(contract)

    def test_import_conflict_overwrite_updates(self, service):
        created = _make_script(service, name="ow_script", timeout_seconds=60)
        contract = {"name": "ow_script", "description": "覆盖后描述",
                    "exec_type": "script",
                    "entrypoint": "main.py",
                    "timeout_seconds": 45}
        imported = service.import_script(contract, overwrite=True)
        assert imported["id"] == created["id"]
        assert imported["timeout_seconds"] == 45
        assert imported["description"] == "覆盖后描述"
        # 库中只有一条同名记录
        rows = service.db.query(PreJudgeScript).filter(PreJudgeScript.name == "ow_script").all()
        assert len(rows) == 1

    def test_import_revives_inactive_same_name(self, service):
        """停用（is_active=False）同名记录：导入复活（id 不变、重新激活）

        停用与删除语义分离后，复活对象是「停用」记录；删除为物理删除不再
        制造停用态（物理删除后同名导入见 test_import_after_physical_delete）。
        """
        created = _make_script(service, name="revive_script")
        service.update_script(created["id"], {"is_active": False})
        contract = {"name": "revive_script", "exec_type": "script",
                    "entrypoint": "main.py"}
        imported = service.import_script(contract)
        assert imported["id"] == created["id"]
        assert imported["is_active"] is True
        assert imported["entrypoint"] == "main.py"
        assert imported["imported_as"] == "revived"

    def test_import_after_physical_delete_creates_new(self, service):
        """物理删除后同名导入：旧记录已删无复活对象，创建新记录（新 id）"""
        created = _make_script(service, name="reborn_script")
        service.delete_script(created["id"])
        contract = {"name": "reborn_script", "exec_type": "script",
                    "entrypoint": "main.py"}
        imported = service.import_script(contract)
        assert imported["imported_as"] == "created"
        assert imported["id"] != created["id"]
        # 库中只有新记录一条
        rows = service.db.query(PreJudgeScript).filter(PreJudgeScript.name == "reborn_script").all()
        assert len(rows) == 1

    def test_import_missing_name_rejected(self, service):
        with pytest.raises(ValueError, match="缺少脚本名称"):
            service.import_script({"exec_type": "script"})

    def test_import_builtin_exec_type_rejected(self, service):
        """历史契约声明 exec_type=builtin：导入一律拒绝（进程内类型已废除）"""
        with pytest.raises(ValueError, match="已废除"):
            service.import_script({"name": "legacy_builtin", "exec_type": "builtin",
                                   "entrypoint": "app.scripts_builtin.xml_diff.run"})

    def test_import_builtin_marker_not_persisted(self, service):
        """契约不再携带 is_builtin；历史契约中的该字段导入时被忽略不落库"""
        contract = service.export_contract("builtin:allure_vs_cases")["script"]
        imported = service.import_script({**contract, "name": "builtin_copy", "is_builtin": True})
        assert "is_builtin" not in imported
        assert "is_builtin" not in contract


# ==================== 3. API 层 ====================

class TestExportApi:
    def test_export_api_contract_with_disposition(self, db_session, service):
        created = _make_script(service)
        response = asyncio.run(export_script(created["id"], db=db_session))
        assert response.status_code == 200
        body = json_loads(response.body)
        assert body["script"]["name"] == created["name"]
        assert response.headers["content-disposition"].startswith("attachment")
        assert f"{created['name']}.json" in response.headers["content-disposition"]

    def test_export_api_404(self, db_session):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(export_script("pjs_ghost", db=db_session))
        assert exc.value.status_code == 404


# ==================== 4. 导出→导入回环 ====================

class TestExportImportRoundTrip:
    def test_round_trip_preserves_contract(self, tmp_path):
        """干净库回环：A 库导出 → B 库导入 → B 库导出，契约 script 节一致"""
        engine_a = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine_a)
        session_a = sessionmaker(bind=engine_a)()
        service_a = PreJudgeScriptService(session_a)
        try:
            created = _make_script(service_a)
            exported = service_a.export_contract(created["id"])["script"]
        finally:
            session_a.close()

        engine_b = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine_b)
        session_b = sessionmaker(bind=engine_b)()
        service_b = PreJudgeScriptService(session_b)
        try:
            imported = service_b.import_script(exported)
            reexported = service_b.export_contract(imported["id"])["script"]
            assert reexported == exported
        finally:
            session_b.close()


def json_loads(raw: bytes) -> dict:
    import json

    return json.loads(bytes(raw).decode("utf-8"))
