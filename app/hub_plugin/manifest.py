"""工具清单：20 个 MCP 工具（28 条路由）的单一事实源。

两层结构：

- ``TOOL_ROUTES`` 是**路由表**，一条 route 对应 ai-evo 的一个 HTTP 端点，
  声明方法、URL 模板、参数去向与 schema；
- ``TOOL_GROUPS`` 是**工具面**，一个工具通过选择器参数（``action``/``view``/
  ``format``）在若干 route 之间分发。工具名与路由名不必相同：合并后的工具用
  新名，被下游脚本显式点名的名字保持原名。

未进 ``TOOL_GROUPS`` 的 route 是**隐藏别名**——不出现在 tools/list，但仍可经
``invoke_plugin`` 的 ``payload.op`` 调用，保证旧调用方一个版本周期内不炸。

参数去向三类：
- ``path_params``  替换 URL 模板里的 ``{占位符}``，如 ``/evaluation/tasks/{task_id}``；
- ``query_params`` 仅当调用方给了才拼进 query string（列表分页/过滤）；
- 其余参数（去掉选择器、``op`` 与 path_params 后）作为 JSON body 发送（POST/PUT）。

``requires_approval`` 是**工具级**布尔（中台无按调用审批），因此只能把审批
策略相同的 route 合进同一个工具；「删除+修改」需人工审批，其余全自动。

route 可选 ``timeout_seconds``：该 route 对 ai-evo 的 HTTP 请求超时覆盖值
（缺省用插件级 AIEVO_TIMEOUT），慢操作（如文档导出的生成+上传）单独放宽。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict

import hubkit
from hubkit.proto.hubv1 import plugin_pb2

from .result_pagination import (
    DEFAULT_PAGE_SIZE as DETAIL_PAGE_SIZE,
    MAX_PAGE_SIZE as DETAIL_MAX_PAGE_SIZE,
    paginate_detail,
)
from .result_summary import (
    DEFAULT_MAX_FAILED_ITEMS as MAX_FAILED_ITEMS,
    summarize_result,
)

PLUGIN_NAME = "ai-evo"
# 中台规则：同版本号契约不可变（VERSION_CONFLICT 拒绝注册）——凡动工具/描述/schema/消息契约必须升此号
# 1.4.0（M1 QE1-2）：新增 run_task_with_gate / get_gate_status 两工具（评测即门禁）
# 1.5.0（M8 QE8-5）：新增 dataset_versions / simulation_run / simulation_run_status
#   三工具（M5 版本查询与 M7 仿真触发）；task_create/task_list 的引擎与类型描述
#   同步为平台白名单实际状态（3 裁判引擎 / 9 种评测类型）
# 1.6.0（合并企业线工具面重构）：28 路由收拢为 20 工具（15 聚合组 + 5 单动作工具），
#   task_execution_export/task_logs 降为隐藏别名，导出格式新增 xlsx（safety 合规报告）
PLUGIN_VERSION = "1.6.0"
PLUGIN_DESCRIPTION = (
    "AI 评测平台（ai-evo）代理：数据集管理与导入（经 anc-hub file-store 的 file_id）"
    "与版本查询、评测任务的创建/修改/删除与执行、任务轻量状态轮询与执行历史、"
    "单次执行明细分页查询与结果文档导出（markdown/json/xlsx 上传 file-store 返回 file_id）、"
    "评测结果摘要查询、任务日志与评测门禁（评测即门禁：触发执行后按门禁 verdict 轮询，"
    "agent 可作为门禁调用方）、仿真评测触发与运行查询（M7）。"
    "调用方需有平台登录态（经 anc-hub 登录后透传），所有操作以真实用户身份执行并落审计。"
)

_ROUTE_TYPE = Dict[str, Any]


def _truncate_result(data: Any, ctx: Dict[str, Any]) -> Any:
    """task_get 的后处理：把任务里的 result 明细裁成摘要，避免大载荷撑爆回包。"""
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data["result"] = summarize_result(data["result"])
    return data


def _strip_execution_records(data: Any, ctx: Dict[str, Any]) -> Any:
    """task_executions 的后处理：执行历史列表剥掉 result/config_snapshot 大列。

    该端点按 ORM 全字段序列化（含每版完整 result），列表场景只留标量元信息
    （序号/状态/成本/错误/起止时间），全量明细走 task_execution_detail。
    """
    if isinstance(data, list):
        for record in data:
            if isinstance(record, dict):
                record.pop("result", None)
                record.pop("config_snapshot", None)
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


def _summarize_simulation_run(data: Any, ctx: Dict[str, Any]) -> Any:
    """simulation_run_status 的后处理：剥掉逐 trial 会话回放，只留指标与 trial 概要。

    运行详情的 result.trials 含完整对话/工具轨迹（trial 回放是前端专属能力，
    agent 轮询只需要终态与指标）；每个 trial 保留 序号/是否通过/终止原因/
    回合数/工具浪费率/故障命中/耗时，对话 turns 整块替换为 turns_omitted 计数。
    """
    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        return data
    result = data["result"]
    trials = result.get("trials")
    if isinstance(trials, list):
        slim_trials = []
        for trial in trials:
            if not isinstance(trial, dict):
                continue
            turns = trial.get("turns") or {}
            turn_list = turns.get("turns") if isinstance(turns, dict) else None
            slim_trials.append({
                "trial": trial.get("trial"),
                "passed": trial.get("passed"),
                "termination": trial.get("termination"),
                "turn_count": len(turn_list) if isinstance(turn_list, list) else 0,
                "tool_count": (trial.get("agent") or {}).get("tool_count"),
                "tool_waste_rate": trial.get("tool_waste_rate"),
                "faults_fired": trial.get("faults_fired"),
                "duration_ms": trial.get("duration_ms"),
            })
        result["trials"] = slim_trials
        result["note"] = "已省略逐 trial 对话回放与终态校验明细；回放/证据请在前端仿真页查看"
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
        "desc": "创建空数据集（带文件的数据集用 dataset_import_hub_files）",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "数据集名称"},
                "description": {"type": "string", "description": "数据集描述"},
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
            "把 file-store 的执行包导入为数据集（异步，返回 job_id，用 hub_import_query 查进度）。"
            "新建给 name，追加给 real_dataset_id；不传 script_id 只登记执行包引用，"
            "传了则逐包执行清洗、脚本产出行作为数据行"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "file_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "file-store 的 file_id 列表（先经 file_upload 分块上传）",
                },
                "name": {"type": "string", "description": "新建数据集名称"},
                "description": {"type": "string"},
                "real_dataset_id": {"type": "string", "description": "追加模式的目标数据集 ID"},
                "script_id": {"type": "string", "description": "清洗脚本 ID 或名称；不传则不展开数据行"},
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
    "dataset_versions": {
        "method": "GET",
        "path": "/api/v1/evaluation/datasets/{dataset_id}/versions",
        "path_params": ["dataset_id"],
        "body": False,
        "desc": (
            "列出数据集的版本历史（M5 版本化，新→旧）：每版含版本号、行数、变更说明、"
            "打版时间与打版人。评测任务可锁定数据集版本（dataset_version_no）保证可复现——"
            "用本工具确认可用版本号；任务配置的版本语义见 task_create/task_update 的数据集绑定说明"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string", "description": "数据集 ID"},
            },
            "required": ["dataset_id"],
        },
    },

    # ── 评测任务 ─────────────────────────────────────────────────────
    "model_list": {
        "method": "GET",
        "path": "/api/v1/models",
        "body": False,
        "desc": "列出可选的被评测模型配置（id 填入 task_create 的 model_provider）",
        "schema": {"type": "object", "properties": {}},
    },
    "task_list": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks",
        "query_params": ["status", "evaluation_type", "name", "skip", "limit"],
        "body": False,
        "desc": "列出评测任务（可按状态/类型/名称过滤）",
        "schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "running", "completed", "failed", "stopped"], "description": "状态过滤（可选）"},
                "evaluation_type": {"type": "string", "enum": ["judge", "agent", "consistency", "success_rate", "rag", "binary_classification", "latency", "plugin", "safety"], "description": "评测类型过滤（可选；simulation 类型经仿真场景运行，见 simulation_run）"},
                "name": {"type": "string", "description": "任务名称模糊过滤（不区分大小写的子串匹配，可选）"},
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
            "创建评测任务。必填 name 与 evaluation_type（judge/agent/consistency/"
            "success_rate/rag/binary_classification/latency/plugin/safety；simulation 类型"
            "经仿真场景运行，不支持直接创建）；judge 类型还需数据集（dataset_id）、待评测数据行"
            "（evaluation_targets，见该字段说明）、被评测模型（model_provider，见 model_list）"
            "与评测维度（evaluation_metrics，含 name/weight/description 的维度列表），"
            "裁判可配 judge_model/judge_engine/judge_count/judge_prompt 等"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "evaluation_type": {
                    "type": "string",
                    "enum": ["judge", "agent", "consistency", "success_rate", "rag", "binary_classification", "latency", "plugin", "safety"],
                    "description": "评测类型：judge（裁判打分）/agent（长会话）/consistency/success_rate/rag/binary_classification/latency/plugin/safety",
                },
                "dataset_id": {"type": "string", "description": "评测数据集 ID（见 dataset_list；可用 dataset_versions 查版本号）"},
                "dataset_version_no": {"type": "integer", "description": "锁定数据集版本（M5，可选）：空=跟随最新，整数=dataset_versions 里的版本号（换 dataset_id 时必须同批切换）"},
                "model_provider": {"type": "string", "description": "被评测模型配置 ID（见 model_list）"},
                "judge_model": {"type": "string", "description": "裁判模型配置 ID（judge 类型，可选）"},
                "judge_engine": {"type": "string", "description": "裁判引擎：claude_agent（默认，Claude Code Agent）/deepseek_agent（DeepSeek Agent，M8）/graph（传统裁判图）"},
                "judge_count": {"type": "integer", "description": "裁判数量（默认 6）"},
                "judge_prompt": {"type": "string", "description": "自定义评测 prompt（可选）"},
                "evaluation_metrics": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": '维度 [{"name":"准确性","weight":0.6,"description":"..."}]，weight 合计 1',
                },
                "evaluation_targets": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        '待评测行 [{"id":"target_x","name":"模型输出","source":"model_output"}]；'
                        "source 可选 model_output/chain_result/dataset_field/script_output/manual_input"
                    ),
                },
                "max_score": {"type": "integer", "description": "满分，默认 10"},
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
    "run_task_with_gate": {
        "method": "POST",
        "path": "/api/v1/evaluation/tasks/{task_id}/run",
        "path_params": ["task_id"],
        "body": False,
        "desc": (
            "以门禁口径触发评测执行（M1 评测即门禁）：任务配置了门禁阈值"
            "（gate_config）时，本次执行的聚合结果会被判定 passed/failed/error 并随执行记录落库。"
            "本工具触发后立即返回，门禁终态用 get_gate_status 轮询——verdict=passed/failed/error "
            "即终态（语义同 CI：passed=放行 / failed=质量不达标 / error=执行或配置异常）；"
            "未配置门禁的任务调用本工具等同 task_run（get_gate_status 返回 gate_enabled=false）"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID（需已在平台配置门禁阈值）"},
            },
            "required": ["task_id"],
        },
    },
    "get_gate_status": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks/{task_id}/gate",
        "path_params": ["task_id"],
        "query_params": ["execution_number"],
        "body": False,
        "desc": (
            "查询评测门禁状态（M1，轮询专用轻端点）：返回任务状态、是否启用门禁与指定执行"
            "（缺省最新一次）的门禁判定 {verdict: passed|failed|error, checks: 逐阈值实际值, "
            "evaluated_at}。agent 作为门禁调用方的判定口径：verdict 非 null 即终态——"
            "passed=放行、failed=质量不达标、error=执行失败/停止或门禁配置异常；"
            "verdict 为 null 且任务 running 时继续轮询，gate_enabled=false 表示任务未启用门禁"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "execution_number": {
                    "type": "integer",
                    "description": "执行序号（第几次执行，从 1 起；缺省=最新一次）",
                },
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
        "desc": "评测结果摘要：通过率/平均分/维度分/top10 失败样例与前端结果页链接",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    "task_status": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks/{task_id}/status",
        "path_params": ["task_id"],
        "body": False,
        "desc": "轻量状态轮询：仅 status/progress/completed_questions/total",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    "task_executions": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks/{task_id}/executions",
        "path_params": ["task_id"],
        "query_params": ["limit"],
        "body": False,
        "postprocess": _strip_execution_records,
        "desc": "执行历史列表（倒序）：序号/状态/成本/错误/起止时间，不含每版全量 result",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "limit": {"type": "integer", "description": "返回上限，默认 50（最大 200）"},
            },
            "required": ["task_id"],
        },
    },
    "task_execution_detail": {
        "method": "GET",
        "path": "/api/v1/evaluation/results/{task_id}/history/{execution_number}",
        "path_params": ["task_id", "execution_number"],
        "query_params": ["skip", "limit"],
        "body": False,
        "postprocess": paginate_detail,
        "desc": (
            f"获取指定执行版本的逐题明细（items 分页）：返回该版汇总指标（通过率/平均分/维度得分，"
            f"不截断）与第 skip 条起的最多 limit 题明细（默认 {DETAIL_PAGE_SIZE} 题/页，上限 "
            f"{DETAIL_MAX_PAGE_SIZE} 题，题目长文本不截断）。失败/停止版本返回错误摘要。"
            "适合翻页定位问题题目；全量归档用 task_execution_export 导出文档"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "execution_number": {"type": "integer", "description": "执行序号（第几次执行，从 1 起；用 task_executions 查可用版本）"},
                "skip": {"type": "integer", "description": f"明细跳过条数（默认 0）"},
                "limit": {"type": "integer", "description": f"明细返回条数上限（默认 {DETAIL_PAGE_SIZE}，最大 {DETAIL_MAX_PAGE_SIZE}）"},
            },
            "required": ["task_id", "execution_number"],
        },
    },
    "task_execution_export": {
        "method": "POST",
        "path": "/api/v1/evaluation/results/{task_id}/history/{execution_number}/export",
        "path_params": ["task_id", "execution_number"],
        "body": True,
        "timeout_seconds": 180,
        "desc": (
            "把指定执行版本的结果导出为文档并上传 anc-hub file-store，返回 file_id（用 "
            "file-store 的 file_download 分块下载）。format=markdown 出全量人读报告（任务元信息/"
            "配置摘要/汇总指标/逐题明细含裁判评语全文；失败版本只出失败简报；safety 任务自动切换"
            "「安全评测报告（合规证据）」——检查项对齐《生成式人工智能服务安全基本要求》类目命名，"
            "含结果/证据/复测建议）；format=json 出无损结构；format=xlsx（仅 safety 任务）出安全"
            "评测合规报告工作簿（检查项汇总/违规证据/复测建议三工作表），可作门禁材料外发。"
            "同版本同格式幂等：重复导出直接复用已上传的 file_id（响应 cached=true）"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "execution_number": {"type": "integer", "description": "执行序号（第几次执行，从 1 起）"},
                "format": {"type": "string", "description": "导出格式：markdown（全量报告，默认；safety 任务出合规安全报告）/ json（无损）/ xlsx（仅 safety 任务，安全合规报告工作簿）"},
            },
            "required": ["task_id", "execution_number"],
        },
    },
    "task_logs": {
        "method": "GET",
        "path": "/api/v1/evaluation/tasks/{task_id}/logs",
        "path_params": ["task_id"],
        "query_params": ["level", "limit"],
        "body": False,
        "desc": "获取评测任务的执行日志（可按级别过滤），排查执行失败时直接看日志",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "level": {"type": "string", "description": "日志级别过滤（可选，如 error/warning/info）"},
                "limit": {"type": "integer", "description": "返回条数上限（默认 100，最大 500）"},
            },
            "required": ["task_id"],
        },
    },

    # ── 仿真评测（M7 QE7-4，第 9 种评测类型） ────────────────────────
    "simulation_run": {
        "method": "POST",
        "path": "/api/v1/simulation/scenarios/{scenario_id}/run",
        "path_params": ["scenario_id"],
        "body": False,
        "desc": (
            "触发仿真评测运行（M7）：对既有仿真场景跑 N 个 trial 的 persona 用户模拟"
            "（含故障注入与终态校验），异步执行立即返回 run_id。用 simulation_run_status "
            "轮询进度/终态（completed/failed/stopped 即终态）；团队并发配额满时返回 429，稍后重试"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "scenario_id": {"type": "string", "description": "仿真场景 ID（在前端仿真页创建）"},
            },
            "required": ["scenario_id"],
        },
    },
    "simulation_run_status": {
        "method": "GET",
        "path": "/api/v1/simulation/runs/{run_id}",
        "path_params": ["run_id"],
        "body": False,
        "postprocess": _summarize_simulation_run,
        "desc": (
            "查询仿真运行状态与指标（轮询专用）：status/trials_done 与完成后指标"
            "（pass^k/任务成功率/平均回合数/工具浪费率/故障恢复率 + 逐 trial 概要）。"
            "逐 trial 对话回放与终态校验明细已省略（载荷过大），回放/证据请在前端仿真页查看"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "simulation_run 返回的 run_id"},
            },
            "required": ["run_id"],
        },
    },
}

# ── 工具面：暴露给 agent 的 15 个工具 ─────────────────────────────
# 一个工具 = 一个选择器（默认 action）+ 若干路由。合并带来的上下文收益来自
# 「工具条目数」而非 schema 体量，因此只合并语义相邻、审批策略相同的路由。
# 未在此声明的 route（dataset_list/dataset_get/...）是隐藏别名，tools/list 看不到，
# 但仍可经 invoke_plugin 的 payload.op 调用——下游未点名使用的旧名保留一个版本周期。

TOOL_GROUPS: Dict[str, Dict[str, Any]] = {
    "dataset_query": {
        "selector": "view",
        "default": "list",
        "actions": {
            "list": "dataset_list",
            "detail": "dataset_get",
            "rows": "dataset_data_list",
        },
        "desc": "查询数据集：list=列表、detail=单个详情、rows=数据行分页（后两者需 dataset_id）",
        "schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["list", "detail", "rows"]},
                "dataset_id": {"type": "string", "description": "detail/rows 必填"},
                "skip": {"type": "integer", "description": "默认 0"},
                "limit": {"type": "integer", "description": "默认 100（rows 最大 500）"},
            },
        },
    },
    "dataset_create": {"actions": {"create": "dataset_create"}},
    "dataset_import_hub_files": {"actions": {"import": "dataset_import_hub_files"}},
    "dataset_manage": {
        "selector": "action",
        "default": "update",
        "actions": {"update": "dataset_update", "delete": "dataset_delete"},
        "requires_approval": True,
        "desc": "修改或删除数据集（需人工审批）",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["update", "delete"], "description": "update=改名/改描述；delete=软删除"},
                "dataset_id": {"type": "string", "description": "数据集 ID"},
                "name": {"type": "string", "description": "新名称（update）"},
                "description": {"type": "string", "description": "新描述（update）"},
            },
            "required": ["action", "dataset_id"],
        },
    },
    "hub_import_query": {
        "selector": "view",
        "default": "files",
        "actions": {"files": "hub_files_query", "job": "hub_import_job_status"},
        "desc": "导入链路查询：files=file-store 已上传文件（找 file_id）、job=导入任务进度（需 job_id）",
        "schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["files", "job"], "description": "files=已上传文件（默认）；job=导入进度"},
                "job_id": {"type": "string", "description": "view=job 必填"},
                "name_like": {"type": "string", "description": "文件名子串（不区分大小写）"},
                "uploaded_after": {"type": "string", "description": "RFC3339，含边界"},
                "uploaded_before": {"type": "string", "description": "RFC3339"},
                "uploader": {"type": "string"},
                "page": {"type": "integer", "description": "页号，从 1 起"},
                "page_size": {"type": "integer", "description": "1-200"},
            },
        },
    },
    "model_list": {"actions": {"list": "model_list"}},
    "task_create": {"actions": {"create": "task_create"}},
    "task_list": {"actions": {"list": "task_list"}},
    "task_get": {
        "selector": "view",
        "default": "detail",
        "actions": {"detail": "task_get", "logs": "task_logs"},
        "desc": "任务详情：detail=状态/进度/配置、logs=执行日志（可按 level 过滤）",
        "schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["detail", "logs"], "description": "detail=状态/进度/配置（默认）；logs=执行日志"},
                "task_id": {"type": "string", "description": "任务 ID"},
                "level": {"type": "string", "description": "日志级别（view=logs）：error/warning/info"},
                "limit": {"type": "integer", "description": "日志条数上限，默认 100（最大 500）"},
            },
            "required": ["task_id"],
        },
    },
    "task_status": {"actions": {"status": "task_status"}},
    "task_results": {"actions": {"results": "task_results"}},
    "task_executions": {"actions": {"list": "task_executions"}},
    "task_execution_detail": {
        "selector": "action",
        "default": "page",
        "actions": {"page": "task_execution_detail", "export": "task_execution_export"},
        "desc": (
            "指定执行版本：page=逐题明细分页（默认 10 题/页，当页长文本不截断）、"
            "export=导出 markdown 全量报告（safety 任务出合规安全报告）/ json 无损 / "
            "xlsx（仅 safety 任务，安全合规工作簿）文档到 file-store 返 file_id"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "execution_number": {"type": "integer", "description": "执行序号，从 1 起"},
                "action": {"type": "string", "enum": ["page", "export"]},
                "format": {"type": "string", "enum": ["markdown", "json", "xlsx"], "description": "action=export 的格式，默认 markdown；xlsx 仅 safety 任务"},
                "skip": {"type": "integer", "description": "跳过条数，默认 0"},
                "limit": {"type": "integer", "description": f"分页大小，默认 {DETAIL_PAGE_SIZE}（最大 {DETAIL_MAX_PAGE_SIZE}）"},
            },
            "required": ["task_id", "execution_number"],
        },
    },
    "task_run": {
        "selector": "action",
        "default": "run",
        "actions": {"run": "task_run", "stop": "task_stop"},
        "desc": "触发或停止评测执行（run 异步提交，用 task_status 轮询进度）",
        "schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "action": {"type": "string", "enum": ["run", "stop"], "description": "run=触发执行（默认）；stop=停止"},
            },
            "required": ["task_id"],
        },
    },
    "task_manage": {
        "selector": "action",
        "default": "update",
        "actions": {"update": "task_update", "delete": "task_delete"},
        "requires_approval": True,
        "desc": "修改或删除评测任务（需人工审批；update 只传要改的字段）",
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["update", "delete"]},
                "task_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "dataset_id": {"type": "string"},
                "model_provider": {"type": "string"},
                "judge_model": {"type": "string"},
                "judge_count": {"type": "integer"},
                "judge_prompt": {"type": "string"},
                "evaluation_metrics": {"type": "array", "items": {"type": "object"}},
                "max_score": {"type": "integer"},
            },
            "required": ["action", "task_id"],
        },
    },
    # ── QE 单动作工具（评测即门禁 / 数据集版本 / 仿真评测） ────────────────
    "dataset_versions": {"actions": {"list": "dataset_versions"}},
    "run_task_with_gate": {"actions": {"run": "run_task_with_gate"}},
    "get_gate_status": {"actions": {"status": "get_gate_status"}},
    "simulation_run": {"actions": {"run": "simulation_run"}},
    "simulation_run_status": {"actions": {"status": "simulation_run_status"}},
}

# 选择器参数：分发前必须从载荷里摘掉，不能拼进 URL/query/body
SELECTOR_KEYS = frozenset({"action", "view"})


def _group_default_action(group: Dict[str, Any]) -> str:
    """工具的默认动作：``default`` 优先，否则取第一个声明（单动作工具）。"""
    return group.get("default") or next(iter(group["actions"]))


def build_tool_decls() -> list[plugin_pb2.ToolDecl]:
    """把 TOOL_GROUPS 转成 manifest 的工具声明列表（隐藏别名不声明）。"""
    tools: list[plugin_pb2.ToolDecl] = []
    for name, group in TOOL_GROUPS.items():
        first_route = TOOL_ROUTES[next(iter(group["actions"].values()))]
        approval = group.get("requires_approval")
        if approval is None:
            approval = first_route.get("requires_approval", False)
        tools.append(
            plugin_pb2.ToolDecl(
                name=name,
                description=group.get("desc") or first_route["desc"],
                input_schema_json=json.dumps(
                    group.get("schema") or first_route["schema"], ensure_ascii=False
                ),
                requires_approval=bool(approval),
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


def resolve_dispatch(env_meta: dict, payload: dict) -> tuple[str | None, str | None]:
    """确定本次调用：``(工具名, 路由名)``。

    meta['hub.tool'] 优先，payload['op'] 兜底。工具名命中 TOOL_GROUPS 时按选择器
    解析到具体 route；否则按隐藏别名/``invoke_plugin`` 旧 op 直接匹配 TOOL_ROUTES。
    任一环节不成立时路由名为 None，由调用方给出可行动的错误卡。
    """
    tool = env_meta.get("hub.tool")
    if not tool:
        op = payload.get("op")
        tool = op if isinstance(op, str) else None
    if not tool:
        return None, None

    group = TOOL_GROUPS.get(tool)
    if group is None:
        return tool, tool if tool in TOOL_ROUTES else None

    selector = group.get("selector", "action")
    action = payload.get(selector) or _group_default_action(group)
    if not isinstance(action, str):
        return tool, None
    return tool, group["actions"].get(action)


def group_actions(tool: str) -> list[str]:
    """工具的选择器取值清单（错误卡里给 agent 看）。"""
    group = TOOL_GROUPS.get(tool)
    return sorted(group["actions"]) if group else []


def sibling_param_keys(tool: str, op: str) -> frozenset[str]:
    """合并工具里「属于兄弟动作、不属于本次路由」的参数键。

    这些键不得进请求体：例如 ``task_execution_detail`` 导出时残留的 ``skip``/``limit``、
    ``hub_import_query`` 查文件时残留的 ``job_id``。单动作工具返回空集（保留自由透传）。
    """
    group = TOOL_GROUPS.get(tool)
    if not group or len(group["actions"]) < 2 or op not in TOOL_ROUTES:
        return frozenset()

    route = TOOL_ROUTES[op]
    own = (
        set(route.get("path_params", []))
        | set(route.get("query_params", []))
        | set((route.get("schema") or {}).get("properties", {}))
    )
    others: set[str] = set()
    for other_op in group["actions"].values():
        if other_op == op:
            continue
        other = TOOL_ROUTES[other_op]
        others |= set(other.get("path_params", [])) | set(other.get("query_params", []))
    return frozenset(others - own)


def postprocess_of(op: str) -> Callable[[Any, Dict[str, Any]], Any] | None:
    """取该路由的响应后处理函数，签名 ``fn(data, ctx) -> data``。"""
    return TOOL_ROUTES[op].get("postprocess")
