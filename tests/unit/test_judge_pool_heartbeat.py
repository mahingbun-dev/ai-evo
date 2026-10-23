"""心跳线程行为测试：续约、死线程回收、失效租约标记、注销

心跳线程本身不启动：register() 会拉起后台定时线程，所有用例都通过
class 级 monkeypatch 把 _ensure_started 变成 no-op，只驱动 run_once()。
"""
import threading

import pytest

from app.services.judge_pool import heartbeat as hb_mod
from app.services.judge_pool.client import Lease
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.heartbeat import JudgePoolHeartbeat, get_heartbeat

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_background_thread(monkeypatch):
    """绝不启动真实定时线程；register() 只登记条目"""
    monkeypatch.setattr(JudgePoolHeartbeat, "_ensure_started", lambda self: None)


class FakeClient:
    def __init__(self, renew_ok=True):
        self.renewed = []
        self.released = []
        self.renew_ok = renew_ok

    def renew(self, lease):
        self.renewed.append(lease.lease_id)
        return self.renew_ok

    def release(self, lease):
        self.released.append(lease.lease_id)
        return True


def _heartbeat(client):
    events = []
    hb = JudgePoolHeartbeat(
        client=client, event_sink=lambda event, lease, **kwargs: events.append(event)
    )
    return hb, events


def _dead_thread():
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    assert not t.is_alive()
    return t


# ---------- 1. 持有线程存活 → 续约 ----------


def test_renews_alive_thread():
    client = FakeClient()
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, threading.current_thread())
    hb.run_once()

    assert client.renewed == ["l1"]
    assert client.released == []
    assert lease.lost is False
    assert events == []


# ---------- 2. 持有线程已死 → 释放（不续约）并审计 ----------


def test_releases_dead_thread_instead_of_renewing():
    client = FakeClient()
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, _dead_thread())
    hb.run_once()

    assert client.released == ["l1"]
    assert client.renewed == []
    assert events == ["owner_thread_dead"]


def test_dead_thread_entry_removed_after_release():
    """回收后条目必须注销，否则每个心跳周期都会重复释放"""
    client = FakeClient()
    hb, _ = _heartbeat(client)

    hb.register(Lease(Engine.GRAPH, "l1", 0), _dead_thread())
    hb.run_once()
    hb.run_once()

    assert client.released == ["l1"]
    assert client.renewed == []


def test_dead_thread_lease_not_marked_lost(monkeypatch):
    """走 release 分支时不置 lost：持有线程已死，不存在重入放行问题"""
    client = FakeClient(renew_ok=False)
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, _dead_thread())
    hb.run_once()

    assert lease.lost is False
    assert events == ["owner_thread_dead"]


# ---------- 3. renew 返回 False → 已回收：注销 + 审计 + 标记 lost ----------


def test_lost_lease_marked_unregistered_and_audited():
    client = FakeClient(renew_ok=False)
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, threading.current_thread())
    hb.run_once()

    assert client.renewed == ["l1"]
    assert client.released == []
    assert lease.lost is True
    assert events == ["lease_lost"]

    # 已注销：下一个周期不再续约、不重复审计
    hb.run_once()
    assert client.renewed == ["l1"]
    assert events == ["lease_lost"]


# ---------- 4. unregister 后不再续约 ----------


def test_unregister_stops_renewal():
    client = FakeClient()
    hb, _ = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, threading.current_thread())
    hb.unregister("l1")
    hb.run_once()

    assert client.renewed == []
    assert client.released == []


def test_unregister_unknown_lease_is_noop():
    client = FakeClient()
    hb, _ = _heartbeat(client)

    hb.unregister("never-registered")
    hb.run_once()

    assert client.renewed == []
    assert client.released == []


# ---------- register 只负责登记与拉起定时线程 ----------


def test_register_ensures_timer_started(monkeypatch):
    calls = []
    hb, _ = _heartbeat(FakeClient())
    monkeypatch.setattr(hb, "_ensure_started", lambda: calls.append(1))

    hb.register(Lease(Engine.GRAPH, "l1", 0), threading.current_thread())

    assert calls == [1]


def test_get_heartbeat_returns_singleton(monkeypatch):
    monkeypatch.setattr(hb_mod, "_heartbeat", None)

    assert get_heartbeat() is get_heartbeat()
