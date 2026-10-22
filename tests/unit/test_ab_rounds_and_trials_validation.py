"""A/B 多轮合并与 trials 配置校验测试（M2 QE2-3）

- ab_test_api._merge_round_results：多轮单侧结果的保存形态
  （任一轮成功/首份非空响应/耗时合计/逐轮明细）；
- ABTestService.create_session：significance 随会话落库（mock db）；
- EvaluationTaskService._validate_trials：judge-only / 1-5 整数白名单。
"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.services.ab_test_service import ABTestService
from app.services.evaluation_task_service import EvaluationTaskService

pytestmark = pytest.mark.unit


def _import_merge_round_results():
    from app.api.ab_test_api import _merge_round_results

    return _merge_round_results


class TestMergeRoundResults:
    def test_merge_shape(self):
        merge = _import_merge_round_results()
        merged = merge([
            {"success": False, "content": "", "request_details": [{"i": 1}], "duration": 1.5},
            {"success": True, "content": "resp", "request_details": [{"i": 2}], "duration": 2.5},
        ])
        assert merged["success"] is True          # 任一轮成功
        assert merged["content"] == "resp"        # 首份非空响应
        assert merged["duration"] == 4.0          # 耗时合计
        assert merged["request_details"] == [{"i": 1}, {"i": 2}]
        assert merged["rounds"] == [
            {"round": 1, "duration": 1.5, "success": False},
            {"round": 2, "duration": 2.5, "success": True},
        ]

    def test_all_failed_rounds(self):
        merge = _import_merge_round_results()
        merged = merge([{"success": False, "content": "", "request_details": [], "duration": 1.0}])
        assert merged["success"] is False
        assert merged["content"] == ""


class TestABSessionSignificance:
    def test_create_session_persists_significance(self):
        service = ABTestService(MagicMock())
        significance = {"rounds": 3, "duration_paired": {"n": 3}, "success_mcnemar": {"n": 3}}
        service.create_session(
            model_a_id="ma", model_b_id="mb",
            result_a={"success": True}, result_b={"success": False},
            significance=significance,
        )
        # MagicMock db：会话对象经 db.add 落库，这里校验构造参数已带上显著性
        added = service.db.add.call_args[0][0]
        assert added.significance == significance

    def test_create_session_default_significance_none(self):
        service = ABTestService(MagicMock())
        service.create_session(model_a_id="ma", model_b_id="mb")
        added = service.db.add.call_args[0][0]
        assert added.significance is None


class TestTrialsValidation:
    def _service(self):
        return EvaluationTaskService(MagicMock())

    def test_judge_trials_valid(self):
        self._service()._validate_trials("judge", 3)
        self._service()._validate_trials("judge", 5)
        self._service()._validate_trials("judge", 1)
        self._service()._validate_trials("judge", None)

    def test_non_integer_rejected(self):
        with pytest.raises(ValueError, match="整数"):
            self._service()._validate_trials("judge", "3")
        with pytest.raises(ValueError, match="整数"):
            self._service()._validate_trials("judge", True)

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="1-5"):
            self._service()._validate_trials("judge", 0)
        with pytest.raises(ValueError, match="1-5"):
            self._service()._validate_trials("judge", 6)

    def test_non_judge_type_rejected(self):
        with pytest.raises(ValueError, match="仅支持 judge"):
            self._service()._validate_trials("consistency", 3)
        with pytest.raises(ValueError, match="仅支持 judge"):
            self._service()._validate_trials("latency", 2)
