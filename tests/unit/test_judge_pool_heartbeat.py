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


# ---------- 3b. renew 返回 None（传输异常）→ 保持登记，下轮重试 ----------


def test_transport_error_keeps_lease_registered_and_retries():
    """Redis 抖动跨过一个心跳周期不得停掉续约

    判失效会让本进程所有在飞租约停止续约，TTL 后额度被回收而调用仍在跑
    （plugin/CC 合法占用 30–120 分钟），池再放 limit 个进来 → 反向超发。
    """
    client = FakeClient(renew_ok=None)
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, threading.current_thread())
    hb.run_once()
    hb.run_once()

    assert lease.lost is False
    assert events == []
    assert client.released == []
    assert client.renewed == ["l1", "l1"], "None 时必须保持登记并在下一轮继续重试"


def test_transport_error_then_success_recovers():
    """抖动恢复后续约成功：租约全程不被标记失效"""
    client = FakeClient(renew_ok=None)
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, threading.current_thread())
    hb.run_once()
    client.renew_ok = True
    hb.run_once()

    assert lease.lost is False
    assert events == []
    assert client.renewed == ["l1", "l1"]


def test_script_zero_still_marks_lost():
    """与 None 区分开：脚本明确返回 0（租约已被回收）才判失效"""
    client = FakeClient(renew_ok=False)
    hb, events = _heartbeat(client)
    lease = Lease(Engine.GRAPH, "l1", 0)

    hb.register(lease, threading.current_thread())
    hb.run_once()

    assert lease.lost is True
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


# ---------- 5. 进程退出：批量释放本进程租约（I3） ----------


def test_shutdown_releases_every_registered_lease():
    """只置位不释放会让每次发布留下最长 lease_ttl 的幽灵占用"""
    client = FakeClient()
    hb, events = _heartbeat(client)
    hb.register(Lease(Engine.GRAPH, "l1", 0), threading.current_thread())
    hb.register(Lease(Engine.PLUGIN, "l2", 0), threading.current_thread())

    hb.shutdown()

    assert sorted(client.released) == ["l1", "l2"]
    assert hb._entries == {}
    assert hb._stop.is_set()


def test_shutdown_stops_later_renewal():
    """退出后再驱动 run_once 不得继续续约（条目已清空）"""
    client = FakeClient()
    hb, _ = _heartbeat(client)
    hb.register(Lease(Engine.GRAPH, "l1", 0), threading.current_thread())

    hb.shutdown()
    client.renewed.clear()
    hb.run_once()

    assert client.renewed == []
    assert client.released == ["l1"], "释放只应发生一次"


def test_shutdown_releases_remaining_leases_when_one_fails():
    class FlakyClient(FakeClient):
        def release(self, lease):
            if lease.lease_id == "l1":
                raise ConnectionError("redis down")
            return super().release(lease)

    client = FlakyClient()
    hb, _ = _heartbeat(client)
    hb.register(Lease(Engine.GRAPH, "l1", 0), threading.current_thread())
    hb.register(Lease(Engine.GRAPH, "l2", 0), threading.current_thread())

    hb.shutdown()  # 不抛

    assert client.released == ["l2"], "单条释放失败不能挡住其余租约"
    assert hb._entries == {}


def test_shutdown_without_entries_is_noop():
    client = FakeClient()
    hb, _ = _heartbeat(client)

    hb.shutdown()

    assert client.released == []
    assert client.renewed == []
    assert hb._stop.is_set()


def test_shutdown_tolerates_unavailable_client():
    """退出时 Redis/客户端不可用不得抛异常（TTL 兜底回收）"""

    class BoomClient:
        def release(self, lease):
            raise ConnectionError("redis down")

    hb, _ = _heartbeat(BoomClient())
    hb.register(Lease(Engine.GRAPH, "l1", 0), threading.current_thread())

    hb.shutdown()  # 不抛

    assert hb._entries == {}


def test_ensure_started_registers_shutdown_hook_once(monkeypatch):
    """atexit 挂的必须是 shutdown（不是只置位的 stop），且重复启动不重复注册"""
    import threading as real_threading
    from types import SimpleNamespace

    # 本用例要跑真实的 _ensure_started：撤销 autouse fixture 打的 no-op 补丁
    monkeypatch.undo()

    created = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self.name = name
            self.alive = True
            created.append(self)

        def start(self):
            pass

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(
        hb_mod, "threading",
        SimpleNamespace(Event=real_threading.Event, Lock=real_threading.Lock, Thread=_FakeThread),
    )
    registered = []
    monkeypatch.setattr(hb_mod, "atexit", SimpleNamespace(register=registered.append))

    hb = hb_mod.JudgePoolHeartbeat(client=FakeClient())
    hb._ensure_started()
    assert len(created) == 1
    assert registered == [hb.shutdown], "退出钩子必须是 shutdown（只置位的 stop 不释放租约）"

    created[0].alive = False  # 模拟心跳线程意外退出后被重新拉起
    hb._ensure_started()

    assert len(created) == 2, "线程退出后应重新拉起"
    assert registered == [hb.shutdown], "重复启动不得重复注册 atexit"
