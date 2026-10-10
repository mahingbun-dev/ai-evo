"""评测目标 script_output 来源（JudgeVariableResolver）单元测试

覆盖契约第「二」节：EvaluationTarget.source=script_output 的行内容解析——
- 指定 output_key：取 script_outputs[output_var 或脚本名] 的对应键
- output_key 缺省/取不到：整体 JSON 序列化为字符串
- 空输出置空：script_outputs 无该键（脚本失败 on_fail=continue）或输出为
  None 时返回空串，走既有空值校验逻辑
- output_var 优先于脚本名定位输出（与 run_pre_scripts 收集口径一致）
- resolve_targets 集成：is_empty 判定；不传 script_outputs 的旧调用不受影响
"""
import json

import pytest

from app.services.judge_variable_resolver import EvaluationTarget, JudgeVariableResolver


def _resolver(script_outputs=None):
    """构造带脚本输出上下文的解析器"""
    return JudgeVariableResolver(
        task_config={},
        dataset_item={},
        model_result={},
        chain_results=[],
        script_outputs=script_outputs,
    )


def _script_target(source_config, required=True):
    """构造 script_output 来源的评测目标"""
    return EvaluationTarget(
        id="t-script",
        name="script_target",
        source="script_output",
        source_config=source_config,
        display_name="脚本产物",
        required=required,
    )


# ---------------- 指定键 ----------------

class TestScriptOutputWithKey:

    def test_output_key_hit(self):
        """指定 output_key：取输出对象对应键的值"""
        resolver = _resolver({"my_var": {"accuracy": 0.92, "report": "好的"}})
        target = _script_target({
            "script_id": "script-1", "output_key": "accuracy", "output_var": "my_var",
        })
        assert resolver.resolve_targets([target])["t-script"]["content"] == "0.92"

    def test_output_key_locates_by_script_name_when_no_output_var(self):
        """无 output_var：按脚本名定位输出键（与 run_pre_scripts 缺省口径一致）"""
        resolver = _resolver({"脚本A": {"accuracy": 1}})
        target = _script_target({"script_id": "脚本A", "output_key": "accuracy"})
        assert resolver._resolve_single_target(target) == "1"

    def test_output_key_bool_value(self):
        """键值为布尔：按 _safe_string 风格转小写文本"""
        resolver = _resolver({"v": {"passed": True}})
        target = _script_target({"script_id": "s", "output_key": "passed", "output_var": "v"})
        assert resolver._resolve_single_target(target) == "true"

    def test_output_key_structured_value_json_text(self):
        """键值为 dict/list：序列化为 JSON 文本"""
        resolver = _resolver({"v": {"items": ["a", "b"]}})
        target = _script_target({"script_id": "s", "output_key": "items", "output_var": "v"})
        assert resolver._resolve_single_target(target) == '["a", "b"]'

    def test_output_key_miss_fallback_whole_json(self):
        """output_key 取不到（键不存在）：整体 JSON 序列化兜底"""
        output = {"accuracy": 0.9}
        resolver = _resolver({"v": output})
        target = _script_target({"script_id": "s", "output_key": "nope", "output_var": "v"})
        assert resolver._resolve_single_target(target) == json.dumps(output, ensure_ascii=False)

    def test_output_key_on_non_dict_output_fallback(self):
        """输出非对象（字符串）：output_key 取不到，整体 JSON 序列化兜底"""
        resolver = _resolver({"v": "纯文本输出"})
        target = _script_target({"script_id": "s", "output_key": "any", "output_var": "v"})
        assert resolver._resolve_single_target(target) == '"纯文本输出"'

    def test_output_var_precedes_script_name(self):
        """同时配置 script_id 与 output_var：优先按 output_var 定位"""
        resolver = _resolver({
            "my_var": {"accuracy": 1},
            "script-1": {"accuracy": 0},
        })
        target = _script_target({
            "script_id": "script-1", "output_var": "my_var", "output_key": "accuracy",
        })
        assert resolver._resolve_single_target(target) == "1"


# ---------------- 缺省整体 / 空输出 ----------------

class TestScriptOutputWholeAndEmpty:

    def test_no_output_key_whole_json(self):
        """output_key 缺省：整体输出 JSON 序列化为字符串（中文不转义）"""
        output = {"accuracy": 0.85, "结论": "良好"}
        resolver = _resolver({"v": output})
        target = _script_target({"script_id": "s", "output_var": "v"})
        assert resolver._resolve_single_target(target) == json.dumps(output, ensure_ascii=False)

    def test_missing_key_empty_content(self):
        """空输出置空：脚本失败（on_fail=continue）时 script_outputs 无该键"""
        resolver = _resolver({})  # 脚本未产出任何输出
        target = _script_target({"script_id": "s", "output_var": "v", "output_key": "accuracy"})
        result = resolver.resolve_targets([target])
        assert result["t-script"]["content"] == ""
        assert result["t-script"]["is_empty"] is True

    def test_none_output_empty_content(self):
        """输出显式为 None：内容置空"""
        resolver = _resolver({"v": None})
        target = _script_target({"script_id": "s", "output_var": "v"})
        assert resolver._resolve_single_target(target) == ""

    def test_missing_script_id_empty_content(self):
        """source_config 缺 script_id/output_var：无法定位，置空不抛错"""
        resolver = _resolver({"s": {"a": 1}})
        target = _script_target({})
        assert resolver._resolve_single_target(target) == ""


# ---------------- 兼容性 ----------------

class TestScriptOutputCompat:

    def test_old_call_without_script_outputs(self):
        """旧调用不传 script_outputs：构造正常，script_output 目标解析为空"""
        resolver = JudgeVariableResolver(
            task_config={},
            dataset_item={"question": "问题"},
            model_result={"content": "模型输出"},
            chain_results=[],
        )
        assert resolver.script_outputs == {}
        target = _script_target({"script_id": "s", "output_key": "a"})
        assert resolver._resolve_single_target(target) == ""

    def test_other_sources_unaffected(self):
        """既有来源（model_output/dataset_field/manual_input）解析不受影响"""
        resolver = _resolver({"v": {"a": 1}})
        targets = [
            EvaluationTarget(id="t1", name="m", source="model_output",
                             display_name="模型输出", source_config={}),
            EvaluationTarget(id="t2", name="d", source="dataset_field",
                             display_name="问题", source_config={"field": "question"}),
            EvaluationTarget(id="t3", name="x", source="manual_input",
                             display_name="手填", source_config={"value": "手工值"}),
        ]
        results = resolver.resolve_targets([targets[0]])
        assert results["t1"]["content"] == ""

        resolver2 = _resolver()
        resolver2.dataset_item = {"question": "问题内容"}
        assert resolver2.resolve_targets([targets[1]])["t2"]["content"] == "问题内容"
        assert resolver2.resolve_targets([targets[2]])["t3"]["content"] == "手工值"

    def test_resolve_targets_mixed_sources(self):
        """混合来源目标列表：script_output 与 model_output 各自正确解析"""
        resolver = _resolver({"v": {"score": 5}})
        targets = [
            EvaluationTarget(id="ta", name="m", source="model_output",
                             display_name="模型输出", source_config={}),
            _script_target({"script_id": "s", "output_var": "v", "output_key": "score"}),
        ]
        results = resolver.resolve_targets(targets)
        assert results["ta"]["content"] == ""
        assert results["ta"]["is_empty"] is True
        assert results["t-script"]["content"] == "5"
        assert results["t-script"]["is_empty"] is False
        assert results["t-script"]["source"] == "script_output"
