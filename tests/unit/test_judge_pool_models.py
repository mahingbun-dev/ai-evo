"""并发池 ORM 模型结构测试（不连 DB，只校验映射元数据）"""
import pytest

from app.models.judge_pool_models import JudgePoolAuditLog, JudgePoolConfig
from app.models.task_models import EvaluationTask

pytestmark = pytest.mark.unit


def test_config_columns():
    cols = {c.name for c in JudgePoolConfig.__table__.columns}
    assert {
        "id", "graph_limit", "claude_agent_limit", "harness_limit",
        "plugin_limit", "lease_ttl_seconds", "heartbeat_interval_seconds",
        "wait_timeout_seconds", "scan_interval_seconds",
        "audit_retention_days", "fail_open", "updated_by", "updated_at",
    } <= cols


def test_audit_columns():
    cols = {c.name for c in JudgePoolAuditLog.__table__.columns}
    assert {
        "id", "event", "engine", "lease_id", "eval_task_id",
        "celery_task_id", "row_idx", "host", "pid", "wait_ms",
        "hold_ms", "detail", "created_at",
    } <= cols


def test_task_has_concurrency_column():
    assert "concurrency" in {c.name for c in EvaluationTask.__table__.columns}
