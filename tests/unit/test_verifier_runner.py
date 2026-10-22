"""终态校验器单元测试（M2 QE2-1，对象 app/services/verifier_runner.py）

覆盖：
1. file_assert：exists/regex/jsonpath 三模式 × pass/fail、negate、路径越界
   （../ 逃出工作区 → error，不读盘）、JSON 非法、文件缺失；
2. db_assert 原生化：SQL 白名单拒绝（INSERT 伪装）、断言失败 → fail、
   DSN 未配置 → error（不回退平台库）；引擎经 monkeypatch 假体；
3. http_assert：SSRF 守卫（内网目标拒绝 → error）、状态码/正则/jsonpath
   断言矩阵（requests 经 monkeypatch 假体，不出网）；
4. script 类型：复用 execute_script 通道的状态映射
   ok→pass / skip→pass / fail(completed)→fail / fail(error)→error；
5. run_verifiers 编排：多校验器聚合、on_fail=abort 短路、未配置返回 None；
6. 聚合与门禁接线：ResultAggregator._aggregate_verification 口径 +
   evaluate_gate 对 verification_pass_rate 的可用/不可用分支；
7. validate_verifier_shape 配置校验矩阵。
"""
import json
from unittest.mock import patch

import pytest

from app.services.verifier_runner import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    run_single_verifier,
    run_verifiers,
    validate_verifier_shape,
)
from app.services.gate_service import evaluate_gate
from app.services.result_aggregator import ResultAggregator

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB；HTTP/DB 均为假体）


# ---------------- 测试工具 ----------------

def _make_task(verifiers=None, task_id="task_vfy_x"):
    return type("Task", (), {
        "id": task_id,
        "verifiers": verifiers,
    })()


def _run(type_, config, run_dir, name="校验器", model_output="模型输出"):
    verifier = {"type": type_, "name": name, "config": config}
    return run_single_verifier(
        verifier, task_id="task_vfy_x", data_id="d1", run_dir=run_dir,
        model_output=model_output, data={}, payload={},
    )


# ==================== 1. file_assert ====================

class TestFileAssert:
    @pytest.fixture
    def workspace(self, tmp_path):
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "result.json").write_text(
            json.dumps({"status": "done", "rows": [{"count": 3}]}), encoding="utf-8")
        (outputs / "log.txt").write_text("order paid\nrefund skipped\n", encoding="utf-8")
        return tmp_path

    def test_exists_pass_and_fail(self, workspace):
        entry = _run("file_assert", {"assertions": [{"name": "结果文件", "path": "outputs/result.json"}]}, workspace)
        assert entry["status"] == STATUS_PASS
        entry = _run("file_assert", {"assertions": [{"path": "outputs/missing.json"}]}, workspace)
        assert entry["status"] == STATUS_FAIL
        assert entry["evidence"]["checks"][0]["error"] == "文件不存在"

    def test_exists_negate(self, workspace):
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/missing.json", "negate": True}]}, workspace)
        assert entry["status"] == STATUS_PASS

    def test_regex_match_and_miss(self, workspace):
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/log.txt", "mode": "regex", "pattern": r"order paid"}]}, workspace)
        assert entry["status"] == STATUS_PASS
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/log.txt", "mode": "regex", "pattern": r"order canceled"}]}, workspace)
        assert entry["status"] == STATUS_FAIL

    def test_regex_negate(self, workspace):
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/log.txt", "mode": "regex",
                            "pattern": r"canceled", "negate": True}]}, workspace)
        assert entry["status"] == STATUS_PASS

    def test_regex_invalid_pattern_is_error(self, workspace):
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/log.txt", "mode": "regex", "pattern": r"([unclosed"}]}, workspace)
        assert entry["status"] == STATUS_ERROR

    def test_jsonpath_value_match(self, workspace):
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/result.json", "mode": "jsonpath",
                            "jsonpath": "status", "expected": "done"}]}, workspace)
        assert entry["status"] == STATUS_PASS
        # 数组下标 + 数值宽容相等（int vs float）
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/result.json", "mode": "jsonpath",
                            "jsonpath": "rows[0].count", "expected": 3.0}]}, workspace)
        assert entry["status"] == STATUS_PASS
        # 值不匹配 → fail
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/result.json", "mode": "jsonpath",
                            "jsonpath": "status", "expected": "pending"}]}, workspace)
        assert entry["status"] == STATUS_FAIL

    def test_jsonpath_missing_key_is_fail(self, workspace):
        entry = _run("file_assert", {
            "assertions": [{"path": "outputs/result.json", "mode": "jsonpath",
                            "jsonpath": "state"}]}, workspace)
        assert entry["status"] == STATUS_FAIL
        assert "不可解析" in entry["evidence"]["checks"][0]["error"]

    def test_path_escape_rejected_without_read(self, workspace):
        secret = workspace.parent / "secret.txt"
        secret.write_text("root secret", encoding="utf-8")
        for raw in ("../secret.txt", "/etc/passwd", "outputs/../../secret.txt"):
            entry = _run("file_assert", {"assertions": [{"path": raw}]}, workspace)
            assert entry["status"] == STATUS_ERROR, raw
            assert "越界" in entry["evidence"]["checks"][0]["error"], raw

    def test_missing_assertions_is_error(self, workspace):
        entry = _run("file_assert", {"assertions": []}, workspace)
        assert entry["status"] == STATUS_ERROR
        assert "assertions" in entry["error"]


# ==================== 2. db_assert 原生化 ====================

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def fetchmany(self, n):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt):
        return _FakeResult(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _FakeConnection(self._rows)

    def dispose(self):
        pass


class TestDbAssert:
    @pytest.fixture
    def workspace(self, tmp_path, monkeypatch):
        # 只读 DSN 固定注入（复用 pre_judge_runner._builtin_db_dsn 的 Settings 读取）
        monkeypatch.setattr("app.services.pre_judge_runner._builtin_db_dsn",
                            lambda: "postgresql+psycopg2://ro:ro@db/readonly")
        return tmp_path

    def test_assertion_pass(self, workspace):
        with patch("app.scripts_builtin.db_assert._create_engine",
                   return_value=_FakeEngine([{"count": 3}])):
            entry = _run("db_assert", {"assertions": [
                {"name": "订单数", "sql": "SELECT count(*) AS count FROM orders",
                 "expected": {"scalar": 3}}]}, workspace)
        assert entry["status"] == STATUS_PASS
        assert entry["evidence"]["output"]["passed"] == 1

    def test_assertion_mismatch_is_fail(self, workspace):
        with patch("app.scripts_builtin.db_assert._create_engine",
                   return_value=_FakeEngine([{"count": 2}])):
            entry = _run("db_assert", {"assertions": [
                {"sql": "SELECT count(*) AS count FROM orders",
                 "expected": {"scalar": 3}}]}, workspace)
        assert entry["status"] == STATUS_FAIL
        assert entry["evidence"]["output"]["failed"] == 1

    def test_write_sql_rejected_by_readonly_guard(self, workspace):
        """只读约束保留：非 SELECT 语句在校验器通道同样被白名单拒绝 → fail"""
        with patch("app.scripts_builtin.db_assert._create_engine",
                   return_value=_FakeEngine([])) as mock_engine:
            entry = _run("db_assert", {"assertions": [
                {"sql": "DELETE FROM orders WHERE 1=1",
                 "expected": {"row_count": 0}}]}, workspace)
        assert entry["status"] == STATUS_FAIL
        assert "只读" in entry["evidence"]["output"]["results"][0]["error"]
        assert not mock_engine.connect.called  # 白名单拒绝在执行前

    def test_missing_dsn_is_error_not_platform_fallback(self, workspace, monkeypatch):
        monkeypatch.setattr("app.services.pre_judge_runner._builtin_db_dsn", lambda: None)
        entry = _run("db_assert", {"assertions": [
            {"sql": "SELECT 1", "expected": {"scalar": 1}}]}, workspace)
        assert entry["status"] == STATUS_ERROR
        assert "DB_ASSERT_READONLY_DSN" in entry["error"]


# ==================== 3. http_assert（egress 守卫） ====================

class _FakeResponse:
    def __init__(self, status_code=200, text='{"status":"paid"}', json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data if json_data is not None else json.loads(text)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class TestHttpAssert:
    def test_guard_rejects_private_target_without_request(self, tmp_path):
        """默认拒绝内网目标（deny by default），请求层未被触达"""
        with patch("requests.request") as mock_request:
            entry = _run("http_assert", {"url": "http://127.0.0.1:8080/callback"}, tmp_path)
        assert entry["status"] == STATUS_ERROR
        assert "egress" in entry["error"] or "拒绝" in entry["error"]
        assert not mock_request.called

    def test_allow_private_opt_in_passes_guard(self, tmp_path):
        """内网回调端点双闸：任务 allow_private=true + 管理员总闸开启才放行

        （M2 评审修正：放行决定权在部署管理员——HTTP_ASSERT_ALLOW_PRIVATE_TARGETS，
        与 M1 webhook 同口径；仅任务侧声明时拒绝的用例见
        tests/security/test_http_assert_ssrf.py）
        """
        from types import SimpleNamespace

        with patch("app.core.config.get_settings",
                   return_value=SimpleNamespace(HTTP_ASSERT_ALLOW_PRIVATE_TARGETS=True)), \
             patch("requests.request", return_value=_FakeResponse()) as mock_request:
            entry = _run("http_assert", {
                "url": "http://10.0.0.5:9000/callback", "allow_private": True,
                "expect_status": 200}, tmp_path)
        assert entry["status"] == STATUS_PASS
        assert mock_request.called

    def test_status_body_regex_jsonpath_matrix(self, tmp_path):
        cases = [
            ({"expect_status": 200}, STATUS_PASS),
            ({"expect_status": [201, 202]}, STATUS_FAIL),
            ({"expect_status": 200, "body_regex": "paid"}, STATUS_PASS),
            ({"expect_status": 200, "body_regex": "canceled"}, STATUS_FAIL),
            ({"expect_status": 200, "jsonpath": "status", "expected": "paid"}, STATUS_PASS),
            ({"expect_status": 200, "jsonpath": "status", "expected": "unpaid"}, STATUS_FAIL),
        ]
        for config, expected in cases:
            with patch("requests.request", return_value=_FakeResponse()):
                # 公网 IP 字面量目标：不依赖 DNS（守卫只做地址段校验，不发请求）
                entry = _run("http_assert", {
                    "url": "http://93.184.216.34/api/o/1", **config}, tmp_path)
            assert entry["status"] == expected, config

    def test_request_exception_is_error(self, tmp_path):
        import requests as _requests
        with patch("requests.request", side_effect=_requests.ConnectTimeout("down")):
            entry = _run("http_assert", {"url": "http://93.184.216.34/api"}, tmp_path)
        assert entry["status"] == STATUS_ERROR
        assert "请求失败" in entry["error"]


# ==================== 4. script 类型状态映射 ====================

class TestScriptVerifier:
    @pytest.mark.parametrize("stdout,finish,expected", [
        ("ok", "completed", STATUS_PASS),
        ("skip", "completed", STATUS_PASS),
        ("fail", "completed", STATUS_FAIL),
        ("fail", "assertion_failed", STATUS_FAIL),
        ("fail", "error", STATUS_ERROR),
        ("fail", "timeout", STATUS_ERROR),
    ])
    def test_status_mapping(self, stdout, finish, expected, tmp_path):
        fake_script = {"script_id": "pjs_x", "name": "vfy", "exec_type": "script",
                       "entrypoint": "main.py", "timeout_seconds": 60}
        with patch("app.services.pre_judge_runner._load_script", return_value=fake_script), \
             patch("app.services.pre_judge_runner.execute_script") as mock_exec:
            mock_exec.return_value = {
                "script_id": "pjs_x", "script_name": "vfy", "status": stdout,
                "finish_reason": finish, "duration_ms": 12,
                "output": {"ok": 1}, "files": {}, "summary": "s", "error": None,
            }
            entry = _run("script", {"script_id": "pjs_x"}, tmp_path)
        assert entry["status"] == expected, (stdout, finish)
        assert entry["evidence"]["kind"] == "verifier"

    def test_script_stdin_carries_model_output_and_kind(self, tmp_path):
        """stdin 契约：kind=verifier 标记 + 模型输出（M2 QE2-1 script 通道输入）"""
        captured = {}

        def fake_execute(script, stdin, timeout_seconds=None, network=False):
            captured.update(stdin)
            return {"script_id": script["script_id"], "script_name": "n",
                    "status": "ok", "finish_reason": "completed", "duration_ms": 1,
                    "output": None, "files": {}, "summary": "", "error": None}

        with patch("app.services.pre_judge_runner.execute_script", side_effect=fake_execute), \
             patch("app.services.pre_judge_runner._load_script",
                   return_value={"script_id": "pjs_x", "name": "n", "exec_type": "script",
                                 "entrypoint": "main.py", "timeout_seconds": 60}):
            entry = _run("script", {"script_id": "pjs_x"}, tmp_path, model_output="MO")
        assert entry["status"] == STATUS_PASS
        assert captured["kind"] == "verifier"
        assert captured["model_output"] == "MO"


# ==================== 5. run_verifiers 编排 ====================

class TestRunVerifiers:
    @pytest.fixture(autouse=True)
    def _isolated_script_runs(self, tmp_path, monkeypatch):
        """run_verifiers 自建工作区锚定临时目录（不落仓库 uploads/）"""
        monkeypatch.setattr("app.services.pre_judge_runner.SCRIPT_RUN_BASE", tmp_path / "script_runs")

    def test_no_verifiers_returns_none(self):
        assert run_verifiers(_make_task(None), {}, "mo") is None
        assert run_verifiers(_make_task([]), {}, "mo") is None

    def test_aggregation_and_abort(self):
        task = _make_task([
            {"type": "file_assert", "name": "文件校验",
             "config": {"assertions": [{"path": "nope.txt"}]}},
            {"type": "http_assert", "name": "回调校验",
             "config": {"url": "http://127.0.0.1/x"}},
        ])
        # 工作区锚定：run_verifiers 用 build_run_dir 自建目录，断言文件不存在 → fail
        result = run_verifiers(task, {"idx": 0, "_dataset_item": {"id": "d1"}}, "mo")
        assert result["status"] == STATUS_FAIL
        assert result["total"] == 2
        assert result["failed"] == 1 and result["errored"] == 1
        assert result["aborted"] is False  # 默认 on_fail=continue，两个都执行

    def test_abort_short_circuits_following_verifiers(self):
        task = _make_task([
            {"type": "file_assert", "name": "必过校验", "on_fail": "abort",
             "config": {"assertions": [{"path": "missing.bin"}]}},
            {"type": "http_assert", "name": "不应执行",
             "config": {"url": "http://127.0.0.1/x"}},
        ])
        with patch("requests.request") as mock_request:
            result = run_verifiers(task, {"idx": 3}, "mo")
        assert result["aborted"] is True
        assert result["total"] == 1  # 第二个校验器未执行
        assert "必过校验" in result["error"]
        assert not mock_request.called

    def test_all_pass(self, tmp_path):
        task = _make_task([
            {"type": "file_assert", "config": {"assertions": [
                {"path": "outputs/ok.txt"}]}},
        ])
        payload = {"idx": 0, "_dataset_item": {"id": "d1"}}
        # 预置工作区文件：build_run_dir monkeypatch 指向 tmp_path 保持测试隔离
        (tmp_path / "outputs").mkdir(exist_ok=True)
        (tmp_path / "outputs" / "ok.txt").write_text("ok", encoding="utf-8")
        with patch("app.services.pre_judge_runner.build_run_dir", return_value=tmp_path):
            result = run_verifiers(task, payload, "mo")
        assert result["status"] == STATUS_PASS
        assert result["passed"] == 1


# ==================== 6. 聚合与门禁接线 ====================

class TestAggregationAndGate:
    def test_aggregate_verification(self):
        rows = [
            {"success": True, "average_score": 9, "verification_result": {"status": "pass"}},
            {"success": True, "average_score": 8, "verification_result": {"status": "fail"}},
            {"success": False, "error": "模型调用失败"},  # 未到校验步骤，不计入分母
        ]
        stats = ResultAggregator._aggregate_verification(rows)
        assert stats == {"total": 2, "passed": 1, "failed": 1, "errored": 0,
                         "pass_rate": 0.5}

    def test_aggregate_without_verification_returns_none(self):
        assert ResultAggregator._aggregate_verification(
            [{"success": True, "average_score": 9}]) is None
        assert ResultAggregator._aggregate_verification([]) is None

    def test_aggregate_writes_pass_rate_only_when_verified(self):
        task = type("Task", (), {"max_score": 10, "evaluation_metrics": []})()
        rows = [
            {"success": True, "average_score": 9, "verification_result": {"status": "pass"}},
            {"success": True, "average_score": 8, "verification_result": {"status": "fail"}},
        ]
        aggregated = ResultAggregator(task).aggregate(rows)
        assert aggregated["verification_pass_rate"] == 0.5
        assert aggregated["verification"]["total"] == 2
        # item 明细携带行级证据（证据块展示用）
        assert aggregated["items"][0]["verification_result"]["status"] == "pass"

    def test_aggregate_legacy_rows_unchanged(self):
        """无校验行的聚合不写 verification 键（存量任务结构不变）"""
        task = type("Task", (), {"max_score": 10, "evaluation_metrics": []})()
        aggregated = ResultAggregator(task).aggregate(
            [{"success": True, "average_score": 9}])
        assert "verification" not in aggregated
        assert "verification_pass_rate" not in aggregated

    def test_gate_uses_verification_pass_rate(self):
        cfg = {"enabled": True, "thresholds": [
            {"metric": "verification_pass_rate", "op": ">=", "value": 1.0}]}
        # 聚合写入指标 → 可用（passed/failed 按阈值判定）
        assert evaluate_gate(cfg, {"verification_pass_rate": 1.0})["verdict"] == "passed"
        assert evaluate_gate(cfg, {"verification_pass_rate": 0.5})["verdict"] == "failed"
        # 任务未配置校验器 → 聚合无该键 → metric 不可用 → error（与 M1 口径一致）
        result = evaluate_gate(cfg, {"average_score": 9.0})
        assert result["verdict"] == "error"
        assert "不可用" in result["checks"][0]["note"]


# ==================== 7. 配置形态校验 ====================

class TestValidateShape:
    def test_valid_configs_pass(self):
        validate_verifier_shape([
            {"type": "script", "config": {"script_id": "pjs_x"}},
            {"type": "db_assert", "config": {"assertions": [
                {"sql": "SELECT 1", "expected": {"scalar": 1}}]}},
            {"type": "file_assert", "config": {"assertions": [
                {"path": "outputs/x.json", "mode": "jsonpath", "jsonpath": "a"}]}},
            {"type": "http_assert", "config": {"url": "https://x.example.com/cb"}},
        ])
        validate_verifier_shape(None)  # 未配置合法

    @pytest.mark.parametrize("verifiers,needle", [
        ([{"type": "sql_assert"}], "类型非法"),
        ([{"type": "script", "config": {}}], "script_id"),
        ([{"type": "script", "on_fail": "explode", "config": {"script_id": "x"}}], "on_fail"),
        ([{"type": "db_assert", "config": {"assertions": []}}], "assertions"),
        ([{"type": "db_assert", "config": {"assertions": [{"sql": "", "expected": {}}]}}], "sql"),
        ([{"type": "file_assert", "config": {"assertions": [{"path": "", "mode": "exists"}]}}], "path"),
        ([{"type": "file_assert", "config": {"assertions": [
            {"path": "a", "mode": "regex"}]}}], "pattern"),
        ([{"type": "http_assert", "config": {"url": "ftp://x/cb"}}], "http"),
        ("not-a-list", "数组"),
    ])
    def test_invalid_configs_rejected(self, verifiers, needle):
        with pytest.raises(ValueError, match=needle):
            validate_verifier_shape(verifiers)
