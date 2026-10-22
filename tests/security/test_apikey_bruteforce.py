"""M1 QE1-1 安全测试：API Key 爆破节流（tests/security 三件套之一）

攻击路径复现（红→绿）：
1. 盲扫：同 IP 高频随机 token（每次不同前缀）——精确层 (IP+prefix) 计数
   天然失效，必须由 IP 层计数拦住（30 次/10 分钟 → 锁 15 分钟，429）；
2. 定向爆破：同 IP 固定前缀乱猜随机段——精确层 5 次/10 分钟 → 锁 15 分钟；
3. 锁定期内**正确 Key 也被拒**（429，先查锁后比对，不泄露 Key 有效性）；
4. 锁过期（TTL 到点）后正确 Key 恢复可用；
5. 吊销/过期 Key → 401（统一文案不区分原因，防探测）；
6. Redis 故障 → fail-open（认证仍按 hash 比对工作，不 5xx）。

基建：dict 假 Redis（带 TTL 时间推进）+ SQLite 内存库直替换中间件
SessionLocal；Request 用 starlette 原生 scope 构造（真实 url.path/headers/
client 语义，防 SimpleNamespace 假体掩盖路径归一化问题）。

独立测试角色；只新增测试，不修改实现代码。
"""
import hashlib
import secrets
import string
import time
from datetime import datetime, timedelta

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

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无真 DB，全部替身）


# ---------------- 测试基建 ----------------


class FakeRedis:
    """带 TTL 语义的 dict 假 Redis（incr/set/get/expire/delete/exists）"""

    def __init__(self):
        self._data = {}
        self._ttl = {}
        self.now = time.monotonic()

    def advance(self, seconds):
        self.now += seconds
        # 过期数据即时淘汰（含 TTL 的 key 到点即消失）
        for key in [k for k, deadline in self._ttl.items() if deadline <= self.now]:
            self._data.pop(key, None)
            self._ttl.pop(key, None)

    def _alive(self, key):
        if key in self._ttl and self._ttl[key] <= self.now:
            self._data.pop(key, None)
            self._ttl.pop(key, None)
            return None
        return self._data.get(key)

    def incr(self, key):
        value = int(self._alive(key) or 0) + 1
        self._data[key] = value
        return value

    def set(self, key, value, ex=None):
        self._data[key] = value
        if ex is not None:
            self._ttl[key] = self.now + ex
        else:
            self._ttl.pop(key, None)
        return True

    def get(self, key):
        return self._alive(key)

    def expire(self, key, seconds):
        if key in self._data:
            self._ttl[key] = self.now + seconds
            return True
        return False

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self._data:
                self._data.pop(key)
                self._ttl.pop(key, None)
                deleted += 1
        return deleted

    def exists(self, *keys):
        return sum(1 for key in keys if self._alive(key) is not None)


class ExplodingRedis(FakeRedis):
    """任何操作都抛错的假体（模拟 Redis 故障，验证 fail-open）"""

    def incr(self, key):
        raise ConnectionError("redis down")

    def exists(self, *keys):
        raise ConnectionError("redis down")


def make_request(path, token, ip="203.0.113.10", method="GET"):
    """构造真实 starlette Request（scope 语义与线上中间件一致）"""
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "headers": headers,
        "query_string": b"",
        "client": (ip, 51234),
        "server": ("testserver", 80),
        "scheme": "http",
    })


async def passthrough(request):
    """call_next 替身：回显 request.state 证明放行链路"""
    user = getattr(request.state, "user", None)
    return PlainTextResponse(f"ok:{user['user_code'] if user else 'none'}")


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def middleware(monkeypatch, fake_redis):
    """SQLite 内存库 + 假 Redis 的 AuthMiddleware（注册一条测试路由放行）"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)

    with TestSession() as db:
        db.add(User(user_code="sec-user", username="sec", is_active=True,
                    super_admin_flag=0, application_admin=0))
        db.commit()

    mw = AuthMiddleware(app=None, api_key_throttle=ApiKeyAuthThrottle(redis_client=fake_redis))
    mw._test_session = TestSession
    return mw


def make_key(middleware, scopes_granted=("tasks:read",), **overrides):
    """在测试库里直插一枚真 Key，返回明文 token"""
    plaintext, prefix, key_hash = api_key_service.generate_api_key()
    with middleware._test_session() as db:
        db.add(ApiKey(
            user_code=overrides.pop("user_code", "sec-user"),
            team_code="DEFAULT",
            name="sec-test",
            key_prefix=prefix,
            key_hash=key_hash,
            scopes=list(scopes_granted),
            **overrides,
        ))
        db.commit()
    return plaintext


@pytest.fixture(autouse=True)
def registered_route():
    """注册一条 scope 化测试路由（与 QE1-4 端点同机制）。

    快照/恢复而非清空：应用模块 import 期的注册（evaluation_task_api 等）
    必须在用例结束后原样保留——清空会让后续依赖注册表的用例（gate 端点等）
    看到被抹掉的白名单。
    """
    from app.core.scopes import _API_KEY_ROUTES, register_api_key_route
    snapshot = list(_API_KEY_ROUTES)
    register_api_key_route(
        "GET", "/api/v1/evaluation/tasks/{task_id}/gate", scope_constants.TASKS_READ
    )
    yield
    _API_KEY_ROUTES[:] = snapshot


async def dispatch(mw, request):
    return await mw.dispatch(request, passthrough)


def wrong_token_with_prefix(prefix: str) -> str:
    """构造与 victim 同前缀、随机段错误但文法/校验段合法的爆破 token"""
    random_part = prefix + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(18)
    )
    checksum = hashlib.sha256(random_part.encode()).hexdigest()[:4]
    return f"aievo_sk_{random_part}_{checksum}"


# ---------------- 1. 正确 Key 基线（节流与鉴权的放行路径） ----------------

@pytest.mark.asyncio
async def test_valid_key_passes_and_sets_state(middleware):
    token = make_key(middleware)
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t1/gate", token))
    assert resp.status_code == 200
    assert resp.body == b"ok:sec-user"


# ---------------- 2. 盲扫：随机 token 高频尝试 → IP 层锁 ----------------

@pytest.mark.asyncio
async def test_blind_bruteforce_random_prefixes_locked_by_ip(middleware, fake_redis):
    """每次换随机前缀的盲扫：精确层计数永远 1，必须由 IP 层（30 次）拦住"""
    from app.services import api_key_service as svc
    for i in range(30):
        # 生成 30 枚互不相同（前缀几乎必然互异）的合法文法 token
        guess, _, _ = svc.generate_api_key()
        resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", guess))
        assert resp.status_code == 401, f"第 {i + 1} 次未认证失败"
    # 第 31 次（IP 层已达 30）：即使换新 token 也 429
    fresh, _, _ = api_key_service.generate_api_key()
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", fresh))
    assert resp.status_code == 429
    assert "429" in resp.body.decode()


# ---------------- 3. 定向爆破：固定前缀猜随机段 → 精确层锁 ----------------

@pytest.mark.asyncio
async def test_targeted_prefix_guessing_locked_at_5(middleware):
    """攻击者已知前 8 位前缀，乱猜剩余段：5 次即锁该 (IP, prefix)"""
    victim_token = make_key(middleware)
    prefix = api_key_service.parse_api_key(victim_token)
    for i in range(5):
        guess = wrong_token_with_prefix(prefix)
        resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", guess))
        assert resp.status_code == 401, f"第 {i + 1} 次未认证失败"
    # 第 6 次：同前缀锁死
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", guess))
    assert resp.status_code == 429


# ---------------- 4. 锁定期内正确 Key 也被拒（不泄露有效性） ----------------

@pytest.mark.asyncio
async def test_locked_ip_rejects_even_valid_key(middleware):
    token = make_key(middleware)
    # 用同 IP 打满 5 次失败（恶意流量），把 (IP, prefix) 锁住
    prefix = api_key_service.parse_api_key(token)
    for _ in range(5):
        guess = wrong_token_with_prefix(prefix)
        await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", guess))
    # 正确 Key 在锁定期内 → 429（不是 200：先查锁后比对）
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t1/gate", token))
    assert resp.status_code == 429


# ---------------- 5. 锁过期恢复 ----------------

@pytest.mark.asyncio
async def test_lock_expires_after_ttl(middleware, fake_redis):
    token = make_key(middleware)
    prefix = api_key_service.parse_api_key(token)
    for _ in range(5):
        await dispatch(middleware, make_request(
            "/api/v1/evaluation/tasks/t/gate", wrong_token_with_prefix(prefix)))
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", token))
    assert resp.status_code == 429
    fake_redis.advance(901)  # 锁 15 分钟（900s）到期
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", token))
    assert resp.status_code == 200


# ---------------- 6. 吊销 / 过期 / 禁用用户 ----------------

@pytest.mark.asyncio
async def test_revoked_key_rejected_with_uniform_message(middleware):
    token = make_key(middleware, revoked_at=datetime.utcnow())
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", token))
    assert resp.status_code == 401
    assert resp.body.decode().find("无效或已失效") >= 0


@pytest.mark.asyncio
async def test_expired_key_rejected(middleware):
    token = make_key(middleware, expires_at=datetime.utcnow() - timedelta(minutes=1))
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", token))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_disabled_user_key_rejected_403(middleware):
    with middleware._test_session() as db:
        db.add(User(user_code="ghost", username="ghost", is_active=False))
        db.commit()
    token = make_key(middleware, user_code="ghost")
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", token))
    assert resp.status_code == 403


# ---------------- 7. 文法非法 token：按失败计（不 5xx） ----------------

@pytest.mark.asyncio
async def test_malformed_tokens_counted_and_rejected(middleware):
    for bad in ("aievo_sk_short", "aievo_sk_" + "a" * 26 + "_zzzz",  # 校验段非 hex
                "aievo_sk_" + "a" * 30 + "_abcd"):
        resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", bad))
        assert resp.status_code == 401, bad


# ---------------- 8. Redis 故障 → fail-open（认证仍工作，不 5xx） ----------------

@pytest.mark.asyncio
async def test_redis_outage_fails_open(middleware):
    middleware._api_key_throttle = ApiKeyAuthThrottle(redis_client=ExplodingRedis())
    token = make_key(middleware)
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t1/gate", token))
    assert resp.status_code == 200  # 正确 Key 仍可用
    random_part = "b" * 26
    bad = "aievo_sk_" + random_part + "_" + hashlib.sha256(random_part.encode()).hexdigest()[:4]
    resp = await dispatch(middleware, make_request("/api/v1/evaluation/tasks/t/gate", bad))
    assert resp.status_code == 401  # 错 Key 仍拒绝（hash 比对不依赖 Redis）
