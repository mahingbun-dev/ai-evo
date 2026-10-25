"""QE4-4 审计覆盖面静态断言：扫 API 注册表，写端点必须接 audited 审计依赖

设计依据：plans/2026-10-22-Agent质量工程平台开发方案.md §4.4「审计补全」——
「新增单测扫描 API 注册表：所有非白名单 POST/PUT/PATCH/DELETE 路由必须声明
审计依赖（静态断言风格同 compose 拓扑测试），堵住『新端点忘接审计』」。

机制（app/services/audit_service.py::audited）：
- 路由级 dependencies=[Depends(audited(action, resource_type))]，依赖函数带
  __audit_action__ / __audit_resource_type__ 标记，本文件据此扫描注册表；
- 白名单只收「无持久化副作用的写动词端点」（连通性探测/试算/校验/预览/
  查询/调试探针）与「匿名认证端点」（登录注册——登录留痕由 handler 内
  手工审计带认证后上下文完成，匿名依赖取不到用户身份）；
- 白名单是精确路径（含路径参数占位符）而非前缀——杜绝宽白名单放行；
- 附带一致性断言：action 与 HTTP 动词语义匹配、白名单不腐化（删端点须同步
  删白名单）、白名单条目不得同时挂审计依赖（避免规则漂移双重记账）。
"""
from fastapi.routing import APIRoute

import pytest

from app.main import app
from app.core.config import get_settings

pytestmark = pytest.mark.unit  # W8 分类门禁：纯静态断言，无网络/DB

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _api_prefix() -> str:
    """注册表里的路由路径带 settings.API_V1_STR 前缀（环境可变，测试统一剥掉）"""
    return get_settings().API_V1_STR.rstrip("/")


# ======================================================================
# 白名单：无持久化副作用的写动词端点 + 匿名认证端点（精确路径，非前缀）
# ======================================================================

AUDIT_WRITE_WHITELIST = {
    # --- 匿名认证端点：请求期无用户身份（登录/注册留痕由 handler 手工审计） ---
    "/auth/login",
    "/auth/register",
    "/auth/logout",
    # --- 连通性自检/无状态解析（只读探测，不落任何库表/磁盘态） ---
    "/auth/ldap/test",
    "/auth/oidc/test",
    "/claude-code-config/test",
    "/deepseek-harness-config/test",
    "/models/{model_id}/test",
    "/models/parse-curl",
    # --- 演示 mock LLM（P0-D）：无状态预置文案端点，不落任何库表，仅固定演示 token 约束 ---
    "/demo/mock-llm/chat/completions",
    # --- 试算/校验/调试探针（纯计算或临时工作区产物，非治理对象） ---
    "/ab-test/significance",
    "/evaluation/judge-config/preview",
    "/evaluation/judge-config/validate",
    "/evaluation/tasks/verifiers/debug",
    "/instruction/preview",
    "/instruction/validate",
    "/pre-judge-scripts/{script_id}/debug",
    "/agent-evaluation/task-completion",
    "/agent-evaluation/tool-correctness",
    "/safety-evaluation/evaluate",
    "/safety-evaluation/bias",
    "/safety-evaluation/toxicity",
    "/dataset-synthesis/from-contexts",
    "/dataset-synthesis/from-scratch",
    # --- 查询型 POST（读语义，body 传查询条件） ---
    "/hub-files/query",
    "/hub-files/match-preview",
    # --- 导入预览工具链（临时态：预览/临时文件不产生持久资源，confirm 才审计） ---
    "/evaluation/datasets/{dataset_id}/import/preview",
    "/evaluation/datasets/{dataset_id}/import/preview/v2",
    "/evaluation/datasets/{dataset_id}/import/preview/upload-temp-file",
    "/evaluation/datasets/{dataset_id}/import/preview/temp-file/{temp_file_id}",
    "/evaluation/datasets/import/sessions/preview",
}


def _write_routes():
    """注册表中的全部写路由 (methods, path, deps)"""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods & WRITE_METHODS:
            yield route


def _audit_marker(route: APIRoute):
    """路由上的审计依赖标记 (action, resource_type)；无则 None"""
    for dep in route.dependencies:
        fn = getattr(dep, "dependency", None)
        action = getattr(fn, "__audit_action__", None)
        if action:
            return action, getattr(fn, "__audit_resource_type__", "")
    return None


# ======================================================================
# 覆盖面静态断言（方案 §4.4 验收：审计覆盖静态断言 100%）
# ======================================================================

class TestAuditCoverage:
    def test_all_write_routes_audited_or_whitelisted(self):
        """非白名单写路由必须声明 audited 审计依赖——新端点忘接审计在此红灯"""
        prefix = _api_prefix()
        missing = []
        for route in _write_routes():
            path = route.path
            if path.startswith(prefix):
                path = path[len(prefix):]
            if path in AUDIT_WRITE_WHITELIST:
                continue
            if _audit_marker(route) is None:
                missing.append(f"{','.join(sorted(route.methods & WRITE_METHODS))} {route.path}")
        assert not missing, (
            "以下写端点未接审计依赖且不在白名单（接 audited(...) 或登记白名单并给出理由）：\n  "
            + "\n  ".join(missing)
        )

    def test_whitelist_entries_all_exist(self):
        """白名单不腐化：条目必须对应真实存在的写路由（删端点须同步删白名单）"""
        prefix = _api_prefix()
        real = set()
        for route in _write_routes():
            path = route.path
            if path.startswith(prefix):
                path = path[len(prefix):]
            real.add(path)
        stale = AUDIT_WRITE_WHITELIST - real
        assert not stale, f"白名单中已无对应路由的僵尸条目：{sorted(stale)}"

    def test_whitelisted_routes_do_not_also_declare_audit(self):
        """白名单端点不得同时挂审计依赖（双规则并存会让覆盖面口径漂移）"""
        prefix = _api_prefix()
        for route in _write_routes():
            path = route.path
            if path.startswith(prefix):
                path = path[len(prefix):]
            if path in AUDIT_WRITE_WHITELIST:
                assert _audit_marker(route) is None, f"{path} 在白名单却又挂了审计依赖"

    def test_audit_action_matches_http_verb_semantics(self):
        """action 与 HTTP 动词语义一致：DELETE→DELETE；PUT/PATCH→UPDATE；POST→开放集"""
        # POST 允许的动作集：CREATE（建资源）/UPDATE（迁移、修复类写）/DELETE
        # （cleanup-duplicates 这类批量清理）/RUN/STOP/EXPORT（触发与外发）
        post_actions = {"CREATE", "UPDATE", "DELETE", "RUN", "STOP", "EXPORT"}
        for route in _write_routes():
            marker = _audit_marker(route)
            if marker is None:
                continue
            action, _resource = marker
            verbs = route.methods & WRITE_METHODS
            if verbs == {"DELETE"}:
                assert action == "DELETE", f"{route.path}: DELETE 动词的 action 应为 DELETE，实为 {action}"
            elif verbs <= {"PUT", "PATCH"}:
                assert action == "UPDATE", f"{route.path}: PUT/PATCH 动词的 action 应为 UPDATE，实为 {action}"
            elif "POST" in verbs:
                assert action in post_actions, f"{route.path}: POST 的 action {action} 不在允许集 {post_actions}"

    def test_audit_dependency_metadata_present(self):
        """每个 audited 依赖都带静态断言标记（缺失标记 = 依赖机制被绕过）"""
        seen = 0
        for route in _write_routes():
            for dep in route.dependencies:
                fn = getattr(dep, "dependency", None)
                if getattr(fn, "__audit_action__", None):
                    assert getattr(fn, "__audit_resource_type__", "")
                    seen += 1
        # 80 个已接线端点（M4 落地基线；端点增减由上面的覆盖面断言把口径）
        assert seen >= 80, f"审计依赖标记数 {seen} 低于基线 80"

    def test_m1_faces_are_audited(self):
        """M1 新增面（api-keys / webhooks CRUD）明确在覆盖面内（方案点名）"""
        prefix = _api_prefix()
        audited_paths = set()
        for route in _write_routes():
            if _audit_marker(route) is not None:
                path = route.path
                if path.startswith(prefix):
                    path = path[len(prefix):]
                audited_paths.add(path)
        assert "/api-keys" in audited_paths
        assert "/api-keys/{key_id}" in audited_paths
        assert "/webhooks" in audited_paths
        assert "/webhooks/{webhook_id}" in audited_paths

    def test_named_gap_resources_are_audited(self):
        """方案点名的既有缺口面（模型配置/脚本库/团队管理/模板/A/B）已接审计"""
        prefix = _api_prefix()
        resources = set()
        for route in _write_routes():
            marker = _audit_marker(route)
            if marker is not None:
                resources.add(marker[1])
        for expected in ("model", "judge_model", "pre_judge_script",
                         "team_member_role", "team_member_status", "template", "ab_session"):
            assert expected in resources, f"资源 {expected} 未接审计依赖"
