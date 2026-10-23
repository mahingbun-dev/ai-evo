"""最小 5 段 cron 解析与下次触发计算（M5 QE5-3）

为定时评测（task_schedules）提供无新依赖的 cron 能力（不引 croniter）：

- 支持 5 段标准 cron（分 时 日 月 周，北京时间口径）；
- 每段支持 ``*``、``*/n``、``a``、``a-b``、``a-b/n`` 与逗号列表组合
  （vixie 子集；周字段 0-6，7 归一为 0=周日；暂不支持英文月/周名与
  ``a/n`` 起始步进简写——前端提供常用预设，复杂表达式用户自担）；
- 日/周同时受限时按标准 cron 语义取「或」匹配（两者任一命中即算命中）；
- ``cron_next`` 分钟粒度向前搜（先按日跳步再按分钟步进，一年封顶），
  一年内无命中返回 None（upsert 侧据此拒绝「永不触发」的表达式）。
"""
from datetime import datetime, timedelta
from typing import Optional, Set, Tuple

from app.services.stats_service import BEIJING_TZ

# 字段定义：(名称, 最小值, 最大值)；顺序即表达式分段顺序
_CRON_FIELDS: Tuple[Tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)

# 向前搜索上限：一年（分钟粒度封顶，防「0 0 30 2 *」类永不触发表达式死循环）
_MAX_LOOKAHEAD_DAYS = 366

_FIELD_LABELS = "分/时/日/月/周"


def _parse_field(raw: str, name: str, lo: int, hi: int) -> Tuple[Set[int], bool]:
    """解析单个 cron 字段，返回 (允许值集合, 是否受限)。

    ``*``（无步进）视为「不受限」——用于日/周字段的 vixie 或语义判定；
    ``*/n`` 及任何显式取值都算受限。
    """
    raw = raw.strip()
    if not raw:
        raise ValueError(f"cron {name} 字段为空")
    allowed: Set[int] = set()
    unrestricted = False
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron {name} 字段含空片段")
        step = 1
        range_part = part
        if "/" in part:
            range_part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                raise ValueError(f"cron {name} 字段步进非法：{step_str}")
            if step < 1:
                raise ValueError(f"cron {name} 字段步进须 ≥1：{step}")
        if range_part == "*":
            start, end = lo, hi
            if step == 1:
                unrestricted = True
        elif "-" in range_part:
            a, b = range_part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise ValueError(f"cron {name} 字段区间非法：{range_part}")
        else:
            try:
                start = end = int(range_part)
            except ValueError:
                raise ValueError(f"cron {name} 字段取值非法：{range_part}")
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron {name} 字段越界（{lo}-{hi}）：{range_part}")
    allowed.update(range(start, end + 1, step))
    return allowed, unrestricted


def parse_cron(expr: str) -> dict:
    """解析 5 段 cron 表达式，返回各字段允许值集合与受限标记；非法抛 ValueError。"""
    if not expr or not expr.strip():
        raise ValueError("cron 表达式不能为空")
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron 表达式须为 5 段（{_FIELD_LABELS}）：{expr!r}")
    parsed = {}
    for raw, (name, lo, hi) in zip(parts, _CRON_FIELDS):
        # 周字段按 vixie 兼容放行 7（=周日），解析后归一为 0
        effective_hi = 7 if name == "weekday" else hi
        allowed, unrestricted = _parse_field(raw, name, lo, effective_hi)
        if name == "weekday" and 7 in allowed:
            allowed.discard(7)
            allowed.add(0)
        parsed[name] = {"allowed": allowed, "restricted": not unrestricted}
    return parsed


def _date_matches(parsed: dict, dt: datetime) -> bool:
    """月/日/周四字段匹配（日/周同时受限时取或——标准 cron 语义）。"""
    if dt.month not in parsed["month"]["allowed"]:
        return False
    # Python weekday(): 周一=0..周日=6；cron 周字段：周日=0..周六=6
    cron_dow = (dt.weekday() + 1) % 7
    dom_ok = dt.day in parsed["day"]["allowed"]
    dow_ok = cron_dow in parsed["weekday"]["allowed"]
    if parsed["day"]["restricted"] and parsed["weekday"]["restricted"]:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def cron_next(expr: str, after: datetime) -> Optional[datetime]:
    """计算 after 之后的下一次触发时刻（北京时间、分钟粒度）。

    一年内无命中返回 None。after 带任意时区偏移均归一到北京口径。
    """
    parsed = parse_cron(expr)
    if after.tzinfo is None:
        cur = after.replace(tzinfo=BEIJING_TZ)
    else:
        cur = after.astimezone(BEIJING_TZ)
    # 从下一分钟整开始（after 所在分钟已过，不重复触发）
    cur = (cur.replace(second=0, microsecond=0) + timedelta(minutes=1))
    deadline = cur + timedelta(days=_MAX_LOOKAHEAD_DAYS)
    while cur < deadline:
        if not _date_matches(parsed, cur):
            # 日期不匹配：直接跳到次日 00:00（避免逐分钟空转）
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if (cur.minute in parsed["minute"]["allowed"]
                and cur.hour in parsed["hour"]["allowed"]):
            return cur
        cur += timedelta(minutes=1)
    return None


def validate_cron(expr: str) -> datetime:
    """校验 cron 表达式（语法 + 一年内至少触发一次），返回下次触发时刻。

    供 upsert_schedule 与 API 层共用；非法抛 ValueError（消息可直接回给用户）。
    """
    nxt = cron_next(expr, datetime.now(BEIJING_TZ))
    if nxt is None:
        raise ValueError(f"cron 表达式一年内无触发时刻：{expr!r}")
    return nxt
