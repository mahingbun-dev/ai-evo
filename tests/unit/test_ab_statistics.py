"""A/B 显著性统计测试（M2 QE2-3，对照 scipy 直算与手算公式）

覆盖 app/services/ab_statistics.py：
- Cliff's delta：与测试内独立双重循环暴力实现逐值对照 + 分级阈值；
- Wilcoxon p：与 scipy.stats.wilcoxon 直算一致 + 全正差小样本精确 p 手算
  （n=5 全同号 → p = 2/2^5 = 0.0625）；
- 均值差 95% CI：与手算公式 mean ± t_{0.975,n-1}·sd/sqrt(n) 一致；
- McNemar 精确 p：与手算二项精确公式
  p = min(1, 2·Σ_{k≤min(b,c)} C(b+c,k)·0.5^(b+c)) 一致；
- 结论语句方向（B 优/A 优/无显著差异）、样本量充足提示、
  样本不足/全零差的收敛行为、compute_ab_significance 轮数门槛。
"""
import math
from math import comb

import pytest
from scipy import stats as sps

from app.services.ab_statistics import (
    ALPHA,
    cliffs_delta,
    compute_ab_significance,
    mcnemar_comparison,
    paired_comparison,
)

pytestmark = pytest.mark.unit


def _brute_force_cliffs_delta(a, b):
    """独立暴力实现（与被测实现不同写法）：#(b>a) - #(b<a) / (n_a*n_b)"""
    greater = sum(1 for bv in b for av in a if bv > av)
    less = sum(1 for bv in b for av in a if bv < av)
    return (greater - less) / (len(a) * len(b))


class TestCliffsDelta:
    def test_matches_brute_force(self):
        # 与 scipy 直算/独立暴力实现对照（被测值 round 4 位小数）
        a = [1.2, 3.4, 5.6, 7.8, 2.1, 4.4]
        b = [2.0, 3.9, 6.1, 0.5, 8.8, 4.4, 5.0]
        delta, magnitude = cliffs_delta(a, b)
        assert delta == pytest.approx(_brute_force_cliffs_delta(a, b), abs=5e-5)

    def test_complete_dominance(self):
        # B 全部大于 A → δ=+1（大效应，B 占优）
        delta, magnitude = cliffs_delta([1.0, 2.0], [3.0, 4.0])
        assert delta == 1.0
        assert magnitude == "大"

    def test_identical_distributions(self):
        # 完全重叠 → δ=0（可忽略）
        delta, magnitude = cliffs_delta([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert delta == 0.0
        assert magnitude == "可忽略"

    def test_empty_inputs(self):
        assert cliffs_delta([], [1.0]) == (0.0, "可忽略")


class TestPairedComparison:
    def test_wilcoxon_p_matches_direct_scipy(self):
        # 与 scipy.stats.wilcoxon 直算逐值对照（同输入差值序列）
        a = [8.2, 9.1, 7.7, 8.9, 9.5, 8.0, 7.4, 9.9, 8.3, 7.1]
        b = [8.9, 9.0, 8.6, 9.3, 9.4, 8.8, 8.1, 9.7, 9.0, 7.9]
        result = paired_comparison(a, b)
        diffs = [bb - aa for aa, bb in zip(a, b) if bb != aa]
        direct = sps.wilcoxon(diffs).pvalue
        assert result["wilcoxon_p"] == pytest.approx(direct, abs=1e-6)
        assert result["n"] == 10

    def test_wilcoxon_exact_all_positive_hand_computed(self):
        # 手算：5 对差值全为 +1（全同号）→ 双侧精确 p = 2·(1/2)^5 = 0.0625
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [2.0, 3.0, 4.0, 5.0, 6.0]
        result = paired_comparison(a, b)
        assert result["wilcoxon_p"] == pytest.approx(2 / 32, abs=1e-6)
        # 未达显著 → 结论为无显著差异
        assert result["significant"] is False
        assert "无显著差异" in result["conclusion"]
        # 常数差值 → sd=0，CI 收敛为单点 [1.0, 1.0]
        assert result["diff_ci95"] == [1.0, 1.0]

    def test_diff_ci95_hand_computed(self):
        # 手算：差值 [1, 2, 3]（B-A），mean=2，sd=1，t_{0.975,2}=4.3027
        # CI = 2 ± 4.3027·1/sqrt(3) ≈ [0.5132, 3.4868]
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]
        result = paired_comparison(a, b)
        t_crit = float(sps.t.ppf(0.975, 2))
        half = t_crit * 1.0 / math.sqrt(3)
        assert result["diff_ci95"][0] == pytest.approx(2 - half, abs=1e-4)
        assert result["diff_ci95"][1] == pytest.approx(2 + half, abs=1e-4)
        # Cliff's δ 手算：a=[1,2,3], b=[2,4,6] 交叉对 greater=7、less=1 → 6/9
        assert result["cliffs_delta"] == pytest.approx(6 / 9, abs=1e-4)

    def test_significant_better_conclusion(self):
        # B 稳定大 3：n=8 全同号 → 精确 p = 2/2^8 = 0.0078 < 0.05 显著
        a = [1.0] * 8
        b = [4.0] * 8
        result = paired_comparison(a, b)
        assert result["wilcoxon_p"] == pytest.approx(2 / 256, abs=1e-6)
        assert result["significant"] is True
        assert "B 显著优于 A" in result["conclusion"]
        assert "p=0.008" in result["conclusion"]
        assert "n=8" in result["conclusion"]

    def test_direction_a_better_latency(self):
        # 耗时场景（lower-is-better）：A 稳定更快（数值小）→ A 显著优于 B
        a = [1.0] * 8  # 耗时小
        b = [3.0] * 8  # 耗时大
        result = paired_comparison(a, b, metric_name="耗时(秒)", higher_is_better=False)
        assert result["significant"] is True
        assert "A 显著优于 B" in result["conclusion"]
        assert result["metric_name"] == "耗时(秒)"

    def test_direction_default_higher_is_better(self):
        # 默认（评分类，higher-is-better）：B 数值稳定更高 → B 显著优于 A
        a = [1.0] * 8
        b = [3.0] * 8
        result = paired_comparison(a, b)
        assert result["significant"] is True
        assert "B 显著优于 A" in result["conclusion"]

    def test_missing_pairs_dropped(self):
        # 任一侧缺失的轮次整对剔除（两侧都没跑完不构成配对证据）
        a = [1.0, None, 2.0, None]
        b = [2.0, 3.0, None, 4.0]
        result = paired_comparison(a, b)
        assert result["n"] == 1
        assert result["p_value"] is None
        assert "样本不足" in result["conclusion"]

    def test_insufficient_samples(self):
        result = paired_comparison([1.0], [2.0])
        assert result["note"] == "insufficient_samples"
        assert result["significant"] is False

    def test_all_zero_diffs_p_is_one(self):
        # 两侧逐对完全一致：无差异证据，p=1 + 说明
        a = [1.0, 2.0, 3.0, 4.0]
        b = [1.0, 2.0, 3.0, 4.0]
        result = paired_comparison(a, b)
        assert result["wilcoxon_p"] == 1.0
        assert "完全一致" in result["note"]

    def test_adequacy_flag_small_n(self):
        result = paired_comparison([1.0, 2.0, 3.0], [2.0, 3.0, 4.0])
        assert result["adequate"] is False
        assert "Wilcoxon" in result["note"]


class TestMcNemarComparison:
    def test_exact_p_hand_computed(self):
        # 手算：b=1（A过B败），c=5（A败B过）→ 不一致对 6
        # p = min(1, 2·Σ_{k=0}^{1} C(6,k)·0.5^6) = 2·(1+6)/64 = 0.21875
        outcomes_a = [True, True, False, False, False, False, False, True]
        outcomes_b = [False, True, True, True, True, True, True, True]
        result = mcnemar_comparison(outcomes_a, outcomes_b)
        expected = min(1.0, 2 * sum(comb(6, k) for k in range(0, 1 + 1)) * 0.5 ** 6)
        assert result["b_a_pass_b_fail"] == 1
        assert result["c_a_fail_b_pass"] == 5
        assert result["p_value"] == pytest.approx(expected, abs=1e-6)

    def test_significant_direction_b(self):
        # b=0，c=10：A 全失败 B 全通过（B 翻盘）→ p = 2·C(10,0)·0.5^10
        # = 0.00195 显著，B 优
        outcomes_a = [False] * 10
        outcomes_b = [True] * 10
        result = mcnemar_comparison(outcomes_a, outcomes_b)
        assert result["p_value"] == pytest.approx(2 * 0.5 ** 10, abs=1e-6)
        assert result["significant"] is True
        assert "B 显著优于 A" in result["conclusion"]

    def test_significant_direction_a(self):
        # 镜像：A 全通过 B 全失败 → A 显著优于 B
        outcomes_a = [True] * 10
        outcomes_b = [False] * 10
        result = mcnemar_comparison(outcomes_a, outcomes_b)
        assert result["significant"] is True
        assert "A 显著优于 B" in result["conclusion"]

    def test_no_discordant_pairs(self):
        # 两侧逐对同胜负：无不一致对，p=1 + 说明
        result = mcnemar_comparison([True, False, True], [True, False, True])
        assert result["discordant"] == 0
        assert result["p_value"] == 1.0
        assert "无不一致对" in result["note"]

    def test_insufficient_samples(self):
        result = mcnemar_comparison([True], [False])
        assert result["p_value"] is None
        assert "样本不足" in result["conclusion"]

    def test_adequacy_follows_discordant_count(self):
        # 判定力看不一致对：n=20 但 b+c=4 → 不充足
        a = [True] * 18 + [True, True]
        b = [True] * 18 + [False, False]
        result = mcnemar_comparison(a, b)
        assert result["discordant"] == 2
        assert result["adequate"] is False


class TestComputeAbSignificance:
    def _rounds(self, count, make):
        return [make(i) for i in range(1, count + 1)]

    def test_below_two_rounds_returns_none(self):
        assert compute_ab_significance([]) is None
        assert compute_ab_significance([
            {"duration_a": 1.0, "duration_b": 2.0, "success_a": True, "success_b": True}
        ]) is None

    def test_bundle_shape(self):
        rounds = self._rounds(6, lambda i: {
            "duration_a": 1.0 + i, "duration_b": 2.0 + i,
            "success_a": True, "success_b": i % 2 == 0,
        })
        bundle = compute_ab_significance(rounds)
        assert bundle["rounds"] == 6
        assert bundle["duration_paired"]["method"] == "wilcoxon_paired"
        assert bundle["success_mcnemar"]["method"] == "mcnemar_exact"
        assert bundle["duration_paired"]["n"] == 6
        assert bundle["success_mcnemar"]["n"] == 6
