"""ai-evo 插件 L1 契约一致性 + manifest 结构不变量。

L1 复现中台注册期的校验规则（hubkit.conformance.local），不需要中台；
manifest 不变量保证「工具面 ↔ 路由表」不漂移：选择器取值可解析、占位符可替换、
审批策略与设计一致（删除+修改需审批且同工具内策略一致）。
"""
import json

import hubkit
from hubkit import conformance

from app.hub_plugin.manifest import (
    TOOL_GROUPS,
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
    assert m.version == "1.6.0"
    # 直接调用型插件：消费/生产 Struct 载荷
    assert {c.fq_name for c in m.consumes} == {"google.protobuf.Struct"}
    assert {c.fq_name for c in m.produces} == {"google.protobuf.Struct"}


def test_tool_decls_match_groups():
    """manifest 工具声明与 TOOL_GROUPS 一一对应，schema 是合法 JSON 对象。"""
    decls = {t.name: t for t in build_tool_decls()}
    assert set(decls) == set(TOOL_GROUPS)
    for name, decl in decls.items():
        schema = json.loads(decl.input_schema_json)
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name
        assert decl.description, name


def test_group_actions_point_to_real_routes():
    """每个工具的选择器取值都映射到真实路由；默认值与选择器自洽。"""
    for name, group in TOOL_GROUPS.items():
        actions = group["actions"]
        assert actions, name
        for action, route in actions.items():
            assert route in TOOL_ROUTES, (name, action, route)
        if "default" in group:
            assert group["default"] in actions, name
        if len(actions) > 1:
            assert group.get("selector") in {"action", "view", "format"}, name


def test_hidden_aliases_not_declared():
    """未进工具面的旧工具名是隐藏别名：不出现在 tools/list，但仍可按 op 调用。"""
    declared = set(TOOL_GROUPS)
    hidden = set(TOOL_ROUTES) - declared
    assert hidden, "至少应保留旧名作为隐藏别名"
    assert declared.isdisjoint(hidden)
    # 旧名必须指向同一实现：隐藏的是「工具名」，不是路由
    for alias in hidden:
        assert alias in TOOL_ROUTES


def test_approval_policy():
    """审批策略：删除+修改类需人工审批，且同一工具内审批策略必须一致。"""
    approval = sorted(
        name for name, g in TOOL_GROUPS.items() if _group_approval(name, g)
    )
    assert approval == ["dataset_manage", "task_manage"]


def _group_approval(name, group) -> bool:
    explicit = group.get("requires_approval")
    flags = {
        bool(TOOL_ROUTES[r].get("requires_approval", False))
        for r in group["actions"].values()
    }
    assert len(flags) == 1, f"{name} 内混合了审批策略不同的路由"
    if explicit is not None:
        assert explicit is True and flags == {True}, f"{name} 的审批声明与路由不一致"
    return bool(flags.pop())


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
