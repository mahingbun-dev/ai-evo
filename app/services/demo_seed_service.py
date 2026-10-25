"""演示数据播种服务（P0-D，降低使用门槛：新用户 10-15 分钟看到第一个评测结果）

「一键体验：加载示例数据并跑一次演示评测」的后端事实源（首页按钮 →
POST /api/v1/demo/seed 与 POST /api/v1/demo/seed-and-run）。

设计要点：
- **幂等**：资源 ID 固定（demo-*-001），已存在即跳过，二次调用不重复插
  （验收：seed 幂等测试）；删除演示资源后可重新播种；
- **零凭据可跑通**：大模型接入遵循产品纪律——环境配置了 LITELLM_BASE_URL/
  LITELLM_API_KEY 时演示模型/裁判走 LiteLLM 网关（模型名 mimo-V2.6，
  OpenAI 兼容）；否则走平台内置的演示 mock 端点
  （POST /api/v1/demo/mock-llm/chat/completions，固定演示 token，见
  app/api/demo_api.py），保证无任何凭据也能看到第一份结果页；
- **任务选型**：AI 裁判员（judge）类型 + graph 裁判引擎——裁判链路在零凭据
  下经 task.judge_model_config 快照 + 演示裁判 ModelConfig（mock/LiteLLM
  端点）即可跑通（config_snapshot.resolve_judge_call_config：快照口径 +
  实时凭据），无需全局 Claude Code 配置；裁判图的分析节点吃自由文本、
  评分节点要求 {"score","reasoning"} JSON，mock 端点按 prompt 特征返回
  对应形态（见 demo_api._mock_reply）。若未来裁判链路无法零凭据 mock，
  退路是播种 latency/success_rate 任务（不依赖裁判），当前无此需要；
- **数据集**：V2 数据（evaluation_data_v2.data JSON：user_input/answer 字段）
  ——执行链路 _load_dataset 只加载 V2（scripts/insert_mock_data.py 的 V1
  表不会被任务执行读取，这是它只保留为开发联调脚本的原因）；
- 演示任务不占用用户既有数据；删除演示资源即完全清理
  （模型 2 + 数据集 1 + 任务 1，均 demo- 前缀固定 ID）。
"""
import logging
import os
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 固定资源 ID（幂等锚点；删除后可重播）
DEMO_EVAL_MODEL_ID = "demo-eval-model-001"
DEMO_JUDGE_MODEL_ID = "demo-judge-model-001"
DEMO_DATASET_ID = "demo-dataset-001"
DEMO_TASK_ID = "demo-task-judge-001"

# 内置演示 mock 端点的固定 token：非机密（代码内可见），作用是把该匿名端点
# （auth 中间件白名单放行）约束为「显式按演示约定调用」而非任意匿名可刷；
# 真正的安全边界在端点只返回预置文案、无任何状态副作用
DEMO_MOCK_TOKEN = "aievo-demo-mock"

_DEMO_ID_PREFIX = "demo-"

# 演示数据集（V2 行：data JSON 内 user_input/answer；与任务
# input_field=user_input / reference_field=answer 默认映射对齐）
DEMO_DATASET_ROWS = [
    {
        "user_input": "用两句话解释什么是机器学习。",
        "answer": "机器学习是让计算机从数据中自动学习规律的技术；训练好的模型可以对新样本做出预测或判断。",
    },
    {
        "user_input": "列出三个提高代码可读性的做法。",
        "answer": "使用清晰的命名；保持函数短小单一职责；添加必要的注释与文档。",
    },
    {
        "user_input": "把「今天天气很好」翻译成英文。",
        "answer": "The weather is nice today.",
    },
]

# 演示维度（与 mock 裁判评分 JSON 兼容：0-10 分量程）
DEMO_EVALUATION_METRICS = [
    {
        "name": "内容质量",
        "weight": "高",
        "max_score": 10,
        "description": "评估回答的准确性、完整性与针对性",
        "score_criteria": {
            "优秀": {"min": 9, "max": 10, "desc": "内容完整准确，切中问题"},
            "良好": {"min": 6, "max": 8, "desc": "内容基本完整，略有遗漏"},
            "较差": {"min": 0, "max": 5, "desc": "内容有明显错误或缺失"},
        },
    },
    {
        "name": "表达规范",
        "weight": "中",
        "max_score": 10,
        "description": "评估语言组织与格式规范性",
        "score_criteria": {
            "优秀": {"min": 9, "max": 10, "desc": "表述清晰，结构良好"},
            "良好": {"min": 6, "max": 8, "desc": "表述基本通顺"},
            "较差": {"min": 0, "max": 5, "desc": "表述混乱，难以理解"},
        },
    },
]

# 演示任务待评测数据（契约 v3 §五：judge 任务必须配置 evaluation_targets）。
# 旧版种子漏掉该行——任务能跑，但任何 PUT 局部更新（如配门禁）都会被
# 「必须配置待评测数据」校验拦成 400，E2E 验收发现并修复
DEMO_EVALUATION_TARGETS = [
    {
        "id": "demo-target-model-answer",
        "name": "model_answer",
        "display_name": "被测模型输出",
        "source": "model_output",
        "source_config": {},
        "required": True,
        "description": "被测模型的回答输出（演示任务）",
    },
]


def resolve_demo_llm_backend() -> Dict[str, Any]:
    """演示模型/裁判的 LLM 后端选择（读取时机：每次 seed 调用实时读 env）

    产品纪律（.env.example「大模型接入约定」）：
    - 配置了 LITELLM_BASE_URL + LITELLM_API_KEY → OpenAI 兼容网关 + 模型名
      mimo-V2.6（custom_http 全 URL + Bearer Key，与平台「模型配置」的
      OpenAI 兼容接入方式一致）；
    - 未配置 → 内置演示 mock 端点（零凭据验收线）。

    返回 {endpoint, model_type, api_key, mock}。
    """
    base = (os.environ.get("LITELLM_BASE_URL") or "").strip()
    key = (os.environ.get("LITELLM_API_KEY") or "").strip()
    if base and key:
        endpoint = base.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
        return {
            "endpoint": endpoint,
            "model_type": "mimo-V2.6",
            "api_key": key,
            "mock": False,
        }
    return {
        "endpoint": mock_llm_endpoint_url(),
        "model_type": "aievo-demo-mock",
        "api_key": DEMO_MOCK_TOKEN,
        "mock": True,
    }


def mock_llm_endpoint_url() -> str:
    """内置 mock 端点的完整 URL（worker 视角可达）

    base URL 解析优先级：
    1. AIEVO_DEMO_MOCK_BASE_URL 显式覆盖（含测试：起临时服务实例时指向它）；
    2. compose 容器内（SERVICE_TYPE=api/worker/migrate 标记）→ 服务名
       http://api:8100（worker 经 compose 内网访问 api）；
    3. 本地直跑（run_local_dev.py）→ 回环 127.0.0.1:8100。
    """
    override = (os.environ.get("AIEVO_DEMO_MOCK_BASE_URL") or "").strip()
    if override:
        return override.rstrip("/") + "/api/v1/demo/mock-llm/chat/completions"
    if os.environ.get("SERVICE_TYPE") in ("api", "worker", "migrate"):
        return "http://api:8100/api/v1/demo/mock-llm/chat/completions"
    return "http://127.0.0.1:8100/api/v1/demo/mock-llm/chat/completions"


def library_is_empty(db: Session) -> bool:
    """「空库」判定：无任何活跃模型配置 / 数据集 / 评测任务（忽略演示资源自身）

    空库时任意登录用户可播种（新部署首用户即超管，此口径主要兜住 SSO 开户
    等首用户非超管的边缘形态）；非空库仅超管可（P0-D 权限验收线）。
    """
    from app.models.dataset_models import EvaluationDataset
    from app.models.model_config_models import ModelConfig
    from app.models.task_models import EvaluationTask

    def _non_demo(model_cls, *extra_filters):
        query = db.query(model_cls.id).filter(~model_cls.id.startswith(_DEMO_ID_PREFIX))
        for f in extra_filters:
            query = query.filter(f)
        return query.first() is not None

    if _non_demo(ModelConfig, ModelConfig.is_active.is_(True)):
        return False
    if _non_demo(EvaluationDataset, EvaluationDataset.is_active.is_(True)):
        return False
    if _non_demo(EvaluationTask):
        return False
    return True


def seed_demo_data(db: Session, actor: str = "demo") -> Dict[str, Any]:
    """幂等播种演示资源，返回资源 ID 与 created 标记

    事务边界：由调用方（API handler）提交；本函数只 add/commit——为幂等简单
    起见每步独立 commit（已存在时跳过，不产生写）。
    """
    from app.models.dataset_models import EvaluationDataset, EvaluationDataV2
    from app.models.model_config_models import ModelConfig
    from app.models.task_models import EvaluationTask
    from app.services.config_snapshot import build_model_config_snapshot

    backend = resolve_demo_llm_backend()
    created: Dict[str, bool] = {}

    # ---- 1. 模型配置（待评测 + 裁判，同一后端）----
    def _model_config(mid: str, name: str, role: str, role_desc: str) -> ModelConfig:
        return ModelConfig(
            id=mid,
            name=name,
            provider="custom_http",
            model_type=backend["model_type"],
            model_role=role,
            endpoint=backend["endpoint"],
            api_key=backend["api_key"],
            # auth_type=none + api_key 非空 → ModelCaller 统一加
            # Authorization: Bearer <api_key>（OpenAI 兼容口径，mock 端点校验它）
            auth_type="none",
            auth_config={},
            request_config={"temperature": 0.2, "max_tokens": 1024, "timeout": 30},
            # 响应解析必须用平台正典结构（parse_type/parse_rule，与「模型配置」
            # 表单一致）——ResponseParser 不读 "path" 键，写错形状会把整个响应
            # dict 当 content 返回，裁判评分解析随之全挂（E2E 验收发现并修复）
            response_parse_config={
                "parse_type": "json_path",
                "parse_rule": {"content": "$.choices[0].message.content"},
            },
            supports_multimodal=False,
            description=(
                "演示模式内置模型（mock 端点，零凭据可跑通）；"
                "接入真实模型请在「模型配置」新建并替换任务绑定"
                if backend["mock"]
                else "演示模型（LiteLLM 网关 mimo-V2.6）"
            ),
            role_description=role_desc,
            is_active=True,
            is_preset=False,
            created_by=actor,
            team_code="DEFAULT",
        )

    if db.get(ModelConfig, DEMO_EVAL_MODEL_ID) is None:
        db.add(_model_config(DEMO_EVAL_MODEL_ID, "演示被测模型", "evaluation", "待评测模型"))
        db.commit()
        created["eval_model"] = True
    if db.get(ModelConfig, DEMO_JUDGE_MODEL_ID) is None:
        db.add(_model_config(DEMO_JUDGE_MODEL_ID, "演示裁判模型", "judge", "裁判模型"))
        db.commit()
        created["judge_model"] = True

    # ---- 2. 数据集（V2 行）----
    if db.get(EvaluationDataset, DEMO_DATASET_ID) is None:
        db.add(
            EvaluationDataset(
                id=DEMO_DATASET_ID,
                name="演示数据集（3 条问答）",
                description="一键体验自动创建的示例数据集：可直接在任务中引用，也可在数据集页查看/删除",
                question_count=len(DEMO_DATASET_ROWS),
                created_by=actor,
                is_active=True,
                team_code="DEFAULT",
            )
        )
        db.commit()
        created["dataset"] = True
    row_count = (
        db.query(EvaluationDataV2)
        .filter(EvaluationDataV2.dataset_id == DEMO_DATASET_ID)
        .count()
    )
    if row_count == 0:
        for idx, row in enumerate(DEMO_DATASET_ROWS, 1):
            db.add(
                EvaluationDataV2(
                    id=f"{DEMO_DATASET_ID}-row-{idx:03d}",
                    dataset_id=DEMO_DATASET_ID,
                    data=dict(row),
                    index=idx,
                    is_active=True,
                )
            )
        db.commit()
        created["dataset_rows"] = True

    # ---- 3. 演示评测任务（AI 裁判员，graph 引擎）----
    # 幂等语义按「存在且 active」判定：软删除（is_active=0）的演示任务会被
    # 重新激活并重建配置快照——否则一键体验在用户删除过演示任务后永远复用
    # 软删行（created 恒空、旧配置快照不更新），E2E 验收发现并修复
    _existing_task = db.get(EvaluationTask, DEMO_TASK_ID)
    if _existing_task is None:
        db.add(
            EvaluationTask(
                id=DEMO_TASK_ID,
                name="演示评测任务（AI 裁判员）",
                description="一键体验自动创建：演示被测模型 × 演示数据集，graph 裁判引擎打分；可克隆后改绑真实模型",
                evaluation_type="judge",
                status="pending",
                # W4 快照口径：与正常建任务同构（口径字段来自快照 + 执行时实时解析凭据）
                model_provider=DEMO_EVAL_MODEL_ID,
                model_config=build_model_config_snapshot(db, DEMO_EVAL_MODEL_ID),
                judge_model=DEMO_JUDGE_MODEL_ID,
                judge_model_config=build_model_config_snapshot(db, DEMO_JUDGE_MODEL_ID),
                # graph 引擎：HTTP 直调裁判模型（快照/实时 ModelConfig），
                # 零凭据下可指向内置 mock 端点；claude_agent 需要全局 CC 配置，
                # 不满足零凭据验收线
                judge_engine="graph",
                dataset_id=DEMO_DATASET_ID,
                input_field="user_input",
                reference_field="answer",
                evaluation_targets=DEMO_EVALUATION_TARGETS,
                judge_variable_config={
                    "user_input": {"source": "dataset_field", "source_config": {"field": "user_input"}},
                    "ref_answer": {"source": "dataset_field", "source_config": {"field": "answer"}},
                    "model_answer": {"source": "model_output"},
                },
                evaluation_metrics=DEMO_EVALUATION_METRICS,
                max_score=10,
                # 1 个裁判：演示以「最快看到结果页」为先，多裁判可在编辑任务时调
                judge_count=1,
                concurrency=4,
                evaluation_scene="AI裁判员评测",
                scene_desc="演示评测：评估模型对常见问答指令的回答质量（内容质量 + 表达规范）",
                validation_enabled=True,
                total_questions=len(DEMO_DATASET_ROWS),
                created_by=actor,
                is_active=True,
                version=0,
                team_code="DEFAULT",
            )
        )
        db.commit()
        created["task"] = True
    elif not _existing_task.is_active:
        # 软删行复活：重建配置快照，保证模型配置修复（如解析规则）能传播到演示任务
        _existing_task.is_active = True
        _existing_task.status = "pending"
        _existing_task.model_config = build_model_config_snapshot(db, DEMO_EVAL_MODEL_ID)
        _existing_task.judge_model_config = build_model_config_snapshot(db, DEMO_JUDGE_MODEL_ID)
        _existing_task.evaluation_targets = DEMO_EVALUATION_TARGETS
        db.commit()
        created["task_reactivated"] = True
    elif not _existing_task.evaluation_targets:
        # 存量 active 行缺 targets（旧版种子产物）：就地回填，保证任务可正常编辑
        _existing_task.evaluation_targets = DEMO_EVALUATION_TARGETS
        db.commit()
        created["task_targets_backfilled"] = True

    result = {
        "eval_model_id": DEMO_EVAL_MODEL_ID,
        "judge_model_id": DEMO_JUDGE_MODEL_ID,
        "dataset_id": DEMO_DATASET_ID,
        "task_id": DEMO_TASK_ID,
        "created": created,
        "mock_mode": backend["mock"],
        "message": (
            "演示数据已就绪（内置 mock 模型，零凭据）"
            if backend["mock"]
            else "演示数据已就绪（LiteLLM 网关 mimo-V2.6）"
        ),
    }
    logger.info("[demo] 演示数据播种完成: %s", result)
    return result


def trigger_demo_run(db: Session, user: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    """触发演示任务运行（走既有 run 入口语义：start_task + scheduler.submit_task）

    Returns:
        (task, error)：error 非 None 时 task 为 None（已在运行/提交失败等）。
    """
    from app.services.evaluation_task_service import EvaluationTaskService
    from app.services.task_scheduler import get_task_scheduler

    service = EvaluationTaskService(db)
    task = service.get_task(DEMO_TASK_ID)
    if task is None:
        return None, "演示任务不存在（seed 失败？）"
    if task.status == "running":
        return task, None

    started = service.start_task(DEMO_TASK_ID)
    if started is None:
        return service.get_task(DEMO_TASK_ID), "演示任务正在执行中"

    scheduler = get_task_scheduler()
    if not scheduler.submit_task(DEMO_TASK_ID):
        service.fail_task(DEMO_TASK_ID, "演示任务提交到 Celery Worker 失败")
        return None, "任务提交失败（worker 未就绪？），请稍后在任务列表重试"
    return service.get_task(DEMO_TASK_ID), None
