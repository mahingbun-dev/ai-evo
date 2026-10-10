"""评测前处理脚本执行器

按编排步骤串行执行前处理脚本（设计：docs/design/pre-judge-scripts.md
第二/三/四/七节 + 统一子进程执行改造）。步骤来源双路径（共享设计契约 v3
§二/§三）：优先取 evaluation_targets 中 script_output 行派生的步骤
（derive_script_steps，targets 为唯一事实源）；未迁移任务回退
task.pre_scripts 旧配置。支持 exec_type：
- script：子进程执行脚本包（uploads/pre_judge_scripts/{id}/ 目录，入口
  main.py；builtin 进程内类型已废除，内置脚本同样迁移为 script 记录）——
  解释器用项目 .venv python（环境变量 PRE_JUDGE_SCRIPT_PYTHON 可覆盖），
  统一经 app/services/pre_judge_entry.py wrapper 调起（run(stdin) 函数式 /
  __main__ 传统式自动适配），PYTHONPATH 注入项目根（支持
  `from app.db.session import SessionLocal` 等平台侧 import），
  cwd 隔离到 uploads/script_runs/{task_id}/{data_id}/{script_id}/，
  标准输入 JSON 走 stdin，取标准输出最后一行 JSON 作为脚本标准输出，
  超时（timeout_seconds）kill，标准输出读取有上限截断
- 严格拷贝语义（核心要求：每条数据评测即建临时工作区，脚本/文件/数据全部
  拷进工作区，评完即清）：脚本包在执行前拷贝到工作区 scripts/ 下从副本
  执行（脚本对自身包目录的写操作只影响本条副本，不污染脚本库与其它数据条）；
  local/上传文件与 hub_file 执行包目录解析时拷进工作区 inputs/ 下，stdin
  一律给工作区内副本路径（inline 值本就落盘工作区）
- claude_agent / deepseek_agent：由 HarnessAdapter 阶段接入，到达此处明确 fail
  （on_fail 默认 continue，裁判可见失败事实）

扩展点（设计：docs/design/pre-judge-scripts.md §九.3，挂载点规范化命名）：
- EXTENSION_POINT_PRE_JUDGE = "evaluation/pre-judge"（已启用）
  本模块 run_pre_scripts 的挂载位置：evaluation_executor._evaluate_single
  步骤1（model_output 生成）之后、步骤2（裁判 Prompt 组装）之前。
- EXTENSION_POINT_RESULT = "evaluation/result"（本期预留）
  本模块 run_result_observers 的挂载位置：evaluation_executor.execute_batch
  聚合完成后，遍历任务配置的 result 观察脚本，只读 result、产出附加到
  result.observations，注册表 RESULT_OBSERVER_REGISTRY 与前处理脚本注册表
  相互独立（防止前处理脚本被误配为观察者）；本期仅接口 + 空实现示例
  （scripts_builtin.result_observers.example_noop）。

执行语义：
- 串行按编排顺序执行，前序脚本标准输出注入后序 stdin.upstream
- 输入映射按 value_type 分流注入 stdin（缺陷B3，与脚手架 README/上传冒烟
  契约 build_smoke_stdin 一致）：inline/local 通道值（本地绝对路径，inline
  写临时文件）注入 stdin.fields；hub_file 通道（阶段B）注入 stdin.inputs——
  字段值为解包区目录：配 path_pattern 时输入值为
  {root, files:[{path,rel_path,size,type}]}（共享匹配器 match_files_detail，
  与预览阶段同语义；匹配 0 文件且 required=true 按 fail 走 on_fail，
  required=false 则 files:[] 继续执行），未配时为 {root, index_path}
  （索引写运行目录 inputs/{field}_index.json）；oss 通道尚未接入，明确报错
- 产物写 uploads/script_runs/{task_id}/{data_id}/（script 类型 cwd 隔离到其下
  {script_id}/ 子目录，stdout/stderr 落盘在该目录）
- script 超时统一子进程 wait timeout + kill（builtin 进程内类型已废除）
- script 类型运行前做静态风险扫描（提示级：subprocess/os.system 等风险点
  写入结果 static_check 并记日志，不做硬拦截）
- on_fail: continue（默认）→ 记录 fail 结果继续后续脚本；abort → 立即终止编排
"""
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.db.session import SessionLocal
from app.models.pre_judge_models import PreJudgeScript
from app.services import pre_judge_script_service
from app.services.hub_file_service import (
    HubFileError,
    build_file_index,
    match_files_detail,
)
from app.services.pre_judge_script_service import (
    EXTENSION_POINT_PRE_JUDGE,
    EXTENSION_POINT_RESULT,
    scan_script_dir_risks,
    scan_script_risks,
)
from app.scripts_builtin import result_observers as _result_observers

logger = logging.getLogger(__name__)

# 产物根目录（相对项目根，与 uploads/judge_sessions 同级约定；测试可 monkeypatch）
SCRIPT_RUN_BASE = Path("uploads/script_runs")

# script 类型子进程解释器：优先项目 venv python，可用环境变量覆盖；
# 项目 venv 不存在（如容器部署）时退回当前解释器
_DEFAULT_VENV_PYTHON = "/Users/maqb11/code/qa/ai-evo/.venv/bin/python"
SCRIPT_PYTHON = (
    os.environ.get("PRE_JUDGE_SCRIPT_PYTHON")
    or (_DEFAULT_VENV_PYTHON if Path(_DEFAULT_VENV_PYTHON).is_file() else sys.executable)
)
# 统一子进程执行 wrapper（app/services/pre_judge_entry.py，从本文件位置推算）
ENTRY_WRAPPER = Path(__file__).resolve().parent / "pre_judge_entry.py"
# 项目根（app 包位置推算，不硬编码）：注入子进程 PYTHONPATH，
# 使脚本可 `from app.db.session import SessionLocal` 等平台侧 import
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# script 类型标准输出读取上限（字节）：只读 stdout 文件尾部该上限内容再解析最后一行 JSON
SCRIPT_STDOUT_LIMIT = 1024 * 1024
# 错误诊断用 stderr 读取上限（字节）
SCRIPT_STDERR_LIMIT = 4 * 1024

_OUTPUT_KEYS = ("status", "output", "files", "summary", "finish_reason", "error")

__all__ = [
    "EXTENSION_POINT_PRE_JUDGE",
    "EXTENSION_POINT_RESULT",
    "ENTRY_WRAPPER",
    "PROJECT_ROOT",
    "RESULT_OBSERVER_REGISTRY",
    "PreScriptInputError",
    "SCRIPT_PYTHON",
    "SCRIPT_RUN_BASE",
    "SCRIPT_STDOUT_LIMIT",
    "build_run_dir",
    "build_script_run_dir",
    "cleanup_item_run_files",
    "cleanup_script_run_files",
    "copy_into_workspace",
    "derive_script_steps",
    "execute_script",
    "register_result_observer",
    "run_pre_scripts",
    "run_result_observers",
    "write_inline_value",
]


class PreScriptInputError(Exception):
    """脚本输入组装失败（字段缺失/通道未接入），按该脚本 fail 处理"""


# ---------------- evaluation/result 扩展点（本期预留：结果观察） ----------------

# 结果观察脚本注册表：name → callable，签名 (stdin: dict) -> dict（与前处理脚本同契约）。
# 与前处理脚本相互独立：前处理脚本不得被误配为结果观察者。
RESULT_OBSERVER_REGISTRY: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def register_result_observer(name: str, func: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
    """注册结果观察脚本（只读 result，产出附加到 result.observations）"""
    if not callable(func):
        raise TypeError(f"结果观察脚本 {name} 必须是可调用对象")
    RESULT_OBSERVER_REGISTRY[name] = func


# 空实现示例（本期唯一注册项，作为观察脚本编写模板）
register_result_observer("example_noop", _result_observers.run)


def run_result_observers(task, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """执行任务配置的结果观察脚本（EXTENSION_POINT_RESULT，设计 §九.3）

    挂载位置：execute_batch 聚合完成后、产物清理之前。对每条 result 逐观察者执行：
    - 只读保证：stdin.result 传入结果快照（深拷贝），观察者修改快照不影响原 result
    - 产出附加：观察者标准输出附加到 result["observations"]（原 result 无该键时创建）
    - 单观察者失败/异常按 fail 条目记录并继续，绝不阻断评测主流程

    Args:
        task: 评测任务对象（读取 result_observers 编排：[{observer: 名称, params: {}}]）
        results: execute_batch 聚合后的评测结果列表（就地附加 observations）

    Returns:
        results 本身（便于链式调用）；任务未配置观察者时原样返回、不新增键
    """
    steps = getattr(task, "result_observers", None) or []
    if not steps:
        return results
    task_id = str(getattr(task, "id", "") or "unknown")

    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        observations: List[Dict[str, Any]] = []
        snapshot = copy.deepcopy(result)  # 只读快照：观察者的修改不回写
        for step in steps:
            if not isinstance(step, dict):
                logger.warning(f"[ResultObservers] 忽略非法观察编排项: {step!r}")
                continue
            observations.append(
                _run_observer_step(task_id, index, step, snapshot)
            )
        if observations:
            existing = result.get("observations")
            if isinstance(existing, list):
                existing.extend(observations)
            else:
                result["observations"] = observations
    return results


def _run_observer_step(
    task_id: str,
    result_index: int,
    step: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """执行单个结果观察步骤，返回 observations 条目（失败不抛异常）"""
    observer_name = str(step.get("observer") or step.get("script_id") or "")
    started = time.time()

    func = RESULT_OBSERVER_REGISTRY.get(observer_name)
    if func is None:
        return _observer_entry(
            observer_name, "fail", started,
            error=f"结果观察脚本未注册: {observer_name}（可用: {', '.join(sorted(RESULT_OBSERVER_REGISTRY))}）",
        )

    stdin: Dict[str, Any] = {
        "task_id": task_id,
        "result": snapshot,
        "result_index": result_index,
        "params": step.get("params") or {},
    }
    try:
        stdout = func(stdin)
    except Exception as e:  # noqa: BLE001 - 观察边界：异常转 fail 条目
        logger.exception(f"[ResultObservers] 观察脚本 {observer_name} 执行异常")
        return _observer_entry(observer_name, "fail", started, error=f"观察脚本执行异常: {e}")

    if not isinstance(stdout, dict):
        return _observer_entry(observer_name, "fail", started, error="观察脚本标准输出不是 JSON 对象")

    status = stdout.get("status") or "fail"
    if status not in ("ok", "skip", "fail"):
        status = "fail"
    return _observer_entry(
        observer_name, status, started,
        output=stdout.get("output"),
        files=stdout.get("files") or {},
        summary=stdout.get("summary") or "",
        error=stdout.get("error"),
    )


def _observer_entry(
    observer_name: str,
    status: str,
    started_at: float,
    output: Any = None,
    files: Optional[Dict[str, str]] = None,
    summary: str = "",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """组装 observations 标准条目（含扩展点标注，供结果页与排障区分来源）"""
    return {
        "extension_point": EXTENSION_POINT_RESULT,
        "observer": observer_name,
        "status": status,
        "output": output,
        "files": files or {},
        "summary": summary,
        "error": error,
        "duration_ms": int((time.time() - started_at) * 1000),
    }


def build_run_dir(task_id: str, data_id: str) -> Path:
    """创建并返回产物目录 uploads/script_runs/{task_id}/{data_id}/

    一律返回绝对路径：该值会以 stdin._run_dir 传给子进程，子进程 cwd 是
    隔离目录，相对路径会被错误拼到子进程 cwd 下导致产物写丢。
    """
    run_dir = (SCRIPT_RUN_BASE / str(task_id) / str(data_id)).absolute()
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def build_script_run_dir(task_id: str, data_id: str, script_id: str) -> Path:
    """创建并返回 script 类型的 cwd 隔离目录

    uploads/script_runs/{task_id}/{data_id}/{script_id}/：脚本进程的 cwd，
    其相对路径写盘的文件（含 stdout/stderr 重定向）都落在该目录，随任务清理。
    返回绝对路径（Popen cwd 与日志展示均按绝对路径更稳）。
    """
    run_dir = (SCRIPT_RUN_BASE / str(task_id) / str(data_id) / _safe_name(script_id)).absolute()
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def cleanup_script_run_files(task_id: str) -> None:
    """评测结束后清理该任务的脚本产物目录 uploads/script_runs/{task_id}/

    复用 judge_sessions 临时文件的清理模式（execute_batch 结束统一调用）。
    目录不存在时为 no-op；清理失败由调用方捕获告警，不影响任务状态。
    """
    target = SCRIPT_RUN_BASE / str(task_id)
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=True)


def cleanup_item_run_files(task_id: str, data_id: str) -> None:
    """单条数据评测完成后清理其临时工作区 uploads/script_runs/{task_id}/{data_id}/

    每条数据评测即建区、评完即清（核心要求）：工作区含 inline 输入落盘、
    脚本 cwd 产物与 stdout/stderr 日志。数据项的 id/idx 派生口径与
    _run_step 的 data_id 一致（id 优先，idx 次之，兜底 "item"）。
    目录不存在时为 no-op；execute_batch 结束的任务级清理保留为兜底
    （进程中断等异常场景的残留）。
    """
    target = SCRIPT_RUN_BASE / str(task_id) / str(data_id)
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=True)


def run_pre_scripts(
    task,
    payload: Optional[Dict[str, Any]] = None,
    dataset_item: Optional[Dict[str, Any]] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """执行任务编排的全部前处理脚本（串行）

    扩展点标注：EXTENSION_POINT_PRE_JUDGE（"evaluation/pre-judge"），挂载于
    evaluation_executor._evaluate_single 步骤1 之后、步骤2（裁判 Prompt）之前。

    Args:
        task: EvaluationTask（读取 pre_scripts / id）
        payload: 评测请求 payload（可含 _dataset_item / model_output / idx）
        dataset_item: 数据项（{id, data: {...}}），缺省时从 payload._dataset_item 取
        steps: 显式编排步骤（共享设计契约 v3 §三）：优先于 task.pre_scripts；
            由 evaluation_targets 的 script_output 行经 derive_script_steps 派生。
            None 保持旧行为读取 task.pre_scripts（未迁移任务兼容路径）

    Returns:
        {
          "pre_script_results": [ {script_id, script_name, status, output, files,
                                   summary, finish_reason, duration_ms, error}, ... ],
          "script_outputs": {output_var或脚本名: output dict},   # 仅 status=ok
          "script_files":   {output_var或脚本名: {文件名: 本地路径}},  # 仅 status=ok
          "aborted": 是否因 on_fail=abort 提前终止,
          "error": abort 时的失败原因,
        }
    """
    payload = payload or {}
    pre_scripts = steps if steps is not None else (getattr(task, "pre_scripts", None) or [])
    task_id = str(getattr(task, "id", "") or "unknown")

    if dataset_item is None:
        dataset_item = payload.get("_dataset_item") or {}
    if not isinstance(dataset_item, dict):
        dataset_item = {}
    data = dataset_item.get("data") if isinstance(dataset_item.get("data"), dict) else {}
    data_id = dataset_item.get("id") or payload.get("idx") or "item"

    results: List[Dict[str, Any]] = []
    script_outputs: Dict[str, Any] = {}
    script_files: Dict[str, Dict[str, str]] = {}
    upstream: Dict[str, Any] = {}
    aborted = False
    abort_error: Optional[str] = None

    for step in pre_scripts:
        if not isinstance(step, dict):
            logger.warning(f"[PreScripts] 忽略非法编排项: {step!r}")
            continue
        result = _run_step(task_id, data_id, step, data, upstream, payload)
        results.append(result)
        script_name = result["script_name"]

        if result["status"] == "ok":
            output_key = step.get("output_var") or script_name
            script_outputs[output_key] = result.get("output") or {}
            script_files[output_key] = result.get("files") or {}
            upstream[script_name] = result
        elif result["status"] == "skip":
            upstream[script_name] = result
        else:  # fail
            upstream[script_name] = result
            on_fail = (step.get("on_fail") or "continue").lower()
            if on_fail == "abort":
                aborted = True
                abort_error = result.get("error") or f"前处理脚本 {script_name} 执行失败"
                logger.warning(f"[PreScripts] 脚本 {script_name} 失败且 on_fail=abort，终止编排: {abort_error}")
                break

    return {
        "pre_script_results": results,
        "script_outputs": script_outputs,
        "script_files": script_files,
        "aborted": aborted,
        "error": abort_error,
    }


def derive_script_steps(evaluation_targets: Any) -> List[Dict[str, Any]]:
    """从待评测数据（evaluation_targets）派生前处理脚本编排步骤（共享设计契约 v3 §二）

    v3 起 evaluation_targets 是脚本编排的唯一事实源：source=script_output 的行声明
    依赖某个前处理脚本，执行器在步骤1.5 用本函数把这些行还原成既有 step 结构后
    调 run_pre_scripts(steps=...)，task.pre_scripts 仅作未迁移任务的兼容路径。

    派生规则：
    - 按行序取 source == "script_output" 的行，跳过其余来源（model_output /
      dataset_field 等）与非法行
    - 按 script_id 去重：同一脚本多行引用时首行决定 input_mapping / params /
      on_fail / output_var（后序行消费同一份脚本输出，不重复执行）
    - output_var 缺省=脚本名（此处以 script_id 兜底，与 JudgeVariableResolver
      script_output 定位键 output_var or script_id 的口径一致）

    Args:
        evaluation_targets: 任务配置的评测目标列表（list[dict]；None/非列表返回空）

    Returns:
        step 列表 [{script_id, input_mapping, params, output_var, on_fail}, ...]，
        与既有 pre_scripts 编排项结构一致
    """
    if not evaluation_targets or not isinstance(evaluation_targets, list):
        return []

    steps: List[Dict[str, Any]] = []
    seen_script_ids: set = set()
    for target in evaluation_targets:
        if not isinstance(target, dict) or target.get("source") != "script_output":
            continue
        source_config = target.get("source_config") or {}
        if not isinstance(source_config, dict):
            source_config = {}
        script_id = str(source_config.get("script_id") or "").strip()
        if not script_id:
            logger.warning(
                f"[PreScripts] 忽略缺少 script_id 的 script_output 评测目标行: {target!r}"
            )
            continue
        if script_id in seen_script_ids:
            continue  # 同一脚本多行引用：首行配置生效，去重不重复执行
        seen_script_ids.add(script_id)

        output_var = source_config.get("output_var") or script_id
        on_fail = source_config.get("on_fail") or "continue"
        steps.append({
            "script_id": script_id,
            "input_mapping": source_config.get("input_mapping") or {},
            "params": source_config.get("params") or {},
            "output_var": output_var,
            "on_fail": on_fail,
        })
    return steps


def _run_step(
    task_id: str,
    data_id: Any,
    step: Dict[str, Any],
    data: Dict[str, Any],
    upstream: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """执行单个编排步骤并组装 pre_script_results 条目"""
    script_id = str(step.get("script_id") or "")
    started = time.time()

    script = _load_script(script_id)
    if script is None:
        return _result(script_id, script_id, "fail", error=f"脚本不存在: {script_id}",
                       finish_reason="error", started_at=started)

    name = script.get("name") or script_id
    try:
        run_dir = build_run_dir(task_id, str(data_id))
        fields: Dict[str, Any] = {}
        # hub_file 通道结果注入 stdin["inputs"]（缺陷B3：与脚手架 README 及上传
        # 冒烟契约 build_smoke_stdin 对齐，标准模板脚本按 inputs 取执行包输入）；
        # 其余通道（inline/local）保持 stdin["fields"]（值为路径字符串）不变
        inputs: Dict[str, Any] = {}
        for field_name, mapping in (step.get("input_mapping") or {}).items():
            value = _resolve_field(field_name, mapping, data, run_dir)
            if _is_hub_file_mapping(mapping):
                inputs[field_name] = value
            else:
                fields[field_name] = value
    except PreScriptInputError as e:
        return _result(script_id, name, "fail", error=str(e), finish_reason="error", started_at=started)

    stdin: Dict[str, Any] = {
        "task_id": task_id,
        "data_id": str(data_id),
        "fields": fields,
        "inputs": inputs,
        # model_output 超长临时文件与会话信息由执行器集成阶段填充；此处透传 payload 既有值
        "model_output": payload.get("model_output"),
        "session": _extract_session_info(data),
        "params": step.get("params") or {},
        "upstream": upstream,
        "_run_dir": str(run_dir),
    }
    return execute_script(script, stdin, timeout_seconds=script.get("timeout_seconds"))


def _is_hub_file_mapping(mapping: Any) -> bool:
    """判断 input_mapping 项是否为 hub_file 通道（与 _resolve_field 的归一化口径一致）"""
    if isinstance(mapping, str):
        mapping = {"field": mapping}
    if not isinstance(mapping, dict):
        return False
    return str(mapping.get("value_type") or "inline").lower() == "hub_file"


def execute_script(
    script: Dict[str, Any],
    stdin: Dict[str, Any],
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """执行单个脚本（统一子进程执行），返回标准输出
    + {script_id, script_name, duration_ms}

    供 run_pre_scripts 编排与 debug 端点复用。script 形如
    {script_id, name, exec_type, entrypoint, timeout_seconds}。
    """
    started = time.time()
    script_id = str(script.get("script_id") or script.get("id") or "")
    name = script.get("name") or script_id
    exec_type = script.get("exec_type") or "script"
    timeout = float(timeout_seconds or script.get("timeout_seconds") or 120)

    if exec_type == "script":
        return _execute_script_subprocess(script, stdin, timeout, started)

    return _result(
        script_id, name, "fail",
        error=(f"暂不支持 exec_type={exec_type}（claude_agent/deepseek_agent 类型由 HarnessAdapter 阶段接入）"
               if exec_type in ("claude_agent", "deepseek_agent")
               else f"exec_type={exec_type} 已废除（builtin 进程内执行已统一为子进程 script 执行）"),
        finish_reason="error", started_at=started,
    )


def _normalize_stdout(
    script_id: str,
    name: str,
    stdout: Any,
    started_at: float,
    **extras: Any,
) -> Dict[str, Any]:
    """把脚本标准输出（dict）归一化为 pre_script_results 条目

    校验：必须是 JSON 对象；status 仅允许 ok/skip/fail（非法值归一为 fail）。
    extras 透传附加键（如 script 类型的 static_check）。
    """
    if not isinstance(stdout, dict):
        return _result(script_id, name, "fail", error="脚本标准输出不是 JSON 对象",
                       finish_reason="error", started_at=started_at, **extras)

    raw_status = stdout.get("status") or "fail"
    result = _result(script_id, name, raw_status,
                     finish_reason=stdout.get("finish_reason") or "completed",
                     started_at=started_at, **extras)
    result["output"] = stdout.get("output")
    result["files"] = stdout.get("files") or {}
    result["summary"] = stdout.get("summary") or ""
    result["error"] = stdout.get("error")
    if raw_status not in ("ok", "skip", "fail"):
        result["status"] = "fail"
        result["error"] = f"非法 status 值: {raw_status}"
    return result


# ---------------- script 类型：子进程执行（统一 wrapper） ----------------

def _resolve_script_entry(script_id: str, entrypoint: str) -> Tuple[Optional[Path], Optional[Path]]:
    """解析 script 类型的脚本目录与入口文件，返回 (script_dir, entry_file)

    解析顺序（目录式优先，历史形态兼容）：
    1. entrypoint 为空 → (None, None)（未上传）
    2. 目录内相对路径（新语义，默认 main.py）：
       SCRIPT_UPLOAD_BASE/{script_id}/{entrypoint}（BASE 相对时按主进程 cwd 补全）
    3. 历史绝对路径 entrypoint：文件存在则直接用（其父目录作为脚本目录，
       兄弟模块 import 与静态扫描按该目录口径）
    4. 历史相对路径（相对主进程 cwd，即项目根）：cwd 转绝对后存在则用
    文件不存在时返回最近候选路径（script_dir 非 None，供错误信息展示）
    """
    if not entrypoint:
        return None, None
    rel = Path(entrypoint)

    base = Path(pre_judge_script_service.SCRIPT_UPLOAD_BASE)
    if not base.is_absolute():
        base = Path.cwd() / base
    script_dir = base / str(script_id or "_")

    if not rel.is_absolute():
        # 目录内相对路径（新语义）
        entry_in_dir = script_dir / rel
        if entry_in_dir.is_file():
            return script_dir, entry_in_dir
        # 历史相对路径（相对主进程 cwd）：转绝对再判（二期 API 联测缺陷①兼容）
        abs_candidate = (Path.cwd() / rel).resolve()
        if abs_candidate.is_file():
            return abs_candidate.parent, abs_candidate
        return script_dir, entry_in_dir

    if rel.is_file():
        return rel.parent, rel
    return None, rel


def _execute_script_subprocess(
    script: Dict[str, Any],
    stdin: Dict[str, Any],
    timeout: float,
    started: float,
) -> Dict[str, Any]:
    """exec_type=script：经 wrapper 子进程执行脚本包

    - 命令 [SCRIPT_PYTHON, app/services/pre_judge_entry.py, <script_dir>, <entry>]：
      wrapper 自动适配 run(stdin) 函数式 / __main__ 传统式脚本（入口缺省
      main.py；历史绝对路径 entrypoint 以其文件名直执），
      文件夹兄弟模块 import 由脚本目录 sys.path[0] 支持
    - 严格拷贝语义：脚本包先拷进该条工作区 scripts/ 下（_stage_script_package），
      子进程按副本执行——脚本对自身包目录的写操作只影响本条工作区
    - env 注入 PYTHONPATH（项目根前置，保留已有值）：脚本可 import 平台侧模块
      （如 from app.db.session import SessionLocal）
    - cwd 隔离到 uploads/script_runs/{task_id}/{data_id}/{script_id}/
    - 标准输入 JSON 走 stdin（ensure_ascii=False + default=str 容错）
    - stdout/stderr 重定向到 cwd 下文件；stdout 只读尾部 SCRIPT_STDOUT_LIMIT
      字节，取最后一个非空行解析 JSON 作为脚本标准输出
    - 超时（timeout 秒）kill 进程；退出码非 0 按 fail（附 stderr 尾部）
    - 运行前静态风险扫描整个脚本目录的 .py（提示级）：结果写入返回条目
      static_check 并记日志（脚本目录不在存储基座内时仅扫入口文件）
    """
    script_id = str(script.get("script_id") or script.get("id") or "")
    name = script.get("name") or script_id
    entrypoint = str(script.get("entrypoint") or "").strip()

    if not entrypoint:
        return _result(script_id, name, "fail",
                       error="script 类型脚本未上传脚本文件（entrypoint 为空）",
                       finish_reason="error", started_at=started)
    script_dir, entry_file = _resolve_script_entry(script_id, entrypoint)
    if script_dir is None or entry_file is None or not entry_file.is_file():
        return _result(script_id, name, "fail",
                       error=f"脚本入口不存在: {entry_file or entrypoint}",
                       finish_reason="error", started_at=started)

    # 入口相对脚本目录的路径（wrapper 第二参数；历史绝对路径形态退化为文件名）
    try:
        entry_rel = entry_file.resolve().relative_to(script_dir.resolve()).as_posix()
    except ValueError:
        entry_rel = entry_file.name

    # 运行前静态检查（提示级，不拦截）：脚本目录在存储基座内则扫描全部 .py
    # （文件夹包整体视角），否则（历史绝对路径）仅扫描入口文件，避免误扫项目根
    base = Path(pre_judge_script_service.SCRIPT_UPLOAD_BASE)
    if not base.is_absolute():
        base = Path.cwd() / base
    if script_dir.resolve().is_relative_to(base.resolve()):
        static_check = scan_script_dir_risks(script_dir)
    else:
        static_check = scan_script_risks(entry_file.read_text(encoding="utf-8", errors="replace"))
    for warning in static_check:
        logger.warning(f"[PreScripts] 脚本 {name} 静态检查提示: {warning}")

    try:
        cwd = build_script_run_dir(
            str(stdin.get("task_id") or "unknown"),
            str(stdin.get("data_id") or "item"),
            script_id or name,
        )
    except OSError as e:
        return _result(script_id, name, "fail", error=f"创建脚本运行目录失败: {e}",
                       finish_reason="error", started_at=started, static_check=static_check)

    # 严格拷贝语义：存储基座内的标准脚本包拷进该条工作区后从副本执行
    # （脚本对自身包目录的写操作只影响本条副本）；基座外的历史绝对路径
    # 兼容形态（非脚本包目录）保持原位执行，避免整树拷贝
    if script_dir.resolve().is_relative_to(base.resolve()):
        # _run_dir 缺省（如 debug 直调 execute_script）时退回 cwd 的父目录
        run_dir_value = stdin.get("_run_dir")
        run_dir = Path(str(run_dir_value)) if run_dir_value else Path(cwd).parent
        try:
            staged_dir = _stage_script_package(script_dir, run_dir, script_id or name)
        except OSError as e:
            return _result(script_id, name, "fail", error=f"脚本包拷贝进工作区失败: {e}",
                           finish_reason="error", started_at=started, static_check=static_check)
    else:
        staged_dir = script_dir

    stdin_payload = json.dumps(stdin, ensure_ascii=False, default=str).encode("utf-8")
    stdout_file = Path(cwd) / "stdout.log"
    stderr_file = Path(cwd) / "stderr.log"
    timed_out = False
    returncode: Optional[int] = None
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath else str(PROJECT_ROOT)
    )
    try:
        with open(stdout_file, "wb") as out_f, open(stderr_file, "wb") as err_f:
            proc = subprocess.Popen(
                [SCRIPT_PYTHON, str(ENTRY_WRAPPER), str(staged_dir), entry_rel],
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=out_f,
                stderr=err_f,
                env=env,
            )
            try:
                proc.stdin.write(stdin_payload)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # 脚本未读完 stdin 即退出，按其退出码/输出处理
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                returncode = proc.wait()
    except OSError as e:
        return _result(script_id, name, "fail", error=f"脚本子进程启动失败: {e}",
                       finish_reason="error", started_at=started, static_check=static_check)

    stdout_tail = _read_file_tail(stdout_file, SCRIPT_STDOUT_LIMIT)
    stderr_tail = _read_file_tail(stderr_file, SCRIPT_STDERR_LIMIT)

    if timed_out:
        return _result(script_id, name, "fail",
                       error=f"脚本执行超时被终止（>{timeout:.0f}秒）",
                       finish_reason="timeout", started_at=started, static_check=static_check)

    if returncode != 0:
        error = f"脚本退出码 {returncode}: {stderr_tail.strip() or '无 stderr 输出'}"
        return _result(script_id, name, "fail", error=error[:4000],
                       finish_reason="error", started_at=started, static_check=static_check)

    try:
        stdout = _parse_last_json_line(stdout_tail)
    except ValueError as e:
        return _result(script_id, name, "fail", error=str(e),
                       finish_reason="error", started_at=started, static_check=static_check)

    return _normalize_stdout(script_id, name, stdout, started, static_check=static_check)


def _parse_last_json_line(stdout_text: str) -> Any:
    """取标准输出最后一个非空行解析为 JSON；失败抛 ValueError（中文消息）"""
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("脚本标准输出为空（未找到最后一行 JSON）")
    last = lines[-1]
    try:
        return json.loads(last)
    except json.JSONDecodeError as e:
        raise ValueError(f"脚本最后一行不是合法 JSON: {e.msg}（输出片段: {last[:200]}）")


def _read_file_tail(path: Path, limit: int) -> str:
    """读取文件尾部最多 limit 字节并按 UTF-8 解码（替换非法字节）"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


# ---------------- 脚本加载 ----------------

def _load_script(script_id: str) -> Optional[Dict[str, Any]]:
    """按 script_id 加载脚本定义（仅查 DB；builtin:<name> 前缀与纯名称为历史
    引用兼容，按名称解析到 DB 记录）

    script 类型记录加载时顺带做旧布局迁移（{id}.py 单文件 → {id}/main.py，
    entrypoint 指向旧文件时改写为 main.py）。
    """
    name = script_id[len("builtin:"):] if script_id.startswith("builtin:") else script_id
    with SessionLocal() as db:
        row = db.query(PreJudgeScript).filter(PreJudgeScript.id == script_id).first()
        if row is None:
            row = db.query(PreJudgeScript).filter(PreJudgeScript.name == name).first()
        if row is None:
            return None
        if row.exec_type == "script":
            pre_judge_script_service.ensure_script_layout(db, row)
        return {
            "script_id": row.id,
            "name": row.name,
            "exec_type": row.exec_type,
            "entrypoint": row.entrypoint,
            "timeout_seconds": row.timeout_seconds if row.timeout_seconds is not None else 120,
        }


# ---------------- 输入映射解析 ----------------

def _resolve_field(script_field: str, mapping: Any, data: Dict[str, Any], run_dir: Path) -> Any:
    """把 input_mapping 的单个字段解析为脚本输入值

    - inline（默认）：字段值即内容，写运行目录 inputs/ 下的临时文件
      （文件名取脚本输入名 script_field，与脚本侧契约一致），返回文件路径
    - local：字段值为本地路径（或数据项 {field}_files[].file_path 上传文件）
    - hub_file（阶段B）：字段值为执行包解包区目录，返回 {root, ...} 结构
      （见 _resolve_hub_file_field），由 _run_step 注入 stdin.inputs（缺陷B3）；
      inline/local 仍返回路径字符串并注入 stdin.fields，脚本侧按键取值
    - oss：通道尚未接入，明确报错
    """
    if isinstance(mapping, str):
        mapping = {"field": mapping}  # 容错：直接给字段名
    if not isinstance(mapping, dict) or not mapping.get("field"):
        raise PreScriptInputError(f"input_mapping 缺少 field: {mapping!r}")
    field = mapping["field"]
    value_type = str(mapping.get("value_type") or "inline").lower()

    if value_type == "hub_file":
        return _resolve_hub_file_field(script_field, mapping, field, data, run_dir)
    if value_type == "oss":
        raise PreScriptInputError(
            f"字段 {script_field} 的 value_type=oss 通道尚未接入（OSS 缓存由后续阶段提供）"
        )

    raw = data.get(field)
    if raw is None:
        # local 通道：上传文件存储于 {field}_files 列表
        for file_info in data.get(f"{field}_files") or []:
            if isinstance(file_info, dict) and file_info.get("file_path"):
                candidate = str(file_info["file_path"])
                if os.path.exists(candidate):
                    return copy_into_workspace(candidate, run_dir, script_field)
        raise PreScriptInputError(f"数据项缺少字段 {field}（或其文件不存在）")

    if isinstance(raw, str):
        if os.path.exists(raw):
            # 严格拷贝语义：已存在的本地文件/目录（local 通道或前置流程自动落盘）
            # 拷进该条工作区，脚本读写均落在副本上
            return copy_into_workspace(raw, run_dir, script_field)
        if value_type == "local":
            raise PreScriptInputError(f"字段 {field} 指向的本地路径不存在: {raw}")
        return write_inline_value(script_field, raw, run_dir)

    # dict/list/标量统一按 inline JSON/文本落地
    return write_inline_value(script_field, raw, run_dir)


def _resolve_hub_file_field(
    script_field: str,
    mapping: Dict[str, Any],
    field: str,
    data: Dict[str, Any],
    run_dir: Path,
) -> Dict[str, Any]:
    """hub_file 通道输入解析（阶段B）：字段值为执行包解包区目录路径

    评测链路 resolve_hub_file_fields 已把数据项中的 file_id 替换为任务内
    镜像目录（uploads/hub_files/{task_id}/{file_id}/），此处直接在该目录上解析：

    - 配 path_pattern → {root, files: [{path, rel_path, size, type}]}
      （match_files_detail 复用共享匹配器 match_files，与预览阶段同语义）；
      匹配 0 文件时 required=true（缺省）抛 PreScriptInputError，该脚本按
      fail 处理走 on_fail(continue/abort)；required=false → files: [] 继续执行
    - 未配 path_pattern → {root, index_path}，索引（build_file_index 产物）
      写运行目录 inputs/{script_field}_index.json，脚本读索引自行遍历
    """
    raw = data.get(field)
    if not (isinstance(raw, str) and os.path.isdir(raw)):
        raise PreScriptInputError(
            f"字段 {field} 的 hub 执行包目录不可用: {raw!r}"
            "（数据项未完成 hub_file 解析或解包区已被清理，请重新导入执行包）"
        )
    # 严格拷贝语义：任务级镜像目录（uploads/hub_files/{task_id}/{file_id}/）
    # 拷进该条工作区，脚本对执行包的读写均落在副本上（同 file_id 多条数据
    # 共享的镜像目录不会被脚本改动污染），副本随本条工作区评完即清
    try:
        root = copy_into_workspace(raw, run_dir, script_field)
    except OSError as e:
        raise PreScriptInputError(f"字段 {script_field} 执行包拷贝进工作区失败: {e}")

    pattern = mapping.get("path_pattern")
    if pattern:
        required = mapping.get("required", True)
        if not isinstance(required, bool):
            raise PreScriptInputError(
                f"input_mapping.{script_field}.required 必须是布尔值: {required!r}"
            )
        try:
            files = match_files_detail(root, str(pattern))
        except HubFileError as e:
            raise PreScriptInputError(f"字段 {script_field} 的 path_pattern 非法: {e}")
        if not files and required:
            raise PreScriptInputError(
                f"字段 {field} 的 hub 执行包按 path_pattern={pattern} 未匹配到任何文件"
            )
        return {"root": root, "files": files}

    index = build_file_index(root)
    index_path = write_inline_value(f"{script_field}_index", index, run_dir)
    return {"root": root, "index_path": index_path}


def write_inline_value(field: str, value: Any, run_dir: Path) -> str:
    """把 inline 字段值写为运行目录下的临时文件，返回绝对路径

    dict/list 写 .json（ensure_ascii=False），其余写 .txt（UTF-8）。
    """
    inputs_dir = Path(run_dir) / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        target = inputs_dir / f"{_safe_name(field)}.json"
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        target = inputs_dir / f"{_safe_name(field)}.txt"
        target.write_text(str(value), encoding="utf-8")
    return str(target)


_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def copy_into_workspace(src: str, run_dir: Path, name: str) -> str:
    """把工作区外的文件/目录拷贝进该条数据工作区，返回工作区内绝对路径

    严格拷贝语义：脚本对副本的读写不触碰源（脚本库/任务级镜像目录/上传
    原件）；目录副本每次解析前重建，保证干净；文件按源后缀落为
    inputs/{name}{ext}。拷贝失败抛 OSError，由调用方按该脚本 fail 处理。
    """
    src_path = Path(src).absolute()
    inputs_dir = Path(run_dir) / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    base = inputs_dir / _safe_name(name)
    if src_path.is_dir():
        if base.exists():
            shutil.rmtree(base)
        shutil.copytree(src_path, base, ignore=_COPY_IGNORE)
        return str(base)
    target = base.with_name(base.name + src_path.suffix)
    shutil.copy2(src_path, target)
    return str(target)


def _stage_script_package(script_dir: Path, run_dir: Path, script_key: str) -> Path:
    """把脚本包拷贝到该条工作区 scripts/{script_key}/ 下，返回副本目录

    严格拷贝语义：脚本从副本执行——脚本对自身包目录（Path(__file__) 附近）
    的写操作只落在本条工作区（评完即清），不污染脚本库与其它数据条；
    副本每次执行前重建。copytree 保留包内结构，入口相对路径在副本中不变。
    """
    target = Path(run_dir) / "scripts" / _safe_name(script_key)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(script_dir, target, ignore=_COPY_IGNORE)
    return target


def _extract_session_info(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从数据项提取会话摘要信息（agent 评测类型；无会话内容时为 None）"""
    if not isinstance(data, dict):
        return None
    if not (data.get("session_summary") or data.get("session_messages")):
        return None
    return {
        "session_summary": data.get("session_summary"),
        "session_stats": data.get("session_stats") or {},
        "session_source": data.get("source") or data.get("session_source") or "",
    }


def _safe_name(field: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(field)) or "field"


def _result(
    script_id: str,
    script_name: str,
    status: str,
    finish_reason: str,
    started_at: float = 0.0,
    **overrides: Any,
) -> Dict[str, Any]:
    """组装 pre_script_results 标准条目（started_at 为计 时起点 time.time()）"""
    result: Dict[str, Any] = {
        "script_id": script_id,
        "script_name": script_name,
        "status": status,
        "output": None,
        "files": {},
        "summary": "",
        "finish_reason": finish_reason,
        "error": None,
        "duration_ms": int((time.time() - started_at) * 1000) if started_at else 0,
    }
    result.update(overrides)
    return result
