"""终态校验器执行器（M2 QE2-1，确定性终态校验优先）

与「前处理脚本」（evaluation/pre-judge，模型调用之后、裁判之前做数据准备）
分离的新概念「校验器」：评测流水线在模型调用/被测 agent 执行**之后**运行
（executor._evaluate_single 步骤1.6），对终态做确定性断言，输出
``{status: pass|fail|error, evidence: {...}}``，行级结果落
evaluation_results.verification_result，聚合出 verification_pass_rate
（接入门禁 metric，见 result_aggregator / gate_service）。

四种内置类型（plans/2026-10-22-Agent质量工程平台开发方案.md §M2 2.1）：

1. ``script``——复用 pre_judge_scripts 沙箱通道（SCRIPT_EXEC_MODE=container
   时经 SandboxDriver；local 模式子进程）与脚本标准输出契约，stdin 增加
   ``kind=verifier`` 标记，输入含模型输出（payload.model_output，超长会话
   为工作区文件路径）与会话 trace 摘要（session）。脚本输出 status
   ok/skip/fail 映射校验器 pass/pass/fail（finish_reason=error/timeout 的
   fail 是基础设施故障 → error，与断言失败区分）；
2. ``db_assert``——内置脚本原生化：不再经子进程，进程内直接调
   app.scripts_builtin.db_assert.run()（SQL 只读白名单/max_rows/只读事务
   约束全部保留，单一事实源）；只读 DSN 仍取 Settings.DB_ASSERT_READONLY_DSN
   且只注入本通道（沿用 pre_judge_runner._builtin_db_dsn）；
3. ``file_assert``——对该条数据工作区（uploads/script_runs/{task_id}/
   {data_id}/，含前处理脚本 stage 的执行包副本）文件做 exists / regex /
   jsonpath 断言；路径只允许工作区内（ensure_within，越界按 error）；
   jsonpath 为极简点路径子集（a.b[0].c，见 _resolve_json_path）；
4. ``http_assert``——对回调端点/服务做状态码/响应体断言，出网走平台
   egress 守卫：默认仅公网目标（复用 webhook_url_guard 的解析后校验，
   DNS 重绑定/内网地址拒绝），内网回调端点需显式声明
   ``config.allow_private: true``（任务配置可审计）。

任务配置（task.verifiers，JSON 列）::

    [
      {"type": "script",      "name": "订单落库校验", "on_fail": "continue",
       "config": {"script_id": "pjs_xxx", "params": {}, "network": false}},
      {"type": "db_assert",   "config": {"assertions": [
          {"name": "订单数", "sql": "SELECT count(*) FROM orders", "expected": {"scalar": 3}}]}},
      {"type": "file_assert", "config": {"assertions": [
          {"name": "结果文件", "path": "outputs/result.json", "mode": "jsonpath",
           "jsonpath": "status", "expected": "done"}]}},
      {"type": "http_assert", "config": {
          "url": "https://sut.example.com/api/orders/1", "method": "GET",
          "expect_status": 200, "body_regex": "paid"}}
    ]

``on_fail``：continue（默认，校验失败记录证据继续）/ abort（该条数据标记
执行失败，不进裁判）。行级整体 status：任一 fail → fail；否则任一 error →
error；否则 pass。

安全口径：校验器配置经 evaluation_task_service.validate_verifiers 白名单
校验（类型/script 存在性/断言结构/http URL 形态）；试跑（debug）与脚本
试跑同口径仅超管（POST /evaluation/tasks/verifiers/debug）。
"""
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.utils.path_safety import PathEscapeError, ensure_within

logger = logging.getLogger(__name__)

# 内置校验器类型白名单（gate 之外的确定性终态校验通道）
VERIFIER_TYPES = ("script", "db_assert", "file_assert", "http_assert")

# file_assert 读取文件的大小上限（字节）：超限按 error（防大文件拖垮 worker）
FILE_ASSERT_READ_LIMIT = 2 * 1024 * 1024

# http_assert 响应体进入 evidence 的截断长度（字符）
HTTP_BODY_SNIPPET_LIMIT = 500

# 行级校验整体状态
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"

__all__ = [
    "VERIFIER_TYPES",
    "run_verifiers",
    "run_single_verifier",
    "build_debug_workspace",
    "validate_verifier_shape",
]


def run_verifiers(
    task,
    payload: Optional[Dict[str, Any]] = None,
    model_output: Any = None,
) -> Optional[Dict[str, Any]]:
    """执行任务配置的全部终态校验器（串行，M2 QE2-1 步骤1.6）

    Args:
        task: 评测任务对象（读取 verifiers / id）
        payload: 评测请求 payload（_dataset_item 供会话摘要与 data_id 派生；
            payload.model_output 优先——agent 超长会话为工作区文件路径）
        model_output: 模型输出兜底值（payload 未携带时使用）

    Returns:
        {status: pass|fail|error, results: [逐校验器条目], passed, failed,
         errored, total, aborted, error}；任务未配置校验器返回 None
        （result 不写 verification_result 键，兼容旧结果结构）。
    """
    verifiers = getattr(task, "verifiers", None) or []
    if not isinstance(verifiers, list) or not verifiers:
        return None

    payload = payload or {}
    dataset_item = payload.get("_dataset_item") or {}
    if not isinstance(dataset_item, dict):
        dataset_item = {}
    data = dataset_item.get("data") if isinstance(dataset_item.get("data"), dict) else {}
    data_id = dataset_item.get("id") or payload.get("idx") or "item"
    task_id = str(getattr(task, "id", "") or "unknown")
    # 模型输出：payload.model_output 优先（agent 超长会话为工作区文件路径）
    verifier_model_output = payload.get("model_output", model_output)

    from app.services.pre_judge_runner import build_run_dir

    run_dir = build_run_dir(task_id, str(data_id))

    results: List[Dict[str, Any]] = []
    aborted = False
    abort_error: Optional[str] = None
    for index, verifier in enumerate(verifiers):
        if not isinstance(verifier, dict):
            logger.warning(f"[Verifiers] 忽略非法校验器配置项: {verifier!r}")
            continue
        entry = run_single_verifier(
            verifier, task_id=task_id, data_id=str(data_id), run_dir=run_dir,
            model_output=verifier_model_output, data=data, payload=payload,
        )
        results.append(entry)

        on_fail = str(verifier.get("on_fail") or "continue").lower()
        if on_fail == "abort" and entry["status"] in (STATUS_FAIL, STATUS_ERROR):
            aborted = True
            name = entry.get("name") or entry.get("type")
            abort_error = (
                f"终态校验器 {name} 未通过（{entry['status']}）："
                f"{entry.get('error') or '断言失败'}"
            )
            logger.warning(f"[Verifiers] 校验器 {name} 失败且 on_fail=abort，终止编排: {abort_error}")
            break

    passed = sum(1 for r in results if r["status"] == STATUS_PASS)
    failed = sum(1 for r in results if r["status"] == STATUS_FAIL)
    errored = sum(1 for r in results if r["status"] == STATUS_ERROR)

    if failed:
        overall = STATUS_FAIL
    elif errored:
        overall = STATUS_ERROR
    else:
        overall = STATUS_PASS

    return {
        "status": overall,
        "results": results,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "total": len(results),
        "aborted": aborted,
        "error": abort_error,
    }


def run_single_verifier(
    verifier: Dict[str, Any],
    *,
    task_id: str,
    data_id: str,
    run_dir: Path,
    model_output: Any = None,
    data: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """执行单个校验器，返回标准条目 {type, name, status, evidence, duration_ms, error}

    任何执行通道异常都收敛为 status=error 条目（绝不抛出，评测主流程不受影响）。
    """
    v_type = str(verifier.get("type") or "").strip()
    name = str(verifier.get("name") or "").strip() or v_type
    config = verifier.get("config") if isinstance(verifier.get("config"), dict) else {}
    started = time.time()

    dispatch = {
        "script": _run_script_verifier,
        "db_assert": _run_db_assert_verifier,
        "file_assert": _run_file_assert_verifier,
        "http_assert": _run_http_assert_verifier,
    }
    handler = dispatch.get(v_type)
    if handler is None:
        return _entry(v_type, name, STATUS_ERROR, started,
                      error=f"未知校验器类型: {v_type!r}（可用: {', '.join(VERIFIER_TYPES)}）")
    try:
        return handler(name, config, task_id, data_id, run_dir, model_output,
                       data or {}, payload or {})
    except Exception as e:  # noqa: BLE001 - 校验边界：异常转 error 条目
        logger.exception(f"[Verifiers] 校验器 {name}（{v_type}）执行异常")
        return _entry(v_type, name, STATUS_ERROR, started, error=f"校验器执行异常: {e}")


def build_debug_workspace(prefix: str = "verifier") -> Path:
    """试跑（debug）工作区：uploads/script_runs/debug/{prefix}_{ts}_{rand}

    与脚本试跑同目录约定（pre_judge_script_api.debug_script 的
    uploads/script_runs/debug/... 形态），便于统一排障；outputs/ 就绪，
    校验器产物落盘语义与生产执行一致。
    """
    import uuid as _uuid

    from app.services.pre_judge_runner import build_run_dir

    stamp = time.strftime("%Y%m%d%H%M%S")
    return build_run_dir(
        "debug", f"{_safe_name(prefix)}_{stamp}_{_uuid.uuid4().hex[:6]}")


# ==================== script：复用 pre_judge 沙箱通道 ====================

def _run_script_verifier(
    name: str,
    config: Dict[str, Any],
    task_id: str,
    data_id: str,
    run_dir: Path,
    model_output: Any,
    data: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """script 校验器：复用 execute_script 沙箱通道与标准输出契约，stdin 加 kind=verifier"""
    from app.services.pre_judge_runner import _load_script, execute_script

    script_id = str(config.get("script_id") or "").strip()
    if not script_id:
        return _entry("script", name, STATUS_ERROR, error="缺少 config.script_id（脚本库脚本 id）")

    script = _load_script(script_id)
    if script is None:
        return _entry("script", name, STATUS_ERROR, error=f"脚本不存在: {script_id}")

    from app.services.pre_judge_runner import _extract_session_info

    stdin: Dict[str, Any] = {
        "task_id": task_id,
        "data_id": data_id,
        # M2 新增标记：同一脚本契约下区分调用方（校验器 vs 前处理）
        "kind": "verifier",
        "fields": {},
        "inputs": {},
        "model_output": model_output,
        "session": _extract_session_info(data),
        "params": config.get("params") or {},
        "upstream": {},
        "_run_dir": str(run_dir),
    }
    result = execute_script(
        script, stdin,
        timeout_seconds=config.get("timeout_seconds"),
        network=bool(config.get("network")),
    )

    # 状态映射：ok→pass；skip→pass（脚本自判跳过，不算失败）；fail 按
    # finish_reason 分流——error/timeout 是基础设施故障（脚本不存在/超时/
    # 退出码非 0），与脚本自己输出的断言失败（completed/assertion_failed 等）区分
    raw_status = result.get("status")
    finish_reason = result.get("finish_reason") or ""
    if raw_status == "ok":
        status = STATUS_PASS
    elif raw_status == "skip":
        status = STATUS_PASS
    elif finish_reason in ("error", "timeout"):
        status = STATUS_ERROR
    else:
        status = STATUS_FAIL

    evidence = {
        "script_id": result.get("script_id") or script.get("script_id"),
        "script_name": result.get("script_name"),
        "kind": "verifier",
        "status_raw": raw_status,
        "finish_reason": result.get("finish_reason"),
        "summary": result.get("summary") or "",
        "output": result.get("output"),
        "files": result.get("files") or {},
        "static_check": result.get("static_check") or [],
    }
    return _entry("script", name, status, started=None,
                  evidence=evidence, error=result.get("error"),
                  duration_ms=result.get("duration_ms"))


# ==================== db_assert：内置脚本原生化 ====================

def _run_db_assert_verifier(
    name: str,
    config: Dict[str, Any],
    task_id: str,
    data_id: str,
    run_dir: Path,
    model_output: Any,
    data: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """db_assert 原生类型：进程内直调 scripts_builtin.db_assert.run()

    只读约束（SQL 白名单/只读事务/max_rows）与断言语义全部复用内置脚本
    实现（单一事实源）；只读 DSN 复用 pre_judge_runner._builtin_db_dsn
    （Settings.DB_ASSERT_READONLY_DSN，未配置时明确报错，不回退平台库）。
    """
    from app.scripts_builtin import db_assert as db_assert_module
    from app.services.pre_judge_runner import _builtin_db_dsn, write_inline_value

    assertions = config.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        return _entry("db_assert", name, STATUS_ERROR,
                      error="缺少 config.assertions（[{name, sql, expected}] 断言清单）")

    dsn = _builtin_db_dsn()
    if not dsn:
        return _entry(
            "db_assert", name, STATUS_ERROR,
            error="平台未配置 DB_ASSERT_READONLY_DSN，db_assert 校验器不可用",
        )

    # 断言清单按脚本契约落盘（fields.assertions 为文件路径），复用
    # write_inline_value 的 inputs/ 落盘语义；产物写 _run_dir/outputs
    assertions_path = write_inline_value("db_assert_assertions", {"assertions": assertions}, run_dir)
    stdin = {
        "task_id": task_id,
        "data_id": data_id,
        "kind": "verifier",
        "fields": {"assertions": assertions_path},
        "db_dsn": dsn,
        "params": {
            "fail_fast": bool(config.get("fail_fast", False)),
            "max_rows": config.get("max_rows"),
        },
        "_run_dir": str(run_dir),
    }
    output = db_assert_module.run(stdin)

    raw_status = output.get("status")
    finish_reason = str(output.get("finish_reason") or "")
    if raw_status == "ok":
        status = STATUS_PASS
    elif finish_reason == "error":
        # 缺输入/DSN/SQL 执行异常等通道故障 → error（与断言失败区分）
        status = STATUS_ERROR
    else:
        status = STATUS_FAIL
    evidence = {
        "kind": "verifier",
        "status_raw": raw_status,
        "finish_reason": output.get("finish_reason"),
        "summary": output.get("summary") or "",
        "output": output.get("output"),
        "files": output.get("files") or {},
    }
    return _entry("db_assert", name, status, started=None,
                  evidence=evidence, error=output.get("error"))


# ==================== file_assert：工作区文件断言 ====================

def _run_file_assert_verifier(
    name: str,
    config: Dict[str, Any],
    task_id: str,
    data_id: str,
    run_dir: Path,
    model_output: Any,
    data: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """file_assert：对该条数据工作区文件做 exists/regex/jsonpath 断言

    路径解析：相对路径以 run_dir 为根；绝对路径也必须落在 run_dir 内
    （ensure_within，.. 穿越/越界按该条 error——工作区含前处理脚本 stage
    的执行包副本与 inline 输入，评完即清）。jsonpath 为极简点路径子集
    （a.b[0].c），expected 缺省只断言可解析。
    """
    assertions = config.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        return _entry("file_assert", name, STATUS_ERROR,
                      error="缺少 config.assertions（[{name, path, mode, pattern|jsonpath, expected}] 断言清单）")

    checks: List[Dict[str, Any]] = []
    any_fail = False
    any_error = False
    for i, assertion in enumerate(assertions):
        check = _file_assert_one(assertion, i, run_dir)
        checks.append(check)
        if check["status"] == STATUS_FAIL:
            any_fail = True
        elif check["status"] == STATUS_ERROR:
            any_error = True

    passed = sum(1 for c in checks if c["status"] == STATUS_PASS)
    failed = len(checks) - passed
    status = STATUS_FAIL if any_fail else (STATUS_ERROR if any_error else STATUS_PASS)
    evidence = {
        "kind": "verifier",
        "total": len(checks),
        "passed": passed,
        "failed": failed,
        "checks": checks,
    }
    return _entry("file_assert", name, status, evidence=evidence,
                  error=f"{failed} 条文件断言未通过" if failed else None)


def _file_assert_one(assertion: Any, index: int, run_dir: Path) -> Dict[str, Any]:
    """执行单条文件断言，返回 {name, path, mode, status, expected, actual, error}"""
    check: Dict[str, Any] = {
        "name": str((assertion or {}).get("name") or f"file_assert_{index}") if isinstance(assertion, dict) else f"file_assert_{index}",
        "path": str(assertion.get("path") or "") if isinstance(assertion, dict) else "",
        "mode": "exists",
        "status": STATUS_ERROR,
        "expected": None,
        "actual": None,
        "error": None,
    }
    if not isinstance(assertion, dict):
        check["error"] = f"断言第 {index} 项必须是对象"
        return check

    mode = str(assertion.get("mode") or "exists").strip().lower()
    check["mode"] = mode
    raw_path = str(assertion.get("path") or "").strip()
    if not raw_path:
        check["error"] = "缺少 path（工作区相对路径，如 outputs/result.json）"
        return check
    negate = bool(assertion.get("negate", False))
    check["negate"] = negate

    # 路径安全：一律锚定工作区（相对/绝对都不得逃出 run_dir）
    try:
        target = Path(ensure_within(str(run_dir), raw_path))
    except PathEscapeError as e:
        check["error"] = f"路径越界（仅允许工作区内文件）: {e}"
        return check
    check["path"] = str(target)

    if not target.is_file():
        # exists 模式允许 negate（断言文件不存在）；其余模式缺文件即失败
        if negate:
            check["status"] = STATUS_PASS
            check["actual"] = "<不存在>"
            return check
        check["status"] = STATUS_FAIL
        check["error"] = "文件不存在"
        return check
    if mode == "exists":
        check["status"] = STATUS_FAIL if negate else STATUS_PASS
        check["actual"] = "<存在>"
        return check

    content = _read_workspace_file(target)
    if content is None:
        check["error"] = f"文件读取失败或超过 {FILE_ASSERT_READ_LIMIT} 字节上限"
        return check

    if mode == "regex":
        pattern = str(assertion.get("pattern") or "")
        check["expected"] = pattern
        try:
            matched = re.search(pattern, content) is not None
        except re.error as e:
            check["error"] = f"正则非法: {e}"
            return check
        passed = (not matched) if negate else matched
        check["status"] = STATUS_PASS if passed else STATUS_FAIL
        check["actual"] = f"<{'命中' if matched else '未命中'}>"
        if not passed:
            check["error"] = "正则未命中" if matched else "正则命中（negate）"
        return check

    if mode == "jsonpath":
        jsonpath = str(assertion.get("jsonpath") or "")
        expected = assertion.get("expected")
        check["expected"] = expected
        try:
            document = json.loads(content)
        except json.JSONDecodeError as e:
            check["error"] = f"文件不是合法 JSON: {e}"
            return check
        resolved, err, err_kind = _resolve_json_path(document, jsonpath)
        if err is not None:
            if err_kind == "syntax":
                # 语法级错误（不支持的表达式/空路径非法）→ 配置错误，不是断言失败
                check["error"] = err
                return check
            # 路径在文档中不可解析（键缺失/下标越界）→ 断言失败（终态没写出期望字段）
            if negate:
                check["status"] = STATUS_PASS
                check["actual"] = "<不可解析>"
                return check
            check["status"] = STATUS_FAIL
            check["error"] = err
            return check
        check["actual"] = _snippet(resolved)
        if expected is None:
            passed = not negate  # 只断言可解析（negate 时断言不可解析）
        else:
            equal = _tolerant_equal(resolved, expected)
            passed = (not equal) if negate else equal
        check["status"] = STATUS_PASS if passed else STATUS_FAIL
        if not passed:
            check["error"] = "值不匹配" if (expected is not None) else "路径已解析（negate）"
        return check

    check["error"] = f"不支持的断言模式: {mode!r}（可用: exists/regex/jsonpath）"
    return check


def _resolve_json_path(document: Any, jsonpath: str) -> Tuple[Optional[Any], Optional[str], str]:
    """极简 JSONPath 子集：点路径 + 数组下标（a.b[0].c），不支持过滤器/通配

    Returns:
        (解析值, 错误, 错误类别)：解析成功错误为 None；错误类别：
        ``missing``（键缺失/下标越界——按断言失败处理）或 ``syntax``
        （不支持的表达式——按配置错误处理）。
    """
    path = (jsonpath or "").strip().lstrip("$.")
    if not path:
        return document, None, ""
    token_re = re.compile(r"\.?\s*([^.\[\]\s]+)\s*(?:\[(\d+)\])?")
    node = document
    consumed = 0
    normalized_len = len(path.replace(" ", ""))
    for match in token_re.finditer(path):
        key, index = match.group(1), match.group(2)
        consumed += len(match.group(0))
        if not isinstance(node, dict) or key not in node:
            return None, f"路径 {jsonpath!r} 在对象键 {key!r} 处不可解析", "missing"
        node = node[key]
        if index is not None:
            idx = int(index)
            if not isinstance(node, list) or idx >= len(node) or idx < -len(node):
                return None, f"路径 {jsonpath!r} 数组下标 [{idx}] 越界", "missing"
            node = node[idx]
    if consumed < normalized_len:
        return None, (
            f"路径 {jsonpath!r} 含不支持的语法（仅支持 a.b[0].c 点路径子集）"), "syntax"
    return node, None, ""


def _read_workspace_file(target: Path) -> Optional[str]:
    """读取工作区文件文本（UTF-8），超限/读失败返回 None"""
    try:
        if target.stat().st_size > FILE_ASSERT_READ_LIMIT:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ==================== http_assert：egress 守卫下的回调断言 ====================

def _run_http_assert_verifier(
    name: str,
    config: Dict[str, Any],
    task_id: str,
    data_id: str,
    run_dir: Path,
    model_output: Any,
    data: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """http_assert：对回调端点/服务做状态与响应体断言

    出网走 egress 守卫（复用 webhook_url_guard 的解析后校验）：默认仅公网
    目标（环回/私网/链路本地/DNS 重绑定一律拒绝）。放行内网目标需两重声明
    （SSRF 控制面与 M1 webhook 同口径，控制权在部署管理员而非任务创建者）：
    1. 服务端管理员置 ``HTTP_ASSERT_ALLOW_PRIVATE_TARGETS=true``（默认关）；
    2. verifier config 显式声明 allow_private=true（任务配置可审计）。
    请求不跟随重定向（allow_redirects=False）：守卫只校验初始 URL，跟随
    3xx 可被公网目标 302 跳内网/环回绕过——需断言重定向行为时用
    expect_status 直接断言 3xx 状态码。
    """
    import requests

    from app.core.config import get_settings
    from app.services.webhook_url_guard import validate_webhook_url

    url = str(config.get("url") or "").strip()
    if not url:
        return _entry("http_assert", name, STATUS_ERROR, error="缺少 config.url")

    # 内网放行双闸：任务级 allow_private 只是「任务侧意向」，真正生效还需
    # 管理员全局开关（默认关）——否则任意任务创建者即可声明后直连内网
    allow_private_requested = bool(config.get("allow_private", False))
    if allow_private_requested and not get_settings().HTTP_ASSERT_ALLOW_PRIVATE_TARGETS:
        return _entry(
            "http_assert", name, STATUS_ERROR,
            error="allow_private 需服务端管理员启用 HTTP_ASSERT_ALLOW_PRIVATE_TARGETS "
                  "后生效（默认拒绝内网目标）",
        )
    allow_private = allow_private_requested
    try:
        # 与 webhook 投递同一 SSRF 守卫（解析后校验，防 DNS 重绑定绕过）
        validate_webhook_url(url, allow_private=allow_private)
    except ValueError as e:
        return _entry("http_assert", name, STATUS_ERROR,
                      error=f"URL 被 egress 守卫拒绝: {e}")

    method = str(config.get("method") or "GET").strip().upper() or "GET"
    if method not in ("GET", "POST", "PUT", "PATCH", "HEAD", "DELETE"):
        return _entry("http_assert", name, STATUS_ERROR, error=f"不支持的 method: {method}")
    timeout = float(config.get("timeout_seconds") or 10)
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}

    checks: List[Dict[str, Any]] = []
    started = time.time()
    try:
        body = config.get("json") if config.get("json") is not None else config.get("body")
        response = requests.request(
            method, url, headers=headers,
            json=config.get("json") if config.get("json") is not None else None,
            data=body if config.get("json") is None else None,
            timeout=timeout,
            # SSRF 硬化：egress 守卫只校验初始 URL，跟随重定向可被「公网目标
            # 302 跳内网/环回」绕过并把内网响应体写进 evidence——禁跟随
            # （与 M1 webhook 投递 httpx 默认不跟随同口径）
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return _entry("http_assert", name, STATUS_ERROR,
                      error=f"请求失败（{method} {url}）: {e}")

    elapsed_ms = int((time.time() - started) * 1000)
    body_text = response.text or ""

    # 状态码断言（缺省 200；支持单值或数组）
    expect_status = config.get("expect_status", 200)
    allowed_statuses = expect_status if isinstance(expect_status, list) else [expect_status]
    try:
        allowed_statuses = [int(s) for s in allowed_statuses]
    except (TypeError, ValueError):
        return _entry("http_assert", name, STATUS_ERROR,
                      error=f"expect_status 配置非法: {expect_status!r}")
    status_passed = response.status_code in allowed_statuses
    checks.append({
        "name": "status", "expected": allowed_statuses,
        "actual": response.status_code, "passed": status_passed,
        "error": None if status_passed else f"状态码期望 {allowed_statuses}，实际 {response.status_code}",
    })

    # 响应体正则断言（可选）
    body_regex = config.get("body_regex")
    if body_regex:
        try:
            matched = re.search(str(body_regex), body_text) is not None
        except re.error as e:
            return _entry("http_assert", name, STATUS_ERROR, error=f"body_regex 非法: {e}")
        checks.append({
            "name": "body_regex", "expected": str(body_regex),
            "actual": f"<{'命中' if matched else '未命中'}>", "passed": matched,
            "error": None if matched else "响应体正则未命中",
        })

    # 响应体 JSONPath 断言（可选）
    jsonpath = config.get("jsonpath")
    if jsonpath:
        expected = config.get("expected")
        try:
            document = response.json()
        except ValueError as e:
            return _entry("http_assert", name, STATUS_ERROR,
                          error=f"响应体不是合法 JSON（jsonpath 断言不可用）: {e}")
        resolved, err, _err_kind = _resolve_json_path(document, str(jsonpath))
        if err is not None:
            checks.append({"name": "jsonpath", "expected": expected,
                           "actual": None, "passed": False, "error": err})
        else:
            passed = _tolerant_equal(resolved, expected)
            checks.append({
                "name": "jsonpath", "expected": expected,
                "actual": _snippet(resolved), "passed": passed,
                "error": None if passed else "响应字段值不匹配",
            })

    any_fail = any(not c["passed"] for c in checks)
    evidence = {
        "kind": "verifier",
        "url": url,
        "method": method,
        "status_code": response.status_code,
        "elapsed_ms": elapsed_ms,
        "body_snippet": body_text[:HTTP_BODY_SNIPPET_LIMIT],
        "checks": checks,
    }
    return _entry("http_assert", name, STATUS_FAIL if any_fail else STATUS_PASS,
                  evidence=evidence,
                  error="回调断言未通过" if any_fail else None)


# ==================== 配置形态校验（evaluation_task_service 调用） ====================

def validate_verifier_shape(verifiers: Any) -> None:
    """校验器配置白名单校验（create/update 前置；非法抛 ValueError → API 400）

    只做形态与静态可校验项：类型白名单、on_fail 白名单、script_id 必填、
    断言清单非空、http URL 形态；脚本存在性/active 由服务层查库校验
    （见 evaluation_task_service._validate_verifiers）。
    """
    if verifiers is None:
        return
    if not isinstance(verifiers, list):
        raise ValueError("verifiers 必须是校验器配置数组")
    for i, verifier in enumerate(verifiers):
        if not isinstance(verifier, dict):
            raise ValueError(f"校验器第 {i} 项必须是对象")
        v_type = str(verifier.get("type") or "").strip()
        if v_type not in VERIFIER_TYPES:
            raise ValueError(
                f"校验器第 {i} 项类型非法: {v_type!r}（可用: {', '.join(VERIFIER_TYPES)}）")
        on_fail = str(verifier.get("on_fail") or "continue").lower()
        if on_fail not in ("continue", "abort"):
            raise ValueError(f"校验器第 {i} 项 on_fail 非法: {on_fail!r}（可用: continue/abort）")
        config = verifier.get("config")
        if config is not None and not isinstance(config, dict):
            raise ValueError(f"校验器第 {i} 项 config 必须是对象")

        if v_type == "script":
            if not str((config or {}).get("script_id") or "").strip():
                raise ValueError(f"校验器第 {i} 项（script）缺少 config.script_id")
        elif v_type in ("db_assert", "file_assert"):
            assertions = (config or {}).get("assertions")
            if not isinstance(assertions, list) or not assertions:
                raise ValueError(
                    f"校验器第 {i} 项（{v_type}）缺少 config.assertions 断言清单")
            for j, assertion in enumerate(assertions):
                if not isinstance(assertion, dict):
                    raise ValueError(f"校验器第 {i} 项断言第 {j} 条必须是对象")
                if v_type == "db_assert":
                    if not str(assertion.get("sql") or "").strip():
                        raise ValueError(f"校验器第 {i} 项断言第 {j} 条缺少 sql")
                    if not isinstance(assertion.get("expected"), dict) or not assertion.get("expected"):
                        raise ValueError(f"校验器第 {i} 项断言第 {j} 条缺少 expected 断言对象")
                else:
                    if not str(assertion.get("path") or "").strip():
                        raise ValueError(f"校验器第 {i} 项断言第 {j} 条缺少 path")
                    mode = str(assertion.get("mode") or "exists").lower()
                    if mode not in ("exists", "regex", "jsonpath"):
                        raise ValueError(
                            f"校验器第 {i} 项断言第 {j} 条 mode 非法: {mode!r}（可用: exists/regex/jsonpath）")
                    if mode == "regex" and not str(assertion.get("pattern") or "").strip():
                        raise ValueError(f"校验器第 {i} 项断言第 {j} 条（regex）缺少 pattern")
                    if mode == "jsonpath" and not str(assertion.get("jsonpath") or "").strip():
                        raise ValueError(f"校验器第 {i} 项断言第 {j} 条（jsonpath）缺少 jsonpath")
        elif v_type == "http_assert":
            url = str((config or {}).get("url") or "").strip()
            if not url:
                raise ValueError(f"校验器第 {i} 项（http_assert）缺少 config.url")
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(f"校验器第 {i} 项（http_assert）url 仅允许 http/https")


# ==================== 工具 ====================

def _tolerant_equal(actual: Any, expected: Any) -> bool:
    """宽容相等：数值跨 int/float 比较，其余按 JSON 序列化字符串比较"""
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    if isinstance(actual, str) and isinstance(expected, (int, float)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return _snippet(actual) == _snippet(expected)


def _snippet(value: Any, limit: int = 200) -> Any:
    """evidence 展示截断：基本类型原样，长字符串截断，其余转 JSON 字符串"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_name(field: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(field)) or "field"


def _entry(
    v_type: str,
    name: str,
    status: str,
    started: Optional[float] = None,
    evidence: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """组装校验器标准条目 {type, name, status, evidence, duration_ms, error}"""
    return {
        "type": v_type,
        "name": name,
        "status": status,
        "evidence": evidence or {},
        "duration_ms": duration_ms if duration_ms is not None
        else int((time.time() - started) * 1000) if started else 0,
        "error": error,
    }
