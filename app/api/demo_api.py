"""演示面 API（P0-D，降低使用门槛）

- POST /api/v1/demo/seed —— 幂等播种演示资源（模型/数据集/可运行任务）；
  权限：超管或空库（非空库非超管 403）；
- POST /api/v1/demo/seed-and-run —— seed 幂等 + 走既有 run 入口触发演示任务；
- POST /api/v1/demo/mock-llm/chat/completions —— 内置演示 mock LLM 端点
  （OpenAI 兼容响应形态；auth 中间件白名单放行 + 固定演示 token 校验，
  供零凭据部署跑通演示评测；无状态、只返回预置文案）。

写端点全部接 audited 审计依赖（mock 端点为无状态探针，登记在
tests/unit/test_audit_coverage.py 的白名单）。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import demo_seed_service
from app.services.access_control import ensure_permission, is_super_admin
from app.services.audit_service import audited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["演示"])


def _ensure_seed_permission(request: Request, db: Session) -> None:
    """播种权限闸：超管或空库（P0-D 验收线：非空库非超管 403）"""
    user = getattr(request.state, "user", None) or {}
    if is_super_admin(user):
        return
    if demo_seed_service.library_is_empty(db):
        return
    raise HTTPException(
        status_code=403,
        detail="演示数据播种仅限超级管理员（或尚未录入任何数据的新部署）",
    )


@router.post(
    "/seed",
    summary="播种演示数据（幂等）",
    description=(
        "创建演示模型配置（内置 mock 端点或 LiteLLM 网关）、示例数据集"
        "（3 条问答，V2 行）与一个可直接运行的 AI 裁判员演示任务。"
        "幂等：二次调用不重复插入。权限：超管或空库。"
    ),
    dependencies=[Depends(audited("CREATE", "demo_seed"))],
)
def seed_demo(request: Request, db: Session = Depends(get_db)):
    _ensure_seed_permission(request, db)
    user = getattr(request.state, "user", None) or {}
    result = demo_seed_service.seed_demo_data(db, actor=user.get("user_code") or "demo")
    # QE4-4：创建类端点无路径 ID，显式暂存新资源标识供审计依赖落库
    request.state.audit_resource = {
        "id": result["task_id"],
        "name": "演示数据",
        "detail": {"created": result["created"], "mock_mode": result["mock_mode"]},
    }
    return result


@router.post(
    "/seed-and-run",
    summary="播种演示数据并触发演示评测",
    description=(
        "seed 幂等播种 + 走既有 run 入口（start_task + scheduler.submit_task）"
        "触发演示任务运行。返回任务 ID 与状态，前端跳转任务进度/结果页轮询。"
        "权限：同 /demo/seed（超管或空库），运行阶段叠加 tasks 资源 RBAC。"
    ),
    dependencies=[Depends(audited("RUN", "demo_task"))],
)
def seed_and_run_demo(request: Request, db: Session = Depends(get_db)):
    _ensure_seed_permission(request, db)
    user = getattr(request.state, "user", None) or {}
    seed = demo_seed_service.seed_demo_data(db, actor=user.get("user_code") or "demo")

    # 运行走既有 RBAC（viewer 不可触发运行，与任务页「执行」按钮同口径）
    ensure_permission(user, "tasks")
    task, error = demo_seed_service.trigger_demo_run(db, user)
    if error and task is None:
        raise HTTPException(status_code=400, detail=error)

    request.state.audit_resource = {
        "id": demo_seed_service.DEMO_TASK_ID,
        "name": "演示评测任务",
        "detail": {
            "created": seed["created"],
            "mock_mode": seed["mock_mode"],
            "task_status": getattr(task, "status", None),
        },
    }
    return {
        **seed,
        "task_status": getattr(task, "status", None),
        "task_error": error,
    }


# ======================================================================
# 内置演示 mock LLM 端点（零凭据验收线的支撑面）
# ======================================================================
#
# 暴露策略：路径前缀 /api/v1/demo/mock-llm 在 AuthMiddleware 白名单内
# （worker 调它不带 JWT；seed 出的模型配置带固定演示 token）。固定 token
# 非机密，作用是约束「按演示约定显式调用」；端点无状态、无 DB、无日志
# 采集面外泄露，只按请求特征返回两类预置文案：
#   - 裁判评分请求（prompt 要求只输出 {"score","reasoning"} JSON）→ 固定
#     演示评分 JSON（judge_graph_nodes 解析该形态）；
#   - 其余（被测模型问答 / 裁判分析节点）→ 预置演示回复文本。


def _mock_reply(messages: List[Dict[str, Any]]) -> str:
    last_user = ""
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = str(msg.get("content") or "")
            break
    # 裁判图评分节点的 prompt 严格要求 JSON（judge_graph_nodes.create_score_
    # dimension_node：「只输出 JSON」+ {"score","reasoning"}）；分析节点与被测
    # 模型问答吃自由文本。按特征分发，保证 judge 链路零凭据可解析出分
    if ("只输出 JSON" in last_user) or ('"score"' in last_user and '"reasoning"' in last_user):
        return '{"score": 8, "reasoning": "演示裁判评分：回答覆盖问题要点、表述清晰（内置 mock 端点固定分，接入真实模型后由真实裁判评分）。"}'
    return (
        "演示回复（内置 mock 端点）：这个问题可以从两个层面来看——"
        "先给出直接结论，再补充关键理由与示例。"
        "当前部署未配置 LITELLM_BASE_URL/LITELLM_API_KEY，演示评测走零凭据 mock；"
        "在「模型配置」接入真实模型后即可获得真实回答。"
    )


@router.post(
    "/mock-llm/chat/completions",
    summary="演示 mock LLM（OpenAI 兼容，固定演示 token）",
    description=(
        "内置演示端点：返回预置文案（评分请求返回演示评分 JSON，其余返回"
        "演示回复），供零凭据部署跑通演示评测。无状态、不落库。"
    ),
)
async def mock_llm_chat_completions(request: Request):
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {demo_seed_service.DEMO_MOCK_TOKEN}":
        raise HTTPException(status_code=401, detail="演示 mock 端点要求固定演示 token（由演示数据播种自动配置）")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    messages = body.get("messages") if isinstance(body, dict) else None
    content = _mock_reply(messages if isinstance(messages, list) else [])
    return {
        "id": "chatcmpl-aievo-demo",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
