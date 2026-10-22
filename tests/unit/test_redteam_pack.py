"""内置红队样本库回归测试（M3 QE3-1）

防退化基线：
- 每类 ≥30 条、ID 唯一、必填字段齐全、枚举值合法；
- 版本号存在（口径可复现的前提）；
- 类目过滤、数据集行转换（可导入可编辑通道）形状正确；
- 非法库名/未知类目被拒（防路径穿越与配置错误）。
"""
import pytest

from app.services.redteam.pack import (
    list_packs,
    load_pack,
    pack_samples_as_dataset_rows,
)

pytestmark = pytest.mark.unit

EXPECTED_CATEGORIES = ("prompt_injection", "jailbreak", "sensitive_content", "factual_assertion")


class TestBuiltinPack:
    def test_pack_listed_with_version(self):
        packs = list_packs()
        names = [p["name"] for p in packs]
        assert "redteam_pack_v1" in names
        pack = next(p for p in packs if p["name"] == "redteam_pack_v1")
        assert pack["version"], "样本库必须带版本号"
        assert {c["key"] for c in pack["categories"]} == set(EXPECTED_CATEGORIES)

    def test_each_category_at_least_30_samples(self):
        pack = load_pack("redteam_pack_v1")
        counts = {c: 0 for c in EXPECTED_CATEGORIES}
        for sample in pack["samples"]:
            counts[sample["category"]] += 1
        for category, count in counts.items():
            assert count >= 30, f"类目 {category} 样本数 {count} < 30"

    def test_sample_ids_unique_and_schema_valid(self):
        pack = load_pack("redteam_pack_v1")
        ids = [s["id"] for s in pack["samples"]]
        assert len(ids) == len(set(ids)), "样本 ID 必须唯一"
        for sample in pack["samples"]:
            assert sample["prompt"].strip(), f"{sample['id']} 攻击提示词为空"
            assert sample["expected_behavior"] in ("refuse", "safe_answer", "fact_check")
            assert sample["severity"] in ("high", "medium", "low")
            assert sample.get("risk_point"), f"{sample['id']} 缺风险点说明"

    def test_category_filter(self):
        pack = load_pack("redteam_pack_v1", categories=["jailbreak"])
        assert pack["categories"] == ["jailbreak"]
        assert all(s["category"] == "jailbreak" for s in pack["samples"])
        assert len(pack["samples"]) >= 30

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError, match="不支持类目"):
            load_pack("redteam_pack_v1", categories=["not_a_surface"])

    def test_unknown_pack_rejected(self):
        with pytest.raises(ValueError, match="内置样本库不存在"):
            load_pack("no_such_pack")

    def test_pack_name_traversal_rejected(self):
        with pytest.raises(ValueError, match="非法样本库名称"):
            load_pack("../secrets")

    def test_samples_as_dataset_rows(self):
        pack = load_pack("redteam_pack_v1", categories=["prompt_injection"])
        rows = pack_samples_as_dataset_rows(pack["samples"])
        assert len(rows) == len(pack["samples"])
        first = rows[0]
        assert first["user_input"] == pack["samples"][0]["prompt"]
        assert first["attack_category"] == "prompt_injection"
        assert first["attack_sample_id"] == pack["samples"][0]["id"]
        assert "expected_behavior" in first and "risk_point" in first
