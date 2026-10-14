"""脚本 MCP 工具化单元测试（docs/design/pre-judge-scripts.md 9.3 最小闭环）

覆盖三层：
1. 进程内单元：inputSchema 生成、工具清单收集（含 DB 降级）、工具执行（stdin
   组装 / 文件落地 / 注册表直纳脚本）、JSON-RPC 消息分发、stdio server 配置生成
2. 配置开关：enable_script_mcp 持久化、get_script_mcp_servers 按开关返回、
   ClaudeCodeAdapter 按开关注入 ClaudeAgentOptions.mcp_servers
3. 子进程集成：spawn `python -m app.services.script_mcp_server`，走
   initialize → notifications/initialized → tools/list → tools/call 全流程断言
"""
import json
import os
import select
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.pre_judge_models import PreJudgeScript
from app.services import script_mcp_server
from app.services.claude_code_config_service import ClaudeCodeConfigService
from app.services.pre_judge_script_service import (
    BUILTIN_SCRIPT_DEFS,
    ensure_builtin_scripts_migrated,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """sqlite 内存库 + 迁移内置脚本（统一子进程执行后 MCP 单一来源），并替换 SessionLocal

    StaticPool 保证多个 session 共享同一内存库（默认池下每次连接是独立空库）；
    SCRIPT_UPLOAD_BASE / SCRIPT_RUN_BASE 指向临时目录（迁移拷源码、试跑落产物）。
    """
    from app.services import pre_judge_script_service as script_service
    from app.services import pre_judge_runner

    upload_base = tmp_path / "pre_judge_scripts"
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")

    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    ensure_builtin_scripts_migrated(Session())
    monkeypatch.setattr("app.db.session.SessionLocal", Session)
    return SimpleNamespace(session=Session, upload_base=upload_base)
from app.services.script_mcp_server import (
    DEFAULT_PROTOCOL_VERSION,
    MCP_SERVER_KEY,
    build_input_schema,
    collect_tools,
    execute_tool,
    get_stdio_server_config,
    handle_message,
)

PROJECT_ROOT = Path(script_mcp_server.__file__).resolve().parents[2]


# ==================== 1. 进程内单元 ====================

class TestBuildInputSchema:
    def test_from_builtin_input_fields(self):
        """内置脚本 input_fields → string 属性 + required + 可选 params"""
        definition = BUILTIN_SCRIPT_DEFS["allure_vs_cases"]
        schema = build_input_schema(definition["input_fields"])
        assert schema["type"] == "object"
        props = schema["properties"]
        assert props["cases_file"]["type"] == "string"
        assert "cases.json" in props["cases_file"]["description"]
        assert "value_type=hub_file" in props["allure_results"]["description"]
        assert schema["required"] == ["cases_file", "allure_results"]
        # params 可选（不在 required 中）
        assert props["params"]["type"] == "object"
        assert "params" not in schema["required"]

    def test_empty_fields(self):
        """无 input_fields → 仅 params 属性、无 required"""
        schema = build_input_schema(None)
        assert schema["properties"] == {
            "params": {
                "type": "object",
                "description": "脚本参数（透传 stdin.params，可选）",
                "additionalProperties": True,
            }
        }
        assert "required" not in schema

    def test_meta_not_dict(self):
        """input_fields 元数据退化为字符串时仍可生成"""
        schema = build_input_schema({"f": "某字段"})
        assert schema["properties"]["f"]["type"] == "string"


class TestCollectTools:
    def test_contains_builtin_scripts(self, seeded_db):
        """统一落库后：补种的内置脚本以 DB 记录暴露为工具"""
        names = {t["name"] for t in collect_tools()}
        assert "allure_vs_cases" in names
        tool = next(t for t in collect_tools() if t["name"] == "allure_vs_cases")
        assert "inputSchema" in tool and tool["inputSchema"]["type"] == "object"
        assert "一致性" in tool["description"] or "比对" in tool["description"]

    def test_db_failure_degrades_to_empty(self, monkeypatch):
        """DB 不可达时降级为空工具列表，不抛异常（协议层不受影响）"""
        monkeypatch.setattr(
            "app.db.session.SessionLocal",
            MagicMock(side_effect=RuntimeError("db down")),
        )
        assert collect_tools() == []

    def test_db_only_rows_are_the_source(self, monkeypatch):
        """DB 记录是唯一工具来源（含补种之外的普通 builtin 记录）"""
        row = MagicMock()
        row.id = "pjs_dbonly01"
        row.name = "db_only_script"
        row.entrypoint = "app.scripts_builtin.allure_vs_cases.run"
        row.timeout_seconds = 60
        row.description = "普通进程内脚本"
        row.output_schema = {}
        row.input_fields = {"f": {"value_type": "inline", "description": "字段"}}
        fake_session = MagicMock()
        fake_session.query.return_value.filter.return_value.all.return_value = [row]
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        monkeypatch.setattr(
            "app.db.session.SessionLocal", MagicMock(return_value=fake_session)
        )
        names = {t["name"] for t in collect_tools()}
        assert names == {"db_only_script"}


class TestExecuteTool:
    def test_script_pkg_end_to_end(self, seeded_db):
        """脚本包全链路（统一子进程执行）：stdin 组装 → wrapper 执行 → 标准输出
        （DB 行 exec_type=script + 脚本目录 main.py）"""
        pkg = Path(seeded_db.upload_base) / "pjs_fakeecho"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(
            "def run(stdin):\n"
            "    return {\n"
            "        'status': 'ok',\n"
            "        'output': {'echo': open(stdin['fields']['msg'], encoding='utf-8').read()},\n"
            "        'files': {}, 'summary': 'echo done',\n"
            "        'finish_reason': 'completed', 'error': None,\n"
            "    }\n",
            encoding="utf-8")
        with seeded_db.session() as db:
            db.add(PreJudgeScript(id="pjs_fakeecho", name="mcp_fake_echo", exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            db.commit()
        result = execute_tool("mcp_fake_echo", {"msg": "hello-mcp"})
        assert result["status"] == "ok"
        assert result["output"]["echo"] == "hello-mcp"
        assert result["finish_reason"] == "completed"

    def test_field_file_object_naming(self, seeded_db):
        """{filename, content} 对象：按指定文件名落盘（allure 单结果需 *-result.json 后缀）"""
        arguments = {
            "cases_file": json.dumps([{"name": "login_ok", "expected": "登录成功", "priority": "P0"}]),
            "allure_results": {
                "filename": "run-1-result.json",
                "content": json.dumps({"name": "login_ok", "status": "passed"}),
            },
        }
        result = execute_tool("allure_vs_cases", arguments)
        assert result["status"] == "ok"
        assert result["output"]["consistency_rate"] == 1.0
        assert result["output"]["mismatched_count"] == 0

    def test_missing_required_field_returns_fail(self, seeded_db):
        """缺少声明字段 → 结构化 fail（不是异常），裁判可见失败事实"""
        result = execute_tool("allure_vs_cases", {})
        assert result["status"] == "fail"
        assert "cases_file" in (result.get("error") or "")

    def test_unknown_tool_raises(self):
        from app.services.script_mcp_server import UnknownToolError

        with pytest.raises(UnknownToolError, match="Unknown tool"):
            execute_tool("no_such_tool", {})

    def test_existing_path_passthrough(self, seeded_db, tmp_path):
        """入参已是本地路径 → 原样透传不重写"""
        case_file = tmp_path / "cases.json"
        case_file.write_text(
            json.dumps([{"name": "c1", "expected": "e", "priority": "P1"}]),
            encoding="utf-8",
        )
        allure_file = tmp_path / "a-result.json"
        allure_file.write_text(json.dumps({"name": "c1", "status": "passed"}), encoding="utf-8")
        result = execute_tool("allure_vs_cases", {
            "cases_file": str(case_file), "allure_results": str(allure_file),
        })
        assert result["status"] == "ok"
        assert result["output"]["consistency_rate"] == 1.0


class TestHandleMessage:
    def test_initialize_echoes_protocol_version(self):
        resp = handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
        })
        assert resp["result"]["protocolVersion"] == "2025-06-18"
        assert resp["result"]["serverInfo"]["name"] == MCP_SERVER_KEY
        assert "tools" in resp["result"]["capabilities"]

    def test_initialize_default_protocol_version(self):
        resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert resp["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION

    def test_initialized_notification_no_response(self):
        assert handle_message({
            "jsonrpc": "2.0", "method": "notifications/initialized"
        }) is None

    def test_ping(self):
        assert handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {
            "jsonrpc": "2.0", "id": 2, "result": {}
        }

    def test_tools_list(self, seeded_db):
        resp = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "allure_vs_cases" in names

    def test_tools_call_returns_stdout_json(self, seeded_db):
        pkg = Path(seeded_db.upload_base) / "pjs_fake2"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "main.py").write_text(
            "def run(stdin):\n"
            "    return {'status': 'ok', 'output': {'x': 1}, 'summary': 's',\n"
            "            'finish_reason': 'completed', 'error': None, 'files': {}}\n",
            encoding="utf-8")
        with seeded_db.session() as db:
            db.add(PreJudgeScript(id="pjs_fake2", name="mcp_fake2", exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            db.commit()
        resp = handle_message({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "mcp_fake2", "arguments": {}},
        })
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        # execute_script 会补齐 script_id/script_name/duration_ms 元数据键，断言核心标准输出键
        assert payload["status"] == "ok"
        assert payload["output"] == {"x": 1}
        assert payload["summary"] == "s"
        assert payload["finish_reason"] == "completed"
        assert payload["error"] is None

    def test_tools_call_unknown_tool_protocol_error(self):
        resp = handle_message({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        })
        assert resp["error"]["code"] == -32602
        assert "nope" in resp["error"]["message"]

    def test_unknown_method(self):
        resp = handle_message({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        assert resp["error"]["code"] == -32601

    def test_non_dict_message(self):
        assert handle_message("bad")["error"]["code"] == -32600

    def test_serve_parse_error_then_request(self):
        """serve 循环：坏行回 -32700，好行正常处理，通知不回包"""
        import io

        lines = [
            "not-json\n",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n",
        ]
        out = io.StringIO()
        script_mcp_server.serve(instream=io.StringIO("".join(lines)), outstream=out)
        responses = [json.loads(x) for x in out.getvalue().splitlines()]
        assert len(responses) == 2  # 通知不产生响应
        assert responses[0]["error"]["code"] == -32700
        assert responses[1]["result"] == {}


class TestStdioServerConfig:
    def test_config_shape(self):
        config = get_stdio_server_config()
        assert config["type"] == "stdio"
        assert config["command"] == sys.executable
        assert config["args"] == ["-m", "app.services.script_mcp_server"]
        # PYTHONPATH 指向项目根，使 -m 导入不依赖子进程 cwd
        pythonpath = Path(config["env"]["PYTHONPATH"])
        assert (pythonpath / "app" / "services" / "script_mcp_server.py").is_file()


# ==================== 2. 配置开关与注入 ====================

SAMPLE_CONFIG = {
    "id": "ccc_test000001",
    "name": "默认配置",
    "base_url": "https://gw.example.com",
    "auth_token": "sk-abcdefghijklmnop",
    "model": "claude-sonnet-4-5",
    "timeout_seconds": 300,
    "max_turns": 30,
    "proxy_url": None,
    "extra_env": {},
    "enable_script_mcp": False,
}


@pytest.fixture(autouse=True)
def _reset_config_cache():
    ClaudeCodeConfigService.invalidate_cache()
    yield
    ClaudeCodeConfigService.invalidate_cache()


class TestEnableScriptMcpConfig:
    def _patch_active(self, monkeypatch, config):
        monkeypatch.setattr(
            ClaudeCodeConfigService, "get_active_config",
            classmethod(lambda cls: config),
        )

    def test_disabled_returns_none(self, monkeypatch):
        self._patch_active(monkeypatch, dict(SAMPLE_CONFIG, enable_script_mcp=False))
        assert ClaudeCodeConfigService.get_script_mcp_servers() is None

    def test_no_config_returns_none(self, monkeypatch):
        self._patch_active(monkeypatch, None)
        assert ClaudeCodeConfigService.get_script_mcp_servers() is None

    def test_enabled_returns_stdio_server(self, monkeypatch):
        self._patch_active(monkeypatch, dict(SAMPLE_CONFIG, enable_script_mcp=True))
        servers = ClaudeCodeConfigService.get_script_mcp_servers()
        assert list(servers.keys()) == [MCP_SERVER_KEY]
        assert servers[MCP_SERVER_KEY] == get_stdio_server_config()

    def test_save_config_persists_flag(self, monkeypatch):
        """save_config 落库 enable_script_mcp（沿用既有 save 测试的 DB mock 模式）"""
        row = MagicMock()
        row.auth_token = "sk-real"
        fake_session = MagicMock()
        fake_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = row
        fake_session.__enter__.return_value = fake_session
        fake_session.__exit__.return_value = False
        monkeypatch.setattr(
            "app.services.claude_code_config_service.SessionLocal",
            MagicMock(return_value=fake_session),
        )
        ClaudeCodeConfigService.save_config({
            "base_url": "https://gw.example.com",
            "auth_token": "sk-real",
            "model": "m",
            "enable_script_mcp": True,
        })
        assert row.enable_script_mcp is True


class TestAdapterInjection:
    """ClaudeCodeAdapter 按开关把脚本 MCP server 注入 ClaudeAgentOptions.mcp_servers"""

    @staticmethod
    def _run_adapter(monkeypatch, config):
        captured = {}

        class FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        async def fake_run(options, prompt):
            return '{"score": 8, "feedback": "ok", "dimension_scores": {}}'

        monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
        monkeypatch.setattr(
            "app.services.claude_code_judge_agent._run_with_prompt", fake_run
        )
        monkeypatch.setattr(
            "app.services.claude_code_config_service.ClaudeCodeConfigService.get_sdk_env",
            staticmethod(lambda: {}),
        )
        from app.services.harness_adapter import ClaudeCodeAdapter

        ClaudeCodeAdapter(config)._execute_raw("sys", "user", 10)
        return captured

    def test_injection_when_enabled(self, monkeypatch):
        config = {"model": "m", "enable_script_mcp": True}
        captured = self._run_adapter(monkeypatch, config)
        servers = captured["mcp_servers"]
        assert list(servers.keys()) == [MCP_SERVER_KEY]
        assert servers[MCP_SERVER_KEY]["type"] == "stdio"
        assert servers[MCP_SERVER_KEY]["command"] == sys.executable

    def test_no_injection_by_default(self, monkeypatch):
        """开关缺省（False）不注入，与历史行为一致"""
        captured = self._run_adapter(monkeypatch, {"model": "m"})
        assert "mcp_servers" not in captured


# ==================== 3. 子进程集成（spawn server 全流程） ====================

class TestSpawnedServerProcess:
    """spawn `python -m app.services.script_mcp_server` 子进程走 MCP 全流程"""

    @pytest.fixture()
    def server(self, tmp_path):
        # 统一落库后子进程从 DB 读脚本：用临时 sqlite 文件库 + DATABASE_URL 覆盖 .env
        db_file = tmp_path / "mcp_server_test.db"
        engine = create_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as seed_session:
            ensure_builtin_scripts_migrated(seed_session)
        engine.dispose()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        env["DATABASE_URL"] = f"sqlite:///{db_file}"
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.services.script_mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            env=env,
            text=True,
            bufsize=1,
        )
        yield proc
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _request(self, proc, payload, timeout=60):
        """发送一条请求并读取一行响应（select 限时，防挂死）"""
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        assert ready, f"server 无响应: {payload.get('method')}"
        line = proc.stdout.readline()
        assert line.strip(), "server 返回空响应"
        return json.loads(line)

    def test_full_mcp_flow(self, server):
        request_id = uuid.uuid4().hex

        # 1) initialize
        resp = self._request(server, {
            "jsonrpc": "2.0", "id": request_id, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        })
        assert resp["id"] == request_id
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert resp["result"]["serverInfo"]["name"] == MCP_SERVER_KEY
        assert "tools" in resp["result"]["capabilities"]

        # 2) notifications/initialized（通知不回包；下一响应应直接是 tools/list 的）
        server.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized"
        }) + "\n")
        server.stdin.flush()

        # 3) tools/list
        resp = self._request(server, {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"})
        assert resp["id"] == "list-1"  # 通知未产生多余响应
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        assert "allure_vs_cases" in tools
        schema = tools["allure_vs_cases"]["inputSchema"]
        assert schema["required"] == ["cases_file", "allure_results"]
        assert "params" in schema["properties"]

        # 4) tools/call（内容模式：cases 传 JSON 字符串、allure 传 filename 对象）
        resp = self._request(server, {
            "jsonrpc": "2.0", "id": "call-1", "method": "tools/call",
            "params": {
                "name": "allure_vs_cases",
                "arguments": {
                    "cases_file": json.dumps(
                        [{"name": "login_ok", "expected": "登录成功", "priority": "P0"}]
                    ),
                    "allure_results": {
                        "filename": "run-1-result.json",
                        "content": json.dumps({"name": "login_ok", "status": "passed"}),
                    },
                    "params": {},
                },
            },
        })
        assert "error" not in resp, resp
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["status"] == "ok", payload
        assert payload["output"]["consistency_rate"] == 1.0
        assert payload["output"]["mismatched_count"] == 0
        assert payload["finish_reason"] == "completed"

        # 5) tools/call 未知工具 → 协议级错误 -32602
        resp = self._request(server, {
            "jsonrpc": "2.0", "id": "call-2", "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        })
        assert resp["error"]["code"] == -32602

        # 6) ping 存活
        resp = self._request(server, {"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
        assert resp["result"] == {}
