"""W4 模型/裁判配置快照单元测试（P0-C）

对齐《整改开发计划-20261013.md》§2 W4「测试设计」单测要求：
1. 创建任务后改 ModelConfig（temperature/endpoint）→ 执行用快照——断言
   executor 收到的 config 来源于快照且 temperature/endpoint 为旧值，
   凭据 api_key 从实时 ModelConfig 解析（密钥轮换后旧钥不可用）；
2. 快照中 api_key 为掩码 + sha256 指纹、无明文（键级 + JSON 全文双断言）；
3. 存量任务（model_config 空）走实时路径并标记 config_snapshot: false。

覆盖面（对应实现 app/services/config_snapshot.py 及其消费点）：
- mask_api_key / api_key_fingerprint 纯函数（含与 657fc49 基线原实现的
  逐字符回归对照，防止掩码逻辑漂移）；
- build_model_config_snapshot 快照结构 / 深拷贝 / 无配置语义；
- resolve_model_call_config 与 resolve_judge_call_config 的快照优先链；
- snapshot_used 标记（result JSON 的 config_snapshot 增量字段）；
- executor（_evaluate_single）与 judge_graph_state（build_state_input）
  两个消费点收到的配置确实来自快照；
- 执行记录写入侧：_strip_plaintext_credentials 剥离明文、
  build_used_config_summary 按 judge_engine 归因；
- update_task 显式切换模型时快照重建 / 同值回传不重置口径锚点。

测试纪律（§5.3）：不弱化断言；全部 mock 无外部依赖（零网关/零 DB）。
"""
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.config_snapshot import (
    FINGERPRINT_FIELD,
    MASKED_KEY_FIELD,
    api_key_fingerprint,
    build_execution_config_snapshot,
    build_model_config_snapshot,
    build_used_config_summary,
    mask_api_key,
    resolve_judge_call_config,
    resolve_model_call_config,
    snapshot_used,
    summarize_call_config,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ===========================================================================
# 工具与 fixture
# ===========================================================================

OLD_ENDPOINT = "http://old-endpoint.example/v1"
NEW_ENDPOINT = "http://new-endpoint.example/v1"
OLD_PLAINTEXT_KEY = "sk-w4unit-oldkey123456"
ROTATED_PLAINTEXT_KEY = "sk-w4unit-rotated789"


def _model_config_row(
    model_id="mc_old",
    name="旧口径模型",
    endpoint=OLD_ENDPOINT,
    api_key=OLD_PLAINTEXT_KEY,
    temperature=0.1,
    max_tokens=1024,
):
    """构造一条 ModelConfig ORM 替身（字段与 app/models/model_config_models.py 一致）"""
    return SimpleNamespace(
        id=model_id,
        name=name,
        provider="openai",
        model_type="gpt-4o",
        endpoint=endpoint,
        api_key=api_key,
        auth_type="api_key",
        auth_config={"header": "Authorization", "prefix": "Bearer"},
        request_config={"temperature": temperature, "max_tokens": max_tokens},
        response_parse_config={"path": "choices[0].message.content"},
    )


def _db_returning(rows_by_id):
    """mock 一个 SQLAlchemy 会话：query(ModelConfig).filter(ModelConfig.id == X).first()

    config_snapshot 的查询形如 db.query(ModelConfig).filter(ModelConfig.id == X).first()，
    ModelConfig.id == X 生成 BinaryExpression，右值在 expr.right.value——按其分派行。
    """
    db = MagicMock()
    lookup = {r.id: r for r in rows_by_id.values()}

    class _Filtered:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _Query:
        def filter(self, *args, **kwargs):
            expr = args[0] if args else None
            model_id = getattr(getattr(expr, "right", None), "value", None)
            return _Filtered(lookup.get(model_id))

    db.query.side_effect = lambda *a, **k: _Query()
    return db


@pytest.fixture
def snapshot_of_old_model():
    """对旧口径 ModelConfig 构建的快照（走真实 build_model_config_snapshot）"""
    db = _db_returning({"mc_old": _model_config_row()})
    return build_model_config_snapshot(db, "mc_old")


# ===========================================================================
# 1. 掩码与指纹（含基线逐字符回归对照）
# ===========================================================================


class TestMaskHelpers:
    def test_mask_basic_shape(self):
        """前 3 位 + 星号补齐 + 后 3 位，长度守恒"""
        assert mask_api_key("sk-1234567890abcdef") == "sk-*************def"
        assert len(mask_api_key("sk-1234567890abcdef")) == 19

    def test_mask_baseline_charwise_identical(self):
        """与 657fc49 基线 model_config_api 原实现逐字符一致（防漂移回归）"""
        def baseline_mask(value):
            # 657fc49 app/api/model_config_api.py ModelDetailResponse.mask_api_key 原文
            if not value:
                return value
            if len(value) <= 8:
                return '*' * len(value)
            return value[:3] + '*' * (len(value) - 6) + value[-3:]

        for sample in [
            None, "", "short", "12345678", "123456789",
            OLD_PLAINTEXT_KEY, ROTATED_PLAINTEXT_KEY, "a" * 200,
        ]:
            assert mask_api_key(sample) == baseline_mask(sample), f"掩码行为与基线不一致: {sample!r}"

    def test_mask_short_key_all_stars(self):
        """长度 <= 8 的键整体打星（不泄露任何片段）"""
        assert mask_api_key("abc") == "***"
        assert mask_api_key("12345678") == "********"

    def test_mask_falsy_passthrough(self):
        assert mask_api_key(None) is None
        assert mask_api_key("") == ""

    def test_fingerprint_is_sha256_prefix8(self):
        """指纹 = sha256(明文) 前 8 位（与 hashlib 直算对照，非黑盒）"""
        assert api_key_fingerprint(OLD_PLAINTEXT_KEY) == (
            hashlib.sha256(OLD_PLAINTEXT_KEY.encode("utf-8")).hexdigest()[:8]
        )
        assert len(api_key_fingerprint(OLD_PLAINTEXT_KEY)) == 8
        assert api_key_fingerprint(None) is None
        assert api_key_fingerprint("") is None

    def test_api_detail_response_mask_delegates(self):
        """model_config_api.ModelDetailResponse.mask_api_key 委托单一来源（W4 收敛点）"""
        from datetime import datetime

        from app.api.model_config_api import ModelDetailResponse

        resp = ModelDetailResponse(
            id="mc1", name="m", provider="openai", model_type="gpt-4o",
            model_role="evaluation", endpoint="http://e", auth_type="api_key",
            supports_multimodal=False, description=None, is_active=True,
            is_preset=False, created_at=datetime.now(),
            api_key=OLD_PLAINTEXT_KEY, auth_config={}, request_config={},
            response_parse_config={}, multimodal_config={},
        )
        assert resp.mask_api_key(OLD_PLAINTEXT_KEY) == mask_api_key(OLD_PLAINTEXT_KEY)
        # 序列化输出也走同一掩码（API 层不漏明文）
        assert OLD_PLAINTEXT_KEY not in resp.model_dump_json()


# ===========================================================================
# 2. 快照构建：结构 / 深拷贝 / 无明文
# ===========================================================================


class TestBuildSnapshot:
    def test_snapshot_fields_and_no_plaintext_keys(self, snapshot_of_old_model):
        snap = snapshot_of_old_model
        # 口径字段齐全（验收：TaskExecutionRecord 可回答「用的什么模型/温度」）
        assert snap["snapshot"] is True
        assert snap["snapshot_at"]
        assert snap["model_id"] == "mc_old"
        assert snap["name"] == "旧口径模型"
        assert snap["provider"] == "openai"
        assert snap["model_type"] == "gpt-4o"
        assert snap["endpoint"] == OLD_ENDPOINT
        assert snap["auth_type"] == "api_key"
        assert snap["request_config"] == {"temperature": 0.1, "max_tokens": 1024}
        assert snap["response_parse_config"] == {"path": "choices[0].message.content"}
        # 掩码 + 指纹
        assert snap[MASKED_KEY_FIELD] == mask_api_key(OLD_PLAINTEXT_KEY)
        assert snap[FINGERPRINT_FIELD] == api_key_fingerprint(OLD_PLAINTEXT_KEY)

        # 严禁明文：键级断言
        assert "api_key" not in snap, "快照不得含 api_key 明文键"
        assert "auth_config" not in snap, "快照不得含 auth_config（登录凭据）键"
        # 严禁明文：JSON 全文断言（防嵌套夹带）
        dumped = json.dumps(snap, ensure_ascii=False)
        assert OLD_PLAINTEXT_KEY not in dumped
        assert "Bearer" not in dumped  # auth_config 内容也不得渗入

    def test_snapshot_deep_copies_request_config(self):
        """深拷贝：改 ORM 对象的 request_config 不影响已建快照"""
        row = _model_config_row()
        db = _db_returning({"mc_old": row})
        snap = build_model_config_snapshot(db, "mc_old")
        row.request_config["temperature"] = 9.9
        row.endpoint = "http://mutated"
        assert snap["request_config"]["temperature"] == 0.1
        assert snap["endpoint"] == OLD_ENDPOINT

    def test_snapshot_none_when_no_config(self):
        db = _db_returning({})
        assert build_model_config_snapshot(db, None) is None
        assert build_model_config_snapshot(db, "") is None
        assert build_model_config_snapshot(db, "missing_id") is None
        assert build_model_config_snapshot(None, None) is None


# ===========================================================================
# 3. 快照优先解析：被评测模型（W4 核心行为）
# ===========================================================================


class TestResolveModelCallConfig:
    def test_snapshot_wins_after_config_changed(self, snapshot_of_old_model):
        """创建任务后改 ModelConfig（endpoint/temperature/轮换 key）→ 执行用快照

        断言：口径字段（endpoint/temperature/max_tokens/response_parse_config）
        为创建时旧值；凭据 api_key 为实时新钥；来源标签 snapshot。
        """
        rotated = _model_config_row(
            endpoint=NEW_ENDPOINT, api_key=ROTATED_PLAINTEXT_KEY, temperature=0.9
        )
        db = _db_returning({"mc_old": rotated})
        task = SimpleNamespace(model_config=dict(snapshot_of_old_model), model_provider="mc_old")

        config, source = resolve_model_call_config(task, db)

        assert source == "snapshot"
        # 口径 = 创建时快照（旧值）
        assert config["endpoint"] == OLD_ENDPOINT
        assert config["request_config"]["temperature"] == 0.1
        assert config["request_config"]["max_tokens"] == 1024
        assert config["model_type"] == "gpt-4o"
        # 凭据 = 实时解析（新钥可用）
        assert config["api_key"] == ROTATED_PLAINTEXT_KEY
        assert config["auth_config"] == {"header": "Authorization", "prefix": "Bearer"}

    def test_live_fallback_for_legacy_task(self):
        """存量任务（model_config 空）→ 实时查询，来源 live"""
        live = _model_config_row(model_id="mc_live", endpoint=NEW_ENDPOINT, temperature=0.7)
        db = _db_returning({"mc_live": live})
        task = SimpleNamespace(model_config=None, model_provider="mc_live")

        config, source = resolve_model_call_config(task, db)

        assert source == "live"
        assert config["endpoint"] == NEW_ENDPOINT
        assert config["request_config"]["temperature"] == 0.7
        assert config["api_key"] == OLD_PLAINTEXT_KEY  # live 路径 api_key 原样
        assert config["model_id"] == "mc_live"

    def test_none_when_no_config_at_all(self):
        task = SimpleNamespace(model_config=None, model_provider=None)
        config, source = resolve_model_call_config(task, MagicMock())
        assert config == {}
        assert source == "none"

    def test_snapshot_fields_survive_live_config_deleted(self, snapshot_of_old_model):
        """实时 ModelConfig 被删：口径字段保留快照，凭据缺失（调用失败与现状一致）"""
        db = _db_returning({})  # 查不到任何实时配置
        task = SimpleNamespace(model_config=dict(snapshot_of_old_model), model_provider="mc_old")

        config, source = resolve_model_call_config(task, db)

        assert source == "snapshot"
        assert config["endpoint"] == OLD_ENDPOINT
        assert config["request_config"]["temperature"] == 0.1
        assert "api_key" not in config  # 快照中只有掩码键，不可用于调用


# ===========================================================================
# 4. 快照优先解析：裁判（优先级链）
# ===========================================================================


def _cc_global_config():
    return {
        "provider": "anthropic",
        "model_type": "claude-sonnet",
        "endpoint": "http://cc-global/v1",
        "api_key": "sk-cc-global",
    }


class TestResolveJudgeCallConfig:
    def test_snapshot_first_even_if_cc_global_exists(self, snapshot_of_old_model, monkeypatch):
        """快照优先于 CC 全局配置（W4 优先级变化点：创建时快照先于全局）"""
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: _cc_global_config()),
        )
        task = SimpleNamespace(
            judge_model_config=dict(snapshot_of_old_model), judge_model="mc_old"
        )

        config, source = resolve_judge_call_config(task, MagicMock())

        assert source == "snapshot"
        assert config["endpoint"] == OLD_ENDPOINT  # 快照口径，而非 CC 全局
        assert config["request_config"]["temperature"] == 0.1

    def test_cc_global_second_when_no_snapshot(self, monkeypatch):
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: _cc_global_config()),
        )
        task = SimpleNamespace(judge_model_config=None, judge_model="mc_old")

        config, source = resolve_judge_call_config(task, MagicMock())

        assert source == "claude_code_global"
        assert config["endpoint"] == "http://cc-global/v1"

    def test_live_fallback_when_no_snapshot_no_cc(self, monkeypatch):
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: None),
        )
        live = _model_config_row(model_id="mc_judge", endpoint=NEW_ENDPOINT)
        db = _db_returning({"mc_judge": live})
        task = SimpleNamespace(judge_model_config=None, judge_model="mc_judge")

        config, source = resolve_judge_call_config(task, db)

        assert source == "live"
        assert config["endpoint"] == NEW_ENDPOINT
        assert config["api_key"] == OLD_PLAINTEXT_KEY

    def test_consistency_config_fallback(self, monkeypatch):
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: None),
        )
        judge_cfg = {"provider": "openai", "endpoint": "http://consistency/v1", "api_key": "k"}
        task = SimpleNamespace(
            judge_model_config=None,
            judge_model=None,
            consistency_config={"judge_config": judge_cfg},
        )

        config, source = resolve_judge_call_config(task, MagicMock())

        assert source == "consistency_config"
        assert config is judge_cfg

    def test_none_when_all_missing(self, monkeypatch):
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: None),
        )
        task = SimpleNamespace(judge_model_config=None, judge_model=None, consistency_config=None)
        config, source = resolve_judge_call_config(task, MagicMock())
        assert config == {}
        assert source == "none"


# ===========================================================================
# 5. result JSON 的 config_snapshot 标记
# ===========================================================================


class TestSnapshotUsedMarker:
    def test_true_when_any_snapshot(self, snapshot_of_old_model):
        task = SimpleNamespace(
            model_config=dict(snapshot_of_old_model), judge_model_config=None
        )
        assert snapshot_used(task) is True
        task2 = SimpleNamespace(model_config=None, judge_model_config={"snapshot": True})
        assert snapshot_used(task2) is True

    def test_false_for_legacy_task(self):
        """存量任务（双快照均空）→ 标记 False（走实时路径的 result 标记）"""
        task = SimpleNamespace(model_config=None, judge_model_config=None)
        assert snapshot_used(task) is False


# ===========================================================================
# 6. 消费点：executor / judge_graph_state 收到的配置来自快照
# ===========================================================================


class TestExecutorReceivesSnapshot:
    """计划原文：断言 executor 收到的 config 来源于快照且 temperature 为旧值"""

    def test_evaluate_single_model_call_uses_snapshot(self, snapshot_of_old_model, monkeypatch):
        from app.services.evaluation_executor import EvaluationExecutor
        from app.services.model_caller import ModelCallerSync

        captured = {}

        def fake_call(messages, config, runtime_variables=None):
            captured.update(config)
            return {"success": True, "content": "模型输出", "chain_results": []}

        monkeypatch.setattr(ModelCallerSync, "call", staticmethod(fake_call))

        # resolve_model_call_config(task) 不传 db → 凭据合并自建 SessionLocal，
        # mock 之返回「已轮换」的实时配置
        rotated = _model_config_row(
            endpoint=NEW_ENDPOINT, api_key=ROTATED_PLAINTEXT_KEY, temperature=0.9
        )
        fake_db = _db_returning({"mc_old": rotated})
        monkeypatch.setattr("app.db.session.SessionLocal", MagicMock(return_value=fake_db))

        task = SimpleNamespace(
            evaluation_type="judge",
            model_config=dict(snapshot_of_old_model),
            model_provider="mc_old",
        )
        executor = EvaluationExecutor(stop_event=threading.Event())
        # 走到模型调用即可（返回内容为空白 → empty_input 分支，不影响已捕获断言）
        result = executor._evaluate_single({"idx": 0, "user_input": "问题"}, task)

        assert "idx" in result  # 执行完成（empty_input 也算完整走完步骤 1）
        # executor 实际发给 ModelCallerSync 的配置：口径=快照旧值，凭据=实时新钥
        assert captured, "ModelCallerSync.call 未被调用"
        assert captured["endpoint"] == OLD_ENDPOINT
        assert captured["request_config"]["temperature"] == 0.1
        assert captured["api_key"] == ROTATED_PLAINTEXT_KEY


class TestJudgeGraphStateReceivesSnapshot:
    """裁判图初始状态拿到的配置：快照优先（有快照时不受 CC 全局配置干扰）"""

    def test_build_state_input_judge_config_from_snapshot(
        self, snapshot_of_old_model, monkeypatch
    ):
        from app.services import claude_code_config_service
        from app.services import judge_graph_state

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: _cc_global_config()),
        )
        task = SimpleNamespace(
            id="task_w4",
            scene_desc="",
            evaluation_metrics=[{"name": "准确性", "weight": 1.0}],
            score_details="按准确性打分",
            judge_prompt="",
            max_score=10,
            judge_model_config=dict(snapshot_of_old_model),
            judge_model="mc_old",
            consistency_config=None,
        )

        state = judge_graph_state.build_state_input(
            task, resolved_targets_content="内容", user_variables=[], overrides=None
        )

        judge_cfg = state["judge_model_config"]
        assert judge_cfg["endpoint"] == OLD_ENDPOINT  # 快照口径，非 CC 全局
        assert judge_cfg["request_config"]["temperature"] == 0.1

    def test_load_judge_model_config_cc_global_when_no_snapshot(self, monkeypatch):
        """无快照（存量/claude_agent 任务）时保持原优先级：CC 全局优先"""
        from app.services import claude_code_config_service
        from app.services import judge_graph_state

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: _cc_global_config()),
        )
        task = SimpleNamespace(judge_model_config=None, judge_model="mc_x")
        cfg = judge_graph_state._load_judge_model_config(task)
        assert cfg["endpoint"] == "http://cc-global/v1"


# ===========================================================================
# 7. 执行记录写入侧：剥离明文 / used 摘要归因
# ===========================================================================


class TestExecutionRecordSnapshotBuild:
    def test_strip_plaintext_credentials(self, snapshot_of_old_model):
        """task.model_config 被人为塞入明文 → 执行记录侧拷贝时剥离"""
        dirty = dict(snapshot_of_old_model)
        dirty["api_key"] = OLD_PLAINTEXT_KEY  # 模拟存量任务/透传路径的明文夹带
        dirty["auth_config"] = {"password": "p@ss"}
        task = SimpleNamespace(model_config=dirty, judge_model_config=None)

        record_snapshot = build_execution_config_snapshot(task)

        model = record_snapshot["model"]
        assert "api_key" not in model
        assert "auth_config" not in model
        assert model[MASKED_KEY_FIELD] == mask_api_key(OLD_PLAINTEXT_KEY)  # 掩码保留
        assert OLD_PLAINTEXT_KEY not in json.dumps(record_snapshot)
        assert "p@ss" not in json.dumps(record_snapshot)
        assert record_snapshot["judge"] is None

    def test_used_summary_graph_engine_judge_task(self, snapshot_of_old_model, monkeypatch):
        """judge + graph 引擎：used.model / used.judge 均来自快照链，掩码+指纹无明文"""
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: _cc_global_config()),
        )
        rotated = _model_config_row(
            endpoint=NEW_ENDPOINT, api_key=ROTATED_PLAINTEXT_KEY, temperature=0.9
        )
        db = _db_returning({"mc_old": rotated})
        task = SimpleNamespace(
            evaluation_type="judge",
            judge_engine="graph",
            model_config=dict(snapshot_of_old_model),
            model_provider="mc_old",
            judge_model_config=dict(snapshot_of_old_model),
            judge_model="mc_old",
            consistency_config=None,
        )

        summary = build_used_config_summary(task, db)

        # 被评测模型摘要：快照口径 + 实时新钥的掩码/指纹
        assert summary["model"]["source"] == "snapshot"
        assert summary["model"]["endpoint"] == OLD_ENDPOINT
        assert summary["model"]["request_config"]["temperature"] == 0.1
        assert summary["model"][MASKED_KEY_FIELD] == mask_api_key(ROTATED_PLAINTEXT_KEY)
        assert summary["model"][FINGERPRINT_FIELD] == api_key_fingerprint(ROTATED_PLAINTEXT_KEY)
        # 裁判摘要：graph 引擎走快照优先链（优先于 CC 全局）
        assert summary["judge"]["source"] == "snapshot"
        assert summary["judge"]["endpoint"] == OLD_ENDPOINT
        # 全文无明文
        dumped = json.dumps(summary, ensure_ascii=False)
        assert ROTATED_PLAINTEXT_KEY not in dumped
        assert OLD_PLAINTEXT_KEY not in dumped

    def test_used_summary_claude_agent_engine_attributes_to_cc_global(
        self, snapshot_of_old_model, monkeypatch
    ):
        """judge + claude_agent 引擎：裁判归因 CC 全局配置（CC 链路归因口径）"""
        from app.services import claude_code_config_service

        monkeypatch.setattr(
            claude_code_config_service.ClaudeCodeConfigService,
            "get_judge_llm_config",
            classmethod(lambda cls: _cc_global_config()),
        )
        task = SimpleNamespace(
            evaluation_type="judge",
            judge_engine="claude_agent",
            model_config=dict(snapshot_of_old_model),
            model_provider="mc_old",
            judge_model_config=dict(snapshot_of_old_model),
            judge_model="mc_old",
            consistency_config=None,
        )

        summary = build_used_config_summary(task, _db_returning({"mc_old": _model_config_row()}))

        assert summary["judge"]["source"] == "claude_code_global"
        assert summary["judge"]["endpoint"] == "http://cc-global/v1"
        assert summary["model"]["source"] == "snapshot"

    def test_used_summary_no_judge_for_latency(self, snapshot_of_old_model):
        """非裁判评测（latency）：judge 摘要为 None（区别于 config_missing）"""
        task = SimpleNamespace(
            evaluation_type="latency",
            model_config=dict(snapshot_of_old_model),
            model_provider="mc_old",
            judge_model_config=None,
            judge_model=None,
            consistency_config=None,
        )
        summary = build_used_config_summary(task, _db_returning({"mc_old": _model_config_row()}))
        assert summary["model"]["source"] == "snapshot"
        assert summary["judge"] is None

    def test_summarize_call_config_masks(self):
        out = summarize_call_config(
            {
                "provider": "openai",
                "model_type": "gpt-4o",
                "endpoint": "http://e/v1",
                "auth_type": "api_key",
                "request_config": {"temperature": 0.1},
                "api_key": OLD_PLAINTEXT_KEY,
            },
            "live",
        )
        assert out == {
            "source": "live",
            "provider": "openai",
            "model_type": "gpt-4o",
            "endpoint": "http://e/v1",
            "auth_type": "api_key",
            "request_config": {"temperature": 0.1},
            MASKED_KEY_FIELD: mask_api_key(OLD_PLAINTEXT_KEY),
            FINGERPRINT_FIELD: api_key_fingerprint(OLD_PLAINTEXT_KEY),
        }
        assert OLD_PLAINTEXT_KEY not in json.dumps(out)

    def test_used_summary_legacy_task_marks_live(self):
        """存量任务（无快照）：used.model.source=live（回退路径可归因）"""
        live = _model_config_row(model_id="mc_live", endpoint=NEW_ENDPOINT)
        db = _db_returning({"mc_live": live})
        task = SimpleNamespace(
            evaluation_type="judge",
            judge_engine="graph",
            model_config=None,
            model_provider="mc_live",
            judge_model_config=None,
            judge_model=None,
            consistency_config=None,
        )
        summary = build_used_config_summary(task, db)
        assert summary["model"]["source"] == "live"
        assert summary["model"]["endpoint"] == NEW_ENDPOINT


# ===========================================================================
# 8. update_task 快照刷新语义（编辑切模型生效 / 同值不重置锚点）
# ===========================================================================


class TestUpdateTaskSnapshotRefresh:
    def _service_with_task(self, task):
        from app.services.evaluation_task_service import EvaluationTaskService

        service = EvaluationTaskService(MagicMock())
        service.get_task = MagicMock(return_value=task)
        return service

    def _task(self, pinned_model_id="mc_old"):
        return SimpleNamespace(
            id="task_w4",
            evaluation_type="judge",
            model_provider=pinned_model_id,
            model_config=(
                {"snapshot": True, "snapshot_at": "2026-10-13T00:00:00+00:00",
                 "model_id": pinned_model_id, "endpoint": OLD_ENDPOINT}
                if pinned_model_id else None
            ),
            judge_model=None,
            judge_model_config=None,
            consistency_config=None,
            score_details="",
            evaluation_metrics=[],
            judge_variable_config=None,
            evaluation_targets=[{"id": "t1", "name": "模型输出", "source": "model_output"}],
            dataset_id=None,
        )

    def test_same_provider_roundtrip_keeps_snapshot(self, monkeypatch):
        """表单回传同值：不重建快照（snapshot_at 不变，口径锚点稳定）"""
        from app.services import config_snapshot as cs_module

        task = self._task("mc_old")
        calls = []
        monkeypatch.setattr(
            cs_module, "build_model_config_snapshot",
            lambda db, mid: calls.append(mid) or {"snapshot": True, "snapshot_at": "REBUILT"},
        )
        service = self._service_with_task(task)

        service.update_task("task_w4", model_provider="mc_old")

        assert calls == [], "同值回传不应重建快照"
        assert task.model_config["snapshot_at"] == "2026-10-13T00:00:00+00:00"

    def test_switch_provider_rebuilds_snapshot(self, monkeypatch):
        """显式切换被评测模型：快照重建为新模型口径"""
        from app.services import config_snapshot as cs_module

        task = self._task("mc_old")
        calls = []
        monkeypatch.setattr(
            cs_module, "build_model_config_snapshot",
            lambda db, mid: calls.append(mid)
            or {"snapshot": True, "snapshot_at": "NEW", "model_id": mid,
                "endpoint": NEW_ENDPOINT},
        )
        service = self._service_with_task(task)

        service.update_task("task_w4", model_provider="mc_new")

        assert calls == ["mc_new"]
        assert task.model_config["model_id"] == "mc_new"
        assert task.model_config["endpoint"] == NEW_ENDPOINT

    def test_switch_judge_model_rebuilds_judge_snapshot(self, monkeypatch):
        from app.services import config_snapshot as cs_module

        task = self._task("mc_old")
        calls = []
        monkeypatch.setattr(
            cs_module, "build_model_config_snapshot",
            lambda db, mid: calls.append(mid) or {"snapshot": True, "model_id": mid},
        )
        service = self._service_with_task(task)

        service.update_task("task_w4", judge_model="mc_judge_new")

        assert calls == ["mc_judge_new"]
        assert task.judge_model_config["model_id"] == "mc_judge_new"

    def test_legacy_task_gets_snapshot_on_first_explicit_model_update(self, monkeypatch):
        """存量任务（无快照）首次显式携带模型字段：按当前配置补建快照"""
        from app.services import config_snapshot as cs_module

        task = self._task(None)  # 存量任务：model_provider 有值但 model_config 空
        task.model_provider = "mc_live"
        calls = []
        monkeypatch.setattr(
            cs_module, "build_model_config_snapshot",
            lambda db, mid: calls.append(mid) or {"snapshot": True, "model_id": mid},
        )
        service = self._service_with_task(task)

        service.update_task("task_w4", model_provider="mc_live")

        assert calls == ["mc_live"]
        assert task.model_config["model_id"] == "mc_live"


# ===========================================================================
# 9. API 序列化透出
# ===========================================================================


class TestExecutionRecordApiResponse:
    def test_response_model_has_config_snapshot_field(self):
        """执行记录 API 响应模型透出 config_snapshot 字段（前端可查归因锚点）"""
        from app.api.evaluation_task_api import ExecutionRecordResponse
        from datetime import datetime

        resp = ExecutionRecordResponse(
            id="rec-1", task_id="t", execution_number=1, status="completed",
            started_at=datetime.now(),
            config_snapshot={
                "model": {"source": "snapshot", "endpoint": OLD_ENDPOINT},
                "judge": None,
                "used": {"model": {"source": "snapshot"}, "judge": None},
            },
        )
        assert resp.config_snapshot["model"]["endpoint"] == OLD_ENDPOINT
        assert resp.config_snapshot["used"]["model"]["source"] == "snapshot"
