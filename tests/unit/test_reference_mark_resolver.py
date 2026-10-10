"""引用标记解析器（reference_mark_resolver）单元测试

覆盖：标记提取（语法变体/无名标记/非字符串输入）、按 id/key 精确命中、
id 未命中按显示名兜底、全未命中置空串且不抛错（记 warning）、
注入格式「【显示名】\\n内容」、resolve_task_ref_overrides 只改副本
（含无标记返回副本、score_details 与 score_criteria[].desc 两处解析）。
"""
import logging
from types import SimpleNamespace

import pytest

from app.services.reference_mark_resolver import (
    REFERENCE_MARK_PATTERN,
    extract_reference_marks,
    resolve_reference_marks,
    resolve_task_ref_overrides,
)


def _make_ctx():
    """构造标准解析上下文：1 个目标 + 2 个变量"""
    return {
        "targets": {
            "t-001": {"name": "模型输出", "content": "这是模型输出内容"},
        },
        "vars": {
            "ref_answer": "这是参考答案",
            "is_valid": True,
        },
    }


# ---------------- extract_reference_marks ----------------

class TestExtractReferenceMarks:

    def test_extract_target_and_var(self):
        """标准语法：target/var 两类标记各提取一条，含原文与各字段"""
        text = "参考 {{ref:target:t-001|模型输出}} 与 {{ref:var:ref_answer|参考答案}}"
        marks = extract_reference_marks(text)
        assert [m["kind"] for m in marks] == ["target", "var"]
        assert marks[0]["key"] == "t-001"
        assert marks[0]["name"] == "模型输出"
        assert marks[0]["raw"] == "{{ref:target:t-001|模型输出}}"
        assert marks[1]["key"] == "ref_answer"
        assert marks[1]["name"] == "参考答案"

    def test_extract_without_name(self):
        """显示名段可省略：name 为空串"""
        marks = extract_reference_marks("{{ref:var:ref_answer}}")
        assert len(marks) == 1
        assert marks[0]["name"] == ""
        assert marks[0]["raw"] == "{{ref:var:ref_answer}}"

    def test_extract_empty_name_with_pipe(self):
        """竖线后为空：name 仍为空串，key 正常提取"""
        marks = extract_reference_marks("{{ref:target:t-001|}}")
        assert marks[0]["key"] == "t-001"
        assert marks[0]["name"] == ""

    def test_extract_non_string_input(self):
        """None/空串/非字符串输入返回空列表，不抛错"""
        assert extract_reference_marks(None) == []
        assert extract_reference_marks("") == []
        assert extract_reference_marks(123) == []

    def test_extract_no_marks(self):
        """无标记文本返回空列表"""
        assert extract_reference_marks("普通文本，没有引用") == []

    def test_pattern_rejects_invalid(self):
        """正则不匹配非法形态：kind 未知、空 key、名称内含 }（无法闭合）"""
        assert REFERENCE_MARK_PATTERN.search("{{ref:other:t1|名称}}") is None
        assert REFERENCE_MARK_PATTERN.search("{{ref:target:|名称}}") is None
        # 名称段 [^}]* 不含 }，遇到 }b} 无法满足结尾的 }}，整条不匹配
        assert extract_reference_marks("{{ref:target:t1|a}b}}") == []

    def test_extract_mark_with_trailing_text(self):
        """标记后紧跟普通文本：raw 只取标记本身"""
        marks = extract_reference_marks("前缀{{ref:var:v1|变量名}}后缀")
        assert marks == [{"kind": "var", "key": "v1", "name": "变量名",
                          "raw": "{{ref:var:v1|变量名}}"}]


# ---------------- resolve_reference_marks ----------------

class TestResolveReferenceMarks:

    def test_resolve_target_by_id(self):
        """按 id 精确命中：注入【显示名】\\n内容"""
        result = resolve_reference_marks(
            "评测对象：{{ref:target:t-001|模型输出}}", _make_ctx()
        )
        assert result == "评测对象：【模型输出】\n这是模型输出内容"

    def test_resolve_var_by_key(self):
        """按 key 精确命中变量；布尔值按 _safe_string 风格转小写文本"""
        result = resolve_reference_marks(
            "{{ref:var:ref_answer|参考答案}} 与 {{ref:var:is_valid|是否有效}}", _make_ctx()
        )
        assert result == "【参考答案】\n这是参考答案 与 【是否有效】\ntrue"

    def test_resolve_target_fallback_by_name(self):
        """id 未命中按显示名兜底：id 写错但名称对得上仍可解析"""
        ctx = _make_ctx()
        result = resolve_reference_marks("{{ref:target:wrong-id|模型输出}}", ctx)
        assert result == "【模型输出】\n这是模型输出内容"

    def test_resolve_id_hit_prefers_display_name_of_mark(self):
        """id 精确命中且标记写了名称：注入显示名优先用标记内声明的名称（别名）"""
        ctx = _make_ctx()
        result = resolve_reference_marks("{{ref:target:t-001|别名}}", ctx)
        assert result == "【别名】\n这是模型输出内容"

    def test_resolve_id_hit_without_name_uses_entry_name(self):
        """id 精确命中但标记未写名称：回退用命中条目的显示名"""
        ctx = _make_ctx()
        result = resolve_reference_marks("{{ref:target:t-001}}", ctx)
        assert result == "【模型输出】\n这是模型输出内容"

    def test_resolve_var_fallback_by_name_as_key(self):
        """var 未命中 key 时把变量名当 key 再查一次（vars 上下文以 key 存值）"""
        ctx = _make_ctx()
        result = resolve_reference_marks("{{ref:var:错的key|ref_answer}}", ctx)
        assert result == "【ref_answer】\n这是参考答案"

    def test_resolve_all_miss_replaced_with_empty(self, caplog):
        """全未命中：替换为空串、不抛错，并记 warning 日志"""
        ctx = _make_ctx()
        text = "前缀 {{ref:target:nope|不存在}} 中间 {{ref:var:also_nope|不存在变量}} 后缀"
        with caplog.at_level(logging.WARNING, logger="app.services.reference_mark_resolver"):
            result = resolve_reference_marks(text, ctx)
        assert result == "前缀  中间  后缀"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert "{{ref:target:nope|不存在}}" in warnings[0].getMessage()

    def test_resolve_empty_ctx_no_error(self):
        """上下文整体缺省/为空：不抛错，未命中的标记置空"""
        result = resolve_reference_marks("{{ref:target:t1|名称}}", None)
        assert result == ""
        result2 = resolve_reference_marks("{{ref:var:v1|名称}}", {})
        assert result2 == ""

    def test_resolve_no_marks_unchanged(self):
        """无标记文本原样返回"""
        assert resolve_reference_marks("普通文本", _make_ctx()) == "普通文本"

    def test_resolve_non_string_passthrough(self):
        """None/非字符串输入原样返回"""
        assert resolve_reference_marks(None, _make_ctx()) is None
        assert resolve_reference_marks(123, _make_ctx()) == 123

    def test_resolve_multiple_marks_in_one_text(self):
        """同一段文本多个标记全部解析，未解析部分保留"""
        ctx = _make_ctx()
        text = "目标={{ref:target:t-001|模型输出}}；答案={{ref:var:ref_answer|参考答案}}；失效={{ref:target:x|不存在}}"
        result = resolve_reference_marks(text, ctx)
        assert result == (
            "目标=【模型输出】\n这是模型输出内容；"
            "答案=【参考答案】\n这是参考答案；"
            "失效="
        )


# ---------------- resolve_task_ref_overrides ----------------

def _make_task():
    """构造带标记的任务对象（模拟 EvaluationTask 的两个字段）"""
    return SimpleNamespace(
        score_details="全局标准，参考 {{ref:target:t-001|模型输出}}",
        evaluation_metrics=[
            {
                "name": "准确性",
                "description": "对照 {{ref:target:t-001|模型输出}} 判定一致性",
                "weight": 0.6,
                "score_criteria": {
                    "high": {"min": 8, "max": 10, "desc": "与 {{ref:var:ref_answer|参考答案}} 一致"},
                    "low": {"min": 0, "max": 3, "desc": "无 {{ref:target:nope|不存在}} 关联"},
                },
            }
        ],
    )


class TestResolveTaskRefOverrides:

    def test_overrides_resolved(self):
        """score_details、维度 description 与每个 score_criteria[等级].desc 的标记都被解析"""
        overrides = resolve_task_ref_overrides(_make_task(), _make_ctx())
        assert overrides["score_details"] == "全局标准，参考 【模型输出】\n这是模型输出内容"
        assert overrides["evaluation_metrics"][0]["description"] == (
            "对照 【模型输出】\n这是模型输出内容 判定一致性"
        )
        criteria = overrides["evaluation_metrics"][0]["score_criteria"]
        assert criteria["high"]["desc"] == "与 【参考答案】\n这是参考答案 一致"
        assert criteria["low"]["desc"] == "无  关联"  # 未命中置空

    def test_overrides_do_not_mutate_task(self):
        """只改副本：解析后任务原对象的标记原样保留"""
        task = _make_task()
        resolve_task_ref_overrides(task, _make_ctx())
        assert task.score_details == "全局标准，参考 {{ref:target:t-001|模型输出}}"
        assert task.evaluation_metrics[0]["description"] == (
            "对照 {{ref:target:t-001|模型输出}} 判定一致性"
        )
        assert task.evaluation_metrics[0]["score_criteria"]["high"]["desc"] == (
            "与 {{ref:var:ref_answer|参考答案}} 一致"
        )

    def test_overrides_no_marks_returns_copy(self):
        """无标记任务：返回内容等于原值，但为深拷贝（修改不影响原对象）"""
        task = SimpleNamespace(
            score_details="没有标记",
            evaluation_metrics=[
                {"name": "维度A", "description": "普通描述", "score_criteria": {"high": {"desc": "好"}}}
            ],
        )
        overrides = resolve_task_ref_overrides(task, _make_ctx())
        assert overrides["score_details"] == "没有标记"
        assert overrides["evaluation_metrics"][0]["description"] == "普通描述"
        assert overrides["evaluation_metrics"] == task.evaluation_metrics
        assert overrides["evaluation_metrics"] is not task.evaluation_metrics
        overrides["evaluation_metrics"][0]["score_criteria"]["high"]["desc"] = "改了"
        assert task.evaluation_metrics[0]["score_criteria"]["high"]["desc"] == "好"

    def test_overrides_none_fields(self):
        """任务字段为 None：不抛错，原样返回"""
        task = SimpleNamespace(score_details=None, evaluation_metrics=None)
        overrides = resolve_task_ref_overrides(task, _make_ctx())
        assert overrides["score_details"] is None
        assert overrides["evaluation_metrics"] is None

    def test_overrides_supports_dict_task(self):
        """任务以 dict 形式传入时同样支持（键访问）"""
        task = {
            "score_details": "{{ref:var:ref_answer|参考答案}}",
            "evaluation_metrics": [],
        }
        overrides = resolve_task_ref_overrides(task, _make_ctx())
        assert overrides["score_details"] == "【参考答案】\n这是参考答案"
        assert overrides["evaluation_metrics"] == []
