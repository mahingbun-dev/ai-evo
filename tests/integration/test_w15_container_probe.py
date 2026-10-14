"""W15 容器内脚本探针（L2 集成，docker 实测）

整改计划 §4 W15 验收项：「容器内脚本探针（读 env/读宿主路径/forkbomb 限制）
全部受限」。本机 docker daemon 可用时真跑（走 execute_script 完整链路：
工作区 staging、路径重写、docker run --rm 隔离清单全量生效）；docker 不可用
或 runner 镜像未构建时明确 skip（集成纪律：跑不了明确 skip，不许假绿）——
命令构造逻辑的离线覆盖见 tests/unit/test_w15_container_isolation.py。

探针矩阵（每条断言对应 W15 隔离边界的一项）：
- env 受限：平台敏感 env（DATABASE_URL/REDIS_URL/SSO/LANGFUSE）不可见，
  键集 ⊆ 注入白名单（剥离平台 env）
- 非 root：uid == 10001
- 平台代码不可 import：无 PYTHONPATH 注入 → import app 失败
- 宿主文件系统不可见：宿主项目根、/app（平台镜像路径）均不存在；
  容器内只有 /workspace（挂载仅本次 script_runs 工作目录）
- 工作区 bind mount 可写：脚本写 {run_dir}/outputs/ → 宿主侧可见
- 资源限制生效：cgroup v2 memory.max==1g、cpu.max==0.5、pids.max==128
- forkbomb 受限：并发 fork 被 pids-limit 卡死（成功进程数 < 上限且 fork 报错）
- 网络默认关：--network none 下外连必败
"""
import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration  # W8 分类门禁：补打标

from app.services import pre_judge_runner, pre_judge_script_service

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_IMAGE = os.environ.get("W15_RUNNER_IMAGE", "ai-evo-script-runner:w15-test")

PROBE_SCRIPT_ID = "pjs_w15_probe"


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _image_present() -> bool:
    # docker 命令不存在（如 CI test-unit 容器）→ 视为未构建 → skip，
    # 不能让 FileNotFoundError 在模块收集期炸掉整个 -m unit 门禁
    try:
        return subprocess.run(["docker", "image", "inspect", RUNNER_IMAGE],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="本机 docker daemon 不可用（实机探针待办）")
requires_image = pytest.mark.skipif(
    not _image_present(), reason=f"runner 镜像 {RUNNER_IMAGE} 未构建"
                                "（先执行 docker/script-runner/README.md 的构建命令）")


# 容器内探针脚本：一次 run 收集全部探测面，结果经契约 JSON 返回平台侧断言
_PROBE_SRC = textwrap.dedent("""
    import json, os, socket, sys, time

    def _read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    def run(stdin):
        probe = {}
        # ① env 面
        probe["env_keys"] = sorted(os.environ.keys())
        # ② 非 root
        probe["uid"] = os.getuid()
        # ③ 平台代码不可 import（无 PYTHONPATH、镜像内无平台源码）
        try:
            import app  # noqa: F401
            probe["import_app"] = "imported"
        except Exception as e:
            probe["import_app"] = type(e).__name__
        # ④ 宿主路径不可见 + 容器内布局
        probe["host_repo_exists"] = os.path.exists(stdin["probe_host_repo"])
        probe["slash_app_exists"] = os.path.exists("/app")
        probe["workspace_exists"] = os.path.exists("/workspace")
        probe["cwd"] = os.getcwd()
        # ⑤ 工作区 bind mount 可写（写 outputs，平台侧回读）
        try:
            out = os.path.join(stdin["_run_dir"], "outputs", "probe.txt")
            with open(out, "w") as f:
                f.write("w15-probe")
            probe["workspace_write"] = "ok"
        except OSError as e:
            probe["workspace_write"] = f"fail: {e}"
        # ⑥ cgroup v2 资源限制
        probe["memory_max"] = _read("/sys/fs/cgroup/memory.max")
        probe["cpu_max"] = _read("/sys/fs/cgroup/cpu.max")
        probe["pids_max"] = _read("/sys/fs/cgroup/pids.max")
        # ⑦ forkbomb 受限：并发持有子进程直到 fork 失败（子进程 2 秒自尽）
        forked, fork_failed = 0, None
        pids = []
        for _ in range(300):
            try:
                pid = os.fork()
            except OSError as e:
                fork_failed = type(e).__name__
                break
            if pid == 0:
                time.sleep(2)
                os._exit(0)
            forked += 1
            pids.append(pid)
        time.sleep(3)  # 等子进程自尽后收尸
        for pid in pids:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        probe["fork_success"] = forked
        probe["fork_failed_with"] = fork_failed
        # ⑧ 网络默认关（--network none）：外连必败
        try:
            socket.create_connection(("1.1.1.1", 80), timeout=3).close()
            probe["egress"] = "connected"
        except OSError as e:
            probe["egress"] = type(e).__name__
        return {"status": "ok", "output": probe, "summary": "w15 probe done"}
""")


@pytest.fixture(scope="module")
def probe_env(tmp_path_factory):
    """container 模式全链路环境（module 作用域：探针脚本只执行一次供全部断言复用）

    手动 patch 环境变量与模块常量（module 作用域不能用 function 级 monkeypatch），
    finalizer 恢复原值。
    """
    tmp = tmp_path_factory.mktemp("w15_probe")
    upload_base = tmp / "pre_judge_scripts"
    pkg = upload_base / PROBE_SCRIPT_ID
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text(_PROBE_SRC, encoding="utf-8")

    saved_env = {k: os.environ.get(k) for k in ("SCRIPT_EXEC_MODE", "SCRIPT_RUNNER_IMAGE")}
    os.environ["SCRIPT_EXEC_MODE"] = "container"
    os.environ["SCRIPT_RUNNER_IMAGE"] = RUNNER_IMAGE
    saved_upload_base = pre_judge_script_service.SCRIPT_UPLOAD_BASE
    saved_run_base = pre_judge_runner.SCRIPT_RUN_BASE
    pre_judge_script_service.SCRIPT_UPLOAD_BASE = upload_base
    pre_judge_runner.SCRIPT_RUN_BASE = tmp / "script_runs"

    yield tmp

    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    pre_judge_script_service.SCRIPT_UPLOAD_BASE = saved_upload_base
    pre_judge_runner.SCRIPT_RUN_BASE = saved_run_base


@requires_docker
@requires_image
class TestContainerProbe:
    @pytest.fixture(scope="class")
    def probe_result(self, probe_env):
        """执行一次探针脚本（完整 execute_script 链路），返回 (结果, run_dir)"""
        run_dir = (probe_env / "script_runs" / "probe-task" / "probe-data").absolute()
        (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
        stdin = {
            "task_id": "probe-task", "data_id": "probe-data",
            "fields": {}, "inputs": {}, "params": {},
            "model_output": None, "session": None, "upstream": {},
            "probe_host_repo": str(REPO_ROOT),
            "_run_dir": str(run_dir),
        }
        script_def = {"script_id": PROBE_SCRIPT_ID, "name": PROBE_SCRIPT_ID,
                      "exec_type": "script", "entrypoint": "main.py", "timeout_seconds": 90}
        result = pre_judge_runner.execute_script(script_def, stdin, timeout_seconds=90)
        assert result["status"] == "ok", f"探针执行失败: {result.get('error')}"
        return result["output"], run_dir

    def test_env_is_restricted(self, probe_result):
        output, _ = probe_result
        # 平台敏感 env 全部不可见（剥离平台 env）
        for sensitive in ("DATABASE_URL", "REDIS_URL", "SSO_LOGIN_URL",
                          "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "DB_ASSERT_READONLY_DSN"):
            assert sensitive not in output["env_keys"], f"平台 env 泄漏进容器: {sensitive}"
        # 键集 ⊆ 注入白名单 ∪ python:3.12-slim 基础镜像固有 ENV
        # （docker inspect 核实：PATH/LANG/GPG_KEY/PYTHON_VERSION/PYTHON_SHA256；
        #   HOSTNAME 由 docker 运行时注入）——无宿主 env 全量透传
        assert set(output["env_keys"]) <= (
            {"PATH", "HOME", "LANG", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
             "HOSTNAME", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}
        )

    def test_runs_as_non_root(self, probe_result):
        output, _ = probe_result
        assert output["uid"] == 10001

    def test_platform_code_not_importable(self, probe_result):
        output, _ = probe_result
        # container 模式移除 PYTHONPATH 注入：脚本无法 import 平台代码
        assert output["import_app"] in ("ModuleNotFoundError", "ImportError")

    def test_host_filesystem_not_visible(self, probe_result):
        output, _ = probe_result
        assert output["host_repo_exists"] is False, "宿主项目根在容器内可见"
        assert output["slash_app_exists"] is False, "平台镜像路径 /app 在 runner 容器内可见"
        assert output["workspace_exists"] is True
        assert output["cwd"].startswith("/workspace")

    def test_workspace_bind_mount_writable(self, probe_result, probe_env):
        output, run_dir = probe_result
        assert output["workspace_write"] == "ok"
        # 脚本在容器内写 /workspace/outputs → 宿主 run_dir/outputs 可见（唯一挂载面）
        assert (run_dir / "outputs" / "probe.txt").read_text() == "w15-probe"

    def test_resource_limits_active(self, probe_result):
        output, _ = probe_result
        assert output["memory_max"] == "1073741824", f"--memory 1g 未生效: {output['memory_max']}"
        assert output["cpu_max"] == "50000 100000", f"--cpus 0.5 未生效: {output['cpu_max']}"
        assert output["pids_max"] == "128", f"--pids-limit 128 未生效: {output['pids_max']}"

    def test_forkbomb_throttled(self, probe_result):
        output, _ = probe_result
        # forkbomb 被资源限制卡死：并发持有进程触顶 pids-limit 后 fork 报错，
        # 成功 fork 数显著低于 300 次上限（宿主不受影响——容器内 pids 名额耗尽即止）
        assert output["fork_failed_with"] is not None, "fork 未被限制（forkbomb 可畅跑）"
        assert output["fork_success"] < 150, f"成功 fork 数异常: {output['fork_success']}"

    def test_network_isolated_by_default(self, probe_result):
        output, _ = probe_result
        # 任务级网络开关默认关：--network none 下外连必败
        assert output["egress"] != "connected", "默认网络档位可出网（--network none 未生效）"

    def test_stdout_logs_written_on_host(self, probe_result):
        _, run_dir = probe_result
        # stdout/stderr 日志仍由宿主 Popen 重定向到工作区（与 local 同位）
        logs_dir = run_dir / PROBE_SCRIPT_ID
        assert (logs_dir / "stdout.log").is_file()
        assert (logs_dir / "stderr.log").is_file()


@requires_docker
@requires_image
def test_container_timeout_reclaims_container(probe_env):
    """超时容器回收：死循环脚本超时后 fail（finish_reason=timeout）且容器被清理"""
    upload_base = probe_env / "pre_judge_scripts"
    pkg = upload_base / "pjs_w15_sleeper"
    pkg.mkdir()
    (pkg / "main.py").write_text(
        "def run(stdin):\n"
        "    import time\n"
        "    time.sleep(120)\n"
        "    return {\"status\": \"ok\"}\n", encoding="utf-8")
    run_dir = (probe_env / "script_runs" / "sleeper-task" / "sleeper-data").absolute()
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    stdin = {"task_id": "sleeper-task", "data_id": "sleeper-data", "fields": {},
             "inputs": {}, "params": {}, "_run_dir": str(run_dir)}
    script_def = {"script_id": "pjs_w15_sleeper", "name": "pjs_w15_sleeper",
                  "exec_type": "script", "entrypoint": "main.py", "timeout_seconds": 5}
    started = time.time()
    result = pre_judge_runner.execute_script(script_def, stdin, timeout_seconds=5)
    elapsed = time.time() - started
    assert result["status"] == "fail"
    assert result["finish_reason"] == "timeout"
    # SIGTERM 宽限 5s 兜底，总回收时长受控（不会等满 120s 的死循环）
    assert elapsed < 30, f"超时回收耗时异常: {elapsed:.1f}s"
    # 容器名兜底清理后无 aievo-script 残留
    listing = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True).stdout
    assert "aievo-script" not in listing, f"超时容器残留: {listing}"


@requires_docker
@requires_image
def test_network_switch_both_positions(probe_env):
    """任务级网络开关双向实测：network=true（bridge）可出网、默认（none）不可

    宿主本身不可出网时明确 skip（不许假绿；bridge 档位出网验证待有网环境）。
    """
    import socket as _socket

    try:
        _socket.create_connection(("1.1.1.1", 80), timeout=5).close()
    except OSError:
        pytest.skip("宿主不可出网，bridge 档位出网能力无法验证（开关 none 档由探针用例覆盖）")

    pkg = probe_env / "pre_judge_scripts" / PROBE_SCRIPT_ID
    assert (pkg / "main.py").is_file()  # 复用探针脚本（含 egress 探测）
    run_dir = (probe_env / "script_runs" / "net-switch" / "net-data").absolute()
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    stdin = {"task_id": "net-switch", "data_id": "net-data", "fields": {},
             "inputs": {}, "params": {}, "probe_host_repo": str(REPO_ROOT),
             "_run_dir": str(run_dir)}
    script_def = {"script_id": PROBE_SCRIPT_ID, "name": PROBE_SCRIPT_ID,
                  "exec_type": "script", "entrypoint": "main.py", "timeout_seconds": 90}

    off = pre_judge_runner.execute_script(dict(script_def), dict(stdin), timeout_seconds=90)
    assert off["status"] == "ok", off.get("error")
    assert off["output"]["egress"] != "connected", "默认档（--network none）不应出网"

    on = pre_judge_runner.execute_script(dict(script_def), dict(stdin),
                                         timeout_seconds=90, network=True)
    assert on["status"] == "ok", on.get("error")
    assert on["output"]["egress"] == "connected", "network=true（bridge 档）应可出网"


@requires_docker
@requires_image
def test_container_smoke_via_validate_chain(probe_env):
    """container 模式契约冒烟实跑：上传校验链（含冒烟）走容器通道成功

    （独立测试角色补注：本用例经 probe_env 固定 SCRIPT_EXEC_MODE=container，
    docker/镜像不可用时 run_smoke_test 会抛 OSError——必须 skip 而非裸跑误报，
    补齐与其他用例一致的 skipif 守卫。）
    """
    pkg = probe_env / "pre_judge_scripts" / "pjs_w15_smoke"
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text(
        "def run(stdin):\n"
        "    return {\"status\": \"ok\", \"output\": {\"note\": stdin[\"fields\"][\"note\"]}}\n",
        encoding="utf-8")
    entry = pkg / "main.py"
    stdout = pre_judge_script_service.run_smoke_test(entry, timeout=60)
    assert stdout["status"] == "ok"
    assert stdout["output"]["note"].endswith(".txt")


@requires_docker
@requires_image
def test_template_script_runs_full_flow_in_container(probe_env, tmp_path, monkeypatch):
    """现有脚手架模板（hub_package）在 container 模式经完整编排链路跑通

    验收项「现有脚本模板在容器模式跑通」：hub_file 输入按严格拷贝语义进工作区
    （宿主路径），stdin 路径重写后模板在容器内从 /workspace/inputs/ 消费执行包
    （manifest 摘要 case_total=3 证明文件面全部可达），__main__ 传统式 +
    lib/ 兄弟模块 import 同样成立。
    """
    import shutil
    import zipfile
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base
    from app.models.pre_judge_models import PreJudgeScript

    pkg_src = tmp_path / "tpl"
    with zipfile.ZipFile(REPO_ROOT / "app" / "static" / "script_templates" / "hub_package.zip") as zf:
        zf.extractall(pkg_src)
    dest = probe_env / "pre_judge_scripts" / "pjs_w15_template"
    shutil.copytree(pkg_src / "scripts" / "hub_package", dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(PreJudgeScript(id="pjs_w15_template", name="pjs_w15_template",
                              exec_type="script", entrypoint="main.py",
                              timeout_seconds=120, is_active=True))
        db.commit()
    monkeypatch.setattr(pre_judge_runner, "SessionLocal", Session)

    task = SimpleNamespace(id="tpl-task", pre_scripts=[{
        "script_id": "pjs_w15_template",
        "input_mapping": {"exec_pkg": {"field": "exec_pkg", "value_type": "hub_file"}},
        "params": {}, "output_var": "tpl_out", "on_fail": "abort",
    }])
    result = pre_judge_runner.run_pre_scripts(
        task, dataset_item={"id": "tpl-data", "data": {"exec_pkg": str(pkg_src / "inputs")}})
    entry = result["pre_script_results"][0]
    assert entry["status"] == "ok", f"模板容器执行失败: {entry.get('error')}"
    output = entry["output"] or {}
    # manifest 摘要证明模板在容器内完整读取了工作区执行包（样例包 3 用例）
    assert output["manifest_summary"]["case_total"] == 3
    assert output["fields"]["exec_pkg"]["file_count"] == 33
