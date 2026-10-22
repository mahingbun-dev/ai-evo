"""pass^k 试验统计核心测试（M2 QE2-3，手算对照）

覆盖 app/services/pass_k.py：
- trial_pass_stats：pass@1 / pass^k 的行级手算对照（τ-bench 语义：k 次全过才记 1）；
- score_stats：样本标准差（n-1 分母）与标准误 std/sqrt(n) 的手算对照；
- normalize_trials：None/非法值/越界 clamp（运行时宽容口径）；
- aggregate_pass_k_rows：任务级均值聚合 + legacy 行（无 pass_at_1）跳过。
"""
import math

import pytest

from app.services.pass_k import (
    aggregate_pass_k_rows,
    normalize_trials,
    row_trials,
    score_stats,
    trial_pass_stats,
)

pytestmark = pytest.mark.unit


class TestTrialPassStats:
    def test_hand_computed_mixed(self):
        # 手算：k=4，通过 [T,T,F,T] → pass@1 = 3/4；全过不成立 → pass^k = 0
        stats = trial_pass_stats([True, True, False, True])
        assert stats["trials"] == 4
        assert stats["pass_count"] == 3
        assert stats["pass_at_1"] == 0.75
        assert stats["pass_pow_k"] == 0

    def test_hand_computed_all_pass(self):
        # 手算：k=3 全过 → pass@1 = 1.0，pass^k = 1
        stats = trial_pass_stats([True, True, True])
        assert stats["pass_at_1"] == 1.0
        assert stats["pass_pow_k"] == 1

    def test_hand_computed_all_fail(self):
        # 手算：全失败 → pass@1 = 0，pass^k = 0
        stats = trial_pass_stats([False, False])
        assert stats["pass_at_1"] == 0.0
        assert stats["pass_pow_k"] == 0

    def test_empty_sequence(self):
        stats = trial_pass_stats([])
        assert stats["trials"] == 0
        assert stats["pass_at_1"] == 0.0
        assert stats["pass_pow_k"] == 0


class TestScoreStats:
    def test_hand_computed_sample_std(self):
        # 手算：[1,2,3] 均值 2，样本方差 = ((1)+(0)+(1))/(3-1) = 1 → std=1，
        # sem = 1/sqrt(3)
        stats = score_stats([1.0, 2.0, 3.0])
        assert stats["score_mean"] == 2.0
        assert stats["score_std"] == 1.0
        assert stats["score_sem"] == round(1 / math.sqrt(3), 4)

    def test_binary_trial_scores(self):
        # consistency/success_rate 的 0/1 trial 分数：[1,0,1,1]
        # 均值 0.75，样本方差 = (0.0625*3 + 0.5625)/3 = 0.25 → std=0.5
        stats = score_stats([1.0, 0.0, 1.0, 1.0])
        assert stats["score_mean"] == 0.75
        assert stats["score_std"] == 0.5
        assert stats["score_sem"] == 0.25

    def test_single_value_no_dispersion(self):
        # n<2 无离散（避免除零）：std/sem = 0
        stats = score_stats([5.0])
        assert stats["score_std"] == 0.0
        assert stats["score_sem"] == 0.0


class TestNormalizeTrials:
    def test_none_and_invalid_to_disabled(self):
        assert normalize_trials(None) == 1
        assert normalize_trials("3") == 1  # 非数字运行时宽容 → 不开启
        assert normalize_trials(True) == 1  # bool 排除

    def test_clamp_range(self):
        assert normalize_trials(1) == 1
        assert normalize_trials(0) == 1
        assert normalize_trials(2) == 2
        assert normalize_trials(5) == 5
        assert normalize_trials(9) == 5  # 越界收口到上限
        assert normalize_trials(4.0) == 4

    def test_row_trials_judge_only(self):
        judge_task = type("Task", (), {"evaluation_type": "judge", "trials": 3})()
        assert row_trials(judge_task) == 3

    def test_row_trials_non_judge_ignored(self):
        # 非 judge 类型（多 trial 走各自 repeat_count 通道）恒为 1
        other_task = type("Task", (), {"evaluation_type": "consistency", "trials": 5})()
        assert row_trials(other_task) == 1

    def test_row_trials_missing_attr(self):
        bare = type("Task", (), {"evaluation_type": "judge"})()
        assert row_trials(bare) == 1


class TestAggregatePassKRows:
    def test_task_level_hand_computed(self):
        # 手算：3 行 pass@1 = [1.0, 0.5, 0.0]，pass^k = [1, 0, 0]
        # → 平均 pass@1 = 0.5，平均 pass^k = 1/3，全过 1 行
        rows = [
            {"pass_at_1": 1.0, "pass_pow_k": 1},
            {"pass_at_1": 0.5, "pass_pow_k": 0},
            {"pass_at_1": 0.0, "pass_pow_k": 0},
        ]
        agg = aggregate_pass_k_rows(rows)
        assert agg["rows"] == 3
        assert agg["average_pass_at_1"] == 0.5
        assert agg["average_pass_pow_k"] == round(1 / 3, 4)
        assert agg["all_pass_rows"] == 1

    def test_metrics_embedded_rows(self):
        # consistency/success_rate 行的 pass^k 内嵌在 metrics 子 dict
        rows = [{"metrics": {"pass_at_1": 1.0, "pass_pow_k": 1}}]
        agg = aggregate_pass_k_rows(rows)
        assert agg["rows"] == 1
        assert agg["average_pass_pow_k"] == 1.0

    def test_legacy_rows_return_none(self):
        # 旧行无 pass^k 字段 → None（聚合不写 pass_k 键）
        assert aggregate_pass_k_rows([{"metrics": {"text_similarity": 0.9}}]) is None
        assert aggregate_pass_k_rows([]) is None

    def test_mixed_rows_skip_legacy(self):
        # 新旧行混排：旧行跳过，只统计携带 pass_at_1 的行
        rows = [
            {"metrics": {}},  # legacy
            {"pass_at_1": 1.0, "pass_pow_k": 1},
        ]
        agg = aggregate_pass_k_rows(rows)
        assert agg["rows"] == 1
        assert agg["average_pass_at_1"] == 1.0
