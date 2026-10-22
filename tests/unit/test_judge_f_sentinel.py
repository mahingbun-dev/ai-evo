#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F 哨兵契约测试：裁判对「本行不涉及」的维度输出字符串 "F" 时，
normalize_judge_result 必须丢弃该维度分——下游维度均分只统计适用行，
平台雷达/维度均分不被哨兵值污染（anc-data-clean 省略路线的确定性替代）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

from app.services.harness_adapter import normalize_judge_result


class TestFSentinel:
    def test_f_sentinel_dropped_from_dimension_scores(self):
        result = normalize_judge_result({
            "score": 7.5,
            "feedback": "权限与数据隔离：本行不涉及",
            "dimension_scores": {"UI截图": 5.5, "权限与数据隔离": "F"},
        })
        assert result["dimension_scores"] == {"UI截图": 5.5}, "F 哨兵维度必须被丢弃"
        assert result["score"] == 7.5

    def test_numeric_dims_survive_alongside_f(self):
        result = normalize_judge_result({
            "score": 8,
            "feedback": "ok",
            "dimension_scores": {"造数过程": 9, "导出校验": "F", "并发与幂等": "6.5"},
        })
        assert set(result["dimension_scores"]) == {"造数过程", "并发与幂等"}
        assert result["dimension_scores"]["并发与幂等"] == 6.5

    def test_all_f_dims_yields_empty(self):
        result = normalize_judge_result({
            "score": 6,
            "feedback": "本行仅涉及造数与UI截图之外维度的反例",
            "dimension_scores": {"权限与数据隔离": "F"},
        })
        assert result["dimension_scores"] == {}
