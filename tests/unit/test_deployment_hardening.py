"""ARCH-0-1 主镜像去 root 与 compose 安全加固——静态门禁（L1，无 docker 依赖）

设计依据：plans/2026-10-15-企业级评测系统架构设计.md §2.3 决策 D3 与 §3 Phase 0
表 0-1 行（验收：两个主镜像均含非 root USER 指令；compose 加固字段齐）。

覆盖：
  1. Dockerfile / Dockerfile.online：创建 uid/gid 10002 的 app 用户；最终 USER 非
     root 且其后无 RUN/COPY/ADD（安装全在 root 阶段完成）；HOME 钉 /home/app；
     Claude CLI 不落 /root（/root 为 0700，非 root 读不到也执行不了）；uploads/logs
     写面预建并 chown app，而 /app 代码保持 root 属主（防篡改，read_only 下无写需求）。
  2. docker-compose.yml：api/worker/worker-ingest/hub-plugin 自有服务 cap_drop [ALL] +
     no-new-privileges；api/worker/worker-ingest read_only + tmpfs（/tmp、/home/app 且
     uid=10002）；
     hub-plugin 按设计 D3 保持 host 网络 sidecar 形态、不加 read_only（注释给理由）；
     volume-init 一次性属主迁移服务（root、仅 CHOWN/DAC_OVERRIDE、只读根、无网络、
     不挂 .env、与 api/worker 挂同一组卷），api/worker depends_on 其成功完成；
     全文件无 privileged。
  3. docker-entrypoint.sh：bash 语法、volume-init/api/worker/hub-plugin 分支接线
     （api/worker 在 exec 前做卷可写预检）、未知 SERVICE_TYPE 退出 1；预检与迁移
     函数以真实函数体在临时目录上执行（非 root 下 chown 必失败 → 验证「告警不
     阻断启动」路径），不依赖 docker。
  4. uid 单一事实源：Dockerfile useradd、compose tmpfs uid、entrypoint APP_UID
     三处同为 10002，且与沙箱 runner 用户（10001）分离。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit  # W8 分类门禁：纯静态断言 + 本地 shell，无 docker/网络/DB

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
ENTRYPOINT_PATH = REPO_ROOT / "docker-entrypoint.sh"

# 设计 D3 钉死的主镜像运行用户；与 script-runner 沙箱用户（10001）分离
APP_UID = 10002
DOCKERFILES = ["Dockerfile", "Dockerfile.online"]
# ARCH-2-1：worker 拆分后 worker（eval 队列）与 worker-ingest 同口径加固
HARDENED_SERVICES = ("api", "worker", "worker-ingest", "hub-plugin")
READ_ONLY_SERVICES = ("api", "worker", "worker-ingest")


# ======================================================================
# 1. Dockerfile：非 root 用户 + 写面收口
# ======================================================================

def parse_dockerfile(path: Path):
    """解析为 [(INSTRUCTION, args)]：去注释/空行，合并反斜杠续行"""
    instructions = []
    buf = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue  # 续行中的注释/空行同样丢弃（与 dockerfile 前端行为一致）
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        head, _, rest = buf.partition(" ")
        instructions.append((head.upper(), rest.strip()))
        buf = ""
    assert not buf, f"{path.name}: 未闭合的续行"
    return instructions


@pytest.fixture(params=DOCKERFILES)
def dockerfile(request):
    name = request.param
    return name, parse_dockerfile(REPO_ROOT / name)


class TestDockerfileNonRoot:
    def test_final_user_is_non_root_app(self, dockerfile):
        """验收主断言：USER 指令存在，最终运行身份非 root 且为 app"""
        name, ins = dockerfile
        users = [args for op, args in ins if op == "USER"]
        assert users, f"{name} 缺 USER 指令（镜像仍以 root 运行）"
        final = users[-1].split(":")[0].strip()
        assert final == "app", (name, final)

    def test_app_user_created_with_uid_gid_10002(self, dockerfile):
        """构建期预建 uid/gid 10002 的 app 用户（设计 D3 钉死的编号）"""
        name, ins = dockerfile
        runs = " ; ".join(args for op, args in ins if op == "RUN")
        assert re.search(r"groupadd\s+-g\s+10002\s+app\b", runs), name
        m = re.search(r"useradd\s+([^;&]*)", runs)
        assert m, f"{name}: 缺 useradd"
        tokens = m.group(1).split()
        assert tokens[tokens.index("-u") + 1] == "10002", (name, tokens)
        assert tokens[tokens.index("-g") + 1] == "10002", (name, tokens)
        assert tokens[-1] == "app", (name, tokens)

    def test_no_install_steps_after_user_switch(self, dockerfile):
        """USER 切换后不得再有 RUN/COPY/ADD：安装/复制全部以 root 完成，身份切换一次到位"""
        name, ins = dockerfile
        last_from = max(i for i, (op, _) in enumerate(ins) if op == "FROM")
        stage = ins[last_from:]  # 多阶段构建只看最终运行阶段
        user_idxs = [i for i, (op, _) in enumerate(stage) if op == "USER"]
        assert user_idxs, f"{name}: 最终阶段缺 USER"
        after = [op for op, _ in stage[user_idxs[-1] + 1:]]
        assert not ({"RUN", "COPY", "ADD"} & set(after)), (name, after)

    def test_home_env_pinned_to_app_home(self, dockerfile):
        """HOME 显式钉到 /home/app（compose 以 tmpfs 挂载，Claude Code CLI 的
        ~/.claude 配置与会话落在这里；不显式设则数字 USER 下 HOME 可能解析为 /）"""
        name, ins = dockerfile
        envs = " ".join(args for op, args in ins if op == "ENV")
        assert "HOME=/home/app" in envs.split(), (name, envs)

    def test_claude_cli_relocated_out_of_root_home(self, dockerfile):
        """/root 为 0700：非 root 运行后 /root/.local 下的 CLI 读不到也执行不了
        （claude-agent-sdk 的 which("claude") 会因此找不到 CLI）"""
        name, ins = dockerfile
        text = " ; ".join(f"{op} {args}" for op, args in ins if op in ("COPY", "RUN", "ENV"))
        assert "/root/.local" not in text, f"{name}: Claude CLI 仍落 /root"
        assert re.search(r"COPY\s+claude-local[*/]?\s+/opt/claude/", text), name  # 离线 claude-local/ 与在线通配 claude-local* 两种写法
        assert "ln -sf /opt/claude/bin/claude /usr/local/bin/claude" in text, name

    def test_writable_dirs_prebuilt_and_code_stays_root_owned(self, dockerfile):
        """写面收口：仅 uploads/logs 预建并 chown app（命名卷首次创建按此初始化属主）；
        /app 代码不整体 chown——运行用户只读，防篡改"""
        name, ins = dockerfile
        runs = " ; ".join(args for op, args in ins if op == "RUN")
        assert "mkdir -p /app/uploads /app/logs" in runs, name
        assert "chown app:app /app/uploads /app/logs" in runs, name
        assert not re.search(r"chown\s+(-R\s+)?app:app\s+/app\s*$", runs, re.M), \
            f"{name}: /app 整树 chown 会让运行用户可改写自身代码"
        for op, args in ins:
            if op == "COPY":
                assert "--chown" not in args, (name, args)

    def test_entrypoint_copied_and_installed(self, dockerfile):
        """入口脚本仍被复制并 chmod +x（加固不得破坏既有入口装配）"""
        name, ins = dockerfile
        copies = " ; ".join(args for op, args in ins if op == "COPY")
        assert "docker-entrypoint.sh /usr/local/bin/" in copies, name
        runs = " ; ".join(args for op, args in ins if op == "RUN")
        assert "chmod +x /usr/local/bin/docker-entrypoint.sh" in runs, name


# ======================================================================
# 2. docker-compose.yml：三服务加固 + volume-init 属主迁移
# ======================================================================

@pytest.fixture(scope="module")
def services():
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert set(data["services"]) >= {*HARDENED_SERVICES, "volume-init"}, data["services"].keys()
    return data["services"]


def _mounts(svc) -> dict:
    """volumes 列表 → {容器路径: 源}。源可能含 ${VAR:-default}，必须 rsplit 取目标"""
    return {entry.rsplit(":", 1)[1]: entry.rsplit(":", 1)[0] for entry in svc.get("volumes", [])}


class TestComposeServiceHardening:
    @pytest.mark.parametrize("name", HARDENED_SERVICES)
    def test_cap_drop_all(self, services, name):
        assert [c.upper() for c in services[name].get("cap_drop", [])] == ["ALL"], name

    @pytest.mark.parametrize("name", HARDENED_SERVICES)
    def test_no_new_privileges(self, services, name):
        assert "no-new-privileges:true" in services[name].get("security_opt", []), name

    @pytest.mark.parametrize("name", HARDENED_SERVICES)
    def test_no_cap_add_no_privileged(self, services, name):
        assert not services[name].get("cap_add"), name
        assert not services[name].get("privileged"), name

    def test_no_privileged_anywhere(self, services):
        for name, svc in services.items():
            assert not svc.get("privileged"), name

    @pytest.mark.parametrize("name", READ_ONLY_SERVICES)
    def test_read_only_rootfs_with_tmpfs(self, services, name):
        """api/worker 根文件系统只读；写面仅 uploads/logs 两个卷 + /tmp、/home/app
        两个 tmpfs（HOME 归 10002，与镜像 USER 一致）；tmpfs 有上限、nosuid"""
        svc = services[name]
        assert svc.get("read_only") is True, name
        tmpfs = {}
        for entry in svc.get("tmpfs", []):
            target, _, opts = entry.partition(":")
            tmpfs[target] = opts
        assert {"/tmp", "/home/app"} <= set(tmpfs), (name, tmpfs)
        home_opts = tmpfs["/home/app"].split(",")
        assert "uid=10002" in home_opts and "gid=10002" in home_opts, (name, home_opts)
        for target, opts in tmpfs.items():
            parts = opts.split(",")
            assert "nosuid" in parts, (name, target)
            assert any(p.startswith("size=") for p in parts), (name, target)

    def test_hub_plugin_form_unchanged_and_reason_documented(self, services):
        """hub-plugin 按 D3 保持 host 网络 sidecar 形态：不挂 env_file；不加
        read_only 时 compose 注释必须给出理由（任务要求：注释说明理由）"""
        hp = services["hub-plugin"]
        assert hp.get("network_mode") == "host"
        assert "env_file" not in hp
        if not hp.get("read_only"):
            text = COMPOSE_PATH.read_text(encoding="utf-8")
            assert "不加 read_only+tmpfs" in text, "hub-plugin 未加 read_only，但未在注释中说明理由"


class TestVolumeInitOwnershipMigration:
    def test_root_but_least_privilege(self, services):
        """迁移必须 root（非 root chown 不了），但只保留 CHOWN/DAC_OVERRIDE，其余全禁"""
        vi = services["volume-init"]
        assert str(vi.get("user")) == "0:0"
        assert [c.upper() for c in vi["cap_drop"]] == ["ALL"]
        assert sorted(c.upper() for c in vi["cap_add"]) == ["CHOWN", "DAC_OVERRIDE"]
        assert "no-new-privileges:true" in vi.get("security_opt", [])
        assert vi.get("read_only") is True
        assert vi.get("network_mode") == "none"
        assert vi.get("restart") == "no"  # 一次性：跑完退出，不随 daemon 重启拉起
        assert "env_file" not in vi  # .env 凭证不进迁移容器
        assert "SERVICE_TYPE=volume-init" in vi["environment"]

    def test_mounts_exactly_the_shared_data_volumes(self, services):
        """迁移容器挂的必须与 api/worker 完全同一组卷（同一 env 源），漏一个就有属主死角
        （ARCH-2-1：worker-ingest 同样读写 uploads/logs，一并纳入等值断言）"""
        names = ("api", "worker", "worker-ingest", "volume-init")
        mounts = {n: _mounts(services[n]) for n in names}
        vi_m = mounts["volume-init"]
        assert set(vi_m) == {"/app/uploads", "/app/logs"}
        for target in vi_m:
            assert len({mounts[n][target] for n in names}) == 1, target

    @pytest.mark.parametrize("name", READ_ONLY_SERVICES)
    def test_api_worker_wait_for_volume_init(self, services, name):
        """api/worker 必须等迁移服务成功退出再起（long-form depends_on 才能表达条件）"""
        deps = services[name].get("depends_on")
        assert isinstance(deps, dict), (name, "depends_on 需 long form")
        assert deps.get("volume-init", {}).get("condition") == "service_completed_successfully", name
        assert "redis" in deps, (name, "原有 redis 依赖不得丢失")


# ======================================================================
# 3. docker-entrypoint.sh：分支接线 + 真实函数体执行
# ======================================================================

@pytest.fixture(scope="module")
def entrypoint_text():
    return ENTRYPOINT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bash():
    found = shutil.which("bash")
    if not found:
        pytest.skip("环境无 bash，无法校验入口脚本")
    return found


class TestEntrypointWiring:
    def test_bash_syntax_ok(self, bash):
        r = subprocess.run([bash, "-n", str(ENTRYPOINT_PATH)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_case_branches(self, entrypoint_text):
        """volume-init 分支存在；api/worker 在 exec 前调用卷可写预检；hub-plugin 不预检"""
        def branch(name):
            m = re.search(rf"^\s*{re.escape(name)}\)\n(.*?)^\s*;;", entrypoint_text, re.M | re.S)
            assert m, f"缺 {name} 分支"
            return m.group(1)

        assert "migrate_volume_ownership" in branch("volume-init")
        for name in ("api", "worker"):
            body = branch(name)
            assert "require_writable_data_dirs" in body, name
            assert body.index("require_writable_data_dirs") < body.index("exec "), name
        assert "require_writable_data_dirs" not in branch("hub-plugin")

    def test_data_dirs_and_uid_constants(self, entrypoint_text):
        """预检/迁移目标是 uploads、logs 两个挂卷点；uid 常量与镜像一致"""
        assert 'DATA_DIRS="/app/uploads /app/logs"' in entrypoint_text
        assert "APP_UID=10002" in entrypoint_text and "APP_GID=10002" in entrypoint_text

    def test_unknown_service_type_exits_1(self, bash):
        r = subprocess.run(
            [bash, str(ENTRYPOINT_PATH)],
            env={"SERVICE_TYPE": "bogus", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "Unknown SERVICE_TYPE: bogus" in r.stdout
        assert "volume-init" in r.stdout  # usage 列出新分支


def _extract_function(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}", text, re.M | re.S)
    assert m, f"未找到函数 {name}"
    return m.group(0)


def _run_function(text: str, name: str, data_dirs: str, app_uid: int = APP_UID):
    """以真实函数体 + 与生产一致的 set -e / 常量跑一遍（bash，无 docker）。
    app_uid 可换成当前 uid，用来在非 root 下复现「属主已匹配」的快路径。"""
    script = "\n".join([
        "set -e",
        f"APP_UID={app_uid}",
        f"APP_GID={app_uid}",
        f'DATA_DIRS="{data_dirs}"',
        _extract_function(text, name),
        name,
    ])
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


class TestEntrypointPreflightFunction:
    def test_all_writable_passes_silently(self, tmp_path, entrypoint_text):
        a, b = tmp_path / "uploads", tmp_path / "logs"
        a.mkdir()
        b.mkdir()
        r = _run_function(entrypoint_text, "require_writable_data_dirs", f"{a} {b}")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert r.stderr == ""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root 对任何目录皆可写，无法构造不可写样本")
    def test_unwritable_dir_fails_fast_with_fix_hint(self, tmp_path, entrypoint_text):
        ro, rw = tmp_path / "ro-uploads", tmp_path / "rw-logs"
        ro.mkdir()
        rw.mkdir()
        ro.chmod(0o555)
        try:
            r = _run_function(entrypoint_text, "require_writable_data_dirs", f"{ro} {rw}")
        finally:
            ro.chmod(0o755)  # 交还写权限，tmp_path 清理不报错
        assert r.returncode == 1
        first_line = r.stderr.splitlines()[0]
        assert str(ro) in first_line and "不可写" in first_line
        assert str(rw) not in first_line  # 只点名不可写的目录
        assert "chown -R 10002:10002" in r.stderr  # 修复指引含精确命令

    def test_missing_dir_counts_as_unwritable(self, tmp_path, entrypoint_text):
        missing = tmp_path / "no-such-volume"
        r = _run_function(entrypoint_text, "require_writable_data_dirs", f"{missing}")
        assert r.returncode == 1
        assert str(missing) in r.stderr


class TestEntrypointMigrationFunction:
    @pytest.mark.skipif(os.geteuid() == 0, reason="root 下 chown 会真成功；本用例专验非 root 的告警不阻断路径")
    def test_non_root_chown_failure_warns_but_never_blocks(self, tmp_path, entrypoint_text):
        d = tmp_path / "uploads"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
        missing = tmp_path / "logs-not-mounted"
        r = _run_function(entrypoint_text, "migrate_volume_ownership", f"{d} {missing}")
        assert r.returncode == 0  # 迁移是 best-effort：失败只告警，不阻断主服务启动
        assert "非 root" in r.stderr
        assert "chown 失败" in r.stderr
        assert f"跳过 {missing}" in r.stdout

    def test_already_owned_dirs_take_fast_path_without_chown(self, tmp_path, entrypoint_text):
        """幂等快路径：目录树里没有任何非目标 uid 的条目 → 不碰 chown、打印无需迁移、退出 0
        （把目标 uid 换成当前用户即可在非 root 下复现；生产里对应「每次 compose up
        都会跑 volume-init，但已迁移过的卷只做只读遍历」）"""
        d = tmp_path / "uploads"
        d.mkdir()
        (d / "sub").mkdir()
        (d / "sub" / "f.txt").write_text("x", encoding="utf-8")
        r = _run_function(entrypoint_text, "migrate_volume_ownership", f"{d}", app_uid=os.getuid())
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert f"{d} 属主已是 {os.getuid()}，无需迁移" in r.stdout
        assert "递归修正属主" not in r.stdout
        assert "chown 失败" not in r.stderr


# ======================================================================
# 4. uid 单一事实源：镜像 / compose / entrypoint / 沙箱分离
# ======================================================================

class TestUidSingleSourceOfTruth:
    def test_compose_tmpfs_uid_matches_dockerfile_uid(self, services):
        for name in READ_ONLY_SERVICES:
            for entry in services[name].get("tmpfs", []):
                if entry.startswith("/home/app"):
                    assert "uid=10002" in entry, (name, entry)
                    break

    def test_sandbox_runner_uid_stays_separate(self):
        """主进程 10002 与沙箱 runner 10001 分离：主进程被攻破也拿不到沙箱身份
        （设计 D3；默认值来自 Settings，单一事实源不重复硬编码第二份清单）"""
        from app.core.config import Settings

        runner_uid = Settings.model_fields["SCRIPT_RUNNER_USER"].default.split(":")[0]
        assert runner_uid == "10001"
        assert runner_uid != str(APP_UID)
