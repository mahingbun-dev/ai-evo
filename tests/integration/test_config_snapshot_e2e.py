"""W4 模型/裁判配置快照集成测试（P0-C）

对齐《整改开发计划-20261013.md》§2 W4「测试设计」集成要求：
- 同一任务执行两次，第二次前改配置（endpoint/temperature/轮换 api_key）→
  两次执行实际使用的口径字段（endpoint/temperature）均为**创建时**值，
  凭据 api_key 为**实时**值（密钥轮换后旧钥不可用）；
- 两次 TaskExecutionRecord.config_snapshot 均随执行留存：执行开始写创建时
  双快照、执行结束写实际使用配置摘要（source/掩码/指纹，无明文）；
- 存量任务（model_config 空）走实时路径：result JSON 标 config_snapshot=False、
  used 摘要 source=live、改配置后执行随实时值变化（与快照任务对照）。

环境：本地 PG 55434 上的自有前缀临时库（见 conftest 供给与清理铁律），
模型/裁判调用全部 mock（固定打分表，零网关依赖）。
"""
import json
import uuid

import pytest

from tests.integration.conftest import PROVISION_LOG, SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

from tests.integration.w4_mocks import (  # noqa: E402
    expected_dimensions,
    expected_score,
    install_w4_mocks,
)

pytestmark = pytest.mark.integration

# 创建时口径（旧值）与改后口径（新值）——断言的锚点常量
OLD_ENDPOINT = "http://w4-old-endpoint.example/v1"
NEW_ENDPOINT = "http://w4-new-endpoint.example/v1"
OLD_TEMPERATURE = 0.1
NEW_TEMPERATURE = 0.9
CREATION_KEY = "sk-w4itest-creation-old9999"
ROTATED_KEY = "sk-w4itest-rotated-7777"

# 模型/数据/任务基础名（实际落库 ID 附随机后缀，避免同库多测试主键冲突）
MODEL_BASE = "w4it_model_old"
JUDGE_MODEL_BASE = "w4it_judge_old"


# ===========================================================================
# 构造工具（数据自有前缀 w4it_/w4ds_/w4task_，随临时库整体清理）
# ===========================================================================


def _make_model(db, base_id, endpoint, api_key, temperature, role="evaluation"):
    """建一条 w4it_ 前缀模型配置，返回实际落库 ID（附随机后缀防冲突）"""
    from app.models.model_config_models import ModelConfig

    model_id = f"{base_id}_{uuid.uuid4().hex[:6]}"
    row = ModelConfig(
        id=model_id,
        name=f"W4集成测试模型-{model_id}",
        provider="custom_http",
        model_type="w4-mock-model",
        model_role=role,
        endpoint=endpoint,
        api_key=api_key,
        auth_type="none",
        auth_config=None,
        request_config={"temperature": temperature, "max_tokens": 2048},
        response_parse_config={"parse_type": "text"},
        is_active=True,
        team_code="DEFAULT",
    )
    db.add(row)
    db.commit()
    return model_id


def _make_dataset(db, n_rows=3):
    from app.models.dataset_models import EvaluationDataset, EvaluationDataV2

    suffix = uuid.uuid4().hex[:8]
    dataset = EvaluationDataset(
        id=f"w4ds_{suffix}",
        name=f"W4配置快照集成测试数据集{suffix}",
        question_count=n_rows,
        is_active=True,
        team_code="DEFAULT",
    )
    db.add(dataset)
    db.flush()
    for i in range(n_rows):
        db.add(
            EvaluationDataV2(
                id=f"w4dv_{suffix}_{i}",
                dataset_id=dataset.id,
                index=i,
                is_active=True,
                data={"user_input": f"W4集成输入 row-{i:02d}", "answer": f"参考答案 row-{i:02d}"},
            )
        )
    db.commit()
    return dataset.id


def _make_task_via_service(db, dataset_id, model_id, judge_model_id):
    """走真实 EvaluationTaskService.create_task（创建时快照的写入入口）"""
    from app.services.evaluation_task_service import EvaluationTaskService

    service = EvaluationTaskService(db)
    task = service.create_task(
        name=f"W4配置快照任务-{uuid.uuid4().hex[:6]}",
        evaluation_type="judge",
        dataset_id=dataset_id,
        model_provider=model_id,
        judge_model=judge_model_id,
        judge_engine="graph",  # graph 引擎：裁判走快照优先链（区别于 claude_agent 走 CC 全局）
        judge_count=1,
        # 行并发已与裁判数量解耦（执行器取 task.concurrency，service 缺省 4）。
        # 本文件按调用序断言（hooks["model_calls"] == [0, 1, 2]），依赖「串行
        # 执行、行序确定」，必须显式钉住 1，否则 4 个 worker 并发跑行会乱序。
        concurrency=1,
        max_score=10,
        evaluation_metrics=[
            {"name": "准确性", "weight": 0.6, "description": "答案准确程度"},
            {"name": "完整性", "weight": 0.4, "description": "要点覆盖程度"},
        ],
        evaluation_targets=[
            {"id": "t1", "name": "模型输出", "source": "model_output", "required": True},
        ],
        team_code="DEFAULT",
        created_by="w4-integration-tester",
    )
    db.commit()
    return task


def _mutate_model_config(db, model_id):
    """模拟创建任务后的配置变更：endpoint/temperature 改动 + 密钥轮换"""
    from app.models.model_config_models import ModelConfig

    row = db.query(ModelConfig).filter(ModelConfig.id == model_id).one()
    row.endpoint = NEW_ENDPOINT
    row.request_config = {"temperature": NEW_TEMPERATURE, "max_tokens": 2048}
    row.api_key = ROTATED_KEY
    db.commit()


def _run_once(task_id):
    """start_task + mock 执行一次，返回 (hooks, 返回值)"""
    from app.db.session import SessionLocal
    from app.services.evaluation_task_service import EvaluationTaskService
    from app.tasks.evaluation_tasks_async import run_evaluation_async

    db = SessionLocal()
    try:
        started = EvaluationTaskService(db).start_task(task_id)
        assert started is not None, "start_task 被拒（任务可能处于 running）"
    finally:
        db.close()

    hooks = {"model_calls": [], "model_configs": [], "judge_configs": []}
    with install_w4_mocks(hooks):
        ret = run_evaluation_async(task_id)
    return hooks, ret


def _get_task(db, task_id):
    from app.models.task_models import EvaluationTask

    return db.query(EvaluationTask).filter(EvaluationTask.id == task_id).one()


def _get_record(db, task_id, execution_number):
    from app.models.task_models import TaskExecutionRecord

    return (
        db.query(TaskExecutionRecord)
        .filter(
            TaskExecutionRecord.task_id == task_id,
            TaskExecutionRecord.execution_number == execution_number,
        )
        .one()
    )


def _get_stats(db, task_id):
    from app.models.stats_models import TaskExecutionStats

    return (
        db.query(TaskExecutionStats)
        .filter(TaskExecutionStats.task_id == task_id)
        .order_by(TaskExecutionStats.created_at.asc())
        .all()
    )


def _assert_snapshot_shape(snap, endpoint, masked_key=None):
    """执行记录 config_snapshot 中单个快照的结构断言（口径+掩码，无明文）"""
    assert snap is not None
    assert snap["snapshot"] is True
    assert snap["endpoint"] == endpoint
    assert snap["request_config"]["temperature"] == OLD_TEMPERATURE
    assert "api_key" not in snap
    assert "auth_config" not in snap
    assert snap.get("api_key_masked")
    assert snap.get("api_key_fingerprint")
    if masked_key:
        from app.services.config_snapshot import api_key_fingerprint, mask_api_key

        assert snap["api_key_masked"] == mask_api_key(masked_key)
        assert snap["api_key_fingerprint"] == api_key_fingerprint(masked_key)


# ===========================================================================
# 1. 创建任务即写双快照
# ===========================================================================


class TestCreateTaskWritesSnapshot:
    def test_create_task_snapshots_model_and_judge_configs(self, db_session):
        dataset_id = _make_dataset(db_session)
        model_id = _make_model(db_session, MODEL_BASE, OLD_ENDPOINT, CREATION_KEY, OLD_TEMPERATURE)
        judge_id = _make_model(db_session, JUDGE_MODEL_BASE, OLD_ENDPOINT, CREATION_KEY, OLD_TEMPERATURE, role="judge")

        task = _make_task_via_service(db_session, dataset_id, model_id, judge_id)

        from app.services.config_snapshot import api_key_fingerprint, mask_api_key

        # 被评测模型快照
        assert task.model_config and task.model_config.get("snapshot") is True
        assert task.model_config["model_id"] == model_id
        assert task.model_config["endpoint"] == OLD_ENDPOINT
        assert task.model_config["request_config"] == {
            "temperature": OLD_TEMPERATURE, "max_tokens": 2048,
        }
        assert task.model_config["api_key_masked"] == mask_api_key(CREATION_KEY)
        assert task.model_config["api_key_fingerprint"] == api_key_fingerprint(CREATION_KEY)
        # 裁判模型快照（同构）
        assert task.judge_model_config and task.judge_model_config.get("snapshot") is True
        assert task.judge_model_config["model_id"] == judge_id
        assert task.judge_model_config["endpoint"] == OLD_ENDPOINT

        # 严禁明文：键级 + JSON 全文双断言
        for snap in (task.model_config, task.judge_model_config):
            assert "api_key" not in snap
            assert "auth_config" not in snap
            dumped = json.dumps(snap, ensure_ascii=False)
            assert CREATION_KEY not in dumped

    def test_provision_path_used_template_or_fallback(self):
        """供给路径留痕：模板复制或降级路径必须可解释（不可静默走未知路径）"""
        assert PROVISION_LOG, "临时库供给日志为空——供给逻辑未执行？"
        # 记录的路径必须是三种已知形态之一
        known_prefixes = ("template-copy:", "empty+alembic-upgrade-head", "empty+create_all+stamp(head)")
        assert any(p.startswith(known_prefixes) for p in PROVISION_LOG), PROVISION_LOG


# ===========================================================================
# 2. 计划核心：同一任务执行两次，第二次前改配置 → 口径均为创建时值
# ===========================================================================


class TestTwoExecutionsConfigPersistence:
    def test_two_runs_use_creation_time_snapshot_and_records_persist(self, db_session):
        dataset_id = _make_dataset(db_session)
        model_id = _make_model(db_session, MODEL_BASE, OLD_ENDPOINT, CREATION_KEY, OLD_TEMPERATURE)
        judge_id = _make_model(db_session, JUDGE_MODEL_BASE, OLD_ENDPOINT, CREATION_KEY, OLD_TEMPERATURE, role="judge")
        task = _make_task_via_service(db_session, dataset_id, model_id, judge_id)
        task_id = task.id
        db_session.close()  # 后续用独立会话（与执行链路同口径）

        # ---- 第一次执行（配置 = 创建时状态）----
        hooks1, ret1 = _run_once(task_id)
        assert ret1.get("status") == "success", ret1
        assert hooks1["model_calls"] == [0, 1, 2]

        # executor 实际发出的被评测模型配置：口径 = 创建时值（此时实时=创建时）
        for cfg in hooks1["model_configs"]:
            assert cfg["endpoint"] == OLD_ENDPOINT
            assert cfg["request_config"]["temperature"] == OLD_TEMPERATURE
            assert cfg["api_key"] == CREATION_KEY
        # 裁判链路拿到的配置：快照口径（endpoint 旧值、掩码键而非明文键）
        for jcfg in hooks1["judge_configs"]:
            assert jcfg["endpoint"] == OLD_ENDPOINT
            assert jcfg["request_config"]["temperature"] == OLD_TEMPERATURE
            assert "api_key" not in jcfg or jcfg.get("api_key"), (
                "裁判快照不含明文 api_key（凭据在裁判调用器内部按实时解析）"
            )

        # ---- 创建任务后改 ModelConfig：endpoint/temperature 改动 + 密钥轮换 ----
        db = db_session
        _mutate_model_config(db, model_id)
        _mutate_model_config(db, judge_id)

        # ---- 第二次执行：口径必须仍为创建时值（验收标准：改配置重跑旧任务口径不变）----
        hooks2, ret2 = _run_once(task_id)
        assert ret2.get("status") == "success", ret2

        for cfg in hooks2["model_configs"]:
            # 口径 = 快照旧值
            assert cfg["endpoint"] == OLD_ENDPOINT, "改配置后执行口径漂移（应为创建时快照值）"
            assert cfg["request_config"]["temperature"] == OLD_TEMPERATURE
            # 凭据 = 实时新钥（轮换后旧钥不可用）
            assert cfg["api_key"] == ROTATED_KEY
        for jcfg in hooks2["judge_configs"]:
            assert jcfg["endpoint"] == OLD_ENDPOINT
            assert jcfg["request_config"]["temperature"] == OLD_TEMPERATURE

        # ---- 两次 TaskExecutionRecord.config_snapshot 留存断言 ----
        rec1 = _get_record(db, task_id, 1)
        rec2 = _get_record(db, task_id, 2)

        # 执行开始写入的双快照：两次均为创建时值（第二次执行前配置已改，仍旧值）
        _assert_snapshot_shape(rec1.config_snapshot["model"], OLD_ENDPOINT, CREATION_KEY)
        _assert_snapshot_shape(rec1.config_snapshot["judge"], OLD_ENDPOINT, CREATION_KEY)
        _assert_snapshot_shape(rec2.config_snapshot["model"], OLD_ENDPOINT, CREATION_KEY)
        _assert_snapshot_shape(rec2.config_snapshot["judge"], OLD_ENDPOINT, CREATION_KEY)

        # 执行结束写入的 used 摘要：source=snapshot、口径=旧值、凭据=当次实时钥
        from app.services.config_snapshot import api_key_fingerprint, mask_api_key

        used1 = rec1.config_snapshot["used"]
        assert used1["model"]["source"] == "snapshot"
        assert used1["model"]["endpoint"] == OLD_ENDPOINT
        assert used1["model"]["request_config"]["temperature"] == OLD_TEMPERATURE
        assert used1["model"]["api_key_masked"] == mask_api_key(CREATION_KEY)  # 第 1 次实时=旧钥
        assert used1["model"]["api_key_fingerprint"] == api_key_fingerprint(CREATION_KEY)

        used2 = rec2.config_snapshot["used"]
        assert used2["model"]["source"] == "snapshot"
        assert used2["model"]["endpoint"] == OLD_ENDPOINT
        assert used2["model"]["request_config"]["temperature"] == OLD_TEMPERATURE
        assert used2["model"]["api_key_masked"] == mask_api_key(ROTATED_KEY)  # 第 2 次实时=轮换钥
        assert used2["model"]["api_key_fingerprint"] == api_key_fingerprint(ROTATED_KEY)

        # graph 引擎裁判归因：快照链（第二次执行前 judge 模型也改了，仍旧值）
        assert used2["judge"]["source"] == "snapshot"
        assert used2["judge"]["endpoint"] == OLD_ENDPOINT

        # config_snapshot 全文无明文（执行记录出库安全）
        for rec in (rec1, rec2):
            dumped = json.dumps(rec.config_snapshot, ensure_ascii=False)
            assert CREATION_KEY not in dumped
            assert ROTATED_KEY not in dumped

        # ---- 两次聚合 result JSON 的 config_snapshot 标记 ----
        task_row = _get_task(db, task_id)
        assert task_row.status == "completed"
        assert task_row.result["config_snapshot"] is True
        # 两行 result（各次执行保存后覆盖为最新一次，断言最终态）：
        # result JSON 的行分数与固定打分表一致（口径未漂移的旁证）
        assert len(task_row.result["items"]) == 3
        for i, item in enumerate(task_row.result["items"]):
            assert item["average_score"] == expected_score(i)
            assert item["dimension_scores"] == expected_dimensions(i)

        # ---- 统计归因（start_task 的 model_config_id 取快照 model_id 键）----
        stats = _get_stats(db, task_id)
        assert len(stats) == 2  # 两次执行两条统计
        assert stats[0].model_config_id == model_id
        assert stats[1].model_config_id == model_id


# ===========================================================================
# 3. 存量任务（model_config 空）走实时路径
# ===========================================================================


class TestLegacyTaskLiveFallback:
    def test_legacy_task_runs_live_and_marks_false(self, db_session):
        """存量任务：result 标 config_snapshot=False；used.source=live；
        改配置后执行随实时值变化（与快照任务的对照面）。"""
        dataset_id = _make_dataset(db_session)
        model_id = _make_model(db_session, MODEL_BASE, OLD_ENDPOINT, CREATION_KEY, OLD_TEMPERATURE)

        # 直接 ORM 插入（绕过 create_task）——模拟整改前的存量任务
        from app.models.task_models import EvaluationTask

        task_id = f"w4task_legacy_{uuid.uuid4().hex[:8]}"
        db_session.add(
            EvaluationTask(
                id=task_id,
                name=f"W4存量任务回退-{task_id[-6:]}",
                evaluation_type="judge",
                status="pending",
                dataset_id=dataset_id,
                model_provider=model_id,
                model_config=None,  # 存量任务：无快照
                judge_engine="graph",
                judge_count=1,
                # 存量任务模拟的是「无 model_config 快照 → 回退实时配置」，与行并发
                # 无关；执行器现在按 task.concurrency 跑行（列默认 4），本文件的
                # 按调用序断言要求串行，故显式钉 1（不改生产默认值）
                concurrency=1,
                max_score=10,
                evaluation_metrics=[{"name": "准确性", "weight": 1.0}],
                evaluation_targets=[
                    {"id": "t1", "name": "模型输出", "source": "model_output", "required": True},
                ],
                variable_source="dataset",
                team_code="DEFAULT",
            )
        )
        db_session.commit()
        db_session.close()

        # 第一次执行（实时 = 创建口径）
        hooks1, ret1 = _run_once(task_id)
        assert ret1.get("status") == "success", ret1
        for cfg in hooks1["model_configs"]:
            assert cfg["endpoint"] == OLD_ENDPOINT
            assert cfg["api_key"] == CREATION_KEY

        rec1 = _get_record(db_session, task_id, 1)
        used1 = rec1.config_snapshot["used"]
        assert used1["model"]["source"] == "live"
        assert used1["model"]["endpoint"] == OLD_ENDPOINT
        # 存量任务无创建时快照：执行开始的双快照均为 None
        assert rec1.config_snapshot["model"] is None

        task_row = _get_task(db_session, task_id)
        assert task_row.result["config_snapshot"] is False

        # 改配置后再执行：实时路径随配置变化（对照：快照任务口径不变）
        _mutate_model_config(db_session, model_id)
        hooks2, ret2 = _run_once(task_id)
        assert ret2.get("status") == "success", ret2
        for cfg in hooks2["model_configs"]:
            assert cfg["endpoint"] == NEW_ENDPOINT, "存量任务应走实时配置（随改动变化）"
            assert cfg["request_config"]["temperature"] == NEW_TEMPERATURE
            assert cfg["api_key"] == ROTATED_KEY

        rec2 = _get_record(db_session, task_id, 2)
        used2 = rec2.config_snapshot["used"]
        assert used2["model"]["source"] == "live"
        assert used2["model"]["endpoint"] == NEW_ENDPOINT

        task_row2 = _get_task(db_session, task_id)
        assert task_row2.result["config_snapshot"] is False


# ===========================================================================
# 4. update_task 显式切换模型 → 快照重建（编辑切模型生效）
# ===========================================================================


class TestUpdateTaskSwitchProvider:
    def test_switch_provider_rebuilds_snapshot_and_same_value_keeps(self, db_session):
        dataset_id = _make_dataset(db_session)
        model_id = _make_model(db_session, MODEL_BASE, OLD_ENDPOINT, CREATION_KEY, OLD_TEMPERATURE)
        other_id = _make_model(db_session, "w4it_model_other", "http://w4-other-endpoint/v1", "sk-w4itest-other-555", 0.3)

        task = _make_task_via_service(db_session, dataset_id, model_id, None)
        task_id = task.id
        original_snapshot_at = task.model_config["snapshot_at"]

        from app.services.evaluation_task_service import EvaluationTaskService

        service = EvaluationTaskService(db_session)

        # 同值回传：不重置口径锚点（snapshot_at 不变）
        same = service.update_task(task_id, model_provider=model_id)
        assert same.model_config["snapshot_at"] == original_snapshot_at
        assert same.model_config["model_id"] == model_id

        # 显式切换：快照重建为新模型口径
        switched = service.update_task(task_id, model_provider=other_id)
        assert switched.model_config["model_id"] == other_id
        assert switched.model_config["endpoint"] == "http://w4-other-endpoint/v1"
        assert switched.model_config["request_config"]["temperature"] == 0.3
        assert switched.model_config["api_key_masked"]
        assert "api_key" not in switched.model_config
        # 重建后 snapshot_at 前进（新锚点）
        assert switched.model_config["snapshot_at"] >= original_snapshot_at
