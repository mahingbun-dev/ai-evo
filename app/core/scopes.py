"""
API Key scope 常量与鉴权依赖（M1 QE1-1，门禁闭环）

scope 是 API Key 的最小授权单元（plans/2026-10-22-Agent质量工程平台开发方案.md
§M1「API Key 鉴权」）：CI/CLI 用 Bearer aievo_sk_... 调平台 API 时，可调端点
由 Key 的 scope 集合决定；页面端点走 JWT 登录态，不受 scope 影响。

设计口径：
- scope 匹配是**精确字符串等值**——不支持通配符（"*"、"tasks:*" 一律不匹配），
  防「宽 scope 旁路」；大小写敏感；
- 「API Key 仅放行白名单 scope 化端点」由 API_KEY_ROUTES 注册表落实：
  中间件对 API Key 请求做 (method, path) 匹配，未注册路由直接 403
  （default-deny，既有页面端点对 API Key 天然不可达）；scope 匹配为
  命中注册路由后的第二道闸。端点侧再挂 require_scope 依赖做纵深防御
  （OpenAPI 文档化 + 防注册表漏配）；
- 注册表在应用启动/路由定义时通过 register_api_key_route 登记（QE1-4
  的新 API 端点落地时接线；QE1-1 阶段注册表为空 = API Key 全量 403，
  即安全缺省）。
"""
import re
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

# ---------------- scope 常量（方案 §M1 指定六个） ----------------

TASKS_RUN = "tasks:run"            # 触发评测任务执行（POST /evaluation/tasks/{id}/run）
TASKS_READ = "tasks:read"          # 读任务状态/执行历史/门禁结果
RESULTS_READ = "results:read"      # 读行级评测结果与导出
DATASETS_READ = "datasets:read"    # 读数据集
DATASETS_WRITE = "datasets:write"  # 上传/写数据集
WEBHOOKS_MANAGE = "webhooks:manage"  # webhook 端点 CRUD 与送达历史查询

ALL_SCOPES: Tuple[str, ...] = (
    TASKS_RUN,
    TASKS_READ,
    RESULTS_READ,
    DATASETS_READ,
    DATASETS_WRITE,
    WEBHOOKS_MANAGE,
)


def normalize_scopes(scopes) -> frozenset:
    """库内 JSON scopes 列 → frozenset（None/非列表/空白项兜底为空集）"""
    if not isinstance(scopes, list):
        return frozenset()
    return frozenset(s for s in scopes if isinstance(s, str) and s.strip())


# ---------------- API Key 可达路由注册表（default-deny） ----------------


class _ApiKeyRoute:
    """一条 API Key 可达路由：(method, path 模板) → 所需 scope

    path 模板与 FastAPI 路由写法一致（如 /api/v1/evaluation/tasks/{task_id}/run），
    匹配时把 {param} 段当任意非空段（不含 /）。
    """

    __slots__ = ("method", "path", "scope", "_regex")

    def __init__(self, method: str, path: str, scope: str):
        self.method = method.upper()
        self.path = path
        self.scope = scope
        parts = []
        for segment in path.split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                parts.append("[^/]+")
            else:
                parts.append(re.escape(segment))
        # 全量匹配（$ 锚定），杜绝前缀伪造（注册 /a 时 /a/b 不得命中）
        self._regex = re.compile("^" + "/".join(parts) + "$")

    def match(self, method: str, path: str) -> bool:
        return self.method == method.upper() and self._regex.match(path) is not None


# 注册表（模块级单例；启动期登记，读多写少，不做并发控制——注册发生在
# import/启动阶段，请求期只读）
_API_KEY_ROUTES: List[_ApiKeyRoute] = []


def register_api_key_route(method: str, path: str, scope: str) -> None:
    """登记一条 API Key 可达路由（QE1-4 新端点接线用；测试亦用）

    path 用 FastAPI 模板写法；scope 须为 ALL_SCOPES 之一（防拼写漂移）。
    """
    if scope not in ALL_SCOPES:
        raise ValueError(f"未知 scope {scope!r}，必须为 ALL_SCOPES 之一")
    _API_KEY_ROUTES.append(_ApiKeyRoute(method, path, scope))


def reset_api_key_routes() -> None:
    """清空注册表（仅测试用；生产不调）"""
    _API_KEY_ROUTES.clear()


def match_api_key_route(method: str, path: str) -> Optional[str]:
    """(method, path) → 所需 scope；未注册返回 None（调用方应 403）"""
    for route in _API_KEY_ROUTES:
        if route.match(method, path):
            return route.scope
    return None


# ---------------- FastAPI 依赖 ----------------


def require_scope(scope: str):
    """声明式 scope 依赖：API Key 请求必须持该 scope，JWT 登录态放行

    用法（QE1-4 新端点）::

        @router.post("/evaluation/tasks/{task_id}/run",
                     dependencies=[Depends(require_scope(scopes.TASKS_RUN))])

    语义：
    - request.state.api_key_scopes 为 None → JWT 登录态（中间件已完整校验
      用户身份/活跃），沿用既有权限模型，本依赖放行；
    - 非 None（API Key 认证成功）→ 精确匹配 scope，缺失即 403。
      中间件已按注册表做过同一道校验，这里是对「注册表漏配」的纵深防御。
    """
    if scope not in ALL_SCOPES:
        raise ValueError(f"未知 scope {scope!r}，必须为 ALL_SCOPES 之一")

    async def dependency(request: Request) -> None:
        granted = getattr(request.state, "api_key_scopes", None)
        if granted is None:
            return  # JWT 登录态：不受 scope 约束
        if scope not in granted:
            raise HTTPException(
                status_code=403,
                detail=f"API Key 缺少所需 scope：{scope}",
            )

    return dependency
