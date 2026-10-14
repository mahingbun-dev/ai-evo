"""W11 独立测试角色补充用例（单元层）——锁泛化语义、fail-open、Redis 故障容错

依据整改计划 §3 W11「测试：并发 acquire/release 计数正确性（多线程 + 事务重试）；
重投同 job 只处理一次；卡死 job 可重试」与开发交付摘要声明点，补充已交付测试
（tests/unit/test_hub_import_job.py、tests/integration/test_hub_import_concurrency.py）
未覆盖的分支：

1. EvalTaskLock key_prefix 泛化（redis_stop_signal.py）：
   - hubjob:{id} 键格式 / SETNX 互斥 / holder()；
   - 缺省前缀 eval_task_lock 不回归（W1 行为）；
   - 同 id 双前缀互相隔离（hub 导入锁不挡评测、评测锁不挡导入）；
   - TTL 透传（HUB_JOB_LOCK_TTL / 缺省 EVAL_TASK_LOCK_TTL）。
2. run_hub_import_job 互斥 fail-open：Redis acquire 异常放行执行、
   release 异常不外抛（TTL 兜底）。
3. 重投拒绝路径零副作用：不获取 DB 会话、不改 job 状态。
4. retry 卡死接管在 Redis 故障下仍能重置 pending（release_job_lock 容错）；
   新鲜 running 在 Redis 故障下仍保守拒绝。
5. release_ref 归零下限：ref_count=0 再释放不得出现 -1（CASE 下限；
   真实 SQL 行为由 tests/integration/test_w11_independent.py 复核）。

隔离纪律：本文件所有触锁用例均 monkeypatch get_redis_client；autouse fixture
在用例前后 cache_clear，防止 lru_cache 单例被替身污染泄漏到同进程后续测试
（与 tests/integration/test_hub_import_concurrency.py::_fresh_redis_client 同因）。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.models.hub_file_models import HubFileRecord, HubImportJob
from app.services.redis_stop_signal import (
    EVAL_TASK_LOCK_PREFIX,
    EVAL_TASK_LOCK_TTL,
    HUB_JOB_LOCK_PREFIX,
    HUB_JOB_LOCK_TTL,
    EvalTaskLock,
    get_redis_client,
)
from app.services.hub_import_job_service import (
    HUB_JOB_STUCK_AFTER_SECONDS,
    retry_job,
)


# ---------------------------------------------------------------------------
# 隔离与替身
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_lock_singleton():
    """用例前后强制重建 redis 客户端缓存：本文件用例全程 monkeypatch
    get_redis_client，cache_clear 双保险防替身/MagicMock 泄漏进 lru_cache"""
    get_redis_client.cache_clear()
    yield
    get_redis_client.cache_clear()


class RecordingRedis:
    """记录 set 调用参数的最小 Redis 替身（SET NX EX / GET / DELETE）"""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.set_calls = []

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append({"key": key, "value": value, "nx": nx, "ex": ex})
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        n = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                n += 1
        return n


class BrokenRedis:
    """模拟 Redis 不可用：任何操作抛异常"""

    def __getattr__(self, name):
        def _raise(*a, **kw):
            raise ConnectionError("redis 不可用（模拟）")
        return _raise


class RefZeroDb:
    """release_ref 归零下限用例的最小 db 替身：

    execute 应答 W11 原子语句（Update ±1 带 CASE 下限 / 条件 Delete / Select 回读），
    语义按真实 SQL 重新实现（与 test_hub_import_job.FakeJobDb 独立同构，不跨文件复用）
    """

    def __init__(self, rec: HubFileRecord):
        self.rec = rec
        self.commits = 0
        self.rolls = 0

    @staticmethod
    def _file_id(stmt):
        for key, value in stmt.compile().params.items():
            if "file_id" in key:
                return value
        raise AssertionError(f"语句缺少 file_id 绑定参数: {stmt}")

    def execute(self, stmt, *a, **kw):
        from sqlalchemy import Delete, Select, Update

        fid = self._file_id(stmt)
        assert fid == self.rec.file_id, f"语句作用于 {fid}，替身只登记 {self.rec.file_id}"
        if isinstance(stmt, Update):
            count = int(self.rec.ref_count or 0)
            sql = str(stmt.compile())
            if "CASE" in sql.upper():
                self.rec.ref_count = max(0, count - 1)
            else:
                self.rec.ref_count = count + 1
            return SimpleNamespace(rowcount=1)
        if isinstance(stmt, Delete):
            if int(self.rec.ref_count or 0) == 0:
                self.rec = None
                return SimpleNamespace(rowcount=1)
            return SimpleNamespace(rowcount=0)
        if isinstance(stmt, Select):
            value = (self.rec.ref_count if self.rec is not None else 0) or 0
            return SimpleNamespace(scalar=lambda: value)
        raise AssertionError(f"未预期的语句类型: {stmt}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rolls += 1

    def close(self):
        pass

    def query(self, model, *criteria):
        """_query_record 预检查应答：HubFileRecord 按 self.rec 命中"""
        q = MagicMock()
        assert model is HubFileRecord
        q.filter.return_value.first.return_value = self.rec
        return q


def make_job(job_id="hubjob_w11ind01", status="running", **kw):
    return HubImportJob(
        id=job_id,
        dataset_id=kw.pop("dataset_id", "ds-w11ind"),
        file_ids=kw.pop("file_ids", ["fid-1"]),
        status=status,
        total_count=kw.pop("total_count", 1),
        success_count=kw.pop("success_count", 0),
        fail_count=kw.pop("fail_count", 0),
        fail_detail=kw.pop("fail_detail", {}),
        succeeded_file_ids=kw.pop("succeeded_file_ids", []),
        created_by="w11-independent-tester",
    )


class _JobDb:
    """retry_job 用的最小 db 替身（query(HubImportJob) 应答 + commit 计数）"""

    def __init__(self, job):
        self.job = job
        self.commits = 0

    def query(self, model, *criteria):
        q = MagicMock()
        assert model is HubImportJob
        q.filter.return_value.first.return_value = self.job
        return q

    def commit(self):
        self.commits += 1

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 1. EvalTaskLock key_prefix 泛化
# ---------------------------------------------------------------------------

class TestEvalTaskLockKeyPrefix:
    def test_hub_prefix_key_format_setnx_and_holder(self, monkeypatch):
        """hubjob:{id} 键格式：SETNX 首抢成功、二抢失败、holder 可查"""
        fake = RecordingRedis()
        monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client", lambda: fake)

        lock = EvalTaskLock("hubjob_w11k01", ttl=HUB_JOB_LOCK_TTL, key_prefix=HUB_JOB_LOCK_PREFIX)
        assert lock.acquire() is True
        assert "hubjob:hubjob_w11k01" in fake.store, f"实际键: {list(fake.store)}"
        value = fake.store["hubjob:hubjob_w11k01"]
        assert isinstance(value, str) and ":" in value, f"锁值应为持锁者标识 hostname:pid，实际 {value!r}"
        assert lock.acquire() is False, "同键二次 SETNX 应失败"
        assert lock.holder() == value

    def test_default_prefix_non_regression(self, monkeypatch):
        """缺省构造仍写 eval_task_lock:{id}（W1 评测互斥行为不回归），且不产生 hubjob 键"""
        fake = RecordingRedis()
        monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client", lambda: fake)

        lock = EvalTaskLock("task-w11")
        assert lock._key == f"{EVAL_TASK_LOCK_PREFIX}:task-w11"
        assert lock.acquire() is True
        assert f"{EVAL_TASK_LOCK_PREFIX}:task-w11" in fake.store
        assert "hubjob:task-w11" not in fake.store
        assert lock.acquire() is False

    def test_prefix_isolation_same_task_id(self, monkeypatch):
        """同一 id 双前缀互相隔离：评测锁被持不影响同 id 导入锁获取，反之亦然"""
        fake = RecordingRedis()
        monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client", lambda: fake)

        eval_lock = EvalTaskLock("same-id", key_prefix=EVAL_TASK_LOCK_PREFIX)
        hub_lock = EvalTaskLock("same-id", key_prefix=HUB_JOB_LOCK_PREFIX)
        assert eval_lock.acquire() is True
        assert hub_lock.acquire() is True, "评测锁被持不得拒绝同 id 的导入锁"
        assert eval_lock.release() is None
        assert fake.store.get(f"{HUB_JOB_LOCK_PREFIX}:same-id") is not None, \
            "释放评测锁不得误删导入锁"

    def test_ttl_forwarded_hub_and_default(self, monkeypatch):
        """TTL 透传：hub 锁用 HUB_JOB_LOCK_TTL，缺省用 EVAL_TASK_LOCK_TTL"""
        fake = RecordingRedis()
        monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client", lambda: fake)

        EvalTaskLock("j", ttl=HUB_JOB_LOCK_TTL, key_prefix=HUB_JOB_LOCK_PREFIX).acquire()
        EvalTaskLock("t").acquire()
        ex_by_key = {c["key"]: c["ex"] for c in fake.set_calls}
        assert ex_by_key["hubjob:j"] == HUB_JOB_LOCK_TTL == 3 * 3600
        assert ex_by_key[f"{EVAL_TASK_LOCK_PREFIX}:t"] == EVAL_TASK_LOCK_TTL
        assert all(c["nx"] for c in fake.set_calls), "acquire 必须是 SET NX"


# ---------------------------------------------------------------------------
# 2. run_hub_import_job 互斥 fail-open / 拒绝路径零副作用
# ---------------------------------------------------------------------------

class TestHubJobMutexFailOpen:
    def test_redis_acquire_exception_fails_open(self, monkeypatch):
        """Redis acquire 抛异常 → 放行执行（防御性互斥不阻断业务），release 异常不外抛"""
        from app.tasks.celery_tasks import run_hub_import_job

        monkeypatch.setattr(
            "app.services.redis_stop_signal.get_redis_client", lambda: BrokenRedis())
        job = make_job("hubjob_failopen1", status="pending")
        db = _JobDb(job)
        processed = []
        with patch("app.services.hub_import_job_service.process_hub_import_job",
                   side_effect=lambda *a, **k: processed.append(a)
                   or {"status": "completed", "job_id": job.id}), \
                patch("app.tasks.evaluation_tasks_async.get_db_session", return_value=db):
            res = run_hub_import_job("hubjob_failopen1")
        assert res["status"] == "completed", "Redis 不可用应 fail-open 放行而非拒绝"
        assert len(processed) == 1, "放行后业务处理必须执行"

    def test_rejected_redelivery_zero_side_effects(self, monkeypatch):
        """重投拒绝路径零副作用：不获取 DB 会话、不触达处理逻辑、job 状态不变"""
        from app.tasks.celery_tasks import run_hub_import_job

        fake = RecordingRedis({"hubjob:hubjob_sidefx1": "other-worker:9"})
        monkeypatch.setattr("app.services.redis_stop_signal.get_redis_client", lambda: fake)
        job = make_job("hubjob_sidefx1", status="running")
        processed = []
        db_factory = MagicMock(side_effect=AssertionError("拒绝路径不得获取 DB 会话"))
        with patch("app.services.hub_import_job_service.process_hub_import_job",
                   side_effect=lambda *a, **k: processed.append(a)), \
                patch("app.tasks.evaluation_tasks_async.get_db_session", db_factory):
            res = run_hub_import_job("hubjob_sidefx1")
        assert res["status"] == "duplicate_rejected"
        assert res["lock_holder"] == "other-worker:9"
        assert processed == []
        assert job.status == "running", "拒绝路径不得改 job 状态"
        assert fake.store["hubjob:hubjob_sidefx1"] == "other-worker:9", "不得误删他人锁"


# ---------------------------------------------------------------------------
# 3. retry 卡死接管的 Redis 故障容错
# ---------------------------------------------------------------------------

class TestRetryTakeoverRedisFaultTolerance:
    def test_takeover_survives_redis_outage(self, monkeypatch):
        """Redis 不可用时接管卡死 job：release_job_lock 失败仅记日志，重置 pending 不受阻断"""
        monkeypatch.setattr(
            "app.services.redis_stop_signal.get_redis_client", lambda: BrokenRedis())
        job = make_job("hubjob_outage1", status="running")
        job.updated_at = datetime.now(timezone.utc) - timedelta(
            seconds=HUB_JOB_STUCK_AFTER_SECONDS + 120)
        assert retry_job(_JobDb(job), job.id).status == "pending"

    def test_fresh_running_still_refused_when_redis_down(self, monkeypatch):
        """Redis 不可用时新鲜 running 仍保守拒绝（不因故障放宽卡死判定）"""
        monkeypatch.setattr(
            "app.services.redis_stop_signal.get_redis_client", lambda: BrokenRedis())
        job = make_job("hubjob_outage2", status="running")
        job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        with pytest.raises(ValueError, match="进行中"):
            retry_job(_JobDb(job), job.id)
        assert job.status == "running"


# ---------------------------------------------------------------------------
# 4. release_ref 归零下限（替身层；真实 SQL 由集成测试复核）
# ---------------------------------------------------------------------------

class TestReleaseRefFloorZeroUnit:
    def test_release_at_zero_returns_zero_never_negative(self):
        """ref_count=0 再释放：返回 0、CASE 下限保证不出现 -1、走归零条件删除"""
        from app.services.hub_file_service import release_ref

        rec = HubFileRecord(id="hub_w11ind", file_id="fid-w11ind-zero", ref_count=0)
        db = RefZeroDb(rec)
        assert release_ref(db, "fid-w11ind-zero") == 0
        assert rec.ref_count == 0, "CASE 下限语义：ref_count 不得减成 -1"
