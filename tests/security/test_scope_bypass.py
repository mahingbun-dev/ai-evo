"""M1 QE1-1 安全测试：scope 旁路（tests/security 三件套之二）

攻击路径复现（红→绿）：
1. default-deny：持合法 Key 访问**未注册**的既有端点（任务列表/用户/模型配置
   等）→ 一律 403，API Key 仅可达显式 scope 化端点；
2. 路径伪装绕过注册表：尾斜杠、双斜杠、URL 编码 %2F、大小写混写、路径参数
   段注入斜杠、路径穿越段——全部不得命中注册路由（403）；
3. scope 越权：Key 只持 tasks:read，访问需 tasks:run 的注册端点 → 403；
4. 通配符/前缀伪造：scopes 存 "*" / "tasks:*" / "tasks:run extra" / 大小写
   变体 → 精确等值匹配，一律不视为持有 tasks:run；
5. 请求头伪造 scope：自带 X-Api-Scopes / X-OAuth-Scopes 头 → 服务端只认
   库内 scopes 列，伪造头无效；
6. JWT 登录态不受 scope 约束（require_scope 对非 API Key 请求放行），
   既有页面端点行为不变；
7. 注册表正则锚定：注册 /a 时 /a/b 不命中；{param} 段不吃斜杠。

独立测试角色；只新增测试，不修改实现代码。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from app.core import scopes as scope_constants
from app.db.session import Base
from app.middleware.auth_middleware import AuthMiddleware
from app.models.api_key_models import ApiKey
from app.models.user_models import User
from app.services import api_key_service
from app.services.api_key_throttle import ApiKeyAuthThrottle
from tests.security.test_apikey_bruteforce import FakeRedis, make_request

pytestmark = pytest.mark.unit


# ---------------- 测试基建（与 test_apikey_bruteforce 同款，多注册一条 run 路由） ----------------


@pytest.fixture
def middleware(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)

    with TestSession() as db:
        db.add(User(user_code="sec-user", username="sec", is_active=True,
                    super_admin_flag=0, application_admin=0))
        db.commit()

    mw = AuthMiddleware(app=None, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    mw._test_session = TestSession
    return mw


def make_key(middleware, granted=("tasks:read",)):
    plaintext, prefix, key_hash = api_key_service.generate_api_key()
    with middleware._test_session() as db:
        db.add(ApiKey(user_code="sec-user", team_code="DEFAULT", name="scope-test",
                      key_prefix=prefix, key_hash=key_hash, scopes=list(granted)))
        db.commit()
    return plaintext


@pytest.fixture(autouse=True)
def registered_routes():
    from app.core.scopes import _API_KEY_ROUTES
    snapshot = list(_API_KEY_ROUTES)  # 快照：import 期注册（gate 端点等）须保留
    scope_constants.register_api_key_route(
        "GET", "/api/v1/evaluation/tasks/{task_id}/gate", scope_constants.TASKS_READ)
    scope_constants.register_api_key_route(
        "POST", "/api/v1/evaluation/tasks/{task_id}/run", scope_constants.TASKS_RUN)
    yield
    _API_KEY_ROUTES[:] = snapshot


async def passthrough(request):
    user = getattr(request.state, "user", None)
    return PlainTextResponse(f"ok:{user['user_code'] if user else 'none'}")


async def dispatch(mw, request):
    return await mw.dispatch(request, passthrough)


# ---------------- 1. default-deny：未注册端点全量 403 ----------------

@pytest.mark.asyncio
async def test_existing_endpoints_denied_for_api_key(middleware):
    """合法 Key 打既有页面端点（任务列表/用户/模型配置/审计）→ 403 不放行"""
    token = make_key(middleware)
    for method, path in (
        ("GET", "/api/v1/evaluation/tasks"),
        ("GET", "/api/v1/users"),
        ("GET", "/api/v1/model-configs"),
        ("GET", "/api/v1/audit/logs"),
        ("POST", "/api/v1/evaluation/tasks"),        # 建任务（写操作）
        ("DELETE", "/api/v1/evaluation/tasks/t1"),   # 删任务（写操作）
        ("GET", "/api/v1/datasets"),
    ):
        resp = await dispatch(middleware, make_request(path, token, method=method))
        assert resp.status_code == 403, f"{method} {path} 未被 default-deny 拦截"
        assert "无权访问" in resp.body.decode()


# ---------------- 2. 路径伪装绕过注册表 ----------------

@pytest.mark.asyncio
async def test_path_tricks_do_not_hit_registered_route(middleware):
    token = make_key(middleware)
    variants = (
        "/api/v1/evaluation/tasks/t1/gate/",          # 尾斜杠
        "/api/v1/evaluation//tasks/t1/gate",          # 双斜杠
        "/api/v1/evaluation/tasks/t1/gate%2f",        # URL 编码斜杠
        "/api/v1/evaluation/tasks/t1/extra/gate",     # 插段
        "/api/v1/evaluation/tasks/../tasks/t1/gate",  # 穿越
    )
    for path in variants:
        resp = await dispatch(middleware, make_request(path, token))
        assert resp.status_code == 403, f"{path} 竟命中注册路由"
    # 正常用法带 query 串：url.path 不含 query，命中注册路由照常放行
    resp = await dispatch(middleware, make_request(
        "/api/v1/evaluation/tasks/t1/gate?verbose=1", token))
    assert resp.status_code == 200
    # 大小写混写（/API/...）不走 /api/ 前缀分支——中间件按非 API 路径透传给
    # SPA/404（FastAPI 路由大小写敏感，不存在 /API/V1 路由），同样到不了
    # 任何 scope 化端点；此处验证它不会被误送进 API Key 分支放行业务数据
    resp = await dispatch(middleware, make_request("/API/V1/evaluation/tasks/t1/gate", token))
    assert resp.body != b"ok:sec-user"  # 不经 request.state.user 回显（非 API 分支）


@pytest.mark.asyncio
async def test_param_segment_does_not_swallow_slashes(middleware):
    """{task_id} 段只匹配单个路径段：t1/run 子路径不得借 gate 路由放行"""
    token = make_key(middleware)
    resp = await dispatch(middleware, make_request(
        "/api/v1/evaluation/tasks/t1/gate/../../run", token))
    assert resp.status_code == 403


# ---------------- 3. scope 越权：持 read 打 run 端点 ----------------

@pytest.mark.asyncio
async def test_scope_escalation_denied(middleware):
    token = make_key(middleware, granted=["tasks:read"])
    resp = await dispatch(middleware, make_request(
        "/api/v1/evaluation/tasks/t1/run", token, method="POST"))
    assert resp.status_code == 403
    assert "tasks:run" in resp.body.decode()
    # 同 Key 打自己 scope 内的端点放行
    resp = await dispatch(middleware, make_request(
        "/api/v1/evaluation/tasks/t1/gate", token))
    assert resp.status_code == 200


# ---------------- 4. 通配符 / 前缀 / 大小写伪造 ----------------

@pytest.mark.asyncio
async def test_wildcard_and_forged_scopes_do_not_match(middleware):
    for forged in (["*"], ["tasks:*"], ["tasks:run "], ["TASKS:RUN"], ["tasks:run:extra"], ""):
        token = make_key(middleware, granted=forged)
        resp = await dispatch(middleware, make_request(
            "/api/v1/evaluation/tasks/t1/run", token, method="POST"))
        assert resp.status_code == 403, f"伪造 scope {forged!r} 竟被放行"


# ---------------- 5. 请求头伪造 scope ----------------

@pytest.mark.asyncio
async def test_scope_header_injection_ignored(middleware):
    """伪造 X-Api-Scopes / X-OAuth-Scopes 头无效：服务端只认库内 scopes 列"""
    token = make_key(middleware, granted=["tasks:read"])
    forged = Request({
        "type": "http", "method": "POST",
        "path": "/api/v1/evaluation/tasks/t1/run",
        "raw_path": b"/api/v1/evaluation/tasks/t1/run",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"x-api-scopes", b"tasks:run"),
            (b"x-oauth-scopes", b"*"),
        ],
        "query_string": b"", "client": ("203.0.113.10", 51234),
        "server": ("testserver", 80), "scheme": "http",
    })
    resp = await dispatch(middleware, forged)
    assert resp.status_code == 403


# ---------------- 6. require_scope 依赖：JWT 放行、API Key 精确匹配 ----------------

@pytest.mark.asyncio
async def test_require_scope_matrix():
    """依赖本体矩阵：JWT 放行；API Key 持有放行；缺失/通配符/大小写伪造 403"""
    dep = scope_constants.require_scope(scope_constants.TASKS_RUN)

    def req_with(scopes_value):
        # starlette Request.state 是 scope["state"] 上的属性——从 scope 注入
        req = Request({"type": "http", "method": "GET", "path": "/probe/run",
                       "headers": [], "query_string": b"", "state": {}})
        req.state.api_key_scopes = scopes_value
        return req

    from fastapi import HTTPException

    # JWT 登录态（api_key_scopes=None）：沿用既有权限模型，放行
    await dep(req_with(None))
    # API Key 持有 scope：放行
    await dep(req_with(frozenset({"tasks:read", "tasks:run"})))
    # API Key 缺 scope / 通配符 / 前缀 / 大小写伪造：403
    for forged in (frozenset({"tasks:read"}), frozenset({"*"}), frozenset({"tasks:*"}),
                    frozenset({"TASKS:RUN"})):
        with pytest.raises(HTTPException) as exc:
            await dep(req_with(forged))
        assert exc.value.status_code == 403


def test_require_scope_rejects_unknown_scope_constant():
    with pytest.raises(ValueError):
        scope_constants.require_scope("not:a:scope")


# ---------------- 7. 注册表正则锚定（防前缀伪造） ----------------

def test_registry_regex_is_fully_anchored():
    scope_constants.reset_api_key_routes()
    scope_constants.register_api_key_route(
        "GET", "/api/v1/evaluation/tasks/{task_id}/gate", scope_constants.TASKS_READ)
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1/gate") == scope_constants.TASKS_READ
    # 前缀伪造：更长路径、更短路径、参数段带斜杠均不命中
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1/gate/x") is None
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1") is None
    assert scope_constants.match_api_key_route(
        "GET", "/api/v1/evaluation/tasks/t1/other") is None
    # 方法不匹配
    assert scope_constants.match_api_key_route(
        "POST", "/api/v1/evaluation/tasks/t1/gate") is None


def test_register_rejects_unknown_scope():
    with pytest.raises(ValueError):
        scope_constants.register_api_key_route("GET", "/x/{y}", "bogus:scope")
