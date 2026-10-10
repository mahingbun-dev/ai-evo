"""
Claude Code Agent 裁判执行器单元测试

覆盖：JSON 宽容提取、结果归一化、最终 prompt 组装（会话文件/输出协议）、
会话临时文件写入与清理、多裁判并发与异常兜底、SDK 调用（mock）。
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.claude_code_judge_agent import (
    ClaudeCodeJudgeAgent,
    extract_json_object,
    write_session_file,
    cleanup_session_files,
)


class TestExtractJsonObject:
    def test_plain_json(self):
        assert extract_json_object('{"score": 8}') == {"score": 8}

    def test_fenced_json(self):
        text = '评分如下：\n```json\n{"score": 8.5, "feedback": "好"}\n```'
        assert extract_json_object(text)["score"] == 8.5

    def test_json_with_prefix_text(self):
        text = '我的评分结果是 {"score": 7, "dimension_scores": {"准确性": 7}} 以上。'
        assert extract_json_object(text)["score"] == 7

    def test_nested_braces(self):
        text = '结果 {"score": 6, "dimension_scores": {"a": 5}, "note": "x{y}z"}'
        parsed = extract_json_object(text)
        assert parsed["dimension_scores"]["a"] == 5

    def test_invalid_returns_none(self):
        assert extract_json_object("没有任何 JSON") is None
        assert extract_json_object("") is None


class TestNormalizeResult:
    def _agent(self):
        task = type("Task", (), {"id": "task_x", "evaluation_metrics": [], "max_score": 10})()
        return ClaudeCodeJudgeAgent(task)

    def test_string_score_converted(self):
        result = self._agent()._normalize_result({"score": "8", "feedback": "ok", "dimension_scores": {"a": "6"}})
        assert result["score"] == 8.0
        assert result["dimension_scores"]["a"] == 6.0

    def test_invalid_values_dropped(self):
        result = self._agent()._normalize_result({"score": None, "feedback": "", "dimension_scores": {"a": "bad"}})
        assert result["score"] == 0.0
        assert result["dimension_scores"] == {}


class TestBuildFinalPrompt:
    def test_with_session_file(self):
        task = type("Task", (), {
            "id": "task_x",
            "evaluation_metrics": [{"name": "任务完成度"}, {"name": "工具使用"}],
            "max_score": 10,
        })()
        agent = ClaudeCodeJudgeAgent(task)
        prompt = agent._build_final_prompt("用户提示", "/tmp/session.md")
        assert "/tmp/session.md" in prompt
        assert "Read" in prompt
        assert "任务完成度" in prompt and "工具使用" in prompt
        assert '"score"' in prompt

    def test_without_session_file(self):
        task = type("Task", (), {
            "id": "task_x",
            "evaluation_metrics": [{"name": "质量"}],
            "max_score": 5,
        })()
        agent = ClaudeCodeJudgeAgent(task)
        prompt = agent._build_final_prompt("用户提示", None)
        assert "0 到 5" in prompt
        assert "质量" in prompt


class TestSessionFiles:
    def test_write_and_cleanup(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = write_session_file("task_abc", "data2_xyz123", "# 会话内容")
        assert Path(path).exists()
        assert path.endswith(".md")
        cleanup_session_files("task_abc")
        assert not Path(path).exists()

    def test_unsafe_data_id_sanitized(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = write_session_file("task_abc", "../危险/ID", "# 会话")
        assert ".." not in Path(path).name
        cleanup_session_files("task_abc")


class TestRunParallel:
    """多裁判并发：mock CC 配置与单裁判执行"""

    def _make_agent(self):
        task = type("Task", (), {
            "id": "task_x", "evaluation_metrics": [], "max_score": 10,
        })()
        return ClaudeCodeJudgeAgent(task, langfuse_host="")

    def test_no_config_raises(self):
        agent = self._make_agent()
        with patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="未配置 Claude Code"):
                agent.run_parallel("sys", "user", None, 1)

    def test_multi_judge_results(self):
        agent = self._make_agent()
        config = {"model": "m", "timeout_seconds": 5, "max_turns": 2}
        with patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
            return_value=config,
        ):
            with patch.object(
                ClaudeCodeJudgeAgent,
                "_run_single",
                side_effect=lambda *a, **k: {"score": 8, "feedback": "ok", "dimension_scores": {}},
            ):
                results = agent.run_parallel("sys", "user", None, 3)
        assert len(results) == 3
        assert all(r["score"] == 8 for r in results)

    def test_single_failure_returns_zero(self):
        agent = self._make_agent()
        config = {"model": "m", "timeout_seconds": 5, "max_turns": 2}
        with patch(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
            return_value=config,
        ):
            with patch.object(
                ClaudeCodeJudgeAgent,
                "_run_single",
                side_effect=RuntimeError("boom"),
            ):
                results = agent.run_parallel("sys", "user", None, 2)
        assert len(results) == 2
        assert all(r["score"] == 0 for r in results)
        assert "boom" in results[0]["feedback"]


class TestRunAsyncWithTimeout:
    """线程级超时执行器（兼容 nest_asyncio 环境）"""

    def test_normal_execution(self):
        from app.services.claude_code_judge_agent import _run_async_with_timeout

        async def coro():
            await __import__("asyncio").sleep(0.01)
            return "ok"

        assert _run_async_with_timeout(coro, 5) == "ok"

    def test_timeout_raises(self):
        import asyncio as aio

        from app.services.claude_code_judge_agent import _run_async_with_timeout

        async def slow():
            await aio.sleep(3)
            return "late"

        with pytest.raises(TimeoutError, match="超时"):
            _run_async_with_timeout(slow, 0.2)

    def test_exception_propagated(self):
        from app.services.claude_code_judge_agent import _run_async_with_timeout

        async def boom():
            raise ValueError("inner error")

        with pytest.raises(ValueError, match="inner error"):
            _run_async_with_timeout(boom, 5)

    def test_works_with_nest_asyncio(self):
        """nest_asyncio 已 patch asyncio.run 的环境（生产 worker 场景）下仍可执行

        apply 是进程级全局补丁且多点，无法在测试进程内安全还原，用子进程隔离
        验证（理由同 test_harness_adapter 同名用例）。
        """
        import subprocess
        import sys
        import textwrap

        code = textwrap.dedent(
            """
            import asyncio as aio
            import nest_asyncio
            nest_asyncio.apply()
            from app.services.claude_code_judge_agent import _run_async_with_timeout

            async def coro():
                await aio.sleep(0.01)
                return "ok-nest"

            assert _run_async_with_timeout(coro, 5) == "ok-nest"
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0 and "OK" in result.stdout, result.stderr[-400:]


class TestQueryAgent:
    """SDK 调用与解析重试（mock query 流）"""

    def _agent(self):
        task = type("Task", (), {"id": "task_x", "evaluation_metrics": [], "max_score": 10})()
        return ClaudeCodeJudgeAgent(task)

    @staticmethod
    def _result_message(text):
        msg = MagicMock()
        type(msg).__name__ = "ResultMessage"
        msg.result = text
        return msg

    def test_valid_json_result(self):
        agent = self._agent()
        payload = json.dumps({"score": 9, "feedback": "优秀", "dimension_scores": {"完成度": 9}})
        config = {"model": "m", "timeout_seconds": 10, "max_turns": 2}

        with patch("app.services.claude_code_judge_agent._run_with_prompt") as mock_run:
            async def fake_run(options, prompt):
                return payload

            mock_run.side_effect = fake_run
            with patch("app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}):
                result = agent._query_agent(config, "sys", "user prompt")
        assert result["score"] == 9
        assert result["dimension_scores"]["完成度"] == 9

    def test_invalid_then_retry(self):
        import asyncio as aio

        agent = self._agent()
        bad = "我认为这个会话完成得不错"
        good = json.dumps({"score": 7, "feedback": "良好", "dimension_scores": {}})
        config = {"model": "m", "timeout_seconds": 10, "max_turns": 2}
        responses = iter([bad, good])

        with patch("app.services.claude_code_judge_agent._run_with_prompt") as mock_run:
            async def fake_run(options, prompt):
                return next(responses)

            mock_run.side_effect = fake_run
            with patch("app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}):
                result = agent._query_agent(config, "sys", "user prompt")
        assert result["score"] == 7
        assert mock_run.call_count == 2

    def test_all_invalid_raises(self):
        import asyncio as aio

        agent = self._agent()
        config = {"model": "m", "timeout_seconds": 10, "max_turns": 2}

        with patch("app.services.claude_code_judge_agent._run_with_prompt") as mock_run:
            async def fake_run(options, prompt):
                return "始终无法解析的文本"

            mock_run.side_effect = fake_run
            with patch("app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}):
                with pytest.raises(RuntimeError, match="无法解析"):
                    agent._query_agent(config, "sys", "user prompt")

    def test_empty_response_raises(self):
        agent = self._agent()
        config = {"model": "m", "timeout_seconds": 10, "max_turns": 2}

        with patch("app.services.claude_code_judge_agent._run_with_prompt") as mock_run:
            async def fake_run(options, prompt):
                return ""

            mock_run.side_effect = fake_run
            with patch("app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env", return_value={}):
                with pytest.raises(RuntimeError, match="无返回内容"):
                    agent._query_agent(config, "sys", "user prompt")
