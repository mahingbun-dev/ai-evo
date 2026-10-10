"""前处理脚本 MCP Server（stdio）——脚本 MCP 工具化最小闭环

设计：docs/design/pre-judge-scripts.md 第九节 9.3（脚本 MCP 工具化：裁判 agent
循环内调用确定性工具）。以最小 stdio MCP server 形态（换行分隔 JSON-RPC 2.0，
MCP stdio transport 约定）把所有 is_active 的 builtin 脚本暴露为 MCP tools：

- tool 名 = 脚本 name
- inputSchema 由脚本 input_fields 生成（每个字段 string：内容或本地路径；
  另有可选 params 对象透传 stdin.params）
- tools/call 即执行脚本并返回标准输出 JSON（{status, output, files, summary,
  finish_reason, error}），失败事实结构化返回，供裁判纳入可信度判断

脚本来源（collect_tools）：DB 中 exec_type=builtin 且 is_active 的记录
（前处理脚本统一落库后的单一来源；查库失败降级为空列表并 stderr 告警，
原内置脚本由 ensure_builtin_scripts_seeded 启动时补种为普通记录）。

启动方式：python -m app.services.script_mcp_server（stdin/stdout 走协议，
日志只允许写 stderr——stdout 是协议通道，任何污染都会破坏 framing）。

注入方式（claude_code_config_service.enable_script_mcp 开启时）：
ClaudeCodeAdapter 以 ClaudeAgentOptions.mcp_servers stdio 配置注入
claude-agent-sdk（get_stdio_server_config），SDK 0.2.163 已验证支持该参数。
"""
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("script_mcp_server")

# MCP 协议常量
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "evo-pre-judge-scripts"
SERVER_VERSION = "1.0.0"

# 注入 ClaudeAgentOptions.mcp_servers 时使用的 key
MCP_SERVER_KEY = SERVER_NAME

# JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# 工具运行产物根目录（系统临时区；MCP 触发的运行无 task_id，不参与任务级清理，
# files 注册的产物路径在 agent 会话内保持可读，由 OS 临时目录策略兜底回收）
MCP_RUN_DIRNAME = "evo_script_mcp"


class UnknownToolError(Exception):
    """tools/call 指定了不存在的工具（MCP 语义：协议级错误 -32602）"""


# ---------------- 工具清单 ----------------

def _iter_db_builtin_scripts() -> List[Dict[str, Any]]:
    """查询 DB 中 is_active 且 exec_type ∈ {script, builtin} 的脚本定义；失败降级为空列表

    exec_type 收敛后的单一脚本来源（builtin 进程内类型已废除：内置脚本由
    ensure_builtin_scripts_migrated 迁移为 script 记录；查询同时兼容迁移
    尚未执行的存量 builtin 行——该形态执行时会按「类型已废除」fail）。
    """
    try:
        from app.db.session import SessionLocal

        from app.models.pre_judge_models import PreJudgeScript

        with SessionLocal() as db:
            rows = (
                db.query(PreJudgeScript)
                .filter(
                    PreJudgeScript.exec_type.in_(("script", "builtin")),
                    PreJudgeScript.is_active == True,  # noqa: E712
                )
                .all()
            )
            return [
                {
                    "script_id": row.id,
                    "name": row.name,
                    "exec_type": row.exec_type,
                    "entrypoint": row.entrypoint,
                    "timeout_seconds": row.timeout_seconds if row.timeout_seconds is not None else 120,
                    "input_fields": row.input_fields or {},
                    "description": row.description or "",
                    "output_schema": row.output_schema or {},
                }
                for row in rows
            ]
    except Exception as e:  # noqa: BLE001 - DB 不可达只降级为空工具列表，不破坏 MCP 协议
        print(f"[script_mcp_server] 查询 DB 脚本失败，本会话不提供脚本工具: {e}", file=sys.stderr)
        return []


def collect_tools() -> List[Dict[str, Any]]:
    """收集全部可用工具描述（MCP tools/list 的 tools 数组项；DB 单一来源）"""
    return [
        {
            "name": script["name"],
            "description": _tool_description(
                script.get("description") or "",
                script.get("output_schema") or {},
            ),
            "inputSchema": build_input_schema(script.get("input_fields")),
        }
        for script in _iter_db_builtin_scripts()
    ]


def _format_schema_value(value: Any) -> str:
    """output_schema 值 → 可读文本：叶子串原样；嵌套结构展平子键路径；其余 JSON 兜底"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("children"), dict):
        paths: List[str] = []

        def walk(children: Dict[str, Any], prefix: str) -> None:
            for key, child in children.items():
                path = f"{prefix}.{key}" if prefix else key
                if len(paths) < 8:
                    paths.append(path)
                if isinstance(child, dict) and isinstance(child.get("children"), dict):
                    walk(child["children"], path)

        walk(value["children"], "")
        head = "：".join(str(x) for x in (value.get("type"), value.get("description")) if x) or "object"
        more = " 等" if len(paths) >= 8 else ""
        return f"{head}（子键: {'、'.join(paths) if paths else '无'}{more}）"
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_description(description: str, output_schema: Dict[str, Any]) -> str:
    """工具描述：脚本用途 + 输出键说明（帮裁判 agent 理解返回结构）"""
    parts = [description.strip() or "评测前处理 builtin 脚本"]
    if output_schema:
        keys = "；".join(f"{k}: {_format_schema_value(v)}" for k, v in output_schema.items())
        parts.append(f"返回 output 键：{keys}")
    parts.append("返回值为标准输出 JSON：{status: ok|skip|fail, output, files, summary, finish_reason, error}；status=fail 是结构化失败事实，不表示协议错误。")
    return "\n".join(parts)


def build_input_schema(input_fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """由脚本 input_fields 生成 MCP inputSchema

    每个声明字段 → string 属性（传内容或本地路径，或 {filename, content} 对象
    指定落盘文件名），全部标记 required；另附可选 params 对象。
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for field_name, meta in (input_fields or {}).items():
        if isinstance(meta, dict):
            desc = str(meta.get("description") or "").strip()
            value_type = meta.get("value_type")
        else:
            desc, value_type = str(meta or ""), None
        suffix = f"（value_type={value_type}）" if value_type else ""
        properties[str(field_name)] = {
            "type": "string",
            "description": (
                f"{desc}{suffix}。传本地绝对路径、或文件内容字符串、或 "
                '{{"filename": "x.json", "content": "..."}} 对象指定落盘文件名'
            ).strip(),
        }
        required.append(str(field_name))
    properties["params"] = {
        "type": "object",
        "description": "脚本参数（透传 stdin.params，可选）",
        "additionalProperties": True,
    }
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


# ---------------- 脚本解析与执行 ----------------

def resolve_script(name: str) -> Optional[Dict[str, Any]]:
    """按工具名（=脚本名）解析为 execute_script 兼容定义（DB 单一来源）；不存在返回 None"""
    for script in _iter_db_builtin_scripts():
        if script["name"] == name:
            return script
    return None


def _materialize_field(field: str, value: Any, run_dir: Path) -> str:
    """把工具入参落地为本地文件路径（复用 pre_judge_runner.write_inline_value 语义）

    - 已存在的路径：原样返回（agent 传服务端已落盘的文件路径）
    - {"filename", "content"} 对象：按指定文件名落盘（覆盖需要特定扩展名的脚本契约，
      如 allure 单结果要求 *-result.json）
    - dict/list：写 <field>.json
    - 其余：写 <field>.txt（JSON 字符串也走 .json 由 write_inline_value 的
      dict/list 分支覆盖不到，这里保持字符串→.txt；脚本按内容解析不依赖扩展名，
      仅 allure 单结果等对扩展名有要求的用 filename 对象形式）
    """
    if isinstance(value, str) and os.path.exists(value):
        return value
    if isinstance(value, dict) and isinstance(value.get("content"), (str, dict, list)):
        filename = str(value.get("filename") or f"{field}.json")
        safe = filename.replace("/", "_").replace("\\", "_") or f"{field}.json"
        target = run_dir / safe
        content = value["content"]
        target.write_text(
            content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(target)
    from app.services.pre_judge_runner import write_inline_value

    return write_inline_value(field, value, run_dir)


def execute_tool(name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """执行工具（= 执行脚本），返回标准输出 dict

    组装标准输入（docs/design/pre-judge-scripts.md 第三节）：MCP 触发无任务编排，
    fields 由工具入参落地，upstream 为空，task_id/data_id 取可选入参或 'mcp'。
    """
    script = resolve_script(name)
    if script is None:
        raise UnknownToolError(f"Unknown tool: {name}")

    arguments = arguments or {}
    run_dir = Path(os.environ.get("TMPDIR", "/tmp")) / MCP_RUN_DIRNAME / uuid.uuid4().hex[:8]
    run_dir.mkdir(parents=True, exist_ok=True)

    declared_fields = script.get("input_fields") or {}
    field_names = list(declared_fields.keys()) or [
        k for k in arguments if k not in ("params", "task_id", "data_id")
    ]
    fields: Dict[str, str] = {}
    for field_name in field_names:
        value = arguments.get(field_name)
        if value is None:
            continue
        fields[str(field_name)] = _materialize_field(str(field_name), value, run_dir)

    stdin: Dict[str, Any] = {
        "task_id": str(arguments.get("task_id") or "mcp"),
        "data_id": str(arguments.get("data_id") or "mcp"),
        "fields": fields,
        "model_output": None,
        "session": None,
        "params": arguments.get("params") or {},
        "upstream": {},
        "_run_dir": str(run_dir),
    }

    # 函数级导入：保持 server 启动轻量（不拉起 claude_agent_sdk 等重依赖）
    from app.services.pre_judge_runner import execute_script

    return execute_script(script, stdin, timeout_seconds=script.get("timeout_seconds"))


# ---------------- JSON-RPC / MCP 消息处理 ----------------

def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；通知类消息返回 None（不回包）"""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: expected object")

    method = message.get("method")
    msg_id = message.get("id")
    is_notification = msg_id is None

    try:
        if method == "initialize":
            requested = message.get("params", {}).get("protocolVersion") if isinstance(message.get("params"), dict) else None
            return _result(msg_id, {
                "protocolVersion": requested or DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": collect_tools()})
        if method == "tools/call":
            return _handle_tools_call(msg_id, message.get("params") or {})
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if is_notification:
            return None
        return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except UnknownToolError as e:
        return _error(msg_id, INVALID_PARAMS, str(e))
    except Exception as e:  # noqa: BLE001 - 协议边界：任何异常转 JSON-RPC error
        print(f"[script_mcp_server] 处理 {method!r} 异常: {e}", file=sys.stderr)
        return _error(msg_id, INTERNAL_ERROR, f"Internal error: {e}")


def _handle_tools_call(msg_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """tools/call：执行脚本。工具执行失败（结构化 fail）走 isError=false + 标准输出
    JSON（裁判需要读到失败事实）；未知工具是协议错误（-32602）。
    """
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise UnknownToolError("tools/call 缺少工具名 name")
    result = execute_tool(name, params.get("arguments") or {})
    return _result(msg_id, {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "isError": False,
    })


def serve(instream: Any = None, outstream: Any = None) -> None:
    """stdio 主循环：换行分隔 JSON-RPC（MCP stdio transport）"""
    instream = instream if instream is not None else sys.stdin
    outstream = outstream if outstream is not None else sys.stdout
    for line in instream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            response = _error(None, PARSE_ERROR, f"Parse error: {e}")
        else:
            response = handle_message(message)
        if response is None:
            continue
        outstream.write(json.dumps(response, ensure_ascii=False) + "\n")
        outstream.flush()


def main() -> None:
    """入口：日志强制走 stderr（stdout 是协议通道）"""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    serve()


def get_stdio_server_config() -> Dict[str, Any]:
    """生成 claude-agent-sdk mcp_servers 的 stdio server 配置

    command 取当前解释器（与平台同 .venv）；PYTHONPATH 指向项目根，
    使 `-m app.services.script_mcp_server` 不依赖子进程 cwd。
    """
    project_root = str(Path(__file__).resolve().parents[2])
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "app.services.script_mcp_server"],
        "env": {"PYTHONPATH": project_root},
    }


if __name__ == "__main__":
    main()
