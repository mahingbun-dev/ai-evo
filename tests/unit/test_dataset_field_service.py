"""
数据集字段元数据服务单元测试

覆盖 parse_fields_from_excel 的辅助列绑定：
- xxx_is_media（既有惯例）：OSS 附件标记，不落库
- xxx_hub_file（设计文档五/决策 #1 Excel 列配置方式）：字段值填 anc-hub file_id，
  字段类型标记为 hub_file，评测执行时自动下载落盘
"""

import pandas as pd
import pytest

from app.services.dataset_field_service import DatasetFieldService, HUB_FIELD_TYPE

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def write_excel(tmp_path, df, name="fields.xlsx"):
    path = tmp_path / name
    df.to_excel(path, index=False)
    return str(path)


class TestAutoBindHubFileFields:
    def test_binds_main_and_aux_column(self):
        svc = DatasetFieldService.__new__(DatasetFieldService)  # 纯函数，无需 db
        cols = ["cases_file", "cases_file_hub_file", "note"]
        assert svc._auto_bind_hub_file_fields(cols) == {"cases_file": "cases_file_hub_file"}

    def test_no_main_column_no_binding(self):
        """辅助列缺少同名主字段时不绑定（孤立列）"""
        svc = DatasetFieldService.__new__(DatasetFieldService)
        assert svc._auto_bind_hub_file_fields(["orphan_hub_file"]) == {}

    def test_no_aux_columns_empty(self):
        svc = DatasetFieldService.__new__(DatasetFieldService)
        assert svc._auto_bind_hub_file_fields(["a", "b"]) == {}


class TestParseFieldsFromExcelHubFileColumn:
    def _parse(self, tmp_path, df):
        return DatasetFieldService.__new__(DatasetFieldService).parse_fields_from_excel(
            write_excel(tmp_path, df))

    def test_hub_file_aux_column_marks_field_type(self, tmp_path):
        """xxx_hub_file 列填 是：主字段类型标记为 hub_file"""
        df = pd.DataFrame({
            "cases_file": ["fid-1", "fid-2"],
            "cases_file_hub_file": ["是", "是"],
            "user_input": ["输入1", "输入2"],
        })
        result = self._parse(tmp_path, df)
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["cases_file"]["type"] == HUB_FIELD_TYPE
        assert by_name["user_input"]["type"] == "string"

    def test_hub_file_flag_false_keeps_inferred_type(self, tmp_path):
        """xxx_hub_file 列填 否：主字段类型保持常规推断，不被标记"""
        df = pd.DataFrame({
            "cases_file": ["fid-1", "fid-2"],
            "cases_file_hub_file": ["否", "否"],
        })
        result = self._parse(tmp_path, df)
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["cases_file"]["type"] == "string"

    def test_hub_file_aux_column_not_saved_as_field(self, tmp_path):
        """_hub_file 辅助列仅作类型标记，不作为数据字段落库（与 _is_media 同惯例）"""
        df = pd.DataFrame({
            "cases_file": ["fid-1"],
            "cases_file_hub_file": ["是"],
        })
        result = self._parse(tmp_path, df)
        names = [f["name"] for f in result["fields"]]
        assert "cases_file_hub_file" not in names
        assert names == ["cases_file"]
        assert result["total_fields_count"] == 1

    def test_hub_file_field_forces_not_media(self, tmp_path):
        """hub_file 字段强制 is_media=False：避免字段值在评测预处理中被当作 OSS URL 置空"""
        df = pd.DataFrame({
            "cases_file": ["fid-1"],
            "cases_file_is_media": ["是"],
            "cases_file_hub_file": ["是"],
        })
        result = self._parse(tmp_path, df)
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["cases_file"]["type"] == HUB_FIELD_TYPE
        assert by_name["cases_file"]["is_media"] is False
        assert by_name["cases_file"]["is_media_field"] is None

    def test_is_media_channel_unaffected(self, tmp_path):
        """既有 is_media 辅助列行为不变（回归）"""
        df = pd.DataFrame({
            "user_input": ["x"],
            "user_input_is_media": ["是"],
        })
        result = self._parse(tmp_path, df)
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["user_input"]["type"] == "string"
        assert by_name["user_input"]["is_media"] is True
        assert by_name["user_input"]["is_media_field"] == "user_input_is_media"

    def test_numeric_file_id_column_marked_hub(self, tmp_path):
        """全数字 file_id 列：hub 标记覆盖 number 推断（file_id 允许纯数字形态）"""
        df = pd.DataFrame({
            "allure_file": ["123456", "789012"],
            "allure_file_hub_file": ["true", "true"],
        })
        result = self._parse(tmp_path, df)
        by_name = {f["name"]: f for f in result["fields"]}
        assert by_name["allure_file"]["type"] == HUB_FIELD_TYPE


class TestSaveAndGetFieldMetadataRoundtrip:
    def test_hub_file_type_roundtrip(self, tmp_path):
        """save/get 元数据往返：hub_file 类型可写入并读回（评测链路 get_hub_file_fields 的数据闸门）"""
        import sqlite3

        # 用临时 sqlite 建最小表，验证真实 SQLAlchemy 写入路径中的 field_type 值
        db_file = tmp_path / "meta.db"
        conn = sqlite3.connect(db_file)
        conn.commit()
        conn.close()

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models.dataset_models import Base

        engine = create_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(engine, tables=[
            Base.metadata.tables["dataset_field_metadata"]])
        Session = sessionmaker(bind=engine)
        db = Session()

        svc = DatasetFieldService(db)
        svc.save_field_metadata("ds-hub", [
            {"name": "cases_file", "label": "用例文件", "type": "hub_file",
             "is_media": False, "is_required": False, "position": 0},
            {"name": "user_input", "label": "用户输入", "type": "string",
             "is_media": False, "is_required": False, "position": 1},
        ])
        fields = svc.get_field_metadata("ds-hub")
        by_name = {f["name"]: f for f in fields}
        assert by_name["cases_file"]["type"] == "hub_file"
        assert by_name["user_input"]["type"] == "string"

        # 端到端闸门：get_hub_file_fields 按 field_type == "hub_file" 命中该字段
        from app.services.hub_file_service import get_hub_file_fields
        assert get_hub_file_fields(db, "ds-hub") == ["cases_file"]
        db.close()


class TestMergeFieldMetadata:
    def test_base_wins_and_existing_preserved(self):
        """base 覆盖同名字段，existing 中 base 未覆盖的字段按序保留"""
        from app.services.dataset_field_service import merge_field_metadata

        existing = [
            {"name": "user_input", "label": "旧标签", "type": "string", "position": 0},
            {"name": "session_file_id", "label": "执行包", "type": "hub_file", "position": 5},
        ]
        base = [
            {"name": "user_input", "label": "用户输入", "type": "string", "position": 0},
            {"name": "answer", "label": "参考答案", "type": "string", "position": 1},
        ]
        merged = merge_field_metadata(existing, base)
        assert [f["name"] for f in merged] == ["user_input", "answer", "session_file_id"]
        assert merged[0]["label"] == "用户输入"  # 同名以 base 为准
        assert merged[2]["type"] == "hub_file"

    def test_empty_existing_equals_base(self):
        """全新数据集（无已有元数据）：合并结果与 base 一致（等价全量保存）"""
        from app.services.dataset_field_service import merge_field_metadata

        base = [{"name": "user_input", "type": "string"}]
        assert merge_field_metadata([], base) == base
        assert merge_field_metadata(None, base) == base


class TestSaveFieldMetadataMerged:
    def test_excel_append_preserves_hub_channel_metadata(self, tmp_path):
        """回归：excel 追加（confirm）合并保存后，此前会话/hub 导入回填的
        session_file_id(hub_file) 等字段元数据不被全删重建清掉"""
        import sqlite3

        conn = sqlite3.connect(tmp_path / "meta.db")
        conn.commit()
        conn.close()

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models.dataset_models import Base

        engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
        Base.metadata.create_all(engine, tables=[
            Base.metadata.tables["dataset_field_metadata"]])
        Session = sessionmaker(bind=engine)
        db = Session()

        svc = DatasetFieldService(db)
        # 模拟此前会话/hub 导入写入的字段元数据
        svc.save_field_metadata("ds-merge", [
            {"name": "user_input", "label": "用户输入", "type": "string",
             "is_media": False, "is_required": False, "position": 0},
            {"name": "session_summary", "label": "会话摘要", "type": "string",
             "is_media": False, "is_required": False, "position": 1},
            {"name": "session_file_id", "label": "执行包", "type": "hub_file",
             "is_media": False, "is_required": False, "position": 2},
        ])

        # excel 追加：本次列只有 user_input/answer（confirm 端点合并保存路径）
        svc.save_field_metadata_merged("ds-merge", [
            {"name": "user_input", "label": "用户输入", "type": "string",
             "is_media": False, "is_required": False, "position": 0},
            {"name": "answer", "label": "参考答案", "type": "string",
             "is_media": False, "is_required": False, "position": 1},
        ])

        by_name = {f["name"]: f for f in svc.get_field_metadata("ds-merge")}
        assert set(by_name) == {"user_input", "answer", "session_summary", "session_file_id"}
        assert by_name["session_file_id"]["type"] == "hub_file"
        assert by_name["user_input"]["label"] == "用户输入"  # 同名以本次 excel 为准

        # 端到端闸门：hub 通道元数据存活 → get_hub_file_fields 仍命中
        from app.services.hub_file_service import get_hub_file_fields
        assert get_hub_file_fields(db, "ds-merge") == ["session_file_id"]
        db.close()


class TestInferFieldType:
    """按行内值推断字段类型（平台零硬编码清单的类型来源）"""

    def test_composite_values_are_json(self):
        from app.services.dataset_field_service import infer_field_type

        assert infer_field_type({"a": 1}) == "json"
        assert infer_field_type([1, 2]) == "json"
        assert infer_field_type({}) == "json"
        assert infer_field_type([]) == "json"

    def test_bool_checked_before_number(self):
        """bool 是 int 子类：顺序错了 True 会被推断成 number"""
        from app.services.dataset_field_service import infer_field_type

        assert infer_field_type(True) == "boolean"
        assert infer_field_type(False) == "boolean"
        assert infer_field_type(0) == "number"
        assert infer_field_type(1) == "number"
        assert infer_field_type(3.5) == "number"

    def test_text_and_unknown_fall_back_to_string(self):
        from app.services.dataset_field_service import infer_field_type

        assert infer_field_type("medium") == "string"
        assert infer_field_type("") == "string"
        # None 无法推断语义 → string（复合字段值为 None 时需产出方显式声明 type）
        assert infer_field_type(None) == "string"


class TestDeriveFieldsFromRow:
    """从数据行推导字段元数据：产出方声明优先、行内容兜底

    这是「平台不持有行 schema 清单」的核心能力——清洗脚本改了行结构，
    字段元数据自动跟随，无需平台侧同步任何常量。
    """

    ROW = {
        "schema_version": "hub_exec_cleaned_v2",
        "complexity": "medium",
        "thread_events": None,
        "clean_summary": {"case_total": 7},
        "field_descriptions": {
            "schema_version": "行内容 schema 版本",
            "complexity": "需求复杂度（simple/medium/complex）",
            "clean_summary": "清洗统计",
            # 嵌套键描述子字段含义，本身不是数据集字段
            "clean_summary.case_total": "用例总数",
            "accuracy_evaluation.items[].total": "细项校验点总数",
        },
    }

    def test_empty_or_invalid_row(self):
        from app.services.dataset_field_service import derive_fields_from_row

        assert derive_fields_from_row({}) == []
        assert derive_fields_from_row(None) == []
        assert derive_fields_from_row("not-a-dict") == []

    def test_labels_come_from_row_descriptions(self):
        """label 取行内自描述对照表；未登记的字段回落键名"""
        from app.services.dataset_field_service import derive_fields_from_row

        fields = derive_fields_from_row(dict(self.ROW))
        by_name = {f["name"]: f for f in fields}
        assert by_name["complexity"]["label"] == "需求复杂度（simple/medium/complex）"
        assert by_name["clean_summary"]["label"] == "清洗统计"
        # field_descriptions 自身未在对照表登记 → label 回落键名
        assert by_name["field_descriptions"]["label"] == "field_descriptions"

    def test_nested_description_keys_are_not_fields(self):
        """嵌套键（含 "." 与 "[]"）不是数据集字段，不得被登记"""
        from app.services.dataset_field_service import derive_fields_from_row

        names = {f["name"] for f in derive_fields_from_row(dict(self.ROW))}
        assert "clean_summary.case_total" not in names
        assert "accuracy_evaluation.items[].total" not in names

    def test_types_inferred_from_values(self):
        from app.services.dataset_field_service import derive_fields_from_row

        by_name = {f["name"]: f for f in derive_fields_from_row(dict(self.ROW))}
        assert by_name["schema_version"]["type"] == "string"
        assert by_name["complexity"]["type"] == "string"
        assert by_name["clean_summary"]["type"] == "json"
        # None 值无法推断语义 → string（要 json 必须由产出方声明）
        assert by_name["thread_events"]["type"] == "string"

    def test_is_required_always_false(self):
        """推导字段一律非必填：save_field_metadata 的 is_required 默认 True，
        不显式置 False 会让数据集被当成「必填字段缺失」拒绝后续导入"""
        from app.services.dataset_field_service import derive_fields_from_row

        fields = derive_fields_from_row(dict(self.ROW))
        assert fields and all(f["is_required"] is False for f in fields)
        assert all(f["is_media"] is False for f in fields)
        assert all(f["is_media_field"] is None for f in fields)

    def test_positions_follow_row_key_order(self):
        """position 按行内键顺序连续递增（= 前端列头顺序）"""
        from app.services.dataset_field_service import derive_fields_from_row

        fields = derive_fields_from_row(dict(self.ROW))
        assert [f["name"] for f in fields] == list(self.ROW.keys())
        assert [f["position"] for f in fields] == list(range(len(fields)))

    def test_start_position_offsets(self):
        """平台注入字段前置时，推导字段从 start_position 接续编号"""
        from app.services.dataset_field_service import derive_fields_from_row

        fields = derive_fields_from_row(dict(self.ROW), start_position=1)
        assert [f["position"] for f in fields] == list(range(1, len(fields) + 1))

    def test_exclude_skips_names(self):
        """exclude 用于跳过平台注入字段（避免与显式声明的 hub_file 元数据重复）"""
        from app.services.dataset_field_service import derive_fields_from_row

        row = dict(self.ROW)
        row["hub_file_id"] = "fid-1"
        fields = derive_fields_from_row(row, exclude=("hub_file_id",))
        assert "hub_file_id" not in {f["name"] for f in fields}

    def test_declared_wins_over_inference(self):
        """产出方声明优先：type/label 以声明为准，声明未覆盖的字段仍走推导"""
        from app.services.dataset_field_service import derive_fields_from_row

        declared = [
            {"name": "thread_events", "label": "会话事件时间线", "type": "json"},
            {"name": "complexity", "type": "string"},  # 无 label → 取对照表
        ]
        fields = derive_fields_from_row(dict(self.ROW), declared=declared)
        by_name = {f["name"]: f for f in fields}
        # 声明修正了 None 值的类型退化
        assert by_name["thread_events"]["type"] == "json"
        assert by_name["thread_events"]["label"] == "会话事件时间线"
        assert by_name["complexity"]["label"] == "需求复杂度（simple/medium/complex）"
        # 声明的字段排在最前（先声明后推导），且不重复
        assert [f["name"] for f in fields][:2] == ["thread_events", "complexity"]
        assert len(fields) == len(self.ROW)

    def test_declared_illegal_type_falls_back_to_inference(self):
        """声明了非法 type 时回落按值推断（不写坏字段元数据）"""
        from app.services.dataset_field_service import derive_fields_from_row

        declared = [{"name": "clean_summary", "type": "not-a-type"}]
        by_name = {f["name"]: f for f in derive_fields_from_row(dict(self.ROW),
                                                                declared=declared)}
        assert by_name["clean_summary"]["type"] == "json"

    def test_declared_hub_file_and_media_semantics_preserved(self):
        """无法从值推断的语义（hub_file 通道、媒体绑定）只能由产出方声明"""
        from app.services.dataset_field_service import derive_fields_from_row

        declared = [
            {"name": "hub_file_id", "type": HUB_FIELD_TYPE},
            {"name": "screenshot", "type": "string", "is_media": True,
             "is_media_field": "image"},
        ]
        row = dict(self.ROW)
        row["hub_file_id"] = "fid-1"
        by_name = {f["name"]: f for f in derive_fields_from_row(row, declared=declared)}
        assert by_name["hub_file_id"]["type"] == HUB_FIELD_TYPE
        assert by_name["screenshot"]["is_media"] is True
        assert by_name["screenshot"]["is_media_field"] == "image"

    def test_declared_invalid_items_dropped(self):
        """声明里的非法项（非 dict / 缺 name / 重名）丢弃，不影响其余字段"""
        from app.services.dataset_field_service import derive_fields_from_row

        declared = ["not-a-dict", {"label": "缺 name"}, None,
                    {"name": "complexity", "type": "string"},
                    {"name": "complexity", "type": "json"}]  # 重名 → 后者丢弃
        by_name = {f["name"]: f for f in derive_fields_from_row(dict(self.ROW),
                                                                declared=declared)}
        assert by_name["complexity"]["type"] == "string"

    def test_declared_only_row_missing_still_registered(self):
        """声明了但行内缺失的字段仍登记（本批数据缺该字段时列头不该消失）"""
        from app.services.dataset_field_service import derive_fields_from_row

        declared = [{"name": "accuracy_evaluation", "type": "json"}]
        names = {f["name"] for f in derive_fields_from_row(dict(self.ROW),
                                                           declared=declared)}
        assert "accuracy_evaluation" in names

    def test_malformed_row_descriptions_ignored(self):
        """行内对照表形态非法（非 dict / 值非字符串 / 空串）时忽略，label 回落键名"""
        from app.services.dataset_field_service import derive_fields_from_row

        for bad in ("不是字典", 42, None, [],
                    {"complexity": 123}, {"complexity": "   "}, {"complexity": None}):
            row = {"complexity": "medium", "field_descriptions": bad}
            by_name = {f["name"]: f for f in derive_fields_from_row(row)}
            assert by_name["complexity"]["label"] == "complexity", f"对照表 {bad!r} 应被忽略"


class TestReindexFieldPositions:
    """position 重排：合并已有字段后 position 会重复，重复值让 order_by(position) 排序不稳"""

    def test_reindex_sequential(self):
        from app.services.dataset_field_service import reindex_field_positions

        fields = [{"name": "a", "position": 5}, {"name": "b", "position": 5},
                  {"name": "c", "position": 0}]
        assert [f["position"] for f in reindex_field_positions(fields)] == [0, 1, 2]

    def test_reindex_in_place_and_returns_same_list(self):
        from app.services.dataset_field_service import reindex_field_positions

        fields = [{"name": "a", "position": 9}]
        result = reindex_field_positions(fields)
        assert result is fields
        assert fields[0]["position"] == 0

    def test_reindex_tolerates_empty_and_non_dict(self):
        from app.services.dataset_field_service import reindex_field_positions

        assert reindex_field_positions([]) == []
        assert reindex_field_positions(None) == []
        mixed = [{"name": "a", "position": 3}, "not-a-dict"]
        assert reindex_field_positions(mixed)[0]["position"] == 0
