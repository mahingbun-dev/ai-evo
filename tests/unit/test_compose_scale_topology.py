"""ARCH-3-1 多机扩容 compose 模板：静态门禁（L1，无 docker 依赖）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §3 Phase 3 表 3-1 行
（worker+executor 成对部署到评测机、连同一 Redis/PG、script_runs 用本机盘、
仅脚本库/数据集缓存走共享存储）/ §2.3 D1-D4 /「远期：K8s 驱动」章节的
工作区路径抽象。

覆盖（yaml 解析断言 docker-compose.scale.yml 结构 + .env.scale.example 占位
完整性 + docs/deploy-scale.md 接线门禁）：
  1. worker-eval：无 profile 门（评测机常驻）、SERVICE_TYPE=worker、
     CELERY_WORKER_QUEUES=eval（ARCH-2-1 配置面 -Q 等价物，队列名与
     app.celery_app.TASK_QUEUE_NAMES 同源）、SCRIPT_EXEC_MODE 缺省 container、
     env_file=.env.scale、无 docker.sock、不发布宿主端口、加固/优雅退出/
     健康探活与主控机 worker 同口径、等 volume-init；
  2. 共享存储边界（设计 3-1 核心）：uploads 根挂共享源（无缺省、:? fail-loud
     ——静默本地缺省会让脚本库分叉），script_runs 经嵌套挂载遮蔽为本机盘
     （缺省 ./script_runs 本地 bind），且 worker-eval 与 executor 两层挂载
     逐字一致（executor 沙箱白名单 realpath 以 /app/uploads/script_runs
     子树为界，视图分叉 = 沙箱落盘与 worker 读盘各说各话）；
  3. executor：与主控机 ARCH-1-3 同口径（profile sandbox、唯一挂 sock、
     无 env_file、EXECUTOR_TOKEN 插值、docker 组补充组、加固、/health）；
  4. egress + aievo-sandbox-egress 网络：与主控机同款（网络名钉死裸名、
     逐字等于 executor/sandbox_params.NET_PROFILES["egress"]、internal）；
  5. 评测机无中心面：不定义 redis/postgres/api/worker-ingest 等服务，
     DATABASE_URL/REDIS_URL 不在 environment 重复声明（经 env_file 注入，
     占位在 .env.scale.example 指向主控机）；
  6. .env.scale.example 占位完整性：compose 里引用的全部 ${VAR} 插值变量
     在模板中有对应条目（含注释态可选项），中心数据面占位齐全；
  7. docs/deploy-scale.md 接线门禁：共享存储边界、队列接手机制、中心
     Redis 暴露面收敛（requirepass + 限源）、LAN 交付流程均有载明。
"""
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit  # W8 分类门禁：纯静态断言，无 docker/网络/DB

REPO_ROOT = Path(__file__).resolve().parents[2]
SCALE_COMPOSE_PATH = REPO_ROOT / "docker-compose.scale.yml"
SCALE_ENV_EXAMPLE_PATH = REPO_ROOT / ".env.scale.example"
SCALE_DOC_PATH = REPO_ROOT / "docs" / "deploy-scale.md"
MAIN_COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

SHARED_SOURCE_EXPR = (
    "${UPLOADS_VOLUME:?须在 .env.scale 配置共享存储挂载源(如 NFS 挂载点)，"
    "缺省本地目录会造成脚本库/数据集缓存与主控机分叉}"
)


# ======================================================================
# 公共 fixture / 工具（口径与 test_compose_topology.py 一致）
# ======================================================================

@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(SCALE_COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose):
    assert {"worker-eval", "executor", "egress", "volume-init"} <= set(compose["services"])
    return compose["services"]


def _mounts(svc) -> dict:
    """volumes 列表 → {容器路径: 源}（与 test_compose_topology.py 同实现；
    源含 ${VAR:-default}/${VAR:?msg} 时 rsplit 取目标仍正确）"""
    mounts = {}
    for entry in svc.get("volumes", []):
        if entry.endswith(":ro"):
            entry = entry[: -len(":ro")]
        target = entry.rsplit(":", 1)[1]
        source = entry.rsplit(":", 1)[0]
        mounts[target] = source
    return mounts


def _env_map(svc) -> dict:
    env = svc.get("environment", [])
    assert isinstance(env, list), "本仓库 compose 约定 environment 用列表形态"
    return dict(entry.split("=", 1) for entry in env)


# ======================================================================
# 1. worker-eval：评测机 eval 车道常驻 worker
# ======================================================================

class TestWorkerEval:
    def test_no_profile_gate_always_on(self, services):
        """评测机模板的存在意义就是 eval 执行面：worker-eval 不挂 profile
        （沙箱面 executor/egress 才有 sandbox 灰度开关）"""
        assert "profiles" not in services["worker-eval"]

    def test_service_type_and_eval_queue_only(self, services):
        """ARCH-2-1 队列语义（配置面 -Q 等价物）：entrypoint 的 worker 分支
        固定 celery 命令不接 -Q，消费队列经 CELERY_WORKER_QUEUES 收敛为
        eval——与主控机 worker 同一队列（经中心 Redis 竞争分流），拼错即
        容器启动 fail-fast。队列名与单一事实源 TASK_QUEUE_NAMES 对齐"""
        from app.celery_app import TASK_QUEUE_NAMES, QUEUE_EVAL

        env = _env_map(services["worker-eval"])
        assert env.get("SERVICE_TYPE") == "worker"
        assert env.get("CELERY_WORKER_QUEUES") == QUEUE_EVAL
        assert QUEUE_EVAL in TASK_QUEUE_NAMES

    def test_same_queue_lane_as_main_worker(self, services):
        """与主控机 worker 同消费 eval 队列（多机分流的语义前提：两机 worker
        必须在同一车道上，ingest/maint 留主控机）"""
        main = yaml.safe_load(MAIN_COMPOSE_PATH.read_text(encoding="utf-8"))
        main_env = _env_map(main["services"]["worker"])
        assert main_env.get("CELERY_WORKER_QUEUES") == \
            _env_map(services["worker-eval"]).get("CELERY_WORKER_QUEUES")

    def test_env_file_points_to_scale_env(self, services):
        """容器 env 经 .env.scale 注入（与主控机 compose 读 .env 的口径平行）；
        文档/命令统一 --env-file .env.scale 让插值与 env_file 同源"""
        assert services["worker-eval"].get("env_file") == [".env.scale"]

    def test_central_db_redis_not_redeclared(self, services):
        """中心 Redis/PG 的连接串占位在 .env.scale（DATABASE_URL/REDIS_URL），
        compose 不重复声明（与主控机 compose 同口径：env_file 注入）"""
        env = _env_map(services["worker-eval"])
        assert "DATABASE_URL" not in env
        assert "REDIS_URL" not in env

    def test_script_exec_mode_defaults_to_container(self, services):
        """ARCH-1-4：合规态即缺省态（评测机与主控机同款，local 仅显式 env）"""
        assert _env_map(services["worker-eval"]).get("SCRIPT_EXEC_MODE") == \
            "${SCRIPT_EXEC_MODE:-container}"

    def test_never_mounts_docker_sock_no_host_ports(self, services):
        """sock 只给成对的 executor（设计 D1 信任边界）；评测机 worker 同样
        不发布宿主端口（纯执行面，无入站需求）"""
        assert not any("/var/run/docker.sock" in v
                       for v in services["worker-eval"].get("volumes", []))
        assert "ports" not in services["worker-eval"]

    def test_hardening_like_main_worker(self, services):
        """ARCH-0-1 加固口径与主控机 worker 逐条一致 + 优雅退出 7200s
        （W10-3，对齐 task_time_limit——也是拔机重投语义的一部分）"""
        w = services["worker-eval"]
        assert [c.upper() for c in w.get("cap_drop", [])] == ["ALL"]
        assert "no-new-privileges:true" in w.get("security_opt", [])
        assert w.get("read_only") is True
        assert any(e.startswith("/tmp:") for e in w.get("tmpfs", []))
        assert w.get("stop_grace_period") == "7200s"

    def test_healthcheck_celery_ping_by_hostname(self, services):
        """健康探活与主控机 worker 同款：celery inspect ping 按
        celery@HOSTNAME 定点自问（EVAL_NODE_NAME 自定义 hostname 下同样成立）"""
        test = services["worker-eval"]["healthcheck"]["test"]
        joined = " ".join(test)
        assert "inspect ping -d celery@$$HOSTNAME" in joined
        assert "${EVAL_NODE_NAME:-" in services["worker-eval"].get("hostname", "")

    def test_waits_for_volume_init(self, services):
        """等卷属主迁移（共享面 + 本机盘两层都按最终视角挂进 volume-init）"""
        deps = services["worker-eval"].get("depends_on")
        assert isinstance(deps, dict)
        assert deps.get("volume-init", {}).get("condition") == \
            "service_completed_successfully"

    def test_waits_for_no_local_redis(self, services):
        """无本地 redis depends_on：broker 在主控机，跨机不能 depends_on；
        断连自愈靠 celery 重连 + restart: unless-stopped"""
        deps = services["worker-eval"].get("depends_on", {})
        assert "redis" not in deps
        assert services["worker-eval"].get("restart") == "unless-stopped"


# ======================================================================
# 2. 共享存储边界（设计 3-1 核心：脚本库/数据集缓存共享、script_runs 本机盘）
# ======================================================================

class TestSharedStorageBoundary:
    def test_uploads_root_is_shared_mount_without_default(self, services):
        """uploads 根 = 共享存储，无缺省、:? fail-loud：漏配直接起不来。
        静默缺省本地目录会让脚本库/数据集缓存与主控机分叉（任务找不到
        脚本包），比 fail-loud 危险得多"""
        m = _mounts(services["worker-eval"])
        assert m.get("/app/uploads") == SHARED_SOURCE_EXPR
        # 占位符形态必须是必填（:?）而非可缺省（:-）
        assert ":?" in m["/app/uploads"] and ":-" not in m["/app/uploads"].split(":?")[0]

    def test_script_runs_nested_mount_shadows_shared_root(self, services):
        """script_runs 经嵌套挂载遮蔽共享面为本机盘（热 IO 不走网络）：
        容器路径必须是 /app/uploads/script_runs（executor 沙箱白名单
        WORKSPACE_ROOT 的子树界），缺省本地 bind ./script_runs"""
        m = _mounts(services["worker-eval"])
        assert m.get("/app/uploads/script_runs") == "${SCRIPT_RUNS_VOLUME:-./script_runs}"

    def test_worker_and_executor_share_identical_mount_view(self, services):
        """成对硬约束：worker-eval 与 executor 的 uploads 两层挂载逐字一致
        ——executor 监督线程往工作区落 stdout/stderr、realpath 白名单校验，
        都以与 worker 相同的视图为前提，分叉即各说各话"""
        w = _mounts(services["worker-eval"])
        e = _mounts(services["executor"])
        for target in ("/app/uploads", "/app/uploads/script_runs"):
            assert e[target] == w[target], target

    def test_volume_init_sees_same_nested_view(self, services):
        """volume-init 按最终视角挂载（共享面 + 本机盘 + 本机日志），
        属主迁移覆盖 worker-eval 实际看到的全部写面"""
        v = _mounts(services["volume-init"])
        w = _mounts(services["worker-eval"])
        for target in ("/app/uploads", "/app/uploads/script_runs", "/app/logs"):
            assert v[target] == w[target], target

    def test_script_runs_source_var_distinct_from_shared(self):
        """本机盘与共享面必须由不同变量驱动（防「共享源顺带挂出子目录」的
        退化写法）；compose 顶层同时声明了本机盘命名卷备选"""
        text = SCALE_COMPOSE_PATH.read_text(encoding="utf-8")
        assert "SCRIPT_RUNS_VOLUME" in text and "UPLOADS_VOLUME" in text
        compose = yaml.safe_load(text)
        assert "script_runs" in compose.get("volumes", {})


# ======================================================================
# 3. executor：与主控机 ARCH-1-3 同口径 + 成对部署
# ======================================================================

class TestScaleExecutor:
    def test_gated_by_sandbox_profile(self, services):
        assert "sandbox" in services["executor"].get("profiles", [])

    def test_only_role_mounting_docker_sock(self, services):
        """评测机上 sock 仍只给 executor 一个角色（worker-eval 永不挂）"""
        for name, svc in services.items():
            has_sock = any(
                entry.startswith("/var/run/docker.sock:")
                for entry in svc.get("volumes", []))
            assert has_sock == (name == "executor"), name

    def test_no_env_file_token_via_interpolation(self, services):
        """凭证边界与主控机同款：不挂 env_file（中心 DB/Redis 凭证不进
        容器），EXECUTOR_TOKEN 仅经插值注入（未配置 = fail-closed）"""
        assert "env_file" not in services["executor"]
        assert _env_map(services["executor"]).get("EXECUTOR_TOKEN") == "${EXECUTOR_TOKEN:-}"

    def test_docker_group_gid_supplementary(self, services):
        assert "${DOCKER_GROUP_GID:-999}" in services["executor"].get("group_add", [])

    def test_hardened_and_health_probe(self, services):
        """加固口径与 /health 探活与主控机 executor 逐条一致"""
        ex = services["executor"]
        assert [c.upper() for c in ex.get("cap_drop", [])] == ["ALL"]
        assert "no-new-privileges:true" in ex.get("security_opt", [])
        assert ex.get("read_only") is True
        assert "http://127.0.0.1:8300/health" in " ".join(ex["healthcheck"]["test"])

    def test_no_host_ports_internal_only(self, services):
        assert "ports" not in services["executor"]


# ======================================================================
# 4. egress 与沙箱网络（与主控机/NET_PROFILES 单一事实源同步）
# ======================================================================

class TestScaleEgressAndNetwork:
    def test_egress_same_shape_as_main(self, services):
        """与主控机 egress 同款：profile sandbox、官方 squid 镜像可换源、
        策略/白名单只读挂载 docker/egress/、双网络、squid -k check 探活、
        不发布宿主端口"""
        eg = services["egress"]
        assert "sandbox" in eg.get("profiles", [])
        assert eg["image"] == "${EGRESS_IMAGE:-ubuntu/squid:latest}"
        assert eg["healthcheck"]["test"] == ["CMD", "squid", "-k", "check"]
        assert "ports" not in eg
        assert set(eg.get("networks", [])) == {"ai-evo-net", "aievo-sandbox-egress"}
        for entry in eg["volumes"]:
            assert entry.endswith(":ro"), entry
        # 挂载源模板真实存在（否则起容器时 docker 落目录）
        for source in ("docker/egress/squid.conf", "docker/egress/whitelist.txt"):
            assert (REPO_ROOT / source).is_file(), source

    def test_sandbox_egress_network_pinned_bare_name(self, compose):
        """网络名逐字等于 executor/sandbox_params.NET_PROFILES["egress"]：
        评测机 executor 用本机宿主侧 docker CLI 的裸网络名起沙箱容器，
        compose 项目前缀必须用 name: 钉死（每台评测机 daemon 独立，同名
        网络互不相干、天然按机隔离）"""
        from executor.sandbox_params import NET_PROFILES

        expected = NET_PROFILES["egress"]
        net = compose["networks"].get(expected)
        assert net is not None, f"compose 缺网络 {expected}"
        assert net.get("name") == expected, "必须 name: 钉死，防 compose 项目前缀"
        assert net.get("internal") is True
        assert net.get("driver") == "bridge"


# ======================================================================
# 5. 评测机无中心面（纯 eval 执行面模板）
# ======================================================================

class TestEvalLaneOnly:
    def test_no_central_or_non_eval_services_defined(self, compose):
        """不定义 redis/postgres/api/worker-ingest/监控栈：中心数据面在
        主控机，ingest/maint 车道留主控机（ARCH-2-1 拆分 + 设计 3-1 成对
        部署的边界——评测机只摊 eval 吞吐）"""
        assert set(compose["services"]) == \
            {"volume-init", "worker-eval", "executor", "egress"}


# ======================================================================
# 6. .env.scale.example 占位完整性
# ======================================================================

class TestScaleEnvExample:
    @pytest.fixture(scope="class")
    def example_keys(self):
        """模板中出现的全部变量键（含注释态可选项）"""
        keys = set()
        for line in SCALE_ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line.strip())
            if m:
                keys.add(m.group(1))
        return keys

    def test_every_compose_interpolation_var_has_example_entry(self, example_keys):
        """compose 里引用的每个 ${VAR} 插值变量在 .env.scale.example 有
        对应条目（防「模板缺占位 → 部署机抄不到」的漂移）"""
        text = SCALE_COMPOSE_PATH.read_text(encoding="utf-8")
        referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::[-?][^}]*)?\}", text))
        assert referenced, "正则应至少抽到 IMAGE_TAG 等插值变量"
        missing = referenced - example_keys
        assert not missing, f".env.scale.example 缺占位: {sorted(missing)}"

    def test_central_plane_placeholders_present(self, example_keys):
        """中心数据面占位齐全：DATABASE_URL/REDIS_URL（指向主控机）+
        worker 启动必填的 AUTH_JWT_SECRET + 沙箱面 EXECUTOR_TOKEN/URL/
        DOCKER_GROUP_GID + 共享挂载 UPLOADS_VOLUME + 节点名 EVAL_NODE_NAME"""
        for key in ("DATABASE_URL", "REDIS_URL", "AUTH_JWT_SECRET", "EXECUTOR_TOKEN",
                    "EXECUTOR_URL", "DOCKER_GROUP_GID", "UPLOADS_VOLUME",
                    "EVAL_NODE_NAME", "IMAGE_TAG", "COMPOSE_PROFILES"):
            assert key in example_keys, f".env.scale.example 缺 {key}"
        text = SCALE_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        # 中心占位指向主控机（模板值含 <主控机IP> 引导），且评测机默认开沙箱面
        assert "主控机IP" in text
        assert "COMPOSE_PROFILES=sandbox" in text


# ======================================================================
# 7. 部署文档门禁（docs/deploy-scale.md）
# ======================================================================

class TestDeployScaleDoc:
    def test_covers_shared_storage_boundary(self):
        """共享存储边界必须写清：脚本库/数据集缓存共享、script_runs 本机盘
        （热 IO 不走网络）、嵌套挂载实现、UPLOADS_VOLUME 必填 fail-loud"""
        text = SCALE_DOC_PATH.read_text(encoding="utf-8")
        for needle in ("pre_judge_scripts", "script_runs", "本机盘", "共享存储",
                        "遮蔽", "UPLOADS_VOLUME", "NFS"):
            assert needle in text, f"deploy-scale.md 缺 {needle}"

    def test_covers_queue_and_takeover_semantics(self):
        """队列语义与 2-1 一致（CELERY_WORKER_QUEUES=eval、不走 -Q 命令行），
        拔机接手机制（visibility_timeout 重投）有载明"""
        text = SCALE_DOC_PATH.read_text(encoding="utf-8")
        for needle in ("CELERY_WORKER_QUEUES=eval", "TASK_ROUTES",
                        "visibility_timeout", "接手"):
            assert needle in text, f"deploy-scale.md 缺 {needle}"

    def test_covers_central_plane_exposure_hardening(self):
        """中心 Redis 暴露面收敛（requirepass + 防火墙限源）必须载明——
        redis 镜像默认无鉴权，这是多机拓扑的新增攻击面"""
        text = SCALE_DOC_PATH.read_text(encoding="utf-8")
        for needle in ("requirepass", "docker-compose.override.yml", "防火墙"):
            assert needle in text, f"deploy-scale.md 缺 {needle}"

    def test_covers_delivery_flow_and_acceptance(self):
        """沿用 build-service/LAN tar 交付（docker save/load + 主控机导出）与
        设计 3-1 验收口径（双机吞吐近线性、拔机接手）均落文档"""
        text = SCALE_DOC_PATH.read_text(encoding="utf-8")
        for needle in ("build-pipeline.md", "docker save", "docker load",
                        "IMAGE_TAG", "近线性", "build-service"):
            assert needle in text, f"deploy-scale.md 缺 {needle}"

    def test_references_workspace_abstraction_for_future(self):
        """「远期：K8s 驱动」的工作区路径抽象（workspace 模块 → 对象存储）
        在共享存储边界处有指引（接入 K8s/MinIO 时本节退役、worker 零改动）"""
        text = SCALE_DOC_PATH.read_text(encoding="utf-8")
        for needle in ("WorkspaceBackend", "workspace.py", "MinIO", "K8s"):
            assert needle in text, f"deploy-scale.md 缺 {needle}"
