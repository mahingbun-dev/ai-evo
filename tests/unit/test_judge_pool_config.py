"""配置加载/保存/缓存测试（Redis 用最小假实现）"""
import pytest

from app.services.judge_pool import config as cfg_mod
from app.services.judge_pool.engine import Engine

pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.raise_on = None
        self.db_config = None
        self.saved_db_payloads = []

    def hgetall(self, key):
        if self.raise_on == "hgetall":
            raise ConnectionError("redis down")
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or kwargs)

    def hsetnx(self, key, field, value):
        h = self.hashes.setdefault(key, {})
        if field in h:
            return 0
        h[field] = value
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cfg_mod, "_redis", lambda: fake)
    monkeypatch.setattr(cfg_mod, "_save_to_db", lambda payload, user: fake.saved_db_payloads.append(payload))
    monkeypatch.setattr(cfg_mod, "_load_from_db", lambda: fake.db_config)
    cfg_mod.invalidate_config_cache()
    yield fake
    cfg_mod.invalidate_config_cache()


def test_defaults_when_redis_empty(fake_redis):
    cfg = cfg_mod.load_config()
    assert cfg.graph_limit == 16
    assert cfg.limit_for(Engine.PLUGIN) == 4
    assert cfg.wait_timeout_seconds == 900


def test_save_then_load(fake_redis):
    cfg_mod.save_config({"graph_limit": 3, "plugin_limit": 1}, "tester")
    cfg = cfg_mod.load_config(force=True)
    assert cfg.graph_limit == 3
    assert cfg.plugin_limit == 1


def test_cache_hit_within_ttl(fake_redis, monkeypatch):
    cfg_mod.load_config()
    fake_redis.hset(cfg_mod.conf_key(), mapping={"graph_limit": "99"})
    assert cfg_mod.load_config().graph_limit == 16
    monkeypatch.setattr(cfg_mod, "CONFIG_CACHE_TTL_SECONDS", 0)
    assert cfg_mod.load_config().graph_limit == 99


def test_redis_down_returns_last_known(fake_redis):
    cfg_mod.load_config()
    fake_redis.raise_on = "hgetall"
    assert cfg_mod.load_config(force=True).graph_limit == 16


def test_empty_redis_backfills_from_db(fake_redis):
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=5, plugin_limit=2, fail_open=False)
    cfg = cfg_mod.load_config(force=True)
    assert cfg.graph_limit == 5
    assert cfg.plugin_limit == 2
    assert cfg.fail_open is False
    stored = fake_redis.hashes[cfg_mod.conf_key()]
    assert stored["graph_limit"] == "5"
    assert stored["plugin_limit"] == "2"
    assert stored["fail_open"] == "0"


def test_redis_down_falls_back_to_db(fake_redis):
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=7, harness_limit=3)
    fake_redis.raise_on = "hgetall"
    cfg = cfg_mod.load_config(force=True)
    assert cfg.graph_limit == 7
    assert cfg.harness_limit == 3


def test_save_merges_on_db_values_not_defaults(fake_redis):
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=2, harness_limit=3)
    cfg = cfg_mod.save_config({"plugin_limit": 1}, "tester")
    assert cfg.graph_limit == 2
    assert cfg.harness_limit == 3
    assert cfg.plugin_limit == 1
    assert fake_redis.saved_db_payloads[-1]["graph_limit"] == 2


def test_save_rejects_invalid_int_before_side_effects(fake_redis):
    with pytest.raises(ValueError):
        cfg_mod.save_config({"graph_limit": "abc"})
    assert fake_redis.saved_db_payloads == []
    assert fake_redis.hashes == {}


def test_save_normalizes_bool_strings(fake_redis):
    assert cfg_mod.save_config({"fail_open": " TRUE \n"}).fail_open is True
    assert cfg_mod.save_config({"fail_open": "off"}).fail_open is False
    assert cfg_mod.save_config({"fail_open": True}).fail_open is True
