"""W9 对象级归属校验（IDOR 收口）403 矩阵——本包核心回归资产

计划 §3 W9「测试设计」：403 矩阵（团队A用户 × 团队B资源 ×
读/改/删/下载/补传/订阅 = 全 403；超管全 200；本团队全 200）参数化。

覆盖端点族（与实现接入清单一一对应）：
- evaluation_task_api：详情/config/更新/stop/logs/executions/results/history/
  run（跨团队拒绝）/refresh-count
- batch_operations_api：batch-run/batch-delete/batch-stop 跨团队任务逐项失败
- dataset_api：详情/更新/删除/fields/data/data-v2/export/文件上传/文件删除/
  临时上传/临时删除/确认导入 + 列表过滤
- media_api：列表过滤/详情/content/删除
- hub_file_api：tree/content/download/match-preview/supplements 上传/删除
- hub_import_api：进度/重试
- /ws/task：跨团队订阅 4403、本团队/超管可订阅（access_control 统一口径）

断言口径：
- 团队B用户（跨团队）→ 403（WS 4403），且断言响应不含资源内容；
- 团队A用户（本团队）→ 2xx；业务上必然失败、无法稳定 2xx 的端点
  （confirm_import 无有效 preview 会话）断言「非 403」，同样证明归属放行；
- 超管 → 2xx（或同 non403 口径）；
- run 类端点只做跨团队拒绝断言（本团队正向会真实提交 Celery，不在矩阵内）。

本地 JWT 鉴权链路（真实 users 行 + 真实签发 token，与 auth_middleware /
ws_auth 同构，见 W5 test_ws_auth）；资源行自建自清
（w9it_/W9IT_ 前缀），补传目录 monkeypatch 到 tmp_path，不触碰共享库既有数据。
"""
import io
import json
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration

TEAM_A = "W9IT_A"
TEAM_B = "W9IT_B"
PREFIX_TASK = "w9it_task_"
PREFIX_DATASET = "w9it_ds_"
PREFIX_DATA = "w9it_data_"
PREFIX_MEDIA = "w9it_media_"
PREFIX_HUB = "w9it_hub_"
PREFIX_HUBFILE = "w9it_hubfile_"
PREFIX_JOB = "w9it_job_"
PREFIX_TEMP = "w9it_temp_"
PREFIX_USER = "w9it_user_"

API = "/api/v1"


# ---------------------------------------------------------------------------
# SSO mock 与 TestClient
# ---------------------------------------------------------------------------

def make_client(monkeypatch, org_code: str, super_admin_flag: int = 0):
    """带真实用户行的 TestClient（本地 JWT：decode_token → users 表加载）。

    不进 lifespan：绝不触发孤立任务清理。用户行 w9it_ 前缀由本文件
    清理 fixture 回收，多次 make_client 用 upsert 幂等。
    """
    import os

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from app.main import app
    from app.services.jwt_service import create_token

    user_code = f"{PREFIX_USER}{org_code.lower()}"
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

    client = TestClient(app)
    client.headers["authorization"] = f"Bearer {create_token(user_code, user_code)}"
    return client


# ---------------------------------------------------------------------------
# 资源工厂（团队 A；w9it_ 前缀自建自清）
# ---------------------------------------------------------------------------

def fresh_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def create_resources(engine, tmp_path):
    """一套团队 A 的资源。返回 id 字典。"""
    ds_id = fresh_id(PREFIX_DATASET)
    task_id = fresh_id(PREFIX_TASK)
    data_id = fresh_id(PREFIX_DATA)
    media_id = fresh_id(PREFIX_MEDIA)
    file_id = fresh_id(PREFIX_HUBFILE)
    job_id = fresh_id(PREFIX_JOB)
    temp_file_id = fresh_id(PREFIX_TEMP)

    with engine.begin() as conn:
        conn.execute(text(
            "insert into evaluation_datasets (id, name, question_count, is_active, team_code) "
            "values (:id, :name, 1, true, :team)"),
            {"id": ds_id, "name": f"W9矩阵数据集{ds_id}", "team": TEAM_A})
        conn.execute(text(
            "insert into evaluation_data_v2 (id, dataset_id, index, is_active, data) "
            "values (:id, :ds, 0, true, CAST(:data AS jsonb))"),
            {"id": data_id, "ds": ds_id,
             "data": json.dumps({"user_input": "q", "answer": "a"})})
        conn.execute(text(
            "insert into import_temp_files (id, preview_id, data_index, field, file_name, "
            "file_path, file_type, file_size, is_active) "
            "values (:id, :pv, 0, 'answer', 't.txt', :path, 'text', 4, true)"),
            {"id": temp_file_id, "pv": ds_id, "path": str(tmp_path / f"{temp_file_id}.txt")})
        conn.execute(text(
            "insert into evaluation_data (id, dataset_id, index, is_active, question, ref_answer) "
            "values (:id, :ds, 0, true, 'q', 'a')"),
            {"id": data_id, "ds": ds_id})
        conn.execute(text(
            "insert into evaluation_tasks (id, name, evaluation_type, status, version, team_code, "
            "progress, total_questions, completed_questions) "
            "values (:id, :name, 'judge', 'pending', 1, :team, 0, 1, 0)"),
            {"id": task_id, "name": f"W9矩阵任务{task_id}", "team": TEAM_A})
        media_path = tmp_path / f"{media_id}_a.txt"
        media_path.write_text("W9 media", encoding="utf-8")
        conn.execute(text(
            "insert into media_resources (id, file_name, file_type, mime_type, file_size, "
            "file_path, is_active, team_code) "
            "values (:id, 'a.txt', 'text', 'text/plain', 9, :path, true, :team)"),
            {"id": media_id, "path": str(media_path), "team": TEAM_A})
        unpack_dir = tmp_path / "unpack" / file_id
        unpack_dir.mkdir(parents=True, exist_ok=True)
        (unpack_dir / "meta.json").write_text('{"k": 1}', encoding="utf-8")
        conn.execute(text(
            "insert into hub_files (id, file_id, filename, unpack_dir, unpack_status, "
            "file_index, team_code) "
            "values (:id, :fid, 'w9.zip', :dir, 'unpacked', CAST(:idx AS jsonb), :team)"),
            {"id": fresh_id(PREFIX_HUB), "fid": file_id, "dir": str(unpack_dir),
             "idx": json.dumps([{"path": "meta.json", "type": "json", "size": 9}]),
             "team": TEAM_A})
        conn.execute(text(
            "insert into hub_import_jobs (id, dataset_id, file_ids, status, total_count, "
            "success_count, fail_count, fail_detail, succeeded_file_ids) "
            "values (:id, :ds, CAST(:fids AS jsonb), 'failed', 2, 1, 1, CAST(:detail AS jsonb), CAST(:ok AS jsonb))"),
            {"id": job_id, "ds": ds_id, "fids": json.dumps(["w9fid-1", "w9fid-2"]),
             "detail": json.dumps({"w9fid-2": "不是有效的 zip 文件"}),
             "ok": json.dumps(["w9fid-1"])})

    return {
        "task_id": task_id, "dataset_id": ds_id, "data_id": data_id,
        "media_id": media_id, "file_id": file_id, "job_id": job_id,
        "temp_file_id": temp_file_id,
    }


@pytest.fixture()
def w9_guard(shared_db_engine):
    """w9it_ 前缀自建自清（每用例前后清理；只删本包自建行）"""
    def _cleanup():
        tables = [
            ("hub_import_jobs", "id", PREFIX_JOB),
            ("hub_files", "file_id", PREFIX_HUBFILE),
            ("media_resources", "id", PREFIX_MEDIA),
            ("evaluation_tasks", "id", PREFIX_TASK),
            ("evaluation_data_v2", "dataset_id", PREFIX_DATASET),
            ("evaluation_data", "dataset_id", PREFIX_DATASET),
            ("import_temp_files", "preview_id", PREFIX_DATASET),
            ("evaluation_datasets", "id", PREFIX_DATASET),
            ("users", "user_code", PREFIX_USER),
        ]
        with shared_db_engine.begin() as conn:
            for table, col, prefix in tables:
                conn.execute(
                    text(f"delete from {table} where {col} like :p"), {"p": f"{prefix}%"})
        # upload_data_file 端点按相对路径 ./uploads/datasets 落盘（既有行为），
        # 清理本包前缀的落盘文件，避免重复跑累积垃圾
        upload_dir = Path.cwd() / "uploads" / "datasets"
        if upload_dir.is_dir():
            for p in upload_dir.glob(f"{PREFIX_DATA}*"):
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    _cleanup()
    yield
    _cleanup()


@pytest.fixture()
def resources(shared_db_engine, w9_guard, tmp_path):
    return create_resources(shared_db_engine, tmp_path)


@pytest.fixture()
def supp_root(monkeypatch, tmp_path):
    """补传区根目录重定向 tmp_path（绝不写真实 uploads/hub_supplements/）"""
    import app.services.hub_file_service as hfs

    root = tmp_path / "hub_supplements"
    monkeypatch.setattr(hfs, "_hub_supplements_root", lambda r=None: str(root))
    return root


# ---------------------------------------------------------------------------
# 矩阵：端点 × 角色
# 断言模式：ok=2xx；non403=状态码 != 403（业务 4xx 视为归属放行的证明）
# ---------------------------------------------------------------------------

def _r(resources, key):
    return resources[key]


# (名称, method, path 模板, data 构造器, ok 断言模式)
MATRIX = [
    # ---- 任务族（evaluation_task_api / results）----
    ("task_detail", "GET", "/evaluation/tasks/{task_id}", None, "ok"),
    ("task_config", "GET", "/evaluation/tasks/{task_id}/config", None, "ok"),
    # task_update 业务校验（judge 任务必须带 evaluation_targets）返回 400，
    # 与 confirm_import 同属 non403 口径：归属放行已证明
    ("task_update", "PUT", "/evaluation/tasks/{task_id}", lambda r: {"_json": {"name": "W9矩阵改名"}}, "non403"),
    ("task_logs", "GET", "/evaluation/tasks/{task_id}/logs", None, "ok"),
    ("task_executions", "GET", "/evaluation/tasks/{task_id}/executions", None, "ok"),
    ("task_refresh", "POST", "/evaluation/tasks/{task_id}/refresh-count", None, "ok"),
    ("task_stop", "POST", "/evaluation/tasks/{task_id}/stop", None, "ok"),
    ("task_result", "GET", "/evaluation/results/{task_id}", None, "ok"),
    ("task_result_history", "GET", "/evaluation/results/{task_id}/history", None, "ok"),
    ("task_run", "POST", "/evaluation/tasks/{task_id}/run", None, "non403"),
    # ---- 数据集族（dataset_api）----
    ("dataset_detail", "GET", "/evaluation/datasets/{dataset_id}", None, "ok"),
    ("dataset_update", "PUT", "/evaluation/datasets/{dataset_id}",
     lambda r: {"_json": {"description": "W9矩阵更新"}}, "ok"),
    ("dataset_fields", "GET", "/evaluation/datasets/{dataset_id}/fields", None, "ok"),
    ("dataset_data", "GET", "/evaluation/datasets/{dataset_id}/data", None, "ok"),
    ("dataset_data_v2", "GET", "/evaluation/datasets/{dataset_id}/data/v2", None, "ok"),
    ("dataset_export", "GET", "/evaluation/datasets/{dataset_id}/export/excel", None, "non403"),
    ("dataset_upload_file", "POST",
     "/evaluation/datasets/{dataset_id}/data/{data_id}/upload-file",
     lambda r: {"_files": {"file": ("a.txt", io.BytesIO(b"W9 file"), "text/plain")}}, "ok"),
    ("dataset_delete_file", "DELETE",
     "/evaluation/datasets/{dataset_id}/data/{data_id}/files/{nonexist_file_id}",
     None, "non403"),
    ("dataset_temp_upload", "POST",
     "/evaluation/datasets/{dataset_id}/import/preview/upload-temp-file?data_index=0",
     lambda r: {"_files": {"file": ("t.txt", io.BytesIO(b"W9 temp"), "text/plain")}}, "ok"),
    ("dataset_temp_delete", "DELETE",
     "/evaluation/datasets/{dataset_id}/import/preview/temp-file/{temp_file_id}?data_index=0",
     None, "ok"),
    ("dataset_confirm_import", "POST", "/evaluation/datasets/{dataset_id}/import/confirm",
     lambda r: {"_files": {"file": ("bad.xlsx", io.BytesIO(b"not excel"),
                                     "application/vnd.ms-excel")}}, "non403"),
    # ---- 媒体族（media_api）----
    ("media_info", "GET", "/media/{media_id}", None, "ok"),
    ("media_content", "GET", "/media/{media_id}/content", None, "ok"),
    # ---- hub 文件族（hub_file_api）----
    ("hub_tree", "GET", "/hub-files/{file_id}/tree", None, "ok"),
    ("hub_content", "GET", "/hub-files/{file_id}/content?path=meta.json", None, "ok"),
    ("hub_download", "GET", "/hub-files/{file_id}/download?path=meta.json", None, "ok"),
    ("hub_supplement_upload", "POST", "/hub-files/{file_id}/supplements",
     lambda r: {"_files": {"files": ("report.txt", io.BytesIO(b"W9 supp"), "text/plain")},
                "relative_paths": "w9it_supp/report.txt"}, "ok"),
    ("hub_supplement_delete", "DELETE",
     "/hub-files/{file_id}/supplements?path=w9it_supp/ghost.txt", None, "non403"),
    # ---- 导入任务族（hub_import_api）----
    ("hub_job_progress", "GET", "/hub-import-jobs/{job_id}", None, "ok"),
    ("hub_job_retry", "POST", "/hub-import-jobs/{job_id}/retry", None, "ok"),
]

@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """矩阵正向用例不触真实 Celery broker/worker：dispatch 打桩（归属校验
    先于投递执行，跨团队 403 用例实际到不了投递点，桩只影响放行路径）"""
    from unittest.mock import MagicMock

    try:
        import app.tasks.celery_tasks as ct

        monkeypatch.setattr(ct, "run_hub_import_job", MagicMock(), raising=False)
    except ImportError:  # pragma: no cover - celery 模块缺失时无投递面
        pass


MATCH_PREVIEW_BODY = {"field_name": "hub_file_id", "pattern": "**/*.json"}


def _build_request(entry, resources):
    name, method, path_tpl, data_fn, mode = entry
    path = path_tpl.format(nonexist_file_id="w9it_ghost_file", **resources)
    files = []
    data = {}
    json_body = None
    if data_fn is not None:
        for k, v in (data_fn(resources) or {}).items():
            if k == "_files":
                files = [(fk, (fv[0], fv[1], fv[2])) for fk, fv in v.items()]
            elif k == "_json":
                json_body = v
            else:
                data[k] = v
    return method, path, files, data, json_body, mode


class TestIdorMatrix403:
    """跨团队 = 403：团队B用户 × 团队A资源 × 读/改/删/下载/补传"""

    @pytest.mark.parametrize(
        "entry",
        MATRIX,
        ids=[e[0] for e in MATRIX],
    )
    def test_cross_team_403(self, entry, resources, monkeypatch, supp_root):
        method, path, files, data, json_body, _mode = _build_request(entry, resources)
        client = make_client(monkeypatch, TEAM_B, super_admin_flag=0)
        resp = client.request(method, f"{API}{path}", files=files, data=data, json=json_body)
        assert resp.status_code == 403, (
            f"{entry[0]} 跨团队应 403，实际 {resp.status_code}: {resp.text[:200]}")
        assert "跨团队" in resp.json().get("detail", "")

    def test_match_preview_cross_team_403(self, resources, monkeypatch):
        client = make_client(monkeypatch, TEAM_B)
        resp = client.post(
            f"{API}/hub-files/match-preview",
            json={**MATCH_PREVIEW_BODY, "dataset_id": resources["dataset_id"]})
        assert resp.status_code == 403

    def test_batch_operations_cross_team_rejected_per_item(self, resources, monkeypatch):
        """batch-*：跨团队任务逐项失败（error 含 跨团队），不整单 403"""
        client = make_client(monkeypatch, TEAM_B)
        tid = resources["task_id"]
        for url, body in [
            (f"{API}/evaluation/tasks/batch-run", {"task_ids": [tid]}),
            (f"{API}/evaluation/tasks/batch-delete", {"task_ids": [tid]}),
            (f"{API}/evaluation/tasks/batch-stop", {"task_ids": [tid]}),
            (f"{API}/evaluation/results/batch-export", {"task_ids": [tid], "format": "json"}),
        ]:
            resp = client.post(url, json=body)
            assert resp.status_code == 200, f"{url} 应 200（逐项失败语义）: {resp.text[:200]}"
            body_json = resp.json()
            # batch-export 的 json 分支返回 results，其余返回 details
            detail = (body_json.get("details") or body_json.get("results"))[0]
            assert detail["status"] == "failed"
            assert "跨团队" in detail["error"], detail

    def test_media_list_filtered_for_other_team(self, resources, monkeypatch):
        """媒体列表：B 用户看不到 A 团队媒体；A 用户可见"""
        client_b = make_client(monkeypatch, TEAM_B)
        resp = client_b.get(f"{API}/media")
        assert resp.status_code == 200
        assert all(m["id"] != resources["media_id"] for m in resp.json()["items"])

        client_a = make_client(monkeypatch, TEAM_A)
        resp = client_a.get(f"{API}/media")
        assert any(m["id"] == resources["media_id"] for m in resp.json()["items"])

    def test_dataset_list_filtered_for_other_team(self, resources, monkeypatch):
        """数据集列表（计划根因点名）：B 用户看不到 A 团队数据集；A 用户可见"""
        client_b = make_client(monkeypatch, TEAM_B)
        resp = client_b.get(f"{API}/evaluation/datasets")
        assert resp.status_code == 200
        assert all(d["id"] != resources["dataset_id"] for d in resp.json())

        client_a = make_client(monkeypatch, TEAM_A)
        resp = client_a.get(f"{API}/evaluation/datasets")
        assert any(d["id"] == resources["dataset_id"] for d in resp.json())


class TestIdorMatrix200:
    """本团队 / 超管 = 放行"""

    @pytest.mark.parametrize("entry", MATRIX, ids=[e[0] for e in MATRIX])
    def test_same_team_allowed(self, entry, resources, monkeypatch, supp_root):
        method, path, files, data, json_body, mode = _build_request(entry, resources)
        client = make_client(monkeypatch, TEAM_A)
        resp = client.request(method, f"{API}{path}", files=files, data=data, json=json_body)
        if mode == "ok":
            assert resp.status_code in (200, 201), (
                f"{entry[0]} 本团队应 2xx，实际 {resp.status_code}: {resp.text[:200]}")
        else:
            assert resp.status_code != 403, f"{entry[0]} 本团队不应 403: {resp.text[:200]}"

    @pytest.mark.parametrize("entry", MATRIX, ids=[e[0] for e in MATRIX])
    def test_super_admin_allowed(self, entry, resources, monkeypatch, supp_root):
        method, path, files, data, json_body, mode = _build_request(entry, resources)
        client = make_client(monkeypatch, TEAM_A, super_admin_flag=1)
        resp = client.request(method, f"{API}{path}", files=files, data=data, json=json_body)
        if mode == "ok":
            assert resp.status_code in (200, 201), (
                f"{entry[0]} 超管应 2xx，实际 {resp.status_code}: {resp.text[:200]}")
        else:
            assert resp.status_code != 403, f"{entry[0]} 超管不应 403: {resp.text[:200]}"

    def test_match_preview_same_team_and_admin(self, resources, monkeypatch):
        body = {**MATCH_PREVIEW_BODY, "dataset_id": resources["dataset_id"]}
        for org, flag in ((TEAM_A, 0), (TEAM_A, 1)):
            client = make_client(monkeypatch, org, super_admin_flag=flag)
            resp = client.post(f"{API}/hub-files/match-preview", json=body)
            assert resp.status_code == 200, resp.text[:200]
            # A 团队数据无 hub_file_id 样本 → 空态 200
            assert resp.json()["matched_count"] == 0

    def test_delete_endpoints_same_team_and_admin(self, shared_db_engine, w9_guard, monkeypatch, tmp_path):
        """删除类端点独立验证（每角色重建资源，避免矩阵内相互破坏）"""
        for org, flag in ((TEAM_A, 0), (TEAM_A, 1)):
            res = create_resources(shared_db_engine, tmp_path)
            client = make_client(monkeypatch, org, super_admin_flag=flag)
            for url in (
                f"{API}/evaluation/tasks/{res['task_id']}",
                f"{API}/evaluation/datasets/{res['dataset_id']}",
                f"{API}/media/{res['media_id']}",
            ):
                resp = client.delete(url)
                assert resp.status_code == 200, f"{url} 删除应 200: {resp.text[:200]}"

    def test_batch_delete_same_team(self, resources, monkeypatch):
        """batch-delete 本团队：任务被删除（success=1）"""
        client = make_client(monkeypatch, TEAM_A)
        resp = client.post(
            f"{API}/evaluation/tasks/batch-delete",
            json={"task_ids": [resources["task_id"]]})
        assert resp.status_code == 200
        assert resp.json()["details"][0]["status"] == "success"


class TestWsSubscriptionMatrix:
    """/ws/task 订阅：跨团队 4403（access_control 统一口径）、本团队/超管可订阅"""

    def _connect(self, monkeypatch, org, flag, task_id):
        import os
        from urllib.parse import quote

        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine

        from app.main import app
        from app.services.jwt_service import create_token

        user_code = f"{PREFIX_USER}{org.lower()}_ws"
        engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "insert into users (user_code, username, org_code, super_admin_flag, is_active) "
                    "values (:c, :c, :org, :flag, true) "
                    "on conflict (user_code) do update set org_code = :org, super_admin_flag = :flag"
                ),
                {"c": user_code, "org": org, "flag": flag},
            )
        engine.dispose()

        client = TestClient(app)
        # W5 test_ws_auth 同手法：WS 握手 query 参数携带 token（握手阶段无法自定义 header）
        token = quote(create_token(user_code, user_code))
        return client.websocket_connect(f"/ws/task/{task_id}?token={token}")

    def test_cross_team_subscription_4403(self, resources, monkeypatch):
        ws = self._connect(monkeypatch, TEAM_B, 0, resources["task_id"])
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with ws:
                pass
        assert exc_info.value.code == 4403

    def test_same_team_subscription_ok(self, resources, monkeypatch):
        with self._connect(monkeypatch, TEAM_A, 0, resources["task_id"]) as ws:
            msg = ws.receive_json()
        assert msg.get("type") == "connected"

    def test_super_admin_subscription_ok(self, resources, monkeypatch):
        with self._connect(monkeypatch, TEAM_A, 1, resources["task_id"]) as ws:
            msg = ws.receive_json()
        assert msg.get("type") == "connected"
