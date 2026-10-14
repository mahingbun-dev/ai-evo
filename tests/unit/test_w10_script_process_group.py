"""W10-6 脚本子进程进程组终止与 stdin 写超时单元测试

整改计划 §3 W10 第 6 项：
- Popen 加 start_new_session=True，kill 用进程组（os.killpg）——超时终止
  连孙进程一并收割，不遗留孤儿孙进程；
- proc.stdin.write 前置校验 payload 大小、写独立线程 + 短超时——「脚本不读
  stdin 的大 payload」不再把执行器永久卡死，有限时间内超时返回。

全部用真实子进程（本 venv python + 真实 wrapper），无 DB/Redis 依赖。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit  # W8 分类门禁：补打标
from app.services import pre_judge_runner, pre_judge_script_service

# ---------------- fake 脚本源码 ----------------

_GC_SPAWNER_SRC = '''
import subprocess
import time


def run(stdin):
    """起一个长睡孙进程并写 pid 文件，然后自身长睡——验证超时终止连孙进程收割"""
    probe = subprocess.Popen(["sleep", "60"])
    with open("gc.pid", "w", encoding="utf-8") as f:
        f.write(str(probe.pid))
    time.sleep(120)
'''

# 传统 __main__ 风格：wrapper 不消费 stdin（stdin 直通脚本），本脚本不读 stdin
_STDIN_DEADLOCK_SRC = '''
import time

print("legacy style, never reading stdin", flush=True)
time.sleep(120)
'''

_ECHO_LEN_SRC = '''
import json


def run(stdin):
    """回显 stdin 字节数（验证大 payload 正常注入不误伤）"""
    return {
        "status": "ok",
        "output": {"task_id": stdin.get("task_id"), "big_len": len(stdin.get("big") or "")},
        "files": {},
        "summary": "echoed",
        "finish_reason": "completed",
        "error": None,
    }
'''


def _wait_pid_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def script_workspace(tmp_path, monkeypatch):
    """脚本包落盘 + 产物根目录指向临时目录（不污染仓库 uploads/）"""
    upload_base = tmp_path / "pre_judge_scripts"
    run_base = tmp_path / "script_runs"
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", run_base)
    return {"upload_base": upload_base, "run_base": run_base}


def _write_pkg(upload_base: Path, script_id: str, source: str) -> None:
    pkg_dir = upload_base / script_id
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "main.py").write_text(source, encoding="utf-8")


def _run_subprocess(script_id: str, source: str, stdin: dict, timeout: float, workspace) -> dict:
    _write_pkg(workspace["upload_base"], script_id, source)
    # _run_dir 模拟生产工作区条目目录（绝对路径，脚本包从其 scripts/ 副本执行）
    stdin.setdefault("_run_dir", str(workspace["run_base"] / "workspace" / script_id / "item"))
    return pre_judge_runner._execute_script_subprocess(
        {"script_id": script_id, "name": script_id, "entrypoint": "main.py"},
        stdin,
        timeout,
        time.time(),
    )


# ---------------------------------------------------------------------------
# 进程组终止：孙进程一并收割
# ---------------------------------------------------------------------------

class TestProcessGroupKill:
    def test_timeout_kills_grandchild_end_to_end(self, script_workspace):
        """脚本超时终止：脚本自己 spawn 的孙进程也被收割（killpg 进程组）"""
        stdin = {"task_id": "w10pg", "data_id": "d1"}
        result = _run_subprocess("w10_gc_spawner", _GC_SPAWNER_SRC, stdin, timeout=2, workspace=script_workspace)

        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"

        cwd = pre_judge_runner.build_script_run_dir("w10pg", "d1", "w10_gc_spawner")
        gc_pid = int((cwd / "gc.pid").read_text(encoding="utf-8").strip())
        assert _wait_pid_gone(gc_pid), f"孙进程 {gc_pid} 未被进程组收割（孤儿进程）"

    def test_kill_process_tree_helper_kills_grandchild(self, tmp_path):
        """_kill_process_tree 直测：start_new_session 子进程与其孙进程一并终止"""
        pid_file = tmp_path / "gc.pid"
        child_src = (
            "import subprocess, time, sys\n"
            f"probe = subprocess.Popen(['sleep', '60'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(probe.pid))\n"
            "time.sleep(120)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_src],
            start_new_session=True,  # 与生产 _execute_script_subprocess 同款启动方式
        )
        try:
            deadline = time.time() + 5
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.05)
            gc_pid = int(pid_file.read_text(encoding="utf-8").strip())

            pre_judge_runner._kill_process_tree(proc)
            proc.wait(timeout=5)

            assert _wait_pid_gone(gc_pid), f"孙进程 {gc_pid} 未被收割"
        finally:
            if proc.poll() is None:
                pre_judge_runner._kill_process_tree(proc)
                proc.wait(timeout=5)

    def test_kill_process_tree_on_exited_proc_is_safe(self):
        """对已退出进程调用 kill：进程组不存在时退回单进程兜底，不抛异常"""
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        pre_judge_runner._kill_process_tree(proc)  # 不应抛异常


# ---------------------------------------------------------------------------
# stdin 死锁防护
# ---------------------------------------------------------------------------

class TestStdinWriteTimeout:
    def test_stdin_deadlock_returns_in_bounded_time(self, script_workspace, monkeypatch):
        """不读 stdin 的传统脚本 + 大 payload（>64KB 管道缓冲）：有限时间超时返回"""
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_STDIN_WRITE_TIMEOUT", 2.0)

        big = "x" * (256 * 1024)
        stdin = {"task_id": "w10dl", "data_id": "d1", "big": big}
        started = time.time()
        result = _run_subprocess(
            "w10_stdin_deadlock", _STDIN_DEADLOCK_SRC, stdin, timeout=60,
            workspace=script_workspace,
        )
        elapsed = time.time() - started

        assert result["status"] == "fail"
        assert result["finish_reason"] == "timeout"
        assert "标准输入" in (result["error"] or "")
        # 有限时间内返回：写超时 + 进程组终止 + 等待，不允许卡满脚本 timeout(60s)
        assert elapsed < 30, f"stdin 死锁未在有限时间返回（耗时 {elapsed:.1f}s）"

    def test_oversize_payload_rejected_before_spawn(self, script_workspace, tmp_path, monkeypatch):
        """payload 超限：前置拒绝，不发起子进程、不写 stdout/stderr 文件"""
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_STDIN_PAYLOAD_LIMIT", 1024)

        stdin = {"task_id": "w10big", "data_id": "d1", "big": "x" * 4096}
        started = time.time()
        result = _run_subprocess(
            "w10_echo_len", _ECHO_LEN_SRC, stdin, timeout=30, workspace=script_workspace,
        )
        elapsed = time.time() - started

        assert result["status"] == "fail"
        assert result["finish_reason"] == "error"
        assert "超限" in (result["error"] or "")
        assert elapsed < 5, "超限拒绝必须是前置快速失败"
        cwd = pre_judge_runner.build_script_run_dir("w10big", "d1", "w10_echo_len")
        assert not (cwd / "stdout.log").exists(), "超限拒绝不应发起子进程"

    def test_large_payload_flows_for_stdin_consuming_script(self, script_workspace):
        """回归：消费 stdin 的脚本 + 大 payload（< 上限）正常注入执行"""
        big = "y" * (256 * 1024)
        stdin = {"task_id": "w10ok", "data_id": "d1", "big": big}
        result = _run_subprocess(
            "w10_echo_len", _ECHO_LEN_SRC, stdin, timeout=30, workspace=script_workspace,
        )

        assert result["status"] == "ok", result
        assert result["output"]["big_len"] == len(big)
        assert result["output"]["task_id"] == "w10ok"

    def test_default_limits_are_sane(self):
        """生产默认值：写超时短（10s）、payload 上限 8MB"""
        assert pre_judge_runner.SCRIPT_STDIN_WRITE_TIMEOUT <= 30
        assert pre_judge_runner.SCRIPT_STDIN_PAYLOAD_LIMIT == 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# 兼容回归：正常脚本仍可执行
# ---------------------------------------------------------------------------

class TestHappyPathUnchanged:
    def test_normal_script_success(self, script_workspace):
        """常规小 payload + 消费 stdin 的脚本：执行成功（写线程路径不破坏正常流）"""
        stdin = {"task_id": "w10ok2", "data_id": "d1", "big": ""}
        result = _run_subprocess(
            "w10_echo_len", _ECHO_LEN_SRC, stdin, timeout=30, workspace=script_workspace,
        )
        assert result["status"] == "ok", result
        assert result["output"]["task_id"] == "w10ok2"
