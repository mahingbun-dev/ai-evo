"""
W5 WebSocket 鉴权与归属校验集成测试（整改计划 §2「W5 测试设计」+ 验收标准）

覆盖计划要求的用例（本地 JWT 形态：握手 query 参数 ?token= 携带，
鉴权链路走真实 decode_token → users 表加载，与 auth_middleware 同构）：
1. 无 token / 无效 token 连接 → 4401（三个 WS 端点：/ws/task/{task_id}、
   /api/v1/ab-test/ws/run、/api/v1/models/ws/test/{model_id}）；
2. 有效 token → 连接成功、心跳 pong、可收 Redis PubSub 推送；
3. 跨团队任务订阅 → 拒绝（4403）；任务不存在 → 4404；超管跨团队放行；
4. 已禁用账号 → 4403（W19 同口径）。

测试用户/任务行自建自清（conftest 的 w5_test_data_guard 按 w5it_/W5IT_
前缀清理）；Redis 用本地 6379（local_redis fixture）。
"""
import json
import time

import pytest
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

import app.services.ws_auth as ws_auth_module
from app.services.jwt_service import create_token

pytestmark = pytest.mark.integration

AB_TEST_WS_URL = "/api/v1/ab-test/ws/run"
MODEL_TEST_WS_URL = "/api/v1/models/ws/test/w5it_no_such_model"


def insert_user(shared_db_engine, user_code: str, org_code: str, super_admin_flag: int = 0, is_active: bool = True):
    """插入测试用户（W5IT_ 前缀 org_code 随 w5_test_data_guard 清理）"""
    with shared_db_engine.begin() as conn:
        conn.execute(
            text(
                "insert into users (user_code, username, org_code, super_admin_flag, is_active) "
                "values (:code, :name, :org, :admin, :active) "
                "on conflict (user_code) do update set org_code = :org, "
                "super_admin_flag = :admin, is_active = :active"
            ),
            {"code": user_code, "name": f"{user_code}-name", "org": org_code,
             "admin": super_admin_flag, "active": is_active},
        )


def insert_task(shared_db_engine, task_id: str, team_code: str):
    """向共享库插入本测试自有的任务行（version 为 NOT NULL 无默认值，必须给）"""
    with shared_db_engine.begin() as conn:
        conn.execute(
            text(
                "insert into evaluation_tasks (id, name, evaluation_type, status, version, team_code) "
                "values (:id, :name, 'judge', 'pending', 1, :team)"
            ),
            {"id": task_id, "name": "W5 integration test task", "team": team_code},
        )


def ws_token(user_code: str) -> str:
    """签发真实 JWT（与登录端点同一签发链路）"""
    return create_token(user_code, f"{user_code}-name")


class TestNoTokenRejected:
    """验收标准：三个 WS 端点未认证一律拒绝（close code 4401）"""

    def test_no_token_task_ws_rejected_4401(self, ws_app_client, shared_db_engine):
        insert_task(shared_db_engine, "w5it_task_nc", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect("/ws/task/w5it_task_nc"):
                pass  # 不应建立成功
        assert exc_info.value.code == 4401

    def test_no_token_ab_test_ws_rejected_4401(self, ws_app_client):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(AB_TEST_WS_URL):
                pass
        assert exc_info.value.code == 4401

    def test_no_token_model_test_ws_rejected_4401(self, ws_app_client):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(MODEL_TEST_WS_URL):
                pass
        assert exc_info.value.code == 4401

    def test_invalid_token_rejected_4401(self, ws_app_client, shared_db_engine):
        """token 存在但签名/格式无效 → 4401"""
        insert_task(shared_db_engine, "w5it_task_badtok", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect("/ws/task/w5it_task_badtok?token=not-a-jwt"):
                pass
        assert exc_info.value.code == 4401


class TestWsTaskAuth:
    """/ws/task/{task_id} 鉴权与最小归属校验"""

    def test_unknown_user_rejected_4401(self, ws_app_client, shared_db_engine):
        """token 有效但用户不在库 → 4401"""
        insert_task(shared_db_engine, "w5it_task_ghost", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(f"/ws/task/w5it_task_ghost?token={ws_token('w5it_user_ghost')}"):
                pass
        assert exc_info.value.code == 4401

    def test_valid_token_connect_and_heartbeat(self, ws_app_client, shared_db_engine):
        """有效 token：连接成功收 connected，ping 收 pong"""
        insert_user(shared_db_engine, "w5it_user_ok", "W5IT_A")
        insert_task(shared_db_engine, "w5it_task_ok", "W5IT_A")

        with ws_app_client.websocket_connect(f"/ws/task/w5it_task_ok?token={ws_token('w5it_user_ok')}") as ws:
            first = ws.receive_json()
            assert first["type"] == "connected"
            assert first["task_id"] == "w5it_task_ok"

            ws.send_text("ping")
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_valid_token_receives_pubsub_push(self, ws_app_client, shared_db_engine, local_redis):
        """有效 token：首连接即可收推送（Redis publish → PubSubListener → broadcast）"""
        task_id = "w5it_task_push"
        insert_user(shared_db_engine, "w5it_user_push", "W5IT_A")
        insert_task(shared_db_engine, task_id, "W5IT_A")

        channel = f"task_status:{task_id}"
        with ws_app_client.websocket_connect(f"/ws/task/{task_id}?token={ws_token('w5it_user_push')}") as ws:
            first = ws.receive_json()
            assert first["type"] == "connected"

            # 等订阅在 Redis 侧生效（订阅命令真正到达服务器）后再 publish
            deadline = time.time() + 5
            while time.time() < deadline:
                if channel in local_redis.pubsub_channels(channel):
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"频道 {channel} 未在 5s 内完成订阅")

            local_redis.publish(
                channel,
                json.dumps(
                    {
                        "type": "status_update",
                        "task_id": task_id,
                        "status": "running",
                        "progress": 50,
                    }
                ),
            )
            # 推送经监听线程（get_message 轮询 0.5s）+ broadcast 到达，阻塞等待；
            # 若链路断裂，由外层命令级 timeout 兜底判失败
            pushed = ws.receive_json()
            assert pushed["type"] == "status_update"
            assert pushed["task_id"] == task_id
            assert pushed["status"] == "running"
            assert pushed["progress"] == 50

    def test_cross_team_subscription_rejected_4403(self, ws_app_client, shared_db_engine):
        """跨团队任务订阅被拒（task.team_code ≠ user.org_code，非超管）→ 4403"""
        insert_user(shared_db_engine, "w5it_user_teamB", "W5IT_B")
        insert_task(shared_db_engine, "w5it_task_teamA", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(f"/ws/task/w5it_task_teamA?token={ws_token('w5it_user_teamB')}"):
                pass
        assert exc_info.value.code == 4403

    def test_disabled_user_rejected_4403(self, ws_app_client, shared_db_engine):
        """W19：已禁用账号连接被拒（与 auth_middleware 的 403 同口径）→ 4403"""
        insert_user(shared_db_engine, "w5it_user_disabled", "W5IT_A", is_active=False)
        insert_task(shared_db_engine, "w5it_task_dis", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(f"/ws/task/w5it_task_dis?token={ws_token('w5it_user_disabled')}"):
                pass
        assert exc_info.value.code == 4403

    def test_task_not_found_rejected_4404(self, ws_app_client, shared_db_engine):
        """任务不存在 → 4404（先于归属判断）"""
        insert_user(shared_db_engine, "w5it_user_404", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(f"/ws/task/w5it_task_does_not_exist?token={ws_token('w5it_user_404')}"):
                pass
        assert exc_info.value.code == 4404

    def test_super_admin_cross_team_allowed(self, ws_app_client, shared_db_engine):
        """超管（super_admin_flag=1）跨团队订阅放行"""
        insert_user(shared_db_engine, "w5it_user_admin", "W5IT_ADMIN", super_admin_flag=1)
        insert_task(shared_db_engine, "w5it_task_admin", "W5IT_A")
        with ws_app_client.websocket_connect(f"/ws/task/w5it_task_admin?token={ws_token('w5it_user_admin')}") as ws:
            first = ws.receive_json()
            assert first["type"] == "connected"
            assert first["task_id"] == "w5it_task_admin"


class TestOtherWsEndpointsAuthed:
    """鉴权通过后其余两个 WS 端点功能可用（不触真实模型网关的路径）"""

    def test_ab_test_ws_valid_token_functional(self, ws_app_client, shared_db_engine):
        """有效 token 连 /ws/run：端点 accept 并正常处理输入（空 model id → error）"""
        insert_user(shared_db_engine, "w5it_user_ab", "W5IT_A")
        with ws_app_client.websocket_connect(f"{AB_TEST_WS_URL}?token={ws_token('w5it_user_ab')}") as ws:
            ws.send_text(json.dumps({"model_a_id": "", "model_b_id": "", "variables": {}}))
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "model_a_id" in msg["content"]

    def test_model_test_ws_unknown_model_rejected_4404(self, ws_app_client, shared_db_engine):
        """有效 token 连 /ws/test/{model_id}：模型不存在时 W19 归属校验 close(4404)（非 4401）"""
        insert_user(shared_db_engine, "w5it_user_mt", "W5IT_A")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws_app_client.websocket_connect(f"{MODEL_TEST_WS_URL}?token={ws_token('w5it_user_mt')}"):
                pass
        assert exc_info.value.code == 4404
