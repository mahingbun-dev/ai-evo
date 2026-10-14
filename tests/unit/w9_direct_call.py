"""W9 端点直调适配 helper（既有单元测试共用）

W9 为约 30 个端点加了对象级归属校验，端点签名新增必传 `request: Request`
（request.state.user）。仓库既有 API 单测按惯例直调端点函数、不经过
TestClient / AuthMiddleware（见 test_hub_file_preview_api.py 模块注释），
本模块提供两类替身：

- fake_request(user=None)：Request 替身。默认**超管**用户（不改变既有
  用例的语义——历史用例建立时资源无团队概念，超管视角等价于「无校验」）；
  跨团队 403 矩阵用例显式传普通用户。
- make_record_db(...)：按模型分发的 db 替身（供归属查询 + 业务查询复用）。

只供 tests/ 使用，不进生产路径。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

SUPER_ADMIN_USER = {
    "user_code": "w9-direct-call",
    "org_code": "DEFAULT",
    "super_admin_flag": 1,
}


def fake_user(org_code: str = "DEFAULT", super_admin_flag: int = 1) -> dict:
    return {
        "user_code": f"w9-direct-{org_code}",
        "org_code": org_code,
        "super_admin_flag": super_admin_flag,
    }


def fake_request(user: dict = None):
    """Request 替身：request.state.user 直读；headers 供 extract_bearer_token
    等取 cookie 的端点逻辑调用（空 dict → get 返回 None → 无凭证，与直调
    request=None 时的历史行为一致）"""
    return SimpleNamespace(headers={}, state=SimpleNamespace(user=user or SUPER_ADMIN_USER))


def fake_request_with_cookie(cookie: str = "PLATFORM_SSO=tok-test", user: dict = None):
    """带平台 Cookie 头 + state.user 的 Request 替身（retry 等同时要凭证与
    归属校验的端点直调用）"""
    return SimpleNamespace(
        headers={"cookie": cookie},
        state=SimpleNamespace(user=user or SUPER_ADMIN_USER),
    )
