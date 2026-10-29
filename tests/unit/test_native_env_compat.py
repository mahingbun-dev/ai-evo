"""原生环境兼容性守卫测试（无环境桩时本机直跑）

背景：一期端到端曾依赖两项临时缓解（nest_asyncio no-op 桩 + PGGSSENCMODE=disable
环境变量），现已改为代码级守卫：
- auth_handler / rag_evaluator：Python 3.14 下不再 apply nest_asyncio
  （nest_asyncio 1.6.0 在 3.14 破坏 sniffio/anyio 上下文检测 → claude-agent-sdk
  抛 NoEventLoopError）；3.12（生产镜像）行为不变。
  单测进程（pytest 在 sys.modules）同样跳过：嵌套补丁是进程级 patch，全量套件
  下嵌套 loop 累积陈旧 I/O，会让 deepeval 度量的 run_until_complete 在 select
  上挂死（Python 3.13 CI 实测整轮无输出直至被杀）。
- app/db/session.py：macOS 上默认 PGGSSENCMODE=disable（libpq GSS 初始化在
  celery prefork 子进程内 SIGSEGV）。
本文件守住这两处不回退。
"""
import asyncio
import inspect
import os
import platform
import sys

import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


def _loop_method_module() -> str:
    """nest_asyncio.apply() 会替换模块级 asyncio.run，可据此探测是否已打补丁

    注意不能探测 run_until_complete：nest_asyncio 的 _patch_loop 对 loop 实例级
    打补丁，类方法 __module__ 保持 asyncio.base_events 不变。
    """
    return asyncio.run.__module__


def test_auth_handler_event_loop_poisoning_guard():
    """导入 auth_handler 后事件循环不被 nest_asyncio 污染（3.14 / 单测）/ 保持嵌套补丁（<3.14 非单测）"""
    import app.services.auth_handler  # noqa: F401

    if sys.version_info >= (3, 14):
        assert _loop_method_module() != "nest_asyncio", (
            "Python 3.14 下 nest_asyncio.apply() 必须被版本守卫跳过，"
            "否则 anyio 上下文检测失效（NoEventLoopError）"
        )
    elif "pytest" in sys.modules:
        # 单测进程守卫同款跳过：嵌套补丁在全量套件下挂死 deepeval 度量（见模块 docstring）
        assert _loop_method_module() != "nest_asyncio", (
            "pytest 进程下 nest_asyncio.apply() 必须被跳过，"
            "否则全量套件在 3.13 上会累积陈旧嵌套 loop 挂死"
        )
    else:
        assert _loop_method_module() == "nest_asyncio"


def test_rag_evaluator_uses_same_version_guard():
    """rag_evaluator 与 auth_handler 使用同一版本守卫模式（源码级检查）

    C2 后 `_ragas_evaluate` 只是入池外壳（with judge_pool_slot），nest_asyncio
    守卫随实现体一起搬到 `_ragas_evaluate_unpooled`，所以检查后者。
    """
    import app.services.rag_evaluator as m

    src = inspect.getsource(m.RagasEvaluator._ragas_evaluate_unpooled)
    assert "sys.version_info < (3, 14)" in src, "rag_evaluator 应带 nest_asyncio 版本守卫"


def _fake_dsn(monkeypatch):
    """W6-3 后 DATABASE_URL 默认空串（不再内置真实口令），直接调 create_db_engine
    的测试需注入假 DSN——create_engine 懒连接，不会真的连库；断言对象是
    PGGSSENCMODE 环境变量，与 DSN 内容无关。"""
    from app.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost:5432/ut")
    # get_settings 带 lru_cache，清掉进程内早先无 DATABASE_URL 时的缓存实例
    get_settings.cache_clear()


@pytest.mark.skipif(platform.system() != "Darwin", reason="仅 macOS 触发 GSS 默认禁用")
def test_darwin_defaults_gssencmode_disable(monkeypatch):
    """macOS 上 create_db_engine 应默认设置 PGGSSENCMODE=disable（不覆盖显式配置）"""
    import app.db.session as s

    _fake_dsn(monkeypatch)
    monkeypatch.delenv("PGGSSENCMODE", raising=False)
    engine = s.create_db_engine()
    try:
        assert os.environ.get("PGGSSENCMODE") == "disable"
    finally:
        engine.dispose()


@pytest.mark.skipif(platform.system() == "Darwin", reason="非 macOS 平台不注入默认值")
def test_non_darwin_keeps_gss_default(monkeypatch):
    """Linux/生产不注入 PGGSSENCMODE 默认值，行为与存量一致"""
    import app.db.session as s

    _fake_dsn(monkeypatch)
    monkeypatch.delenv("PGGSSENCMODE", raising=False)
    engine = s.create_db_engine()
    try:
        assert os.environ.get("PGGSSENCMODE") is None
    finally:
        engine.dispose()
