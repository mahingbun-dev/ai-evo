"""tests/unit 套件级显式声明：前处理脚本执行模式钉 local（ARCH-1-4）

ARCH-1-4 把 SCRIPT_EXEC_MODE 的代码缺省从 local 切到 container
（app/core/config.py）。本目录大量既有用例验证 local 通道的子进程直跑语义
（W10 进程组/超时/死锁、脚本上传冒烟校验链、pre_judge runner local 分支、
MCP server 全链路……），此前隐式依赖「代码缺省 = local」。缺省切换后这些
用例一律不依赖缺省值：由本 autouse fixture 统一显式声明 local。

- 验证 container 行为的用例在用例体内 monkeypatch.setenv 覆盖（fixture
  先设、用例后设，后写生效——既有 container 用例零改动）；
- 验证缺省值本身的门禁（container 为企业部署默认）在
  test_exec_mode_default.py，用例体内 delenv 后以 _env_file=None 的干净
  Settings 断言，不经本 fixture 的值。

不动 tests/integration（实机探针自管 env）与 tests/ 根 conftest（W1 fixture）。
"""
import pytest


@pytest.fixture(autouse=True)
def _pin_script_exec_mode_local(monkeypatch):
    """单测套件显式 local（ARCH-1-4 后 local 须显式声明，不再依赖缺省）"""
    monkeypatch.setenv("SCRIPT_EXEC_MODE", "local")
