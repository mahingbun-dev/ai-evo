"""插件主体：ai-evo REST API 的 anc-hub 代理。

设计对齐中台既有插件的通用形态：纯 HTTP 代理、不含业务逻辑；``hub.tool``（聚合工具
路径）优先、``payload.op``（invoke_plugin 通用入口）兜底分发；业务失败返回
结构化错误卡而不是抛异常（只有基础设施故障才会让中台把这次调用记成插件故障）。
"""

from __future__ import annotations

import os
from typing import Any, Dict

import hubkit
from hubkit.proto.hubv1 import envelope_pb2, plugin_pb2

from .client import AievoApiError, AievoClient
from .manifest import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
    TOOL_ROUTES,
    _OPS,
    build_manifest,
    postprocess_of,
    resolve_op,
)

# hub 侧登录闸门通过后才会注入的 meta 键（见 plugin-hub 仓库 hub-core/src/auth.rs，开源与企业中台同键名）
MAS_TOKEN_META = "hub.mas_token"

DEFAULT_API_BASE = "http://127.0.0.1:8100"
DEFAULT_FRONTEND_BASE = "http://127.0.0.1:5173"
DEFAULT_TIMEOUT_SECONDS = 30.0


class AievoHubPlugin(hubkit.Plugin):
    """把 ai-evo 的 18 个核心操作暴露为 anc-hub MCP 工具。"""

    def __init__(self) -> None:
        self._base = os.environ.get("AIEVO_API_BASE", DEFAULT_API_BASE).rstrip("/")
        self._frontend_base = os.environ.get("AIEVO_FRONTEND_BASE", DEFAULT_FRONTEND_BASE)
        self._timeout = float(os.environ.get("AIEVO_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
        self._client = AievoClient(self._base, timeout=self._timeout)

    # ── 契约 ────────────────────────────────────────────────────────

    def manifest(self) -> plugin_pb2.PluginManifest:
        return build_manifest()

    def descriptor(self) -> bytes:
        # 只用 google.protobuf.Struct 承载 JSON 载荷，无自有 proto
        return b""

    def validate(self, ctx: Any, env: envelope_pb2.Envelope) -> plugin_pb2.ValidateResponse:
        payload, is_json = hubkit.payload_json(env)
        if not is_json:
            return hubkit.invalid(hubkit.issue("payload", "载荷必须是 JSON 对象"))

        op = resolve_op(env.meta, payload or {})
        if op is None:
            return hubkit.invalid(
                hubkit.issue(
                    "payload",
                    f"无法确定要调用的工具：meta 里没有 hub.tool，载荷里也没有 op。"
                    f"已知工具：{sorted(_OPS)}",
                )
            )
        if op not in _OPS:
            return hubkit.invalid(
                hubkit.issue("payload", f"未知的工具 {op!r}，已知：{sorted(_OPS)}")
            )
        return hubkit.valid()

    # ── 插件体 ──────────────────────────────────────────────────────

    def handle(self, ctx: Any, env: envelope_pb2.Envelope) -> envelope_pb2.Envelope:
        payload, _ = hubkit.payload_json(env)
        payload = payload or {}
        op = resolve_op(env.meta, payload) or ""

        mas_token = (env.meta.get(MAS_TOKEN_META) or "").strip()
        if not mas_token:
            # 与 dc-dict 同口径：登录态缺失是业务结果，给 agent 一张可行动的卡
            return hubkit.with_payload_json(
                env,
                {
                    "success": False,
                    "error_code": "login_required",
                    "plugin": PLUGIN_NAME,
                    "tool": op,
                    "message": (
                        "缺少平台登录态：请先经 anc-hub 登录（auth__login，或在 MCP "
                        "客户端配置 Authorization: Bearer <平台token>）后再调用 ai-evo 工具"
                    ),
                },
            )

        route = TOOL_ROUTES.get(op)
        if route is None:
            # 正常链路走不到这里（validate 已拦），防御性兜底：不给中台记 INTERNAL
            return hubkit.with_payload_json(
                env,
                {
                    "success": False,
                    "error_code": "unknown_tool",
                    "plugin": PLUGIN_NAME,
                    "tool": op or "",
                    "message": f"未知的工具 {op!r}，已知：{sorted(_OPS)}",
                },
            )

        built = _build_request(route, payload)
        if isinstance(built, dict):
            # 参数缺失的错误卡（补齐与其他卡一致的标识字段）
            built.setdefault("plugin", PLUGIN_NAME)
            built.setdefault("tool", op)
            return hubkit.with_payload_json(env, built)

        method, path, query, body = built

        try:
            resp = self._client.request(
                method, path, mas_token, params=query, json_body=body
            )
        except AievoApiError as e:
            return hubkit.with_payload_json(
                env,
                {
                    "success": False,
                    "error_code": e.error_code,
                    "plugin": PLUGIN_NAME,
                    "tool": op,
                    "message": e.message,
                },
            )

        status = resp["status"]
        data = resp["data"]
        success = 200 <= status < 300

        if status == 401:
            # ai-evo 鉴权失败：token 无效/过期，与「没带 token」区分开
            return hubkit.with_payload_json(
                env,
                {
                    "success": False,
                    "error_code": "login_invalid",
                    "plugin": PLUGIN_NAME,
                    "tool": op,
                    "message": "平台登录态无效或已过期（ai-evo 返回 401），请重新登录后重试",
                    "http_status": status,
                },
            )

        postprocess = postprocess_of(op)
        if success and postprocess is not None:
            ctx_info = {
                "payload": payload,
                "frontend_base": self._frontend_base,
            }
            data = postprocess(data, ctx_info)

        result = {
            "success": success,
            "http_status": status,
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "tool": op,
            "data": data,
        }
        return hubkit.with_payload_json(env, result)

    def health(self) -> plugin_pb2.HealthResponse:
        """健康探测：ai-evo 不可达时自报不健康，中台会摘除实例而不是硬吃 502。"""
        if self._client.health():
            return plugin_pb2.HealthResponse(
                healthy=True, message=f"ai-evo {self._base} 可达"
            )
        return plugin_pb2.HealthResponse(
            healthy=False, message=f"无法连接 ai-evo {self._base}"
        )


# ── 请求拼装 ──────────────────────────────────────────────────────


def _build_request(route: Dict[str, Any], payload: Dict[str, Any]):
    """按 route 把 payload 拼成 (method, path, query, body)；参数缺失返回错误卡。"""
    method = route["method"]
    path = route["path"]

    params = dict(payload)
    params.pop("op", None)

    for key in route.get("path_params", []):
        value = params.pop(key, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return {
                "success": False,
                "error_code": "missing_param",
                "message": f"缺少必填参数 {key}",
            }
        path = path.replace("{" + key + "}", str(value))

    query: Dict[str, Any] = {}
    for key in route.get("query_params", []):
        if key in params:
            query[key] = params.pop(key)

    body = params if route.get("body") else None
    return (method, path, query, body)
