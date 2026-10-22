"""轨迹匹配器单元测试（M2 QE2-2，对象 app/services/trajectory_matcher.py）

覆盖（方案 §M2 2.5「matcher 四模式矩阵」）：
1. 四模式语义矩阵：strict（顺序+全集+无多余）/ unordered（计数，顺序无关）/
   subset（按序子序列，允许多余）/ superset（实际 ⊆ 期望，漏掉不算失败）；
2. expected 条目语义：tool 精确匹配、args_contains 全包含、min_calls 计数；
3. forbidden：任何模式命中即失败、args_contains 细粒度禁用；
4. extract_tool_calls：SessionRecord/裸 messages/markdown 字符串（不可提取）；
5. run_trajectory_match：未配置返回 None、markdown 会话 → error；
6. 聚合与门禁：trajectory_match_rate 接线（可用/不可用分支）；
7. validate_trajectory_shape 配置校验矩阵。
"""
import pytest

from app.services.gate_service import evaluate_gate
from app.services.result_aggregator import ResultAggregator
from app.services.trajectory_matcher import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    TRAJECTORY_MODES,
    extract_tool_calls,
    match_trajectory,
    run_trajectory_match,
    validate_trajectory_shape,
)

pytestmark = pytest.mark.unit


# ---------------- 测试工具 ----------------

def _calls(*specs):
    """构造工具调用序列：("grep", {"pattern": "x"}) 形态"""
    return [{"tool": tool, "args": args, "timestamp": None} for tool, args in specs]


GREP = ("grep", {"pattern": "order_id"})
READ = ("Read", {"file_path": "/tmp/a.py"})
BASH = ("Bash", {"command": "ls -la"})
RM = ("Bash", {"command": "rm -rf /tmp/x"})


def _config(mode, expected=None, forbidden=None):
    cfg = {"mode": mode}
    if expected is not None:
        cfg["expected"] = expected
    if forbidden is not None:
        cfg["forbidden"] = forbidden
    return cfg


# ==================== 1. 四模式语义矩阵 ====================

class TestModeMatrix:
    """同一组序列 × 四模式的期望判定矩阵（方案 §M2 2.5 四模式矩阵）

    期望条目：grep + Bash 各一次（Read 始终不在期望集合内）。
    序列 A：grep → Read → Bash（顺序正确、含未期望的 Read）
    序列 B：Read → grep → Bash（乱序）
    序列 C：grep → Read（缺 Bash）
    序列 D：grep → Read → Bash → Bash（多余一次 Bash）
    序列 E：grep → Bash（全在期望集合内、顺序正确）
    序列 F：Bash → grep（全在期望集合内、乱序）
    """

    SEQ_A = _calls(GREP, READ, BASH)     # 顺序对但含未期望 Read
    SEQ_B = _calls(READ, GREP, BASH)     # 乱序
    SEQ_C = _calls(GREP, READ)           # 漏 Bash
    SEQ_D = _calls(GREP, READ, BASH, BASH)  # 多余 Bash
    SEQ_E = _calls(GREP, BASH)           # 期望集合内的精确序列
    SEQ_F = _calls(BASH, GREP)           # 期望集合内但乱序

    EXPECTED = [{"tool": "grep"}, {"tool": "Bash"}]

    # (模式, 序列, 期望 status)
    # - strict：顺序 + 全集 + 无多余（Read 未期望 → A/B/D 失败；F 乱序失败）
    # - unordered：计数语义（多一个 Read 不判失败，只要期望都凑够）
    # - subset：按序子序列（允许多余调用，顺序不可逆）
    # - superset：实际 ⊆ 期望（每条调用都要被期望覆盖；漏掉期望不算失败）
    MATRIX = [
        ("strict",   SEQ_A, STATUS_FAIL),   # Read 未期望
        ("strict",   SEQ_E, STATUS_PASS),
        ("strict",   SEQ_F, STATUS_FAIL),   # 乱序
        ("strict",   SEQ_C, STATUS_FAIL),   # 漏 Bash
        ("strict",   SEQ_D, STATUS_FAIL),   # 多余调用
        ("unordered", SEQ_A, STATUS_PASS),
        ("unordered", SEQ_B, STATUS_PASS),  # 顺序无关
        ("unordered", SEQ_C, STATUS_FAIL),  # 计数不足
        ("unordered", SEQ_D, STATUS_PASS),  # 多余不判失败
        ("unordered", SEQ_E, STATUS_PASS),
        ("subset",   SEQ_A, STATUS_PASS),   # 子序列允许中间夹 Read
        ("subset",   SEQ_B, STATUS_PASS),   # grep→Bash 仍按序出现（Read 在前不碍）
        ("subset",   SEQ_C, STATUS_FAIL),   # 漏 Bash
        ("subset",   SEQ_D, STATUS_PASS),   # 多余不判失败
        ("subset",   SEQ_E, STATUS_PASS),
        ("subset",   SEQ_F, STATUS_FAIL),   # 逆序（Bash 先于 grep）不构成子序列
        ("superset", SEQ_E, STATUS_PASS),   # 全覆盖且无未期望调用
        ("superset", SEQ_F, STATUS_PASS),   # 顺序无关、全覆盖
        ("superset", SEQ_C, STATUS_FAIL),   # Read 未被期望覆盖
        ("superset", _calls(GREP), STATUS_PASS),  # 漏掉 Bash 不算失败
        ("superset", SEQ_D, STATUS_FAIL),   # Read 未被期望覆盖
    ]

    @pytest.mark.parametrize("mode,seq,expected_status", MATRIX)
    def test_matrix(self, mode, seq, expected_status):
        result = match_trajectory(seq, _config(mode, self.EXPECTED))
        assert result["status"] == expected_status, (
            f"mode={mode} seq={[c['tool'] for c in seq]} → {result['status']}")

    def test_superset_unexpected_detail(self):
        """superset：未被期望覆盖的调用记 unexpected（判失败维度）"""
        seq = _calls(GREP, READ)
        result = match_trajectory(seq, _config("superset", self.EXPECTED))
        assert result["status"] == STATUS_FAIL
        assert result["unexpected"][0]["tool"] == "Read"
        assert result["missing"] == []  # superset 模式 missing 恒空

    def test_strict_positional_details(self):
        """strict 错位：逐位对齐失败——全部调用记 unexpected、全部期望记 missing"""
        result = match_trajectory(self.SEQ_B, _config("strict", self.EXPECTED))
        assert [(u["index"], u["tool"]) for u in result["unexpected"]] == [
            (0, "Read"), (1, "grep"), (2, "Bash")]
        assert [m["tool"] for m in result["missing"]] == ["grep", "Bash"]
        assert result["matched"] == []

    def test_output_contract_keys(self):
        """方案 §M2 2.2 输出契约：{matched, missing[], unexpected[], forbidden_hits[]}"""
        result = match_trajectory(self.SEQ_A, _config("subset", self.EXPECTED))
        for key in ("matched", "missing", "unexpected", "forbidden_hits"):
            assert isinstance(result[key], list), key
        assert result["matched_count"] == 2
        assert result["total_calls"] == 3
        assert [t["tool"] for t in result["matched"]] == ["grep", "Bash"]


# ==================== 2. expected 条目语义 ====================

class TestExpectedEntries:
    def test_args_contains_all_must_match(self):
        calls = _calls(("Bash", {"command": "git push origin main"}))
        entry = {"tool": "Bash", "args_contains": ["git push", "origin"]}
        assert match_trajectory(calls, _config("unordered", [entry]))["status"] == STATUS_PASS
        miss = {"tool": "Bash", "args_contains": ["git push", "svn"]}
        result = match_trajectory(calls, _config("unordered", [miss]))
        assert result["status"] == STATUS_FAIL
        assert result["missing"][0]["matched_count"] == 0

    def test_min_calls_counting(self):
        calls = _calls(GREP, GREP, READ)
        ok = {"tool": "grep", "min_calls": 2}
        assert match_trajectory(calls, _config("unordered", [ok]))["status"] == STATUS_PASS
        strict_more = {"tool": "grep", "min_calls": 3}
        result = match_trajectory(calls, _config("unordered", [strict_more]))
        assert result["status"] == STATUS_FAIL
        assert result["missing"][0]["matched_count"] == 2

    def test_min_calls_strict_expands_sequence(self):
        """strict 下 min_calls=k 展开为 k 个连续期望位"""
        calls = _calls(GREP, GREP, READ)
        ok = [{"tool": "grep", "min_calls": 2}, {"tool": "Read"}]
        assert match_trajectory(calls, _config("strict", ok))["status"] == STATUS_PASS
        wrong_order = _calls(GREP, READ, GREP)
        result = match_trajectory(wrong_order, _config("strict", ok))
        assert result["status"] == STATUS_FAIL

    def test_subset_subsequence_with_gap(self):
        """subset：中间允许插入未期望调用（子序列语义），但顺序不可逆"""
        seq = _calls(GREP, READ, BASH)
        assert match_trajectory(seq, _config("subset", self_subset := [
            {"tool": "grep"}, {"tool": "Bash"}]))["status"] == STATUS_PASS
        reversed_seq = _calls(BASH, READ, GREP)
        result = match_trajectory(reversed_seq, _config("subset", [
            {"tool": "grep"}, {"tool": "Bash"}]))
        assert result["status"] == STATUS_FAIL

    def test_tool_exact_match(self):
        """tool 精确名匹配（grep 不匹配 Grep/grep -i）"""
        calls = _calls(("Grep", {}))
        result = match_trajectory(calls, _config("unordered", [{"tool": "grep"}]))
        assert result["status"] == STATUS_FAIL


# ==================== 3. forbidden ====================

class TestForbidden:
    def test_forbidden_hit_fails_in_all_modes(self):
        calls = _calls(GREP, RM)
        for mode in TRAJECTORY_MODES:
            result = match_trajectory(
                calls, _config(mode, [{"tool": "grep"}], [{"tool": "Bash", "args_contains": ["rm"]}]))
            assert result["status"] == STATUS_FAIL, mode
            assert result["forbidden_hits"][0]["tool"] == "Bash"

    def test_forbidden_args_scoping(self):
        """forbidden 支持参数细粒度：只禁含 sudo 的 Bash，不禁普通 Bash"""
        calls = _calls(BASH)  # ls -la，不含 sudo
        result = match_trajectory(
            calls, _config("unordered", [{"tool": "Bash"}],
                           [{"tool": "Bash", "args_contains": ["sudo"]}]))
        assert result["status"] == STATUS_PASS
        assert result["forbidden_hits"] == []

    def test_forbidden_tool_only(self):
        calls = _calls(("rm", {"path": "/tmp/x"}))
        result = match_trajectory(calls, _config("unordered", [], [{"tool": "rm"}]))
        assert result["status"] == STATUS_FAIL
        assert result["forbidden_hits"][0]["index"] == 0


# ==================== 4. extract_tool_calls ====================

class TestExtract:
    def _record(self, blocks_by_role):
        messages = [
            {"role": role, "content": blocks, "timestamp": "2026-10-23T10:00:00"}
            for role, blocks in blocks_by_role
        ]
        return {"session_id": "s1", "source": "claude_code", "messages": messages,
                "stats": {}}

    def test_extract_from_session_record(self):
        record = self._record([
            ("user", [{"type": "text", "text": "帮我查"}]),
            ("assistant", [
                {"type": "text", "text": "好的"},
                {"type": "tool_use", "name": "grep", "input": {"pattern": "x"}},
            ]),
            ("user", [{"type": "tool_result", "content": "hit"}]),
            ("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
        ])
        calls, error = extract_tool_calls(record)
        assert error is None
        assert [c["tool"] for c in calls] == ["grep", "Bash"]
        assert calls[0]["args"] == {"pattern": "x"}

    def test_extract_from_bare_messages_list(self):
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {}}]},
        ]
        calls, error = extract_tool_calls(messages)
        assert error is None
        assert [c["tool"] for c in calls] == ["Read"]

    def test_markdown_string_not_extractable(self):
        calls, error = extract_tool_calls("# 会话\n用户：你好")
        assert calls == []
        assert "渲染文本" in error

    def test_empty_record_no_calls(self):
        calls, error = extract_tool_calls({"messages": []})
        assert error is None
        assert calls == []

    def test_bad_structure_is_error(self):
        calls, error = extract_tool_calls({"foo": 1})
        assert calls == [] and error


# ==================== 5. run_trajectory_match（执行入口） ====================

class TestRunTrajectoryMatch:
    def _task(self, trajectory_config, evaluation_type="agent"):
        return type("Task", (), {
            "id": "task_traj_x",
            "evaluation_type": evaluation_type,
            "trajectory_config": trajectory_config,
        })()

    def test_no_config_returns_none(self):
        assert run_trajectory_match(self._task(None), {}) is None
        assert run_trajectory_match(self._task({}), {}) is None

    def test_config_with_session(self):
        record = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "grep", "input": {"pattern": "x"}}]},
        ]}
        payload = {"_dataset_item": {"id": "d1", "data": {"session_messages": record}}}
        result = run_trajectory_match(
            self._task({"mode": "unordered", "expected": [{"tool": "grep"}]}), payload)
        assert result["status"] == STATUS_PASS
        assert result["total_calls"] == 1

    def test_markdown_session_yields_error(self):
        payload = {"_dataset_item": {"id": "d1", "data": {"session_messages": "# 渲染文本"}}}
        result = run_trajectory_match(
            self._task({"mode": "subset", "expected": [{"tool": "grep"}]}), payload)
        assert result["status"] == STATUS_ERROR
        assert "渲染文本" in result["error"]

    def test_invalid_mode_yields_error(self):
        record = {"messages": []}
        payload = {"_dataset_item": {"id": "d1", "data": {"session_messages": record}}}
        result = run_trajectory_match(
            self._task({"mode": "fuzzy", "expected": []}), payload)
        assert result["status"] == STATUS_ERROR
        assert "非法匹配模式" in result["error"]


# ==================== 6. 聚合与门禁接线 ====================

class TestAggregationAndGate:
    def test_aggregate_trajectory(self):
        stats = ResultAggregator._aggregate_trajectory([
            {"success": True, "trajectory_match": {"status": "pass"}},
            {"success": True, "trajectory_match": {"status": "fail"}},
            {"success": True, "trajectory_match": {"status": "error"}},
            {"success": False, "error": "模型调用失败"},  # 未到匹配步骤，不入分母
        ])
        assert stats == {"total": 3, "passed": 1, "failed": 1, "errored": 1,
                         "pass_rate": round(1 / 3, 4)}

    def test_aggregate_without_match_returns_none(self):
        assert ResultAggregator._aggregate_trajectory(
            [{"success": True}]) is None
        assert ResultAggregator._aggregate_trajectory([]) is None

    def test_aggregate_writes_match_rate(self):
        task = type("Task", (), {"max_score": 10, "evaluation_metrics": []})()
        rows = [
            {"success": True, "average_score": 9,
             "trajectory_match": {"status": "pass"}},
            {"success": True, "average_score": 8,
             "trajectory_match": {"status": "fail"}},
        ]
        aggregated = ResultAggregator(task).aggregate(rows)
        assert aggregated["trajectory_match_rate"] == 0.5
        assert aggregated["trajectory"]["total"] == 2
        assert aggregated["items"][0]["trajectory_match"]["status"] == "pass"

    def test_aggregate_legacy_rows_unchanged(self):
        task = type("Task", (), {"max_score": 10, "evaluation_metrics": []})()
        aggregated = ResultAggregator(task).aggregate([{"success": True, "average_score": 9}])
        assert "trajectory" not in aggregated
        assert "trajectory_match_rate" not in aggregated

    def test_gate_uses_trajectory_match_rate(self):
        cfg = {"enabled": True, "thresholds": [
            {"metric": "trajectory_match_rate", "op": ">=", "value": 1.0}]}
        assert evaluate_gate(cfg, {"trajectory_match_rate": 1.0})["verdict"] == "passed"
        assert evaluate_gate(cfg, {"trajectory_match_rate": 0.5})["verdict"] == "failed"
        # 未配置轨迹断言 → 聚合无该键 → metric 不可用 → error
        result = evaluate_gate(cfg, {"average_score": 9.0})
        assert result["verdict"] == "error"
        assert "不可用" in result["checks"][0]["note"]


# ==================== 7. 配置形态校验 ====================

class TestValidateShape:
    def test_valid_config_passes(self):
        validate_trajectory_shape({
            "mode": "strict",
            "expected": [{"tool": "grep", "args_contains": ["pattern"], "min_calls": 2}],
            "forbidden": [{"tool": "rm"}],
        })
        validate_trajectory_shape(None)

    @pytest.mark.parametrize("config,needle", [
        ({"mode": "fuzzy"}, "mode"),
        ({"expected": [{"tool": ""}]}, "tool"),
        ({"expected": [{"tool": "x", "args_contains": "not-list"}]}, "args_contains"),
        ({"expected": [{"tool": "x", "args_contains": [""]}]}, "args_contains"),
        ({"expected": [{"tool": "x", "min_calls": 0}]}, "min_calls"),
        ({"expected": [{"tool": "x", "min_calls": "2"}]}, "min_calls"),
        ({"forbidden": "not-list"}, "数组"),
        ("not-a-dict", "对象"),
    ])
    def test_invalid_config_rejected(self, config, needle):
        with pytest.raises(ValueError, match=needle):
            validate_trajectory_shape(config)
