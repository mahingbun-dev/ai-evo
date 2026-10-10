"""
列表接口性能修复单元测试

覆盖三项改动（实测修复前：数据集列表 14.5s / 预览 7.3s / 任务列表 3.2s）：
- P0-1 get_datasets 的 has_media 从行级数据判断改为字段元数据批量推导，
  无元数据的数据集退回行级判断保语义
- P0-2 get_data_list_v2_slim 在 DB 端把超大字段替换为占位摘要
- P0-3 get_tasks defer 列表接口不需要的大 JSON 列

运行: pytest tests/unit/test_list_performance.py -v
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.dataset_service import DatasetService, DatasetDataService
from app.services.evaluation_task_service import EvaluationTaskService


def make_dataset(ds_id, name="ds"):
    return SimpleNamespace(id=ds_id, name=name, question_count=0, has_media=False)


def chain_mock(result):
    """构造支持任意链式调用并最终返回 result 的 mock"""
    m = MagicMock()
    m.filter.return_value = m
    m.order_by.return_value = m
    m.offset.return_value = m
    m.limit.return_value = m
    m.group_by.return_value = m
    m.options.return_value = m
    m.all.return_value = result
    return m


class TestGetDatasetsHasMedia:
    """P0-1：has_media 字段元数据批量推导"""

    def _make_service(self, datasets, count_rows, meta_rows):
        svc = DatasetService.__new__(DatasetService)
        svc.db = MagicMock()
        svc.db.query.side_effect = [
            chain_mock(datasets),   # EvaluationDataset 列表
            chain_mock(count_rows),  # 题目数聚合
            chain_mock(meta_rows),   # 字段元数据（dataset_id, is_media）
        ]
        return svc

    def test_media_metadata_true_sets_has_media(self):
        """字段元数据标记 is_media=true → has_media=true，不走行级判断"""
        svc = self._make_service(
            [make_dataset("ds1")], [],
            [SimpleNamespace(dataset_id="ds1", is_media=True)],
        )
        svc._check_has_media = MagicMock(return_value=False)  # 若被调用则断言可辨
        result = svc.get_datasets()
        assert result[0].has_media is True
        svc._check_has_media.assert_not_called()

    def test_media_metadata_all_false_keeps_false(self):
        """字段元数据存在但全非媒体 → has_media=false，不走行级判断"""
        svc = self._make_service(
            [make_dataset("ds1")], [],
            [SimpleNamespace(dataset_id="ds1", is_media=False)],
        )
        svc._check_has_media = MagicMock(return_value=True)
        result = svc.get_datasets()
        assert result[0].has_media is False
        svc._check_has_media.assert_not_called()

    def test_no_metadata_falls_back_to_row_check(self):
        """无字段元数据（json 导入/单条添加的旧数据集）→ 退回行级判断"""
        svc = self._make_service([make_dataset("ds1")], [], [])
        svc._check_has_media = MagicMock(return_value=True)
        result = svc.get_datasets()
        assert result[0].has_media is True
        svc._check_has_media.assert_called_once_with("ds1")

    def test_question_count_written_back(self):
        """题目数聚合结果写回 question_count"""
        svc = self._make_service(
            [make_dataset("ds1"), make_dataset("ds2")],
            [SimpleNamespace(dataset_id="ds1", cnt=7)],
            [],
        )
        svc._check_has_media = MagicMock(return_value=False)
        result = svc.get_datasets()
        assert result[0].question_count == 7
        assert result[1].question_count == 0


class TestGetDataListV2Slim:
    """P0-2：列表展示用 DB 端瘦身查询"""

    def _make_service(self, rows):
        svc = DatasetDataService.__new__(DatasetDataService)
        svc.db = MagicMock()
        svc.db.execute.return_value.mappings.return_value.all.return_value = rows
        return svc

    def test_returns_dict_items_and_passes_params(self):
        rows = [{"id": "d1", "index": 1, "data": {"user_input": "x"}}]
        svc = self._make_service(rows)
        result = svc.get_data_list_v2_slim("ds1", skip=10, limit=5)
        assert result == [{"id": "d1", "index": 1, "data": {"user_input": "x"}}]
        params = svc.db.execute.call_args[0][1]
        assert params == {"ds_id": "ds1", "skip": 10, "lim": 5,
                          "max_bytes": DatasetDataService.OVERSIZE_FIELD_BYTES}

    def test_custom_max_field_bytes_overrides_default(self):
        svc = self._make_service([])
        svc.get_data_list_v2_slim("ds1", max_field_bytes=1024)
        params = svc.db.execute.call_args[0][1]
        assert params["max_bytes"] == 1024

    def test_default_threshold_is_64kb(self):
        """阈值 64KB：常规字段永不命中，只有会话内容类大字段被替换"""
        assert DatasetDataService.OVERSIZE_FIELD_BYTES == 64 * 1024


class TestSlimItemData:
    """P0-2 补充：add/update 写接口响应瘦身"""

    def test_oversize_value_replaced_with_placeholder(self):
        from app.api.dataset_api import _slim_item_data
        big = {"messages": ["x" * 100 for _ in range(1000)]}  # > 64KB
        result = _slim_item_data({"small": "ok", "big": big})
        assert result["small"] == "ok"
        assert result["big"]["__oversize__"] is True
        assert result["big"]["size_bytes"] > 64 * 1024

    def test_normal_data_untouched(self):
        from app.api.dataset_api import _slim_item_data
        data = {"user_input": "问题", "stats": {"turns": 3}}
        assert _slim_item_data(data) == data

    def test_non_dict_passthrough(self):
        from app.api.dataset_api import _slim_item_data
        assert _slim_item_data(None) is None
        assert _slim_item_data("x") == "x"


class TestUpdateDataV2Patch:
    """P0-4：小字段补丁式更新（避免整行读写大 JSON）"""

    def _make_service(self, execute_result=None):
        svc = DatasetDataService.__new__(DatasetDataService)
        svc.db = MagicMock()
        svc.db.execute.return_value.mappings.return_value.first.return_value = execute_result
        return svc

    def test_patchable_small_value(self):
        svc = self._make_service()
        assert svc._is_patchable_field("user_input", "小值") is True
        assert svc._is_patchable_field("stats", {"turns": 3}) is True

    def test_oversize_value_not_patchable(self):
        svc = self._make_service()
        assert svc._is_patchable_field("big", "x" * (64 * 1024 + 1)) is False

    def test_all_small_fields_take_patch_path(self):
        """全部小字段 → 走补丁 SQL（不读整行 ORM）"""
        svc = self._make_service(execute_result={"id": "d1", "index": 0, "dataset_id": "ds1"})
        result = svc.update_data_v2("d1", {"a": 1, "b": "x"})
        assert (result.id, result.index) == ("d1", 0)
        assert result.data == {"a": 1, "b": "x"}
        sql = svc.db.execute.call_args[0][0].text
        assert "||" in sql  # jsonb 顶层合并

    def test_oversize_field_falls_back_to_full_row(self):
        """含超大值 → 回退整行合并路径（get_data_v2 被调用）"""
        svc = self._make_service()
        svc.get_data_v2 = MagicMock(return_value=None)  # None → 直接返回 None
        result = svc.update_data_v2("d1", {"session_messages": "x" * (64 * 1024 + 1)})
        assert result is None
        svc.get_data_v2.assert_called_once_with("d1")

    def test_missing_row_returns_none(self):
        """补丁 UPDATE 未命中行 → None（与整行路径语义一致）"""
        svc = self._make_service(execute_result=None)
        assert svc.update_data_v2("d1", {"a": 1}) is None


class TestGetTasksDeferredColumns:
    """P0-3：任务列表 defer 大 JSON 列"""

    def test_defer_options_applied_to_query(self):
        svc = EvaluationTaskService.__new__(EvaluationTaskService)
        svc.db = MagicMock()
        query = chain_mock([])
        svc.db.query.return_value = query
        svc.get_tasks(status="running", limit=50)
        svc.db.query.assert_called_once()
        # options 收到 8 个独立的 defer 加载器（对应 8 个大列）
        args, _ = query.options.call_args
        assert len(args) == len(EvaluationTaskService._TASK_LIST_DEFERRED_COLUMNS)

    def test_deferred_columns_not_in_task_response(self):
        """defer 列必须都不在 TaskResponse 字段里，否则会触发逐行惰性加载（N+1）"""
        from app.api.evaluation_task_api import TaskResponse
        overlap = set(EvaluationTaskService._TASK_LIST_DEFERRED_COLUMNS) & set(
            TaskResponse.model_fields)
        assert overlap == set(), f"defer 列出现在 TaskResponse: {overlap}"

    def test_result_column_is_deferred(self):
        """result 列（存量平均 127KB/行）必须 defer"""
        assert "result" in EvaluationTaskService._TASK_LIST_DEFERRED_COLUMNS
