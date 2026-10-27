"""钉住 conftest 的 ragas 遥测线程歼灭逻辑（CI 静默死真凶的根治点）

import ragas 会在模块级启动 AnalyticsBatcher daemon 线程（每秒
time.sleep(1) 轮询上报缓冲、永不退出）。测试进程里任何把全局 time.sleep
换成 no-op 假时钟的用例都会被它放大成 CPU 空转（2026-10-26 CI 三轮
30 分钟整段无输出被杀的事故根因）。tests/conftest.py 在收集期把它关掉，
本用例守住不回退——若这里红了，检查 conftest 顶部歼灭块是否被人移除。
"""
import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def test_ragas_analytics_batcher_thread_neutered():
    from ragas import _analytics

    assert _analytics._analytics_batcher._running is False, (
        "ragas 遥测 flush 线程在测试进程中存活：它会以每秒 time.sleep(1) "
        "轮询整个套件生命周期，任何 no-op 假时钟测试都会被它放大成 CPU "
        "空转（CI 静默死真凶）。tests/conftest.py 的歼灭块被移除了？"
    )


def test_ragas_do_not_track_enabled():
    from ragas._analytics import do_not_track

    assert do_not_track() is True, (
        "测试进程必须不外发 ragas 匿名遥测（conftest 设 RAGAS_DO_NOT_TRACK）"
    )
