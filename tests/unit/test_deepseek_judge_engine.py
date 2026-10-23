"""DeepSeek 裁判任务流接通测试（M8 QE8-2）

覆盖方案 §M8「DeepSeek 裁判接通」主链路：
1. 执行器分发：judge_engine=deepseek_agent → DeepSeekJudgeAgent（不触
   ClaudeCodeJudgeAgent / JudgeLangGraphAgent）；claude_agent/graph 分发
   回归不回归被破坏；
2. DeepSeekJudgeAgent：会话内联/截断/文件缺失兜底、失败标记口径
   （score=None + judge_failed + fail_reason）、全局配置缺失 RuntimeError；
3. 任务创建校验：deepseek_agent 合法、未知引擎 ValueError；
4. 双引擎对拍报告：样本锚定既有输出、逐行分差/汇总口径、无行级结果
   与引擎配置缺失的前置校验；API 面（viewer 403 / 400 / 200 打桩）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock, patch

from app.api.evaluation_task_api import router as task_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.dataset_models import EvaluationDataset, EvaluationDataV2
from app.models.task_models import EvaluationResult, EvaluationTask
from app.models.user_models import User
from app.services.deepseek_judge_agent import DeepSeekJudgeAgent
from app.services.evaluation_executor import EvaluationExecutor
from app.services.evaluation_task_service import EvaluationTaskService
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.jwt_service import create_token
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit


# ======================================================================
# 1. 执行器分发（judge_engine=deepseek_agent）
# ======================================================================

def _make_task(evaluation_type="judge", judge_engine="graph", judge_count=2):
    return type("Task", (), {
        "id": "task_test",
        "evaluation_type": evaluation_type,
        "judge_engine": judge_engine,
        "judge_count": judge_count,
        "model_config": {"provider": "openai"},
        "model_provider": None,
        "max_score": 10,
        "evaluation_metrics": [{"name": "维度A"}],
        "judge_variable_config": {},
        "evaluation_targets": None,
        "runtime_variables": {},
    })()


class TestEngineDispatch:
    """_evaluate_single 步骤3 分派验证（mock 三个引擎）"""

    def _run_evaluate(self, task, payload):
        executor = EvaluationExecutor(max_workers=1, task_id="task_test")
        with patch("app.services.prompt_builder.PromptBuilder") as mock_pb, \
             patch("app.services.variable_injector.VariableInjector") as mock_vi, \
             patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent") as mock_cc, \
             patch("app.services.deepseek_judge_agent.DeepSeekJudgeAgent") as mock_dsh, \
             patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent") as mock_lg:
            pb_instance = mock_pb.return_value
            pb_instance.build_user_prompt_from_judge_vars.return_value = "user prompt"
            pb_instance._build_system_prompt.return_value = "system prompt"
            vi_instance = mock_vi.return_value
            vi_instance.build_judge_payload.return_value = {"user_input": "x", "_dataset_item": {}}
            vi_instance.script_output_missing = []

            mock_cc.return_value.run_parallel.return_value = [
                {"score": 7, "feedback": "cc", "dimension_scores": {}}]
            mock_dsh.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "dsh", "dimension_scores": {}}]
            mock_lg.return_value.run_parallel.return_value = [
                {"score": 5, "feedback": "graph", "dimension_scores": {}}]
            result = executor._evaluate_single(payload, task)
        return result, mock_cc, mock_dsh, mock_lg

    def test_deepseek_agent_engine(self):
        """judge_engine=deepseek_agent 时走 DSH Agent，不触 CC 与图引擎"""
        task = _make_task(judge_engine="deepseek_agent")
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "模型输出", "chain_results": []}
            result, mock_cc, mock_dsh, mock_lg = self._run_evaluate(
                task, {"idx": 0, "user_input": "问题", "_dataset_item": {}})
        assert result["success"] is True
        assert mock_dsh.return_value.run_parallel.called
        assert not mock_cc.return_value.run_parallel.called
        assert not mock_lg.return_value.run_parallel.called
        assert result["average_score"] == 9
        # 分发参数与 CC 分支同口径（system/user prompt + judge_count）
        kwargs = mock_dsh.return_value.run_parallel.call_args.kwargs
        assert kwargs["judge_count"] == 2
        assert kwargs["system_prompt"] and kwargs["user_prompt"]

    def test_claude_and_graph_dispatch_untouched(self):
        """既有分发回归：claude_agent 走 CC、graph 走图引擎"""
        with patch("app.services.model_caller.ModelCallerSync") as mock_caller:
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "模型输出", "chain_results": []}
            result_cc, mock_cc, mock_dsh, _ = self._run_evaluate(
                _make_task(judge_engine="claude_agent"),
                {"idx": 0, "user_input": "问题", "_dataset_item": {}})
            assert result_cc["average_score"] == 7
            assert mock_cc.return_value.run_parallel.called
            assert not mock_dsh.return_value.run_parallel.called

            result_lg, mock_cc2, _, mock_lg = self._run_evaluate(
                _make_task(judge_engine="graph"),
                {"idx": 0, "user_input": "问题", "_dataset_item": {}})
            assert result_lg["success"] is True
            assert mock_lg.return_value.run_parallel.called


# ======================================================================
# 2. DeepSeekJudgeAgent 单元（prompt 构建 / 失败口径 / 配置缺失）
# ======================================================================

class TestDeepSeekJudgeAgent:
    def _task(self):
        return type("Task", (), {
            "id": "t1", "evaluation_metrics": [{"name": "维度A"}], "max_score": 10,
        })()

    def test_final_prompt_inlines_session_and_protocol(self, tmp_path):
        session = tmp_path / "session.md"
        session.write_text("# 会话\n用户：你好", encoding="utf-8")
        agent = DeepSeekJudgeAgent(self._task())
        prompt = agent._build_final_prompt("user prompt", str(session))
        assert prompt.startswith("user prompt")
        assert "# 会话\n用户：你好" in prompt          # 会话全文内联
        assert "只包含一个 JSON 对象" in prompt          # 输出协议
        assert "维度A" in prompt                        # 维度量程说明

    def test_final_prompt_truncates_overlong_session(self, tmp_path):
        from app.services.deepseek_judge_agent import SESSION_INLINE_LIMIT_CHARS
        session = tmp_path / "big.md"
        session.write_text("x" * (SESSION_INLINE_LIMIT_CHARS + 100), encoding="utf-8")
        agent = DeepSeekJudgeAgent(self._task())
        prompt = agent._build_final_prompt("u", str(session))
        assert "已截断" in prompt
        assert len(prompt) < SESSION_INLINE_LIMIT_CHARS + 5000

    def test_final_prompt_missing_session_file_tolerated(self, tmp_path):
        agent = DeepSeekJudgeAgent(self._task())
        prompt = agent._build_final_prompt("user prompt", str(tmp_path / "nope.md"))
        assert "user prompt" in prompt and "会话记录" not in prompt

    def test_run_parallel_failure_marks_judge_failed(self):
        """适配器抛错 → score=None + judge_failed + fail_reason（W2-1 口径）"""
        with patch(
            "app.services.deepseek_harness_config_service.DeepSeekHarnessConfigService.get_active_config",
            return_value={"model": "deepseek-v4-flash", "timeout_seconds": 5},
        ), patch("app.services.deepseek_adapter.DeepSeekAdapter") as mock_adapter:
            mock_adapter.return_value.run_prompt.side_effect = RuntimeError("boom")
            agent = DeepSeekJudgeAgent(self._task())
            results = agent.run_parallel("sys", "user", None, judge_count=2)
        assert len(results) == 2
        for r in results:
            assert r["score"] is None and r["judge_failed"] is True
            assert r["fail_reason"] == "llm_error"
            assert "DeepSeek 裁判执行失败" in r["feedback"]

    def test_run_parallel_config_missing_raises(self):
        with patch(
            "app.services.deepseek_harness_config_service.DeepSeekHarnessConfigService.get_active_config",
            return_value=None,
        ):
            agent = DeepSeekJudgeAgent(self._task())
            with pytest.raises(RuntimeError, match="未配置 DeepSeek Harness"):
                agent.run_parallel("sys", "user", None, judge_count=1)


# ======================================================================
# 3. 任务创建校验（deepseek_agent 合法）
# ======================================================================

class TestCreateTaskValidation:
    def _service(self, session):
        return EvaluationTaskService(session)

    def test_deepseek_agent_accepted_and_bogus_rejected(self, stack):
        with stack.session() as db:
            service = self._service(db)
            task = service.create_task(
                name="dsh 任务", evaluation_type="judge", judge_engine="deepseek_agent",
                model_output_source="none",
                evaluation_targets=[{"id": "t1", "name": "模型输出", "source": "model_output"}],
                created_by="eng",
            )
            assert task.judge_engine == "deepseek_agent"
            with pytest.raises(ValueError, match="不支持的裁判引擎"):
                service.create_task(
                    name="坏任务", evaluation_type="judge", judge_engine="gpt_agent",
                    model_output_source="none",
                    evaluation_targets=[{"id": "t1", "name": "模型输出", "source": "model_output"}],
                )


# ======================================================================
# 3.5 裁判治理快照（deepseek_agent 引擎归因到 DSH 全局配置，掩码凭据）
# ======================================================================

class TestJudgeConfigSnapshot:
    def test_snapshot_uses_dsh_global_config(self, monkeypatch):
        from app.services.config_snapshot import build_judge_config_snapshot

        monkeypatch.setattr(
            "app.services.deepseek_harness_config_service.DeepSeekHarnessConfigService.get_active_config",
            classmethod(lambda cls, use_cache=True: {
                "provider": "deepseek-official", "model": "deepseek-v4-flash",
                "protocol": "deepseek", "base_url": "https://api.deepseek.com",
                "api_key": "sk-test-1234567890abcdef",
            }),
        )
        task = type("Task", (), {
            "evaluation_type": "judge", "judge_engine": "deepseek_agent",
            "judge_prompt": "p", "score_details": None,
            "evaluation_metrics": [{"name": "维度A"}], "max_score": 10,
            "judge_guard_disabled": False, "judge_canary": False,
        })()
        snapshot = build_judge_config_snapshot(task)
        assert snapshot["engine"] == "deepseek_agent"
        assert snapshot["judge_model"]["source"] == "deepseek_harness_global"
        assert snapshot["judge_model"]["model_type"] == "deepseek-v4-flash"
        # 掩码纪律：快照不落明文 key（前3后3脱敏）
        assert "sk-test-1234567890abcdef" not in str(snapshot)
        assert snapshot["judge_model"]["api_key_masked"].startswith("sk-")

    def test_snapshot_config_missing_marker(self, monkeypatch):
        from app.services.config_snapshot import build_judge_config_snapshot

        monkeypatch.setattr(
            "app.services.deepseek_harness_config_service.DeepSeekHarnessConfigService.get_active_config",
            classmethod(lambda cls, use_cache=True: None),
        )
        task = type("Task", (), {
            "evaluation_type": "judge", "judge_engine": "deepseek_agent",
            "judge_prompt": None, "score_details": None,
            "evaluation_metrics": [], "max_score": 10,
            "judge_guard_disabled": False, "judge_canary": False,
        })()
        snapshot = build_judge_config_snapshot(task)
        assert snapshot["judge_model"]["config_missing"] is True


# ======================================================================
# 4. 双引擎对拍（judge_comparison_service + API）
# ======================================================================

@pytest.fixture
def stack(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.middleware.auth_middleware.SessionLocal", TestSession)

    with TestSession() as db:
        db.add(User(user_code="eng", username="eng", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT"))
        db.add(User(user_code="viewer", username="viewer", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="DEFAULT",
                    role="viewer"))
        # 对拍样本任务：judge 类型 + 数据集 3 行 + 第 1 次执行 3 行级结果
        db.add(EvaluationTask(
            id="tcmp", name="对拍任务", evaluation_type="judge",
            judge_engine="claude_agent", max_score=10, execution_count=1,
            team_code="DEFAULT", status="completed", dataset_id="dscmp",
            evaluation_targets=[{"id": "t1", "name": "模型输出", "source": "model_output"}]))
        db.add(EvaluationDataset(id="dscmp", name="对拍数据集", team_code="DEFAULT"))
        for i, score in enumerate([9.0, 6.0, 2.0]):
            db.add(EvaluationDataV2(
                id=f"data2_{i}", dataset_id="dscmp", index=i,
                data={"user_input": f"q{i}", "answer": f"a{i}"}))
            db.add(EvaluationResult(
                task_id="tcmp", execution_number=1, data_index=i, status="completed",
                model_output=f"模型输出{i}",
                aggregate={"average_score": score}))
        db.commit()

    app = FastAPI()
    app.include_router(task_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()
    return type("Stack", (), {
        "app": app, "session": TestSession,
        "jwt_eng": create_token("eng", "eng"),
        "jwt_viewer": create_token("viewer", "viewer"),
    })()


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


_BOTH_ENGINES = {
    "claude_agent": {"model": "claude-sonnet"},
    "deepseek_agent": {"model": "deepseek-v4-flash", "protocol": "deepseek"},
}


class TestComparisonService:
    def _patched_engines(self, monkeypatch, cc_dsh_scores):
        monkeypatch.setattr(
            "app.services.judge_comparison_service._engine_configs",
            lambda: dict(_BOTH_ENGINES))
        calls = []

        def fake_run_engine(task, engine, system_prompt, user_prompt, langfuse_host):
            calls.append((engine, system_prompt, user_prompt))
            return [{"score": cc_dsh_scores[engine], "feedback": "ok",
                     "dimension_scores": {}}]

        monkeypatch.setattr(
            "app.services.judge_comparison_service._run_engine", fake_run_engine)
        return calls

    def test_report_rows_and_summary(self, stack, monkeypatch):
        """逐行分差与汇总口径：3 行 (8,6)/(7,6)/(5,2) → 分差 2/1/3"""
        monkeypatch.setattr(
            "app.services.judge_comparison_service._engine_configs",
            lambda: dict(_BOTH_ENGINES))
        scores = {"claude_agent": [8, 7, 5], "deepseek_agent": [6, 6, 2]}
        seq = {"claude_agent": iter(scores["claude_agent"]),
               "deepseek_agent": iter(scores["deepseek_agent"])}

        def fake_run_engine(task, engine, system_prompt, user_prompt, langfuse_host):
            return [{"score": next(seq[engine]), "feedback": "ok"}]

        monkeypatch.setattr(
            "app.services.judge_comparison_service._run_engine", fake_run_engine)

        from app.services.judge_comparison_service import generate_judge_comparison
        with stack.session() as db:
            task = db.get(EvaluationTask, "tcmp")
            report = generate_judge_comparison(db, task, sample_count=5)

        assert report["task_id"] == "tcmp"
        assert report["summary"]["sample_count"] == 3
        assert report["summary"]["both_scored"] == 3
        assert [r["score_diff"] for r in report["rows"]] == [2, 1, 3]
        assert report["summary"]["mean_abs_diff"] == 2.0
        assert report["summary"]["max_abs_diff"] == 3.0
        # 容差 1 分内：3 行中 1 行 → 1/3
        assert report["summary"]["within_tolerance_rate"] == pytest.approx(1 / 3, abs=1e-3)
        # 引擎摘要进报告（对拍报告可归因到配置）
        assert report["engines"]["deepseek_agent"]["model"] == "deepseek-v4-flash"

    def test_sample_count_capped_and_rows_anchored_to_existing_outputs(self, stack, monkeypatch):
        calls = self._patched_engines(monkeypatch, {"claude_agent": 8, "deepseek_agent": 8})
        from app.services.judge_comparison_service import generate_judge_comparison
        with stack.session() as db:
            task = db.get(EvaluationTask, "tcmp")
            report = generate_judge_comparison(db, task, sample_count=99)  # 上限 10
        assert report["summary"]["sample_count"] == 3  # 实际只有 3 行样本
        assert len(calls) == 6  # 3 行 × 2 引擎
        # 两引擎每行拿到相同 prompt（分差只归因引擎）
        for i in range(3):
            assert calls[2 * i][1] == calls[2 * i + 1][1]  # system_prompt 相同
            assert calls[2 * i][2] == calls[2 * i + 1][2]  # user_prompt 相同

    def test_no_exec_rows_rejected(self, stack, monkeypatch):
        self._patched_engines(monkeypatch, {"claude_agent": 8, "deepseek_agent": 8})
        with stack.session() as db:
            db.add(EvaluationTask(
                id="tnone", name="未执行", evaluation_type="judge",
                max_score=10, execution_count=0, team_code="DEFAULT",
                status="pending",
                evaluation_targets=[{"id": "t1", "name": "模型输出", "source": "model_output"}]))
            db.commit()
            from app.services.judge_comparison_service import (
                JudgeComparisonError, generate_judge_comparison)
            with pytest.raises(JudgeComparisonError, match="先完整执行一次任务"):
                generate_judge_comparison(db, db.get(EvaluationTask, "tnone"))

    def test_missing_engine_config_rejected(self, stack, monkeypatch):
        monkeypatch.setattr(
            "app.services.judge_comparison_service._engine_configs",
            lambda: {"claude_agent": {"model": "claude-sonnet"}, "deepseek_agent": None})
        from app.services.judge_comparison_service import (
            JudgeComparisonError, generate_judge_comparison)
        with stack.session() as db:
            with pytest.raises(JudgeComparisonError, match="DeepSeek Harness"):
                generate_judge_comparison(db, db.get(EvaluationTask, "tcmp"))

    def test_non_judge_task_rejected(self, stack, monkeypatch):
        from app.services.judge_comparison_service import (
            JudgeComparisonError, generate_judge_comparison)
        with stack.session() as db:
            db.add(EvaluationTask(
                id="tsr", name="成功率任务", evaluation_type="success_rate",
                max_score=10, execution_count=1, team_code="DEFAULT", status="completed"))
            db.commit()
            with pytest.raises(JudgeComparisonError, match="使用裁判的任务"):
                generate_judge_comparison(db, db.get(EvaluationTask, "tsr"))


class TestComparisonApi:
    def test_viewer_forbidden(self, client, stack):
        resp = client.post("/api/v1/evaluation/tasks/tcmp/judge-comparison",
                           headers=auth(stack.jwt_viewer), json={"sample_count": 3})
        assert resp.status_code == 403

    def test_missing_engine_configs_maps_400(self, client, stack, monkeypatch):
        # 未配置引擎全局配置（干净 sqlite 无 CC/DSH 配置行）→ 400 + 明确指引
        resp = client.post("/api/v1/evaluation/tasks/tcmp/judge-comparison",
                           headers=auth(stack.jwt_eng), json={"sample_count": 3})
        assert resp.status_code == 400
        assert "模型配置页" in resp.json()["detail"]

    def test_success_with_stubbed_service(self, client, stack, monkeypatch):
        fake = {"task_id": "tcmp", "task_name": "对拍任务", "execution_number": 1,
                "engines": _BOTH_ENGINES,
                "summary": {"sample_count": 3, "both_scored": 3, "claude_failed": 0,
                            "deepseek_failed": 0, "mean_abs_diff": 0.5,
                            "max_abs_diff": 1.0, "within_tolerance_rate": 1.0,
                            "tolerance": 1.0},
                "rows": []}
        monkeypatch.setattr(
            "app.services.judge_comparison_service.generate_judge_comparison",
            lambda db, task, sample_count=5, langfuse_host="": fake)
        resp = client.post("/api/v1/evaluation/tasks/tcmp/judge-comparison",
                           headers=auth(stack.jwt_eng), json={"sample_count": 3})
        assert resp.status_code == 200, resp.text
        assert resp.json()["summary"]["mean_abs_diff"] == 0.5

    def test_sample_count_bounds(self, client, stack):
        resp = client.post("/api/v1/evaluation/tasks/tcmp/judge-comparison",
                           headers=auth(stack.jwt_eng), json={"sample_count": 11})
        assert resp.status_code == 422  # pydantic ge/le 校验
