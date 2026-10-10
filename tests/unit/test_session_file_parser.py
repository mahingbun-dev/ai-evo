"""
会话文件解析器单元测试

覆盖 Claude Code .jsonl 解析、Langfuse trace 解析、分发入口、markdown 渲染与摘要。
"""
import json

import pytest

from app.services.session_file_parser import (
    detect_and_parse,
    parse_claude_code_jsonl,
    parse_langfuse_trace,
    render_session_markdown,
    summarize_first_user_message,
)


def _write_lines(tmp_path, name, lines):
    """将字符串行列表写入临时文件，返回路径"""
    target = tmp_path / name
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(target)


def _claude_line(
    line_type,
    content,
    sid="sess-main",
    ts=None,
    is_sidechain=False,
    model=None,
    **extra,
):
    """构造一行 claude code 会话 JSON"""
    message = {"role": line_type, "content": content}
    if model:
        message["model"] = model
    row = {
        "parentUuid": None,
        "isSidechain": is_sidechain,
        "userType": "external",
        "cwd": "/tmp/project",
        "sessionId": sid,
        "version": "1.0.66",
        "gitBranch": "main",
        "type": line_type,
        "message": message,
        "uuid": f"uuid-{line_type}-{ts}",
        "timestamp": ts,
    }
    row.update(extra)
    return json.dumps(row)


FULL_SESSION_LINES = [
    _claude_line("user", "帮我查看当前目录文件", ts="2025-01-01T10:00:00.000Z"),
    _claude_line(
        "assistant",
        [
            {"type": "thinking", "thinking": "用户想看目录，我调用 ls"},
            {"type": "text", "text": "我来查看目录"},
            {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "ls"}},
        ],
        ts="2025-01-01T10:00:05.000Z",
        model="claude-sonnet-4-5",
    ),
    _claude_line(
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01",
                "content": "file_a.py\nfile_b.py",
                "is_error": False,
            }
        ],
        ts="2025-01-01T10:00:10.000Z",
    ),
    # 子代理消息：默认排除
    _claude_line(
        "assistant",
        [{"type": "text", "text": "子代理回复"}],
        sid="sess-main",
        ts="2025-01-01T10:00:12.000Z",
        is_sidechain=True,
    ),
    # isMeta 消息：跳过
    _claude_line("user", "Caveat: 命令包装", ts="2025-01-01T10:00:13.000Z", isMeta=True),
    # 其他 sessionId 的消息：丢弃
    _claude_line("user", "另一会话消息", sid="sess-other", ts="2025-01-01T10:00:14.000Z"),
    _claude_line("user", "谢谢，第二个问题", ts="2025-01-01T10:01:30.000Z"),
    # summary 行
    json.dumps({"type": "summary", "summary": "目录查询会话", "leafUuid": "uuid-x"}),
    # 坏行
    "{ this is not valid json",
]


class TestParseClaudeCodeJsonl:
    """Claude Code .jsonl 解析测试"""

    def test_full_session_parse(self, tmp_path):
        """多轮对话 + tool_use/tool_result/thinking + summary + 坏行 + 混合行过滤"""
        path = _write_lines(tmp_path, "session.jsonl", FULL_SESSION_LINES)
        record = parse_claude_code_jsonl(path)

        assert record["session_id"] == "sess-main"
        assert record["source"] == "claude_code"
        # 默认排除 sidechain / isMeta / 其他 sessionId，共 4 条
        assert len(record["messages"]) == 4
        roles = [m["role"] for m in record["messages"]]
        assert roles == ["user", "assistant", "user", "user"]

        # metadata 保留原始元数据
        assert record["metadata"]["cwd"] == "/tmp/project"
        assert record["metadata"]["version"] == "1.0.66"
        assert record["metadata"]["gitBranch"] == "main"
        assert record["metadata"]["summary"] == "目录查询会话"
        assert record["metadata"]["skipped_lines"] == 1
        assert record["metadata"]["dropped_session_ids"] == ["sess-other"]
        assert record["metadata"]["models"] == ["claude-sonnet-4-5"]

        # assistant 消息保留 thinking/text/tool_use 三块
        assistant_blocks = record["messages"][1]["content"]
        assert [b["type"] for b in assistant_blocks] == ["thinking", "text", "tool_use"]
        assert assistant_blocks[2]["name"] == "Bash"
        assert assistant_blocks[2]["input"] == {"command": "ls"}

        # tool_result is_error 缺省为 False
        tool_result = record["messages"][2]["content"][0]
        assert tool_result["tool_use_id"] == "toolu_01"
        assert tool_result["is_error"] is False

    def test_sidechain_included_when_requested(self, tmp_path):
        """include_sidechain=True 时包含子代理消息"""
        path = _write_lines(tmp_path, "session.jsonl", FULL_SESSION_LINES)
        record = parse_claude_code_jsonl(path, include_sidechain=True)
        texts = [
            b["text"]
            for m in record["messages"]
            for b in m["content"]
            if b.get("type") == "text"
        ]
        assert "子代理回复" in texts
        assert len(record["messages"]) == 5

    def test_stats(self, tmp_path):
        """turns / message_count / tool_call_count / duration_seconds 计算"""
        path = _write_lines(tmp_path, "session.jsonl", FULL_SESSION_LINES)
        stats = parse_claude_code_jsonl(path)["stats"]
        # 真实用户消息 2 条（tool_result 回传不计入）
        assert stats["turns"] == 2
        assert stats["message_count"] == 4
        assert stats["tool_call_count"] == 1
        # 首条 10:00:00 ~ 末条 10:01:30
        assert stats["duration_seconds"] == 90.0

    def test_time_range(self, tmp_path):
        """started_at / ended_at 为 ISO 字符串"""
        path = _write_lines(tmp_path, "session.jsonl", FULL_SESSION_LINES)
        record = parse_claude_code_jsonl(path)
        assert record["started_at"] is not None
        assert record["started_at"].startswith("2025-01-01T10:00:00")
        assert record["ended_at"].startswith("2025-01-01T10:01:30")

    def test_main_session_is_most_frequent(self, tmp_path):
        """出现次数最多的 sessionId 为主会话"""
        lines = [
            _claude_line("user", "主会话 1", sid="sess-a", ts="2025-01-01T10:00:00.000Z"),
            _claude_line("user", "杂音 1", sid="sess-b", ts="2025-01-01T10:00:01.000Z"),
            _claude_line("user", "杂音 2", sid="sess-b", ts="2025-01-01T10:00:02.000Z"),
            _claude_line("assistant", "主会话回复 1", sid="sess-a", ts="2025-01-01T10:00:03.000Z"),
            _claude_line("user", "主会话 2", sid="sess-a", ts="2025-01-01T10:00:04.000Z"),
        ]
        path = _write_lines(tmp_path, "session.jsonl", lines)
        record = parse_claude_code_jsonl(path)

        # sess-a 出现 3 次为主会话，sess-b 出现 2 次被丢弃
        assert record["session_id"] == "sess-a"
        texts = [b["text"] for m in record["messages"] for b in m["content"]]
        assert "主会话 1" in texts and "主会话 2" in texts
        assert "杂音 1" not in texts
        assert record["metadata"]["dropped_session_ids"] == ["sess-b"]

    def test_bad_lines_only_raises(self, tmp_path):
        """有效消息为 0 时抛 ValueError，含文件名与坏行数"""
        path = _write_lines(tmp_path, "broken.jsonl", ["not json 1", "not json 2"])
        with pytest.raises(ValueError) as exc_info:
            parse_claude_code_jsonl(path)
        assert "broken.jsonl" in str(exc_info.value)
        assert "2" in str(exc_info.value)

    def test_system_line_ignored(self, tmp_path):
        """system 行不产生消息，也不计坏行"""
        lines = [
            json.dumps({"type": "system", "content": "hook 事件", "sessionId": "s1"}),
            _claude_line("user", "你好", sid="s1", ts="2025-01-01T10:00:00.000Z"),
        ]
        path = _write_lines(tmp_path, "session.jsonl", lines)
        record = parse_claude_code_jsonl(path)
        assert len(record["messages"]) == 1
        assert "skipped_lines" not in record["metadata"]


def _generation(
    input_value,
    output_value,
    model="gpt-4o",
    start="2025-02-01T09:00:00.000Z",
    end="2025-02-01T09:00:05.000Z",
):
    return {
        "id": "gen-1",
        "name": "chat-gen",
        "model": model,
        "startTime": start,
        "endTime": end,
        "input": input_value,
        "output": output_value,
    }


def _write_json(tmp_path, name, data):
    target = tmp_path / name
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(target)


class TestParseLangfuseTrace:
    """Langfuse trace 解析测试"""

    def test_single_trace_with_generations(self, tmp_path):
        """单 trace：generations 内 messages input/output 各转一条消息"""
        trace = {
            "id": "trace-1",
            "name": "demo",
            "tags": ["eval"],
            "userId": "u-1",
            "sessionId": "lf-session-1",
            "timestamp": "2025-02-01T09:00:00.000Z",
            "generations": [
                _generation(
                    input_value={
                        "messages": [
                            {"role": "user", "content": "你好"},
                            {"role": "user", "content": "补充一句"},
                        ]
                    },
                    output_value={"messages": [{"role": "assistant", "content": "你好，我是助手"}]},
                )
            ],
        }
        records = parse_langfuse_trace(_write_json(tmp_path, "trace.json", trace))

        assert len(records) == 1
        record = records[0]
        assert record["session_id"] == "trace-1"
        assert record["source"] == "langfuse"
        assert len(record["messages"]) == 2
        assert record["messages"][0]["role"] == "user"
        assert record["messages"][0]["content"] == [
            {"type": "text", "text": "你好"},
            {"type": "text", "text": "补充一句"},
        ]
        assert record["messages"][1]["role"] == "assistant"

        # metadata 保留 name/tags/userId/sessionId 与 generation 元信息
        assert record["metadata"]["name"] == "demo"
        assert record["metadata"]["tags"] == ["eval"]
        assert record["metadata"]["userId"] == "u-1"
        assert record["metadata"]["sessionId"] == "lf-session-1"
        assert record["metadata"]["generations"][0]["model"] == "gpt-4o"

        # stats：1 轮 user，1 条 assistant
        assert record["stats"]["turns"] == 1
        assert record["stats"]["message_count"] == 2
        assert record["stats"]["duration_seconds"] == 5.0

    def test_trace_array(self, tmp_path):
        """trace 数组输入返回多个 SessionRecord，保持顺序"""
        traces = [
            {"id": "t-a", "generations": [_generation("问题 A", "回答 A")]},
            {"id": "t-b", "generations": [_generation("问题 B", "回答 B")]},
        ]
        records = parse_langfuse_trace(_write_json(tmp_path, "traces.json", traces))
        assert [r["session_id"] for r in records] == ["t-a", "t-b"]
        assert records[0]["messages"][0]["content"][0]["text"] == "问题 A"

    def test_traces_wrapper(self, tmp_path):
        """{"traces": [...]} 包装输入"""
        data = {"traces": [{"id": "t-w", "generations": [_generation("问", "答")]}]}
        records = parse_langfuse_trace(_write_json(tmp_path, "wrapped.json", data))
        assert len(records) == 1
        assert records[0]["session_id"] == "t-w"

    def test_plain_text_input_output(self, tmp_path):
        """input/output 为纯文本时转为对应角色单条 text 消息"""
        trace = {"id": "t-plain", "generations": [_generation("纯文本问题", "纯文本回答")]}
        records = parse_langfuse_trace(_write_json(tmp_path, "plain.json", trace))
        messages = records[0]["messages"]
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == [{"type": "text", "text": "纯文本问题"}]
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == [{"type": "text", "text": "纯文本回答"}]

    def test_arbitrary_json_truncated_to_text(self, tmp_path):
        """input 为其他 JSON 结构时序列化为 text 并截断到 2000 字符"""
        big_value = {"messages": "not-a-list", "payload": "x" * 3000}
        trace = {"id": "t-json", "generations": [_generation(big_value, None)]}
        records = parse_langfuse_trace(_write_json(tmp_path, "jsonval.json", trace))
        block = records[0]["messages"][0]["content"][0]
        assert block["type"] == "text"
        assert "已截断，原长度" in block["text"]
        assert len(block["text"]) < 2100

    def test_fallback_to_top_level_input_output(self, tmp_path):
        """generations 产不出消息时回退顶层 input/output"""
        trace = {
            "id": "t-fallback",
            "input": {"messages": [{"role": "user", "content": "顶层问题"}]},
            "output": {"messages": [{"role": "assistant", "content": "顶层回答"}]},
            "generations": [{"id": "gen-empty", "input": None, "output": None}],
        }
        records = parse_langfuse_trace(_write_json(tmp_path, "fallback.json", trace))
        messages = records[0]["messages"]
        assert len(messages) == 2
        assert messages[0]["content"][0]["text"] == "顶层问题"
        assert messages[1]["role"] == "assistant"

    def test_observations_generation_fallback(self, tmp_path):
        """observations 中 type == GENERATION 的项作为消息来源"""
        trace = {
            "id": "t-obs",
            "observations": [
                {"type": "SPAN", "name": "retrieval"},
                {
                    "type": "GENERATION",
                    "model": "gpt-4o-mini",
                    "startTime": "2025-02-01T09:00:00.000Z",
                    "endTime": "2025-02-01T09:00:03.000Z",
                    "input": {"messages": [{"role": "user", "content": "obs 问题"}]},
                    "output": {"messages": [{"role": "assistant", "content": "obs 回答"}]},
                },
            ],
        }
        records = parse_langfuse_trace(_write_json(tmp_path, "obs.json", trace))
        messages = records[0]["messages"]
        assert len(messages) == 2
        assert messages[0]["content"][0]["text"] == "obs 问题"
        assert records[0]["metadata"]["generations"][0]["model"] == "gpt-4o-mini"

    def test_messages_sorted_by_timestamp(self, tmp_path):
        """消息按时间戳排序"""
        trace = {
            "id": "t-sort",
            "generations": [
                _generation(
                    "晚的问题",
                    "晚的回答",
                    start="2025-02-01T10:00:00.000Z",
                    end="2025-02-01T10:00:05.000Z",
                ),
                _generation(
                    "早的问题",
                    "早的回答",
                    start="2025-02-01T09:00:00.000Z",
                    end="2025-02-01T09:00:05.000Z",
                ),
            ],
        }
        records = parse_langfuse_trace(_write_json(tmp_path, "sort.json", trace))
        texts = [
            b["text"] for m in records[0]["messages"] for b in m["content"]
        ]
        assert texts.index("早的问题") < texts.index("晚的问题")


class TestDetectAndParse:
    """分发入口测试"""

    def test_jsonl_dispatch(self, tmp_path):
        """.jsonl → Claude Code 解析"""
        path = _write_lines(tmp_path, "s.jsonl", FULL_SESSION_LINES)
        records = detect_and_parse(path)
        assert len(records) == 1
        assert records[0]["source"] == "claude_code"

    def test_json_dispatch(self, tmp_path):
        """.json → Langfuse 解析"""
        path = _write_json(
            tmp_path, "t.json", {"traces": [{"id": "t-1", "generations": [_generation("问", "答")]}]}
        )
        records = detect_and_parse(path)
        assert len(records) == 1
        assert records[0]["source"] == "langfuse"

    def test_unknown_extension_raises(self, tmp_path):
        """未知扩展名抛 ValueError"""
        target = tmp_path / "session.txt"
        target.write_text("hello", encoding="utf-8")
        with pytest.raises(ValueError):
            detect_and_parse(str(target))


def _build_session():
    """构造带工具调用的会话记录用于渲染测试"""
    return {
        "session_id": "sess-render",
        "source": "claude_code",
        "started_at": "2025-01-01T10:00:00+00:00",
        "ended_at": "2025-01-01T10:01:30+00:00",
        "metadata": {},
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "查看目录"}],
                "timestamp": "2025-01-01T10:00:00.000Z",
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "调用 ls"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
                "timestamp": "2025-01-01T10:00:05.000Z",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": ["a.py", {"file": "b.py"}],
                        "is_error": False,
                    }
                ],
                "timestamp": "2025-01-01T10:00:10.000Z",
            },
        ],
        "stats": {
            "turns": 1,
            "message_count": 3,
            "tool_call_count": 1,
            "duration_seconds": 90.0,
        },
    }


class TestRenderSessionMarkdown:
    """markdown 渲染测试"""

    def test_header_and_sequence(self):
        """头部信息与序号连续递增（user/assistant/tool_result 消息均进序号）"""
        markdown = render_session_markdown(_build_session())
        assert "# 会话记录（来源：Claude Code）" in markdown
        assert "- 会话 ID: sess-render" in markdown
        assert "- 轮次: 1，消息数: 3，工具调用: 1" in markdown
        assert "## [1] 👤 用户" in markdown
        assert "## [2] 🤖 助手" in markdown
        assert "## [3] 👤 用户" in markdown

    def test_tool_rendering(self):
        """tool_use 渲染为 JSON 代码块，thinking 为引用块"""
        markdown = render_session_markdown(_build_session())
        assert "### 🔧 工具调用: Bash" in markdown
        assert '"command": "ls"' in markdown
        assert "💭 思考：" in markdown
        # tool_result content 为 list 时逐项渲染，dict 序列化为 JSON
        assert "a.py" in markdown
        assert '"file": "b.py"' in markdown

    def test_error_tool_result_annotated(self):
        """is_error 的工具结果标注 ❌ 错误"""
        session = _build_session()
        session["messages"][2]["content"][0]["is_error"] = True
        markdown = render_session_markdown(session)
        assert "### 📋 工具结果 ❌ 错误" in markdown

    def test_truncation(self):
        """单个内容块超过 max_block_chars 时截断并标注原长度"""
        session = _build_session()
        session["messages"][0]["content"][0]["text"] = "长" * 100
        markdown = render_session_markdown(session, max_block_chars=20)
        assert "长" * 20 in markdown
        assert "已截断，原长度 100 字符" in markdown
        assert "长" * 100 not in markdown

    def test_empty_session(self):
        """空会话仅输出头部信息"""
        session = {
            "session_id": "s-empty",
            "source": "langfuse",
            "started_at": None,
            "ended_at": None,
            "metadata": {},
            "messages": [],
            "stats": {"turns": 0, "message_count": 0, "tool_call_count": 0, "duration_seconds": None},
        }
        markdown = render_session_markdown(session)
        assert "会话记录（来源：Langfuse）" in markdown
        assert "- 时间: 未知" in markdown
        assert "## [" not in markdown


class TestSummarizeFirstUserMessage:
    """首条真实用户消息摘要测试"""

    def test_skips_tool_result_messages(self):
        """跳过 tool_result 回传，取第一条真实用户消息"""
        session = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "结果", "is_error": False}],
                },
                {"role": "user", "content": [{"type": "text", "text": "真正的第一个问题"}]},
            ]
        }
        assert summarize_first_user_message(session) == "真正的第一个问题"

    def test_truncation_with_limit(self):
        """超限截断并追加省略号"""
        session = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "字" * 100}]}
            ]
        }
        summary = summarize_first_user_message(session, limit=10)
        assert summary == "字" * 10 + "..."

    def test_no_user_message_returns_empty(self):
        """无真实用户消息返回空字符串"""
        assert summarize_first_user_message({"messages": []}) == ""
        assert summarize_first_user_message(
            {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]}
        ) == ""

    def test_slash_command_wrapping_humanized(self):
        """斜杠命令 XML 包装转可读的 /命令名 形式"""
        session = {
            "messages": [
                {
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "<command-message>anc-wms:testcase-generation</command-message>\n"
                            "<command-name>/anc-wms:testcase-generation</command-name>\n"
                            "<command-args>/Users/dev/需求.docx</command-args>"
                        ),
                    }],
                }
            ]
        }
        summary = summarize_first_user_message(session)
        assert summary == "/anc-wms:testcase-generation /Users/dev/需求.docx"
        assert "<command" not in summary

    def test_slash_command_with_trailing_text(self):
        """命令包装外的用户正文拼入摘要"""
        session = {
            "messages": [
                {
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": (
                            "<command-message>x</command-message>\n"
                            "<command-name>/cmd</command-name>\n"
                            "请帮我 review 一下这个需求"
                        ),
                    }],
                }
            ]
        }
        summary = summarize_first_user_message(session)
        assert summary == "/cmd 请帮我 review 一下这个需求"

    def test_plain_text_untouched(self):
        """普通文本消息不做任何标签处理"""
        session = {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "  多  行  文本 "}]}]
        }
        assert summarize_first_user_message(session) == "多 行 文本"
