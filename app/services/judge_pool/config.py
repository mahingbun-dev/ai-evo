"""并发池配置：Redis 为热源，DB 为持久化，进程内 5 秒缓存"""
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

from .engine import Engine
from .redis_keys import conf_key

logger = logging.getLogger(__name__)

CONFIG_CACHE_TTL_SECONDS = 5

_INT_FIELDS = (
    "graph_limit", "claude_agent_limit", "harness_limit", "plugin_limit",
    "lease_ttl_seconds", "heartbeat_interval_seconds",
    "wait_timeout_seconds", "scan_interval_seconds", "audit_retention_days",
)
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class JudgePoolConfig:
    graph_limit: int = 16
    claude_agent_limit: int = 8
    harness_limit: int = 8
    plugin_limit: int = 4
    lease_ttl_seconds: int = 180
    heartbeat_interval_seconds: int = 30
    wait_timeout_seconds: int = 900
    scan_interval_seconds: int = 60
    audit_retention_days: int = 30
    fail_open: bool = True

    def limit_for(self, engine: Engine) -> int:
        return {
            Engine.GRAPH: self.graph_limit,
            Engine.CLAUDE_AGENT: self.claude_agent_limit,
            Engine.HARNESS: self.harness_limit,
            Engine.PLUGIN: self.plugin_limit,
        }[engine]

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULTS = JudgePoolConfig()

_lock = threading.Lock()
_cached: Optional[JudgePoolConfig] = None
_cached_at: float = 0.0


def _redis():
    from app.services.redis_stop_signal import get_redis_client

    return get_redis_client()


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValueError(f"无法解析为布尔值: {value!r}")


def _parse_int(field: str, value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} 必须是整数: {value!r}") from e


def _from_mapping(raw: dict) -> JudgePoolConfig:
    values = DEFAULTS.to_dict()
    for field in _INT_FIELDS:
        if raw.get(field) not in (None, ""):
            try:
                values[field] = int(raw[field])
            except (TypeError, ValueError):
                pass
    if raw.get("fail_open") not in (None, ""):
        try:
            values["fail_open"] = _parse_bool(raw["fail_open"])
        except ValueError:
            pass
    return JudgePoolConfig(**values)


def _normalize_payload(payload: dict) -> dict:
    """写侧归一化：非法值显式抛 ValueError，未知字段忽略。"""
    normalized = {}
    for key, value in payload.items():
        if key in _INT_FIELDS:
            normalized[key] = _parse_int(key, value)
        elif key == "fail_open":
            normalized[key] = _parse_bool(value)
    return normalized


def _mapping_from_config(cfg: JudgePoolConfig) -> dict:
    return {k: ("1" if v is True else "0" if v is False else str(v)) for k, v in cfg.to_dict().items()}


def invalidate_config_cache() -> None:
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0


def load_config(force: bool = False) -> JudgePoolConfig:
    global _cached, _cached_at
    now = time.monotonic()
    if not force and _cached is not None and now - _cached_at < CONFIG_CACHE_TTL_SECONDS:
        return _cached
    try:
        client = _redis()
        raw = client.hgetall(conf_key())
        if not raw:
            raw = _mapping_from_config(_load_from_db() or DEFAULTS)
            client.hset(conf_key(), mapping=raw)
        cfg = _from_mapping(raw)
    except Exception as e:  # noqa: BLE001 - 配置读取失败不能阻断评测
        logger.warning(f"[judge_pool] 读取配置失败，回退持久化配置/上次值/默认值: {e}")
        cfg = _load_from_db() or _cached or DEFAULTS
    with _lock:
        _cached = cfg
        _cached_at = now
    return cfg


def _load_from_db() -> Optional[JudgePoolConfig]:
    try:
        from app.db.session import SessionLocal
        from app.models.judge_pool_models import JudgePoolConfig as ConfigRow

        with SessionLocal() as db:
            row = db.query(ConfigRow).order_by(ConfigRow.id.asc()).first()
            if not row:
                return None
            return JudgePoolConfig(
                graph_limit=row.graph_limit,
                claude_agent_limit=row.claude_agent_limit,
                harness_limit=row.harness_limit,
                plugin_limit=row.plugin_limit,
                lease_ttl_seconds=row.lease_ttl_seconds,
                heartbeat_interval_seconds=row.heartbeat_interval_seconds,
                wait_timeout_seconds=row.wait_timeout_seconds,
                scan_interval_seconds=row.scan_interval_seconds,
                audit_retention_days=row.audit_retention_days,
                fail_open=bool(row.fail_open),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[judge_pool] 读取配置表失败: {e}")
        return None


def _save_to_db(payload: dict, updated_by: str = "") -> None:
    try:
        from app.db.session import SessionLocal
        from app.models.judge_pool_models import JudgePoolConfig as ConfigRow

        with SessionLocal() as db:
            row = db.query(ConfigRow).order_by(ConfigRow.id.asc()).first()
            if row is None:
                row = ConfigRow(id=1)
                db.add(row)
            for key, value in payload.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.updated_by = updated_by
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[judge_pool] 配置持久化失败（仅内存/Redis 生效）: {e}")


def save_config(payload: dict, updated_by: str = "") -> JudgePoolConfig:
    # 先校验，非法输入在任何副作用（DB/Redis）之前失败
    normalized = _normalize_payload(payload)
    # DB 是持久化源：以其当前值为底，避免用故障态的默认值覆盖运维配置
    current = (_load_from_db() or load_config(force=True)).to_dict()
    current.update(normalized)
    cfg = JudgePoolConfig(**current)
    _save_to_db(current, updated_by)
    try:
        _redis().hset(conf_key(), mapping=_mapping_from_config(cfg))
    except Exception as e:  # noqa: BLE001
        logger.error(f"[judge_pool] 配置写入 Redis 失败: {e}")
    invalidate_config_cache()
    with _lock:
        global _cached, _cached_at
        _cached = cfg
        _cached_at = time.monotonic()
    return cfg
