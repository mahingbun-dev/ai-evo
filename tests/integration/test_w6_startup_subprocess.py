"""W6-3 启动校验子进程集成测试（整改开发计划 §2 W6「测试设计」第三条）。

断言：unset DATABASE_URL 启动 API → 明确报错退出（非零退出码 + 可读 stderr）；
配好 DSN 时校验放行、服务正常启动（fail-fast 不误伤）。

红→绿：基点 657fc49 的 config.py DATABASE_URL 带硬编码默认值且无
validate_runtime_settings——无 DSN 启动会直接成功（本组断言必红）。为避免在
主仓实跑旧代码触发 lifespan 清理共享库 running 任务（危险副作用），红验证
采用静态证据：主仓 config.py:19 默认值非空 + validate_runtime_settings 符号
不存在（单测层 test_w6_secrets_and_config 在基点 collection error 已证红）。
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from tests.integration.conftest import REPO_ROOT

import pytest
pytestmark = pytest.mark.integration  # W8 分类：integration（需本地 PG/Redis）

PY = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_startup_without_dsn_fails_fast():
    """unset DATABASE_URL 启动 API → 非零退出 + stderr 可读报错。"""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    port = _free_port()
    r = subprocess.run(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode != 0, "缺 DATABASE_URL 时服务必须拒绝启动"
    combined = r.stdout + r.stderr
    assert "DATABASE_URL 未配置" in combined, (
        f"stderr 必须含可读的缺失配置报错，实得: {combined[-600:]}"
    )
    assert "Application startup failed" in combined, "uvicorn 应报启动失败"


def test_startup_with_dsn_passes_validation():
    """配好 DSN（校验只看存在性，连接性由运行期自暴露）→ 校验放行，服务正常启动。"""
    env = dict(os.environ)
    env["DATABASE_URL"] = "postgresql://w67ut:w67ut@127.0.0.1:59999/w67t_none"
    port = _free_port()
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # 等待启动完成（import + lifespan）；若中途退出则立即失败
        deadline = time.time() + 30
        output_so_far = ""
        while time.time() < deadline:
            if proc.poll() is not None:
                output_so_far = proc.stdout.read()
                raise AssertionError(
                    f"有 DSN 时服务不应启动失败（fail-fast 误伤）: {output_so_far[-800:]}"
                )
            time.sleep(0.5)

        # 进程存活 30 秒即认为通过校验并完成启动
        assert proc.poll() is None, "服务应保持运行"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
