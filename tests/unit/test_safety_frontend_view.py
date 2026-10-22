"""M3 QE3-3 前端安全视图契约测试

Tasks 安全类型配置与 Results 安全专视图的前端源断言（仓库 W12 起的前端契约
测试模式：无前端测试运行器，以源码关键结构断言钉住功能不回退）：

- Tasks/SafetyEvaluationConfig：类目多选、样本库选择（含导入数据集）、变异开关；
- Results 安全专视图：类目热力（ECharts heatmap + 渲染分支）、证据引用
  （行展开面板 + 引用串 + 掩码证据 + deepeval 分值）、违规行筛选。
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


class TestTasksSafetyConfig:
    """Tasks 安全类型配置（SafetyEvaluationConfig.vue）"""

    def test_category_multi_select_with_four_surfaces(self):
        content = _read("components/SafetyEvaluationConfig.vue")
        assert 'v-model="formData.categories"' in content
        for key in ("prompt_injection", "jailbreak", "sensitive_content", "factual_assertion"):
            assert key in content, f"攻击面类目缺失: {key}"

    def test_pack_select_with_import_dataset(self):
        content = _read("components/SafetyEvaluationConfig.vue")
        assert 'v-model="formData.pack"' in content
        assert "redteamApi.packs()" in content
        assert "导入为数据集" in content
        assert "redteamApi.importPack" in content

    def test_mutation_switch_with_count_bounds(self):
        content = _read("components/SafetyEvaluationConfig.vue")
        assert 'v-model="formData.mutation.enabled"' in content
        assert 'v-model="formData.mutation.count"' in content
        assert ":min=\"1\"" in content and ":max=\"3\"" in content


class TestResultsSafetyView:
    """Results 安全专视图（Results.vue）"""

    def test_category_heatmap_rendered(self):
        content = _read("views/Results.vue")
        # 热力图 DOM 挂点 + ECharts heatmap 系列 + 渲染分支接线
        assert 'id="safetyCategoryHeatmap"' in content
        assert "type: 'heatmap'" in content
        assert "const renderSafetyHeatmap" in content
        assert "renderSafetyHeatmap()" in content
        # renderCharts 对 safety 类型走热力渲染分支
        assert "evaluation_type === 'safety'" in content

    def test_heatmap_matrix_counts_by_channel(self):
        """热力矩阵交叉计数口径：确定性 triggered / 裁判 violation /
        deepeval 逐指标 success===false 且非不可用（与聚合口径一致）"""
        content = _read("views/Results.vue")
        assert "channels.deterministic?.triggered) counts[y][0] += 1" in content
        assert "channels.judge_rubric?.violation) counts[y][1] += 1" in content
        assert "m.success === false && !m.metric_unavailable" in content

    def test_evidence_expand_panel_with_citation(self):
        content = _read("views/Results.vue")
        # 行展开证据面板 + 可复制引用串
        assert '<el-table-column type="expand">' in content
        assert "safetyEvidenceCite(row)" in content
        assert "const safetyEvidenceCite" in content
        # 样本溯源信息（样本 ID/期望行为/风险点/变异溯源）
        for anchor in ("样本 ID", "期望行为", "风险点", "样本溯源"):
            assert anchor in content, f"证据面板缺少: {anchor}"

    def test_evidence_masked_pii_not_leaked(self):
        """确定性通道证据展示必须掩码优先（不泄露 PII 原文）"""
        content = _read("views/Results.vue")
        assert "f.masked || f.evidence" in content

    def test_evidence_blocks_cover_all_channels(self):
        content = _read("views/Results.vue")
        assert "确定性通道证据" in content
        assert "裁判 rubric 证据" in content
        assert "deepeval 指标证据" in content
        # 裁判证据含回复摘录引用
        assert "row.safety.channels.judge_rubric.evidence" in content

    def test_violation_row_filter(self):
        content = _read("views/Results.vue")
        assert 'v-model="safetyOnlyViolations"' in content
        assert "rows.filter(row => row.safety?.violation)" in content
