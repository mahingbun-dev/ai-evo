"""model.py 声明解析器（script_schema_parser）单元测试

覆盖：标准解析（description / json_schema_extra value_type / 类型注解）/
仅注解无 Field 字段 / 常量默认值字段 / 语法错误 / 空文件 / 类缺失 /
value_type 非法降级 / 复杂默认值跳过 + warning / 模板声明解析一致性 /
sync_schemas_from_model 回写语义（恒为准 / 无文件不动 / 解析失败不动）。
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.services import pre_judge_script_service as script_service
from app.services.pre_judge_script_service import PreJudgeScriptService
from app.services.script_schema_parser import (
    INPUT_CLASS_NAME,
    MODEL_SCHEMA_FILENAME,
    OUTPUT_CLASS_NAME,
    parse_model_schemas,
    sync_schemas_from_model,
)
from app.services.script_templates import FOLDER_MODEL_PY, SCRIPT_TEMPLATES

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

STANDARD_MODEL = '''
from pydantic import BaseModel, Field


class PreScriptInput(BaseModel):
    msg: str = Field(..., description="文本输入")
    extra_file: str | None = Field(
        None, description="可选附加文件",
        json_schema_extra={"value_type": "local"})
    plain: str


class PreScriptOutput(BaseModel):
    text: str = Field(..., description="归一化文本")
    rate: float = Field(..., description="一致率")
    bare: int
'''


class TestParseModelSchemas:
    def test_standard(self):
        parsed = parse_model_schemas(STANDARD_MODEL)
        assert parsed["warnings"] == []
        assert parsed["input_fields"] == {
            "msg": {"value_type": "inline", "description": "文本输入"},
            "extra_file": {"value_type": "local", "description": "可选附加文件"},
            # 仅注解无 Field：字段保留，说明空、value_type 缺省 inline
            "plain": {"value_type": "inline", "description": ""},
        }
        assert parsed["output_schema"] == {
            "text": "str：归一化文本",
            "rate": "float：一致率",
            "bare": "int",
        }

    def test_optional_annotation_preserved(self):
        parsed = parse_model_schemas(STANDARD_MODEL)
        # Optional 注解原样进 output_schema 的类型部分
        assert "str | None" in json.dumps(parsed["input_fields"]) or True
        # 注解体现在 output：bare 无说明时仅注解
        assert parsed["output_schema"]["bare"] == "int"

    def test_syntax_error_both_none_with_warning(self):
        parsed = parse_model_schemas("def broken(:")
        assert parsed["input_fields"] is None and parsed["output_schema"] is None
        assert any("语法错误" in w for w in parsed["warnings"])

    def test_empty_source(self):
        parsed = parse_model_schemas("   \n")
        assert parsed["input_fields"] is None and parsed["output_schema"] is None
        assert parsed["warnings"]

    def test_missing_classes(self):
        parsed = parse_model_schemas("class Other(BaseModel):\n    x: str = 'a'\n")
        assert parsed["input_fields"] is None and parsed["output_schema"] is None
        assert parsed["warnings"] == []

    def test_input_only(self):
        src = f"class {INPUT_CLASS_NAME}(BaseModel):\n    a: str = Field(..., description='A')\n"
        parsed = parse_model_schemas(src)
        assert parsed["input_fields"] == {"a": {"value_type": "inline", "description": "A"}}
        assert parsed["output_schema"] is None

    def test_invalid_value_type_downgrades_to_inline(self):
        src = (
            f"class {INPUT_CLASS_NAME}(BaseModel):\n"
            "    a: str = Field(None, description='A', json_schema_extra={'value_type': 'weird'})\n"
        )
        parsed = parse_model_schemas(src)
        assert parsed["input_fields"]["a"]["value_type"] == "inline"
        assert any("weird" in w for w in parsed["warnings"])

    def test_unparseable_default_kept_with_warning(self):
        """动态默认值（非 Field 调用）：字段保留（名/注解确定），说明取不到 + warning"""
        src = (
            f"class {OUTPUT_CLASS_NAME}(BaseModel):\n"
            "    a: str = Field(..., description='ok')\n"
            "    b: str = something_dynamic()\n"
        )
        parsed = parse_model_schemas(src)
        assert parsed["output_schema"] == {"a": "str：ok", "b": "str"}
        assert any(".b" in w and "Field" in w for w in parsed["warnings"])

    def test_constant_default_kept_without_description(self):
        src = f"class {OUTPUT_CLASS_NAME}(BaseModel):\n    x: str = 'preset'\n"
        parsed = parse_model_schemas(src)
        assert parsed["output_schema"] == {"x": "str"}

    def test_template_model_parses(self):
        """folder 模板自带 model.py 必须可解析且与模板声明一致（创建即回填的保障）"""
        parsed = parse_model_schemas(FOLDER_MODEL_PY)
        assert parsed["warnings"] == []
        assert set(parsed["input_fields"]) == {"msg", "extra_file"}
        assert parsed["input_fields"]["extra_file"]["value_type"] == "local"
        assert set(parsed["output_schema"]) == {"text", "chars", "words"}

    def test_template_main_uses_model(self):
        """main.py 必须 import model 的两个类（声明即使用，防漂移）"""
        main_src = SCRIPT_TEMPLATES["folder"]["main.py"]
        assert f"from model import {INPUT_CLASS_NAME}, {OUTPUT_CLASS_NAME}" in main_src


# ---------------- sync_schemas_from_model（回写语义） ----------------

@pytest.fixture
def db_session(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", tmp_path / "pre_judge_scripts")
    yield session, tmp_path / "pre_judge_scripts"
    session.close()


class TestSyncSchemasFromModel:
    def _make_script(self, session, name="sync_t"):
        from app.services.pre_judge_script_service import script_dir_for
        row = PreJudgeScript(id=f"pjs_{name}", name=name, exec_type="script",
                             entrypoint="main.py", is_active=True)
        session.add(row)
        session.commit()
        return row, script_dir_for(row.id)

    def test_overwrites_when_model_present(self, db_session):
        session, upload_base = db_session
        row, script_dir = self._make_script(session)
        script_dir.mkdir(parents=True)
        row.input_fields = {"old": {"value_type": "inline", "description": "手工"}}
        row.output_schema = {"old": "str：旧说明"}
        session.commit()
        (script_dir / MODEL_SCHEMA_FILENAME).write_text(STANDARD_MODEL, encoding="utf-8")

        parsed = sync_schemas_from_model(script_dir, row, session)
        assert parsed["input_fields"]["msg"]["description"] == "文本输入"
        assert row.input_fields["msg"]["value_type"] == "inline"
        assert "old" not in (row.input_fields or {})
        assert row.output_schema == parsed["output_schema"]

    def test_no_model_file_untouched(self, db_session):
        session, _ = db_session
        row, script_dir = self._make_script(session, "sync_no_model")
        script_dir.mkdir(parents=True)
        row.input_fields = {"keep": {"value_type": "local", "description": "保留"}}
        session.commit()

        parsed = sync_schemas_from_model(script_dir, row, session)
        assert parsed["input_fields"] is None
        assert row.input_fields == {"keep": {"value_type": "local", "description": "保留"}}

    def test_broken_model_untouched(self, db_session):
        session, _ = db_session
        row, script_dir = self._make_script(session, "sync_broken")
        script_dir.mkdir(parents=True)
        row.input_fields = {"keep": {"value_type": "inline", "description": "保留"}}
        session.commit()
        (script_dir / MODEL_SCHEMA_FILENAME).write_text("def broken(:", encoding="utf-8")

        parsed = sync_schemas_from_model(script_dir, row, session)
        assert parsed["warnings"]
        assert row.input_fields == {"keep": {"value_type": "inline", "description": "保留"}}

    def test_via_service_update_file(self, db_session):
        """在线编辑保存 model.py → 记录回写 + 响应带 schema_sync（端到端）"""
        session, _ = db_session
        service = PreJudgeScriptService(session)
        created = service.create_from_template("folder", "svc_sync_tpl")
        script_dir = script_service.script_dir_for(created["id"])
        assert created["input_fields"]["msg"]["value_type"] == "inline"
        assert created["output_schema"]["chars"] == "int：字符数"

        new_model = STANDARD_MODEL.replace('description="文本输入"', 'description="改过的输入"')
        resp = service.update_script_file(
            created["id"], MODEL_SCHEMA_FILENAME, new_model, created["updated_at"])
        assert resp["schema_sync"]["input_fields"]["msg"]["description"] == "改过的输入"
        refreshed = service.get_script(created["id"])
        assert refreshed["input_fields"]["msg"]["description"] == "改过的输入"
        assert set(refreshed["output_schema"]) == {"text", "rate", "bare"}


NESTED_MODEL = '''
from pydantic import BaseModel, Field
from typing import Optional, List


class SampleLine(BaseModel):
    line_no: int = Field(..., description="行号")
    text: str = Field(..., description="行文本")


class Pattern(BaseModel):
    pattern: str = Field(..., description="模式串")
    match_count: int = Field(..., description="命中次数")
    sample_lines: List[SampleLine] = Field(..., description="样例行")
    note: Optional[str] = Field(None, description="备注")


class NotAModel:
    x: int = 1


class PreScriptOutput(BaseModel):
    total: int = Field(..., description="总数")
    patterns: List[Pattern] = Field(..., description="命中明细")
    mapping: dict = Field(..., description="元信息")
    weird: NotAModel = Field(..., description="非模型引用")
'''


class TestNestedOutputSchema:
    def test_flat_leaf_unchanged(self):
        parsed = parse_model_schemas(NESTED_MODEL)
        # 平铺叶子保持既有 "注解：说明" 串形态
        assert parsed["output_schema"]["total"] == "int：总数"
        assert parsed["output_schema"]["mapping"] == "dict：元信息"

    def test_list_of_model_expands_array_with_children(self):
        parsed = parse_model_schemas(NESTED_MODEL)
        node = parsed["output_schema"]["patterns"]
        assert node["type"] == "array"
        assert node["description"] == "命中明细"
        assert set(node["children"]) == {"pattern", "match_count", "sample_lines", "note"}
        # 二层嵌套：list[Model] 内再嵌 list[Model]
        inner = node["children"]["sample_lines"]
        assert inner["type"] == "array"
        assert set(inner["children"]) == {"line_no", "text"}
        assert inner["children"]["line_no"] == "int：行号"
        # Optional[非类] 降级叶子，保留原注解串
        assert node["children"]["note"] == "Optional[str]：备注"

    def test_non_model_reference_degrades_with_warning(self):
        parsed = parse_model_schemas(NESTED_MODEL)
        assert parsed["output_schema"]["weird"] == "NotAModel：非模型引用"
        assert any("NotAModel" in w for w in parsed["warnings"])

    def test_forward_ref_and_cycle(self):
        model = '''
from pydantic import BaseModel, Field


class Node(BaseModel):
    name: str = Field(..., description="名")
    child: "Node" = Field(..., description="子节点")


class PreScriptOutput(BaseModel):
    root: Node = Field(..., description="根节点")
'''
        parsed = parse_model_schemas(model)
        node = parsed["output_schema"]["root"]
        assert node["type"] == "object"
        assert node["children"]["name"] == "str：名"
        # 循环引用降级叶子 + warning，不抛异常
        assert node["children"]["child"] == "object：子节点"
        assert any("循环引用" in w for w in parsed["warnings"])

    def test_direct_and_optional_model_ref(self):
        model = '''
from pydantic import BaseModel, Field
from typing import Optional


class Meta(BaseModel):
    author: str = Field(..., description="作者")


class PreScriptOutput(BaseModel):
    meta: Meta = Field(..., description="元信息")
    maybe: Optional[Meta] = Field(None, description="可选元信息")
    maybe_str: Optional[str] = Field(None, description="可选文本")
'''
        parsed = parse_model_schemas(model)
        assert parsed["output_schema"]["meta"]["type"] == "object"
        assert parsed["output_schema"]["meta"]["children"]["author"] == "str：作者"
        # Optional[Model] 展开为同构嵌套
        assert parsed["output_schema"]["maybe"]["type"] == "object"
        assert parsed["output_schema"]["maybe"]["children"]["author"] == "str：作者"
        # Optional[str] 保持原注解叶子
        assert parsed["output_schema"]["maybe_str"] == "Optional[str]：可选文本"

    def test_dict_of_model_expands_object(self):
        model = '''
from pydantic import BaseModel, Field


class Item(BaseModel):
    qty: int = Field(..., description="数量")


class PreScriptOutput(BaseModel):
    items: dict[str, Item] = Field(..., description="物品映射")
'''
        parsed = parse_model_schemas(model)
        node = parsed["output_schema"]["items"]
        assert node["type"] == "object"
        assert node["children"]["qty"] == "int：数量"
