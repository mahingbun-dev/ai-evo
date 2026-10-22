"""M1 QE1-5 CLI 退出码矩阵与命令行为测试（fake client 注入，不出网）。

覆盖 aievo-cli 的核心约定：
1. run --wait 退出码矩阵：0=passed / 1=failed / 2=error（含未启用门禁、
   无判定终态、超时、触发失败、认证失败）；
2. --json 输出为单对象（verdict/checks 可机读）；
3. 非 --wait 触发成功退出 0；
4. config add/list：明文 Key 落盘 0600、回显打码、缺 profile 报错 2；
5. results -o json/csv；
6. datasets upload（json/excel 分流与错误格式）；
7. AievoClient 对 401/403/404/网络错误的统一映射（httpx.MockTransport）。
"""
import stat
import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

# CLI 是独立子包（cli/pyproject.toml），测试环境直接挂源码路径
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))

from aievo_cli import EXIT_ERROR, EXIT_FAILED, EXIT_PASSED  # noqa: E402
from aievo_cli import cli as cli_mod  # noqa: E402
from aievo_cli import config as cfg  # noqa: E402
from aievo_cli.client import AievoApiError, AievoClient  # noqa: E402

pytestmark = pytest.mark.unit

runner = CliRunner()

PASSED_GATE = {
    "task_id": "t1", "status": "completed", "execution_number": 2,
    "gate_enabled": True, "gate_mode": "all", "verdict": "passed",
    "gate_result": {"verdict": "passed", "checks": [
        {"metric": "total_score", "op": ">=", "value": 8.0,
         "actual": 8.5, "passed": True, "note": ""}]},
}
FAILED_GATE = {
    **PASSED_GATE, "verdict": "failed",
    "gate_result": {"verdict": "failed", "checks": [
        {"metric": "total_score", "op": ">=", "value": 9.0,
         "actual": 8.5, "passed": False, "note": ""}]},
}
ERROR_GATE = {
    **PASSED_GATE, "verdict": "error",
    "gate_result": {"verdict": "error", "checks": [
        {"metric": "dimension:acc", "op": ">=", "value": 8.0,
         "actual": None, "passed": False, "note": "指标不可用"}]},
}


class FakeClient:
    """替代 AievoClient：gate_states 逐次弹出，最后一个重复；其余按需抛错"""

    def __init__(self, *, gate_states=None, run_error=None, result=None,
                 dataset_id="ds-1", import_count=7):
        self.gate_states = gate_states or [PASSED_GATE]
        self.gate_calls = 0
        self.run_error = run_error
        self.result = result
        self.dataset_id = dataset_id
        self.import_count = import_count
        self.created = None
        self.imported = None
        self.closed = False

    def close(self):
        self.closed = True

    def run_task(self, task_id):
        if self.run_error:
            raise self.run_error
        return {"task_id": task_id, "status": "running", "execution_number": 3}

    def get_gate(self, task_id, execution_number=None):
        state = self.gate_states[min(self.gate_calls, len(self.gate_states) - 1)]
        self.gate_calls += 1
        return state

    def get_result(self, task_id):
        return self.result

    def get_result_version(self, task_id, execution_number):
        return self.result

    def create_dataset(self, name, description=None):
        self.created = (name, description)
        return {"id": self.dataset_id, "name": name, "question_count": self.import_count}

    def import_dataset_excel(self, dataset_id, file_path):
        self.imported = ("excel", dataset_id, str(file_path))
        return self.import_count

    def import_dataset_json(self, dataset_id, data):
        self.imported = ("json", dataset_id, data)
        return self.import_count


@pytest.fixture
def use_fake_client(monkeypatch):
    """注入 fake client，返回 holder 以便每个用例定制"""
    holder = {}

    def factory(profile):
        return holder["client"]

    monkeypatch.setattr(cli_mod, "_build_client", factory)
    return holder


def gate(status="running", verdict=None, enabled=True):
    return {"task_id": "t1", "status": status, "execution_number": 3,
            "gate_enabled": enabled, "gate_mode": "all",
            "verdict": verdict, "gate_result": None}


# ---------------- 1. run --wait 退出码矩阵 ----------------

@pytest.mark.parametrize("state,expected", [
    (PASSED_GATE, EXIT_PASSED),
    (FAILED_GATE, EXIT_FAILED),
    (ERROR_GATE, EXIT_ERROR),
])
def test_run_wait_verdict_matrix(use_fake_client, state, expected):
    use_fake_client["client"] = FakeClient(gate_states=[state])
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait"])
    assert result.exit_code == expected


def test_run_wait_json_output(use_fake_client):
    import json as jsonlib
    use_fake_client["client"] = FakeClient(gate_states=[FAILED_GATE])
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait", "--json"])
    assert result.exit_code == EXIT_FAILED
    payload = jsonlib.loads(result.stdout)
    assert payload["verdict"] == "failed"
    assert payload["checks"][0]["metric"] == "total_score"
    assert payload["execution_number"] == 2


def test_run_wait_gate_disabled(use_fake_client):
    """未启用门禁的任务 --wait 无法产出语义化退出码 → 错误 2"""
    use_fake_client["client"] = FakeClient(gate_states=[gate(enabled=False)])
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait"])
    assert result.exit_code == EXIT_ERROR


def test_run_wait_terminal_without_verdict(use_fake_client):
    """任务 failed/stopped 收尾仍无判定（兜底路径）→ 错误 2"""
    use_fake_client["client"] = FakeClient(gate_states=[gate(status="failed")])
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait"])
    assert result.exit_code == EXIT_ERROR


def test_run_wait_timeout(use_fake_client):
    """超时 → 错误 2（timeout=1s + interval=1s，第二次轮询触达 deadline）"""
    use_fake_client["client"] = FakeClient(gate_states=[gate()])
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait", "--timeout", "1"])
    assert result.exit_code == EXIT_ERROR


def test_run_trigger_error(use_fake_client):
    """触发失败（如任务正在运行 400）→ 错误 2"""
    use_fake_client["client"] = FakeClient(run_error=AievoApiError(400, "任务正在执行中"))
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait"])
    assert result.exit_code == EXIT_ERROR


def test_run_trigger_auth_error(use_fake_client):
    use_fake_client["client"] = FakeClient(run_error=AievoApiError(401, "认证失败"))
    result = runner.invoke(cli_mod.app, ["run", "t1"])
    assert result.exit_code == EXIT_ERROR


def test_run_no_wait_triggers_and_exits_zero(use_fake_client):
    use_fake_client["client"] = FakeClient(gate_states=[gate()])
    result = runner.invoke(cli_mod.app, ["run", "t1"])
    assert result.exit_code == EXIT_PASSED
    client = use_fake_client["client"]
    assert client.gate_calls == 0  # 非 --wait 不轮询
    assert client.closed


def test_run_wait_polls_until_verdict(use_fake_client):
    """先 running 再 passed：轮询推进而非首查即判"""
    use_fake_client["client"] = FakeClient(gate_states=[gate(), gate(), PASSED_GATE])
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait", "--interval", "1"])
    assert result.exit_code == EXIT_PASSED
    assert use_fake_client["client"].gate_calls == 3


def test_run_missing_profile_exits_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AIEVO_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(cli_mod.app, ["run", "t1", "--wait"])
    assert result.exit_code == EXIT_ERROR


# ---------------- 2. config ----------------

def test_config_add_persists_and_masks(monkeypatch, tmp_path):
    monkeypatch.setenv("AIEVO_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(cli_mod.app, [
        "config", "add", "https://aievo.example.com/", "aievo_sk_abc123def456"])
    assert result.exit_code == 0
    path = cfg.config_path()
    assert path.exists()
    # 明文 Key 落盘：仅属主可读写
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    profile = cfg.get_profile()
    assert profile["url"] == "https://aievo.example.com"  # 尾斜杠归一化
    assert profile["api_key"] == "aievo_sk_abc123def456"
    # list 回显打码
    listing = runner.invoke(cli_mod.app, ["config", "list"])
    assert "aievo_sk_abc****" in listing.stdout
    assert "abc123def456" not in listing.stdout


def test_config_multiple_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("AIEVO_CONFIG_DIR", str(tmp_path / "cfg"))
    runner.invoke(cli_mod.app, ["config", "add", "https://a.example.com", "aievo_sk_k1"])
    runner.invoke(cli_mod.app, ["config", "add", "https://b.example.com", "aievo_sk_k2",
                                "--profile", "staging"])
    assert cfg.get_profile("staging")["url"] == "https://b.example.com"
    assert cfg.get_profile()["url"] == "https://a.example.com"


# ---------------- 3. results ----------------

SAMPLE_RESULT = {
    "task_id": "t1", "status": "completed",
    "result": {
        "total": 3, "success_count": 2, "failed_count": 1,
        "items": [
            {"idx": 1, "success": True, "row_status": "success",
             "average_score": 8.5, "feedback": "好"},
            {"idx": 2, "success": True, "row_status": "success",
             "average_score": 7.0, "feedback": "一般"},
        ],
        "failed_items": [
            {"idx": 3, "success": False, "row_status": "judge_failed",
             "average_score": None, "feedback": "裁判失败"},
        ],
    },
}


def test_results_json(use_fake_client):
    import json as jsonlib
    use_fake_client["client"] = FakeClient(result=SAMPLE_RESULT)
    result = runner.invoke(cli_mod.app, ["results", "t1", "-o", "json"])
    assert result.exit_code == 0
    assert jsonlib.loads(result.stdout)["result"]["total"] == 3


def test_results_csv_includes_failed_rows(use_fake_client):
    """csv 输出行级明细：items + failed_items 一并按 idx 排序"""
    use_fake_client["client"] = FakeClient(result=SAMPLE_RESULT)
    result = runner.invoke(cli_mod.app, ["results", "t1", "-o", "csv"])
    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "idx,success,row_status,average_score,feedback"
    assert lines[1] == "1,True,success,8.5,好"
    assert lines[3] == "3,False,judge_failed,,裁判失败"


def test_results_csv_empty_exits_error(use_fake_client):
    use_fake_client["client"] = FakeClient(result={"task_id": "t1", "status": "running", "result": {}})
    result = runner.invoke(cli_mod.app, ["results", "t1", "-o", "csv"])
    assert result.exit_code == EXIT_ERROR


def test_results_bad_format(use_fake_client):
    use_fake_client["client"] = FakeClient(result=SAMPLE_RESULT)
    result = runner.invoke(cli_mod.app, ["results", "t1", "-o", "xml"])
    assert result.exit_code == EXIT_ERROR


# ---------------- 4. datasets upload ----------------

def test_datasets_upload_json(use_fake_client, tmp_path):
    use_fake_client["client"] = FakeClient()
    data_file = tmp_path / "set.json"
    data_file.write_text('[{"question": "1+1", "ref_answer": "2"}]', encoding="utf-8")
    result = runner.invoke(cli_mod.app, [
        "datasets", "upload", str(data_file), "--name", "回归集"])
    assert result.exit_code == 0
    client = use_fake_client["client"]
    assert client.created == ("回归集", None)
    kind, dataset_id, data = client.imported
    assert kind == "json" and dataset_id == "ds-1"
    assert data == [{"question": "1+1", "ref_answer": "2"}]
    assert "导入 7 条" in result.stdout


def test_datasets_upload_json_wrapper(use_fake_client, tmp_path):
    """{"data": [...]} 包装格式同样接受"""
    use_fake_client["client"] = FakeClient()
    data_file = tmp_path / "wrap.json"
    data_file.write_text('{"data": [{"q": "hi"}]}', encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["datasets", "upload", str(data_file)])
    assert result.exit_code == 0
    assert use_fake_client["client"].imported[2] == [{"q": "hi"}]
    assert use_fake_client["client"].created[0] == "wrap"  # 默认名取文件主干


def test_datasets_upload_excel(use_fake_client, tmp_path):
    use_fake_client["client"] = FakeClient()
    xlsx = tmp_path / "set.xlsx"
    xlsx.write_bytes(b"fake-xlsx-bytes")
    result = runner.invoke(cli_mod.app, ["datasets", "upload", str(xlsx)])
    assert result.exit_code == 0
    kind, dataset_id, path = use_fake_client["client"].imported
    assert kind == "excel" and path.endswith("set.xlsx")


def test_datasets_upload_unsupported_type(use_fake_client, tmp_path):
    use_fake_client["client"] = FakeClient()
    bad = tmp_path / "set.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    result = runner.invoke(cli_mod.app, ["datasets", "upload", str(bad)])
    assert result.exit_code == EXIT_ERROR
    assert use_fake_client["client"].created is None  # 未创建数据集即拒绝


# ---------------- 5. AievoClient 错误映射（MockTransport，不出网） ----------------

def test_client_maps_401_403_404():
    def handler(request):
        path = request.url.path
        if path.endswith("/run"):
            return httpx.Response(401, json={"detail": "unauthorized"})
        if path.endswith("/gate"):
            return httpx.Response(403, json={"detail": "API Key 缺少所需 scope"})
        return httpx.Response(404, json={"detail": "not found"})

    client = AievoClient("https://aievo.example.com", "aievo_sk_x",
                         transport=httpx.MockTransport(handler))
    with pytest.raises(AievoApiError) as e1:
        client.run_task("t1")
    assert e1.value.status_code == 401 and "认证失败" in e1.value.message
    with pytest.raises(AievoApiError) as e2:
        client.get_gate("t1")
    assert e2.value.status_code == 403 and "scope" in e2.value.message
    with pytest.raises(AievoApiError) as e3:
        client.get_result("nope")
    assert e3.value.status_code == 404


def test_client_maps_network_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = AievoClient("https://down.example.com", "aievo_sk_x",
                         transport=httpx.MockTransport(handler))
    with pytest.raises(AievoApiError) as e:
        client.run_task("t1")
    assert e.value.status_code is None
    assert "网络错误" in e.value.message


def test_client_gate_request_shape():
    """门禁轮询请求：路径与 execution_number 查询参数组装正确"""
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json=PASSED_GATE)

    client = AievoClient("https://aievo.example.com/", "aievo_sk_x",
                         transport=httpx.MockTransport(handler))
    assert client.get_gate("t1", 3)["verdict"] == "passed"
    method, url = seen[0]
    assert method == "GET"
    assert url == "https://aievo.example.com/api/v1/evaluation/tasks/t1/gate?execution_number=3"
    assert "Bearer aievo_sk_x" in request_auth(client)


def request_auth(client):
    return client._client.headers["Authorization"]
