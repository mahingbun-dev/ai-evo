"""W1-3 攻击性验证：脚本子进程环境变量白名单（整改开发计划 §2 W1「测试设计」，必须做）

攻击路径复现（红→绿基线 657fc49 上应失败）：
1. 以超管身份走真实 API 链（create → upload → debug 试跑端点）运行一个
   「探针脚本」——探针尝试读取敏感环境变量（DATABASE_URL / REDIS_URL /
   LANGFUSE_SECRET_KEY / ANC_HUB_MCP_ENDPOINT / DB_PASSWORD / SSO_LOGIN_URL）
   并尝试 `from app.db.session import SessionLocal` 直连平台库；
2. 主进程（pytest 进程 = API 进程替身）持有上述敏感 env（DATABASE_URL 指向
   带 marker 行的 sqlite probe 库，见 §5.1 readonly_dsn fixture）；
3. 断言：
   - 探针 stdout 输出 leaked_env == {}（全部不可见）；
   - SessionLocal 因拿不到真实凭据无法连到主进程注入的库（marker 读不到，
     连接报错）——证明平台凭据没有泄漏到脚本子进程。

旁路同口径验证：
- runner.execute_script（debug 端点内部同通道）；
- service.run_smoke_test（契约冒烟，历史继承 API 进程 env）；
- build_script_env() 键集 ⊆ 白名单 ∪ {PYTHONPATH}。

独立测试角色编写；只新增测试，不修改实现代码。
"""
import asyncio
import io
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.services import pre_judge_runner, pre_judge_script_service
from app.api.pre_judge_script_api import (
    PreJudgeScriptDebugPayload,
    PreJudgeScriptPayload,
    create_script,
    debug_script,
    upload_script_file,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

# ---------------- 探针脚本（上传到平台、经真实子进程执行） ----------------

PROBE_SCRIPT = '''"""W1 攻击性验证探针：尝试读取平台敏感环境变量与数据库凭据"""
import json

SENSITIVE_KEYS = (
    "DATABASE_URL", "REDIS_URL", "LANGFUSE_SECRET_KEY",
    "ANC_HUB_MCP_ENDPOINT", "DB_PASSWORD", "SSO_LOGIN_URL",
)


def run(stdin):
    import os
    leaked = {k: os.environ.get(k) for k in SENSITIVE_KEYS if k in os.environ}

    # 平台凭据可达性探测：主进程的 DATABASE_URL 指向 probe 库（w1_probe 表
    # 含 marker 行）。子进程若泄漏到该凭据，SessionLocal 将连上并读到标记行
    marker_found = False
    db_error = None
    try:
        from sqlalchemy import text
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            row = s.execute(
                text("SELECT marker FROM w1_probe WHERE id = 1")).fetchone()
            marker_found = bool(row and row[0] == "leaked")
    except Exception as e:  # noqa: BLE001 - 探针边界：失败原因原样上报
        db_error = "{0}: {1}".format(type(e).__name__, e)

    return {
        "status": "ok",
        "output": {
            "leaked_env": leaked,
            "session_local_marker_found": marker_found,
            "session_local_error": db_error,
        },
        "files": {},
        "summary": "probe finished",
        "finish_reason": "completed",
        "error": None,
    }
'''

# 主进程（API 进程替身）持有的敏感环境变量；值全为带明显标记的假凭据，
# DATABASE_URL 由 readonly_dsn fixture 提供真实可达的 sqlite probe 库
_SENSITIVE_FAKE_VALUES = {
    "REDIS_URL": "redis://w1-should-not-leak:6379/0",
    "LANGFUSE_SECRET_KEY": "sk-w1-should-not-leak",
    "ANC_HUB_MCP_ENDPOINT": "http://w1-should-not-leak",
    "DB_PASSWORD": "w1-should-not-leak",
    "SSO_LOGIN_URL": "https://w1-should-not-leak",
}


# ---------------- 测试基建 ----------------

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def isolated_upload_paths(tmp_path, monkeypatch):
    """脚本包目录与运行目录隔离到 tmp（不污染仓库 uploads/）"""
    upload_base = tmp_path / "pre_judge_scripts"
    upload_base.mkdir()
    monkeypatch.setattr(pre_judge_script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")
    return upload_base


@pytest.fixture
def sensitive_env(monkeypatch, readonly_dsn):
    """主进程持有敏感 env（模拟生产 API 进程），DATABASE_URL 指向 probe 库"""
    values = {"DATABASE_URL": readonly_dsn, **_SENSITIVE_FAKE_VALUES}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _admin_request():
    """构造最小 Request（鉴权中间件写入的 request.state.user = 超管）"""
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [], "query_string": b""})
    request.state.user = {"user_code": "w1_probe_admin", "super_admin_flag": 1}
    return request


def _upload_probe(db_session, name=None):
    """经真实 API 端点链上传探针脚本（create → upload），返回脚本记录"""
    name = name or f"w1_probe_{uuid.uuid4().hex[:6]}"
    created = asyncio.run(create_script(
        PreJudgeScriptPayload(name=name, exec_type="script", timeout_seconds=60),
        request=_admin_request(), db=db_session))
    file = UploadFile(file=io.BytesIO(PROBE_SCRIPT.encode("utf-8")),
                      filename="probe.py")
    asyncio.run(upload_script_file(
        created["id"], request=_admin_request(), file=file,
        risk_acknowledged=None, declared_requirements=None, db=db_session))
    return created


def _assert_probe_clean(probe_output):
    """探针输出的核心安全断言（三个通道共用）"""
    assert probe_output["leaked_env"] == {}, (
        f"敏感环境变量泄漏到脚本子进程: {probe_output['leaked_env']}")
    assert probe_output["session_local_marker_found"] is False, (
        "探针经 SessionLocal 读到了主进程注入的 probe 库标记行——"
        "平台数据库凭据泄漏到脚本子进程")
    assert probe_output["session_local_error"], (
        "SessionLocal 应因拿不到真实凭据而连接失败（连接报错即可）")


# ==================== 1. debug 端点试跑（计划原文攻击路径） ====================

class TestProbeViaDebugEndpoint:
    def test_probe_reads_no_sensitive_env_via_debug(self, db_session,
                                                     isolated_upload_paths,
                                                     sensitive_env):
        """上传探针脚本 → 经 debug 试跑端点真实子进程执行 → 敏感 env 全部不可见

        计划 §2 W1 攻击性验证原文：断言 stdout 输出为 {}（全部不可见）；
        再断言探针脚本 from app.db.session import SessionLocal 在新 env 下
        因缺 DATABASE_URL 无法连接目标库。
        """
        created = _upload_probe(db_session)
        body = asyncio.run(debug_script(
            created["id"], PreJudgeScriptDebugPayload(fields={}),
            request=_admin_request(), db=db_session))
        result = body["result"]
        assert result["status"] == "ok", f"探针试跑失败: {result.get('error')}"
        _assert_probe_clean(result["output"])


# ==================== 2. runner 直调（debug 端点内部同通道） ====================

class TestProbeViaRunner:
    def test_probe_reads_no_sensitive_env_via_execute_script(
            self, db_session, isolated_upload_paths, sensitive_env):
        """runner.execute_script 通道（生产编排 / debug 端点共用）同口径"""
        created = _upload_probe(db_session)
        script_def = {
            "script_id": created["id"],
            "name": created["name"],
            "exec_type": "script",
            "entrypoint": created.get("entrypoint") or "main.py",
            "timeout_seconds": 60,
        }
        result = pre_judge_runner.execute_script(
            script_def,
            {"task_id": "w1_probe_task", "data_id": "d1", "fields": {},
             "inputs": {}, "model_output": None, "session": None,
             "params": {}, "upstream": {}},
            timeout_seconds=60)
        assert result["status"] == "ok", f"探针执行失败: {result.get('error')}"
        _assert_probe_clean(result["output"])


# ==================== 3. 契约冒烟旁路（run_smoke_test） ====================

class TestProbeViaSmokeTest:
    def test_smoke_test_passes_whitelist_env_only(self, tmp_path,
                                                   isolated_upload_paths,
                                                   sensitive_env):
        """run_smoke_test（上传链路的契约冒烟）同样只透传白名单 env

        冒烟返回解析后的 stdout JSON——探针在冒烟子进程内同样读不到敏感 env。
        """
        entry = tmp_path / "probe_pkg" / "main.py"
        entry.parent.mkdir(parents=True)
        entry.write_text(PROBE_SCRIPT, encoding="utf-8")
        stdout = pre_judge_script_service.run_smoke_test(entry)
        assert stdout.get("status") == "ok"
        _assert_probe_clean(stdout["output"])


# ==================== 4. 白名单构造单元面 ====================

class TestBuildScriptEnv:
    """build_script_env：键集 ⊆ 白名单 ∪ {PYTHONPATH}，PYTHONPATH=项目根"""

    def test_keys_subset_of_whitelist_plus_pythonpath(self, monkeypatch):
        from app.services.pre_judge_runner import (
            PROJECT_ROOT,
            SCRIPT_ENV_WHITELIST,
            build_script_env,
        )
        for key, value in {"DATABASE_URL": "postgresql://leak",
                           "REDIS_URL": "redis://leak",
                           "LANGFUSE_SECRET_KEY": "leak",
                           "SSO_LOGIN_URL": "https://leak"}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("http_proxy", "http://proxy-w1:1")
        env = build_script_env()
        assert set(env) <= set(SCRIPT_ENV_WHITELIST) | {"PYTHONPATH"}
        for key in ("DATABASE_URL", "REDIS_URL", "LANGFUSE_SECRET_KEY",
                    "SSO_LOGIN_URL"):
            assert key not in env
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["LANG"] == "en_US.UTF-8"
        assert env["http_proxy"] == "http://proxy-w1:1"
        assert env["PYTHONPATH"] == str(PROJECT_ROOT)

    def test_absent_whitelist_keys_not_in_env(self, monkeypatch):
        """白名单键缺省时不进 env（不造空值），敏感键无论何时不可见"""
        from app.services.pre_judge_runner import (
            SCRIPT_ENV_WHITELIST,
            build_script_env,
        )
        monkeypatch.delenv("http_proxy", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.setenv("DB_PASSWORD", "w1-should-not-leak")
        env = build_script_env()
        assert "http_proxy" not in env and "https_proxy" not in env
        assert "DB_PASSWORD" not in env
        assert set(env) <= set(SCRIPT_ENV_WHITELIST) | {"PYTHONPATH"}


# ==================== 5. db_assert 只读 DSN（§5.1 readonly_dsn fixture） ====================

class TestDbDsnInjectionIdentity:
    """只读 DSN 只注入内置 pjs_builtin_db_assert 记录（W1-3 凭据边界）

    身份判定以记录 id 为准：用户无法创建 pjs_builtin_ 前缀的记录，
    同名自建脚本（builtin 记录被删后用户占用该名）与其他内置脚本
    均不得获得 DSN——防止篡改/仿冒记录窃取只读凭据。
    """

    def _inject(self, monkeypatch, script_id, stdin=None, dsn="sqlite:///w1-readonly"):
        from types import SimpleNamespace as _NS

        monkeypatch.setattr(pre_judge_runner, "get_settings",
                            lambda: _NS(DB_ASSERT_READONLY_DSN=dsn))
        return pre_judge_runner._inject_builtin_db_dsn(
            {"script_id": script_id, "name": "db_assert"},
            dict(stdin or {}))

    def test_builtin_db_assert_record_gets_dsn(self, monkeypatch):
        out = self._inject(monkeypatch, "pjs_builtin_db_assert")
        assert out["db_dsn"] == "sqlite:///w1-readonly"

    def test_user_created_same_name_script_gets_no_dsn(self, monkeypatch):
        """同名自建脚本（name=db_assert 但 id 为用户记录）不注入"""
        out = self._inject(monkeypatch, "pjs_usercopy123")
        assert "db_dsn" not in out

    def test_other_builtin_scripts_get_no_dsn(self, monkeypatch):
        """其他内置脚本（log_grep 等）不注入"""
        out = self._inject(monkeypatch, "pjs_builtin_log_grep")
        assert "db_dsn" not in out

    def test_unconfigured_dsn_injects_nothing(self, monkeypatch):
        """DB_ASSERT_READONLY_DSN 未配置（空串）→ 不注入，由脚本侧明确报错"""
        out = self._inject(monkeypatch, "pjs_builtin_db_assert", dsn="")
        assert "db_dsn" not in out

    def test_injection_preserves_other_stdin_keys(self, monkeypatch):
        stdin = {"task_id": "t1", "fields": {"a": "b"}, "params": {"x": 1}}
        out = self._inject(monkeypatch, "pjs_builtin_db_assert", stdin)
        assert out["task_id"] == "t1" and out["fields"] == {"a": "b"}
        assert out["params"] == {"x": 1}
        # 原 stdin 不被就地修改（调用方持有的 dict 不受影响）
        assert "db_dsn" not in stdin


class TestDbAssertReadonlyDsnFixture:
    def test_db_assert_runs_via_stdin_dsn(self, tmp_path, readonly_dsn):
        """fixture 冒烟：db_assert 只认 stdin.db_dsn（不回退平台库），
        用 sqlite 测试 DSN 跑通现有断言用例（D-5 真实只读账号未到位，
        真实账号回归见 notCovered）"""
        from app.scripts_builtin.db_assert import run as db_assert_run

        assertions = tmp_path / "assertions.json"
        assertions.write_text(json.dumps({"assertions": [
            {"name": "行数", "sql": "SELECT id FROM db_assert_smoke WHERE ok = false",
             "expected": {"row_count": 1}},
        ]}, ensure_ascii=False), encoding="utf-8")
        out = db_assert_run({"fields": {"assertions": str(assertions)},
                             "db_dsn": readonly_dsn, "params": {}})
        assert out["status"] == "ok"
        assert out["output"]["passed"] == 1 and out["output"]["total"] == 1
        assert out["output"]["consistency_rate"] == 1.0

    def test_db_assert_without_dsn_fails_explicitly(self, tmp_path):
        """未注入 db_dsn：明确报「缺少只读数据库连接」失败，不静默回退平台库"""
        from app.scripts_builtin.db_assert import run as db_assert_run

        assertions = tmp_path / "assertions.json"
        assertions.write_text(json.dumps({"assertions": [
            {"name": "行数", "sql": "SELECT 1", "expected": {"row_count": 1}},
        ]}, ensure_ascii=False), encoding="utf-8")
        out = db_assert_run({"fields": {"assertions": str(assertions)},
                             "params": {}})
        assert out["status"] == "fail"
        assert "db_dsn" in out["error"] and "DB_ASSERT_READONLY_DSN" in out["error"]
