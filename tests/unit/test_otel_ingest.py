"""OTel 兼容导入测试（M8 QE8-4）

链路是真实的：TestClient → 真 AuthMiddleware → ingest router → sqlite。
覆盖方案 §M8「OTel 兼容导入」验收主链路「trace 推送 → 会话数据集 → trace 评测链路」：
1. OTLP/HTTP JSON 解析：LLM span（gen_ai.prompt/completion kvlist）转
   user/assistant 消息、工具 span 转 tool_use/tool_result 对（ERROR 态
   is_error=True）、ns 时间戳转 ISO、resource 属性进 metadata、按 traceId
   分组一 trace 一会话；
2. Phoenix 导出（traceId+spans，OpenInference 平铺属性）：input.value/
   output.value 与 llm.input/output_messages（JSON 字符串）两形态；
3. Langfuse 载荷经同一入口复用既有解析；未知形态 400；
4. 入库：新建/追加、session_messages 整包进数据集行、字段元数据与
   question_count 刷新、渲染进既有评测链路（render_session_markdown）；
5. 限额与权限：200 trace / 5000 span 上限、viewer 403、跨团队追加 403、
   缺 dataset_name/append_to 400。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.ingest_api import router as ingest_router
from app.db.session import Base, get_db
from app.middleware.auth_middleware import AuthMiddleware
from app.models.dataset_models import EvaluationDataV2, EvaluationDataset
from app.models.user_models import User
from app.services.api_key_throttle import ApiKeyAuthThrottle
from app.services.jwt_service import create_token
from app.services.otel_trace_parser import (
    detect_trace_payload_format,
    parse_trace_payload,
)
from app.services.trace_ingest_service import MAX_TRACES_PER_IMPORT, TraceIngestError, ingest_trace_payload
from tests.security.test_apikey_bruteforce import FakeRedis

pytestmark = pytest.mark.unit


def _otlp_payload() -> dict:
    """两 trace 的 OTLP/HTTP JSON：t1=LLM 轮次+工具失败、t2=单 LLM 轮次"""
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "agent-demo"}},
                {"key": "deployment.environment.name", "value": {"stringValue": "uat"}},
            ]},
            "scopeSpans": [{"spans": [
                {
                    "traceId": "t1", "spanId": "s1", "name": "chat", "kind": 1,
                    "startTimeUnixNano": "1730000000000000000",
                    "endTimeUnixNano": "1730000001000000000",
                    "attributes": [
                        {"key": "gen_ai.prompt", "value": {"stringValue": "帮我查北京天气"}},
                        {"key": "gen_ai.completion", "value": {"stringValue": "今天晴天，25 度"}},
                    ],
                    "status": {"code": 1},
                },
                {
                    "traceId": "t1", "spanId": "s2", "parentSpanId": "s1",
                    "name": "get_weather", "kind": 1,
                    "startTimeUnixNano": "1730000000500000000",
                    "endTimeUnixNano": "1730000000800000000",
                    "attributes": [{"key": "city", "value": {"stringValue": "北京"}}],
                    "status": {"code": 2},  # ERROR → tool_result is_error
                },
                {
                    "traceId": "t2", "spanId": "s3", "name": "chat", "kind": 1,
                    "startTimeUnixNano": "1730000100000000000",
                    "endTimeUnixNano": "1730000100500000000",
                    "attributes": [
                        {"key": "gen_ai.prompt", "value": {"stringValue": "写首诗"}},
                        {"key": "gen_ai.completion", "value": {"stringValue": "春风得意"}},
                    ],
                    "status": {"code": 1},
                },
            ]}],
        }],
    }


def _phoenix_payload() -> dict:
    """Phoenix trace 导出（CLI/UI 下载形态：traceId + spans 平铺 OpenInference 属性）"""
    return {
        "traceId": "px1",
        "spans": [
            {
                "name": "chat_completion", "context": {"trace_id": "px1", "span_id": "a"},
                "span_kind": "LLM", "parent_id": None,
                "start_time": "2026-01-17T10:00:00.000Z",
                "end_time": "2026-01-17T10:00:01.250Z",
                "status_code": "OK",
                "attributes": {
                    "llm.model_name": "gpt-4",
                    "input.value": "What is the weather?",
                    "output.value": "The weather is sunny.",
                },
            },
        ],
    }


class TestOtlpParsing:
    def test_llm_and_tool_spans_to_session_record(self):
        records, fmt = parse_trace_payload(_otlp_payload())
        assert fmt == "otlp" and len(records) == 2
        t1 = next(r for r in records if r["session_id"] == "t1")
        assert t1["source"] == "otel"
        roles = [m["role"] for m in t1["messages"]]
        # LLM 轮次（user/assistant）+ 工具 span（assistant tool_use + user tool_result）
        assert roles == ["user", "assistant", "assistant", "user"]
        assert t1["messages"][0]["content"][0]["text"] == "帮我查北京天气"
        assert t1["messages"][1]["content"][0]["text"] == "今天晴天，25 度"
        tool_use = t1["messages"][2]["content"][0]
        assert tool_use["type"] == "tool_use" and tool_use["name"] == "get_weather"
        tool_result = t1["messages"][3]["content"][0]
        assert tool_result["type"] == "tool_result" and tool_result["is_error"] is True
        # 统计口径：1 轮（首条 user 非 tool_result）+1 工具调用；时间转 ISO
        # （1730000000e9 ns = 2024-10-27T03:33:20Z）
        assert t1["stats"]["turns"] == 1
        assert t1["stats"]["tool_call_count"] == 1
        assert t1["started_at"] == "2024-10-27T03:33:20+00:00"
        # resource 属性进 metadata（归因锚点）
        assert t1["metadata"]["service.name"] == "agent-demo"
        assert t1["metadata"]["deployment.environment.name"] == "uat"
        assert t1["metadata"]["span_count"] == 2

    def test_record_renders_into_existing_eval_chain(self):
        """转换产物可直接进既有评测链路（裁判渲染 + 首条用户消息摘要）"""
        from app.services.session_file_parser import (
            render_session_markdown,
            summarize_first_user_message,
        )
        records, _ = parse_trace_payload(_otlp_payload())
        t1 = next(r for r in records if r["session_id"] == "t1")
        markdown = render_session_markdown(t1)
        assert "会话记录" in markdown and "get_weather" in markdown
        assert summarize_first_user_message(t1) == "帮我查北京天气"


class TestPhoenixParsing:
    def test_phoenix_trace_export(self):
        records, fmt = parse_trace_payload(_phoenix_payload())
        assert fmt == "phoenix" and len(records) == 1
        record = records[0]
        assert record["session_id"] == "px1" and record["source"] == "phoenix"
        texts = [b["text"] for m in record["messages"] for b in m["content"]]
        assert "What is the weather?" in texts and "The weather is sunny." in texts

    def test_phoenix_llm_messages_attribute(self):
        payload = {"data": [{"traceId": "px2", "spans": [{
            "name": "llm", "context": {"trace_id": "px2", "span_id": "b"},
            "span_kind": "LLM", "start_time": "2026-01-17T11:00:00Z",
            "end_time": "2026-01-17T11:00:01Z", "status_code": "OK",
            "attributes": {
                "llm.input_messages": json.dumps([{"role": "user", "content": "你好"}]),
                "llm.output_messages": json.dumps([{"role": "assistant", "content": "在的"}]),
            }}]}]}
        records, fmt = parse_trace_payload(payload)
        assert fmt == "phoenix"
        texts = [(m["role"], m["content"][0]["text"]) for m in records[0]["messages"]]
        assert ("user", "你好") in texts and ("assistant", "在的") in texts


class TestFormatDetection:
    def test_langfuse_payload_reuses_existing_parser(self):
        payload = {"traces": [{
            "id": "lf1", "timestamp": "2026-01-01T00:00:00Z",
            "input": {"messages": [{"role": "user", "content": "问题"}]},
            "output": {"messages": [{"role": "assistant", "content": "回答"}]},
            "generations": [{
                "id": "g1", "startTime": "2026-01-01T00:00:00Z",
                "input": {"messages": [{"role": "user", "content": "问题"}]},
                "output": {"messages": [{"role": "assistant", "content": "回答"}]},
            }],
        }]}
        records, fmt = parse_trace_payload(payload)
        assert fmt == "langfuse" and len(records) == 1
        assert records[0]["source"] == "langfuse"

    def test_unknown_payload_rejected(self):
        assert detect_trace_payload_format({"foo": "bar"}) is None
        with pytest.raises(ValueError, match="无法识别"):
            parse_trace_payload({"foo": "bar"})


# ======================================================================
# 入库 + API
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
        db.add(User(user_code="other", username="other", is_active=True,
                    super_admin_flag=0, application_admin=0, org_code="OTHER"))
        # 本团队既有数据集（追加模式用）与异团队数据集（越权用）
        db.add(EvaluationDataset(id="ds-mine", name="既有会话集", team_code="DEFAULT"))
        db.add(EvaluationDataset(id="ds-other", name="他队会话集", team_code="OTHER"))
        db.commit()

    app = FastAPI()
    app.include_router(ingest_router, prefix="/api/v1")
    app.add_middleware(
        AuthMiddleware, api_key_throttle=ApiKeyAuthThrottle(redis_client=FakeRedis()))
    app.dependency_overrides[get_db] = lambda: TestSession()
    return type("Stack", (), {
        "app": app, "session": TestSession,
        "jwt_eng": create_token("eng", "eng"),
        "jwt_viewer": create_token("viewer", "viewer"),
        "jwt_other": create_token("other", "other"),
    })()


@pytest.fixture
def client(stack):
    return TestClient(stack.app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestIngestService:
    def test_ingest_creates_session_dataset(self, stack):
        with stack.session() as db:
            result = ingest_trace_payload(
                db, _otlp_payload(), name="OTel 会话集", created_by="eng",
                team_code="DEFAULT")
        assert result["format"] == "otlp" and result["traces"] == 2
        with stack.session() as db:
            dataset = db.get(EvaluationDataset, result["dataset_id"])
            assert dataset is not None and dataset.team_code == "DEFAULT"
            rows = (db.query(EvaluationDataV2)
                    .filter(EvaluationDataV2.dataset_id == dataset.id).all())
            assert len(rows) == 2
            assert dataset.question_count == 2
            row = next(r for r in rows if r.data["session_id"] == "t1")
            # 行结构与 Claude Code / Langfuse 会话导入同构（进既有评测链路）
            assert row.data["source"] == "otel"
            assert row.data["user_input"] == "帮我查北京天气"
            assert row.data["session_messages"]["messages"][0]["role"] == "user"
            assert "session_messages" in row.data["field_descriptions"]

    def test_append_to_existing_dataset(self, stack):
        with stack.session() as db:
            result = ingest_trace_payload(
                db, _phoenix_payload(), real_dataset_id="ds-mine", created_by="eng",
                team_code="DEFAULT")
        assert result["dataset_id"] == "ds-mine" and result["count"] == 1
        with stack.session() as db:
            assert db.get(EvaluationDataset, "ds-mine").question_count == 1

    def test_trace_cap(self, stack):
        payload = _otlp_payload()
        # 构造超限 trace 数（同一批 resourceSpans 展开）
        spans = []
        for i in range(MAX_TRACES_PER_IMPORT + 1):
            spans.append({
                "traceId": f"t{i}", "spanId": f"s{i}", "name": "chat", "kind": 1,
                "startTimeUnixNano": "1730000000000000000",
                "endTimeUnixNano": "1730000001000000000",
                "attributes": [
                    {"key": "gen_ai.prompt", "value": {"stringValue": "q"}},
                    {"key": "gen_ai.completion", "value": {"stringValue": "a"}}],
            })
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"] = spans
        with stack.session() as db:
            with pytest.raises(TraceIngestError, match="超上限"):
                ingest_trace_payload(db, payload, name="超限")

    def test_no_readable_content_rejected(self, stack):
        payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
            "traceId": "t0", "spanId": "s0", "name": "framework", "kind": 1,
            "startTimeUnixNano": "1730000000000000000", "attributes": [],
        }]}]}]}
        with stack.session() as db:
            with pytest.raises(TraceIngestError, match="未解析出"):
                ingest_trace_payload(db, payload, name="空")


class TestIngestApi:
    def test_otlp_ingest_200(self, client, stack):
        resp = client.post(
            "/api/v1/ingest/otel-traces",
            params={"dataset_name": "API 导入会话集"},
            headers=auth(stack.jwt_eng), json=_otlp_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "otlp" and body["traces"] == 2
        assert body["dataset_id"]

    def test_phoenix_ingest_200(self, client, stack):
        resp = client.post(
            "/api/v1/ingest/otel-traces",
            params={"append_to": "ds-mine"},
            headers=auth(stack.jwt_eng), json=_phoenix_payload())
        assert resp.status_code == 200, resp.text
        assert resp.json()["format"] == "phoenix"

    def test_viewer_forbidden(self, client, stack):
        resp = client.post(
            "/api/v1/ingest/otel-traces",
            params={"dataset_name": "x"},
            headers=auth(stack.jwt_viewer), json=_otlp_payload())
        assert resp.status_code == 403

    def test_cross_team_append_forbidden(self, client, stack):
        resp = client.post(
            "/api/v1/ingest/otel-traces",
            params={"append_to": "ds-other"},
            headers=auth(stack.jwt_eng), json=_otlp_payload())
        assert resp.status_code == 403

    def test_missing_dataset_target_400(self, client, stack):
        resp = client.post(
            "/api/v1/ingest/otel-traces",
            headers=auth(stack.jwt_eng), json=_otlp_payload())
        assert resp.status_code == 400
        assert "dataset_name" in resp.json()["detail"]

    def test_unknown_payload_400(self, client, stack):
        resp = client.post(
            "/api/v1/ingest/otel-traces",
            params={"dataset_name": "x"},
            headers=auth(stack.jwt_eng), json={"hello": "world"})
        assert resp.status_code == 400
        assert "无法识别" in resp.json()["detail"]
