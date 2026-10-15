"""W11 hub 导入 job 加固——并发正确性集成测试（真实 PG 临时库 + 本地 Redis）。

覆盖整改计划 §3 W11 测试项：
1. 并发 acquire/release 计数正确性：多线程各持独立连接/事务，原子
   `UPDATE ... SET ref_count = ref_count ± 1` 在行锁串行化下无丢失更新
   （单语句事务在 READ COMMITTED 下天然无需应用层重试，重试路径由
   tests/unit/test_hub_import_job.py 的瞬时 DBAPI 错误用例覆盖）；
2. 同 job 重投（acks_late 孤儿消息）只处理一次：hubjob:{job_id} SETNX 互斥，
   并发重投仅一个 worker 执行，数据行不重复、ref_count 不双计；
3. 卡死 running job（updated_at 超 30 分钟未推进）可被 retry 接管自愈，
   残留互斥锁一并清除；新鲜 running 仍拒绝重试。

环境约定（tests/integration/conftest.py）：PG 用 W4 会话级临时库（runtime_db，
自建自清）；Redis 跟随 settings.REDIS_URL（local_redis，不可达明确 skip，不假绿）。
测试数据一律 w11 前缀、用例内唯一（uuid 后缀），只删自建数据。
"""
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration  # W8 分类门禁：补打标
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.dataset_models import EvaluationDataV2, EvaluationDataset
from app.models.hub_file_models import HubFileRecord, HubImportJob
from app.services.hub_file_service import acquire_ref, release_ref
from app.services.hub_import_job_service import retry_job

N_THREADS = 8
ACQUIRES_PER_THREAD = 15


@pytest.fixture(autouse=True)
def _fresh_redis_client():
    """防进程级测试污染：get_redis_client 为 lru_cache 单例，若同进程更早的
    单测在 patch redis.from_url 的窗口内触发过其首次构建，缓存会永久持有
    MagicMock 客户端，令互斥锁静默失效。每个用例前强制重建（真实 Redis）。"""
    from app.services.redis_stop_signal import get_redis_client

    get_redis_client.cache_clear()
    yield


@pytest.fixture()
def pg_engine(runtime_db):
    """临时库独立引擎（多线程各自建连接；与 db_session 共库）"""
    engine = create_engine(runtime_db, pool_pre_ping=True)
    yield engine
    engine.dispose()


def _make_dataset_with_job(db, n_files: int = 2, status: str = "pending"):
    """造数据集 + 导入 job（w11 前缀 + uuid 后缀，自建自清）"""
    suffix = uuid.uuid4().hex[:8]
    ds = EvaluationDataset(
        id=f"w11ds_{suffix}",
        name=f"W11并发集成测试数据集{suffix}",
        question_count=0,
        is_active=True,
        team_code="DEFAULT",
    )
    fids = [f"w11fid_{suffix}_{i}" for i in range(n_files)]
    job = HubImportJob(
        id=f"w11job_{suffix}",
        dataset_id=ds.id,
        file_ids=fids,
        status=status,
        total_count=n_files,
        success_count=0,
        fail_count=0,
        fail_detail={},
        succeeded_file_ids=[],
        created_by="w11-integration-tester",
    )
    db.add(ds)
    db.add(job)
    db.commit()
    return ds, job, fids


def _register_unpacked(db, fid: str, unpack_dir):
    """登记已解包执行包（unpack_dir 真实落盘 → ensure_unpacked 零下载命中）"""
    unpack_dir.mkdir(parents=True, exist_ok=True)
    (unpack_dir / "readme.txt").write_text("hello w11")
    rec = HubFileRecord(
        id=f"hub_{fid}", file_id=fid, unpack_status="unpacked",
        unpack_dir=str(unpack_dir), file_count=1,
        file_index=[{"path": "readme.txt", "type": "file", "size": 8}],
        ref_count=0,
    )
    db.add(rec)
    db.commit()
    return rec


# ---------------------------------------------------------------------------
# 1. 并发 acquire/release 计数正确性（多线程独立事务）
# ---------------------------------------------------------------------------

class TestConcurrentRefCount:
    def test_concurrent_acquires_then_releases_no_lost_update(self, pg_engine, db_session):
        """8 线程并发 acquire（8×15 次）→ 权威计数 120；再并发 release 8×14 次 → 8

        读改写实现下并发自增必然丢失更新（终值 < 120）；原子 UPDATE 由行锁
        串行化，终值精确。
        """
        fid = f"w11fid_conc_{uuid.uuid4().hex[:8]}"
        db_session.add(HubFileRecord(
            id=f"hub_{fid}", file_id=fid, unpack_status="unpacked", ref_count=0))
        db_session.commit()

        maker = sessionmaker(bind=pg_engine)
        errors: list = []

        def _run(op: str, times: int, barrier: threading.Barrier):
            session = maker()
            try:
                barrier.wait(timeout=30)
                for _ in range(times):
                    (acquire_ref if op == "acquire" else release_ref)(session, fid)
            except Exception as e:  # noqa: BLE001 - 线程内异常汇集到主线程断言
                errors.append(e)
            finally:
                session.close()

        barrier = threading.Barrier(N_THREADS)
        threads = [threading.Thread(target=_run, args=("acquire", ACQUIRES_PER_THREAD, barrier))
                   for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
        with maker() as s:
            assert s.query(HubFileRecord).filter(
                HubFileRecord.file_id == fid).one().ref_count == N_THREADS * ACQUIRES_PER_THREAD

        # 并发 release（每线程少放一次 → 终值 = 线程数，记录仍在）
        barrier = threading.Barrier(N_THREADS)
        threads = [threading.Thread(
            target=_run, args=("release", ACQUIRES_PER_THREAD - 1, barrier))
            for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
        with maker() as s:
            assert s.query(HubFileRecord).filter(
                HubFileRecord.file_id == fid).one().ref_count == N_THREADS

        # 收尾释放（w11 前缀自建自清：归零清理登记记录）
        with maker() as s:
            while release_ref(s, fid) > 0:
                pass
            assert s.query(HubFileRecord).filter(HubFileRecord.file_id == fid).first() is None

    def test_concurrent_release_to_zero_cleans_exactly_once(self, pg_engine, db_session, tmp_path):
        """ref_count=1 时两线程并发 release：都归零返回、无预期外异常、登记删除、目录被清

        败者可能因胜者已条件删除登记记录而在预检查收到「未登记」（HubFileError，
        与旧实现一致的幂等语义）；除此外不得有其它异常。
        """
        from app.services.hub_file_service import HubFileError

        fid = f"w11fid_zero_{uuid.uuid4().hex[:8]}"
        unpack_dir = tmp_path / fid
        unpack_dir.mkdir()
        (unpack_dir / "x.txt").write_text("1")
        db_session.add(HubFileRecord(
            id=f"hub_{fid}", file_id=fid, unpack_status="unpacked",
            unpack_dir=str(unpack_dir), ref_count=1))
        db_session.commit()

        maker = sessionmaker(bind=pg_engine)
        results: list = []
        barrier = threading.Barrier(2)

        def _release():
            session = maker()
            try:
                barrier.wait(timeout=30)
                results.append(release_ref(session, fid))
            except Exception as e:  # noqa: BLE001
                results.append(e)
            finally:
                session.close()

        threads = [threading.Thread(target=_release) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        unexpected = [r for r in results
                      if isinstance(r, Exception) and not isinstance(r, HubFileError)]
        assert unexpected == []
        zeros = [r for r in results if not isinstance(r, Exception)]
        assert len(zeros) >= 1 and all(v == 0 for v in zeros)
        assert not unpack_dir.exists(), "末引用归零应清理解包目录"
        with maker() as s:
            assert s.query(HubFileRecord).filter(HubFileRecord.file_id == fid).first() is None


# ---------------------------------------------------------------------------
# 2. 同 job 重投（acks_late 孤儿消息）只处理一次（hubjob 互斥锁）
# ---------------------------------------------------------------------------

class TestHubJobRedeliveryMutex:
    def test_duplicate_redelivery_processes_once(
        self, pg_engine, db_session, local_redis, monkeypatch, tmp_path,
    ):
        """两线程并发执行同一 job：一个 completed、一个 duplicate_rejected，
        数据行 2 条（不重复）、ref_count 各 1（不双计）、锁最终释放"""
        from app.tasks.celery_tasks import run_hub_import_job

        ds, job, fids = _make_dataset_with_job(db_session, n_files=2)
        for fid in fids:
            _register_unpacked(db_session, fid, tmp_path / fid)
        job_id = job.id
        lock_key = f"hubjob:{job_id}"

        # worker 会话注入临时库（任务内 get_db_session 每次新建会话）
        maker = sessionmaker(bind=pg_engine)
        monkeypatch.setattr(
            "app.tasks.evaluation_tasks_async.get_db_session", lambda: maker())

        # 首包写入放慢 1s，制造「T1 持锁处理中」窗口，T2 重投必撞锁
        from app.services.dataset_service import DatasetDataService

        orig_add = DatasetDataService.add_data_v2
        calls = {"n": 0}

        def slow_add(self, dataset_id, data):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(1.0)
            return orig_add(self, dataset_id, data)

        monkeypatch.setattr(DatasetDataService, "add_data_v2", slow_add)

        outcomes: dict = {}

        def _run(tag: str, delay: float):
            time.sleep(delay)
            try:
                outcomes[tag] = run_hub_import_job(job_id)
            except Exception as e:  # noqa: BLE001
                outcomes[tag] = e

        t1 = threading.Thread(target=_run, args=("first", 0.0))
        t2 = threading.Thread(target=_run, args=("second", 0.3))
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)

        statuses = sorted(
            o["status"] if isinstance(o, dict) else f"error:{o}" for o in outcomes.values()
        )
        assert statuses == ["completed", "duplicate_rejected"], f"outcomes={outcomes}"

        with maker() as s:
            rows = s.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == ds.id).all()
            assert len(rows) == 2, f"数据行不重复（实际 {len(rows)} 条）"
            hub_fields = sorted(r.data.get("hub_file_id") for r in rows)
            assert hub_fields == sorted(fids)
            done = s.query(HubImportJob).filter(HubImportJob.id == job_id).one()
            assert done.status == "completed"
            assert sorted(done.succeeded_file_ids or []) == sorted(fids)
            for fid in fids:
                rec = s.query(HubFileRecord).filter(HubFileRecord.file_id == fid).one()
                assert rec.ref_count == 1, f"{fid} 引用不双计"
        assert local_redis.get(lock_key) is None, "任务结束后互斥锁应释放"
        local_redis.delete(lock_key)


# ---------------------------------------------------------------------------
# 3. 卡死 running job 可被 retry 接管自愈
# ---------------------------------------------------------------------------

class TestStuckJobTakeover:
    def test_stuck_running_job_taken_over_and_completes(
        self, pg_engine, db_session, local_redis, monkeypatch, tmp_path,
    ):
        """updated_at 超 30 分钟的 running：retry 接管（清残留锁）→ 重跑至 completed"""
        from app.tasks.celery_tasks import run_hub_import_job

        ds, job, fids = _make_dataset_with_job(db_session, n_files=2, status="running")
        for fid in fids:
            _register_unpacked(db_session, fid, tmp_path / fid)
        job_id = job.id
        lock_key = f"hubjob:{job_id}"

        # 模拟 worker 死亡：updated_at 停在 32 分钟前（逐包 commit 本会持续推进）+
        # 残留互斥锁未释放
        stale_ts = datetime.now(timezone.utc) - timedelta(minutes=32)
        db_session.execute(
            text("UPDATE hub_import_jobs SET updated_at = :ts WHERE id = :id"),
            {"ts": stale_ts, "id": job_id},
        )
        db_session.commit()
        db_session.expire_all()
        local_redis.set(lock_key, "dead-worker:1")

        retried = retry_job(db_session, job_id)
        assert retried.status == "pending"
        assert local_redis.get(lock_key) is None, "接管应清除死亡 worker 的残留锁"

        monkeypatch.setattr(
            "app.tasks.evaluation_tasks_async.get_db_session",
            lambda: sessionmaker(bind=pg_engine)())
        res = run_hub_import_job(job_id)
        assert res["status"] == "completed"

        with sessionmaker(bind=pg_engine)() as s:
            assert s.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == ds.id).count() == 2
            done = s.query(HubImportJob).filter(HubImportJob.id == job_id).one()
            assert done.status == "completed"
            for fid in fids:
                assert s.query(HubFileRecord).filter(
                    HubFileRecord.file_id == fid).one().ref_count == 1
        local_redis.delete(lock_key)

    def test_live_running_job_retry_refused(self, db_session, local_redis):
        """新鲜 running（updated_at 30 分钟内）拒绝重试，且不清除他人锁"""
        _, job, _ = _make_dataset_with_job(db_session, n_files=1, status="running")
        job_id = job.id
        lock_key = f"hubjob:{job_id}"
        db_session.execute(
            text("UPDATE hub_import_jobs SET updated_at = now() WHERE id = :id"),
            {"id": job_id},
        )
        db_session.commit()
        db_session.expire_all()
        local_redis.set(lock_key, "worker-a:1")
        try:
            with pytest.raises(ValueError, match="进行中"):
                retry_job(db_session, job_id)
            assert local_redis.get(lock_key) == "worker-a:1"
        finally:
            local_redis.delete(lock_key)
