"""工具清单：18 个 MCP 工具的单一事实源。

每个工具的 route 声明 HTTP 方法、URL 模板、参数去向与 input schema，
manifest 的 ToolDecl 与 handle 的分发共用这一张表，杜绝声明与实现漂移。

参数去向三类：
- ``path_params``  替换 URL 模板里的 ``{占位符}``，如 ``/evaluation/tasks/{task_id}``；
- ``query_params`` 仅当调用方给了才拼进 query string（列表分页/过滤）；
- 其余参数（去掉 ``op`` 与 path_params 后）作为 JSON body 发送（POST/PUT）。

``requires_approval`` 按「删除+修改需人工审批」的策略标记：agent 只能提交
草稿，由人在中台侧确认后才执行。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict

import hubkit
from hubkit.proto.hubv1 import plugin_pb2

from .result_summary import (
    DEFAULT_MAX_FAILED_ITEMS as MAX_FAILED_ITEMS,
    summarize_result,
)

PLUGIN_NAME = "ai-evo"
PLUGIN_VERSION = "1.1.0"
PLUGIN_DESCRIPTION = (
    "AI 评测平台（ai-evo）代理：数据集管理与导入（经 anc-hub file-store 的 file_id）、"
    "评测任务的创建/修改/删除与执行、任务状态与评测结果查询。"
    "调用方需有平台登录态（经 anc-hub 登录后透传），所有操作以真实用户身份执行并落审计。"
)

_ROUTE_TYPE = Dict[str, Any]


def _truncate_result(data: Any, ctx: Dict[str, Any]) -> Any:
    """task_get 的后处理：把任务里的 result 明细裁成摘要，避免大载荷撑爆回包。"""
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data["result"] = summarize_result(data["result"])
    return data


def _summarize_result(data: Any, ctx: Dict[str, Any]) -> Any:
    """task_results 的后处理：把全量明细裁成「摘要 + top 失败样例 + 结果页链接」。"""
    if isinstance(data, dict):
        data["result"] = summarize_result(data.get("result"))
        if isinstance(data.get("result"), dict):
            data["result"]["note"] = (
                f"已省略全部成功项明细与第 {MAX_FAILED_ITEMS} 条之后的失败样例；"
                "需要全量明细请在前端结果页查看"
            )
        frontend_base = ctx.get("frontend_base") or ""
        task_id = ctx.get("payload", {}).get("task_id") or data.get("task_id") or ""
        if frontend_base and task_id:
            # 拼成可直接点击的深链；前端当前忽略 query 参数（落地结果展示页，
            # 在下拉中选任务），保留该参数以便前端未来支持自动选中。
            data["result_url"] = f"{frontend_base.rstrip('/')}/results?task_id={task_id}"
            data["result_url_task_id"] = str(task_id)
    return data


TOOL_ROUTES: Dict[str, _ROUTE_TYPE] = {
    # ── 数据集管理与浏览 ──────────────────────────────────────────────
    "dataset_list": {
        "method": "GET",
        "path": "/api/v1/evaluation/datasets",
        "query_params": ["skip", "limit"],
        "body": False,
        "desc": "列出评测数据集（含题目数、是否含媒体），支持分页",
        "schema": {
            "type": "object",
            "properties": {
                "skip": {"type": "integer", "description": "跳过条数（默认 0）"},
                "limit": {"type": "integer", "description": "返回条数上限（默认 100，最大 1000）"},
            },
        },
    },
    "dataset_get": {
        "method": "GET",
        "path": "/api/v1/evaluation/datasets/{dataset_id}",
        "path_params": ["dataset_id"],
        "body": False,
        "desc": "获取数据集详情（名称/描述/题目数/是否含媒体）",
        "schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "数据集 ID"},
            },
            "required": ["dataset_id"],
        },
    },
    "dataset_data_list": {
        "method": "GET",
        "path": "/api/v1/evaluation/datasets/{dataset_id}/data/v2",
        "path_params": ["dataset_id"],
        "query_params": ["skip", "limit"],
        "body": False,
        "desc": "分页列出数据集的评测数据行（自由格式，用于建任务前确认数据内容）",
        "schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "数据集 ID"},
                "skip": {"type": "integer", "description": "跳过条数（默认 0）"},
                "limit": {"type": "integer", "description": "返回条数上限（默认 100，最大 500）"},
            },
            "required": ["dataset_id"],
        },
    },
    "dataset_create": {
        "method": "POST",
        "path": "/api/v1/evaluation/datasets",
        "body": True,
        "desc": "创建空数据集（字段结构由导入流程回填；带文件的数据集请用 dataset_import_hub_files）",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "数据集名称"},
                "description": {"type": "string", "description": "数据集描述（可选）"},
            },
            "required": ["name"],
        },
    },
    "dataset_update": {
        "method": "PUT",
        "path": "/api/v1/evaluation/datasets/{dataset_id}",
        "path_params": ["dataset_id"],
        "body": True,
        "requires_approval": True,
        "desc": "更新数据集名称/描述（需人工审批）",
        "schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "数据集 ID"},
                "name": {"type": "string", "description": "新名称（可选）"},
                "description": {"type": "string", "description": "新描述（可选）"},
            },
            "required": ["dataset_id"],
        },
    },
    "dataset_delete": {
        "method": "DELETE",
        "path": "/api/v1/evaluation/datasets/{dataset_id}",
        "path_params": ["dataset_id"],
        "body": False,
        "requires_approval": True,
        "desc": "删除数据集（软删，需人工审批；评测数据集可能几小时才建好，误删不可恢复）",
        "schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "数据集 ID"},
            },
            "required": ["dataset_id"],
        },
    },

    # ── 经 anc-hub file-store 的数据集导入 ──────────────────────────
    "hub_files_query": {
        "method": "POST",
        "path": "/api/v1/hub-files/query",
        "body": True,
        "desc": (
            "查询 anc-hub file-store 里已上传的文件（找 file_id 用）。"
            "agent 应先用 file-store 的 file_upload 分块上传把数据集文件传上去，"
            "再把得到的 file_id 用于 dataset_import_hub_files"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name_like": {"type": "string", "description": "文件名子串过滤（不区分大小写，可选）"},
                "uploaded_after": {"type": "string", "description": "RFC3339 时间，含边界（可选）"},
                "uploaded_before": {"type": "string", "description": "RFC3339 时间（可选）"},
                "uploader": {"type": "string", "description": "按上传者过滤（可选）"},
                "page": {"type": "integer", "description": "页号，从 1 起（可选）"},
                "page_size": {"type": "integer", "description": "页大小，1-200（可选）"},
            },
        },
    },
    "dataset_import_hub_files": {
        "method": "POST",
        "path": "/api/v1/evaluation/datasets/import/hub-files",
        "body": True,
        "desc": (
            "把 anc-hub file-store 的执行包文件导入为评测数据集（异步任务，返回 job_id，"
            "用 hub_import_job_status 查进度）。新建数据集给 name；追加到已有数据集给 real_dataset_id；"
            "挂清洗脚本给 script_id（脚本库 id 或名称）——非空时逐包执行清洗、脚本产出的行内容"
            "作为数据集行，不传则只登记执行包引用（数据行停在 hub_file_id/hub_summary、内容不展开）"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "file-store 的 file_id 列表（file_upload 成功后返回的 id）",
                },
                "name": {"type": "string", "description": "新建数据集的名称（新建模式必填）"},
                "description": {"type": "string", "description": "数据集描述（可选）"},
                "real_dataset_id": {"type": "string", "description": "追加模式下目标数据集 ID"},
                "script_id": {
                    "type": "string",
                    "description": (
                        "可选：脚本库清洗脚本 ID 或名称——非空时逐包执行清洗，脚本产出的行内容"
                        "作为数据集行；不传则只登记执行包引用（数据行只有 hub_file_id/hub_summary，内容不展开）"
                    ),
                },
            },
            "required": ["file_ids"],
        },
    },
    "hub_import_job_status": {
        "method": "GET",
        "path": "/api/v1/hub-import-jobs/{job_id}",
        "path_params": ["job_id"],
        "body": False,
        "desc": "查询数据集导入任务进度（status/总数/成功数/失败明细）",
        "schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "dataset_import_hub_files 返回的 job id"},
            },
            "required": ["job_id"],
        },
    },

    # ── 评测任务 ─────────────────────────────────────────────────────
    "model_list": {
        "method": "GET",
        "path": "/api/v1/models",
        "body": False,
        "desc": "列出可选的被评测模型配置（建评测任务时把模型 id 填进 model_provider）",
        "schema": {"type": "object", "properties": {}},
    },
    "task_list": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks",
        "query_params": ["status", "evaluation_type", "skip", "limit"],
        "body": False,
        "desc": "列出评测任务（可按状态/评测类型过滤）",
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "状态过滤：pending/running/completed/failed/stopped（可选）"},
                "evaluation_type": {"type": "string", "description": "评测类型过滤：judge/consistency/success_rate/rag/binary_classification（可选）"},
                "skip": {"type": "integer", "description": "跳过条数（默认 0）"},
                "limit": {"type": "integer", "description": "返回条数上限（默认 100，最大 1000）"},
            },
        },
    },
    "task_get": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks/{task_id}",
        "path_params": ["task_id"],
        "body": False,
        "postprocess": _truncate_result,
        "desc": (
            "获取评测任务详情：状态（pending/running/completed/failed/stopped）、进度、"
            "已完成题数与任务配置。这也是评测执行中的显式状态查询工具——agent 需要轮询时自行"
            "按自己的节奏重复调用本工具，插件不做自动轮询"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    "task_create": {
        "method": "POST",
        "path": "/api/v1/evaluation/tasks",
        "body": True,
        "desc": (
            "创建评测任务。必填 name 与 evaluation_type（judge/consistency/success_rate/"
            "rag/binary_classification）；judge 类型还需数据集（dataset_id）、待评测数据行"
            "（evaluation_targets，见该字段说明）、被评测模型（model_provider，见 model_list）"
            "与评测维度（evaluation_metrics，含 name/weight/description 的维度列表），"
            "裁判可配 judge_model/judge_count/judge_prompt 等"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "任务名称"},
                "description": {"type": "string", "description": "任务描述（可选）"},
                "evaluation_type": {
                    "type": "string",
                    "description": "评测类型：judge（裁判打分）/consistency/success_rate/rag/binary_classification",
                },
                "dataset_id": {"type": "string", "description": "评测数据集 ID（见 dataset_list）"},
                "model_provider": {"type": "string", "description": "被评测模型配置 ID（见 model_list）"},
                "judge_model": {"type": "string", "description": "裁判模型配置 ID（judge 类型，可选）"},
                "judge_engine": {"type": "string", "description": "裁判引擎：claude_agent（默认）/graph"},
                "judge_count": {"type": "integer", "description": "裁判数量（默认 6）"},
                "judge_prompt": {"type": "string", "description": "自定义评测 prompt（可选）"},
                "evaluation_metrics": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "评测维度列表，如 [{\"name\": \"准确性\", \"weight\": 0.6, \"description\": \"...\"}]",
                },
                "evaluation_targets": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "待评测数据行（judge/agent 类型必填，否则 400）。每行如 "
                        "{\"id\": \"target_xxx\", \"name\": \"模型输出\", \"display_name\": \"模型输出\", "
                        "\"source\": \"model_output\", \"source_config\": {}, \"required\": true, \"description\": \"\"}；"
                        "source 可选 model_output/chain_result/dataset_field/script_output/manual_input"
                    ),
                },
                "max_score": {"type": "integer", "description": "最大分值（默认 10）"},
                "score_details": {"type": "string", "description": "分值标准说明（可选）"},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "评分步骤（可选）"},
                "runtime_variables": {"type": "object", "description": "运行时变量（可选）"},
                "variable_source": {"type": "string", "description": "变量来源：manual（默认）/dataset"},
            },
            "required": ["name", "evaluation_type"],
        },
    },
    "task_update": {
        "method": "PUT",
        "path": "/api/v1/evaluation/tasks/{task_id}",
        "path_params": ["task_id"],
        "body": True,
        "requires_approval": True,
        "desc": "修改评测任务配置（需人工审批；仅传要改的字段）",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "name": {"type": "string", "description": "新名称（可选）"},
                "description": {"type": "string", "description": "新描述（可选）"},
                "dataset_id": {"type": "string", "description": "换绑数据集（可选）"},
                "model_provider": {"type": "string", "description": "换被评测模型（可选）"},
                "judge_model": {"type": "string", "description": "换裁判模型（可选）"},
                "judge_count": {"type": "integer", "description": "裁判数量（可选）"},
                "judge_prompt": {"type": "string", "description": "评测 prompt（可选）"},
                "evaluation_metrics": {"type": "array", "items": {"type": "object"}, "description": "评测维度（可选）"},
                "max_score": {"type": "integer", "description": "最大分值（可选）"},
            },
            "required": ["task_id"],
        },
    },
    "task_delete": {
        "method": "DELETE",
        "path": "/api/v1/evaluation/tasks/{task_id}",
        "path_params": ["task_id"],
        "body": False,
        "requires_approval": True,
        "desc": "删除评测任务（需人工审批）",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    "task_run": {
        "method": "POST",
        "path": "/api/v1/evaluation/tasks/{task_id}/run",
        "path_params": ["task_id"],
        "body": False,
        "desc": (
            "触发评测执行（异步，立即返回 running 状态）。评测耗时分钟到小时级，"
            "用 task_get 显式查询进度/状态，用 task_results 拿结果；需要监控时由调用方自行轮询"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    "task_stop": {
        "method": "POST",
        "path": "/api/v1/evaluation/tasks/{task_id}/stop",
        "path_params": ["task_id"],
        "body": False,
        "desc": "停止正在执行的评测任务",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    "task_results": {
        "method": "GET",
        "path": "/api/v1/evaluation/results/{task_id}",
        "path_params": ["task_id"],
        "body": False,
        "postprocess": _summarize_result,
        "desc": (
            "获取评测结果摘要：通过率/平均分/各维度得分/top 失败样例（含裁判评语，已截断）"
            "与前端结果页链接。全量成功项明细已省略（载荷过大），人类查看完整报告请访问返回的 result_url"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
}

_OPS = frozenset(TOOL_ROUTES)


def build_tool_decls() -> list[plugin_pb2.ToolDecl]:
    """把 TOOL_ROUTES 转成 manifest 的工具声明列表。"""
    tools: list[plugin_pb2.ToolDecl] = []
    for name, route in TOOL_ROUTES.items():
        tools.append(
            plugin_pb2.ToolDecl(
                name=name,
                description=route["desc"],
                input_schema_json=json.dumps(route["schema"], ensure_ascii=False),
                requires_approval=bool(route.get("requires_approval", False)),
            )
        )
    return tools


def build_manifest() -> plugin_pb2.PluginManifest:
    return plugin_pb2.PluginManifest(
        name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        description=PLUGIN_DESCRIPTION,
        owner="qa",
        consumes=[plugin_pb2.MessageContract(fq_name=hubkit.STRUCT_FQ_NAME)],
        produces=[plugin_pb2.MessageContract(fq_name=hubkit.STRUCT_FQ_NAME)],
        tools=build_tool_decls(),
    )


def resolve_op(env_meta: dict, payload: dict) -> str | None:
    """确定本次调用要执行的工具：meta['hub.tool'] 优先，payload['op'] 兜底。"""
    tool = env_meta.get("hub.tool")
    if tool:
        return tool
    op = payload.get("op")
    return op if isinstance(op, str) else None


def postprocess_of(op: str) -> Callable[[Any, Dict[str, Any]], Any] | None:
    """取该工具的响应后处理函数，签名 ``fn(data, ctx) -> data``。"""
    return TOOL_ROUTES[op].get("postprocess")
