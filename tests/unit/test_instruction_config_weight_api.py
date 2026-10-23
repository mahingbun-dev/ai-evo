"""评测模板权重类型兼容（str / 数值）API 回归测试

背景（缺陷复现）：
    任务页「保存为模板」把任务的 evaluation_metrics 原样 POST 给模板接口，
    任务里 weight 是数字（如 0.4/0.6，远端 task_4f3035370420 / task_9965d1479b0e
    的维度「完整性」「准确性」），而 EvaluationMetricModel.weight 原声明为 str，
    导致 Pydantic 请求校验层直接 422：
        [{"type":"string_type","loc":["body","evaluation_metrics",0,"weight"],
          "msg":"Input should be a valid string","input":0.4}, ...]

修复：weight 改为 Union[str, float]（app/api/instruction_config_api.py:21）。

本测试刻意用 fastapi.testclient.TestClient 走真实请求链路（挂载 router +
Pydantic 请求校验层），而非直调内部函数——缺陷正落在这一层。
DB 用自建自清的 sqlite 临时库，绝不触碰远端库。
"""
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, get_db
from app.models.template_models import EvaluationTemplate  # noqa: F401 建表需要
from app.api.instruction_config_api import router as instruction_router

pytestmark = pytest.mark.unit

# ---- 自建自清 sqlite 临时库，隔离于任何远端/本地 postgres ----
_DB_DIR = tempfile.mkdtemp(prefix="weight_api_test_")
_DB_PATH = os.path.join(_DB_DIR, "weight.sqlite")
_engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})
_TestingSessionLocal = sessionmaker(bind=_engine, autoflush=False)
Base.metadata.create_all(bind=_engine)

# 独立测试 app：挂载生产同款 router 前缀（settings.API_V1_STR="/api/v1"）
_test_app = FastAPI()

# M4 QE4-3：模板写端点挂了 RBAC 闸（ensure_permission(request.state.user,...)），
# 本测试 app 不带鉴权中间件——注入 engineer 用户上下文补齐（生产链路该
# 上下文由 AuthMiddleware 设置）
from starlette.middleware.base import BaseHTTPMiddleware


class _UserInjectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.user = {
            "user_code": "u_test_engineer", "super_admin_flag": 0,
            "role": "engineer", "org_code": "QE",
        }
        return await call_next(request)


_test_app.add_middleware(_UserInjectMiddleware)
_test_app.include_router(instruction_router, prefix="/api/v1")


def _override_get_db():
    db = _TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


_test_app.dependency_overrides[get_db] = _override_get_db
_client = TestClient(_test_app)

BASE = "/api/v1/instruction/config"

# 与远端真实任务 task_4f3035370420 / task_9965d1479b0e 完全对齐的数字权重维度
_REAL_TASK_METRICS = [
    {"id": "metric_b1", "name": "完整性", "weight": 0.4, "max_score": 10,
     "description": "B1 步骤完整性", "score_criteria": {}, "dependencies": []},
    {"id": "metric_b2", "name": "准确性", "weight": 0.6, "max_score": 10,
     "description": "B2 行准确度", "score_criteria": {}, "dependencies": []},
]


def _create(payload):
    resp = _client.post(BASE, json=payload)
    return resp


def _read_metrics(config_id):
    resp = _client.get(f"{BASE}/{config_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["evaluation_metrics"]


def _raw_db_row(config_id):
    """直读 sqlite 的模板行（验证 JSON 列落库真实类型 / instruction_template）"""
    db = _TestingSessionLocal()
    try:
        row = db.query(EvaluationTemplate).filter(EvaluationTemplate.id == config_id).first()
        assert row is not None
        return row
    finally:
        db.close()


def _raw_db_metrics(config_id):
    return _raw_db_row(config_id).evaluation_metrics


class TestOriginalDefectReproduction:
    """1. 原缺陷复现回归：真实任务数字权重保存不再 422"""

    def test_numeric_weight_04_06_POST_no_422(self):
        r = _create({"name": "回归-数字权重", "scene": "测试执行质量B线",
                     "evaluation_metrics": _REAL_TASK_METRICS, "max_score": 10})
        assert r.status_code == 200, f"期望 200，实际 {r.status_code}: {r.text}"
        cid = r.json()["id"]
        metrics = _read_metrics(cid)
        assert [m["weight"] for m in metrics] == [0.4, 0.6], metrics
        # 落库真实类型
        raw = _raw_db_metrics(cid)
        assert all(isinstance(m["weight"], (int, float)) for m in raw), raw


class TestEquivalenceClasses:
    """2. 等价类：数字/整数/字符串(中英文)/混合"""

    @pytest.mark.parametrize("weight,expect_num", [
        (0.4, True), (0.6, True), (1, True),
        ("medium", False), ("high", False), ("低", False),
    ])
    def test_single_type_POST(self, weight, expect_num):
        r = _create({"name": f"等价-{weight}", "scene": "s",
                     "evaluation_metrics": [{"name": "d", "weight": weight}], "max_score": 10})
        assert r.status_code == 200, r.text
        got = _read_metrics(r.json()["id"])[0]["weight"]
        if expect_num:
            assert float(got) == float(weight)
        else:
            assert got == weight

    def test_mixed_list_POST(self):
        r = _create({"name": "等价-混合", "scene": "s", "evaluation_metrics": [
            {"name": "a", "weight": 0.4}, {"name": "b", "weight": "高"},
            {"name": "c", "weight": 1}], "max_score": 10})
        assert r.status_code == 200, r.text
        m = _read_metrics(r.json()["id"])
        assert float(m[0]["weight"]) == 0.4 and m[1]["weight"] == "高" and float(m[2]["weight"]) == 1

    def test_PUT_numeric_weight(self):
        cid = _create({"name": "PUT-基线", "scene": "s",
                       "evaluation_metrics": [{"name": "d", "weight": "中"}], "max_score": 10}).json()["id"]
        r = _client.put(f"{BASE}/{cid}", json={"evaluation_metrics": _REAL_TASK_METRICS})
        assert r.status_code == 200, f"PUT 期望 200，实际 {r.status_code}: {r.text}"
        assert [m["weight"] for m in _read_metrics(cid)] == [0.4, 0.6]


class TestStringCompatRegression:
    """3. 存量兼容：字符串权重（线上 74 维度实际形态）保存/更新/读取不受影响"""

    def test_string_weight_roundtrip(self):
        r = _create({"name": "存量-字符串", "scene": "s", "evaluation_metrics": [
            {"name": "完整性", "weight": "高"}, {"name": "准确性", "weight": "中"},
            {"name": "流畅性", "weight": "低"}], "max_score": 10})
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        m = _read_metrics(cid)
        assert [x["weight"] for x in m] == ["高", "中", "低"]
        # 更新仍为字符串
        r2 = _client.put(f"{BASE}/{cid}", json={"evaluation_metrics": [
            {"name": "完整性", "weight": "medium"}]})
        assert r2.status_code == 200, r2.text
        assert _read_metrics(cid)[0]["weight"] == "medium"


class TestDownstreamNoRegression:
    """4. 下游不回归：build 生成指令模板 + preview / validate 对数字权重不报错"""

    def test_build_generated_instruction_template_exists(self):
        # create 端点内部会调 InstructionBuilder.build 生成指令模板并落库；
        # 200 本身即证明 build 未抛错（build 在 db.add 之前调用），
        # 再直读 DB 的 instruction_template 列确认生成内容含数字权重文本。
        r = _create({"name": "下游-build", "scene": "s",
                     "evaluation_metrics": _REAL_TASK_METRICS, "max_score": 10})
        assert r.status_code == 200, r.text
        tmpl = _raw_db_row(r.json()["id"]).instruction_template
        assert tmpl, "instruction_template 未生成"
        assert "权重：0.4" in tmpl and "权重：0.6" in tmpl, tmpl

    def test_preview_numeric_weight(self):
        r = _client.post("/api/v1/instruction/preview", json={
            "scene": "s", "evaluation_metrics": _REAL_TASK_METRICS, "max_score": 10})
        assert r.status_code == 200, r.text
        assert r.json()["full_prompt"]

    def test_validate_numeric_weight(self):
        r = _client.post("/api/v1/instruction/validate", json={
            "scene": "s", "evaluation_metrics": _REAL_TASK_METRICS, "max_score": 10})
        assert r.status_code == 200, r.text
        assert r.json()["valid"] is True, r.text


class TestIllegalTypesStillRejected:
    """5. 边界：非法类型仍应被拒绝，放宽未变成不校验"""

    @pytest.mark.parametrize("bad_weight", [[0.4, 0.6], {"v": 0.4}, None])
    def test_illegal_weight_rejected(self, bad_weight):
        r = _create({"name": "非法", "scene": "s",
                     "evaluation_metrics": [{"name": "d", "weight": bad_weight}], "max_score": 10})
        assert r.status_code == 422, f"weight={bad_weight!r} 期望 422，实际 {r.status_code}: {r.text}"

    def test_bool_weight_coerced_to_number_documented(self):
        """已知放宽边界（非回归）：Pydantic v2 中 bool 是 int 子类，Union[str, float]
        会把 True/False 收成 1.0/0.0。原 str 声明下 True 会 422。此差异仅影响
        weight=布尔 这种无意义输入，不构成真实场景风险，此处显式记录实际行为。"""
        r = _create({"name": "bool边界", "scene": "s",
                     "evaluation_metrics": [{"name": "d", "weight": True}], "max_score": 10})
        assert r.status_code == 200, r.text
        assert float(_read_metrics(r.json()["id"])[0]["weight"]) == 1.0
