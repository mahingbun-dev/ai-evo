"""会话数据集导入的字段契约测试（session_dataset_service）

覆盖：
- build_session_data 随行内容产出自描述对照表 field_descriptions
- 守卫：行内容的每个顶层键都必须在 SESSION_FIELD_DESCRIPTIONS 登记——
  防止重演「平台侧字段清单停在旧 schema，新字段在数据集里看不见」
- import_sessions 按「平台注入字段前置 + 行内容推导」注册字段元数据：
  label 取行内对照表、position 连续唯一、推导字段一律非必填
- hub 通道字段（session_file_id，type=hub_file）由平台声明，不靠推导

mock 手法与 test_hub_import_api.py 同构：patch 打在源模块，db 用 MagicMock，
会话文件用 tmp_path 真实落盘（走真实 detect_and_parse 解析链路）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.session_dataset_service import (
    SESSION_FIELD_DESCRIPTIONS,
    SESSION_HUB_FILE_METADATA,
    SESSION_HUB_REF_FIELDS,
    build_session_data,
    import_sessions,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

SESSION_JSONL = (
    '{"type":"user","sessionId":"s1","timestamp":"2026-01-01T00:00:00.000Z",'
    '"message":{"role":"user","content":"帮我跑测试"}}\n'
    '{"type":"assistant","sessionId":"s1","timestamp":"2026-01-01T00:00:10.000Z",'
    '"message":{"role":"assistant","content":"好的，开始执行"}}\n'
)

# 平台注入字段（hub 通道）由导入链路写入，不由 build_session_data 产出
_PLATFORM_INJECTED = {m["name"] for m in SESSION_HUB_FILE_METADATA}


def make_record(tmp_path, name="s1.jsonl"):
    """落一个真实会话 jsonl，返回解析后的 SessionRecord"""
    from app.services.session_file_parser import detect_and_parse

    src = tmp_path / name
    src.write_text(SESSION_JSONL, encoding="utf-8")
    records = detect_and_parse(str(src))
    assert records, "样本会话应能解析出记录"
    return records[0], src


class TestBuildSessionData:
    def test_row_carries_field_descriptions(self, tmp_path):
        """行内自带字段对照表：平台据此取 label，无需持有任何行 schema 清单"""
        record, _ = make_record(tmp_path)
        data = build_session_data(record)
        assert isinstance(data.get("field_descriptions"), dict)
        assert data["field_descriptions"] == SESSION_FIELD_DESCRIPTIONS
        # 是副本：改行内对照表不得污染模块常量
        data["field_descriptions"]["user_input"] = "被改了"
        assert SESSION_FIELD_DESCRIPTIONS["user_input"] != "被改了"

    def test_every_row_key_is_described(self, tmp_path):
        """守卫：行内容新增字段必须同步登记对照表

        漏登记的字段仍会被平台推导登记（label 回落英文键名），但前端列头
        就没有中文说明——本用例把这类漂移挡在提交前。
        """
        record, _ = make_record(tmp_path)
        row_keys = set(build_session_data(record).keys())
        assert row_keys <= set(SESSION_FIELD_DESCRIPTIONS), (
            f"行内容字段未在 SESSION_FIELD_DESCRIPTIONS 登记: "
            f"{sorted(row_keys - set(SESSION_FIELD_DESCRIPTIONS))}"
        )

    def test_descriptions_have_no_stale_keys(self, tmp_path):
        """反向守卫：对照表里不应留下行内容已不存在的字段（说明已删字段未清理）"""
        record, _ = make_record(tmp_path)
        row_keys = set(build_session_data(record).keys())
        assert set(SESSION_FIELD_DESCRIPTIONS) <= row_keys, (
            f"SESSION_FIELD_DESCRIPTIONS 存在行内容已无的字段: "
            f"{sorted(set(SESSION_FIELD_DESCRIPTIONS) - row_keys)}"
        )

    def test_descriptions_are_non_empty_text(self):
        """对照表值必须是非空中文说明（空值会被平台忽略、label 回落键名）"""
        for key, label in SESSION_FIELD_DESCRIPTIONS.items():
            assert isinstance(label, str) and label.strip(), f"{key} 的说明为空"
            # 嵌套键（"a.b" / "a[]"）不是数据集字段，会话行对照表不应出现
            assert "." not in key and "[]" not in key, f"{key} 不是顶层字段"

    def test_user_input_is_first_row_key(self, tmp_path):
        """user_input 排在行首：推导按行内键序定 position，它决定前端首列"""
        record, _ = make_record(tmp_path)
        assert next(iter(build_session_data(record))) == "user_input"


class TestImportSessionsFieldMetadata:
    def _import(self, tmp_path, src, **kwargs):
        db = MagicMock()
        db.query.return_value.filter.return_value.scalar.return_value = 1
        saved = []
        with patch("app.services.dataset_service.DatasetService") as mock_ds, \
                patch("app.services.dataset_service.DatasetDataService"), \
                patch("app.services.dataset_field_service.DatasetFieldService") as mock_fs:
            mock_ds.return_value.create_dataset.return_value = SimpleNamespace(id="ds-sess")
            mock_ds.return_value.get_dataset.return_value = SimpleNamespace(id="ds-sess")
            mock_fs.return_value.get_field_metadata.return_value = kwargs.pop("existing", [])
            mock_fs.return_value.save_field_metadata.side_effect = \
                lambda dataset_id, fields: saved.append((dataset_id, [dict(f) for f in fields]))
            import_sessions(db, [(src.name, str(src))], **kwargs)
        assert saved, "字段元数据应已注册"
        return saved[-1][1]

    def test_labels_come_from_row_descriptions(self, tmp_path):
        """注册字段的 label 取行内自描述对照表（精修中文说明未丢失）"""
        _, src = make_record(tmp_path)
        fields = self._import(tmp_path, src, name="会话集")
        by_name = {f["name"]: f for f in fields}
        assert by_name["user_input"]["label"] == SESSION_FIELD_DESCRIPTIONS["user_input"]
        assert by_name["session_messages"]["label"] == SESSION_FIELD_DESCRIPTIONS["session_messages"]
        # 值推断类型：dict → json，str → string
        assert by_name["session_stats"]["type"] == "json"
        assert by_name["session_metadata"]["type"] == "json"
        assert by_name["session_messages"]["type"] == "json"
        assert by_name["source"]["type"] == "string"

    def test_all_row_fields_registered(self, tmp_path):
        """行内容的全部顶层字段都被登记（含此前硬编码清单漏掉的 session_id 等）"""
        record, src = make_record(tmp_path)
        fields = self._import(tmp_path, src, name="会话集")
        assert {f["name"] for f in fields} == set(build_session_data(record).keys())

    def test_derived_fields_are_optional(self, tmp_path):
        """推导字段一律非必填：save_field_metadata 的 is_required 默认 True"""
        _, src = make_record(tmp_path)
        fields = self._import(tmp_path, src, name="会话集")
        assert fields and all(f["is_required"] is False for f in fields)

    def test_positions_sequential(self, tmp_path):
        """position 连续唯一（重复值会让 order_by(position) 排序不稳）"""
        _, src = make_record(tmp_path)
        fields = self._import(tmp_path, src, name="会话集")
        assert [f["position"] for f in fields] == list(range(len(fields)))

    def test_plain_import_has_no_hub_fields(self, tmp_path):
        """本地上传（无 file_extra）不注册 hub 通道字段"""
        _, src = make_record(tmp_path)
        fields = self._import(tmp_path, src, name="会话集")
        assert not (_PLATFORM_INJECTED & {f["name"] for f in fields})

    def test_hub_import_injects_channel_fields_first(self, tmp_path):
        """hub 导入：平台注入字段前置且 type=hub_file（语义无法从行内值推导）"""
        _, src = make_record(tmp_path)
        fields = self._import(
            tmp_path, src, name="hub 会话集",
            file_extra={src.name: {"session_file_id": "fid-1",
                                   "session_file_name": src.name}},
        )
        by_name = {f["name"]: f for f in fields}
        assert by_name["session_file_id"]["type"] == "hub_file"
        assert by_name["session_file_name"]["type"] == "string"
        # 注入字段排在推导字段之前
        assert [f["name"] for f in fields][:2] == ["session_file_id", "session_file_name"]
        # 注入字段不被推导重复登记
        assert len(fields) == len({f["name"] for f in fields})
        assert [f["position"] for f in fields] == list(range(len(fields)))

    def test_hub_ref_fields_still_derived_from_metadata(self):
        """hub 引用字段名清单仍与平台注入元数据同源（释放端逐行释放依赖它成对）"""
        assert SESSION_HUB_REF_FIELDS == ["session_file_id"]
        assert SESSION_HUB_REF_FIELDS == [
            m["name"] for m in SESSION_HUB_FILE_METADATA if m.get("type") == "hub_file"]

    def test_append_keeps_existing_metadata(self, tmp_path):
        """追加导入保留数据集已有字段元数据（save_field_metadata 是全删重建）"""
        _, src = make_record(tmp_path)
        existing = [{"name": "custom_col", "label": "自定义列", "type": "string",
                     "is_media": False, "is_media_field": None,
                     "is_required": False, "position": 99}]
        fields = self._import(tmp_path, src, real_dataset_id="ds-sess", existing=existing)
        by_name = {f["name"]: f for f in fields}
        assert by_name["custom_col"]["label"] == "自定义列"
        # 已有字段的 position 被重排进连续序列，不再与新字段冲突
        assert by_name["custom_col"]["position"] == len(fields) - 1
        assert "user_input" in by_name
