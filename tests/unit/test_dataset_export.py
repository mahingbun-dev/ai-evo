"""
数据集导出单元测试

覆盖点：
- 导出列顺序（主字段后紧跟其 is_media 列）
- 与导入侧 parse_fields_from_excel 的 round-trip（字段结构不漂移）
- 媒体列取值与 OSS URL 原文输出
- V1 老数据集/空数据集的四列降级
- 行数上限
- 导出接口的 404 / 400 与文件名
"""

import io
import json
import re
import tempfile
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tests.unit.w9_direct_call import fake_request  # W9: 端点直调适配
from fastapi import HTTPException

from app.api.dataset_api import export_dataset_excel
from app.services.dataset_export_service import (
    DatasetExportService,
    DatasetExportTooLargeError,
    MAX_EXPORT_ROWS,
    SHEET_DATA,
    SHEET_FIELDS,
)
from app.services.dataset_field_service import DatasetFieldService

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

OSS_URL = (
    "https://oss.example.com/api-oss/public/oss/file/hashDownload/"
    "abc123def456.png"
)
OSS_URL_2 = (
    "https://oss.example.com/api-oss/public/oss/file/hashDownload/"
    "789xyz.png"
)


def make_field(name, label=None, is_media=False, is_media_field=None,
               is_required=False, position=0, field_type="string"):
    """构造 get_field_metadata() 的输出形态"""
    return {
        "name": name,
        "label": label or name,
        "type": field_type,
        "is_media": is_media,
        "is_media_field": is_media_field,
        "is_required": is_required,
        "position": position,
    }


def make_v2_item(data, item_id="data2_test", index=0):
    """构造 EvaluationDataV2 的最小替身"""
    return SimpleNamespace(id=item_id, index=index, data=data)


def make_v1_item(question, ref_answer, meta=None, item_id="data_test", index=0):
    """构造 EvaluationData 的最小替身"""
    return SimpleNamespace(
        id=item_id, index=index, question=question,
        ref_answer=ref_answer, meta_data=meta,
    )


def build_service(fields=None, v2_items=None, v1_items=None, db=None):
    """装配一个只桩掉数据库查询的 DatasetExportService

    field_service / data_service 仍为真实实现，因此 flatten_data_item_v2
    等逻辑会真实执行，只有落库查询被替换。
    """
    service = DatasetExportService(db if db is not None else MagicMock())
    fields = fields if fields is not None else []
    v2_items = v2_items if v2_items is not None else []
    v1_items = v1_items if v1_items is not None else []

    service.field_service.get_field_metadata = lambda dataset_id: list(fields)
    service.data_service.get_all_active_v2 = lambda dataset_id: list(v2_items)
    service.data_service.get_all_active_v1 = lambda dataset_id: list(v1_items)
    service.data_service.count_active_v2 = lambda dataset_id: len(v2_items)
    return service


def read_sheets(content: bytes):
    """读回导出的 Excel，返回 {sheet 名: DataFrame}"""
    return pd.read_excel(io.BytesIO(content), sheet_name=None)


def parse_export_with_importer(content: bytes):
    """用导入侧的真实解析器解析导出文件，模拟回灌

    parse_fields_from_excel 只依赖文件本身（不查库），因此传 db=None 即可
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        return DatasetFieldService(None).parse_fields_from_excel(tmp_path)
    finally:
        os.unlink(tmp_path)


# 一个典型的 V2 数据集：4 个主字段，其中两个带媒体绑定
TYPICAL_FIELDS = [
    make_field("user_input", "用户输入", is_media=True,
               is_media_field="user_input_is_media", is_required=True, position=0),
    make_field("answer", "参考答案", is_media=True,
               is_media_field="answer_is_media", is_required=True, position=2),
    make_field("scene", "场景", position=4),
    make_field("biz_line", "业务线", position=5),
]

TYPICAL_ROWS = [
    make_v2_item({
        "user_input": OSS_URL,
        "user_input_is_media": True,
        "answer": "这是参考答案",
        "answer_is_media": False,
        "scene": "售前",
        "biz_line": "物流",
    }, item_id="data2_a", index=0),
    make_v2_item({
        "user_input": "纯文本问题",
        "user_input_is_media": False,
        "answer": OSS_URL_2,
        "answer_is_media": True,
        "scene": "售后",
        "biz_line": "仓储",
    }, item_id="data2_b", index=1),
]


# ======================================================================
# 列结构
# ======================================================================

class TestColumnLayout:
    """导出列顺序与命名"""

    def test_is_media_column_follows_its_main_field(self):
        """每个主字段后紧跟其 is_media 列"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        sheets = read_sheets(service.export_to_excel("ds1"))

        assert list(sheets[SHEET_DATA].columns) == [
            "user_input", "user_input_is_media",
            "answer", "answer_is_media",
            "scene", "biz_line",
        ]

    def test_field_without_media_binding_emits_no_is_media_column(self):
        """元数据里既没绑定也不算媒体的字段，不额外补 is_media 列

        补了会让回灌时的字段结构与原数据集不一致（is_media_field 从 None
        变成 <name>_is_media），因此必须保持省略。
        """
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        sheets = read_sheets(service.export_to_excel("ds1"))
        columns = list(sheets[SHEET_DATA].columns)

        assert "scene_is_media" not in columns
        assert "biz_line_is_media" not in columns

    def test_no_id_or_index_column(self):
        """不输出 id / index，避免回灌时被当成新字段"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        sheets = read_sheets(service.export_to_excel("ds1"))
        columns = list(sheets[SHEET_DATA].columns)

        assert "id" not in columns
        assert "index" not in columns

    def test_is_media_true_without_binding_still_gets_column(self):
        """is_media 为真但未绑定字段名时，按约定补 <name>_is_media"""
        fields = [make_field("user_input", is_media=True, is_media_field=None)]
        rows = [make_v2_item({"user_input": OSS_URL, "user_input_is_media": True})]

        service = build_service(fields, rows)
        sheets = read_sheets(service.export_to_excel("ds1"))

        assert list(sheets[SHEET_DATA].columns) == [
            "user_input", "user_input_is_media",
        ]


# ======================================================================
# 取值格式
# ======================================================================

class TestCellValues:
    """单元格取值"""

    def test_is_media_values_are_chinese(self):
        """is_media 列输出 是 / 否"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert list(frame["user_input_is_media"]) == ["是", "否"]
        assert list(frame["answer_is_media"]) == ["否", "是"]

    @pytest.mark.parametrize("raw,expected", [
        (True, "是"), ("true", "是"), ("True", "是"), ("1", "是"),
        ("是", "是"), (1, "是"), (False, "否"), ("false", "否"),
        (None, "否"), ("0", "否"), ("", "否"),
    ])
    def test_is_media_value_normalization(self, raw, expected):
        """各种形态的 is_media 值都能归一到 是/否"""
        fields = [make_field("user_input", is_media=True,
                             is_media_field="user_input_is_media")]
        rows = [make_v2_item({"user_input": "x", "user_input_is_media": raw})]

        service = build_service(fields, rows)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]
        assert frame["user_input_is_media"].iloc[0] == expected

    def test_media_url_output_verbatim(self):
        """媒体字段原样输出 OSS URL，不改写、不转超链接"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert frame["user_input"].iloc[0] == OSS_URL
        assert frame["answer"].iloc[1] == OSS_URL_2

    def test_composite_values_are_stringified(self):
        """list/dict 等 openpyxl 无法直写的值降级为 JSON 文本"""
        fields = [make_field("user_input", label="问题", is_required=True)]
        rows = [make_v2_item({"user_input": "q", "tags": ["a", "b"]})]
        # tags 未在元数据中声明，会被忽略；换个已声明的字段验证序列化
        fields = [
            make_field("user_input", label="问题", is_required=True),
            make_field("tags", label="标签", position=1),
        ]

        service = build_service(fields, rows)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert frame["tags"].iloc[0] == json.dumps(["a", "b"], ensure_ascii=False)

    def test_undeclared_data_keys_are_ignored(self):
        """数据里有但元数据未声明的字段不导出，避免污染字段结构"""
        fields = [make_field("user_input", label="问题", is_required=True)]
        rows = [make_v2_item({"user_input": "q", "extra_col": "不该出现"})]

        service = build_service(fields, rows)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert list(frame.columns) == ["user_input"]


# ======================================================================
# round-trip：导出的文件必须能被导入侧原样解析回来
# ======================================================================

class TestRoundTrip:
    """导出 → 导入解析 的字段结构一致性"""

    def test_round_trip_preserves_field_structure(self):
        """回灌时字段名、顺序、媒体绑定均不变"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        content = service.export_to_excel("ds1")

        parsed = parse_export_with_importer(content)["fields"]

        assert [f["name"] for f in parsed] == [
            "user_input", "answer", "scene", "biz_line",
        ]
        assert [f["is_media"] for f in parsed] == [True, True, False, False]
        assert [f["is_media_field"] for f in parsed] == [
            "user_input_is_media", "answer_is_media", None, None,
        ]

    def test_round_trip_data_values_survive(self):
        """回灌时数据值（含 OSS URL）不变"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        content = service.export_to_excel("ds1")
        frame = read_sheets(content)[SHEET_DATA]

        assert frame["user_input"].iloc[0] == OSS_URL
        assert frame["scene"].iloc[1] == "售后"
        assert len(frame) == 2

    def test_round_trip_of_media_only_dataset(self):
        """全媒体字段的数据集同样保持结构"""
        fields = [
            make_field("user_input", is_media=True,
                       is_media_field="user_input_is_media", is_required=True),
        ]
        rows = [
            make_v2_item({"user_input": OSS_URL, "user_input_is_media": True}),
            make_v2_item({"user_input": OSS_URL_2, "user_input_is_media": True},
                         item_id="data2_c", index=1),
        ]

        service = build_service(fields, rows)
        parsed = parse_export_with_importer(service.export_to_excel("ds1"))["fields"]

        assert parsed[0]["is_media"] is True
        assert parsed[0]["is_media_field"] == "user_input_is_media"

    def test_round_trip_preserves_required_flag_by_presence(self):
        """必填字段在导出文件中必须有值，否则回灌会被判为无效行"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert frame["user_input"].notna().all()
        assert frame["answer"].notna().all()


class TestUndeclaredFieldLogging:
    """未声明字段的告警：不能把绑定的 is_media 列误报，也不能逐行刷屏

    线上实测：一个 226 行的数据集导出会打 226 条告警，其中 user_input_is_media /
    answer_is_media 是误报（它们确实被导出了，只是 declared 集合没纳入绑定列名）。
    """

    def test_bound_is_media_columns_not_reported(self, caplog):
        """已绑定的 is_media 列不应被当成未声明字段"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)

        with caplog.at_level("WARNING"):
            service.export_to_excel("ds1")

        assert "user_input_is_media" not in caplog.text
        assert "answer_is_media" not in caplog.text

    def test_internal_keys_reported_once_per_dataset(self, caplog):
        """内部记账键确实未声明，但只汇总一条，不逐行打"""
        fields = [make_field("user_input", label="问题", is_required=True)]
        rows = [
            make_v2_item({"user_input": f"q{i}", "files": [], "answer_files": []},
                         item_id=f"data2_{i}", index=i)
            for i in range(10)
        ]
        service = build_service(fields, rows)

        with caplog.at_level("WARNING"):
            service.export_to_excel("ds1")

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1, f"应只汇总一条告警，实际 {len(warnings)} 条"
        assert "files" in caplog.text

    def test_no_warning_when_all_fields_declared(self, caplog):
        """字段都在元数据里声明时不产生任何告警"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)

        with caplog.at_level("WARNING"):
            service.export_to_excel("ds1")

        assert [r for r in caplog.records if r.levelname == "WARNING"] == []


# ======================================================================
# 降级路径
# ======================================================================

class TestFallbacks:
    """V1 老数据集与空数据集"""

    def test_legacy_dataset_uses_template_columns(self):
        """V1 数据集降级为四列模板，与 /download-template 一致"""
        v1_items = [
            make_v1_item("问题一", "答案一", meta={"user_input_is_media": False}),
            make_v1_item("问题二", "答案二", meta={"answer_is_media": True},
                         item_id="data_2", index=1),
        ]
        service = build_service(fields=[], v2_items=[], v1_items=v1_items)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert list(frame.columns) == [
            "user_input", "user_input_is_media", "answer", "answer_is_media",
        ]
        assert list(frame["user_input"]) == ["问题一", "问题二"]
        assert list(frame["answer_is_media"]) == ["否", "是"]

    def test_legacy_dataset_round_trip(self):
        """V1 降级后的文件也能被导入侧解析成四字段结构"""
        v1_items = [make_v1_item("问题一", "答案一")]
        service = build_service(fields=[], v2_items=[], v1_items=v1_items)
        parsed = parse_export_with_importer(service.export_to_excel("ds1"))["fields"]

        assert [f["name"] for f in parsed] == ["user_input", "answer"]

    def test_empty_dataset_exports_header_only(self):
        """空数据集导出只有表头，可当模板用"""
        service = build_service(fields=[], v2_items=[], v1_items=[])
        sheets = read_sheets(service.export_to_excel("ds1"))

        assert len(sheets[SHEET_DATA]) == 0
        assert list(sheets[SHEET_DATA].columns) == [
            "user_input", "user_input_is_media", "answer", "answer_is_media",
        ]

    def test_dataset_with_metadata_but_no_rows(self):
        """有字段结构但没数据时，保持该数据集的字段结构"""
        service = build_service(TYPICAL_FIELDS, v2_items=[])
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert len(frame) == 0
        assert list(frame.columns) == [
            "user_input", "user_input_is_media",
            "answer", "answer_is_media",
            "scene", "biz_line",
        ]

    def test_v2_without_metadata_derives_columns_from_data(self):
        """字段元数据丢失的老 V2 数据集，从数据键推导列结构"""
        rows = [
            make_v2_item({
                "user_input": "q1", "user_input_is_media": False,
                "answer": "a1", "answer_is_media": False,
            }),
        ]
        service = build_service(fields=[], v2_items=rows)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert list(frame.columns) == [
            "user_input", "user_input_is_media", "answer", "answer_is_media",
        ]

    def test_derived_columns_exclude_internal_bookkeeping_keys(self):
        """推导列结构时排除附件清单等内部记账键

        导入确认流程会给每条数据写入 user_input_files / answer_files / files，
        它们不是业务字段，导出成可编辑列会污染回灌后的字段结构。
        """
        rows = [
            make_v2_item({
                "user_input": "q1", "user_input_is_media": False,
                "answer": "a1", "answer_is_media": False,
                "user_input_files": [], "answer_files": [],
                "files": [], "multi_question_fields": None,
                "multi_answer_fields": None,
                "biz_line": "物流",
            }),
        ]
        service = build_service(fields=[], v2_items=rows)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert list(frame.columns) == [
            "user_input", "user_input_is_media",
            "answer", "answer_is_media",
            "biz_line",
        ]

    def test_derived_round_trip_has_no_internal_fields(self):
        """排除内部键后，回灌解析出的字段结构不含内部字段"""
        rows = [
            make_v2_item({
                "user_input": "q1", "user_input_is_media": False,
                "answer": "a1", "answer_is_media": False,
                "answer_files": [{"file_name": "x.png"}],
                "files": [{"file_name": "x.png"}],
            }),
        ]
        service = build_service(fields=[], v2_items=rows)
        parsed = parse_export_with_importer(service.export_to_excel("ds1"))["fields"]

        assert [f["name"] for f in parsed] == ["user_input", "answer"]

    def test_derivation_falls_back_when_all_keys_internal(self):
        """数据里只有内部键时回落四列模板，避免产出零列 Excel"""
        rows = [make_v2_item({"files": [], "answer_files": []})]
        service = build_service(fields=[], v2_items=rows)
        frame = read_sheets(service.export_to_excel("ds1"))[SHEET_DATA]

        assert list(frame.columns) == [
            "user_input", "user_input_is_media", "answer", "answer_is_media",
        ]


# ======================================================================
# 行数上限
# ======================================================================

class TestRowLimit:
    """导出上限"""

    def test_exceeding_limit_raises(self):
        """超过上限抛 DatasetExportTooLargeError"""
        fields = [make_field("user_input", is_required=True)]
        rows = [
            make_v2_item({"user_input": f"q{i}"}, item_id=f"data2_{i}", index=i)
            for i in range(MAX_EXPORT_ROWS + 1)
        ]
        service = build_service(fields, rows)

        with pytest.raises(DatasetExportTooLargeError):
            service.export_to_excel("ds1")

    def test_count_exportable_rows_for_v2(self):
        """count_exportable_rows 反映 V2 数据量"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        assert service.count_exportable_rows("ds1") == 2

    def test_count_exportable_rows_for_legacy(self):
        """count_exportable_rows 在无 V2 数据时回落到 V1"""
        v1_items = [make_v1_item("q", "a")]
        service = build_service(fields=[], v2_items=[], v1_items=v1_items)
        assert service.count_exportable_rows("ds1") == 1

    def test_count_exportable_rows_for_empty_dataset_with_fields(self):
        """有字段结构但无数据 → 0"""
        service = build_service(TYPICAL_FIELDS, v2_items=[])
        assert service.count_exportable_rows("ds1") == 0


# ======================================================================
# 字段说明 sheet
# ======================================================================

class TestFieldDescriptionSheet:
    """第二个 sheet：字段说明"""

    def test_both_sheets_present(self):
        """导出文件同时包含数据 sheet 与字段说明 sheet"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        sheets = read_sheets(service.export_to_excel("ds1"))

        assert SHEET_DATA in sheets
        assert SHEET_FIELDS in sheets

    def test_description_lists_main_fields_with_chinese_labels(self):
        """字段说明列出主字段（不含 is_media 辅助列）及中文显示名"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        desc = read_sheets(service.export_to_excel("ds1"))[SHEET_FIELDS]

        assert list(desc["字段名"]) == [
            "user_input", "answer", "scene", "biz_line",
        ]
        assert list(desc["显示名"]) == [
            "用户输入", "参考答案", "场景", "业务线",
        ]
        assert "user_input_is_media" not in list(desc["字段名"])

    def test_description_marks_media_and_required(self):
        """字段说明标注是否媒体、是否必填"""
        service = build_service(TYPICAL_FIELDS, TYPICAL_ROWS)
        desc = read_sheets(service.export_to_excel("ds1"))[SHEET_FIELDS]

        assert list(desc["是否媒体"]) == ["是", "是", "否", "否"]
        assert list(desc["是否必填"]) == ["是", "是", "否", "否"]


# ======================================================================
# 导出接口
# ======================================================================

class FakeDataset:
    def __init__(self, name="测试数据集", team_code=None):
        self.id = "ds1"
        self.name = name
        self.team_code = team_code  # W9：归属校验读取（None 按 DEFAULT 口径）


def make_request(user=None):
    """构造一个最小的 Starlette Request（不启动 TestClient）

    项目里 auth_handler / rag_evaluator 会调用 nest_asyncio.apply()，
    全量跑测试时 TestClient 的 anyio portal 会被它破坏，因此这里直接
    以函数级方式调用端点，与 test_dataset_template.py 的既有风格一致。
    """
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    request = Request(scope)
    # W9：导出端点做对象级归属校验，state.user 必须存在；user=None 的历史
    # 用例与用户无关，缺省超管（等价无校验），「审计在无 user 时不炸」由
    # test_export_works_without_authenticated_user 显式构造无 user 的 scope 验证
    request.state.user = user if user is not None else fake_request().state.user
    return request


def export_endpoint(dataset_id="ds1", user=None, dataset="__default__"):
    """直接调用导出端点（W9：db 替身的归属查询链按需返回数据集对象/None）"""
    db = MagicMock()
    if dataset == "__default__":
        db.query.return_value.filter.return_value.first.return_value = FakeDataset()
    else:
        db.query.return_value.filter.return_value.first.return_value = dataset
    return export_dataset_excel(dataset_id, make_request(user), db)


class TestExportEndpoint:
    """GET /{dataset_id}/export/excel"""

    def test_404_when_dataset_missing(self, monkeypatch):
        with pytest.raises(HTTPException) as exc:
            export_endpoint("nope", dataset=None)
        assert exc.value.status_code == 404

    def test_400_when_over_row_limit(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetService.get_dataset",
            lambda self, dataset_id: FakeDataset(),
        )
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: MAX_EXPORT_ROWS + 1,
        )
        with pytest.raises(HTTPException) as exc:
            export_endpoint()

        assert exc.value.status_code == 400
        assert "上限" in exc.value.detail
        assert str(MAX_EXPORT_ROWS) in exc.value.detail

    def test_success_returns_xlsx_with_timestamped_filename(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 2,
        )
        response = export_endpoint(dataset=FakeDataset(name="回归数据集"))

        assert response.media_type == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        disposition = response.headers["content-disposition"]
        assert "%E5%9B%9E%E5%BD%92%E6%95%B0%E6%8D%AE%E9%9B%86" in disposition  # 回归数据集
        assert disposition.startswith("attachment;")
        assert ".xlsx" in disposition

    def test_filename_strips_illegal_chars(self, monkeypatch):
        """数据集名中的文件名非法字符被剔除"""
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 0,
        )
        response = export_endpoint(dataset=FakeDataset(name='a/b:c*d?e"f<g>h|i'))

        from urllib.parse import unquote
        disposition = unquote(response.headers["content-disposition"])
        # 取 RFC 5987 的 filename*=UTF-8''<name> 参数值，它才是真正的文件名
        match = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
        assert match, f"未找到 filename* 参数: {disposition}"
        filename = match.group(1)

        assert filename.startswith("abcdefghi_")
        assert filename.endswith(".xlsx")
        for ch in '/\\:*?"<>|':
            assert ch not in filename

    def test_export_failure_returns_500(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetService.get_dataset",
            lambda self, dataset_id: FakeDataset(),
        )
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 0,
        )

        def boom(self, dataset_id):
            raise RuntimeError("写文件失败")

        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.export_to_excel", boom
        )
        with pytest.raises(HTTPException) as exc:
            export_endpoint()
        assert exc.value.status_code == 500

    def test_too_large_error_from_service_returns_400(self, monkeypatch):
        """服务层直接抛超限异常时，接口同样返回 400 而非 500"""
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetService.get_dataset",
            lambda self, dataset_id: FakeDataset(),
        )
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 0,
        )

        def boom(self, dataset_id):
            raise DatasetExportTooLargeError("超过上限")

        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.export_to_excel", boom
        )
        with pytest.raises(HTTPException) as exc:
            export_endpoint()
        assert exc.value.status_code == 400

    def test_audit_logged_on_export(self, monkeypatch):
        """导出会写审计日志，action 为 EXPORT"""
        recorded = {}

        def fake_log_operation(user, action, resource_type, resource_id,
                               resource_name="", detail=None, ip_address=""):
            recorded.update({
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "resource_name": resource_name,
                "detail": detail,
                "ip_address": ip_address,
            })

        monkeypatch.setattr(
            "app.services.audit_service.log_operation", fake_log_operation
        )
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 7,
        )

        user = {"user_code": "U001", "user_name": "张三", "org_code": "ORG1"}
        response = export_endpoint(
            user=user,
            dataset=FakeDataset(name="审计数据集", team_code="ORG1"))

        assert response.media_type.endswith("spreadsheetml.sheet")
        assert recorded["action"] == "EXPORT"
        assert recorded["resource_type"] == "dataset"
        assert recorded["resource_id"] == "ds1"
        assert recorded["resource_name"] == "审计数据集"
        assert recorded["detail"]["row_count"] == 7
        assert recorded["ip_address"] == "127.0.0.1"

    def test_export_works_without_authenticated_user(self, monkeypatch):
        """未注入 request.state.user 时不应因审计而报错

        W9：归属校验读取 state.user——HTTP 侧由 AuthMiddleware 保证存在；
        本用例改为用 MagicMock request（getattr 恒有值、可跳过归属细节）
        专测审计分支对无 user 的容错。"""
        from unittest.mock import MagicMock as _M

        monkeypatch.setattr(
            "app.api.dataset_api.DatasetService.get_dataset",
            lambda self, dataset_id: FakeDataset(),
        )
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 0,
        )
        request = _M()
        request.state.user = None  # W9：无身份请求被归属校验拒绝（403，不得泄漏资源）
        with pytest.raises(HTTPException) as exc:
            export_dataset_excel("ds1", request, MagicMock())
        assert exc.value.status_code == 403

    def test_export_succeeds_when_audit_fails(self, monkeypatch):
        """审计写入失败不应影响导出本身"""
        def boom(*args, **kwargs):
            raise RuntimeError("审计库不可用")

        monkeypatch.setattr("app.services.audit_service.log_operation", boom)
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetService.get_dataset",
            lambda self, dataset_id: FakeDataset(),
        )
        monkeypatch.setattr(
            "app.api.dataset_api.DatasetExportService.count_exportable_rows",
            lambda self, dataset_id: 0,
        )
        response = export_endpoint()
        assert response.media_type.endswith("spreadsheetml.sheet")
