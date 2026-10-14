"""ARCH-1-3 compose 编排：executor 服务与 egress 出口——静态门禁（L1，无 docker 依赖）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §2.2 拓扑 / §2.3 D1-D2 /
§3 Phase 1 表 1-3 行（compose 增加 executor + 沙箱专用 bridge 网络；worker 出网
切 egress proxy）。

覆盖（yaml 解析断言服务/网络结构 + squid 策略静态断言 + 文档接线门禁）：
  1. executor 服务：唯一挂 docker.sock 的角色——非 root 访问经 docker 组补充
     组、不发布宿主端口（仅 compose 内网）、不挂 env_file（.env 平台凭证不进
     容器，仅插值注入 EXECUTOR_TOKEN）、与 api/worker 同口径加固、健康探活、
     等待 volume-init 卷属主迁移、profile sandbox 灰度开关；
  2. egress 代理：squid 官方镜像、白名单配置只读挂载 docker/egress/、域名
     清单模板可经 env 整体替换、双网络（服务 worker + 沙箱 internal 网）、
     不发布宿主端口、healthcheck；
  3. aievo-sandbox-egress 网络：internal（沙箱无直连出网路由，唯一出口 =
     egress 代理）且网络名逐字等于 executor/sandbox_params.NET_PROFILES
     的 egress 档（单一事实源：compose 默认给网络加项目前缀，必须 name: 钉死）；
  4. worker 侧 proxy 接线（只改 compose 不改代码）：HTTP(S)_PROXY 大小写各一、
     缺省空 = 灰度关、NO_PROXY 覆盖 compose 服务名与回环；worker 永不挂
     docker.sock（sock 只给 executor 一个角色）；
  5. squid 策略语义：deny 内网目的地址先于 allow 白名单（SSRF 收口不可被
     白名单顺序绕过）、末条 deny all、CONNECT 仅 443；白名单文件全部为合法
     dstdomain 条目；
  6. 部署接线文档：.env.example 收录全部新开关，build-pipeline.md 载明
     SCRIPT_NET_PROFILE 档位与 compose 网络对应关系及启用步骤。
"""
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit  # W8 分类门禁：纯静态断言，无 docker/网络/DB

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
BUILD_DOC_PATH = REPO_ROOT / "docs" / "build-pipeline.md"
EGRESS_CONF_PATH = REPO_ROOT / "docker" / "egress" / "squid.conf"
EGRESS_WHITELIST_PATH = REPO_ROOT / "docker" / "egress" / "whitelist.txt"

SANDBOX_PROFILE = "sandbox"


# ======================================================================
# 公共 fixture / 工具
# ======================================================================

@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose):
    assert {"executor", "egress", "worker", "volume-init"} <= set(compose["services"])
    return compose["services"]


def _mounts(svc) -> dict:
    """volumes 列表 → {容器路径: 源}（剥离尾缀 :ro；源含 ${VAR:-default} 时
    rsplit 取目标仍正确——容器路径不含冒号）"""
    mounts = {}
    for entry in svc.get("volumes", []):
        if entry.endswith(":ro"):
            entry = entry[: -len(":ro")]
        target = entry.rsplit(":", 1)[1]
        source = entry.rsplit(":", 1)[0]
        mounts[target] = source
    return mounts


def _env_map(svc) -> dict:
    """environment（列表形态）→ {KEY: 原始值}；EXECUTOR_TOKEN=${EXECUTOR_TOKEN:-}
    这类插值保留原样，断言按插值形态做"""
    env = svc.get("environment", [])
    assert isinstance(env, list), "本仓库 compose 约定 environment 用列表形态"
    return dict(entry.split("=", 1) for entry in env)


# ======================================================================
# 1. executor 服务：唯一持 docker.sock 的角色
# ======================================================================

class TestExecutorService:
    def test_gated_by_sandbox_profile(self, services):
        """灰度开关：不设 COMPOSE_PROFILES 时既有部署行为完全一致
        （离线部署机未预载新镜像不受影响）"""
        assert SANDBOX_PROFILE in services["executor"].get("profiles", [])

    def test_no_host_ports_internal_only(self, services):
        """仅 compose 内网监听：不发布任何宿主端口（worker 经
        http://executor:8300 访问；8300 只在镜像 EXPOSE 层面）"""
        assert "ports" not in services["executor"]

    def test_only_role_mounting_docker_sock(self, services):
        """docker.sock 只挂给 executor 一个角色（worker/api 永不挂——设计 D1
        信任边界：被攻破面最大的 worker 不直连宿主 daemon）"""
        for name, svc in services.items():
            has_sock = any(
                entry.startswith("/var/run/docker.sock:")
                for entry in svc.get("volumes", []))
            assert has_sock == (name == "executor"), name
        sock_mount = _mounts(services["executor"])["/var/run/docker.sock"]
        assert sock_mount == "/var/run/docker.sock"
        # sock 挂载不能 :ro——unix socket 连接需要写权限
        assert not any(
            e.startswith("/var/run/docker.sock:") and e.endswith(":ro")
            for e in services["executor"]["volumes"])

    def test_shares_uploads_volume_with_worker(self, services):
        """与 api/worker 挂同一 uploads 卷（同一 env 源）：工作区
        stdout/stderr 由 executor 监督线程落盘到该卷"""
        ex_m = _mounts(services["executor"])
        assert ex_m["/app/uploads"] == _mounts(services["worker"])["/app/uploads"]
        assert "${UPLOADS_VOLUME:-./uploads}" in ex_m["/app/uploads"]

    def test_token_via_interpolation_without_env_file(self, services):
        """凭证边界：不挂 env_file（DATABASE_URL/Redis 等平台凭证不进容器），
        EXECUTOR_TOKEN 仅经插值注入；未配置 = fail-closed（executor/app.py）"""
        assert "env_file" not in services["executor"]
        env = _env_map(services["executor"])
        assert env.get("EXECUTOR_TOKEN") == "${EXECUTOR_TOKEN:-}"

    def test_docker_group_gid_supplementary_group(self, services):
        """非 root 访问 sock：部署机 docker 组 gid 经 .env 插值为补充组
        （镜像 USER 10002 不变；群晖等自定 gid 部署机必配 DOCKER_GROUP_GID）"""
        group_add = services["executor"].get("group_add", [])
        assert "${DOCKER_GROUP_GID:-999}" in group_add

    def test_hardened_like_api_worker(self, services):
        """与 api/worker 同口径加固（ARCH-0-1 模式）：cap_drop ALL +
        no-new-privileges + read_only + tmpfs（docker CLI 经 sock 通信不需要
        任何 capability）"""
        ex = services["executor"]
        assert [c.upper() for c in ex.get("cap_drop", [])] == ["ALL"]
        assert "no-new-privileges:true" in ex.get("security_opt", [])
        assert ex.get("read_only") is True
        assert any(e.startswith("/tmp:") for e in ex.get("tmpfs", []))
        assert not ex.get("cap_add") and not ex.get("privileged")

    def test_healthcheck_hits_local_health_route(self, services):
        """健康探活：镜像内 python 拨 127.0.0.1:8300/health（executor/app.py
        无鉴权探针；端口与 Dockerfile.executor EXPOSE/CMD 一致）"""
        test = services["executor"]["healthcheck"]["test"]
        joined = " ".join(test)
        assert "urllib.request" in joined
        assert "http://127.0.0.1:8300/health" in joined

    def test_waits_for_volume_init(self, services):
        """工作区日志写 uploads 卷：等属主迁移到 10002 完成再起（同 api/worker）"""
        deps = services["executor"].get("depends_on")
        assert isinstance(deps, dict)
        assert deps.get("volume-init", {}).get("condition") == "service_completed_successfully"

    def test_build_uses_executor_dockerfile(self, services):
        """镜像来自 docker/Dockerfile.executor（ARCH-1-1），tag 可经
        EXECUTOR_IMAGE_TAG 覆盖（CI 回滚口径与 IMAGE_TAG 一致）"""
        assert services["executor"]["build"]["dockerfile"] == "docker/Dockerfile.executor"
        assert services["executor"]["image"].startswith("ai-evo-executor:${EXECUTOR_IMAGE_TAG:")

    def test_on_internal_compose_network_only(self, services, compose):
        assert services["executor"].get("networks") == ["ai-evo-net"]


# ======================================================================
# 2. egress 出口代理
# ======================================================================

class TestEgressProxy:
    def test_gated_by_sandbox_profile_official_squid_image(self, services):
        assert SANDBOX_PROFILE in services["egress"].get("profiles", [])
        # ubuntu/squid 为 Canonical 官方维护、内网镜像源可拉（squid:alpine 非
        # 官方且内网源白名单不含——等价选型）；镜像可经 EGRESS_IMAGE 换源
        assert services["egress"]["image"] == "${EGRESS_IMAGE:-ubuntu/squid:latest}"

    def test_no_host_ports(self, services):
        assert "ports" not in services["egress"]

    def test_policy_and_whitelist_mounted_readonly_from_docker_egress(self, services):
        """白名单配置挂载 docker/egress/（策略 + 域名清单模板），两者只读；
        清单源含 ${EGRESS_WHITELIST_FILE:-…}——部署侧整体替换、不改模板"""
        for entry in services["egress"]["volumes"]:
            assert entry.endswith(":ro"), entry
        m = _mounts(services["egress"])
        assert m["/etc/squid/squid.conf"] == "${EGRESS_SQUID_CONF:-./docker/egress/squid.conf}"
        assert m["/etc/squid/whitelist.txt"] == \
            "${EGRESS_WHITELIST_FILE:-./docker/egress/whitelist.txt}"
        # 挂载源指向的模板文件必须真实存在（否则起容器时 docker 落目录）
        for source in ("docker/egress/squid.conf", "docker/egress/whitelist.txt"):
            assert (REPO_ROOT / source).is_file(), source

    def test_dual_network_worker_serving_and_sandbox_egress(self, services):
        """双网络：ai-evo-net 服务 worker 外呼与自身出网；aievo-sandbox-egress
        服务沙箱 egress 档"""
        assert set(services["egress"].get("networks", [])) == {"ai-evo-net", "aievo-sandbox-egress"}

    def test_healthcheck_squid_k_check(self, services):
        """镜像内无 curl/python：squid 自带进程状态检查（/run/squid.pid 信号 0，
        ARCH-1-3 本机冒烟验证过退出码 0 与 healthy 迁移）"""
        test = services["egress"]["healthcheck"]["test"]
        assert test == ["CMD", "squid", "-k", "check"]

    def test_no_credentials_no_data_volumes(self, services):
        """第三方官方镜像、无凭证无数据卷：不挂 env_file，卷只有两份只读配置"""
        assert "env_file" not in services["egress"]
        assert len(_mounts(services["egress"])) == 2


# ======================================================================
# 3. 沙箱 egress 网络（与 NET_PROFILES 单一事实源同步）
# ======================================================================

class TestSandboxEgressNetwork:
    def test_network_name_pinned_to_net_profiles(self, compose):
        """网络名逐字等于 executor/sandbox_params.NET_PROFILES 的 egress 档：
        executor 用宿主侧 docker CLI 裸网络名起沙箱容器，compose 默认的项目
        前缀（<project>_xxx）必须用 name: 钉死——两侧漂移 = egress 档 404 网络"""
        from executor.sandbox_params import NET_PROFILES

        expected = NET_PROFILES["egress"]
        net = compose["networks"].get(expected)
        assert net is not None, f"compose 缺网络 {expected}"
        assert net.get("name") == expected, "必须 name: 钉死，防 compose 项目前缀"

    def test_internal_no_direct_route(self, compose):
        """internal：沙箱无直连出网网关/NAT，唯一出口 = 同网段的 egress 代理
        （白名单 + 审计在网络层不可绕过——设计 D2）"""
        net = compose["networks"]["aievo-sandbox-egress"]
        assert net.get("internal") is True
        assert net.get("driver") == "bridge"

    def test_net_profiles_currently_none_and_egress_only(self):
        """档位单一事实源现状：none|egress（open 档设计 D2 规划、Phase 1 未
        开放）——本测试钉住档位面，扩档必须同步 compose 与部署文档"""
        from executor.sandbox_params import NET_PROFILES

        assert set(NET_PROFILES) == {"none", "egress"}
        assert NET_PROFILES["none"] == "none"


# ======================================================================
# 4. worker 侧 proxy 接线（只改 compose 不改代码）
# ======================================================================

class TestWorkerProxyWiring:
    def test_proxy_env_upper_and_lower_defaults_to_empty(self, services):
        """灰度开关：大小写各一份（客户端读取口径不一），缺省空 = 不走 proxy
        （urllib/getproxies 跳过空值，与既有部署行为一致）"""
        env = _env_map(services["worker"])
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert env.get(key) == "${WORKER_EGRESS_PROXY:-}", key
        for key in ("NO_PROXY", "no_proxy"):
            assert key in env, key

    def test_no_proxy_covers_internal_names(self, services):
        """内网目标不走 proxy：compose 服务名 + 回环，防 worker→api/redis 等
        内呼被 proxy 截胡"""
        env = _env_map(services["worker"])
        default = env["NO_PROXY"].partition(":-")[2].rstrip("}")
        covered = {h.strip() for h in default.split(",")}
        assert {"localhost", "127.0.0.1", "::1", "api", "redis", "postgres",
                "executor", "egress"} <= covered

    def test_worker_never_mounts_docker_sock(self, services):
        """sock 只给 executor 一个角色（在 executor 用例断言全集，这里点名
        worker——被攻破面最大的进程绝不直连宿主 daemon）"""
        assert not any("/var/run/docker.sock" in v for v in services["worker"].get("volumes", []))


# ======================================================================
# 5. squid 策略语义（SSRF 收口不可被顺序绕过）
# ======================================================================

@pytest.fixture(scope="module")
def squid_rules():
    """(http_access 规则序, 非注释非空行全文)——squid.conf 首条命中即停"""
    lines = [ln.strip() for ln in EGRESS_CONF_PATH.read_text(encoding="utf-8").splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("#")]
    # 续行合并（acl private_dst dst … \ 多段）
    merged, buf = [], ""
    for ln in body:
        ln = f"{buf} {ln}" if buf else ln
        if ln.endswith("\\"):
            buf = ln[:-1].rstrip()
        else:
            merged.append(ln)
            buf = ""
    assert not buf, "squid.conf 未闭合的续行"
    access = [ln for ln in merged if ln.startswith("http_access")]
    return access, merged


class TestSquidPolicy:
    def test_deny_private_dst_precedes_allow_whitelist(self, squid_rules):
        """SSRF 收口顺序语义：内网/回回/链路本地目的地址的 deny 必须先于
        allow whitelist——白名单域名解析到内网段也拒绝"""
        access, _ = squid_rules
        deny_idx = next(i for i, r in enumerate(access) if "deny private_dst" in r)
        allow_idx = next(i for i, r in enumerate(access) if "allow whitelist" in r)
        assert deny_idx < allow_idx
        # private_dst 覆盖的网段集合（防删段绕过：逐网段点名）
        conf = EGRESS_CONF_PATH.read_text(encoding="utf-8")
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                     "169.254.0.0/16", "fc00::/7", "fe80::/10"):
            assert cidr in conf, f"private_dst 缺内网段 {cidr}"

    def test_default_deny_and_connect_only_443(self, squid_rules):
        """默认拒绝（末条 deny all）+ CONNECT 隧道仅 443"""
        access, body = squid_rules
        assert access[-1] == "http_access deny all"
        assert any("deny CONNECT !SSL_ports" in r for r in access)
        assert "acl SSL_ports port 443" in body

    def test_whitelist_acl_points_to_mounted_file(self, squid_rules):
        """白名单 ACL 引用的容器内路径 = compose 挂载目标（模板/覆盖文件
        都落在同一路径，策略不用改）"""
        _, body = squid_rules
        assert 'acl whitelist dstdomain "/etc/squid/whitelist.txt"' in body

    def test_http_port_and_no_disk_cache(self, squid_rules):
        _, body = squid_rules
        assert "http_port 3128" in body  # 与 compose worker 接线 http://egress:3128 同源
        assert "cache deny all" in body  # 纯转发不落盘

    def test_whitelist_entries_are_valid_dstdomains(self):
        """清单全部条目为合法 dstdomain 模式（一行一域，前导点匹配子域），
        且非空模板——空清单 = 全拒绝，属部署事故面，模板至少带示例域"""
        import re

        entries = [
            ln.strip() for ln in EGRESS_WHITELIST_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert entries, "白名单模板不能为空"
        pattern = re.compile(r"^\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                             r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")
        for e in entries:
            assert pattern.match(e), f"非法 dstdomain 条目: {e!r}"


# ======================================================================
# 6. 部署接线文档门禁
# ======================================================================

class TestDeploymentDocs:
    def test_env_example_covers_new_switches(self):
        text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        for key in ("EXECUTOR_TOKEN=", "EXECUTOR_URL", "DOCKER_GROUP_GID",
                    "EXECUTOR_IMAGE_TAG", "EGRESS_IMAGE", "EGRESS_WHITELIST_FILE",
                    "EGRESS_SQUID_CONF", "WORKER_EGRESS_PROXY", "WORKER_NO_PROXY",
                    "COMPOSE_PROFILES=sandbox"):
            assert key in text, f".env.example 缺 {key}"

    def test_build_doc_documents_net_profile_mapping_and_enablement(self):
        """完成标准：与 1-2 的 SCRIPT_NET_PROFILE 档位对应关系写进部署文档
        （docs/build-pipeline.md 第十节：档位表 + 启用步骤 + SSRF 回归口径）。
        档位取值面与 ARCH-1-2 落地的 _VALID_NET_PROFILES 逐字对齐（防文档漂移）"""
        from app.services.script_container import _VALID_NET_PROFILES

        text = BUILD_DOC_PATH.read_text(encoding="utf-8")
        for needle in ("SCRIPT_NET_PROFILE", "NET_PROFILES", "COMPOSE_PROFILES=sandbox",
                       "aievo-sandbox-egress", "DOCKER_GROUP_GID", "WORKER_EGRESS_PROXY",
                       "EXECUTOR_URL", "ARCH-1-3",
                       *tuple(f"`{p}`（" for p in _VALID_NET_PROFILES)):
            assert needle in text, f"build-pipeline.md 缺 {needle}"
