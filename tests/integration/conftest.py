"""集成测试公共 fixture（W5 + W6W7 + W4 + W3 四包合并版）。

环境约定（计划 §5.1 L2：跑不了明确 skip，不许假绿）：
- 共享开发库 DSN 解析优先级：环境变量 DATABASE_URL（W5）/ W67_IT_SHARED_DSN
  （W6W7）> 主仓 .env；解析不到 / 连接不可达时各 fixture 明确 skip；
- W4 临时库供给（本目录下数据库快照类集成测试专用）：本地 PG 可达时，在
  conftest 导入期（先于一切 app.* 导入）将 DATABASE_URL 无条件覆盖为
  w4t_cfgsnap_ 前缀临时库（TEMPLATE 复制共享库结构 + alembic stamp/upgrade，
  会话结束 DROP，自建自清）——engine 单例随之绑定临时库，目录内全部用例
  统一跑在隔离副本上；本地 PG 不可达时不覆盖，其余用例回退共享库直跑；
- 测试数据自建自清：W5 用 w5it_ / W5IT_ 前缀，W6W7 临时库一律 w67t_ 前缀，
  只删自建数据，严禁对共享库既有对象做 DROP/TRUNCATE。

注意：DATABASE_URL 必须在 import app.* 之前确定——app.db.session 在 import
时通过 get_settings()（lru_cache）创建引擎，本 conftest 先于测试模块执行，
模块级完成设置（先 W5 预设置补齐，后 W4 临时库覆盖）。
"""
import atexit
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import psycopg2
import pytest
from sqlalchemy import create_engine, text

# 测试数据前缀（W5：自建自清，禁止触碰既有对象）
W5_TASK_PREFIX = "w5it_"
W5_USER_PREFIX = "w5it_"
W5_TEAM_PREFIX = "W5IT_"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _database_url_from_env_file() -> str | None:
    """逐级向上探测 .env 并解析 DATABASE_URL（worktree 自身通常无 .env）。"""
    here = Path(__file__).resolve()
    for base in [here.parent, *list(here.parents)[:6]]:
        env_file = base / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


def _main_repo_root() -> Path:
    """主仓根（worktree 的 git-common-dir 的父目录）。"""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(out).resolve().parent


# ---- W5 预设置：必须发生在任何 app.* import 之前（仅当环境变量未显式给出）
if not os.environ.get("DATABASE_URL"):
    _url = _database_url_from_env_file()
    if _url:
        os.environ["DATABASE_URL"] = _url


def _resolve_shared_dsn() -> str:
    dsn = os.environ.get("W67_IT_SHARED_DSN", "").strip()
    if dsn:
        return dsn
    env_file = _main_repo_root() / ".env"
    if env_file.exists():
        try:
            from dotenv import dotenv_values
            return (dotenv_values(env_file) or {}).get("DATABASE_URL", "")
        except ImportError:
            return ""
    return ""


def dsn_parts(dsn: str):
    r = urlparse(dsn)
    return dict(
        host=r.hostname, port=r.port or 5432,
        user=r.username, password=r.password,
        dbname=r.path.lstrip("/"),
    )


class PgAdmin:
    """临时库生命周期管理（w67t_ 前缀，自建自清）。

    注意：psycopg2 2.9 下 `with conn`（connection 上下文管理器）与
    autocommit 组合会开启事务块，CREATE/DROP DATABASE 因此失败
    （已实测 probe 定位）；故本类统一用显式 try/finally 管理连接。
    """

    PREFIX = "w67t_"

    def __init__(self, dsn: str):
        self._psycopg2 = psycopg2
        self._base = dsn_parts(dsn)
        self._base_dbname = dsn_parts(dsn)["dbname"]

    def _admin_connect(self):
        params = dict(self._base, dbname="postgres")
        conn = self._psycopg2.connect(connect_timeout=10, **params)
        conn.autocommit = True
        return conn

    def create_db(self, name: str):
        assert name.startswith(self.PREFIX), f"临时库必须 {self.PREFIX} 前缀: {name}"
        conn = self._admin_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if cur.fetchone():
                    cur.execute(f'DROP DATABASE "{name}"')
                cur.execute(f'CREATE DATABASE "{name}"')
        finally:
            conn.close()

    def drop_db(self, name: str):
        conn = self._admin_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if cur.fetchone():
                    cur.execute(f'DROP DATABASE "{name}"')
        finally:
            conn.close()

    def dsn_for(self, name: str) -> str:
        p = dict(self._base, dbname=name)
        return (
            f"postgresql://{p['user']}:{p['password']}@{p['host']}:{p['port']}/{name}"
        )

    def scalar(self, dbname: str, sql: str):
        params = dict(self._base, dbname=dbname)
        conn = self._psycopg2.connect(connect_timeout=10, **params)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchone()
        finally:
            conn.close()


# ---- W4 临时库供给（导入期执行，见模块 docstring）

# 共享开发库基线 DSN（只读复制结构，绝不做迁移）。解析优先级：
# W4_BASE_DSN 显式覆盖 > DATABASE_URL（环境变量，缺省时上方 W5 预设置
# 已从主仓 .env 探测回填）> 历史缺省（与 CI 一致的 ai_evo:ai_evo）。
# 此前硬编码 ai_evo:ai_evo 与本地 dev 栈口令（docker-compose.dev.yml 为
# ai_evo_dev）不符时探测必失败——W4 临时库供给静默降级为共享库直跑，
# SIGKILL 子进程用例仍把（连不上的）TEMP_DSN 注入子进程而假红。
BASE_DSN = (
    os.environ.get("W4_BASE_DSN", "").strip()
    or os.environ.get("DATABASE_URL", "").strip()
    or "postgresql://ai_evo:ai_evo@127.0.0.1:5432/ai_evo"
)
TEMP_DB_PREFIX = "w4t_cfgsnap_"

_w4parts = urlsplit(BASE_DSN)
_W4_ADMIN_DSN = urlunsplit(_w4parts._replace(path="/postgres"))
TEMP_DB_NAME = f"{TEMP_DB_PREFIX}{secrets.token_hex(4)}"
TEMP_DSN = urlunsplit(_w4parts._replace(path=f"/{TEMP_DB_NAME}"))

# 供给路径证据（测试断言与验收报告引用）
PROVISION_LOG: list = []

# 共享库不可达时的 skip 原因（None = 可用，正常供给）
SKIP_REASON: str | None = None


def _w4_admin_connect():
    conn = psycopg2.connect(_W4_ADMIN_DSN, connect_timeout=5)
    conn.autocommit = True
    return conn


def _base_db_reachable() -> bool:
    try:
        conn = psycopg2.connect(BASE_DSN, connect_timeout=5)
        conn.close()
        return True
    except Exception:  # noqa: BLE001 - 不可达原因不影响 skip 决策
        return False


def _create_database(name: str, template: str | None = None):
    conn = _w4_admin_connect()
    try:
        with conn.cursor() as cur:
            if template:
                cur.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
            else:
                cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


def _alembic_config(dsn: str):
    """显式指向 dsn 的 alembic Config（绝不依赖 alembic.ini 默认 DSN——
    alembic.ini 默认指向远程共享 dev 库，误用会改动共享库）"""
    import alembic.config

    cfg = alembic.config.Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


def _drop_temp_database(name: str):
    conn = _w4_admin_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        conn.close()


def _provision_runtime_db():
    """按优先级供给运行时临时库（路径与原因记录进 PROVISION_LOG）"""
    import alembic.command

    # 路径1：TEMPLATE 复制共享库 + stamp(W4 前基点) + upgrade head
    # 共享库 stamp=f3a9c1d2e4b5 不在仓库迁移链（先存漂移），需先落到
    # W4 迁移的父版本 e4f7b1a9c2d8 再 upgrade（只作用于临时库副本）
    try:
        _create_database(TEMP_DB_NAME, template=_w4parts.path.lstrip("/"))
        alembic.command.stamp(_alembic_config(TEMP_DSN), "e4f7b1a9c2d8")
        alembic.command.upgrade(_alembic_config(TEMP_DSN), "head")
        PROVISION_LOG.append(
            f"template-copy:{_w4parts.path.lstrip('/')}+stamp(e4f7b1a9c2d8)+upgrade(head)"
        )
        return
    except Exception as e:  # noqa: BLE001 - 供给路径逐级降级，原因全部留痕
        PROVISION_LOG.append(f"template-copy-failed:{type(e).__name__}:{str(e).strip()[:160]}")

    # 路径2：空库 + alembic upgrade head 全量
    try:
        _drop_temp_database(TEMP_DB_NAME)  # 清理上一路径半成品
        _create_database(TEMP_DB_NAME)
        alembic.command.upgrade(_alembic_config(TEMP_DSN), "head")
        PROVISION_LOG.append("empty+alembic-upgrade-head")
        return
    except Exception as e:  # noqa: BLE001
        PROVISION_LOG.append(f"empty+alembic-upgrade-head-failed:{type(e).__name__}:{str(e).strip()[:160]}")

    # 路径3：空库 + create_all + stamp head（运行时 schema 与迁移 head 等价）
    _drop_temp_database(TEMP_DB_NAME)  # 清理上一路径半成品
    _create_database(TEMP_DB_NAME)
    # DATABASE_URL 必须在 app.* 导入前指向临时库（见模块 docstring）
    os.environ["DATABASE_URL"] = TEMP_DSN
    from app.db.session import Base, engine
    import app.models  # noqa: F401 - 注册全部模型
    import app.models.ab_test_models  # noqa: F401
    import app.models.test_task_models  # noqa: F401

    Base.metadata.create_all(engine)
    alembic.command.stamp(_alembic_config(TEMP_DSN), "head")
    PROVISION_LOG.append("empty+create_all+stamp(head)")


if not _base_db_reachable():
    # 共享库实例不可达：不覆盖 DATABASE_URL，W4 用例按 SKIP_REASON skip，
    # 其余用例回退共享库直跑（W5/W6W7 原语义）
    SKIP_REASON = (
        f"共享开发库实例不可达（{_w4parts.hostname}:{_w4parts.port}），"
        "W4 集成测试需要本地 PG 临时库供给"
    )
else:
    # DATABASE_URL 必须先于 app.* 任何导入（保证 engine 绑定临时库；
    # 路径1/2 不触 app.*，路径3 及测试模块导入前均已就位）
    os.environ["DATABASE_URL"] = TEMP_DSN
    _provision_runtime_db()
    atexit.register(_drop_temp_database, TEMP_DB_NAME)


# ---- fixtures：W5

@pytest.fixture(scope="module")
def shared_db_engine():
    """共享开发库引擎；DSN 未知或不可达时明确 skip（不假绿）。

    跟随实际 DATABASE_URL：W4 临时库覆盖生效时即临时库（用例结束随库
    DROP 自清），否则为共享库直跑。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip(
            "未设置 DATABASE_URL 且未探测到 .env，共享开发库 DSN 未知，"
            "W5 集成用例跳过（§5.1：跑不了明确 skip）"
        )
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as e:  # noqa: BLE001 - 探测失败统一 skip
        pytest.skip(f"共享开发库不可达（{e}），W5 集成用例跳过（§5.1：跑不了明确 skip）")
    return engine


@pytest.fixture(autouse=True)
def w5_test_data_guard():
    """W5 测试数据自建自清：每用例前后按 w5it_ / W5IT_ 前缀清理。

    注意：不能依赖 shared_db_engine fixture——conftest 是目录级 autouse，
    若在库不可达时 skip 会连带 skip 掉其他包的集成用例。因此本 fixture
    自持引擎：DSN 缺失/不可达时清理降级为 no-op，不 skip 用例。
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        yield
        return
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception:  # noqa: BLE001 - 探测失败只跳过清理
        yield
        return

    def _cleanup():
        with engine.begin() as conn:
            conn.execute(text("delete from evaluation_tasks where id like :p"), {"p": f"{W5_TASK_PREFIX}%"})
            conn.execute(text("delete from users where user_code like :p"), {"p": f"{W5_USER_PREFIX}%"})
            conn.execute(text("delete from teams where team_code like :p"), {"p": f"{W5_TEAM_PREFIX}%"})

    _cleanup()
    try:
        yield
    finally:
        _cleanup()
        # 本 fixture 每用例新建引擎、从不复用：teardown 显式归还连接。
        # 否则 idle 连接只能等 GC 回收，全量 suite 运行时堆积可打满
        # PG max_connections=100（实测单临时库峰值 ~89 连接），殃及
        # 后续用例/迁移探针库管理连接（FATAL: too many clients already）
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001 - 清理降级为 no-op
            pass


@pytest.fixture
def local_redis():
    """被测代码实际连接的本地 Redis（推送链路用）；不可达时明确 skip（不假绿）

    跟随应用同源解析（settings.REDIS_URL：环境变量 > 主仓 .env）。此前硬编码
    127.0.0.1:6379 与 CI 一致，但本地 dev 栈常把 Redis 映射到非 6379 端口
    （docker-compose.dev.yml 用 6380，6379 被其他项目占用）——PubSubListener/
    EvalTaskLock 等被测代码都连 settings.REDIS_URL，测试若查另一台 Redis，
    订阅可见性/锁状态断言必假红（两台实例间永远看不到对方的写入）。
    """
    import redis as redis_lib

    from app.core.config import get_settings

    url = get_settings().REDIS_URL
    try:
        client = redis_lib.from_url(url, decode_responses=True)
        client.ping()
    except Exception as e:  # noqa: BLE001 - 探测失败统一 skip
        pytest.skip(
            f"Redis {urlparse(url).netloc or url} 不可达（{e}），"
            "推送链路用例跳过（§5.1：跑不了明确 skip，不许假绿）"
        )
    yield client
    client.close()


@pytest.fixture
def ws_app_client():
    """FastAPI TestClient（不用 with：绝不触发 lifespan startup）。

    lifespan startup 会把共享库中所有 running 状态任务 fail 掉
    （app/main.py 孤立任务清理），因此这里只构造 client、不进入上下文
    管理器。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    yield client
    # 全局 PubSub 监听线程/连接按用例回收，避免用例间串扰
    from app.api.pubsub_handler import stop_pubsub_listener

    stop_pubsub_listener()


# ---- fixtures：W6W7

@pytest.fixture(scope="module")
def shared_dsn() -> str:
    dsn = _resolve_shared_dsn()
    if not dsn:
        pytest.skip("集成环境缺失：找不到共享开发库 DSN（W67_IT_SHARED_DSN 或主仓 .env）")
    try:
        conn = psycopg2.connect(connect_timeout=5, **dsn_parts(dsn))
        conn.close()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"共享开发库不可达，跳过集成实跑（不假绿）: {e}")
    return dsn


@pytest.fixture(scope="module")
def pg_admin(shared_dsn) -> PgAdmin:
    return PgAdmin(shared_dsn)


# ---- fixtures：W4

@pytest.fixture(scope="session")
def runtime_db():
    """会话级临时库 DSN；结束时 dispose 连接并 DROP 临时库（自建自清）"""
    if SKIP_REASON:
        pytest.skip(SKIP_REASON)
    yield TEMP_DSN
    try:
        from app.db.session import engine

        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    _drop_temp_database(TEMP_DB_NAME)


@pytest.fixture()
def db_session():
    """函数级数据库会话（指向 DATABASE_URL 当前指向的库，见模块 docstring）"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


# ---- 测试 helper：W3（行级落库集成测试共用） ----

def make_judge_task(db, n_rows: int = 10):
    """建 1 个 judge 任务 + V2 数据集（n 行固定 row-NN 标记数据）并 start_task

    返回 task_id。start_task 原子递增 execution_count → 1 并创建执行记录，
    与 celery 链路（API 层 start_task → worker 执行）同口径。
    """
    import uuid

    from app.models.dataset_models import EvaluationDataset, EvaluationDataV2
    from app.models.task_models import EvaluationTask
    from app.services.evaluation_task_service import EvaluationTaskService

    suffix = uuid.uuid4().hex[:8]
    dataset = EvaluationDataset(
        id=f"w3ds_{suffix}",
        name=f"W3行级落库集成测试数据集{suffix}",
        question_count=n_rows,
        is_active=True,
        team_code="DEFAULT",
    )
    db.add(dataset)
    db.flush()
    for i in range(n_rows):
        db.add(
            EvaluationDataV2(
                id=f"w3dv_{suffix}_{i}",
                dataset_id=dataset.id,
                index=i,
                is_active=True,
                data={
                    "user_input": f"W3测试输入 row-{i:02d}（固定打分表数据行）",
                    "answer": f"W3参考答案 row-{i:02d}",
                },
            )
        )
    task = EvaluationTask(
        id=f"w3task_{suffix}",
        name=f"W3行级落库集成测试{suffix}",
        evaluation_type="judge",
        status="pending",
        dataset_id=dataset.id,
        judge_engine="graph",  # 走 JudgeLangGraphAgent 路径（mock 注入点）
        judge_count=1,  # max_workers=1：行序确定，断言可精确
        max_score=10,
        evaluation_metrics=[
            {"name": "准确性", "weight": 0.6, "description": "答案准确程度"},
            {"name": "完整性", "weight": 0.4, "description": "要点覆盖程度"},
        ],
        model_config={
            "provider": "http",
            "model_type": "mock",
            "endpoint": "http://w3-mock.invalid/v1/chat",
        },
        variable_source="dataset",
        team_code="DEFAULT",
        created_by="w3-integration-tester",
    )
    db.add(task)
    db.commit()

    EvaluationTaskService(db).start_task(task.id)
    db.expire_all()
    return task.id


@pytest.fixture()
def judge_task_10_rows(db_session):
    """10 行 mock 评测任务（已 start_task，execution_count=1）"""
    return make_judge_task(db_session, n_rows=10)


def fetch_result_rows(db, task_id: str, execution_number: int = 1):
    """查询行级表（按 data_index 升序）"""
    from app.models.task_models import EvaluationResult

    return (
        db.query(EvaluationResult)
        .filter(
            EvaluationResult.task_id == task_id,
            EvaluationResult.execution_number == execution_number,
        )
        .order_by(EvaluationResult.data_index.asc())
        .all()
    )
