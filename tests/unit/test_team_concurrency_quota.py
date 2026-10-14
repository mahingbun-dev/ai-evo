"""ARCH-2-2 team 并发配额 + eval 队列优先级：单元测试（L1，mock Redis）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §2.3 决策 D4 /
§3 Phase 2 表 2-2 行（team 并发配额 + 优先级路由；验收口径：单团队 100
并发任务不打满全局）。

覆盖：
  1. 配额计数器（app/services/quota.py，FakeRedis 假实现命令面）：
     - 占用：INCR 计数 + TTL 兜底续期；
     - 超限：拒绝且撤销本次占位（计数器回落，不泄漏额度）；
     - 释放：DECR 递减、减到 0 删 key、下限保护不出负数；
     - 超时回收：TTL 到期（整本计数器过期）后额度自动回满；
     - 泄漏对账：计数器高于 DB 在途真值的部分被裁回（只裁不抬）；
     - 团队隔离 / NULL team_code 归一 DEFAULT / limit 缺省自配置。
  2. 调度器接线（app/services/task_scheduler.py）：
     - 提交前校验：超限拒绝 → 不入队不写 celery_task_id；
     - 优先级：high_priority 缺省 → PRIORITY_HIGH，显式 False → NORMAL；
     - apply_async 失败时撤销已占额度（占用/释放成对）；
     - stop_task / cleanup_task 释放额度；Redis 异常走既有失败语义。
  3. 配置面（TEAM_MAX_CONCURRENT 默认 8、gt=0）与 celery 优先级档位声明。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit  # W8 分类门禁：mock Redis/DB，无网络无真 DB


# ======================================================================
# 测试替身
# ======================================================================

class FakeRedis:
    """仅实现 quota 用到的命令面（incr/decr/get/set/expire/delete）。

    语义对齐 redis-py(decode_responses=True)：值一律存 str；EXPIRE 记录
    TTL 供断言（到期需测试显式模拟——real Redis 的自动过期不在 L1 层验证）。
    """

    def __init__(self):
        self.data = {}
        self.expires = {}  # key -> ttl（最近一次 expire 设置值）

    def incr(self, key):
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    def decr(self, key):
        value = int(self.data.get(key, "0")) - 1
        self.data[key] = str(value)
        return value

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = str(value)

    def expire(self, key, ttl):
        # real Redis：key 不存在时 EXPIRE 无效（返回 0）
        if key in self.data:
            self.expires[key] = ttl
            return 1
        return 0

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.data:
                del self.data[key]
                self.expires.pop(key, None)
                removed += 1
        return removed

    # 测试辅助：模拟 key 因 TTL 到期被 Redis 回收
    def simulate_ttl_expiry(self, key):
        self.data.pop(key, None)
        self.expires.pop(key, None)


class _FakeQuery:
    """submit_task 用到的两种查询形态：任务行查询（first）与计数查询（scalar）"""

    def __init__(self, session, kind):
        self._session = session
        self._kind = kind
        self.filter_args = ()

    def filter(self, *args, **kwargs):
        self.filter_args = args
        if self._kind == "count":
            self._session.count_filter_args = args
        return self

    def first(self):
        return self._session.task_row

    def scalar(self):
        return self._session.running_count


class _FakeSession:
    """TaskScheduler 依赖的最小 DB 会话面（query/commit/rollback/close）"""

    def __init__(self, task_row=None, running_count=0):
        self.task_row = task_row
        self.running_count = running_count
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.count_filter_args = ()

    def query(self, *entities):
        from app.models.task_models import EvaluationTask
        kind = "task" if entities and entities[0] is EvaluationTask else "count"
        return _FakeQuery(self, kind)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class QuotaStub:
    """TeamConcurrencyQuota 替身：行为经类属性预置（fixture 每用例重置），
    记录 acquire/release 调用供断言。"""

    instances = []
    next_acquire_result = True
    next_acquire_error = None

    def __init__(self, redis_client=None, limit=None, slot_ttl=None):
        self.acquire_result = QuotaStub.next_acquire_result
        self.acquire_error = QuotaStub.next_acquire_error
        self.acquire_calls = []
        self.release_calls = []
        self.limit = limit if limit is not None else 8
        QuotaStub.instances.append(self)

    def acquire(self, team_code, db_in_flight):
        self.acquire_calls.append((team_code, db_in_flight))
        if self.acquire_error:
            raise self.acquire_error
        return self.acquire_result

    def release(self, team_code):
        self.release_calls.append(team_code)
        return 0


class StopCheckerStub:
    set_calls = []
    cleanup_calls = []

    def __init__(self, task_id):
        self.task_id = task_id

    def set(self):
        StopCheckerStub.set_calls.append(self.task_id)

    def cleanup(self):
        StopCheckerStub.cleanup_calls.append(self.task_id)


@pytest.fixture
def quota_stub(monkeypatch):
    """替换 task_scheduler 命名空间里的 TeamConcurrencyQuota（含实例台账与
    行为预置的重置）"""
    QuotaStub.instances = []
    QuotaStub.next_acquire_result = True
    QuotaStub.next_acquire_error = None
    monkeypatch.setattr("app.services.task_scheduler.TeamConcurrencyQuota", QuotaStub)
    return QuotaStub


@pytest.fixture
def stop_checker_stub(monkeypatch):
    """替换 RedisStopSignalChecker（stop/cleanup 会真实触 Redis，L1 层隔离）"""
    StopCheckerStub.set_calls = []
    StopCheckerStub.cleanup_calls = []
    monkeypatch.setattr(
        "app.services.task_scheduler.RedisStopSignalChecker", StopCheckerStub
    )
    return StopCheckerStub


def _make_task(task_id="t-1", team_code="TEAM-A", status="running",
               celery_task_id=None):
    return SimpleNamespace(
        id=task_id, team_code=team_code, status=status,
        celery_task_id=celery_task_id,
    )


def _celery_task_mock(task_id="celery-1"):
    mock = MagicMock()
    mock.apply_async.return_value = SimpleNamespace(id=task_id)
    return mock


# ======================================================================
# 1. 配额计数器（app/services/quota.py）
# ======================================================================

class TestQuotaCounter:
    def test_acquire_increments_and_sets_ttl(self):
        """占用：INCR 计数成功，且每次占用刷新整本计数器 TTL（兜底防泄漏）"""
        from app.services.quota import TEAM_QUOTA_TTL, TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=3)

        assert quota.acquire("TEAM-A", db_in_flight=1) is True
        assert quota.current("TEAM-A") == 1
        assert fake.expires["team_quota:TEAM-A"] == TEAM_QUOTA_TTL

    def test_acquire_rejects_over_limit_and_counter_falls_back(self):
        """超限：第 limit+1 次占用被拒，且撤销本次占位（计数器回落到上限，
        不因「试占被拒」泄漏额度）"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=2)

        assert quota.acquire("TEAM-A", 1) is True   # 计数 1
        assert quota.acquire("TEAM-A", 2) is True   # 计数 2（满）
        assert quota.acquire("TEAM-A", 3) is False  # 超限拒绝
        assert quota.current("TEAM-A") == 2         # 拒绝方占位已撤销

    def test_release_decrements_and_deletes_key_at_zero(self):
        """释放：DECR 递减；减到 0 删 key（Redis 不残留空账本）"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=4)
        quota.acquire("TEAM-A", 1)
        quota.acquire("TEAM-A", 2)

        assert quota.release("TEAM-A") == 1
        assert quota.release("TEAM-A") == 0
        assert "team_quota:TEAM-A" not in fake.data

    def test_release_floor_never_negative(self):
        """下限保护：无占用时释放不产生负计数（过量释放清零账本，不反向
        放行超额）"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=4)
        quota.acquire("TEAM-A", 1)
        quota.release("TEAM-A")
        quota.release("TEAM-A")  # 已是 0 再释放

        assert quota.current("TEAM-A") == 0
        assert "team_quota:TEAM-A" not in fake.data

    def test_ttl_expiry_recovers_quota(self):
        """超时回收：整本计数器 TTL 到期后额度自动回满（worker 崩溃等
        无释放调用场景的兜底路径）"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=1)
        assert quota.acquire("TEAM-A", 1) is True
        assert quota.acquire("TEAM-A", 2) is False  # 满了

        fake.simulate_ttl_expiry("team_quota:TEAM-A")  # 模拟 TTL 到期回收
        assert quota.current("TEAM-A") == 0
        assert quota.acquire("TEAM-A", 1) is True      # 额度已回满

    def test_leak_reconcile_trims_counter_to_db_truth(self):
        """泄漏对账：计数器虚高（丢失 release）时，占用时裁回 DB 在途真值
        ——自然完成任务无任务体释放钩子，此路径是常规回收（TTL 仅兜底）"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=8)
        fake.set("team_quota:TEAM-A", "10")  # 模拟泄漏：已完成未释放

        # DB 在途 4（3 running + 本次待提交），计数 11 → 裁回 4，放行
        assert quota.acquire("TEAM-A", 4) is True
        assert quota.current("TEAM-A") == 4

    def test_reconcile_never_inflates_counter(self):
        """对账只裁不抬：计数低于真值（功能上线前已在途的存量任务）时
        不抬高计数器（±1 量级误差是公平性护栏可接受面，见 quota.py 说明）"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=8)

        assert quota.acquire("TEAM-A", 6) is True  # 计数 INCR=1，不抬到 6
        assert quota.current("TEAM-A") == 1

    def test_teams_are_isolated(self):
        """团队隔离：A 团队配额占满不影响 B 团队（各自独立计数器）——
        「单团队 100 并发不打满全局」验收口径的调度侧实现"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=1)
        assert quota.acquire("TEAM-A", 1) is True
        assert quota.acquire("TEAM-A", 2) is False

        assert quota.acquire("TEAM-B", 1) is True
        assert quota.current("TEAM-B") == 1

    def test_null_team_code_normalized_to_default_ledger(self):
        """NULL team_code 归一：落 DEFAULT 账本（与列 server_default 同语义），
        current(None) 与 current('DEFAULT') 同账"""
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake, limit=8)
        assert quota.acquire(None, 1) is True

        assert "team_quota:DEFAULT" in fake.data
        assert quota.current(None) == 1
        assert quota.current("DEFAULT") == 1

    def test_limit_defaults_to_settings(self):
        """limit 缺省自配置（TEAM_MAX_CONCURRENT）；显式注入优先"""
        from app.core.config import Settings
        from app.services.quota import TeamConcurrencyQuota

        fake = FakeRedis()
        quota = TeamConcurrencyQuota(redis_client=fake)
        assert quota.limit == Settings().TEAM_MAX_CONCURRENT

        assert TeamConcurrencyQuota(redis_client=fake, limit=3).limit == 3


# ======================================================================
# 2. 调度器接线（app/services/task_scheduler.py）
# ======================================================================

class TestSchedulerQuotaWiring:
    def _patch_submit_deps(self, monkeypatch, session, task_mock):
        monkeypatch.setattr(
            "app.services.task_scheduler._get_db_session", lambda: session
        )
        monkeypatch.setattr("app.tasks.celery_tasks.run_evaluation_task", task_mock)

    def test_submit_admits_within_quota_with_high_priority(self, quota_stub, monkeypatch):
        """放行路径：占用成功 → apply_async（args+priority）→ 写回
        celery_task_id → True；在途口径 = 本团队 status=running 计数（含本任务）"""
        from app.celery_app import PRIORITY_HIGH
        from app.services.task_scheduler import TaskScheduler

        task_row = _make_task()
        session = _FakeSession(task_row=task_row, running_count=5)
        run_task_mock = _celery_task_mock("celery-1")
        self._patch_submit_deps(monkeypatch, session, run_task_mock)

        assert TaskScheduler().submit_task("t-1") is True

        instance = quota_stub.instances[-1]
        assert instance.acquire_calls == [("TEAM-A", 5)]  # 含本任务（已 running）
        run_task_mock.apply_async.assert_called_once_with(
            args=["t-1"], priority=PRIORITY_HIGH
        )
        assert task_row.celery_task_id == "celery-1"
        assert session.committed and session.closed
        # 在途口径门禁：计数查询过滤条件 = 本团队 + status running
        # （SQLAlchemy 2.0 的 str(expr) 渲染绑定参数，须 literal_binds 编译）
        compiled = [
            str(c.compile(compile_kwargs={"literal_binds": True}))
            for c in session.count_filter_args
        ]
        assert any("team_code" in text and "TEAM-A" in text for text in compiled)
        assert any("status" in text and "running" in text for text in compiled)

    def test_submit_normal_priority_for_batch(self, quota_stub, monkeypatch):
        """批量档：显式 high_priority=False → PRIORITY_NORMAL——批量入口
        （app/api/batch_operations_api.py）的一行接线随 app/api 归属批次落地"""
        from app.celery_app import PRIORITY_NORMAL
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(), running_count=1)
        run_task_mock = _celery_task_mock("celery-2")
        self._patch_submit_deps(monkeypatch, session, run_task_mock)

        assert TaskScheduler().submit_task("t-1", high_priority=False) is True
        run_task_mock.apply_async.assert_called_once_with(
            args=["t-1"], priority=PRIORITY_NORMAL
        )

    def test_submit_rejected_when_quota_full(self, quota_stub, monkeypatch):
        """超限拒绝：返回 False，不入队（apply_async 不调用）、不写
        celery_task_id——调用方既有失败路径负责置 failed"""
        from app.services.task_scheduler import TaskScheduler

        task_row = _make_task()
        session = _FakeSession(task_row=task_row, running_count=9)
        run_task_mock = _celery_task_mock()
        self._patch_submit_deps(monkeypatch, session, run_task_mock)
        quota_stub.next_acquire_result = False  # 后续构造的实例一律拒绝

        assert TaskScheduler().submit_task("t-1") is False
        run_task_mock.apply_async.assert_not_called()
        assert task_row.celery_task_id is None

    def test_submit_counts_self_when_not_running(self, quota_stub, monkeypatch):
        """防御：未经 start_task 的非标准调用（status != running）时对账
        值 +1，保证本次占位不被对账误裁"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(status="pending"), running_count=2)
        run_task_mock = _celery_task_mock("celery-3")
        self._patch_submit_deps(monkeypatch, session, run_task_mock)

        assert TaskScheduler().submit_task("t-1") is True
        assert quota_stub.instances[-1].acquire_calls == [("TEAM-A", 3)]

    def test_submit_failure_after_acquire_releases_slot(self, quota_stub, monkeypatch):
        """占用/释放成对：apply_async 失败（broker 不可达）时撤销已占额度，
        返回 False 走既有失败语义（rollback + 不写 celery_task_id）"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(), running_count=1)
        run_task_mock = MagicMock()
        run_task_mock.apply_async.side_effect = ConnectionError("broker down")
        self._patch_submit_deps(monkeypatch, session, run_task_mock)

        assert TaskScheduler().submit_task("t-1") is False
        assert quota_stub.instances[-1].release_calls == ["TEAM-A"]
        assert session.rolled_back and session.closed

    def test_submit_quota_redis_error_fails_submission(self, quota_stub, monkeypatch):
        """Redis 不可达：配额校验异常走既有提交失败语义（False）——Redis
        故障时 broker 同样不可用，静默放行无意义"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(), running_count=1)
        run_task_mock = _celery_task_mock()
        self._patch_submit_deps(monkeypatch, session, run_task_mock)
        quota_stub.next_acquire_error = ConnectionError("redis down")

        assert TaskScheduler().submit_task("t-1") is False
        run_task_mock.apply_async.assert_not_called()

    def test_submit_missing_task_skips_quota(self, quota_stub, monkeypatch):
        """任务不存在：不触碰配额（从未占用）"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=None)
        run_task_mock = _celery_task_mock()
        self._patch_submit_deps(monkeypatch, session, run_task_mock)

        assert TaskScheduler().submit_task("missing") is False
        assert quota_stub.instances == []


class TestSchedulerReleaseWiring:
    def _patch(self, monkeypatch, session, revoke_mock=None):
        monkeypatch.setattr(
            "app.services.task_scheduler._get_db_session", lambda: session
        )
        if revoke_mock is not None:
            from app.celery_app import celery_app
            monkeypatch.setattr(celery_app.control, "revoke", revoke_mock)

    def test_stop_task_releases_quota(self, quota_stub, stop_checker_stub, monkeypatch):
        """停止释放：stop_task 撤销 celery 任务后释放 team 额度（ARCH-2-2
        释放路径 1）"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(celery_task_id="celery-xyz"))
        revoke_mock = MagicMock()
        self._patch(monkeypatch, session, revoke_mock)

        assert TaskScheduler().stop_task("t-1") is True
        revoke_mock.assert_called_once()
        assert quota_stub.instances[-1].release_calls == ["TEAM-A"]

    def test_stop_task_quota_release_error_does_not_break_stop(
        self, quota_stub, stop_checker_stub, monkeypatch
    ):
        """释放失败仅告警：stop 语义不被配额故障破坏（仍 True；泄漏由
        对账/TTL 兜底）"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(celery_task_id="celery-xyz"))
        self._patch(monkeypatch, session, MagicMock())

        def _raising_release(team_code):
            raise ConnectionError("redis down")

        # 预置：stop_task 内构造的实例 release 即抛错
        original_release = QuotaStub.release
        QuotaStub.release = _raising_release
        try:
            assert TaskScheduler().stop_task("t-1") is True
        finally:
            QuotaStub.release = original_release

    def test_cleanup_task_releases_quota(self, quota_stub, stop_checker_stub, monkeypatch):
        """清理释放：cleanup_task（调度器清理钩子，ARCH-2-2 释放路径 2）；
        任务不存在时不释放（从未占用）"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task())
        self._patch(monkeypatch, session)

        TaskScheduler().cleanup_task("t-1")
        assert quota_stub.instances[-1].release_calls == ["TEAM-A"]

        # 任务不存在：不释放
        empty_session = _FakeSession(task_row=None)
        monkeypatch.setattr(
            "app.services.task_scheduler._get_db_session", lambda: empty_session
        )
        TaskScheduler().cleanup_task("missing")
        assert quota_stub.instances[-1].release_calls == []

    def test_cleanup_task_releases_null_team_on_default_ledger(
        self, quota_stub, stop_checker_stub, monkeypatch
    ):
        """NULL team_code 的任务同样释放（acquire 侧归一到 DEFAULT 账本，
        释放必须同账本，否则该账本只进不出）"""
        from app.services.task_scheduler import TaskScheduler

        session = _FakeSession(task_row=_make_task(team_code=None))
        self._patch(monkeypatch, session)

        TaskScheduler().cleanup_task("t-1")
        assert quota_stub.instances[-1].release_calls == [None]


# ======================================================================
# 3. 配置面与 celery 优先级档位
# ======================================================================

class TestConfigAndCeleryPlumbing:
    def test_team_max_concurrent_default_and_override(self):
        """TEAM_MAX_CONCURRENT 默认 8（设计 §2.3 D4 / 任务说明缺省），
        env/构造参数可覆盖"""
        from app.core.config import Settings

        assert Settings().TEAM_MAX_CONCURRENT == 8
        assert Settings(TEAM_MAX_CONCURRENT=16).TEAM_MAX_CONCURRENT == 16

    @pytest.mark.parametrize("bad", [0, -1])
    def test_team_max_concurrent_must_be_positive(self, bad):
        """gt=0 门禁：0/-1 直接 ValidationError（0 无「禁用」歧义语义，
        彻底放开请配大值——fail-fast 在配置解析层，不带病进运行时）"""
        from pydantic import ValidationError

        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(TEAM_MAX_CONCURRENT=bad)

    def test_priority_constants_and_transport_steps(self):
        """优先级常量取值钉住（⚠ 按本栈实测行为反转的映射，见 celery_app
        ARCH-2-2 注释块）：kombu 5.6.2 Redis transport 的 BRPOP keys 按
        priority_steps 升序下发、Redis 多 key 左先服务 → 数值小的先消费，
        故人工调试 HIGH=0（先消费）、批量 NORMAL=9。两值均须落在
        priority_steps 档位上；visibility_timeout（W10-1）不被 ARCH-2-2 改动。
        升级 kombu 若修正消费顺序，本断言与常量须同步翻转。"""
        from app.celery_app import PRIORITY_HIGH, PRIORITY_NORMAL, celery_app

        assert (PRIORITY_HIGH, PRIORITY_NORMAL) == (0, 9)
        steps = celery_app.conf.broker_transport_options["priority_steps"]
        assert PRIORITY_HIGH in steps and PRIORITY_NORMAL in steps
        assert celery_app.conf.broker_transport_options["visibility_timeout"] == 7800
