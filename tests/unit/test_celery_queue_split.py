"""ARCH-2-1 Celery 队列拆分 eval/ingest/maint：路由表 + 消费队列接线门禁（L1 静态）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §2.3 决策 D4 /
§3 Phase 2 表 2-1 行（队列拆分 eval/ingest/maint + 多 worker 副本）。

覆盖：
  1. 路由表（app/celery_app.py TASK_ROUTES 是队列归属单一事实源）：
     - run_evaluation_task → eval、run_hub_import_job → ingest：经 celery
       router 真实解析断言（非仅 dict 字面量），且登记名与任务装饰器 name=
       逐字一致（防改名后 dangling key 静默落兜底队列）；
     - 维护任务命名约定 app.tasks.maintenance.* → maint（glob；当前仓库无
       维护任务，为条目 2-4 归档清理预留）；
     - 未登记任务名兜底 eval（拆分前「单 worker 全消费」的存活等价；落回
       celery 缺省队列会因无消费者而静默积压）；
     - 三队列各自独立同名 exchange/routing_key（direct 直连，不复用缺省
       exchange——两个队列绑同一 routing_key 会被投进多个队列重复消费）。
  2. worker 消费队列经 CELERY_WORKER_QUEUES 注入（docker-entrypoint.sh 的
     worker 分支不接 -Q，依赖「无 -Q 消费 task_queues 全集」的 celery 语义）：
     - 未注入 = 全量三队列（api 等生产者容器/本地开发）；
     - 注入单队列 → 队列声明收敛为该队列（worker 即只消费它）；
     - 拼错队列名 / 空串 → RuntimeError fail-fast（不静默消费错队列）。
  3. compose 两个 worker 定义完整且同口径（worker=eval，服务名保持不变以
     兼容部署保活脚本；worker-ingest=ingest）：队列 env、无 command/entrypoint
     覆盖（走 entrypoint 预检）、healthcheck（inspect ping 定点问本容器）、
     stop_grace_period=7200s（W10-3）、日志轮转（W13，json-file 50m×3）、
     共享 uploads/logs 卷、等 volume-init 完成、加固字段。
"""
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit  # W8 分类门禁：纯静态断言，无 docker/网络/DB

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# 拆分后的两个 worker 服务：服务名 → 绑定队列（与 app/celery_app.py 对齐）
WORKER_QUEUE_BINDING = {"worker": "eval", "worker-ingest": "ingest"}


@pytest.fixture(scope="module")
def services():
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert set(WORKER_QUEUE_BINDING) <= set(data["services"]), data["services"].keys()
    return data["services"]


def _env_map(svc) -> dict:
    """environment（列表形态）→ {KEY: 值}"""
    return dict(entry.split("=", 1) for entry in svc.get("environment", []))


# ======================================================================
# 1. 路由表（单一事实源）
# ======================================================================

class TestTaskRoutes:
    def test_eval_and_ingest_tasks_routed_by_celery_router(self):
        """两个存量任务经 celery router 真实解析到各自队列（评测→eval、
        hub 导入→ingest，设计 D4 按任务类型隔离消费）"""
        from app.celery_app import celery_app

        router = celery_app.amqp.router
        assert router.route({}, "app.tasks.celery_tasks.run_evaluation_task")["queue"].name == "eval"
        assert router.route({}, "app.tasks.celery_tasks.run_hub_import_job")["queue"].name == "ingest"

    def test_route_keys_match_registered_task_names(self):
        """路由表登记名 == 任务装饰器声明的 name（逐字）：装饰器改名而路由表
        未同步 → dangling key，任务落兜底队列、被错误 worker 消费"""
        from app.celery_app import TASK_ROUTES
        from app.tasks.celery_tasks import run_evaluation_task, run_hub_import_job

        assert TASK_ROUTES[run_evaluation_task.name]["queue"] == "eval"
        assert TASK_ROUTES[run_hub_import_job.name]["queue"] == "ingest"

    def test_maintenance_namespace_glob_routes_to_maint(self):
        """维护任务命名约定 app.tasks.maintenance.* → maint（glob 预留给条目
        2-4；落地维护任务时无需改路由表）"""
        from app.celery_app import celery_app

        for name in ("app.tasks.maintenance.archive_cleanup",
                     "app.tasks.maintenance.retention_prune"):
            assert celery_app.amqp.router.route({}, name)["queue"].name == "maint"

    def test_exact_name_takes_precedence_over_glob(self):
        """精确名优先于 glob：若未来维护任务命名空间下出现需走 eval 的同名
        前缀任务，精确登记必须能压过 maintenance.* 通配（MapRoute 语义）"""
        from app.celery_app import TASK_ROUTES, _task_queues
        from celery import Celery

        app = Celery("t", broker="memory://", backend="cache+memory://")
        app.conf.update(
            task_queues=_task_queues(("eval", "ingest", "maint")),
            task_routes={**TASK_ROUTES,
                         "app.tasks.maintenance.urgent_eval": {"queue": "eval"}},
        )
        assert app.amqp.router.route({}, "app.tasks.maintenance.urgent_eval")["queue"].name == "eval"

    def test_unrouted_tasks_fall_back_to_eval(self):
        """兜底队列 = eval：未登记任务不落无人消费的缺省队列（celery）静默
        积压——拆分前单 worker 全消费的存活语义等价"""
        from app.celery_app import celery_app

        assert celery_app.conf.task_default_queue == "eval"
        routed = celery_app.amqp.router.route({}, "app.tasks.celery_tasks.some_new_task")
        assert routed["queue"].name == "eval"

    def test_queues_have_dedicated_exchange_and_routing_key(self):
        """三队列各自独立同名 exchange/routing_key：不复用缺省 exchange，
        两个队列绑同一 routing_key 会让 direct 交换机重复投递（重复消费）"""
        from app.celery_app import celery_app

        for name, q in celery_app.amqp.queues.items():
            assert q.exchange.name == name, (name, q.exchange.name)
            assert q.routing_key == name, (name, q.routing_key)
            assert q.exchange.type == "direct", (name, q.exchange.type)


# ======================================================================
# 2. worker 消费队列经 CELERY_WORKER_QUEUES 注入
# ======================================================================

def _fresh_queues(monkeypatch, env_value):
    """以指定 env 复现 celery_app 的接线（task_queues=_resolve_task_queues()）
    并返回该 app 的队列消费集（无 -Q 的 worker 消费 task_queues 全集）"""
    from celery import Celery

    from app.celery_app import _resolve_task_queues

    if env_value is None:
        monkeypatch.delenv("CELERY_WORKER_QUEUES", raising=False)
    else:
        monkeypatch.setenv("CELERY_WORKER_QUEUES", env_value)
    app = Celery("t", broker="memory://", backend="cache+memory://")
    app.conf.update(task_queues=_resolve_task_queues())
    return set(app.amqp.queues.consume_from)


class TestWorkerQueueBinding:
    def test_unset_env_declares_all_three_queues(self, monkeypatch):
        """未注入 = 全量三队列（api 等生产者容器的声明面：按路由表解析每个
        队列的绑定；本地开发跑单 worker 也因此三队列全消费，与拆分前行为
        一致）"""
        from app.celery_app import TASK_QUEUE_NAMES

        assert _fresh_queues(monkeypatch, None) == set(TASK_QUEUE_NAMES)

    @pytest.mark.parametrize("env_value,expected", [
        ("eval", {"eval"}),
        ("ingest", {"ingest"}),
        ("maint,ingest", {"maint", "ingest"}),  # 逗号分隔多队列（worker-maint 预留）
    ])
    def test_env_restricts_worker_consume_set(self, monkeypatch, env_value, expected):
        """注入即收敛：worker 容器只声明/只消费注入的队列（compose 的
        worker=eval、worker-ingest=ingest 即此机制）"""
        assert _fresh_queues(monkeypatch, env_value) == expected

    @pytest.mark.parametrize("env_value", ["bogus", "", "  ", "eval,typo"])
    def test_invalid_env_fails_fast(self, monkeypatch, env_value):
        """拼错队列名/空串 → RuntimeError（fail-fast：宁可不启动，不静默
        消费错队列或全量队列）"""
        from app.celery_app import _resolve_task_queues

        monkeypatch.setenv("CELERY_WORKER_QUEUES", env_value)
        with pytest.raises(RuntimeError, match="CELERY_WORKER_QUEUES"):
            _resolve_task_queues()


# ======================================================================
# 3. compose：两个 worker 定义完整且同口径
# ======================================================================

class TestComposeWorkerSplit:
    def test_both_workers_bind_own_queue(self, services):
        """worker=eval / worker-ingest=ingest：队列名与 celery 侧常量同源
        （拼错会在容器启动时 fail-fast，这里把漂移挡在 CI）"""
        from app.celery_app import TASK_QUEUE_NAMES

        for svc_name, queue in WORKER_QUEUE_BINDING.items():
            env = _env_map(services[svc_name])
            assert env.get("CELERY_WORKER_QUEUES") == queue, svc_name
            assert queue in TASK_QUEUE_NAMES, svc_name

    def test_worker_name_kept_for_deploy_selfheal_scripts(self, services):
        """eval worker 的服务名/容器名保持 worker / ai-evo-worker 不变：
        scripts/ensure_worker.sh（cron 保活）与 do_deploy.sh（应急旁路）点名
        该容器名，改名即断保活链路（ARCH-2-1 拆分的最小改动约束）"""
        assert services["worker"]["container_name"] == "ai-evo-worker"
        assert services["worker-ingest"]["container_name"] == "ai-evo-worker-ingest"

    @pytest.mark.parametrize("svc_name", list(WORKER_QUEUE_BINDING))
    def test_no_command_or_entrypoint_override(self, services, svc_name):
        """不得用 command/entrypoint 覆盖传 -Q：会绕过 docker-entrypoint.sh
        的卷可写预检（ARCH-0-1）；队列绑定只走 CELERY_WORKER_QUEUES 配置面"""
        svc = services[svc_name]
        assert "command" not in svc, svc_name
        assert "entrypoint" not in svc, svc_name
        assert "SERVICE_TYPE=worker" in svc["environment"], svc_name

    @pytest.mark.parametrize("svc_name", list(WORKER_QUEUE_BINDING))
    def test_healthcheck_stop_grace_and_log_rotation(self, services, svc_name):
        """沿用既有 worker 惯例：inspect ping 定点问本容器（W13）、
        stop_grace_period=7200s 与 task_time_limit 对齐（W10-3）、
        json-file 50m×3 日志轮转（W13）"""
        svc = services[svc_name]
        test = svc["healthcheck"]["test"]
        assert " ".join(test).startswith("CMD-SHELL celery -A app.celery_app inspect ping")
        assert "-d celery@$$HOSTNAME" in " ".join(test)
        assert svc["stop_grace_period"] == "7200s"
        assert svc["logging"]["driver"] == "json-file"
        assert svc["logging"]["options"] == {"max-size": "50m", "max-file": "3"}

    @pytest.mark.parametrize("svc_name", list(WORKER_QUEUE_BINDING))
    def test_shared_volumes_and_volume_init_dependency(self, services, svc_name):
        """与 api 共享同一组 uploads/logs 卷（导入/评测都读写 uploads），
        并等 volume-init 属主迁移完成再起（ARCH-0-1）"""
        svc = services[svc_name]
        mounts = {e.rsplit(":", 1)[1]: e.rsplit(":", 1)[0] for e in svc["volumes"]}
        api_mounts = {e.rsplit(":", 1)[1]: e.rsplit(":", 1)[0]
                      for e in services["api"]["volumes"]}
        assert mounts == api_mounts, svc_name
        deps = svc["depends_on"]
        assert deps.get("volume-init", {}).get("condition") == "service_completed_successfully"
        assert "redis" in deps, svc_name

    @pytest.mark.parametrize("svc_name", list(WORKER_QUEUE_BINDING))
    def test_hardening_and_restart_policy(self, services, svc_name):
        """两 worker 同口径加固（cap_drop ALL / no-new-privileges / read_only /
        tmpfs uid 10002）+ unless-stopped 自愈（worker-ingest 无 cron 保活，
        restart 策略是它的底线自愈）"""
        svc = services[svc_name]
        assert [c.upper() for c in svc["cap_drop"]] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert svc["read_only"] is True
        assert any(e.startswith("/home/app:") and "uid=10002" in e for e in svc["tmpfs"])
        assert svc["restart"] == "unless-stopped"
        assert svc.get("networks") == ["ai-evo-net"]
