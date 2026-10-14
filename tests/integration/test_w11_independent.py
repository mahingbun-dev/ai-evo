"""W11 独立测试角色补充用例（集成层，真实 PG 临时库 + 本地 Redis）。

依据整改计划 §3 W11「测试：并发 acquire/release 计数正确性（多线程 + 事务重试）；
重投同 job 只处理一次；卡死 job 可重试」，在已交付的
tests/integration/test_hub_import_concurrency.py 之外补充三类更强压力/边界：

1. 混合并发 acquire/release（读改写实现下混合丢更新同样丢计数）：
   4 线程 ×10 acquire 与 4 线程 ×5 release 同时开跑 → 权威计数精确 = 10+40-20=30；
2. release 归零下限：ref_count=0 再释放不出现 -1（真实 PG 执行 CASE 下限），
   归零条件删除/目录清理语义正常；
3. release vs 并发 acquire 竞态不变量（多轮采样）：记录存在 ⇔ 解包目录存在，
   acquire 方要么拿到 ≥1 的权威计数、要么收到「未登记」——绝不出现
   「acquire 成功但记录/目录已被清理」的悬空引用；
4. 互斥锁压力版：4 worker 同 job 重投仅 1 个执行（3 个 duplicate_rejected），
   数据行不重复、ref_count 不双计、锁最终释放。

环境约定（tests/integration/conftest.py）：PG 用 W4 会话级临时库（runtime_db，
自建自清）；Redis 用本地 6379（local_redis，不可达明确 skip，不假绿）。
测试数据一律 w11i 前缀、用例内唯一（uuid 后缀）。
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
from app.services.hub_file_service import HubFileError, acquire_ref, release_ref
from app.services.hub_import_job_service import retry_job

N = 8  # 混合并发线程总数（4 acquire + 4 release）


@pytest.fixture(autouse=True)
def _fresh_redis_client():
    """防进程级测试污染：get_redis_client 为 lru_cache 单例，若更早的单测在
    patch redis.from_url / 替身窗口内触发过首次构建，缓存会永久持有替身客户端。
    每个用例前强制重建（真实 Redis）。"""
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
    suffix = uuid.uuid4().hex[:8]
    ds = EvaluationDataset(
        id=f"w11ids_{suffix}",
        name=f"W11独立集成测试数据集{suffix}",
        question_count=0,
        is_active=True,
        team_code="DEFAULT",
    )
    fids = [f"w11ifid_{suffix}_{i}" for i in range(n_files)]
    job = HubImportJob(
        id=f"w11ijob_{suffix}",
        dataset_id=ds.id,
        file_ids=fids,
        status=status,
        total_count=n_files,
        success_count=0,
        fail_count=0,
        fail_detail={},
        succeeded_file_ids=[],
        created_by="w11-independent-tester",
    )
    db.add(ds)
    db.add(job)
    db.commit()
    return ds, job, fids


def _register_unpacked(db, fid: str, unpack_dir):
    """登记已解包执行包（unpack_dir 真实落盘 → ensure_unpacked 零下载命中）"""
    unpack_dir.mkdir(parents=True, exist_ok=True)
    (unpack_dir / "readme.txt").write_text("hello w11i")
    rec = HubFileRecord(
        id=f"hub_{fid}", file_id=fid, unpack_status="unpacked",
        unpack_dir=str(unpack_dir), file_count=1,
        file_index=[{"path": "readme.txt", "type": "file", "size": 10}],
        ref_count=0,
    )
    db.add(rec)
    db.commit()
    return rec


# ---------------------------------------------------------------------------
# 1. 混合并发 acquire/release：净计数精确（多线程独立事务，真实 PG）
# ---------------------------------------------------------------------------

class TestMixedConcurrentRefCount:
    def test_mixed_acquire_release_net_exact(self, pg_engine, db_session):
        """初始 10；4 线程 ×10 acquire 与 4 线程 ×5 release 同时开跑 → 精确 30

        混合操作下读改写实现同样丢更新（自增与自减互相覆盖）；原子 UPDATE
        由行锁串行化，净计数 = 初始 + Σ(+1) - Σ(-1)。
        """
        fid = f"w11ifid_mix_{uuid.uuid4().hex[:8]}"
        db_session.add(HubFileRecord(
            id=f"hub_{fid}", file_id=fid, unpack_status="unpacked", ref_count=10))
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

        barrier = threading.Barrier(N)
        threads = [threading.Thread(target=_run, args=("acquire", 10, barrier))
                   for _ in range(4)]
        threads += [threading.Thread(target=_run, args=("release", 5, barrier))
                    for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)

        assert errors == [], f"混合并发不得有预期外异常: {errors}"
        with maker() as s:
            assert s.query(HubFileRecord).filter(
                HubFileRecord.file_id == fid).one().ref_count == 10 + 4 * 10 - 4 * 5

        # 收尾：全部释放 → 归零清理（w11i 前缀自建自清）
        with maker() as s:
            while release_ref(s, fid) > 0:
                pass
            assert s.query(HubFileRecord).filter(
                HubFileRecord.file_id == fid).first() is None

    def test_release_floor_zero_no_negative(self, pg_engine, db_session, tmp_path):
        """ref_count=0 再释放：返回 0、真实 SQL CASE 下限保证不出现 -1、记录被条件删除"""
        fid = f"w11ifid_floor_{uuid.uuid4().hex[:8]}"
        unpack_dir = tmp_path / fid
        _register_unpacked(db_session, fid, unpack_dir)
        # 直接置 0（登记时即 0，模拟补偿释放后已无引用的登记残留）
        db_session.query(HubFileRecord).filter(
            HubFileRecord.file_id == fid).update({"ref_count": 0})
        db_session.commit()

        with sessionmaker(bind=pg_engine)() as s:
            remaining = release_ref(s, fid)
            assert remaining == 0
            assert s.query(HubFileRecord).filter(
                HubFileRecord.file_id == fid).first() is None, "归零条件删除应命中"
        assert not unpack_dir.exists(), "归零清理应清理解包目录"


# ---------------------------------------------------------------------------
# 2. release vs 并发 acquire 竞态不变量（多轮采样）
# ---------------------------------------------------------------------------

class TestReleaseAcquireRaceInvariant:
    def test_release_vs_acquire_race_preserves_invariant(
        self, pg_engine, db_session, tmp_path,
    ):
        """ref_count=1 时 release 与 acquire 同时开跑，多轮采样不变量：

        记录存在 ⇔ 解包目录存在；acquire 方要么返回 ≥1 权威计数、要么收到
        「未登记」HubFileError——绝不出现「acquire 成功但记录/目录已被清理」
        的悬空引用（条件删除 WHERE ref_count=0 保证）。
        """
        maker = sessionmaker(bind=pg_engine)
        rounds = 6
        for r in range(rounds):
            fid = f"w11ifid_race_{uuid.uuid4().hex[:8]}"
            unpack_dir = tmp_path / f"{fid}_r{r}"
            _register_unpacked(db_session, fid, unpack_dir)
            db_session.query(HubFileRecord).filter(
                HubFileRecord.file_id == fid).update({"ref_count": 1})
            db_session.commit()

            barrier = threading.Barrier(2)
            outcomes: dict = {}

            def _release():
                session = maker()
                try:
                    barrier.wait(timeout=30)
                    outcomes["release"] = release_ref(session, fid)
                except Exception as e:  # noqa: BLE001
                    outcomes["release"] = e
                finally:
                    session.close()

            def _acquire():
                session = maker()
                try:
                    barrier.wait(timeout=30)
                    outcomes["acquire"] = acquire_ref(session, fid)
                except Exception as e:  # noqa: BLE001
                    outcomes["acquire"] = e
                finally:
                    session.close()

            t1 = threading.Thread(target=_release)
            t2 = threading.Thread(target=_acquire)
            t1.start()
            t2.start()
            t1.join(timeout=60)
            t2.join(timeout=60)

            # release 方：成功返回 0/1 或记录已被并发 acquire 侧…（acquire 不删记录）
            # release 的预检查只可能因记录不存在而失败，而 acquire 方不删记录，
            # 故 release 必成功返回 int
            assert isinstance(outcomes.get("release"), int), \
                f"第 {r} 轮 release 应成功: {outcomes}"

            acq = outcomes.get("acquire")
            with maker() as s:
                rec = s.query(HubFileRecord).filter(
                    HubFileRecord.file_id == fid).first()
                if rec is None:
                    # 不变量 A：记录不存在 ⇔ 目录不存在，且 acquire 必收到「未登记」
                    assert not unpack_dir.exists(), \
                        f"第 {r} 轮记录已清理但解包目录残留（悬空目录）: {outcomes}"
                    assert isinstance(acq, HubFileError) and "未登记" in str(acq), \
                        f"第 {r} 轮记录已清理但 acquire 未收到未登记错误: {outcomes}"
                else:
                    # 不变量 B：记录存在 ⇔ 目录存在，且 acquire 成功拿到 ≥1 权威计数
                    assert unpack_dir.exists(), \
                        f"第 {r} 轮记录仍在但解包目录被误清（悬空引用）: {outcomes}"
                    assert int(rec.ref_count) >= 1
                    assert isinstance(acq, int) and acq >= 1, \
                        f"第 {r} 轮记录仍在但 acquire 未成功: {outcomes}"
                    # 收尾释放（acquire 持有的引用 + release 未触达时的初始引用）
                    with_data = int(rec.ref_count)
                    s.close()
                    sess = maker()
                    try:
                        for _ in range(with_data):
                            release_ref(sess, fid)
                    finally:
                        sess.close()
                    assert not unpack_dir.exists(), f"第 {r} 轮收尾后目录应清理"


# ---------------------------------------------------------------------------
# 3. 互斥锁压力版：4 worker 同 job 重投仅 1 个执行
# ---------------------------------------------------------------------------

class TestRedeliveryMutexStress:
    def test_four_workers_same_job_single_execution(
        self, pg_engine, db_session, local_redis, monkeypatch, tmp_path,
    ):
        """4 线程并发投递同一 job：恰 1 个 completed、3 个 duplicate_rejected；
        数据行 2 条不重复、ref_count 各 1 不双计、锁最终释放"""
        from app.tasks.celery_tasks import run_hub_import_job

        ds, job, fids = _make_dataset_with_job(db_session, n_files=2)
        for fid in fids:
            _register_unpacked(db_session, fid, tmp_path / fid)
        job_id = job.id
        lock_key = f"hubjob:{job_id}"

        maker = sessionmaker(bind=pg_engine)
        monkeypatch.setattr(
            "app.tasks.evaluation_tasks_async.get_db_session", lambda: maker())

        # 首包写入放慢 1.2s：持锁者处理期间其余 3 个重投必撞锁
        from app.services.dataset_service import DatasetDataService

        orig_add = DatasetDataService.add_data_v2
        calls = {"n": 0}

        def slow_add(self, dataset_id, data):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(1.2)
            return orig_add(self, dataset_id, data)

        monkeypatch.setattr(DatasetDataService, "add_data_v2", slow_add)

        outcomes: list = []

        def _run(tag: int):
            time.sleep(tag * 0.25)  # 错峰投递，全部落在首个 worker 的持锁窗口内
            try:
                outcomes.append(run_hub_import_job(job_id))
            except Exception as e:  # noqa: BLE001
                outcomes.append(e)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)

        statuses = sorted(
            o["status"] if isinstance(o, dict) else f"error:{o}" for o in outcomes
        )
        assert statuses == ["completed"] + ["duplicate_rejected"] * 3, \
            f"4 重投应恰 1 执行 3 拒绝: {outcomes}"
        rejected = [o for o in outcomes if isinstance(o, dict)
                    and o["status"] == "duplicate_rejected"]
        assert all(o["job_id"] == job_id for o in rejected)
        assert all(o.get("lock_holder") for o in rejected), "拒绝回执应带持锁者标识"

        with maker() as s:
            rows = s.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == ds.id).all()
            assert len(rows) == 2, f"数据行不重复（实际 {len(rows)} 条）"
            assert sorted(r.data.get("hub_file_id") for r in rows) == sorted(fids)
            done = s.query(HubImportJob).filter(HubImportJob.id == job_id).one()
            assert done.status == "completed"
            assert sorted(done.succeeded_file_ids or []) == sorted(fids)
            for fid in fids:
                rec = s.query(HubFileRecord).filter(
                    HubFileRecord.file_id == fid).one()
                assert rec.ref_count == 1, f"{fid} 引用不双计"
        assert local_redis.get(lock_key) is None, "任务结束后互斥锁应释放"
        local_redis.delete(lock_key)


# ---------------------------------------------------------------------------
# 4. 卡死接管（真实 PG timestamptz）：与已交付用例互补的边界
# ---------------------------------------------------------------------------

class TestStuckTakeoverBoundary:
    def test_takeover_at_exactly_threshold_plus_grace_refused(
        self, db_session, local_redis,
    ):
        """updated_at 恰在阈值内（阈值-1s）的 running 拒绝接管——边界不提前放行"""
        _, job, _ = _make_dataset_with_job(db_session, n_files=1, status="running")
        job_id = job.id
        db_session.execute(
            text("UPDATE hub_import_jobs SET updated_at = :ts WHERE id = :id"),
            {"ts": datetime.now(timezone.utc)
                - timedelta(seconds=30 * 60 - 1), "id": job_id},
        )
        db_session.commit()
        db_session.expire_all()
        with pytest.raises(ValueError, match="进行中"):
            retry_job(db_session, job_id)
        assert local_redis.get(f"hubjob:{job_id}") is None, "拒绝路径不得产生锁副作用"
