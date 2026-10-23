"""配置加载/保存/缓存测试（Redis 用最小假实现）"""
import pytest

from app.services.judge_pool import config as cfg_mod
from app.services.judge_pool.engine import Engine
from app.services.judge_pool.errors import JudgePoolError, JudgePoolRedisSyncError

pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.raise_on = None
        self.db_config = None
        self.db_ok = True  # DB 读取是否成功（与「没有配置行」区分）
        self.saved_db_payloads = []

    def hgetall(self, key):
        if self.raise_on == "hgetall":
            raise ConnectionError("redis down")
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        if self.raise_on == "hset":
            raise ConnectionError("redis down")
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

    def _save_to_db(payload, updated_by=""):
        fake.saved_db_payloads.append(payload)
        return True  # 与真实实现一致：成功返回 True

    monkeypatch.setattr(cfg_mod, "_redis", lambda: fake)
    monkeypatch.setattr(cfg_mod, "_save_to_db", _save_to_db)
    monkeypatch.setattr(cfg_mod, "_load_from_db", lambda: fake.db_config)
    monkeypatch.setattr(cfg_mod, "_load_from_db_checked", lambda: (fake.db_config, fake.db_ok))
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


# ---------------------------------------------------------------------------
# 不变式：心跳必须小于租约的一半（写入基准 = DB 现值合并后）
# ---------------------------------------------------------------------------


def test_save_rejects_heartbeat_not_less_than_half_ttl_after_merge(fake_redis):
    """基准分叉：热源 ttl=180、DB 现值 ttl=60，只提交心跳必须按 60 判"""
    cfg_mod.load_config(force=True)  # 缓存/Redis 基准 180
    fake_redis.db_config = cfg_mod.JudgePoolConfig(lease_ttl_seconds=60)
    with pytest.raises(ValueError, match="心跳"):
        cfg_mod.save_config({"heartbeat_interval_seconds": 30})
    assert fake_redis.saved_db_payloads == []  # 校验先于任何副作用


def test_save_rejects_heartbeat_equal_half_ttl(fake_redis):
    with pytest.raises(ValueError, match="心跳"):
        cfg_mod.save_config({"lease_ttl_seconds": 60, "heartbeat_interval_seconds": 30})


def test_save_allows_heartbeat_below_half_ttl(fake_redis):
    cfg = cfg_mod.save_config({"lease_ttl_seconds": 60, "heartbeat_interval_seconds": 29})
    assert cfg.heartbeat_interval_seconds == 29
    assert fake_redis.saved_db_payloads[-1]["lease_ttl_seconds"] == 60


# ---------------------------------------------------------------------------
# strict：持久化链路失败必须显式暴露
# ---------------------------------------------------------------------------


def test_save_strict_raises_when_db_persist_fails(fake_redis, monkeypatch):
    monkeypatch.setattr(cfg_mod, "_save_to_db", lambda payload, updated_by="": False)
    with pytest.raises(JudgePoolError, match="持久化"):
        cfg_mod.save_config({"graph_limit": 8}, strict=True)


def test_save_non_strict_ignores_db_persist_failure(fake_redis, monkeypatch):
    """默认行为不变：DB 失败只记日志，配置仍在 Redis/内存生效"""
    monkeypatch.setattr(cfg_mod, "_save_to_db", lambda payload, updated_by="": False)
    assert cfg_mod.save_config({"graph_limit": 8}).graph_limit == 8


def test_save_strict_reports_redis_sync_failure(fake_redis):
    fake_redis.raise_on = "hset"
    with pytest.raises(JudgePoolRedisSyncError) as exc:
        cfg_mod.save_config({"graph_limit": 8}, strict=True)
    assert exc.value.config.graph_limit == 8
    assert fake_redis.saved_db_payloads[-1]["graph_limit"] == 8  # DB 已落库


# ---------------------------------------------------------------------------
# I4：DB 读失败不得回填默认值；扫描周期做 DB→Redis 对账
# ---------------------------------------------------------------------------


def test_db_read_failure_does_not_backfill_defaults_into_redis(fake_redis):
    """Redis 空 + DB 读失败：用 DEFAULTS 回填会把故障态默认值永久压住 DB 真值

    回填后 Redis 非空，load_config 的「空才回落 DB」再不触发 —— 分叉不可自愈。
    """
    fake_redis.db_ok = False
    fake_redis.db_config = None

    cfg = cfg_mod.load_config(force=True)

    assert cfg == cfg_mod.DEFAULTS
    assert fake_redis.hashes == {}, "DB 读失败时不得写 Redis"


def test_db_read_failure_prefers_last_known_over_defaults(fake_redis):
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=5)
    assert cfg_mod.load_config(force=True).graph_limit == 5
    fake_redis.hashes.clear()  # 模拟 Redis 被清空
    fake_redis.db_ok = False

    assert cfg_mod.load_config(force=True).graph_limit == 5, "应回退上次已知值而非默认值"
    assert fake_redis.hashes == {}


def test_db_row_still_backfills_redis(fake_redis):
    """与「读失败」区分开：DB 读成功且有配置行时照旧回填"""
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=5)

    assert cfg_mod.load_config(force=True).graph_limit == 5
    assert fake_redis.hashes[cfg_mod.conf_key()]["graph_limit"] == "5"


def test_reconcile_pulls_redis_back_to_db_after_failed_write(fake_redis):
    """strict 保存时 Redis 写失败 → DB 新值 / Redis 旧值，必须由对账收敛"""
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=16)
    assert cfg_mod.reconcile_config_to_redis() is True
    assert fake_redis.hashes[cfg_mod.conf_key()]["graph_limit"] == "16"

    # 运维改成 3：DB 落库成功、Redis 写失败（hash 里还是 16）
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=3)
    fake_redis.raise_on = "hset"
    with pytest.raises(JudgePoolRedisSyncError):
        cfg_mod.save_config({"graph_limit": 3}, "tester", strict=True)
    assert fake_redis.hashes[cfg_mod.conf_key()]["graph_limit"] == "16"
    assert cfg_mod.load_config(force=True).graph_limit == 16, "分叉期读到的是 Redis 旧值"

    # Redis 恢复后，一次对账就把热源拉回 DB 值
    fake_redis.raise_on = None
    assert cfg_mod.reconcile_config_to_redis() is True
    assert fake_redis.hashes[cfg_mod.conf_key()]["graph_limit"] == "3"
    assert cfg_mod.load_config(force=True).graph_limit == 3


def test_reconcile_noop_when_already_consistent(fake_redis):
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=5)

    assert cfg_mod.reconcile_config_to_redis() is True  # 首次写入
    assert cfg_mod.reconcile_config_to_redis() is False  # 已一致：不再写


def test_reconcile_skips_when_db_read_fails(fake_redis):
    """DB 读失败不得对账：否则等于用故障态默认值覆盖热源"""
    fake_redis.hashes[cfg_mod.conf_key()] = {"graph_limit": "7"}
    fake_redis.db_ok = False
    fake_redis.db_config = None

    assert cfg_mod.reconcile_config_to_redis() is False
    assert fake_redis.hashes[cfg_mod.conf_key()] == {"graph_limit": "7"}


def test_reconcile_skips_when_no_db_row(fake_redis):
    fake_redis.hashes[cfg_mod.conf_key()] = {"graph_limit": "7"}
    fake_redis.db_config = None

    assert cfg_mod.reconcile_config_to_redis() is False
    assert fake_redis.hashes[cfg_mod.conf_key()] == {"graph_limit": "7"}


def test_reconcile_survives_redis_failure(fake_redis):
    fake_redis.db_config = cfg_mod.JudgePoolConfig(graph_limit=5)
    fake_redis.raise_on = "hgetall"

    assert cfg_mod.reconcile_config_to_redis() is False  # 不抛，下轮重试


class _ScannerFakeRedis:
    """扫描器用的最小假 Redis：锁 + 峰值 + 配置 hash"""

    def __init__(self):
        self.hashes = {}
        self.set_calls = []

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        return True

    def zcard(self, key):
        return 0

    def eval(self, script, numkeys, *args):
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or kwargs)


def test_scanner_run_once_reconciles_config_before_use(monkeypatch):
    """扫描周期必须做一次 DB→Redis 对账：这是 Redis 写失败后唯一的收敛路径"""
    import app.db.session as db_session

    from app.services.judge_pool import scanner as scan_mod

    def _no_db():
        raise RuntimeError("单测不连库")

    monkeypatch.setattr(db_session, "SessionLocal", _no_db)

    order = []
    monkeypatch.setattr(scan_mod, "reconcile_config_to_redis", lambda: order.append("reconcile"))
    monkeypatch.setattr(
        scan_mod, "load_config",
        lambda force=False: order.append("load_config") or cfg_mod.DEFAULTS,
    )

    class _Client:
        def prune_expired(self, engine):
            return []

    scanner = scan_mod.JudgePoolScanner(client=_Client())
    monkeypatch.setattr(scanner, "_redis", lambda: _ScannerFakeRedis())

    scanner.run_once()

    assert order[0] == "reconcile", "对账必须发生（且先于按配置干活）"
    assert "load_config" in order


def test_scanner_run_once_survives_reconcile_failure(monkeypatch):
    """对账抛异常不能打断僵尸清理"""
    import app.db.session as db_session

    from app.services.judge_pool import scanner as scan_mod

    def _no_db():
        raise RuntimeError("单测不连库")

    monkeypatch.setattr(db_session, "SessionLocal", _no_db)

    def _boom():
        raise RuntimeError("对账炸了")

    pruned = []
    monkeypatch.setattr(scan_mod, "reconcile_config_to_redis", _boom)
    monkeypatch.setattr(scan_mod, "load_config", lambda force=False: cfg_mod.DEFAULTS)

    class _Client:
        def prune_expired(self, engine):
            pruned.append(engine.value)
            return []

    scanner = scan_mod.JudgePoolScanner(client=_Client())
    monkeypatch.setattr(scanner, "_redis", lambda: _ScannerFakeRedis())

    scanner.run_once()  # 不抛

    assert len(pruned) == len(list(scan_mod.Engine)), "对账失败不得影响逐引擎清理"
