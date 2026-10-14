"""W15 容器化隔离——独立测试角色验证（L2 集成，docker 实测）

与开发交付的 tests/integration/test_w15_container_probe.py 互补：本文件只覆盖
开发探针未实测的面与独立角度，全部断言经本机 docker 实机跑通后落地：

1. env 剥离的独立角度：平台进程 os.environ 真的持有敏感值（monkeypatch 注入
   假 DSN/密钥）→ docker run 不透传 → 容器内不可见（开发探针只断言了容器内
   env 键集 ⊆ 白名单，未验证「宿主进程有值」场景）；
2. --read-only 实机验证：容器内写 /usr/bin、/etc、/opt/runner、/ 全部被拒
   （开发只做了命令构造断言，未实测根文件系统只读行为）；
3. tmpfs noexec 实机验证：/tmp 可写但不可 exec（直接 execve 被拒，非 sh 读
   解释——sh 读脚本属 read 不受 noexec 拦截）；
4. --memory 1g 实机 OOM：容器内申请 3g → cgroup OOM kill（退出码 137）→
   执行 fail，宿主进程存活不受影响（开发只读 cgroup 值未实测超限行为）；
5. --cpus 0.5 实机节流：多进程 busy loop 的 cpu/wall 比值 < 1.0（本机对照
   实测：无限制多进程比值 1.91，容器内 0.48；注意 threading 版受 GIL 影响
   无区分度——宿主/容器实测均 ~0.8x，故必须用多进程探针）；
6. 挂载面强断言：script_runs 下其他任务工作目录（sibling-task）容器内不可见，
   本次 run_dir 自身可见且 _run_dir 重写为 /workspace（开发探针只验证了宿主
   项目根不可见）；
7. 镜像实装集合 ⊇ RUNNER_IMAGE_PACKAGES：镜像内 pip freeze 对照静态清单
   （开发单测只对 packages.txt 文本做一致性门禁，未对实际构建产物验证——
   防「清单声称已装、镜像实际未装」的双向漂移中的单向）；
8. W1 行为保留回归：local 模式 build_script_env 仍注入 PYTHONPATH=项目根。

docker daemon 或 runner 镜像不可用时明确 skip（集成纪律：跑不了明确 skip，
不许假绿；skip 原因见各 skipif 的 reason）。
"""
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration  # W8 分类门禁：补打标

from app.services import pre_judge_runner, pre_judge_script_service, script_container

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_IMAGE = "ai-evo-script-runner:w15-test"


def _docker_available() -> bool:
    # skip 原因：实机探针需要 docker daemon；不可用时无法在本机验证隔离行为
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _image_present() -> bool:
    # skip 原因：探针跑在 ai-evo-script-runner 镜像内；未构建则先执行
    # docker/script-runner/README.md 的构建命令（离线预载属 D-3）。
    # docker 命令不存在（如 CI test-unit 容器）同样视为未构建 → skip——
    # 模块收集期抛 FileNotFoundError 会炸掉整个 -m unit 门禁（main 流水线实测）
    try:
        return subprocess.run(["docker", "image", "inspect", RUNNER_IMAGE],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="本机 docker daemon 不可用，容器隔离实机验证待办")
requires_image = pytest.mark.skipif(
    not _image_present(), reason=f"runner 镜像 {RUNNER_IMAGE} 未构建（见 docker/script-runner/README.md）")


@pytest.fixture
def container_env(tmp_path, monkeypatch):
    """container 模式全链路环境（function 级：每条探针独立工作区与配置）"""
    monkeypatch.setenv("SCRIPT_EXEC_MODE", "container")
    monkeypatch.setenv("SCRIPT_RUNNER_IMAGE", RUNNER_IMAGE)
    upload_base = tmp_path / "pre_judge_scripts"
    run_base = tmp_path / "script_runs"
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)
    return {"upload_base": upload_base, "run_base": run_base}


def _exec(container_env, sid, src, timeout=120, network=False):
    """写探针脚本包并经 execute_script 完整链路执行（staging+路径重写+docker run）"""
    pkg = container_env["upload_base"] / sid
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text(src, encoding="utf-8")
    task, data = f"task-{sid}", f"data-{sid}"
    run_dir = (container_env["run_base"] / task / data).absolute()
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    stdin = {"task_id": task, "data_id": data, "fields": {}, "inputs": {},
             "params": {}, "model_output": None, "session": None, "upstream": {},
             "_run_dir": str(run_dir)}
    script_def = {"script_id": sid, "name": sid, "exec_type": "script",
                  "entrypoint": "main.py", "timeout_seconds": timeout}
    result = pre_judge_runner.execute_script(script_def, stdin,
                                             timeout_seconds=timeout, network=network)
    return result, run_dir


@requires_docker
@requires_image
class TestIndependentContainerVerify:
    def test_verify_host_env_not_leaked(self, container_env, monkeypatch):
        """平台进程 env 真持有敏感值 → docker run 不透传 → 容器内不可见"""
        monkeypatch.setenv("DATABASE_URL", "postgresql://leak-user:leak-pass@leak-host/leak-db")
        monkeypatch.setenv("REDIS_URL", "redis://leak-host:6379/0")
        monkeypatch.setenv("AI_EVO_W15_SECRET", "supersecret-w15-leak")
        src = (
            "import json, os\n"
            "def run(stdin):\n"
            "    targets = ('DATABASE_URL', 'REDIS_URL', 'AI_EVO_W15_SECRET')\n"
            "    leaked = [k for k in targets if k in os.environ]\n"
            "    return {'status': 'ok', 'output': {'leaked': leaked}, 'summary': 'env probe'}\n"
        )
        result, _ = _exec(container_env, "pjs_w15v_env", src)
        assert result["status"] == "ok", result.get("error")
        assert result["output"]["leaked"] == [], (
            f"宿主进程持有的敏感 env 泄漏进容器: {result['output']['leaked']}")

    def test_verify_readonly_rootfs(self, container_env):
        """--read-only 实机：根文件系统全部写入点被拒；工作区（bind mount）可写为对照"""
        src = (
            "import json, os\n"
            "def _try(p):\n"
            "    try:\n"
            "        with open(p, 'w') as f: f.write('x')\n"
            "        return 'ok'\n"
            "    except OSError:\n"
            "        return 'blocked'\n"
            "def run(stdin):\n"
            "    r = {p: _try(p) for p in ('/usr/bin/evil', '/etc/evil',\n"
            "                              '/opt/runner/evil', '/evil')}\n"
            "    r['workspace_writable'] = _try(os.path.join(stdin['_run_dir'], 'outputs', 'rw.txt'))\n"
            "    return {'status': 'ok', 'output': r, 'summary': 'ro probe'}\n"
        )
        result, run_dir = _exec(container_env, "pjs_w15v_ro", src)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        for p in ("/usr/bin/evil", "/etc/evil", "/opt/runner/evil", "/evil"):
            assert out[p] == "blocked", f"容器内可写 {p}（--read-only 未生效）"
        assert out["workspace_writable"] == "ok", "工作区应可写（对照失败说明探针无效）"
        assert (run_dir / "outputs" / "rw.txt").read_text() == "x"

    def test_verify_tmpfs_noexec(self, container_env):
        """tmpfs noexec 实机：/tmp 可写（对照）但直接 execve 被拒"""
        src = (
            "import json, os, subprocess\n"
            "def run(stdin):\n"
            "    with open('/tmp/exec_me.sh', 'w') as f:\n"
            "        f.write('#!/bin/sh\\necho pwned\\n')\n"
            "    os.chmod('/tmp/exec_me.sh', 0o755)\n"
            "    try:\n"
            "        p = subprocess.run(['/tmp/exec_me.sh'], capture_output=True)\n"
            "        exec_result = 'ok' if p.returncode == 0 else 'blocked'\n"
            "    except (OSError, PermissionError):\n"
            "        exec_result = 'blocked'\n"
            "    return {'status': 'ok', 'output': {'exec': exec_result},\n"
            "            'summary': 'noexec probe'}\n"
        )
        result, _ = _exec(container_env, "pjs_w15v_noexec", src)
        assert result["status"] == "ok", result.get("error")
        assert result["output"]["exec"] == "blocked", "/tmp 下可执行文件能跑（noexec 未生效）"

    def test_verify_memory_cap_oom_kills_container(self, container_env):
        """--memory 1g 实机 OOM：容器内申请 3g → cgroup OOM kill（退出码 137），
        执行 fail；宿主测试进程存活不受影响"""
        src = (
            "def run(stdin):\n"
            "    chunks = []\n"
            "    for _ in range(30):\n"
            "        chunks.append(bytearray(100 * 1024 * 1024))\n"
            "    return {'status': 'ok', 'output': {'allocated_gb': 3}, 'summary': 'oom probe'}\n"
        )
        result, _ = _exec(container_env, "pjs_w15v_oom", src, timeout=120)
        assert result["status"] == "fail", f"超内存脚本未被终止: {result}"
        assert "137" in str(result.get("error", "")), (
            f"cgroup OOM kill 退出码 137 未出现: {result.get('error')}")
        # 宿主存活：同 fixture 工作区继续可用（容器资源失控不外溢）
        assert container_env["run_base"].is_dir()

    def test_verify_cpu_quota_throttles_throughput(self, container_env):
        """--cpus 0.5 实机节流：多进程 busy loop 的 cpu/wall 比值 < 1.0

        本机对照实测（2026-10-13）：无限制多进程比值 1.91，容器内 0.48。
        注意 threading 探针受 GIL 限制无区分度（宿主/容器均 ~0.8x），必须多进程。
        """
        src = (
            "import multiprocessing as mp\n"
            "import time\n"
            "\n"
            "def _burn(q):\n"
            "    t0 = time.time(); c0 = time.process_time()\n"
            "    while time.time() - t0 < 1.5:\n"
            "        pass\n"
            "    q.put(time.process_time() - c0)\n"
            "\n"
            "def run(stdin):\n"
            "    ctx = mp.get_context('fork')\n"
            "    q = ctx.Queue()\n"
            "    t0 = time.time()\n"
            "    procs = [ctx.Process(target=_burn, args=(q,)) for _ in range(4)]\n"
            "    for p in procs: p.start()\n"
            "    for p in procs: p.join()\n"
            "    wall = max(time.time() - t0, 1e-6)\n"
            "    cpu = sum(q.get() for _ in range(4))\n"
            "    return {'status': 'ok',\n"
            "            'output': {'wall': round(wall, 2), 'cpu': round(cpu, 2),\n"
            "                       'ratio': round(cpu / wall, 2)},\n"
            "            'summary': 'cpu probe'}\n"
        )
        result, _ = _exec(container_env, "pjs_w15v_cpu", src, timeout=120)
        assert result["status"] == "ok", result.get("error")
        ratio = result["output"]["ratio"]
        assert ratio < 1.0, (
            f"4 进程 busy loop cpu/wall 比值 {ratio} ≥ 1.0——0.5 核配额未生效"
            "（本机无限制对照 1.91）")

    def test_verify_mount_excludes_sibling_run_dirs(self, container_env):
        """挂载仅本次工作目录（强断言）：run_base 下其他任务目录容器内不可见"""
        sib = container_env["run_base"] / "sibling-task" / "data"
        sib.mkdir(parents=True)
        (sib / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
        src = (
            "import json, os\n"
            "def run(stdin):\n"
            "    return {'status': 'ok', 'output': {\n"
            "        'sibling_secret': os.path.exists('/workspace/sibling-task/data/secret.txt'),\n"
            "        'own_outputs': os.path.exists('/workspace/outputs'),\n"
            "        'run_dir_value': stdin['_run_dir'],\n"
            "    }, 'summary': 'mount probe'}\n"
        )
        result, _ = _exec(container_env, "pjs_w15v_mount", src)
        assert result["status"] == "ok", result.get("error")
        out = result["output"]
        assert out["sibling_secret"] is False, "script_runs 下其他任务目录在容器内可见（挂载面越界）"
        assert out["own_outputs"] is True, "本次工作区自身不可见（挂载失效，探针无效）"
        assert out["run_dir_value"] == "/workspace", "_run_dir 未重写为容器路径"


@requires_docker
@requires_image
def test_verify_image_packages_match_static_manifest():
    """镜像实装集合 ⊇ RUNNER_IMAGE_PACKAGES（对实际构建产物验证，单向包含：
    pip freeze 另含传递依赖，不要求相等；防清单/镜像单向漂移）"""
    def _norm(n: str) -> str:
        return n.lower().replace("_", "-")

    freeze = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", RUNNER_IMAGE,
         "pip", "freeze", "--disable-pip-version-check"],
        capture_output=True, text=True, timeout=180)
    assert freeze.returncode == 0, freeze.stderr
    installed = {_norm(line.split("==", 1)[0])
                 for line in freeze.stdout.splitlines() if "==" in line}
    assert installed, "pip freeze 无输出，探针无效"
    for spec in script_container.RUNNER_IMAGE_PACKAGES:
        base = _norm(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", spec.strip()).group(0))
        assert base in installed, f"静态清单声称 {spec}，但镜像内未安装（清单/镜像漂移）"


def test_verify_local_pythonpath_injection_preserved(monkeypatch):
    """W15 不破坏 W1：local 模式（默认）build_script_env 仍注入 PYTHONPATH=项目根，
    白名单 env 保留（模板脚本依赖平台 import 面，container 化不影响 local 行为）"""
    monkeypatch.delenv("SCRIPT_EXEC_MODE", raising=False)
    env = pre_judge_runner.build_script_env()
    assert env.get("PYTHONPATH") == str(pre_judge_runner.PROJECT_ROOT)
    # 白名单键集的强断言由 W1 security 测试（tests/security/test_env_whitelist.py）承担，
    # 此处只守护 PYTHONPATH 注入在 W15 后仍保留
    assert "PYTHONPATH" in env
