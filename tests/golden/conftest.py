"""W17 golden 门禁的公共装置：时钟冻结。

逐字节存档的前提是输出完全确定。聚合（judge/rag 的 completed_at）、
JSON 导出（export_time）、xlsx（openpyxl docProps created/modified）、
latency 行记录（timestamp）都直接取 now() —— 统一冻结为固定时刻。
"""
import datetime as _dt
import types

import pytest

# 固定时钟：所有 golden 基线中的时间戳均为该值
FROZEN_NOW = _dt.datetime(2026, 1, 1, 12, 0, 0)


class _FrozenDatetime(_dt.datetime):
    """datetime 替身：now() 恒返回 FROZEN_NOW，其余行为不变"""

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return FROZEN_NOW.replace(tzinfo=tz)
        return FROZEN_NOW

    @classmethod
    def utcnow(cls):
        return FROZEN_NOW


@pytest.fixture
def frozen_clock(monkeypatch):
    """冻结聚合/导出链路的全部时钟源

    - app.services.result_aggregator：`from datetime import datetime`（聚合
      completed_at 与 export_to_json 的 export_time）
    - app.services.latency_evaluator：同上（行记录 timestamp）
    - openpyxl.packaging.core：`import datetime` 模块风格（xlsx docProps
      created），用 SimpleNamespace 代理模块属性
    - openpyxl.writer.excel：保存时重打 docProps modified（同模块风格）——
      不冻结则 xlsx 内容跨进程漂移
    - zipfile：xlsx 是 zip 包，每个条目的 date_time 取
      time.localtime(time.time())（ZipInfo 默认值）——同样冻结，否则
      xlsx 内容一致但 zip 头逐次漂移
    """
    import zipfile as zipfile_mod
    import app.services.result_aggregator as result_aggregator_mod
    import app.services.latency_evaluator as latency_evaluator_mod
    import openpyxl.packaging.core as openpyxl_core_mod
    import openpyxl.writer.excel as openpyxl_writer_mod

    monkeypatch.setattr(result_aggregator_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(latency_evaluator_mod, "datetime", _FrozenDatetime)
    openpyxl_datetime_proxy = types.SimpleNamespace(
        datetime=_FrozenDatetime,
        timezone=_dt.timezone,
        timedelta=_dt.timedelta,
        date=_dt.date,
        time=_dt.time,
    )
    monkeypatch.setattr(openpyxl_core_mod, "datetime", openpyxl_datetime_proxy)
    monkeypatch.setattr(openpyxl_writer_mod, "datetime", openpyxl_datetime_proxy)
    # 2026-01-01 12:00:00（周四）：struct_time 依次为
    # (年, 月, 日, 时, 分, 秒, 星期[0=周一], 年内天[1 起], 是否夏令时[-1 未知])
    import time as time_mod
    fixed_struct = time_mod.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, -1))
    monkeypatch.setattr(
        zipfile_mod,
        "time",
        types.SimpleNamespace(
            localtime=lambda t=None: fixed_struct,
            time=time_mod.time,
        ),
    )
    return FROZEN_NOW
