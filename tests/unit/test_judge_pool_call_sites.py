"""断言 graph 引擎的裁判调用点都经过并发池，且裁判线程池继承任务上下文

覆盖（Task 8）：
- app/services/judge_graph_nodes.py::_call_llm
- app/services/judge_client.py::_call_judge_model（首次 + 兜底重试）
- app/services/rag_evaluator.py::LLMRagEvaluator._call_llm
- app/services/consistency_evaluator.py::_judge_evaluate
- app/services/platform_llm_wrapper.py::_generate（generate/batch/a_generate 汇入点）
- app/services/response_parser.py 阶段4 LLM 兜底解析
- judge 线程池（langgraph / claude code / plugin evaluator）的任务上下文继承

覆盖（Task 9）：
- app/services/harness_adapter.py::HarnessAdapter.run_prompt（CC → claude_agent、DS → harness）
- app/services/plugin_evaluator.py::PluginEvaluator.evaluate / evaluate_trace（plugin）
"""
import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


class _Ctx:
    """judge_pool_slot 的最小替身（__enter__/__exit__ 协议），进出各留一道痕"""

    def __init__(self, calls, events, engine, meta):
        self.calls = calls
        self.events = events
        self.engine = engine
        self.meta = meta

    def __enter__(self):
        self.calls.append((self.engine, self.meta))
        self.events.append(("enter", self.engine, self.meta))
        return None

    def __exit__(self, *exc):
        self.events.append(("exit", self.engine, self.meta))
        return False


def _recording_slot(monkeypatch, module, calls=None, events=None):
    """把 module 命名空间里的 judge_pool_slot 换成记录器

    - calls：在 `__enter__` 时追加（不是构造时），只断言它证明不了模型调用
      发生在额度窗口内 —— 把调用移出 with 块同样会留下记录；
    - events：enter/exit 时间线，被测代码把真实调用也 append 进来，
      据此断言调用确实夹在 enter 与 exit 之间。
    """
    calls = [] if calls is None else calls
    events = [] if events is None else events

    def slot(engine, meta=None):
        return _Ctx(calls, events, engine, meta)

    monkeypatch.setattr(module, "judge_pool_slot", slot)
    return calls


# ---------------------------------------------------------------- 调用点接入

def test_judge_graph_nodes_enters_slot(monkeypatch):
    from app.services import judge_graph_nodes as mod

    calls = _recording_slot(monkeypatch, mod)
    monkeypatch.setattr(
        mod,
        "ModelCallerSync",
        lambda: SimpleNamespace(call=lambda *a, **k: {"success": True, "content": "{}"}),
    )

    result = mod._call_llm("hi", {"judge_model_config": {"provider": "openai"}})

    assert result == "{}"
    assert calls == [("graph", {"call_site": "judge_graph_nodes"})]


def _make_judge_client():
    from app.services.judge_client import JudgeClient

    task = SimpleNamespace(
        judge_model="m",
        judge_prompt="",
        max_score=10,
        evaluation_metrics=[],
        judge_model_config={"provider": "openai"},
    )
    return JudgeClient(task)


def test_judge_client_enters_slot(monkeypatch):
    import app.services.model_caller as mc

    from app.services import judge_client as mod

    calls = _recording_slot(monkeypatch, mod)
    monkeypatch.setattr(
        mc.ModelCallerSync, "call", staticmethod(lambda *a, **k: {"success": True, "content": "ok"})
    )

    client = _make_judge_client()

    assert client._call_judge_model("sys", "user") == "ok"
    assert calls == [("graph", {"call_site": "judge_client"})]


def test_judge_client_retry_enters_slot(monkeypatch):
    import app.services.model_caller as mc

    from app.services import judge_client as mod

    calls = _recording_slot(monkeypatch, mod)
    results = [
        {"success": False, "error": "maximum context length exceeded"},
        {"success": True, "content": "retried"},
    ]

    monkeypatch.setattr(mc.ModelCallerSync, "call", staticmethod(lambda *a, **k: results.pop(0)))

    client = _make_judge_client()

    assert client._call_judge_model("sys", "user") == "retried"
    assert calls == [
        ("graph", {"call_site": "judge_client"}),
        ("graph", {"call_site": "judge_client_retry"}),
    ]


def test_rag_evaluator_enters_slot(monkeypatch):
    import app.services.model_caller as mc

    from app.services import rag_evaluator as mod

    calls = _recording_slot(monkeypatch, mod)
    monkeypatch.setattr(
        mc.ModelCallerSync, "call", staticmethod(lambda *a, **k: {"success": True, "content": "x"})
    )

    evaluator = mod.LLMRagEvaluator(llm_config={"provider": "openai"})

    assert evaluator._call_llm("prompt") == "x"
    assert calls == [("graph", {"call_site": "rag_evaluator"})]


def test_consistency_evaluator_enters_slot(monkeypatch):
    from app.services import consistency_evaluator as mod

    calls = _recording_slot(monkeypatch, mod)
    evaluator = mod.ConsistencyEvaluator(
        model_config={"provider": "openai"}, evaluation_metrics=[]
    )

    async def fake_call_model(messages, config, **kwargs):
        return {"success": True, "content": '{"score": 8, "feedback": "ok"}'}

    monkeypatch.setattr(evaluator.engine, "_call_model", fake_call_model)

    judge_assessment, _combined = asyncio.run(
        evaluator._judge_evaluate(["回复1"], [])
    )

    assert judge_assessment["综合评分"] == 8
    assert calls == [("graph", {"call_site": "consistency_evaluator"})]


def test_platform_llm_wrapper_enters_slot_once_per_model_call(monkeypatch):
    """generate / batch_generate / a_generate 都汇入 _generate，只在最内层入池一次"""
    from app.services import platform_llm_wrapper as mod

    calls = _recording_slot(monkeypatch, mod)
    wrapper = mod.PlatformLLMWrapper({"provider": "openai"})
    monkeypatch.setattr(wrapper, "_call_openai", lambda prompt: "text")

    assert wrapper.generate("p1") == "text"
    assert calls == [("graph", {"call_site": "platform_llm_wrapper"})]

    calls.clear()
    assert wrapper.batch_generate(["a", "b"]) == ["text", "text"]
    # 两个 prompt → 两次模型调用 → 两层额度（不因 generate/batch 叠加成 4 层）
    assert calls == [("graph", {"call_site": "platform_llm_wrapper"})] * 2

    calls.clear()
    assert asyncio.run(wrapper.a_generate("p")) == "text"
    assert calls == [("graph", {"call_site": "platform_llm_wrapper"})]

    calls.clear()
    # 原方法体留在 _generate_unpooled，直接调用不再入池（避免 with 外层自套递归）
    assert wrapper._generate_unpooled("p") == "text"
    assert calls == []


def test_response_parser_stage4_enters_slot(monkeypatch):
    import app.services.model_caller as mc

    from app.services import response_parser as mod

    calls = _recording_slot(monkeypatch, mod)
    monkeypatch.setattr(
        mc.ModelCallerSync,
        "call",
        staticmethod(lambda *a, **k: {"success": True, "content": '{"score": 7}'}),
    )

    parser = mod.JudgeResponseParser(judge_config={"provider": "openai"})

    assert parser._stage4_ai_correction("raw") == {"score": 7}
    assert calls == [("graph", {"call_site": "response_parser"})]


def test_pool_disabled_call_site_is_unchanged(monkeypatch):
    """JUDGE_POOL_ENABLED=false：调用点与接入前完全一致（完全不动并发池）"""
    from app.services import judge_graph_nodes as mod
    from app.services.judge_pool import slot as slot_mod

    monkeypatch.setenv("JUDGE_POOL_ENABLED", "false")
    monkeypatch.setattr(
        slot_mod, "get_pool_client", lambda: pytest.fail("禁用并发池时不应访问池客户端")
    )
    monkeypatch.setattr(
        mod,
        "ModelCallerSync",
        lambda: SimpleNamespace(call=lambda *a, **k: {"success": True, "content": "plain"}),
    )

    assert mod._call_llm("hi", {"judge_model_config": {"provider": "openai"}}) == "plain"


# ------------------------------------------------ 池异常必须透传（不被兜底吞掉）

def _must_not_be_called(*args, **kwargs):
    raise AssertionError("池异常已抛出，模型调用不应该发生")


def _pool_raising(monkeypatch, module, exc):
    def slot(engine, meta=None):
        raise exc

    monkeypatch.setattr(module, "judge_pool_slot", slot)


def test_pool_timeout_propagates_from_fallback_sites(monkeypatch):
    """池满等待超时不能被 "LLM 调用异常" 兜底吞成 None（否则上游给中性分）"""
    import app.services.model_caller as mc

    from app.services import rag_evaluator as rag_mod
    from app.services import response_parser as parser_mod
    from app.services.judge_pool import JudgePoolTimeout

    monkeypatch.setattr(mc.ModelCallerSync, "call", staticmethod(_must_not_be_called))

    _pool_raising(monkeypatch, rag_mod, JudgePoolTimeout("池满等待超时"))
    with pytest.raises(JudgePoolTimeout):
        rag_mod.LLMRagEvaluator(llm_config={"provider": "openai"})._call_llm("prompt")

    _pool_raising(monkeypatch, parser_mod, JudgePoolTimeout("池满等待超时"))
    with pytest.raises(JudgePoolTimeout):
        parser_mod.JudgeResponseParser(judge_config={"provider": "openai"})._stage4_ai_correction("raw")


def test_pool_aborted_propagates_from_fallback_sites(monkeypatch):
    """任务停止（JudgePoolAborted）不能被兜底吞掉，行必须按停止标记"""
    import app.services.model_caller as mc

    from app.services import rag_evaluator as rag_mod
    from app.services import response_parser as parser_mod
    from app.services.judge_pool import JudgePoolAborted

    monkeypatch.setattr(mc.ModelCallerSync, "call", staticmethod(_must_not_be_called))

    _pool_raising(monkeypatch, rag_mod, JudgePoolAborted("并发池等待被中止"))
    with pytest.raises(JudgePoolAborted):
        rag_mod.LLMRagEvaluator(llm_config={"provider": "openai"})._call_llm("prompt")

    _pool_raising(monkeypatch, parser_mod, JudgePoolAborted("并发池等待被中止"))
    with pytest.raises(JudgePoolAborted):
        parser_mod.JudgeResponseParser(judge_config={"provider": "openai"})._stage4_ai_correction("raw")


# ---------------------------------------------------------- 线程池上下文继承

def test_langgraph_run_parallel_inherits_context(monkeypatch):
    from app.services.judge_langgraph_agent import JudgeLangGraphAgent
    from app.services.judge_pool import bind_judge_context, current_judge_context

    agent = object.__new__(JudgeLangGraphAgent)
    seen = []

    def fake_run(state_input, judge_index):
        ctx = current_judge_context()
        seen.append((ctx.eval_task_id, ctx.row_idx))
        return {"score": 1.0, "feedback": "", "dimension_scores": {}}

    monkeypatch.setattr(agent, "run", fake_run)

    with bind_judge_context(eval_task_id="task-1", row_idx=3):
        results = agent.run_parallel({}, 3)

    assert len(results) == 3
    assert seen == [("task-1", 3)] * 3


def test_claude_code_run_parallel_inherits_context(monkeypatch):
    from app.services import claude_code_judge_agent as mod
    from app.services.judge_pool import bind_judge_context, current_judge_context

    agent = object.__new__(mod.ClaudeCodeJudgeAgent)
    seen = []

    def fake_single(config, system_prompt, user_prompt, session_file, judge_index):
        ctx = current_judge_context()
        seen.append((ctx.eval_task_id, ctx.row_idx))
        return {"score": 1.0, "feedback": "", "dimension_scores": {}}

    monkeypatch.setattr(agent, "_run_single", fake_single)
    monkeypatch.setattr(
        "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
        staticmethod(lambda: {"model": "m"}),
    )

    with bind_judge_context(eval_task_id="task-2", row_idx=1):
        results = agent.run_parallel("sys", "user", None, 3)

    assert len(results) == 3
    assert seen == [("task-2", 1)] * 3


def test_plugin_evaluator_judge_layer_inherits_context(monkeypatch):
    from app.services import plugin_evaluator as mod
    from app.services.judge_pool import bind_judge_context, current_judge_context

    agent = object.__new__(mod.PluginEvaluator)
    seen = []

    def fake_query(prompt, model, timeout=300.0, usage_sink=None):
        seen.append(current_judge_context().eval_task_id)
        return {"score": 8}

    monkeypatch.setattr(mod, "_cc_query", fake_query)
    monkeypatch.setattr(
        "app.services.claude_code_config_service.ClaudeCodeConfigService.get_active_config",
        staticmethod(lambda: {"model": "m", "timeout_seconds": 1}),
    )

    skill = SimpleNamespace(name="demo", description="desc", raw_content="content")

    with bind_judge_context(eval_task_id="task-3"):
        outcome = agent._run_judge_layer(skill)

    assert len(seen) == 4
    assert set(seen) == {"task-3"}
    assert outcome["layer_result"]["layer"] == "judge"


# ------------------------------------------- Harness 适配层与插件评测接入（Task 9）

def test_harness_adapter_engine_mapping(monkeypatch):
    from app.services import harness_adapter as mod

    calls = []

    class Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "judge_pool_slot", lambda engine, meta=None: (calls.append(engine), Ctx())[1])

    class CC(mod.ClaudeCodeAdapter):
        def _execute_raw(self, system_prompt, prompt, timeout):
            return '{"score": 1}', "completed"

    CC(config={"timeout_seconds": 5}).run_prompt("sys", "user", timeout=5)
    assert calls == ["claude_agent"]

    class DS(mod.HarnessAdapter):
        def _execute_raw(self, system_prompt, prompt, timeout):
            return '{"score": 1}', "completed"

    DS(config={"timeout_seconds": 5}).run_prompt("sys", "user", timeout=5)
    assert calls == ["claude_agent", "harness"]


def test_plugin_evaluator_uses_plugin_engine(monkeypatch):
    from app.services import plugin_evaluator as mod

    calls = []

    class Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "judge_pool_slot", lambda engine, meta=None: (calls.append(engine), Ctx())[1])
    evaluator = mod.PluginEvaluator(engine="legacy")
    monkeypatch.setattr(evaluator, "_evaluate_legacy", lambda *a, **k: {"success": True})
    evaluator.evaluate(plugin_path="/tmp/x")
    assert calls == ["plugin"]


def test_plugin_evaluator_trace_uses_plugin_engine(monkeypatch):
    """evaluate_trace 的最外层包裹必须有测试守住：漏掉后长达数十分钟的 trace 评测
    会静默不占 plugin 额度（限额失控），且行级行为本身察觉不到"""
    from app.services import plugin_evaluator as mod

    calls = []

    class Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "judge_pool_slot", lambda engine, meta=None: (calls.append(engine), Ctx())[1])
    evaluator = mod.PluginEvaluator(engine="legacy")
    # 只验入池位置，不真跑评测（真实实现会起子进程/评数十分钟）
    monkeypatch.setattr(
        evaluator, "_evaluate_trace_unpooled", lambda *a, **k: {"success": True, "evaluation_mode": "trace"}
    )
    result = evaluator.evaluate_trace("/tmp/x")
    assert result["success"] is True
    assert calls == ["plugin"]


# ------------------------------------------- RAGAS 链路入池与池异常穿透（C2）

def test_ragas_evaluate_runs_both_ascore_inside_slot(monkeypatch):
    """ragas 新旧 API 的两次 single_turn_ascore 都必须在 slot 的 enter/exit 之间"""
    import ragas.metrics as ragas_metrics

    from app.services import rag_evaluator as mod

    events = []
    _recording_slot(monkeypatch, mod, events=events)

    class _FakeMetric:
        def __init__(self, name):
            self.name = name

        async def single_turn_ascore(self, sample):
            events.append(("ascore", self.name))
            return 0.5

    monkeypatch.setattr(mod.RagasEvaluator, "_build_ragas_llm", lambda self: object())
    monkeypatch.setattr(
        ragas_metrics, "LLMContextPrecisionWithReference",
        lambda **kwargs: _FakeMetric("precision"),
    )
    monkeypatch.setattr(
        ragas_metrics, "LLMContextRecall", lambda **kwargs: _FakeMetric("recall")
    )

    scores = mod.RagasEvaluator(llm_config={"api_key": "k"})._ragas_evaluate("q", "gt", ["ctx"])

    assert scores == {"context_precision": 0.5, "context_recall": 0.5}
    assert [e[0] for e in events] == ["enter", "ascore", "ascore", "exit"], events
    assert events[0][1:] == ("graph", {"call_site": "rag_evaluator_ragas"})
    assert events[-1][1:] == ("graph", {"call_site": "rag_evaluator_ragas"})


def test_ragas_slot_released_when_body_raises(monkeypatch):
    """slot 内抛异常也必须退出（否则额度要等 TTL 才回收）"""
    from app.services import rag_evaluator as mod

    events = []
    _recording_slot(monkeypatch, mod, events=events)

    def _boom(self):
        events.append(("build_llm",))
        raise RuntimeError("裁判模型配置缺失")

    monkeypatch.setattr(mod.RagasEvaluator, "_build_ragas_llm", _boom)

    with pytest.raises(RuntimeError):
        mod.RagasEvaluator(llm_config={"api_key": "k"})._ragas_evaluate("q", "gt", ["ctx"])

    assert [e[0] for e in events] == ["enter", "build_llm", "exit"], events


def test_pool_errors_propagate_out_of_rag_evaluate(monkeypatch):
    """池超时/停止必须穿透 RAG 评测的三层兜底，不能被洗成缺指标或关键词降级分"""
    from app.services import rag_evaluator as mod
    from app.services.judge_pool import JudgePoolAborted, JudgePoolTimeout

    def _raiser(exc):
        def _inner(*args, **kwargs):
            raise exc

        return _inner

    cases = [
        (mod.RagasEvaluator, "_ragas_evaluate", ["context_precision"], JudgePoolTimeout("池满等待超时")),
        (mod.RagasEvaluator, "_ragas_evaluate", ["context_recall"], JudgePoolAborted("并发池等待被中止")),
        (mod.LLMRagEvaluator, "noise_sensitivity", ["noise_sensitivity"], JudgePoolTimeout("池满等待超时")),
        (mod.LLMRagEvaluator, "negative_rejection", ["negative_rejection"], JudgePoolAborted("并发池等待被中止")),
    ]
    for cls, name, metrics, exc in cases:
        monkeypatch.setattr(cls, name, _raiser(exc))
        fallback_calls = []
        monkeypatch.setattr(
            mod.RagasEvaluator, "_fallback_precision",
            lambda *a, **k: fallback_calls.append(a) or 0.0,
        )
        monkeypatch.setattr(
            mod.RagasEvaluator, "_fallback_recall",
            lambda *a, **k: fallback_calls.append(a) or 0.0,
        )

        evaluator = mod.RAGEvaluator(llm_config={"api_key": "k"})
        with pytest.raises(type(exc)):
            evaluator.evaluate("q", "gt", ["ctx"], metrics_list=metrics)
        assert fallback_calls == [], f"{name} 的池异常被降级成关键词算法"

        monkeypatch.undo()


# ------------------------------------- 调用点真实性：调用必须落在额度窗口内

def test_graph_model_call_happens_inside_slot_window(monkeypatch):
    """graph：把模型调用移出 with 块，本用例必须变红

    只断言「slot 被调用过」挡不住这种回归 —— 额度窗口和真实调用脱开后，
    池统计与限流全都失真，而行级结果看起来完全正常。
    """
    from app.services import judge_graph_nodes as mod

    events = []
    calls = _recording_slot(monkeypatch, mod, events=events)

    def fake_call(*args, **kwargs):
        events.append(("call",))
        return {"success": True, "content": "{}"}

    monkeypatch.setattr(mod, "ModelCallerSync", lambda: SimpleNamespace(call=fake_call))

    assert mod._call_llm("hi", {"judge_model_config": {"provider": "openai"}}) == "{}"
    assert [e[0] for e in events] == ["enter", "call", "exit"], events
    assert calls == [("graph", {"call_site": "judge_graph_nodes"})]


def test_plugin_evaluation_happens_inside_slot_window(monkeypatch):
    """plugin：整个插件评测（可达数十分钟）必须在额度窗口内跑完"""
    from app.services import plugin_evaluator as mod

    events = []
    calls = _recording_slot(monkeypatch, mod, events=events)
    evaluator = mod.PluginEvaluator(engine="legacy")

    def fake_evaluate_legacy(*args, **kwargs):
        events.append(("call",))
        return {"success": True}

    monkeypatch.setattr(evaluator, "_evaluate_legacy", fake_evaluate_legacy)

    assert evaluator.evaluate(plugin_path="/tmp/x") == {"success": True}
    assert [e[0] for e in events] == ["enter", "call", "exit"], events
    assert calls == [("plugin", {"call_site": "plugin_evaluator:legacy"})]


def test_rag_llm_judge_call_happens_inside_slot_window(monkeypatch):
    """graph（RAG LLM 裁判）：_call_llm 的模型调用同样必须在窗口内"""
    import app.services.model_caller as mc

    from app.services import rag_evaluator as mod

    events = []
    _recording_slot(monkeypatch, mod, events=events)

    def fake_call(*args, **kwargs):
        events.append(("call",))
        return {"success": True, "content": "x"}

    monkeypatch.setattr(mc.ModelCallerSync, "call", staticmethod(fake_call))

    assert mod.LLMRagEvaluator(llm_config={"provider": "openai"})._call_llm("prompt") == "x"
    assert [e[0] for e in events] == ["enter", "call", "exit"], events
