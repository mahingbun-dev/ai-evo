"""W14 集成测试：轻端点真实链路 + WS hub 导入 job 通道（测试角色补充）

开发交付的 tests/unit/test_w14_*.py 全部基于 mock（不触真实 DB/WS/Redis）。
本文件补真实链路验证（参照 tests/integration/test_ws_auth.py 的 W5 先例）：

1. GET /api/v1/evaluation/tasks/{task_id}/status（HTTP 真链路）：
   - 路由已在 app 注册（单测只调了函数，未证注册）；
   - 真实行 progress/completed_questions/total_questions 为 NULL → 四字段归零；
   - 真实行 running/40/4/10 → 返回精确四字段；
   - 404 detail 与 detail 端点（GET /{task_id}）逐字一致（「任务不存在」）。
2. WS /ws/task/{hubjob_id} 归属校验扩展（真实连接 + 真实 Redis PubSub 推送）：
   - 创建人 cookie → 连接成功并收到 hub_import_progress 推送（发布形状对齐
     app/services/hub_import_job_service._publish_job_progress）；
   - 非创建人 → 4403；超管（非创建人）→ 连接成功；
   - hubjob_ 前缀但两表查无 → 4404。

数据纪律：仅插入/删除自有 w14it_ 前缀行（evaluation_tasks / hub_import_jobs /
users / teams），不触碰共享库既有数据；SSO 网关一律 mock，不触真实网关。
"""
import json
import time
import uuid

import pytest
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration


PREFIX = "w14it_"


# ---------------- 数据自建自清（仅动 w14it_ 前缀自有行） ----------------

@pytest.fixture(autouse=True)
def w14_data_guard():
    """每用例前后清理本文件自建的 w14it_ 前缀行；库不可达时降级 no-op"""
    import os

    from sqlalchemy import create_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        yield
        return
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception:  # noqa: BLE001 - 清理失败只跳过，不影响 skip 语义
        yield
        return

    def _cleanup():
        with engine.begin() as conn:
            conn.execute(text("delete from hub_import_jobs where id like :p"), {"p": f"{PREFIX}%"})
            conn.execute(text("delete from evaluation_tasks where id like :p"), {"p": f"{PREFIX}%"})
            conn.execute(text("delete from users where user_code like :p"), {"p": f"{PREFIX}%"})
            conn.execute(text("delete from teams where team_code like :p"), {"p": f"{PREFIX}%"})

    _cleanup()
    yield
    _cleanup()
    engine.dispose()


def insert_task(shared_db_engine, task_id: str, team_code: str = "DEFAULT"):
    """裸 SQL 插入任务行：progress/total_questions/completed_questions 留 NULL
    （DB 层无 server_default，正是「None 归零」的真实数据形态）"""
    with shared_db_engine.begin() as conn:
        conn.execute(
            text(
                "insert into evaluation_tasks (id, name, evaluation_type, status, version, team_code) "
                "values (:id, :name, 'judge', :status, 1, :team)"
            ),
            {"id": task_id, "name": "W14 integration task", "status": "running", "team": team_code},
        )


def insert_hub_job(shared_db_engine, job_id: str, created_by):
    """插入 hub 导入 job 行（dataset_id 无 FK，可指向占位值）"""
    with shared_db_engine.begin() as conn:
        conn.execute(
            text(
                "insert into hub_import_jobs (id, dataset_id, file_ids, status, total_count, created_by) "
                "values (:id, :ds, :fids, 'running', 3, :created_by)"
            ),
            {
                "id": job_id,
                "ds": f"{PREFIX}ds_placeholder",
                "fids": json.dumps(["f1", "f2", "f3"]),
                "created_by": created_by,
            },
        )


def insert_w14_user(user_code: str, org_code: str = "DEFAULT", super_admin_flag: int = 0):
    """插入真实用户行（本地 JWT 链路：decode_token → users 表加载，w14_data_guard 清理）"""
    import os

    from sqlalchemy import create_engine

    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into users (user_code, username, org_code, super_admin_flag, is_active) "
                "values (:c, :c, :org, :flag, true) "
                "on conflict (user_code) do update set org_code = :org, super_admin_flag = :flag"
            ),
            {"c": user_code, "org": org_code, "flag": super_admin_flag},
        )
    engine.dispose()


def auth_header(user_code: str) -> dict:
    """HTTP 链 Authorization 头（与登录端点同一签发链路）"""
    from app.services.jwt_service import create_token

    return {"authorization": f"Bearer {create_token(user_code, user_code)}"}


def ws_token_query(user_code: str) -> str:
    """WS 握手 query 参数（握手阶段无法自定义 header）"""
    from urllib.parse import quote

    from app.services.jwt_service import create_token

    return f"?token={quote(create_token(user_code, user_code))}"


def wait_channel_subscribed(local_redis, channel: str, timeout: float = 5.0):
    """等订阅在 Redis 侧真正生效后再 publish（W5 同手法）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if channel in local_redis.pubsub_channels(channel):
            return
        time.sleep(0.05)
    pytest.fail(f"频道 {channel} 未在 {timeout}s 内完成订阅")


def hub_progress_message(job_id: str, **overrides) -> dict:
    """发布消息形状对齐 hub_import_job_service._publish_job_progress"""
    msg = {
        "type": "hub_import_progress",
        "job_id": job_id,
        "status": "running",
        "progress": 33,
        "success_count": 1,
        "fail_count": 0,
        "total": 3,
        "timestamp": "2026-10-13T00:00:00+08:00",
    }
    msg.update(overrides)
    return msg


# ---------------- HTTP 轻端点（真实 DB + 真实路由） ----------------

class TestLightEndpointRealChain:
    def test_route_registered_on_app(self, ws_app_client):
        """轻端点必须真的挂在 app 路由表上（单测只证了函数行为）"""
        paths = {
            getattr(r, "path", "")
            for r in ws_app_client.app.routes
            if "GET" in getattr(r, "methods", set())
        }
        assert "/api/v1/evaluation/tasks/{task_id}/status" in paths

    def test_real_row_null_scalars_coerced_to_zero(self, ws_app_client, shared_db_engine, monkeypatch):
        """真实行三标量列为 NULL → 四字段归零（or 0 兜底在真链路生效）"""
        task_id = f"{PREFIX}task_null"
        insert_task(shared_db_engine, task_id)
        insert_w14_user(f"{PREFIX}u1")

        resp = ws_app_client.get(
            f"/api/v1/evaluation/tasks/{task_id}/status", headers=auth_header(f"{PREFIX}u1")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "running", "progress": 0, "completed_questions": 0, "total": 0}

    def test_real_row_running_returns_exact_four_fields(self, ws_app_client, shared_db_engine, monkeypatch):
        task_id = f"{PREFIX}task_run"
        insert_task(shared_db_engine, task_id)
        with shared_db_engine.begin() as conn:
            conn.execute(
                text("update evaluation_tasks set progress=40, completed_questions=4, total_questions=10 where id=:i"),
                {"i": task_id},
            )
        insert_w14_user(f"{PREFIX}u1")

        resp = ws_app_client.get(
            f"/api/v1/evaluation/tasks/{task_id}/status", headers=auth_header(f"{PREFIX}u1")
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "running", "progress": 40, "completed_questions": 4, "total": 10}

    def test_404_semantics_identical_to_detail_endpoint(self, ws_app_client, monkeypatch):
        """/status 与 detail 的 404 detail 文案逐字一致（交付摘要承诺）"""
        insert_w14_user(f"{PREFIX}u404")
        missing = f"{PREFIX}task_missing_{uuid.uuid4().hex[:6]}"

        r_status = ws_app_client.get(
            f"/api/v1/evaluation/tasks/{missing}/status", headers=auth_header(f"{PREFIX}u404")
        )
        r_detail = ws_app_client.get(
            f"/api/v1/evaluation/tasks/{missing}", headers=auth_header(f"{PREFIX}u404")
        )
        assert r_status.status_code == 404
        assert r_detail.status_code == 404
        assert r_status.json()["detail"] == r_detail.json()["detail"] == "任务不存在"


# ---------------- WS hub 导入 job 通道（真实连接 + 真实 Redis 推送） ----------------

class TestWsHubJobChannelRealChain:
    def test_creator_connects_and_receives_hub_import_progress(
        self, ws_app_client, shared_db_engine, local_redis, monkeypatch
    ):
        """创建人订阅成功：Redis 发布 hub_import_progress → 客户端原样收到"""
        job_id = f"hubjob_{PREFIX}{uuid.uuid4().hex[:6]}"
        insert_hub_job(shared_db_engine, job_id, created_by=f"{PREFIX}creator")
        insert_w14_user(f"{PREFIX}creator")

        channel = f"task_status:{job_id}"
        with ws_app_client.websocket_connect(
            f"/ws/task/{job_id}{ws_token_query(f'{PREFIX}creator')}"
        ) as ws:
            first = ws.receive_json()
            assert first["type"] == "connected"
            assert first["task_id"] == job_id

            wait_channel_subscribed(local_redis, channel)
            local_redis.publish(channel, json.dumps(hub_progress_message(job_id)))

            pushed = ws.receive_json()
            assert pushed["type"] == "hub_import_progress"
            assert pushed["job_id"] == job_id
            assert pushed["status"] == "running"
            assert pushed["progress"] == 33
            assert pushed["success_count"] == 1
            assert pushed["total"] == 3

    def test_other_user_rejected_4403(self, ws_app_client, shared_db_engine, monkeypatch):
        """非创建人（非超管）订阅他人 job → 4403"""
        job_id = f"hubjob_{PREFIX}{uuid.uuid4().hex[:6]}"
        insert_hub_job(shared_db_engine, job_id, created_by=f"{PREFIX}creator")
        insert_w14_user(f"{PREFIX}intruder", "OTHER_TEAM")

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(
                f"/ws/task/{job_id}{ws_token_query(f'{PREFIX}intruder')}"
            ):
                pass
        assert exc_info.value.code == 4403

    def test_super_admin_allowed_even_not_creator(self, ws_app_client, shared_db_engine, monkeypatch):
        """超管（非创建人）订阅放行——与交付摘要口径一致"""
        job_id = f"hubjob_{PREFIX}{uuid.uuid4().hex[:6]}"
        insert_hub_job(shared_db_engine, job_id, created_by=f"{PREFIX}creator")
        insert_w14_user(f"{PREFIX}root", "ROOT", 1)

        with ws_app_client.websocket_connect(
            f"/ws/task/{job_id}{ws_token_query(f'{PREFIX}root')}"
        ) as ws:
            first = ws.receive_json()
            assert first["type"] == "connected"
            assert first["task_id"] == job_id

    def test_unknown_id_rejected_4404(self, ws_app_client, monkeypatch):
        """hubjob_ 前缀但两表均查无 → 4404（真实链路版回归）"""
        insert_w14_user(f"{PREFIX}u404")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(
                f"/ws/task/hubjob_{PREFIX}no_such_{uuid.uuid4().hex[:6]}{ws_token_query(f'{PREFIX}u404')}",
            ):
                pass
        assert exc_info.value.code == 4404
