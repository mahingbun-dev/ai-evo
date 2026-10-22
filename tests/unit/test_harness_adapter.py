"""
Harness 适配层单元测试

覆盖：HarnessAdapter 模板 run（files 物化与清理、宽容 JSON 提取、
一次格式纠正重试、结果归一化与 finish_reason）、run_sync_with_timeout
线程级超时、ClaudeCodeAdapter SDK 调用（mock）。
"""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.harness_adapter import (
    SYSTEM_PROMPT_ARGV_LIMIT_BYTES,
    ClaudeCodeAdapter,
    HarnessAdapter,
    normalize_judge_result,
    run_sync_with_timeout,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class FakeAdapter(HarnessAdapter):
    """测试用假适配器：按脚本依次返回 (原始回复, finish_reason)"""

    def __init__(self, config=None, responses=None, finish_reason="completed"):
        super().__init__(config=config or {})
        self.responses = list(responses or [])
        self.calls = []  # (system_prompt, prompt, timeout)

    def _execute_raw(self, system_prompt, prompt, timeout):
        self.calls.append((system_prompt, prompt, timeout))
        if not self.responses:
            return "", None
        raw = self.responses.pop(0)
        if isinstance(raw, Exception):
            raise raw
        return raw, self.finish_reason if hasattr(self, "finish_reason") else "completed"


VALID_JSON = json.dumps({"score": 8.5, "feedback": "良好", "dimension_scores": {"完成度": 8}})


class TestNormalizeJudgeResult:
    def test_full_result(self):
        result = normalize_judge_result({"score": "8", "feedback": "ok", "dimension_scores": {"a": "6"}})
        assert result["score"] == 8.0
        assert result["feedback"] == "ok"
        assert result["dimension_scores"] == {"a": 6.0}
        assert result["finish_reason"] == "completed"

    def test_invalid_values_dropped(self):
        result = normalize_judge_result({"score": None, "feedback": "", "dimension_scores": {"a": "bad"}})
        assert result["score"] == 0.0
        assert result["dimension_scores"] == {}

    def test_finish_reason_passthrough(self):
        result = normalize_judge_result({"score": 5}, finish_reason="max_turns")
        assert result["finish_reason"] == "max_turns"

    def test_dimension_feedbacks_passthrough(self):
        """裁判输出分维度理由时透传为 {维度名: 理由} 映射"""
        result = normalize_judge_result({
            "score": 8,
            "feedback": "ok",
            "dimension_scores": {"a": 6},
            "dimension_feedbacks": {"a": "内容准确", "b": "结构清晰"},
        })
        assert result["dimension_feedbacks"] == {"a": "内容准确", "b": "结构清晰"}

    def test_dimension_feedbacks_invalid_entries_filtered(self):
        """键或值非（非空）字符串的条目被过滤，仅保留合法条目"""
        result = normalize_judge_result({
            "score": 8,
            "dimension_feedbacks": {
                "a": "有效理由",
                "b": "",      # 空字符串 → 过滤
                "c": "   ",   # 纯空白 → 过滤
                "d": 123,     # 值非字符串 → 过滤
                "e": None,    # 值为 None → 过滤
            },
        })
        assert result["dimension_feedbacks"] == {"a": "有效理由"}

    def test_dimension_feedbacks_missing_no_key(self):
        """裁判未输出 dimension_feedbacks 时结果不含该键（旧形态兼容）"""
        result = normalize_judge_result({"score": 5, "dimension_scores": {}})
        assert "dimension_feedbacks" not in result

    def test_dimension_feedbacks_all_invalid_no_key(self):
        """条目全部非法时同样不输出该键"""
        result = normalize_judge_result({"score": 5, "dimension_feedbacks": {"a": "", "b": None}})
        assert "dimension_feedbacks" not in result

    def test_dimension_feedbacks_non_dict_dropped(self):
        """非 dict（裁判违反协议输出 list/str/数字）优雅丢弃该字段而非抛错"""
        for bad in (["a", "b"], "内容准确", 123):
            result = normalize_judge_result({"score": 5, "dimension_feedbacks": bad})
            assert result["score"] == 5.0
            assert "dimension_feedbacks" not in result


class TestHarnessAdapterRun:
    """模板 run：文件物化、JSON 解析、一次纠正重试"""

    def test_run_returns_normalized_result(self):
        adapter = FakeAdapter(responses=[VALID_JSON])
        result = adapter.run("sys", "user prompt", timeout=10)
        assert result == {"score": 8.5, "feedback": "良好", "dimension_scores": {"完成度": 8}, "finish_reason": "completed"}
        # 输出协议 prompt 透传给 _execute_raw
        assert adapter.calls[0][0] == "sys"

    def test_retry_once_on_invalid_json(self):
        adapter = FakeAdapter(responses=["我认为完成得不错", VALID_JSON])
        result = adapter.run("sys", "user prompt", timeout=10)
        assert result["score"] == 8.5
        assert len(adapter.calls) == 2
        # 重试 prompt 附加格式纠正指令
        assert "无法解析为 JSON" in adapter.calls[1][1]

    def test_all_invalid_raises(self):
        adapter = FakeAdapter(responses=["始终无法解析", "还是不行"])
        with pytest.raises(RuntimeError, match="无法解析"):
            adapter.run("sys", "user prompt", timeout=10)
        assert len(adapter.calls) == 2  # 只重试一次

    def test_fenced_json_parsed(self):
        adapter = FakeAdapter(responses=["```json\n" + VALID_JSON + "\n```"])
        assert adapter.run("sys", "user", timeout=10)["score"] == 8.5

    def test_default_timeout_from_config(self):
        adapter = FakeAdapter(config={"timeout_seconds": 123}, responses=[VALID_JSON])
        adapter.run("sys", "user")
        assert adapter.calls[0][2] == 123.0

    def test_default_timeout_fallback_300(self):
        adapter = FakeAdapter(responses=[VALID_JSON])
        adapter.run("sys", "user")
        assert adapter.calls[0][2] == 300.0


class TestFilesMaterialization:
    """files 物化：内容落临时文件、路径附进 prompt、run 结束清理"""

    def test_files_materialized_and_cleaned(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        adapter = FakeAdapter(responses=[VALID_JSON])
        result = adapter.run("sys", "user prompt", files={"cases_file": "用例内容"}, timeout=10)
        assert result["score"] == 8.5
        # prompt 中包含文件绝对路径
        prompt = adapter.calls[0][1]
        assert "cases_file" in prompt and "输入文件" in prompt
        # run 结束后临时目录已清理
        assert not (tmp_path / "uploads" / "harness_runs").exists() or not any(
            (tmp_path / "uploads" / "harness_runs").iterdir()
        )

    def test_unsafe_file_name_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        adapter = FakeAdapter(responses=[VALID_JSON])
        adapter.run("sys", "user", files={"../危险/name": "x"}, timeout=10)
        # listing 格式 "- {name}: {path}"：物化路径不含路径穿越字符
        listing_line = adapter.calls[0][1].split("\n")[-1]
        materialized_path = listing_line.rsplit(": ", 1)[-1]
        assert ".." not in materialized_path

    def test_no_files_no_prompt_suffix(self):
        adapter = FakeAdapter(responses=[VALID_JSON])
        adapter.run("sys", "user prompt", timeout=10)
        assert "输入文件" not in adapter.calls[0][1]


class TestRunSyncWithTimeout:
    """线程级超时执行器（兼容 nest_asyncio 环境）"""

    def test_normal_execution(self):
        assert run_sync_with_timeout(lambda: "ok", 5) == "ok"

    def test_timeout_raises(self):
        import time

        def slow():
            time.sleep(3)
            return "late"

        with pytest.raises(TimeoutError, match="超时"):
            run_sync_with_timeout(slow, 0.2)

    def test_exception_propagated(self):
        def boom():
            raise ValueError("inner error")

        with pytest.raises(ValueError, match="inner error"):
            run_sync_with_timeout(boom, 5)

    def test_works_with_nest_asyncio(self):
        """nest_asyncio 已 patch 的环境（生产 worker 场景）下仍可执行

        apply 是进程级全局补丁且多点（asyncio.run / loop 类方法），无法在测试
        进程内安全还原，故用子进程隔离验证：主进程保持干净，避免串扰其他用例
        （如 deepeval 对事件循环状态敏感、test_native_env_compat 的补丁探测）。
        """
        import subprocess
        import sys
        import textwrap

        code = textwrap.dedent(
            """
            import asyncio
            import nest_asyncio
            nest_asyncio.apply()
            from app.services.harness_adapter import run_sync_with_timeout

            async def _coro():
                await asyncio.sleep(0.01)
                return "ok-nest"

            def runner():
                return asyncio.run(_coro())

            assert run_sync_with_timeout(runner, 5) == "ok-nest"
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0 and "OK" in result.stdout, result.stderr[-400:]


class TestClaudeCodeAdapter:
    """ClaudeCodeAdapter：mock claude-agent-sdk query 流"""

    @staticmethod
    def _patch_sdk(payloads):
        """patch _run_with_prompt 返回预设文本序列"""
        responses = iter(payloads)

        async def fake_run(options, prompt):
            return next(responses)

        return patch("app.services.claude_code_judge_agent._run_with_prompt", side_effect=fake_run)

    def test_execute_raw_returns_completed(self):
        adapter = ClaudeCodeAdapter({"model": "m", "timeout_seconds": 10, "max_turns": 2})
        with self._patch_sdk([VALID_JSON]), patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}
        ):
            raw, finish_reason = adapter._execute_raw("sys", "user prompt", 10)
        assert raw == VALID_JSON
        assert finish_reason == "completed"

    def test_run_prompt_with_retry(self):
        adapter = ClaudeCodeAdapter({"model": "m", "timeout_seconds": 10, "max_turns": 2})
        with self._patch_sdk(["无法解析的文本", VALID_JSON]), patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}
        ):
            result = adapter.run_prompt("sys", "user prompt", 10)
        assert result["score"] == 8.5
        assert result["finish_reason"] == "completed"

    def test_empty_response_raises(self):
        adapter = ClaudeCodeAdapter({"model": "m", "timeout_seconds": 10, "max_turns": 2})
        with self._patch_sdk([""]), patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}
        ):
            with pytest.raises(RuntimeError, match="无返回内容"):
                adapter._execute_raw("sys", "user prompt", 10)


class TestClaudeCodeAdapterArgvGuard:
    """argv 安全兜底：system_prompt 超限降级并入首条消息（execve E2BIG 修复）"""

    @staticmethod
    def _patch_sdk_capture(captured):
        """patch _run_with_prompt 并捕获每次调用的 (options.system_prompt, prompt)"""
        responses = iter([VALID_JSON, VALID_JSON, VALID_JSON, VALID_JSON])

        async def fake_run(options, prompt):
            captured.append((options.system_prompt, prompt))
            return next(responses)

        return patch("app.services.claude_code_judge_agent._run_with_prompt", side_effect=fake_run)

    def _execute(self, system_prompt, prompt, captured):
        adapter = ClaudeCodeAdapter({"model": "m", "timeout_seconds": 10, "max_turns": 2})
        with self._patch_sdk_capture(captured), patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}
        ):
            adapter._execute_raw(system_prompt, prompt, 10)

    def test_small_system_prompt_passthrough(self):
        captured = []
        self._execute("短系统提示", "user prompt", captured)
        sp, prompt = captured[0]
        assert sp == "短系统提示"
        assert prompt == "user prompt"

    def test_oversized_system_prompt_demoted_into_prompt(self):
        # 50000 汉字 = 150000B，超 100KB argv 安全阈值（生产 Errno 7 场景量级）
        big = "评" * 50000
        captured = []
        self._execute(big, "user prompt", captured)
        sp, prompt = captured[0]
        assert sp is None  # SDK 不再传 --system-prompt flag
        assert prompt.startswith(big)
        assert "\n\n---\n" in prompt
        assert prompt.endswith("user prompt")

    def test_byte_boundary_passthrough(self):
        # 恰好 100000B（ASCII 单字节）不降级
        boundary = "a" * SYSTEM_PROMPT_ARGV_LIMIT_BYTES
        captured = []
        self._execute(boundary, "p", captured)
        assert captured[0][0] == boundary
        assert captured[0][1] == "p"

    def test_byte_boundary_demoted(self):
        captured = []
        self._execute("a" * (SYSTEM_PROMPT_ARGV_LIMIT_BYTES + 1), "p", captured)
        assert captured[0][0] is None
        assert captured[0][1].startswith("a" * (SYSTEM_PROMPT_ARGV_LIMIT_BYTES + 1))

    def test_empty_system_prompt_untouched(self):
        captured = []
        self._execute("", "user prompt", captured)
        assert captured[0] == ("", "user prompt")

    def test_retry_path_keeps_demotion(self):
        # run_prompt 首次解析失败重试：两次调用对同一超限 system_prompt 处理一致
        big = "评" * 50000
        adapter = ClaudeCodeAdapter({"model": "m", "timeout_seconds": 10, "max_turns": 2})
        captured = []
        responses = iter(["非 JSON 文本", VALID_JSON])

        async def fake_run(options, prompt):
            captured.append((options.system_prompt, prompt))
            return next(responses)

        with patch("app.services.claude_code_judge_agent._run_with_prompt", side_effect=fake_run), patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}
        ):
            adapter.run_prompt(big, "user prompt", 10)
        assert len(captured) == 2
        assert all(sp is None for sp, _ in captured)
        assert captured[0][1].endswith("user prompt")
        # 重试 prompt 在降级后的首条消息尾部附加格式纠正指令
        assert "无法解析为 JSON" in captured[1][1]
        assert "user prompt" in captured[1][1]
