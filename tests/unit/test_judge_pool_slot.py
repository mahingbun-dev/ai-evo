import pytest

from app.services.judge_pool import slot as slot_mod
from app.services.judge_pool.client import Lease
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.slot import judge_pool_slot, pool_enabled

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, engine, meta=None):
        lease = Lease(Engine(engine), "lease-1", 0, meta)
        self.acquired.append((engine, meta))
        return lease

    def release(self, lease):
        self.released.append(lease.lease_id)
        return True


def test_disabled_flag_is_noop(monkeypatch):
    monkeypatch.setenv("JUDGE_POOL_ENABLED", "false")
    assert pool_enabled() is False
    with judge_pool_slot(Engine.GRAPH) as lease:
        assert lease is None


def test_enabled_acquires_and_releases(monkeypatch):
    monkeypatch.setenv("JUDGE_POOL_ENABLED", "true")
    fake = FakeClient()
    monkeypatch.setattr(slot_mod, "get_pool_client", lambda: fake)
    monkeypatch.setattr(slot_mod, "get_scanner", lambda: None)
    with judge_pool_slot("graph", meta={"call_site": "test"}) as lease:
        assert lease.lease_id == "lease-1"
    assert fake.released == ["lease-1"]


def test_release_on_exception(monkeypatch):
    monkeypatch.setenv("JUDGE_POOL_ENABLED", "true")
    fake = FakeClient()
    monkeypatch.setattr(slot_mod, "get_pool_client", lambda: fake)
    monkeypatch.setattr(slot_mod, "get_scanner", lambda: None)
    with pytest.raises(ValueError):
        with judge_pool_slot(Engine.GRAPH):
            raise ValueError("boom")
    assert fake.released == ["lease-1"]
