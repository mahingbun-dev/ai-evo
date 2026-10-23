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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.audit_models import OperationLog


@pytest.fixture(autouse=True)
def _pin_script_exec_mode_local(monkeypatch):
    """单测套件显式 local（ARCH-1-4 后 local 须显式声明，不再依赖缺省）"""
    monkeypatch.setenv("SCRIPT_EXEC_MODE", "local")


@pytest.fixture(autouse=True)
def _hermetic_audit_session(monkeypatch):
    """QE4-4：审计落库替身——写端点统一接 audited 依赖后，审计写放大

    走 TestClient 的用例若不隔离，log_operation 的独立 SessionLocal 会
    连 .env 的开发库写脏数据（或连接失败告警）。本套件级替身把审计写
    收进一次性内存 sqlite；用例自建 fixture 已 patch
    app.services.audit_service.SessionLocal 的不受影响（后应用者胜出）。
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[OperationLog.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.services.audit_service.SessionLocal", factory)
    yield factory
    engine.dispose()
