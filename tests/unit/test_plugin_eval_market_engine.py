"""插件评测 market 引擎单元测试

覆盖：引擎开关与默认值、argv/env 构造（白名单 + Claude Code env + PYTHONPATH）、
评测动作定位（市场 + 懒加载锚点 + 版本号）、子进程失败路径（超时/非零/非 JSON）、
market JSON → 行结果映射（未测维度剔除、反模式汇总、中文 label）、
深度守卫（插件级禁用 standard）、以及任务流里 market 引擎的接入。

子进程一律 monkeypatch ``subprocess.run``（不起真进程、不调模型）；评测动作定位
monkeypatch 市场服务的懒加载入口。
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import plugin_eval_market_runner as runner
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.plugin_evaluator import PluginEvaluator, plugin_eval_engine
from app.services.plugin_marketplace_service import MarketplaceError

pytestmark = pytest.mark.unit


GOOD_SKILL_MD = """---
name: demo-checker
description: Use when the user asks to check project demo files for broken links. Use proactively.
---

# Demo Checker

Run the checker.
"""


def _market_payload(score=53.2, badge="silver", confidence="Estimated", measured_quality=True):
    """市场版 plugin-eval 的 JSON 输出（字段取自真实运行，含未测维度）"""
    dims = [
        {"name": "triggering_accuracy", "weight": 0.25, "score": 0.41, "measured": True, "grade": "F"},
        {"name": "orchestration_fitness", "weight": 0.20, "score": 0.80, "measured": True, "grade": "B-"},
        {
            "name": "output_quality",
            "weight": 0.15,
            "score": 0.74 if measured_quality else 0.0,
            "measured": measured_quality,
            "grade": "C" if measured_quality else "—",
        },
        {"name": "scope_calibration", "weight": 0.12, "score": 0.0, "measured": False, "grade": "—"},
        {"name": "token_efficiency", "weight": 0.06, "score": 0.9484, "measured": True, "grade": "A"},
    ]
    return {
        "plugin_path": "/tmp/target",
        "timestamp": "2026-10-15T10:00:00+00:00",
        "config": {"depth": "quick", "concurrency": 4},
        "layers": [
            {
                "layer": "static",
                "score": 0.5516,
                "sub_scores": {"frontmatter_quality": 0.41, "token_efficiency": 0.9484},
                "anti_patterns": [
                    {"flag": "MISSING_TRIGGER", "description": "no trigger", "severity": 0.15},
                    {"flag": "DEAD_CROSS_REF", "description": "dead ref", "severity": 0.05},
                ],
                "metadata": {"skill_name": "demo-checker", "line_count": 12},
            }
        ],
        "composite": {
            "score": score,
            "anti_pattern_penalty": 0.9,
            "badge": badge,
            "confidence_label": confidence,
            "dimensions": dims,
        },
        "elo": {"rating": 1200},
        "model_usage": {},
        "total_duration_ms": 321,
        "skill_results": [],
    }


def _fake_evaluator(package_root="/mnt/market/public/plugin-eval", version="0.5.0", commit="abc123"):
    return {
        "name": "plugin-eval",
        "version": version,
        "market_id": 2,
        "market_name": "ANC 插件市场",
        "source": "./public/plugin-eval",
        "commit": commit,
        "stale": False,
        "refresh_error": "",
        "package_root": str(package_root),
    }


class TestEngineSwitch:
    """引擎开关：默认 market，可切 legacy，非法值回落 market"""

    def test_default_is_market(self, monkeypatch):
        monkeypatch.delenv("PLUGIN_EVAL_ENGINE", raising=False)
        assert plugin_eval_engine() == "market"

    def test_explicit_legacy(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_EVAL_ENGINE", "legacy")
        assert plugin_eval_engine() == "legacy"

    def test_invalid_falls_back_to_market(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_EVAL_ENGINE", "no_such_engine")
        assert plugin_eval_engine() == "market"

    def test_evaluator_engine_param_overrides_env(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_EVAL_ENGINE", "market")
        assert PluginEvaluator(depth="quick", engine="legacy").engine == "legacy"


class TestBuildArgvAndEnv:
    """子进程契约：argv 形态与 env 注入口径"""

    def test_argv_shape(self):
        argv = runner.build_argv(Path("/mkt/public/plugin-eval"), "/mkt/skill", "standard", 4)
        assert argv[0]
        assert argv[1] == str(runner.ENTRY_SCRIPT)
        assert argv[2] == "/mkt/public/plugin-eval"
        assert argv[3:5] == ["score", "/mkt/skill"]
        assert argv[5:7] == ["--depth", "standard"]
        assert argv[7:9] == ["--output", "json"]
        assert argv[9:11] == ["--concurrency", "4"]

    def test_env_has_pythonpath_and_no_bytecode(self):
        env = runner.build_env(Path("/mkt/public/plugin-eval"))
        assert env["PYTHONPATH"] == "/mkt/public/plugin-eval/src"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_env_whitelist_strips_platform_secrets(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
        monkeypatch.setenv("REDIS_URL", "redis://secret")
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        env = runner.build_env(Path("/mkt/plugin-eval"))
        assert "DATABASE_URL" not in env and "REDIS_URL" not in env
        assert env["LANG"] == "zh_CN.UTF-8"

    def test_env_merges_cc_env(self):
        env = runner.build_env(Path("/mkt/plugin-eval"), {"ANTHROPIC_MODEL": "deepseek-flash"})
        assert env["ANTHROPIC_MODEL"] == "deepseek-flash"


class TestResolveEvaluator:
    """评测动作定位：市场 + 懒加载锚点 + 版本号"""

    def _patch_market(self, monkeypatch, markets, info):
        monkeypatch.setattr("app.services.plugin_marketplace_service.list_marketplaces", lambda db: markets)
        monkeypatch.setattr(
            "app.services.plugin_marketplace_service.prepare_market_plugin_for_eval",
            lambda market_id, source, **kw: info(market_id, source),
        )

    def test_uses_given_market_and_reads_version(self, tmp_path, monkeypatch):
        package_root = tmp_path / "plugin-eval"
        (package_root / "src").mkdir(parents=True)
        (package_root / "pyproject.toml").write_text(
            '[project]\nname = "plugin-eval"\nversion = "0.5.0"\n', encoding="utf-8"
        )
        info = _fake_evaluator(package_root=package_root)
        self._patch_market(
            monkeypatch,
            [{"id": 2, "sync_status": "ok"}],
            lambda mid, src: {**info, "path": str(package_root)},
        )

        resolved = runner.resolve_evaluator(market_id=2)
        assert resolved["version"] == "0.5.0"
        assert resolved["market_id"] == 2
        assert resolved["package_root"] == str(package_root)

    def test_falls_back_to_first_synced_market(self, tmp_path, monkeypatch):
        package_root = tmp_path / "plugin-eval"
        (package_root / "src").mkdir(parents=True)
        seen = {}
        self._patch_market(
            monkeypatch,
            [{"id": 7, "sync_status": "ok"}, {"id": 9, "sync_status": "ok"}],
            lambda mid, src: seen.update({"market_id": mid, "source": src})
            or {**_fake_evaluator(package_root=package_root), "path": str(package_root)},
        )

        runner.resolve_evaluator()
        assert seen["market_id"] == 7
        assert seen["source"] == "./public/plugin-eval"

    def test_no_synced_market_raises(self, monkeypatch):
        self._patch_market(monkeypatch, [{"id": 1, "sync_status": "failed"}], lambda mid, src: {})
        with pytest.raises(runner.MarketEvalError, match="没有可用的插件市场"):
            runner.resolve_evaluator()

    def test_missing_src_dir_raises(self, tmp_path, monkeypatch):
        package_root = tmp_path / "plugin-eval"
        package_root.mkdir()
        self._patch_market(
            monkeypatch, [{"id": 2, "sync_status": "ok"}],
            lambda mid, src: {**_fake_evaluator(package_root=package_root), "path": str(package_root)},
        )
        with pytest.raises(runner.MarketEvalError, match="源码目录"):
            runner.resolve_evaluator(market_id=2)

    def test_marketplace_error_propagates(self, monkeypatch):
        def _boom(market_id, source, **kw):
            raise MarketplaceError("市场内未找到插件目录")

        monkeypatch.setattr("app.services.plugin_marketplace_service.list_marketplaces", lambda db: [])
        monkeypatch.setattr("app.services.plugin_marketplace_service.prepare_market_plugin_for_eval", _boom)
        monkeypatch.setattr(
            "app.services.plugin_marketplace_service.list_marketplaces",
            lambda db: [{"id": 2, "sync_status": "ok"}],
        )
        with pytest.raises(MarketplaceError):
            runner.resolve_evaluator(market_id=2)


class TestRunMarketPluginEval:
    """子进程执行：成功解析 JSON 与失败路径"""

    def _patch_cc(self, monkeypatch, configured=True):
        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
            classmethod(lambda cls: {"timeout_seconds": 600} if configured else None),
        )
        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env",
            classmethod(lambda cls: {"ANTHROPIC_MODEL": "deepseek-flash"}),
        )

    def _patch_run(self, monkeypatch, returncode=0, stdout="", stderr="", capture=None):
        def fake_run(argv, **kwargs):
            if capture is not None:
                capture.update({"argv": argv, **kwargs})
            return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

    def test_success_returns_payload_and_anchor(self, monkeypatch):
        self._patch_cc(monkeypatch)
        self._patch_run(monkeypatch, stdout=json.dumps(_market_payload(), ensure_ascii=False))
        out = runner.run_market_plugin_eval("/tmp/skill", depth="quick", evaluator=_fake_evaluator())
        assert out["payload"]["composite"]["score"] == 53.2
        assert out["evaluator"]["commit"] == "abc123"
        assert out["duration_ms"] >= 0

    def test_quick_does_not_require_cc_config(self, monkeypatch):
        self._patch_cc(monkeypatch, configured=False)
        self._patch_run(monkeypatch, stdout=json.dumps(_market_payload()))
        out = runner.run_market_plugin_eval("/tmp/skill", depth="quick", evaluator=_fake_evaluator())
        assert "composite" in out["payload"]

    def test_standard_without_cc_config_raises(self, monkeypatch):
        self._patch_cc(monkeypatch, configured=False)
        with pytest.raises(runner.MarketEvalError, match="未配置 Claude Code"):
            runner.run_market_plugin_eval("/tmp/skill", depth="standard", evaluator=_fake_evaluator())

    def test_standard_passes_cc_env(self, monkeypatch):
        self._patch_cc(monkeypatch)
        capture = {}
        self._patch_run(monkeypatch, stdout=json.dumps(_market_payload()), capture=capture)
        runner.run_market_plugin_eval("/tmp/skill", depth="standard", evaluator=_fake_evaluator())
        assert capture["env"]["ANTHROPIC_MODEL"] == "deepseek-flash"

    def test_nonzero_returncode_raises_with_stderr(self, monkeypatch):
        self._patch_cc(monkeypatch)
        self._patch_run(monkeypatch, returncode=3, stderr="加载 plugin_eval.cli 失败: ModuleNotFoundError")
        with pytest.raises(runner.MarketEvalError, match="加载 plugin_eval.cli 失败"):
            runner.run_market_plugin_eval("/tmp/skill", depth="quick", evaluator=_fake_evaluator())

    def test_invalid_json_raises(self, monkeypatch):
        self._patch_cc(monkeypatch)
        self._patch_run(monkeypatch, stdout="这不是 JSON")
        with pytest.raises(runner.MarketEvalError, match="不是合法 JSON"):
            runner.run_market_plugin_eval("/tmp/skill", depth="quick", evaluator=_fake_evaluator())

    def test_missing_composite_raises(self, monkeypatch):
        self._patch_cc(monkeypatch)
        self._patch_run(monkeypatch, stdout=json.dumps({"layers": []}))
        with pytest.raises(runner.MarketEvalError, match="缺少 composite"):
            runner.run_market_plugin_eval("/tmp/skill", depth="quick", evaluator=_fake_evaluator())

    def test_timeout_raises(self, monkeypatch):
        self._patch_cc(monkeypatch)

        def fake_run(argv, **kwargs):
            raise runner.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        monkeypatch.setattr(runner.subprocess, "run", fake_run)
        with pytest.raises(runner.MarketEvalError, match="执行超时"):
            runner.run_market_plugin_eval("/tmp/skill", depth="quick", evaluator=_fake_evaluator())

    def test_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_EVAL_TIMEOUT_SECONDS", "42")
        assert runner._timeout_for("standard") == 42.0
        monkeypatch.setenv("PLUGIN_EVAL_TIMEOUT_SECONDS", "abc")
        assert runner._timeout_for("quick") == runner.QUICK_TIMEOUT_SECONDS


class TestMarketRowMapping:
    """market JSON → 平台行结果 schema"""

    @pytest.fixture
    def skill_dir(self, tmp_path):
        d = tmp_path / "demo-checker"
        d.mkdir()
        (d / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
        return d

    def _evaluate(self, monkeypatch, skill_dir, depth="quick", payload=None, error=None):
        if error:
            monkeypatch.setattr(
                "app.services.plugin_evaluator.run_market_plugin_eval",
                lambda *a, **k: (_ for _ in ()).throw(error),
            )
        else:
            monkeypatch.setattr(
                "app.services.plugin_evaluator.run_market_plugin_eval",
                lambda target, depth, evaluator: {
                    "payload": payload or _market_payload(),
                    "evaluator": _fake_evaluator(),
                    "duration_ms": 100,
                },
            )
        monkeypatch.setattr("app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: _fake_evaluator())
        return PluginEvaluator(depth=depth, engine="market").evaluate(plugin_path=str(skill_dir))

    def test_row_fields_from_composite(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir)
        assert row["success"] is True
        assert row["target_type"] == "skill"
        assert row["target_name"] == "demo-checker"
        assert row["average_score"] == 53.2
        assert row["badge"] == "silver"
        assert row["plugin_eval"]["composite"]["confidence_label"] == "Estimated"
        assert row["plugin_eval"]["depth"] == "quick"
        json.dumps(row, ensure_ascii=False)  # 落库要求可序列化

    def test_unmeasured_dimensions_excluded(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir)
        assert "scope_calibration" not in row["dimension_scores"]  # measured=false 的 0.0 不得入内
        assert row["dimension_scores"]["triggering_accuracy"] == 0.41
        assert set(row["dimension_scores"]) == {
            "triggering_accuracy", "orchestration_fitness", "output_quality", "token_efficiency"
        }
        # judge 维度未测出（quick 深度）时同样剔除，不拉低聚合均分
        quick_row = self._evaluate(
            monkeypatch, skill_dir, payload=_market_payload(measured_quality=False)
        )
        assert "output_quality" not in quick_row["dimension_scores"]

    def test_dimension_label_localized(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir)
        labels = {d["name"]: d["label"] for d in row["plugin_eval"]["composite"]["dimensions"]}
        assert labels["triggering_accuracy"] == "触发准确性"
        assert labels["scope_calibration"] == "范围校准"

    def test_anti_patterns_merged_from_layers(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir)
        flags = [ap["flag"] for ap in row["anti_patterns"]]
        assert flags == ["MISSING_TRIGGER", "DEAD_CROSS_REF"]

    def test_evaluator_anchor_recorded(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir)
        evaluator = row["plugin_eval"]["evaluator"]
        assert evaluator["version"] == "0.5.0"
        assert evaluator["commit"] == "abc123"
        assert "package_root" not in evaluator  # 宿主路径不出库
        assert any("plugin-eval" in w for w in row["plugin_eval"]["warnings"])

    def test_raw_payload_kept_for_audit(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir)
        assert row["plugin_eval"]["raw"]["elo"] == {"rating": 1200}

    def test_plugin_level_standard_downgraded_to_quick(self, monkeypatch, tmp_path):
        plugin_dir = tmp_path / "some-plugin"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "some-plugin"}), encoding="utf-8"
        )
        captured = {}
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_eval",
            lambda target, depth, evaluator: captured.update({"depth": depth})
            or {"payload": _market_payload(), "evaluator": _fake_evaluator(), "duration_ms": 1},
        )
        monkeypatch.setattr("app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: _fake_evaluator())

        row = PluginEvaluator(depth="standard", engine="market").evaluate(plugin_path=str(plugin_dir))

        assert captured["depth"] == "quick"  # 执行期降级
        assert row["plugin_eval"]["depth"] == "quick"
        assert any("只支持 quick" in w for w in row["plugin_eval"]["warnings"])

    def test_market_error_becomes_failure_row(self, monkeypatch, skill_dir):
        row = self._evaluate(monkeypatch, skill_dir, error=runner.MarketEvalError("没有可用的插件市场"))
        assert row["success"] is False
        assert "评测动作不可用" in row["error"]
        assert "没有可用的插件市场" in row["error"]

    def test_inline_skill_content_uses_temp_dir_and_cleans_up(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_eval",
            lambda target, depth, evaluator: captured.update({"target": target})
            or {"payload": _market_payload(), "evaluator": _fake_evaluator(), "duration_ms": 1},
        )
        monkeypatch.setattr("app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: _fake_evaluator())

        row = PluginEvaluator(depth="quick", engine="market").evaluate(skill_content=GOOD_SKILL_MD)

        assert row["success"] is True
        assert row["target_name"] == "demo-checker"
        temp_dir = Path(captured["target"])
        assert not temp_dir.exists()  # 临时目录已清理
        assert (temp_dir / "SKILL.md").name == "SKILL.md"

    def test_missing_target_fails(self, monkeypatch):
        monkeypatch.setattr("app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: _fake_evaluator())
        row = PluginEvaluator(depth="quick", engine="market").evaluate()
        assert row["success"] is False
        assert "plugin_path" in row["error"]


class TestDepthGuardValidation:
    """深度守卫：插件级目标只支持 quick（创建期前置拒绝）"""

    def _service(self):
        return EvaluationTaskService(MagicMock())

    def test_marketplace_plugin_level_standard_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.plugin_marketplace_service.list_marketplace_plugins",
            lambda db, market_id: [
                {"source": "./public/anc-demo", "target_type": "plugin"},
                {"source": "./public/anc-demo/skills/x", "target_type": "skill"},
            ],
        )
        with pytest.raises(ValueError, match="只支持 quick"):
            self._service()._validate_plugin_config({
                "depth": "standard", "source": "marketplace",
                "market_id": 2, "market_plugin": "./public/anc-demo",
            })

    def test_marketplace_skill_level_standard_allowed(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.plugin_marketplace_service.list_marketplace_plugins",
            lambda db, market_id: [{"source": "./public/anc-demo/skills/x", "target_type": "skill"}],
        )
        self._service()._validate_plugin_config({
            "depth": "standard", "source": "marketplace",
            "market_id": 2, "market_plugin": "./public/anc-demo/skills/x",
        })

    def test_manual_plugin_dir_standard_rejected(self, tmp_path):
        plugin_dir = tmp_path / "p"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="只支持 quick"):
            self._service()._validate_plugin_config({
                "depth": "standard", "plugin_path": str(plugin_dir),
            })

    def test_manual_skill_dir_standard_allowed(self, tmp_path):
        skill_dir = tmp_path / "s"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(GOOD_SKILL_MD, encoding="utf-8")
        self._service()._validate_plugin_config({"depth": "standard", "plugin_path": str(skill_dir)})

    def test_inline_content_standard_allowed(self):
        self._service()._validate_plugin_config({"depth": "standard", "skill_content": GOOD_SKILL_MD})

    def test_market_not_synced_skips_guard(self, monkeypatch):
        def _boom(db, market_id):
            raise MarketplaceError("市场尚未同步成功")

        monkeypatch.setattr("app.services.plugin_marketplace_service.list_marketplace_plugins", _boom)
        # 判不出类型时不在创建期拦截，交给执行期按 quick 降级
        self._service()._validate_plugin_config({
            "depth": "standard", "source": "marketplace",
            "market_id": 2, "market_plugin": "./public/anc-demo",
        })

    def test_quick_depth_never_guarded(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.plugin_marketplace_service.list_marketplace_plugins",
            lambda db, market_id: (_ for _ in ()).throw(AssertionError("quick 不该查目标类型")),
        )
        self._service()._validate_plugin_config({
            "depth": "quick", "source": "marketplace",
            "market_id": 2, "market_plugin": "./public/anc-demo",
        })


class TestTaskFlowWithMarketEngine:
    """任务流接入：market 引擎下插件级 standard 被降级并落到行结果"""

    def test_plugin_level_row_downgraded(self, monkeypatch, tmp_path):
        from app.tasks.evaluation_tasks_async import _execute_plugin_evaluation

        plugin_dir = tmp_path / "some-plugin"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "some-plugin"}), encoding="utf-8"
        )
        monkeypatch.setenv("PLUGIN_EVAL_ENGINE", "market")
        monkeypatch.setattr("app.services.plugin_evaluator.resolve_evaluator", lambda market_id=None: _fake_evaluator())
        monkeypatch.setattr(
            "app.services.plugin_evaluator.run_market_plugin_eval",
            lambda target, depth, evaluator: {
                "payload": _market_payload(), "evaluator": _fake_evaluator(), "duration_ms": 1,
            },
        )

        task = MagicMock()
        task.evaluation_type = "plugin"
        task.id = "task-market"
        task.plugin_config = {"depth": "standard", "source": "manual", "plugin_path": str(plugin_dir)}
        payloads = [{"_orig_idx": 0, "plugin_path": str(plugin_dir)}]

        with patch("app.tasks.evaluation_tasks_async._persist_row"), \
             patch("app.tasks.evaluation_tasks_async._update_progress"):
            results = _execute_plugin_evaluation(task, payloads, "task-market", None, None, execution_number=1)

        assert results[0]["success"] is True
        assert results[0]["plugin_eval"]["depth"] == "quick"
        assert results[0]["plugin_eval"]["evaluator"]["version"] == "0.5.0"
