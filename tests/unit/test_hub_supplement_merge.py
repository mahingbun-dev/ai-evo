"""W9 merged_index 同名补传替换单测（补传预览/执行优先级统一）

计划 §3 W9：补传预览/执行优先级统一（hub_file_service 预览 :1579 merged_index
vs 执行 resolve_hub_file_fields :817-830）——预览视图改为「合并后视图 +
补传条目打 is_manual 标记」，保证所见即所评：
- 旧实现：supplement_index 简单追加在 file_index 后 → 同名 path 出现两条，
  匹配预览 matched_count 虚高（执行镜像 hardlink 合并后同名只有一个文件）；
- 新实现：同名 path 的补传条目**替换**原始条目（保留原始位置），与执行镜像
  「解包区先、补传区后、同名以补传版为准」严格一致。

纯函数测试（不连库），配套集成矩阵见 tests/integration/test_idor_matrix.py。
"""
from types import SimpleNamespace

import pytest

from app.services.hub_file_service import match_files_in_index, merged_index

pytestmark = pytest.mark.unit


def make_record(file_index, supplement_index):
    return SimpleNamespace(
        id="hub_rec_w9", file_id="fid-w9", file_index=file_index,
        supplement_index=supplement_index,
    )


class TestMergedIndexSameNameReplacement:
    def test_no_supplements_returns_base_asis(self):
        base = [{"path": "a.json", "type": "text", "size": 3}]
        assert merged_index(make_record(base, [])) == base

    def test_supplement_appended_with_is_manual_mark(self):
        base = [{"path": "a.json", "type": "text", "size": 3}]
        supp = [{"path": "new/report.json", "type": "text", "size": 5}]
        merged = merged_index(make_record(base, supp))
        assert [e["path"] for e in merged] == ["a.json", "new/report.json"]
        assert merged[1]["is_manual"] is True
        assert "is_manual" not in merged[0]

    def test_same_name_supplement_replaces_base_entry(self):
        """同名补传替换原始条目：合并视图中同名 path 只出现一条（补传版）"""
        base = [
            {"path": "reports/allure.json", "type": "json", "size": 100},
            {"path": "logs/run.log", "type": "text", "size": 10},
        ]
        supp = [{"path": "reports/allure.json", "type": "json", "size": 250}]
        merged = merged_index(make_record(base, supp))
        paths = [e["path"] for e in merged]
        assert paths == ["logs/run.log", "reports/allure.json"], paths
        by_path = {e["path"]: e for e in merged}
        # 补传版（size=250）替换原始版（size=100）
        assert by_path["reports/allure.json"]["size"] == 250
        assert by_path["reports/allure.json"]["is_manual"] is True
        # 原始条目让位后其余视图保持纯原始（无 is_manual 标记）
        assert "is_manual" not in by_path["logs/run.log"]

    def test_same_name_match_preview_count_equals_execution(self):
        """匹配预览与执行镜像同语义：同名覆盖后 matched_count=1（旧实现虚高为 2）"""
        base = [
            {"path": "r/summary.json", "type": "json", "size": 1},
            {"path": "other.txt", "type": "text", "size": 1},
        ]
        supp = [{"path": "r/summary.json", "type": "json", "size": 2}]
        preview_index = merged_index(make_record(base, supp))
        matched = match_files_in_index(preview_index, "r/*.json")
        assert matched == ["r/summary.json"], matched

    def test_base_untouched_by_merge(self):
        """file_index 落库保持纯原始：merged_index 不改写 base 列表与条目"""
        base = [{"path": "r/summary.json", "type": "json", "size": 1}]
        supp = [{"path": "r/summary.json", "type": "json", "size": 2}]
        record = make_record(base, supp)
        merged_index(record)
        assert record.file_index[0]["size"] == 1
        assert "is_manual" not in record.file_index[0]
