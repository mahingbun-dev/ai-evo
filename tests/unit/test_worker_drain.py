"""worker 非阻塞优雅换代单元测试（_up_services / worker_container_state / swap_worker / watcher）

覆盖 feat/frontend-only-fastpath-worker-drain 的优化 2：
  - _up_services：compose up 目标不再包含 worker（由 swap_worker 单独编排）
  - worker_container_state：docker inspect 输出解析，inspect 失败 → ("missing", "")
  - swap_worker 三条路径：
      noop      容器不在运行 → 直接 compose up -d worker
      fast      SIGTERM 后 DRAIN_WAIT_SEC 内退出 → compose up -d worker
      deferred  超时仍在运行 → 后台 watcher（Popen, start_new_session）接管
    docker update/kill 失败只记日志、不阻断
  - WORKER_WATCHER_SCRIPT 内联脚本契约与 spawn_drain_watcher 的容错

所有外部命令（bs.subprocess.run / bs.subprocess.Popen / bs.compose /
bs.worker_container_state）一律 monkeypatch 假体并记录调用，绝不真的敲 docker；
时间敏感的轮询用 DRAIN_WAIT_SEC=1 + no-op sleep（fast）与 DRAIN_WAIT_SEC=0
（deferred，循环体一次都不执行）规避真实等待。
"""
import io
import os
import subprocess as sp
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB/无 docker）

# ---------------------------------------------------------------
# 导入前准备：build_service 模块级会执行 load_config()（与 test_build_service 同手法）
# ---------------------------------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="build-service-test-drain-"))
_WS = _TMP / "ws"
_WS.mkdir(parents=True, exist_ok=True)

_CONFIG = _TMP / "config.toml"
_CONFIG.write_text(
    f"""
[auth]
token = "test-token"

[paths]
workspace = "{_WS}"
build_workspace = "{_TMP / 'ws-build'}"
deploy_dir = "{_WS}"
data_dir = "{_TMP / 'data'}"
""",
    encoding="utf-8",
)
# 必须 setdefault 而非赋值：pytest 按文件名字母序收集，test_build_service.py
# 先加载模块，后加载的文件不得破坏已加载模块的配置
os.environ.setdefault("BUILD_SERVICE_CONFIG", str(_CONFIG))

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "tools" / "build-service"
sys.path.insert(0, str(_SERVICE_DIR))

import build_service as bs  # noqa: E402


# ---------------------------------------------------------------
# 公共假体
# ---------------------------------------------------------------

def _forbid_popen(monkeypatch):
    """fast path 绝不应当启动后台 watcher——Popen 被调用即失败（且不产生真进程）"""
    def boom(*args, **kwargs):
        raise AssertionError("fast path 不应调用 subprocess.Popen 启动 watcher")
    monkeypatch.setattr(bs.subprocess, "Popen", boom)


def _recording_compose(monkeypatch, *, rc=0, events=None):
    calls = []

    def fake_compose(*args, **kwargs):
        calls.append(args)
        if events is not None:
            events.append(("compose", args))
        return sp.CompletedProcess(args, rc, "", "")

    monkeypatch.setattr(bs, "compose", fake_compose)
    return calls


class _StateSequence:
    """按序返回 worker_container_state 的假体（用尽后报错，暴露多余轮询）"""

    def __init__(self, states):
        self._states = list(states)

    def __call__(self, *args, **kwargs):
        if not self._states:
            raise AssertionError("worker_container_state 被调用次数超出预期")
        return self._states.pop(0)


# ======================================================================
# _up_services：compose up 目标剥离 worker
# ======================================================================

class TestUpServices:
    def test_default_config_excludes_worker(self):
        """默认 config（services=["api","worker"]）→ 只 up api"""
        assert bs.DEPLOY_SERVICES == ["api", "worker"], \
            "前置假设：测试配置未设 [deploy].services，取代码默认值"
        assert bs._up_services() == ["api"]

    def test_filters_worker_from_mixed_services(self, monkeypatch):
        """多服务配置里只剔除 worker，其余原样保留且保序"""
        monkeypatch.setattr(bs, "DEPLOY_SERVICES", ["api", "worker", "hub-plugin"])
        assert bs._up_services() == ["api", "hub-plugin"]

    def test_all_worker_yields_empty(self, monkeypatch):
        """只配了 worker → 列表为空（compose up 不带任何服务）"""
        monkeypatch.setattr(bs, "DEPLOY_SERVICES", ["worker"])
        assert bs._up_services() == []


# ======================================================================
# worker_container_state：docker inspect 解析
# ======================================================================

class TestWorkerContainerState:
    def test_running_state_with_container_id(self, monkeypatch):
        """inspect 成功：解析出 (State.Status, 容器 ID)"""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return sp.CompletedProcess(cmd, 0, "running abc123def456\n", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)

        state, cid = bs.worker_container_state("ai-evo-worker")

        assert (state, cid) == ("running", "abc123def456")
        assert calls[0][:3] == ["docker", "inspect", "--format"]

    def test_inspect_failure_reports_missing(self, monkeypatch):
        """inspect 退出码非 0（容器不存在等）→ ("missing", "")"""
        def fake_run(cmd, **kwargs):
            return sp.CompletedProcess(cmd, 1, "", "No such object")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)

        assert bs.worker_container_state("ai-evo-worker") == ("missing", "")

    def test_empty_stdout_reports_unknown(self, monkeypatch):
        """inspect 成功但输出为空 → ("unknown", "")（防御性分支）"""
        def fake_run(cmd, **kwargs):
            return sp.CompletedProcess(cmd, 0, "\n", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)

        assert bs.worker_container_state("ai-evo-worker") == ("unknown", "")


# ======================================================================
# swap_worker：fast path（SIGTERM 后窗口期内退出）
# ======================================================================

class TestSwapWorkerFast:
    def test_drained_worker_recreated_immediately(self, monkeypatch):
        """运行中的旧 worker：update --restart=no + kill --signal=TERM（顺序固定、
        都在 compose up 之前）→ 窗口期内退出 → compose up -d worker，返回 fast"""
        monkeypatch.setattr(bs, "worker_container_state",
                            _StateSequence([("running", "oldcid123456"),
                                            ("exited", "oldcid123456")]))
        monkeypatch.setattr(bs, "DRAIN_WAIT_SEC", 1)
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)
        _forbid_popen(monkeypatch)

        events = []

        def fake_run(cmd, **kwargs):
            events.append(("subprocess", list(cmd)))
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        compose_calls = _recording_compose(monkeypatch, events=events)

        result = bs.swap_worker(io.StringIO())

        assert result == "fast"
        assert compose_calls == [("up", "-d", "worker")]
        # docker update / docker kill 恰好各一次、顺序固定
        subprocess_cmds = [c for kind, c in events if kind == "subprocess"]
        assert subprocess_cmds == [
            ["docker", "update", "--restart=no", bs.WORKER_CONTAINER],
            ["docker", "kill", "--signal=TERM", bs.WORKER_CONTAINER],
        ]
        # 两条 docker 命令都必须先于 compose up（先停旧、后起新）
        update_idx = events.index(("subprocess", subprocess_cmds[0]))
        kill_idx = events.index(("subprocess", subprocess_cmds[1]))
        up_idx = events.index(("compose", ("up", "-d", "worker")))
        assert update_idx < up_idx
        assert kill_idx < up_idx


# ======================================================================
# swap_worker：deferred path（窗口期内未退出 → 后台 watcher）
# ======================================================================

class TestSwapWorkerDeferred:
    def test_timeout_spawns_background_watcher(self, tmp_path, monkeypatch):
        """DRAIN_WAIT_SEC=0：轮询循环体一次都不执行，state 仍 running →
        spawn_drain_watcher（Popen, start_new_session=True），返回 deferred，
        compose 零调用（流水线不等长任务）"""
        monkeypatch.setattr(bs, "worker_container_state",
                            lambda *a, **k: ("running", "oldcidddee"))
        monkeypatch.setattr(bs, "DRAIN_WAIT_SEC", 0)
        monkeypatch.setattr(bs, "DRAIN_FORCE_KILL_SEC", 86400)
        monkeypatch.setattr(bs, "ALERT_LOG_PATH", tmp_path / "alerts.log")
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)

        # docker update / docker kill（SIGTERM）在 deferred 路径同样会发
        docker_cmds = []

        def fake_run(cmd, **kwargs):
            docker_cmds.append(list(cmd))
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)

        popen_calls = []

        class FakeProc:
            pass

        def fake_popen(cmd, **kwargs):
            popen_calls.append((list(cmd), dict(kwargs)))
            return FakeProc()

        monkeypatch.setattr(bs.subprocess, "Popen", fake_popen)
        compose_calls = _recording_compose(monkeypatch)

        result = bs.swap_worker(io.StringIO())

        assert result == "deferred"
        assert len(popen_calls) == 1
        cmd, kwargs = popen_calls[0]
        assert bs.WORKER_WATCHER_SCRIPT in cmd
        assert "oldcidddee" in cmd                       # 按旧容器 ID 盯（不是按名字）
        assert str(bs.DRAIN_FORCE_KILL_SEC) in cmd       # "86400"
        assert bs.WORKER_CONTAINER in cmd
        assert str(bs.ALERT_LOG_PATH) in cmd
        assert kwargs.get("start_new_session") is True   # 脱离部署线程存活
        assert compose_calls == []                       # deferred 不当场拉起 worker
        # SIGTERM 链路照常发出（update --restart=no + kill --signal=TERM）
        assert docker_cmds == [
            ["docker", "update", "--restart=no", bs.WORKER_CONTAINER],
            ["docker", "kill", "--signal=TERM", bs.WORKER_CONTAINER],
        ]


# ======================================================================
# swap_worker：noop path（容器不在运行）
# ======================================================================

class TestSwapWorkerNoop:
    def test_not_running_directly_ups_worker(self, monkeypatch):
        """容器不在运行（exited/missing 等）→ 直接 compose up -d worker，
        不发 update/kill，返回 noop"""
        monkeypatch.setattr(bs, "worker_container_state",
                            lambda *a, **k: ("exited", "oldcid"))
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(list(cmd))
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        compose_calls = _recording_compose(monkeypatch)

        result = bs.swap_worker(io.StringIO())

        assert result == "noop"
        assert compose_calls == [("up", "-d", "worker")]
        assert run_calls == []

    def test_compose_failure_raises(self, monkeypatch):
        """compose up worker 失败（退出码非 0）→ RuntimeError 终止部署"""
        monkeypatch.setattr(bs, "worker_container_state",
                            lambda *a, **k: ("missing", ""))
        _recording_compose(monkeypatch, rc=1)

        with pytest.raises(RuntimeError, match="compose up -d worker 失败"):
            bs.swap_worker(io.StringIO())


# ======================================================================
# swap_worker：docker update/kill 失败不阻断
# ======================================================================

class TestSwapWorkerDockerCmdFailure:
    def test_update_kill_failure_does_not_block(self, monkeypatch):
        """update/kill 退出码非 0 只记日志：state 脚本照常走完（不抛异常）"""
        monkeypatch.setattr(bs, "worker_container_state",
                            _StateSequence([("running", "oldcid"),
                                            ("exited", "oldcid")]))
        monkeypatch.setattr(bs, "DRAIN_WAIT_SEC", 1)
        monkeypatch.setattr(bs.time, "sleep", lambda s: None)
        _forbid_popen(monkeypatch)

        def fake_run(cmd, **kwargs):
            return sp.CompletedProcess(cmd, 1, "", "cannot update container")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        compose_calls = _recording_compose(monkeypatch)

        logf = io.StringIO()
        result = bs.swap_worker(logf)

        assert result == "fast"
        assert compose_calls == [("up", "-d", "worker")]
        assert "失败" in logf.getvalue()  # 失败被记录但未阻断


# ======================================================================
# WORKER_WATCHER_SCRIPT：内联脚本契约
# ======================================================================

class TestWorkerWatcherScript:
    def test_script_compiles(self):
        """watcher 脚本必须是合法 Python（python3 -c 直接执行，坏了只会静默失败）"""
        compile(bs.WORKER_WATCHER_SCRIPT, "worker-watcher", "exec")

    def test_script_contract(self):
        """脚本契约：按容器 ID 强制回收（rm -f）、读 ALERT_WEBHOOK_URL、
        compose --project-directory 拉起新 worker；restart=no 只在主流程"""
        raw = bs.WORKER_WATCHER_SCRIPT
        # 源码里 rm -f 是列表形式 ["docker", "rm", "-f", old_id]，归一化后断言
        normalized = " ".join(
            tok for tok in raw.replace('"', " ").replace(",", " ").split()
        )
        assert "docker rm -f" in normalized
        assert "--restart=no" not in raw  # restart 策略调整只在主流程 docker update
        assert "ALERT_WEBHOOK_URL" in raw  # 与主应用同一告警环境变量
        assert "compose" in raw
        assert "--project-directory" in raw


# ======================================================================
# spawn_drain_watcher：容错（watcher 启动失败不阻断部署）
# ======================================================================

class TestSpawnDrainWatcher:
    def test_popen_failure_does_not_raise(self, monkeypatch):
        """Popen 抛异常（如 fork 失败）→ 不抛出，日志记录「watcher 启动失败」
        （cron 的 ensure_worker.sh 每 5 分钟兜底）"""
        def boom(*args, **kwargs):
            raise OSError("fork failed")

        monkeypatch.setattr(bs.subprocess, "Popen", boom)
        logf = io.StringIO()

        bs.spawn_drain_watcher("oldcid123456", logf)  # 不抛异常即通过

        assert "watcher 启动失败" in logf.getvalue()
