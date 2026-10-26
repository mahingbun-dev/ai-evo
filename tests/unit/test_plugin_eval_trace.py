"""插件评测 · 会话 Trace 口径单测（history/真实数据评测）

覆盖：
- 线程发现（裸 jsonl / 嵌套 claude-thread.zip / 都没有 / 同类取最大）
- 市场历史规则候选发现
- 线程内被测插件识别（两种调用形态、多候选取最多、无命中）
- trace 子进程 argv/env/失败路径（缺 real_data、超时、非零退出、未配 CC）
- 行结果映射（只出真实数据分、不产维度分/徽章、锚点齐备、明细裁剪）
- 未测行的各种不可评原因（不计失败）
- 聚合/导出对 trace 口径的补充字段与非 trace 口径的不变性
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import plugin_eval_trace as trace
from app.services import plugin_eval_market_runner as runner
from app.services.plugin_evaluator import PluginEvaluator

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

THREAD_LINES = [
    {"type": "user", "sessionId": "sess-1", "message": {"role": "user", "content": [
        {"type": "text", "text": "<command-name>/anc-tms:run</command-name>"}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "echo hi"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "hi"}]}},
]


def _write_thread(path: Path, lines=None) -> Path:
    rows = lines if lines is not None else THREAD_LINES
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def _history_payload(score=83.33, status="partial", plugin="anc-tms") -> dict:
    """plugin-eval 合并口径输出的最小真实结构

    一次评测同时含设计质量（composite/layers/skill_results）与真实数据
    （real_data/history/history_generation）两部分，两套分数相互独立。
    """
    return {
        "plugin_path": "/market/project/anc-tms",
        "timestamp": "2026-10-16T01:00:00Z",
        "total_duration_ms": 79000,
        "model_usage": {"deepseek/deepseek-flash": 1200},
        "config": {"depth": "standard", "history_phase": "all", "history_scope": "key",
                   "history_only": False},
        "composite": {
            "score": 34.233944249087266,
            "badge": "no_badge",
            "confidence_label": "Assessed",
            "anti_pattern_penalty": 0.5,
            "dimensions": [
                {"name": "triggering_accuracy", "score": 0.7658035714285714,
                 "weight": 0.25, "measured": True},
                {"name": "robustness", "score": 0.0, "weight": 0.05, "measured": False},
            ],
        },
        "layers": [{
            "layer": "static", "score": 0.5532955040960864,
            "sub_scores": {"frontmatter_quality": 0.81},
            "anti_patterns": [{"flag": "DEAD_CROSS_REF", "severity": 0.5, "description": "死链"}],
        }],
        "skill_results": [{
            "plugin_path": "/market/project/anc-tms/skills/run",
            "composite": {
                "score": 53.3, "badge": "no_badge", "confidence_label": "Assessed",
                "dimensions": [{"name": "triggering_accuracy", "score": 0.5, "measured": True},
                               {"name": "robustness", "score": 0.0, "measured": False}],
            },
            "layers": [{"layer": "static", "score": 0.53},
                       {"layer": "judge", "score": 0.9, "anti_patterns": [{"flag": "X"}]}],
        }],
        "real_data": {
            "metric": "real_data_reliability",
            "status": status,
            "score": score,
            "coverage": 75.0,
            "counts": {"passed": 5, "failed": 1, "insufficient": 2, "not_applicable": 0,
                       "measured": 6, "applicable": 8},
            "extraction_complete": False,
            "limitations": ["已禁用模型，仅执行可确定的结构化检查"],
            "phases": {"historical_execution_reliability": {"status": status}},
            "behavior_coverage": {"scope": "key_steps", "total": 91, "covered": 10,
                                  "rate": 10.99, "uncovered": [{"id": "L26B0"}]},
        },
        "history": {
            "metric": "historical_execution_reliability",
            "plugin": plugin,
            "thread_id": "21a46d7d",
            "status": status,
            "score": score,
            "coverage": 75.0,
            "counts": {"passed": 5, "failed": 1, "insufficient": 2},
            # 方向分项：plugin-eval 每个方向给 score/coverage/counts 三件套
            "dimensions": {"procedure_adherence": {
                "score": score, "coverage": 75.0,
                "counts": {"passed": 5, "failed": 1, "insufficient": 2,
                           "not_applicable": 0, "measured": 6, "applicable": 8},
            }},
            "rule": {"file": "anc-tms.yaml", "version": "1.1.0", "sha256": "abc"},
            "source": {"thread_file": "t.jsonl", "artifact_manifest": [{"name": "a"}, {"name": "b"}]},
            "limitations": ["关键步骤模式"],
            "model_usage": {"deepseek/deepseek-flash": 1200},
            "behavior_coverage": {"scope": "key_steps", "total": 91, "covered": 10, "uncovered": []},
            "checks": [{
                "id": "c1", "rule_id": "write-calls", "dimension": "procedure_adherence",
                "subject": "L122B0", "status": "passed", "reason": "写后回查",
                "method": "deterministic", "expectation": "写后回查", "actual": "x" * 2000,
                "evidence_ids": [f"e{i}" for i in range(40)],
                "citations": [{"evidence_id": "e1", "quote": "q"}] * 20,
            }],
            "key_steps": [{"id": "K001", "step": "investigate", "name": "检索与定位",
                           "signature": "investigate", "task_epoch": 1, "line_start": 17,
                           "line_end": 777, "attempts": 48, "achieved": True, "check_count": 2,
                           "evidence_ids": [f"e{i}" for i in range(50)]}],
            "evidence": [{"id": "e1", "text": "huge"}],
        },
        "history_generation": None,
    }


def _fake_evaluator(package_root="/market/public/plugin-eval") -> dict:
    return {
        "name": "plugin-eval", "version": "0.5.1", "market_id": 1,
        "market_name": "ANC 插件市场", "source": "./public/plugin-eval",
        "commit": "b112b5608587", "stale": False, "refresh_error": "",
        "package_root": package_root,
    }


def _patch_cc(monkeypatch, configured=True):
    from app.services.claude_code_config_service import ClaudeCodeConfigService

    monkeypatch.setattr(
        ClaudeCodeConfigService, "get_active_config",
        classmethod(lambda cls: ({"id": "cc"} if configured else None)),
    )
    monkeypatch.setattr(
        ClaudeCodeConfigService, "get_sdk_env",
        classmethod(lambda cls: {"ANTHROPIC_BASE_URL": "http://gw"}),
    )


def _patch_run(monkeypatch, returncode=0, stdout="{}", stderr=""):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# 线程发现
# ---------------------------------------------------------------------------

class TestFindThreadFile:

    def test_bare_jsonl(self, tmp_path):
        _write_thread(tmp_path / "t.jsonl")
        src = trace.find_thread_file(str(tmp_path), str(tmp_path / "wd"))
        assert src is not None
        assert src.kind == "jsonl"
        assert src.origin == "t.jsonl"

    def test_nested_thread_zip_preferred(self, tmp_path):
        _write_thread(tmp_path / "noise.jsonl")
        package = tmp_path / "claude-thread.zip"
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr("inner/session.jsonl", "\n".join(json.dumps(r) for r in THREAD_LINES))
        src = trace.find_thread_file(str(tmp_path), str(tmp_path / "wd"))
        assert src is not None
        assert src.kind == "nested_zip"
        assert src.origin == "claude-thread.zip"
        assert Path(src.jsonl_path).name == "session.jsonl"

    def test_underscore_variant_zip(self, tmp_path):
        package = tmp_path / "claude_thread.zip"
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr("s.jsonl", json.dumps(THREAD_LINES[0]))
        src = trace.find_thread_file(str(tmp_path), str(tmp_path / "wd"))
        assert src is not None and src.kind == "nested_zip"

    def test_largest_jsonl_wins(self, tmp_path):
        small = _write_thread(tmp_path / "small.jsonl")
        big = _write_thread(
            tmp_path / "big.jsonl",
            THREAD_LINES + [THREAD_LINES[1]] * 20,
        )
        src = trace.find_thread_file(str(tmp_path), str(tmp_path / "wd"))
        assert Path(src.jsonl_path).name == "big.jsonl"
        assert big.stat().st_size > small.stat().st_size

    def test_missing_returns_none(self, tmp_path):
        (tmp_path / "row.json").write_text("{}", encoding="utf-8")
        assert trace.find_thread_file(str(tmp_path), str(tmp_path / "wd")) is None

    def test_non_directory_returns_none(self, tmp_path):
        assert trace.find_thread_file(str(tmp_path / "nope"), str(tmp_path)) is None


class TestBuildArtifactDir:
    """产物镜像：排除会话线程（线程不是产物，且会被 extractor 反复解析）"""

    def test_excludes_thread_keeps_products(self, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        (tmp_path / "test_cases.json").write_text("[]", encoding="utf-8")
        allure = tmp_path / "results" / "executions" / "EXEC-1" / "allure-data"
        allure.mkdir(parents=True)
        (allure / "a-result.json").write_text("{}", encoding="utf-8")
        src = trace.ThreadSource(jsonl_path=str(thread), kind="jsonl", origin="t.jsonl")

        mirror = trace.build_artifact_dir(str(tmp_path), str(tmp_path / "wd"), src)
        assert mirror is not None
        files = sorted(str(p.relative_to(mirror)) for p in Path(mirror).rglob("*") if p.is_file())
        assert files == [
            "results/executions/EXEC-1/allure-data/a-result.json", "test_cases.json",
        ]

    def test_excludes_nested_thread_zip(self, tmp_path):
        package = tmp_path / "claude-thread.zip"
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr("s.jsonl", json.dumps(THREAD_LINES[0]))
        (tmp_path / "case_list.json").write_text("{}", encoding="utf-8")
        src = trace.ThreadSource(
            jsonl_path=str(tmp_path / "wd" / "s.jsonl"), kind="nested_zip", origin="claude-thread.zip"
        )

        mirror = trace.build_artifact_dir(str(tmp_path), str(tmp_path / "wd"), src)
        assert mirror is not None
        files = [p.name for p in Path(mirror).rglob("*") if p.is_file()]
        assert files == ["case_list.json"]

    def test_thread_only_package_has_no_artifacts(self, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        src = trace.ThreadSource(jsonl_path=str(thread), kind="jsonl", origin="t.jsonl")
        assert trace.build_artifact_dir(str(tmp_path), str(tmp_path / "wd"), src) is None

    def test_mirror_files_readable(self, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        (tmp_path / "row.json").write_text('{"k": 1}', encoding="utf-8")
        src = trace.ThreadSource(jsonl_path=str(thread), kind="jsonl", origin="t.jsonl")
        mirror = trace.build_artifact_dir(str(tmp_path), str(tmp_path / "wd"), src)
        assert (Path(mirror) / "row.json").read_text(encoding="utf-8") == '{"k": 1}'


class TestDiscoverRulePlugins:

    def test_reads_rule_filenames(self, tmp_path):
        rules = tmp_path / "rules" / "history"
        rules.mkdir(parents=True)
        for name in ("anc-tms", "anc-wms"):
            (rules / f"{name}.yaml").write_text("plugin: x", encoding="utf-8")
        (rules / "README.md").write_text("x", encoding="utf-8")
        assert trace.discover_rule_plugins(str(tmp_path)) == ["anc-tms", "anc-wms"]

    def test_packaged_fallback(self, tmp_path):
        packaged = tmp_path / "src" / "plugin_eval" / "history_rulefiles"
        packaged.mkdir(parents=True)
        (packaged / "anc-srm.yaml").write_text("x", encoding="utf-8")
        assert trace.discover_rule_plugins(str(tmp_path)) == ["anc-srm"]

    def test_missing_returns_empty(self, tmp_path):
        assert trace.discover_rule_plugins(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# 插件识别
# ---------------------------------------------------------------------------

class TestDetectInvokedPlugin:

    CANDIDATES = ["anc-tms", "anc-wms", "anc-data"]

    def test_command_name_form(self, tmp_path):
        path = _write_thread(tmp_path / "t.jsonl")
        result = trace.detect_invoked_plugin(str(path), self.CANDIDATES)
        assert result["plugin"] == "anc-tms"
        assert result["counts"] == {"anc-tms": 1}
        assert result["session_id"] == "sess-1"
        assert result["candidates"] == sorted(self.CANDIDATES)

    def test_slash_line_form(self, tmp_path):
        lines = [{"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "/anc-wms:run https://x"}]}}]
        path = _write_thread(tmp_path / "t.jsonl", lines)
        assert trace.detect_invoked_plugin(str(path), self.CANDIDATES)["plugin"] == "anc-wms"

    def test_most_invocations_wins(self, tmp_path):
        lines = [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "/anc-wms:run"}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "/anc-data:run"}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "/anc-data:result-validate"}]}},
        ]
        path = _write_thread(tmp_path / "t.jsonl", lines)
        result = trace.detect_invoked_plugin(str(path), self.CANDIDATES)
        assert result["plugin"] == "anc-data"
        assert result["counts"] == {"anc-data": 2, "anc-wms": 1}

    def test_tie_takes_earliest(self, tmp_path):
        lines = [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "/anc-wms:run"}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "/anc-tms:run"}]}},
        ]
        path = _write_thread(tmp_path / "t.jsonl", lines)
        assert trace.detect_invoked_plugin(str(path), self.CANDIDATES)["plugin"] == "anc-wms"

    def test_non_candidate_ignored(self, tmp_path):
        """anc-runner 这类无历史规则的插件不构成候选（市场无规则即不可评）"""
        lines = [{"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "/anc-runner:run-quick 需求A"}]}}]
        path = _write_thread(tmp_path / "t.jsonl", lines)
        result = trace.detect_invoked_plugin(str(path), self.CANDIDATES)
        assert result["plugin"] == ""
        assert result["counts"] == {}

    def test_assistant_text_not_counted(self, tmp_path):
        """助手发言里的同名文字不算调用（只认 user 侧命令包装）"""
        lines = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "/anc-tms:run 计划"}]}}]
        path = _write_thread(tmp_path / "t.jsonl", lines)
        assert trace.detect_invoked_plugin(str(path), self.CANDIDATES)["plugin"] == ""

    def test_bad_lines_tolerated(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text('{"bad json\n' + json.dumps(THREAD_LINES[0], ensure_ascii=False) + "\n",
                        encoding="utf-8")
        assert trace.detect_invoked_plugin(str(path), self.CANDIDATES)["plugin"] == "anc-tms"


# ---------------------------------------------------------------------------
# 子进程运行器
# ---------------------------------------------------------------------------

class TestTraceRunner:

    def test_build_trace_argv(self, tmp_path):
        argv = runner.build_trace_argv(
            Path("/pkg"), "/market/project/anc-tms", "/t/thread.jsonl",
            ["/t/pkg"], concurrency=3,
        )
        assert argv[3:6] == ["score", "/market/project/anc-tms", "--thread"]
        assert argv[6] == "/t/thread.jsonl"
        assert argv[7:9] == ["--depth", "standard"]          # 合并口径：静态层 + LLM 评审
        assert argv[9:11] == ["--artifacts", "/t/pkg"]
        # 不带 --history-only：综合分与真实数据分在同一次评测里产出
        assert "--history-only" not in argv
        assert argv[argv.index("--history-phase") + 1] == "all"
        assert argv[argv.index("--history-scope") + 1] == "key"
        assert argv[argv.index("--output") + 1] == "json"
        assert argv[argv.index("--concurrency") + 1] == "3"
        # history-only 与 threshold 互斥，不得带 --threshold
        assert "--threshold" not in argv

    def test_no_artifacts_omits_flag(self):
        argv = runner.build_trace_argv(Path("/pkg"), "/p", "/t.jsonl", None)
        assert "--artifacts" not in argv

    def test_history_only_argv_skips_design_line(self):
        """逐行复用设计质量时只跑历史线：--history-only 必须出现"""
        argv = runner.build_trace_argv(
            Path("/pkg"), "/p", "/t.jsonl", None, history_only=True
        )
        assert "--history-only" in argv
        assert argv.count("--history-only") == 1

    def test_requires_cc_config(self, monkeypatch):
        _patch_cc(monkeypatch, configured=False)
        with pytest.raises(runner.MarketEvalError, match="Claude Code"):
            runner.run_market_plugin_trace_eval(
                "/market/project/anc-tms", "/t.jsonl", evaluator=_fake_evaluator()
            )

    def test_missing_real_data_raises(self, monkeypatch):
        _patch_cc(monkeypatch)
        _patch_run(monkeypatch, stdout=json.dumps({"composite": None}))
        with pytest.raises(runner.MarketEvalError, match="real_data"):
            runner.run_market_plugin_trace_eval(
                "/market/project/anc-tms", "/t.jsonl", evaluator=_fake_evaluator()
            )

    def test_success_returns_payload(self, monkeypatch):
        _patch_cc(monkeypatch)
        payload = _history_payload()
        calls = _patch_run(monkeypatch, stdout=json.dumps(payload))
        outcome = runner.run_market_plugin_trace_eval(
            "/market/project/anc-tms", "/t.jsonl", ["/t/pkg"], evaluator=_fake_evaluator()
        )
        assert outcome["payload"]["real_data"]["score"] == 83.33
        assert outcome["evaluator"]["commit"] == "b112b5608587"
        env = calls["kwargs"]["env"]
        assert env["PYTHONPATH"] == "/market/public/plugin-eval/src"
        assert env["ANTHROPIC_BASE_URL"] == "http://gw"

    def test_timeout_raises(self, monkeypatch):
        _patch_cc(monkeypatch)

        def fake_run(argv, **kwargs):
            raise runner.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        with pytest.raises(runner.MarketEvalError, match="执行超时"):
            runner.run_market_plugin_trace_eval(
                "/p", "/t.jsonl", evaluator=_fake_evaluator()
            )

    def test_nonzero_returncode_raises(self, monkeypatch):
        _patch_cc(monkeypatch)
        _patch_run(monkeypatch, returncode=2, stderr="没有 anc-tms 的历史抽取规则")
        with pytest.raises(runner.MarketEvalError, match="没有 anc-tms 的历史抽取规则"):
            runner.run_market_plugin_trace_eval(
                "/p", "/t.jsonl", evaluator=_fake_evaluator()
            )

    def test_offline_judgement_adds_no_llm_flag(self):
        argv = runner.build_trace_argv(
            Path("/pkg"), "/p", "/t.jsonl", None, judgement="offline"
        )
        assert "--history-no-llm" in argv
        # 离线必须全程零模型调用：静态深度同时退到 quick（标准深度会跑 LLM 评审）
        assert argv[argv.index("--depth") + 1] == "quick"
        assert "--history-only" not in argv

    def test_model_judgement_has_no_offline_flag(self):
        argv = runner.build_trace_argv(Path("/pkg"), "/p", "/t.jsonl", None, judgement="model")
        assert "--history-no-llm" not in argv

    def test_offline_judgement_does_not_require_cc(self, monkeypatch):
        _patch_cc(monkeypatch, configured=False)
        calls = _patch_run(monkeypatch, stdout=json.dumps(_history_payload()))
        outcome = runner.run_market_plugin_trace_eval(
            "/p", "/t.jsonl", evaluator=_fake_evaluator(), judgement="offline"
        )
        assert outcome["payload"]["real_data"]["score"] == 83.33
        assert "--history-no-llm" in calls["argv"]

    def test_on_tick_keeps_calling_while_running(self, monkeypatch):
        """长行必须持续回调（任务层据此刷新执行心跳，reaper 阈值 15 分钟）"""
        ticks = []
        state = {"n": 0}

        class FakeProc:
            def __init__(self, *a, **kw):
                self._out = kw.get("stdout")

            def poll(self):
                state["n"] += 1
                if state["n"] >= 3:
                    self._out.write(json.dumps(_history_payload()).encode("utf-8"))
                    return 0
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(runner, "subprocess", MagicMock(Popen=FakeProc))
        monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
        payload, _duration = runner._run_cli(
            ["plugin-eval"], {}, Path("/tmp"), 30.0, "trace",
            on_tick=lambda elapsed: ticks.append(elapsed), tick_seconds=0.0,
        )
        assert payload["real_data"]["score"] == 83.33
        assert len(ticks) >= 1

    def test_on_tick_failure_does_not_break_run(self, monkeypatch):
        """心跳回调异常不得影响评测（日志/心跳是旁路）"""
        state = {"n": 0}

        class FakeProc:
            def __init__(self, *a, **kw):
                self._out = kw.get("stdout")

            def poll(self):
                state["n"] += 1
                if state["n"] >= 2:
                    self._out.write(json.dumps(_history_payload()).encode("utf-8"))
                    return 0
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def boom(_elapsed):
            raise RuntimeError("heartbeat down")

        monkeypatch.setattr(runner, "subprocess", MagicMock(Popen=FakeProc))
        monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
        payload, _ = runner._run_cli(
            ["plugin-eval"], {}, Path("/tmp"), 30.0, "trace",
            on_tick=boom, tick_seconds=0.0,
        )
        assert payload["real_data"]["status"] == "partial"

    def test_trace_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_EVAL_TRACE_TIMEOUT_SECONDS", "42")
        assert runner._trace_timeout() == 42.0
        monkeypatch.setenv("PLUGIN_EVAL_TRACE_TIMEOUT_SECONDS", "abc")
        assert runner._trace_timeout() == runner.TRACE_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# 模型调用并发（PLUGIN_EVAL_CONCURRENCY）
# ---------------------------------------------------------------------------

class TestConcurrency:
    """并发解析：显式入参 > 环境变量 > 默认 20；一律夹到 plugin-eval 的 [1,20]"""

    def _argv_of_trace(self, monkeypatch, **kwargs):
        _patch_cc(monkeypatch)
        calls = _patch_run(monkeypatch, stdout=json.dumps(_history_payload()))
        runner.run_market_plugin_trace_eval(
            "/p", "/t.jsonl", evaluator=_fake_evaluator(), **kwargs
        )
        argv = calls["argv"]
        return argv[argv.index("--concurrency") + 1]

    def test_default_is_twenty(self, monkeypatch):
        monkeypatch.delenv(runner.CONCURRENCY_ENV, raising=False)
        assert runner.DEFAULT_CONCURRENCY == 20
        assert self._argv_of_trace(monkeypatch) == "20"

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv(runner.CONCURRENCY_ENV, "12")
        assert self._argv_of_trace(monkeypatch) == "12"

    def test_env_clamped_to_plugin_eval_max(self, monkeypatch):
        """plugin-eval 的 EvalConfig 是 le=20，传超会被 pydantic 拒掉整行失败，故夹到上限"""
        monkeypatch.setenv(runner.CONCURRENCY_ENV, "50")
        assert runner._concurrency() == 20
        assert self._argv_of_trace(monkeypatch) == "20"

    def test_env_clamped_low(self, monkeypatch):
        monkeypatch.setenv(runner.CONCURRENCY_ENV, "0")
        assert runner._concurrency() == 1

    def test_env_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(runner.CONCURRENCY_ENV, "abc")
        assert runner._concurrency() == runner.DEFAULT_CONCURRENCY

    def test_explicit_argument_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(runner.CONCURRENCY_ENV, "8")
        assert self._argv_of_trace(monkeypatch, concurrency=3) == "3"

    def test_static_depth_uses_same_resolution(self, monkeypatch):
        _patch_cc(monkeypatch)
        monkeypatch.setenv(runner.CONCURRENCY_ENV, "10")
        calls = _patch_run(monkeypatch, stdout=json.dumps({"composite": {"score": 1}}))
        runner.run_market_plugin_eval("/skill", depth="quick", evaluator=_fake_evaluator())
        argv = calls["argv"]
        assert argv[argv.index("--concurrency") + 1] == "10"


# ---------------------------------------------------------------------------
# 行结果映射与未测行
# ---------------------------------------------------------------------------

class TestEvaluateTrace:

    def _evaluator(self):
        return PluginEvaluator(depth="quick", market_id=1)

    def _patch_env(self, monkeypatch, tmp_path, package_dir=None, package_root="/pkg"):
        """打桩：评测器锚点、规则候选、线程位置、插件目录、子进程结果"""
        evaluator = _fake_evaluator(package_root=package_root)
        monkeypatch.setattr(
            "app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: evaluator
        )
        monkeypatch.setattr(
            "app.services.plugin_evaluator.discover_rule_plugins", lambda root: ["anc-tms", "anc-wms"]
        )
        monkeypatch.setattr(
            "app.services.plugin_evaluator.detect_invoked_plugin",
            lambda path, candidates: {"plugin": "anc-tms", "counts": {"anc-tms": 2},
                                      "first_line": 7, "session_id": "sess-1",
                                      "candidates": list(candidates)},
        )
        monkeypatch.setattr(
            "app.services.plugin_evaluator.resolve_plugin_dir",
            lambda market_id, name, branch=None: {"name": name, "source": f"./project/{name}",
                                     "path": f"/market/project/{name}", "version": "0.15.1",
                                     "target_type": "plugin"},
        )
        monkeypatch.setattr(
            "app.services.plugin_evaluator.find_thread_file",
            lambda pkg, wd: trace.ThreadSource(
                jsonl_path=str(package_dir or (tmp_path / "t.jsonl")),
                kind="jsonl", origin="t.jsonl",
            ),
        )

    def test_row_mapping(self, monkeypatch, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1234},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        assert row["success"] is True
        assert row["target_type"] == "plugin"
        assert row["target_name"] == "anc-tms"
        # 行分与其余来源同义（综合分）；真实数据分只在 real_data 里，两套互不并入
        assert row["average_score"] == 34.23
        assert row["badge"] == "no_badge"
        # 未测维度（measured=false）剔除，避免拉低维度均分
        assert row["dimension_scores"] == {"triggering_accuracy": 0.7658035714285714}
        assert [ap["flag"] for ap in row["anti_patterns"]] == ["DEAD_CROSS_REF"]
        assert row["error"] == ""
        assert row["real_data"]["status"] == "partial"
        assert row["real_data"]["score"] == 83.33
        assert row["real_data"]["counts"]["passed"] == 5
        assert "uncovered" not in row["real_data"]["behavior_coverage"]
        assert row["plugin_eval"]["mode"] == "trace"
        assert row["plugin_eval"]["depth"] == "standard"       # 合并口径的静态深度
        assert row["plugin_eval"]["composite"]["score"] == 34.233944249087266
        assert [l["layer"] for l in row["plugin_eval"]["layers"]] == ["static"]
        # 报告「Skill 明细」表
        assert [s["name"] for s in row["plugin_eval"]["skills"]] == ["run"]
        assert row["plugin_eval"]["skills"][0]["score"] == 53.3
        assert row["plugin_eval"]["skills"][0]["measured_dimensions"] == 1
        assert row["plugin_eval"]["skills"][0]["anti_pattern_count"] == 1
        assert row["plugin_eval"]["evaluator"]["commit"] == "b112b5608587"
        assert "package_root" not in row["plugin_eval"]["evaluator"]
        # 线程锚点与识别结果
        assert row["trace"]["thread_file"] == "t.jsonl"
        assert row["trace"]["thread_sha256"]
        assert row["trace"]["detected"]["plugin"] == "anc-tms"
        assert row["trace"]["plugin_path"] == "/market/project/anc-tms"
        assert row["trace"]["rule"]["file"] == "anc-tms.yaml"
        assert row["trace"]["history"]["source"]["artifact_count"] == 2
        assert "artifact_manifest" not in row["trace"]["history"]["source"]
        # 明细裁剪（actual 限长、证据引用限量）
        assert len(row["checks"]) == 1
        assert len(row["checks"][0]["actual"]) < 2000
        assert len(row["checks"][0]["evidence_ids"]) == 20
        # raw 为审计摘要：保留被引用证据（单条 500 字），逐条未覆盖只留分类计数
        history_raw = row["plugin_eval"]["raw"]["history"]
        assert history_raw["evidence"][0]["id"] == "e1"
        assert len(history_raw["evidence"][0]["text"]) <= 505
        assert "uncovered" not in history_raw["behavior_coverage"]
        assert row["plugin_eval"]["raw"]["raw_size_bytes"] > 0
        assert row["evaluation_mode"] == "trace"
        # 行结果可 JSON 序列化（落库要求）
        json.dumps(row, ensure_ascii=False)

    def test_design_quality_evaluated_once_per_plugin(self, monkeypatch, tmp_path):
        """设计质量线只评一次：首行跑设计+历史，后续行只跑历史并复用设计快照"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        history_only_payload = {
            **payload,
            "composite": None,
            "layers": [],
            "skill_results": [],
            "config": {**(payload.get("config") or {}), "history_only": True},
        }
        calls = []

        def fake_run(*args, **kwargs):
            history_only = bool(kwargs.get("history_only"))
            calls.append(history_only)
            return {
                "payload": history_only_payload if history_only else payload,
                "evaluator": _fake_evaluator(),
                "duration_ms": 10,
            }

        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval", fake_run
        )
        evaluator = self._evaluator()
        cache = {}
        first = evaluator.evaluate_trace(str(tmp_path), design_cache=cache)
        second = evaluator.evaluate_trace(str(tmp_path), design_cache=cache)

        assert calls == [False, True]
        assert len(cache) == 1
        assert first["design_reused"] is False
        assert second["design_reused"] is True
        # 两行设计质量一致：第二行来自缓存，而不是重新调 judge
        assert first["average_score"] == second["average_score"] == 34.23
        assert second["plugin_eval"]["design"]["reused"] is True
        # 复用时 raw 仍能离线复盘设计线
        assert second["plugin_eval"]["raw"]["composite"]["score"] == 34.23

    def test_unmeasured_phase_carries_detected_and_reason(self, monkeypatch, tmp_path):
        """生成阶段未测：detected=false + 原因随阶段块落库（页面据此显示未测而不是 0）"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        payload["history_generation"] = {
            "metric": "historical_generation_reliability",
            "plugin": "anc-tms",
            "thread_id": "21a46d7d",
            "status": "unmeasured",
            "score": None,
            "coverage": None,
            "counts": {"passed": 0, "failed": 0, "insufficient": 0,
                       "not_applicable": 0, "measured": 0, "applicable": 0},
            "dimensions": {
                "source_alignment": {"score": None, "coverage": None,
                                     "counts": {"passed": 0, "failed": 0, "insufficient": 0}},
            },
            "rule": {"file": "anc-tms.yaml", "version": "1.1.0", "sha256": "abc"},
            "source": {"phase": "generation", "phase_detected": False},
            "limitations": ["线程中未发现该阶段的受支持任务，该阶段未测。"],
        }
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 10},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))
        generation = [
            p for p in row["trace"]["phases"]
            if p["metric"] == "historical_generation_reliability"
        ][0]
        assert generation["status"] == "unmeasured"
        assert generation["detected"] is False
        assert "未测" in generation["unmeasured_reason"]

    def test_rates_rounded_to_two_decimals(self, monkeypatch, tmp_path):
        """比率保留两位小数：plugin-eval 出长浮点，明细表与导出会直接展示"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        payload["real_data"]["coverage"] = 47.36842105263158
        payload["real_data"]["behavior_coverage"]["rate"] = 42.857142857142854
        payload["history"]["coverage"] = 47.36842105263158
        payload["history"]["behavior_coverage"]["rate"] = 42.857142857142854
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        assert row["real_data"]["coverage"] == 47.37
        assert row["real_data"]["behavior_coverage"]["rate"] == 42.86
        assert row["trace"]["history"]["coverage"] == 47.37
        assert row["trace"]["history"]["behavior_coverage"]["rate"] == 42.86
        assert row["plugin_eval"]["raw"]["real_data"]["coverage"] == 47.37

    def test_phase_block_follows_report(self, monkeypatch, tmp_path):
        """行结构照 plugin-eval 报告：方向出中文名，分项出可信度/判定有效率/可判定·适用"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        payload["history"]["dimensions"] = {
            "reporting_accuracy": {"score": 100.0, "coverage": 22.580645161290324,
                                   "counts": {"passed": 7, "failed": 0, "insufficient": 24}},
            "procedure_adherence": {"score": 94.44444444444444, "coverage": 40.0,
                                    "counts": {"passed": 17, "failed": 1, "insufficient": 27}},
        }
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        phase = row["trace"]["phases"][0]
        assert phase["metric"] == "historical_execution_reliability"
        assert phase["label"] == "历史执行可信度"
        assert phase["score"] == 83.33
        assert phase["check_count"] == 1
        assert phase["key_step_total"] == 1
        # 方向用报告里的中文名；可判定/适用按 reporter 同口径（passed+failed，+insufficient）
        assert [dim["label"] for dim in phase["dimensions"]] == ["报告准确性", "流程遵循"]
        assert phase["dimensions"][1] == {
            "key": "procedure_adherence", "label": "流程遵循", "score": 94.44,
            "coverage": 40.0, "measured": 18, "applicable": 45,
            "counts": {"passed": 17, "failed": 1, "insufficient": 27},
        }
        assert row["trace"]["phases"] == [phase]
        assert row["trace"]["history"]["label"] == "历史执行可信度"
        # 逐检查项带方向名与所属阶段（页面/导出不再出现 procedure_adherence 这类抽取期键）
        assert row["checks"][0]["dimension"] == "procedure_adherence"
        assert row["checks"][0]["dimension_label"] == "流程遵循"
        assert row["checks"][0]["phase"] == "historical_execution_reliability"
        # raw 审计摘要同口径
        assert row["plugin_eval"]["raw"]["history"]["dimensions"][0]["label"] == "报告准确性"
        assert row["plugin_eval"]["raw"]["real_data"]["phases"][
            "historical_execution_reliability"
        ]["label"] == "历史执行可信度"

    def test_failed_checks_kept_outside_check_cap(self, monkeypatch, tmp_path):
        """检查项明细有上限，失败项单独留档——判定结论不能被截掉"""
        from app.services import plugin_evaluator as pv

        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        checks = [
            {"id": f"c{i}", "rule_id": "prerequisite-order", "dimension": "procedure_adherence",
             "subject": f"L{i}B0", "status": "insufficient", "reason": "无证据"}
            for i in range(pv.MAX_TRACE_CHECKS + 5)
        ]
        checks.append({
            "id": "boom", "rule_id": "credential-handling", "dimension": "error_handling",
            "subject": "L3961B0", "status": "failed",
            "reason": "当时规则要求凭据经 Helper 注入，未见对应调用",
        })
        payload["history"]["checks"] = checks
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        assert len(row["checks"]) == pv.MAX_TRACE_CHECKS
        assert row["checks_total"] == len(checks)
        phase = row["trace"]["phases"][0]
        assert phase["check_count"] == len(checks)
        assert phase["failed_total"] == 1
        assert [check["id"] for check in phase["failed_checks"]] == ["boom"]
        assert phase["failed_checks"][0]["dimension_label"] == "异常处理可信度"
        assert phase["failed_checks"][0]["phase"] == "historical_execution_reliability"

    def test_key_steps_keep_call_check(self, monkeypatch, tmp_path):
        """关键步骤表要出「本步调用确认」，故 call_check 必须落库"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        payload["history"]["key_steps"] = [
            {"id": "K001", "step": "write", "name": "业务写与造数", "attempts": 2,
             "achieved": True, "check_count": 3, "call_check": "failed"},
            {"id": "K002", "step": "investigate", "name": "检索与定位", "attempts": 5,
             "achieved": True, "check_count": 2},
        ]
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        steps = row["trace"]["phases"][0]["key_steps"]
        assert [step["call_check"] for step in steps] == ["failed", None]

    def test_generation_phase_gets_own_block(self, monkeypatch, tmp_path):
        """阶段各自成一节：出现生成阶段时并列入 trace.phases，方向名同口径"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload()
        payload["real_data"]["phases"]["historical_generation_reliability"] = {"status": "partial"}
        payload["history_generation"] = {
            "metric": "historical_generation_reliability", "plugin": "anc-tms",
            "thread_id": "21a46d7d", "status": "partial", "score": 100.0, "coverage": 38.5,
            "counts": {"passed": 20, "failed": 0, "insufficient": 32},
            "dimensions": {"source_alignment": {"score": None, "coverage": 0.0,
                                                "counts": {"passed": 0, "failed": 0,
                                                           "insufficient": 7}}},
            "key_steps": [], "checks": [],
        }
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        assert [phase["label"] for phase in row["trace"]["phases"]] == [
            "历史执行可信度", "历史生成可信度",
        ]
        assert row["trace"]["phases"][1]["dimensions"][0]["label"] == "来源一致性"
        # 执行阶段仍是 trace.history（页面列与规则锚点取它）
        assert row["trace"]["history"]["metric"] == "historical_execution_reliability"
        assert row["plugin_eval"]["raw"]["history_generation"]["label"] == "历史生成可信度"

    def test_artifacts_exclude_thread(self, monkeypatch, tmp_path):
        """产物目录传的是「除线程外的镜像」，不是整包（线程不作为产物重复解析）"""
        thread = _write_thread(tmp_path / "t.jsonl")
        (tmp_path / "test_cases.json").write_text("[]", encoding="utf-8")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        captured = {}

        def fake_run(plugin_dir, thread_path, artifact_dirs=None, **kwargs):
            # 镜像目录随 work_dir 清理，必须在调用时刻读取清单
            captured["artifact_dirs"] = artifact_dirs
            captured["names"] = (
                sorted(p.name for p in Path(artifact_dirs[0]).rglob("*") if p.is_file())
                if artifact_dirs else []
            )
            return {"payload": _history_payload(), "evaluator": _fake_evaluator(),
                    "duration_ms": 1}

        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval", fake_run
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))

        assert captured["artifact_dirs"] and len(captured["artifact_dirs"]) == 1
        assert captured["names"] == ["test_cases.json"]
        assert row["trace"]["artifact_files"] == 1
        assert row["trace"]["package_dir"] == str(tmp_path)

    def test_task_not_located_records_reason(self, monkeypatch, tmp_path):
        """plugin-eval 未定位到任务（出未测分）时行内写明原因"""
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        payload = _history_payload(score=None, status="unmeasured")
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_trace_eval",
            lambda *a, **kw: {"payload": payload, "evaluator": _fake_evaluator(),
                              "duration_ms": 1},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is True
        # 真实数据未测，但综合分照出（合并口径下两套分数各算各的）
        assert row["real_data"]["score"] is None
        assert row["average_score"] == 34.23
        assert "未在本次线程中定位到该插件的任务" in row["trace"]["unavailable_reason"]

    def test_unmeasured_without_package_dir(self):
        row = self._evaluator().evaluate_trace("")
        assert row["success"] is True
        assert row["average_score"] is None
        assert row["real_data"]["status"] == "unmeasured"
        assert "hub_file" in row["trace"]["unavailable_reason"]

    def test_unmeasured_missing_dir(self, tmp_path):
        row = self._evaluator().evaluate_trace(str(tmp_path / "nope"))
        assert row["success"] is True
        assert "镜像目录不存在" in row["trace"]["unavailable_reason"]

    def test_unmeasured_without_thread(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: _fake_evaluator()
        )
        monkeypatch.setattr(
            "app.services.plugin_evaluator.find_thread_file", lambda pkg, wd: None
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is True
        assert row["average_score"] is None
        assert "原始会话文件" in row["trace"]["unavailable_reason"]

    def test_unmeasured_without_rule_plugins(self, monkeypatch, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        monkeypatch.setattr(
            "app.services.plugin_evaluator.discover_rule_plugins", lambda root: []
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is True
        assert "rules/history" in row["trace"]["unavailable_reason"]

    def test_unmeasured_without_invocation(self, monkeypatch, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        monkeypatch.setattr(
            "app.services.plugin_evaluator.detect_invoked_plugin",
            lambda path, candidates: {"plugin": "", "counts": {}, "first_line": None,
                                      "session_id": "", "candidates": list(candidates)},
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is True
        assert row["average_score"] is None
        assert "没有可评插件的技能调用" in row["trace"]["unavailable_reason"]
        assert "anc-tms" in row["trace"]["unavailable_reason"]  # 列出规则覆盖范围

    def test_unmeasured_plugin_not_in_market(self, monkeypatch, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)
        monkeypatch.setattr(
            "app.services.plugin_evaluator.resolve_plugin_dir", lambda market_id, name, branch=None: None
        )
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is True
        assert "市场未收录插件" in row["trace"]["unavailable_reason"]

    def test_action_failure_is_failure_row(self, monkeypatch, tmp_path):
        thread = _write_thread(tmp_path / "t.jsonl")
        self._patch_env(monkeypatch, tmp_path, package_dir=thread)

        def boom(*args, **kwargs):
            raise runner.MarketEvalError("评测动作执行失败（退出码 2）")

        monkeypatch.setattr("app.services.plugin_evaluator.run_market_plugin_trace_eval", boom)
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is False
        assert "real_data" not in row
        assert row["evaluation_mode"] == "trace"  # 行全失败时聚合仍按 trace 渲染
        assert "trace 评测动作不可用" in row["error"]

    def test_evaluator_unavailable_is_failure_row(self, monkeypatch, tmp_path):
        def boom(market_id=None):
            raise runner.MarketEvalError("没有可用的插件市场")

        monkeypatch.setattr("app.services.plugin_evaluator.resolve_evaluator", boom)
        row = self._evaluator().evaluate_trace(str(tmp_path))
        assert row["success"] is False
        assert "没有可用的插件市场" in row["error"]

    def test_unmeasured_row_is_json_serializable(self, tmp_path):
        json.dumps(self._evaluator().evaluate_trace(""), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 聚合与导出
# ---------------------------------------------------------------------------

class TestTraceAggregation:

    def _trace_row(self, idx=0, score=83.33, status="partial", reason="", design_reused=False):
        from app.services.plugin_evaluator import PluginEvaluator, _design_snapshot

        detail = {
            "mode": "trace", "thread_file": "t.jsonl", "thread_sha256": "deadbeef",
            "detected": {"plugin": "anc-tms", "counts": {"anc-tms": 2}, "first_line": 7,
                         "session_id": "sess-1", "candidates": ["anc-tms"]},
        }
        if reason:
            row = PluginEvaluator._unmeasured_row(reason, detail, 0.0, _fake_evaluator())
        else:
            payload = _history_payload(score=score, status=status)
            row = PluginEvaluator._row_from_trace(
                {"payload": payload, "evaluator": _fake_evaluator(), "duration_ms": 10},
                "anc-tms", detail, ["w"], 0.0,
                design=_design_snapshot(payload), design_reused=design_reused,
            )
        row["idx"] = idx
        return row

    def test_trace_mode_summary(self):
        from app.services.result_aggregator import ResultAggregator

        agg = ResultAggregator.aggregate_plugin_results([
            self._trace_row(0, score=80.0), self._trace_row(1, score=90.0, design_reused=True),
            self._trace_row(2, reason="线程里没有可评插件的技能调用"),
        ])
        assert agg["mode"] == "trace"
        # 设计质量线按插件只评一次：两行同一插件只算一个样本，未测行不补
        assert agg["average_score"] == 34.23
        assert agg["badge_summary"] == {"no_badge": 1}
        assert agg["composite_summary"] == {
            "average_score": 34.23, "measured_count": 1, "unmeasured_count": 0, "unit": "plugin",
        }
        # 设计质量线明细：同一插件一条，覆盖 3 行（含未测行），其中 1 行复用首次评测
        design = agg["design_quality"]
        assert design["evaluated_count"] == 1
        assert design["plugins"][0]["plugin"] == "anc-tms"
        assert design["plugins"][0]["score"] == 34.23
        assert design["plugins"][0]["covered_rows"] == 3
        assert design["plugins"][0]["reused_rows"] == 1
        assert agg["items"][1]["design_reused"] is True
        assert agg["items"][0]["design_reused"] is False
        summary = agg["real_data_summary"]
        assert summary["average_score"] == 85.0
        assert summary["measured_count"] == 2
        assert summary["unmeasured_count"] == 1
        assert summary["status_summary"]["unmeasured"] == 1
        assert summary["average_coverage"] == 75.0
        assert summary["average_behavior_coverage"] == 10.99
        assert summary["check_summary"]["passed"] == 10  # 两行各 5
        # 得分标签与样本量：partial 的线程按「部分样本得分」标；分母只有通过+失败
        assert summary["score_label"] == "部分样本得分"
        assert summary["decidable_count"] == 12          # (5 + 1) × 2 行
        assert summary["applicable_count"] == 16         # 再加证据不足 2 × 2
        assert summary["conservative_score"] == 62.5     # 证据不足全按违规计的下界 10/16
        # 方向分项按阶段分行（未测阶段不参与，避免整行 0/0）
        assert summary["direction_summary"] == [{
            "key": "procedure_adherence", "label": "流程遵循",
            "phase": "历史执行可信度", "phase_metric": "historical_execution_reliability",
            "phase_status": "partial",
            "average_score": 85.0, "average_coverage": 75.0, "measured": 12, "applicable": 16,
        }]
        # 阶段汇总：本夹具只有执行阶段
        assert summary["phase_summary"] == [{
            "metric": "historical_execution_reliability", "label": "历史执行可信度",
            "status": "partial", "detected": None,
            "average_score": 85.0, "average_coverage": 75.0,
            "measured_rows": 2, "row_count": 2, "decidable": 12, "applicable": 16,
        }]
        assert summary["behavior_scope"] == "key_steps"
        assert agg["items"][2]["unmeasured_reason"]
        assert agg["items"][0]["real_data"]["score"] == 80.0
        assert agg["items"][0]["trace"]["detected"]["plugin"] == "anc-tms"
        # 两套分数的显式取值：综合分与真实数据分各自独立可读（老记录没有这两个键）
        assert agg["items"][0]["composite_score"] == 34.23
        assert agg["items"][0]["real_data_score"] == 80.0
        assert [s["name"] for s in agg["items"][0]["skills"]] == ["run"]
        assert agg["items"][2]["composite_score"] is None
        assert agg["items"][2]["real_data_score"] is None

    def test_unmeasured_phase_not_in_direction_summary(self):
        """未测阶段不参与方向分项（不出现整行 0/0），但保留在阶段汇总里"""
        from app.services.result_aggregator import ResultAggregator

        row = self._trace_row(0)
        row["trace"]["phases"].append({
            "metric": "historical_generation_reliability",
            "label": "历史生成可信度",
            "status": "unmeasured",
            "detected": False,
            "score": None,
            "coverage": None,
            "counts": {"passed": 0, "failed": 0, "insufficient": 0,
                       "not_applicable": 0, "measured": 0, "applicable": 0},
            "dimensions": [{
                "key": "source_alignment", "label": "来源一致性",
                "score": None, "coverage": None, "measured": 0, "applicable": 0,
            }],
        })
        agg = ResultAggregator.aggregate_plugin_results([row])
        keys = [d["key"] for d in agg["real_data_summary"]["direction_summary"]]
        assert "source_alignment" not in keys
        assert "procedure_adherence" in keys
        generation = [
            p for p in agg["real_data_summary"]["phase_summary"]
            if p["metric"] == "historical_generation_reliability"
        ][0]
        assert generation["status"] == "unmeasured"
        assert generation["average_score"] is None
        assert generation["decidable"] == 0

    def test_non_trace_unchanged(self):
        from app.services.result_aggregator import ResultAggregator

        rows = [{
            "success": True, "idx": 0, "target_name": "skill-a", "target_type": "skill",
            "average_score": 62.5, "badge": "silver", "dimension_scores": {"output_quality": 0.8},
            "anti_patterns": [{"flag": "MISSING_TRIGGER", "severity": 0.1, "description": "d"}],
            "plugin_eval": {"depth": "quick", "composite": {"confidence_label": "Estimated"},
                            "layers": [], "warnings": []},
        }]
        agg = ResultAggregator.aggregate_plugin_results(rows)
        assert agg["mode"] is None
        assert agg["real_data_summary"] is None
        assert agg["badge_summary"] == {"silver": 1}
        assert agg["items"][0]["real_data"] is None
        assert agg["items"][0]["unmeasured_reason"] == ""

    def test_empty_results_carry_new_keys(self):
        from app.services.result_aggregator import ResultAggregator

        agg = ResultAggregator.aggregate_plugin_results([])
        assert agg["mode"] is None and agg["real_data_summary"] is None

    def test_export_columns(self):
        from app.services.result_aggregator import ResultAggregator
        from app.services.evaluators.plugin import PluginEvaluatorAdapter

        agg = ResultAggregator.aggregate_plugin_results([self._trace_row(0)])
        adapter = PluginEvaluatorAdapter()
        csv_text = adapter.export_csv([agg])
        header = csv_text.splitlines()[0]
        for column in ("得分口径", "得分标签", "综合分", "真实数据分", "可判定/适用", "判定有效率",
                       "关键步骤覆盖率", "识别插件", "线程文件", "线程SHA256", "历史规则", "未测原因",
                       "通过", "失败", "证据不足", "失败项", "报告准确性", "流程遵循",
                       "异常处理可信度", "设计质量复用", "阶段得分", "未测阶段"):
            assert column in header
        # 得分标签与样本量同行落表：得分高但有效率低时不能只看到分数
        body = csv_text.splitlines()[1]
        assert "部分样本得分" in body
        assert "6 / 8" in body
        # 两套分数各占一列，且标明本次是合并口径（不能只看一列就下结论）
        assert "合并口径（综合分 + 真实数据分）" in body
        assert "34.23" in body and "83.33" in body
        workbook = adapter.export_excel([agg])
        assert workbook[:2] == b"PK"

    def test_export_legacy_trace_row_blanks_composite(self):
        """改动前落库的 trace 行：复合分列装的是真实数据分，导出必须清空并标口径"""
        from app.services.evaluators.plugin import _real_data_cells

        cells = _real_data_cells({
            "score": 100.0, "depth": "trace",
            "real_data": {"status": "partial", "score": 100.0,
                          "counts": {"passed": 9, "failed": 0, "insufficient": 16}},
            "trace": {},
        })
        assert cells["复合分"] == ""
        assert cells["综合分"] == ""
        assert cells["真实数据分"] == 100.0
        assert cells["得分口径"] == "历史口径（仅真实数据分）"
