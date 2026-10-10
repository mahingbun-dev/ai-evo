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
