"""ai-evo 插件 L1 契约一致性 + manifest 结构不变量。

L1 复现中台注册期的校验规则（hubkit.conformance.local），不需要中台；
manifest 不变量保证「路由表 ↔ 工具声明」不漂移：占位符可解析、
必填参数齐备、审批策略与设计一致（删除+修改需审批）。
"""
import json

import hubkit
from hubkit import conformance

from app.hub_plugin.manifest import (
    TOOL_ROUTES,
    build_manifest,
    build_tool_decls,
)
from app.hub_plugin.plugin import AievoHubPlugin

import pytest
pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def test_conformance_local_passes():
    """L1 契约自洽：manifest/proto/工具声明全绿（接入中台的第一关）。"""
    report = conformance.local(AievoHubPlugin())
    assert report.passed(), str(report)


def test_manifest_identity():
    m = build_manifest()
    assert m.name == "ai-evo"
    assert m.version == "1.3.0"
    # 直接调用型插件：消费/生产 Struct 载荷
    assert {c.fq_name for c in m.consumes} == {"google.protobuf.Struct"}
    assert {c.fq_name for c in m.produces} == {"google.protobuf.Struct"}


def test_tool_decls_match_routes():
    """manifest 工具声明与 TOOL_ROUTES 一一对应，schema 是合法 JSON 对象。"""
    decls = {t.name: t for t in build_tool_decls()}
    assert set(decls) == set(TOOL_ROUTES)
    for name, decl in decls.items():
        route = TOOL_ROUTES[name]
        assert decl.description == route["desc"]
        assert decl.requires_approval == bool(route.get("requires_approval"))
        schema = json.loads(decl.input_schema_json)
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name


def test_approval_policy():
    """审批策略：仅删除+修改类需要人工审批，其余全自动。"""
    approval = sorted(
        name for name, r in TOOL_ROUTES.items() if r.get("requires_approval")
    )
    assert approval == [
        "dataset_delete",
        "dataset_update",
        "task_delete",
        "task_update",
    ]


def test_routes_well_formed():
    """每个路由的占位符都有对应 path_params；必填参数可从 schema 找到。"""
    for name, route in TOOL_ROUTES.items():
        assert route["method"] in {"GET", "POST", "PUT", "DELETE"}
        assert route["path"].startswith("/"), name

        import re

        placeholders = set(re.findall(r"\{(\w+)\}", route["path"]))
        assert placeholders == set(route.get("path_params", [])), name

        schema = json.loads(json.dumps(route["schema"]))
        required = set(schema.get("required", []))
        assert set(route.get("path_params", [])) <= required, name


def test_path_params_resolve():
    """路径占位符替换后不再残留 {。"""
    from app.hub_plugin.plugin import _build_request

    for name, route in TOOL_ROUTES.items():
        payload = {}
        for key in route.get("path_params", []):
            payload[key] = f"stub-{key}"
        for key in route.get("query_params", []):
            payload[key] = 1
        built = _build_request(route, payload)
        assert isinstance(built, tuple), name
        method, path, query, body = built
        assert "{" not in path and "}" not in path, (name, path)


def test_plugin_name_valid():
    """插件名满足中台规则（工具名会拼进 MCP 标识）。"""
    assert hubkit.valid_plugin_name("ai-evo")
