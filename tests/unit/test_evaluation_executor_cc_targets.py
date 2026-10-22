"""EvaluationExecutor._build_cc_targets_prompt 单元测试（CC 裁判 E2BIG 修复）

claude-agent-sdk 以 --system-prompt argv 传 system_prompt，Linux 单参数上限
131072B；CC 引擎的 targets 待评数据改为小内容并入 prompt、大内容落盘
（裁判 Read 阅读），并与超长会话 session_file 复用去重。
"""
import re
from pathlib import Path

import pytest

from app.services.evaluation_executor import (
    CC_TARGETS_INLINE_LIMIT_CHARS,
    EvaluationExecutor,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestBuildCcTargetsPrompt:
    def test_empty_targets_returns_empty(self):
        assert EvaluationExecutor._build_cc_targets_prompt({}, "task1", {}) == ""
        assert EvaluationExecutor._build_cc_targets_prompt(None, "task1", {}) == ""

    def test_small_content_inline(self):
        targets = {"k": {"name": "输出", "content": "短内容正文", "is_empty": False}}
        prompt = EvaluationExecutor._build_cc_targets_prompt(
            targets, "task1", {"_dataset_item": {"id": "d1"}}
        )
        assert "## 待评测目标内容" in prompt
        assert "### 输出" in prompt
        assert "短内容正文" in prompt
        assert "本地文件" not in prompt

    def test_large_content_written_to_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        content = "长" * (CC_TARGETS_INLINE_LIMIT_CHARS + 1)
        targets = {"k": {"name": "输出", "content": content, "is_empty": False}}
        prompt = EvaluationExecutor._build_cc_targets_prompt(targets, "task1", {"idx": 3})
        assert "本地文件" in prompt
        assert "Read" in prompt
        path = re.search(r"本地文件：(\S+)", prompt).group(1)
        assert Path(path).read_text(encoding="utf-8") == content

    def test_reuse_session_file_for_same_content(self, tmp_path, monkeypatch):
        """与超长会话落盘内容一致时复用 session_file，不重复写盘"""
        monkeypatch.chdir(tmp_path)
        session_md = "会" * (CC_TARGETS_INLINE_LIMIT_CHARS + 1)
        session_file = str(Path(tmp_path / "session.md"))
        Path(session_file).write_text(session_md, encoding="utf-8")
        targets = {"k": {"name": "输出", "content": session_md, "is_empty": False}}
        prompt = EvaluationExecutor._build_cc_targets_prompt(
            targets, "task1", {}, session_file=session_file, session_markdown=session_md
        )
        assert session_file in prompt
        assert not Path("uploads/judge_sessions").exists()

    def test_boundary_chars_inline(self, tmp_path, monkeypatch):
        """恰好阈值（30000 字符）走 inline 不落盘（> 判断语义）"""
        monkeypatch.chdir(tmp_path)
        content = "界" * CC_TARGETS_INLINE_LIMIT_CHARS
        targets = {"k": {"name": "输出", "content": content, "is_empty": False}}
        prompt = EvaluationExecutor._build_cc_targets_prompt(targets, "task1", {"idx": 0})
        assert content in prompt
        assert "本地文件" not in prompt
        assert not Path("uploads/judge_sessions").exists()

    def test_empty_and_blank_targets_skipped(self):
        targets = {
            "a": {"name": "输出", "content": "", "is_empty": False},
            "b": {"name": "空目标", "content": "有内容但标记为空", "is_empty": True},
        }
        assert EvaluationExecutor._build_cc_targets_prompt(targets, "task1", {}) == ""

    def test_mixed_inline_and_file(self, tmp_path, monkeypatch):
        """小内容并入 + 大内容落盘可共存于同一 prompt"""
        monkeypatch.chdir(tmp_path)
        big = "大" * (CC_TARGETS_INLINE_LIMIT_CHARS + 1)
        targets = {
            "a": {"name": "描述", "content": "短的需求描述", "is_empty": False},
            "b": {"name": "输出", "content": big, "is_empty": False},
        }
        prompt = EvaluationExecutor._build_cc_targets_prompt(
            targets, "task1", {"idx": 1}
        )
        assert "短的需求描述" in prompt
        assert "本地文件" in prompt
