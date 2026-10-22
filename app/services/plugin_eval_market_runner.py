"""市场版 plugin-eval 运行器：评测期以子进程执行市场克隆里的 plugin-eval

为什么必须"实时加载"：评测动作本身（plugin-eval）也在市场里演进，ai-evo 里的移植
副本会随之漂移（实测同一 skill 静态层 0.5516 vs 0.5164、插件级组装口径不同）。
本模块把市场克隆里的 plugin-eval 当作**外部动作**执行，评测器版本随市场懒加载一起更新。

为什么走子进程：
- 市场代码是外部且随时更新的代码，进程内 import 会以"先加载者为准"污染 worker 的
  模块与依赖解析，它的异常也不该拖垮 worker；
- 子进程把「评测动作」收敛成一条 JSON 协议（argv 进、stdout JSON 出），可测可审计。

环境注入：照 ``pre_judge_runner`` 的白名单口径透传系统变量，剥离平台敏感变量
（DATABASE_URL/REDIS_URL 等），另注入 Claude Code 的 SDK env（judge 层经它走平台网关）
与 ``PYTHONPATH=<plugin-eval>/src``。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# 评测器在市场上的默认位置（anc-marketplace 里 plugin-eval 是普通插件条目）
DEFAULT_EVALUATOR_SOURCE = "./public/plugin-eval"

# 子进程环境白名单（同 pre_judge_runner.SCRIPT_ENV_WHITELIST 口径：只透传系统必需项）
ENV_WHITELIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "TMPDIR",
)

# 超时：quick 纯静态分析（实测秒级）；其余深度含 judge 的模型调用，给足余量
QUICK_TIMEOUT_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 1800.0

# trace 评测（一份报告同时含综合分与真实数据分）：单条 26MB 线程的执行阶段
# 就有 91 个关键步骤实例逐个抽取 + 逐检查判定，实测 >60 分钟；再叠加生成阶段与
# 逐 skill 的 LLM 评审（standard 深度），故给 120 分钟窗口
TRACE_TIMEOUT_SECONDS = 7200.0

# 历史评测的阶段与范围（产品口径：执行 + 生成两阶段、关键步骤）
# 与本地报告一致：phase=all 时一次评测同时出「历史执行可信度」与「历史生成可信度」两节
TRACE_PHASE = "all"
TRACE_SCOPE = "key"

# trace 口径的静态深度：standard = 静态层 + 逐 skill 的 4 项 rubric LLM 评审（与本地报告同口径）；
# 离线判定不调模型，故退到 quick（只跑静态层，保持「零模型调用」的承诺）
TRACE_DEPTH = "standard"
TRACE_OFFLINE_DEPTH = "quick"

# 历史评测判定方式：model=逐关键步骤抽取 + 逐检查项模型判定（准但慢，实测单行 26MB
# 线程 >1 小时）；offline=--history-no-llm，只跑确定性结构检查（单行 ~80 秒，得分与
# 判定有效率保留但语义检查项未测，覆盖率更低）
TRACE_MODES = ("model", "offline")
DEFAULT_TRACE_MODE = "model"

# 模型调用并发（--concurrency，作用于**一行内部**；行与行仍串行）。
# 上限由 plugin-eval 自己的 EvalConfig 决定：concurrency = Field(ge=1, le=20)，
# 传超过 20 会被 pydantic 拒掉、CLI 非零退出、整行判失败，故这里一律夹到 [1, 20]。
# 每 1 路并发就是一个模型调用（claude-agent-sdk 会拉起一个 claude 子进程），
# 调高要同时看网关限流与部署机内存。
MAX_CONCURRENCY = 20
DEFAULT_CONCURRENCY = 20
CONCURRENCY_ENV = "PLUGIN_EVAL_CONCURRENCY"

ENTRY_SCRIPT = Path(__file__).resolve().with_name("plugin_eval_market_entry.py")


class MarketEvalError(Exception):
    """评测动作（市场版 plugin-eval）不可用或执行失败"""


def _concurrency(override: Optional[int] = None) -> int:
    """生效并发：显式入参 > PLUGIN_EVAL_CONCURRENCY > 默认 20，统一夹到 [1, MAX_CONCURRENCY]"""
    if override is not None:
        return max(1, min(int(override), MAX_CONCURRENCY))

    raw = (os.environ.get(CONCURRENCY_ENV) or "").strip()
    if not raw:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"[插件评测] {CONCURRENCY_ENV} 非法，回落默认 {DEFAULT_CONCURRENCY}: {raw!r}")
        return DEFAULT_CONCURRENCY
    if value != max(1, min(value, MAX_CONCURRENCY)):
        logger.warning(
            f"[插件评测] {CONCURRENCY_ENV}={value} 超出 plugin-eval 允许范围 "
            f"[1, {MAX_CONCURRENCY}]，已夹到 {max(1, min(value, MAX_CONCURRENCY))}"
        )
    return max(1, min(value, MAX_CONCURRENCY))


def _timeout_for(depth: str) -> float:
    raw = (os.environ.get("PLUGIN_EVAL_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning(f"[插件评测] PLUGIN_EVAL_TIMEOUT_SECONDS 非法，回落默认: {raw!r}")
    return QUICK_TIMEOUT_SECONDS if depth == "quick" else DEFAULT_TIMEOUT_SECONDS


def _trace_timeout() -> float:
    """历史评测超时：PLUGIN_EVAL_TRACE_TIMEOUT_SECONDS 覆盖，非法值回落默认"""
    raw = (os.environ.get("PLUGIN_EVAL_TRACE_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning(f"[插件评测] PLUGIN_EVAL_TRACE_TIMEOUT_SECONDS 非法，回落默认: {raw!r}")
    return TRACE_TIMEOUT_SECONDS


def read_evaluator_version(package_root: Path) -> str:
    """从 plugin-eval 的 pyproject.toml 读版本号（读不到返回空串）"""
    import tomllib

    try:
        with (package_root / "pyproject.toml").open("rb") as fh:
            return str((tomllib.load(fh).get("project") or {}).get("version") or "")
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"[插件评测] 读取 plugin-eval 版本失败: {e}")
        return ""


def resolve_evaluator(market_id: Optional[int] = None) -> Dict[str, Any]:
    """定位评测动作：市场 + source → 已懒加载到最新的 plugin-eval 目录与版本锚点

    market_id 为 None（manual/dataset 来源没有市场）时取第一个已同步成功的市场。
    评测器与被测插件共用同一套懒加载与 commit 锚点（评测动作也参与实时更新）。
    """
    from app.services.plugin_marketplace_service import list_marketplaces, prepare_market_plugin_for_eval

    source = (os.environ.get("PLUGIN_EVAL_SOURCE") or DEFAULT_EVALUATOR_SOURCE).strip()
    target_market = market_id

    if target_market is None:
        db = SessionLocal()
        try:
            synced = [m for m in list_marketplaces(db) if m["sync_status"] == "ok"]
        finally:
            db.close()
        if not synced:
            raise MarketEvalError(
                "没有可用的插件市场，无法加载评测器 plugin-eval（请先在「插件市场」页添加并同步市场）"
            )
        target_market = synced[0]["id"]

    info = prepare_market_plugin_for_eval(int(target_market), source)
    package_root = Path(info["path"])
    if not (package_root / "src").is_dir():
        raise MarketEvalError(
            f"市场内未找到 plugin-eval 源码目录（{info['path']}/src 不存在），"
            f"请确认市场内存在 {source}"
        )

    return {
        "name": "plugin-eval",
        "version": read_evaluator_version(package_root),
        "market_id": int(target_market),
        "market_name": info.get("market_name") or "",
        "source": source,
        "commit": info.get("commit") or "",
        "stale": bool(info.get("stale")),
        "refresh_error": info.get("refresh_error") or "",
        "package_root": str(package_root),
    }


def build_env(package_root: Path, cc_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """子进程环境：白名单透传 + plugin-eval 源码 PYTHONPATH + Claude Code SDK env"""
    env = {k: v for k in ENV_WHITELIST if (v := os.environ.get(k))}
    env["PYTHONPATH"] = str(package_root / "src")
    # 不往市场克隆目录里写 __pycache__（工作树保持干净，reset --hard 语义更可预期）
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update({k: str(v) for k, v in (cc_env or {}).items()})
    return env


def build_argv(
    package_root: Path,
    target_path: str,
    depth: str,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> List[str]:
    """子进程 argv：入口脚本 → plugin-eval score <目标> --depth <d> --output json"""
    return [
        sys.executable,
        str(ENTRY_SCRIPT),
        str(package_root),
        "score",
        str(target_path),
        "--depth",
        depth,
        "--output",
        "json",
        "--concurrency",
        str(int(concurrency)),
    ]


def build_trace_argv(
    package_root: Path,
    plugin_dir: str,
    thread_path: str,
    artifact_dirs: Optional[List[str]] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    judgement: str = DEFAULT_TRACE_MODE,
    depth: Optional[str] = None,
    history_only: bool = False,
) -> List[str]:
    """trace 评测 argv（默认一次出综合分与真实数据分）

    以被测插件目录作为 score 目标（plugin-eval 由 manifest name 加载历史规则）。
    ``history_only=False``：静态层 + judge 层与历史层在同一次评测里一起产出，
    与本地 ``score <plugin> --thread <jsonl> --depth standard`` 的报告结构一致；
    ``history_only=True``：只跑历史层（``--history-only``），设计质量由任务级缓存复用，
    避免同一插件在每个线程上重复跑静态层与 judge。
    历史阶段与范围固定为「执行 + 生成」两阶段、关键步骤。
    """
    static_depth = depth or (TRACE_OFFLINE_DEPTH if judgement == "offline" else TRACE_DEPTH)
    argv = [
        sys.executable,
        str(ENTRY_SCRIPT),
        str(package_root),
        "score",
        str(plugin_dir),
        "--thread",
        str(thread_path),
        "--depth",
        static_depth,
    ]
    for directory in artifact_dirs or []:
        argv.extend(["--artifacts", str(directory)])
    if judgement == "offline":
        # 离线只关历史侧的模型调用；静态深度同时退到 quick，全程零模型调用
        argv.append("--history-no-llm")
    if history_only:
        # 设计质量线已在任务级评过一次：逐行只补历史线
        argv.append("--history-only")
    argv.extend([
        "--history-phase",
        TRACE_PHASE,
        "--history-scope",
        TRACE_SCOPE,
        "--output",
        "json",
        "--concurrency",
        str(int(concurrency)),
    ])
    return argv


def _run_cli(
    argv: List[str],
    env: Dict[str, str],
    package_root: Path,
    limit: float,
    label: str,
    on_tick: Optional["Callable[[int], None]"] = None,
    tick_seconds: float = 60.0,
) -> "tuple[Any, int]":
    """执行入口脚本子进程并解析 stdout JSON，返回 (payload, duration_ms)

    超时/启动失败/非零退出/非 JSON 一律抛 MarketEvalError（调用方按行处理，
    不做静默回退——否则分数口径会悄悄换掉）。

    on_tick 供长任务用：给了就改走 Popen 轮询（每 tick_seconds 回调一次已运行秒数），
    让调用方在评测动作跑着的几十分钟里刷新执行心跳/写进度日志——历史评测单行实测
    近一小时，而平台收尸 reaper 的阈值是 15 分钟，一次部署重启就能把仍在跑的任务
    误判为 failed。stdout/stderr 落临时文件（线程内直读，避免管道写满后子进程阻塞）。
    """
    started = time.perf_counter()
    if on_tick is None:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=limit,
                env=env,
                cwd=str(package_root),
            )
        except subprocess.TimeoutExpired:
            raise MarketEvalError(f"评测动作执行超时（>{limit:.0f}s，{label}）")
        except OSError as e:
            raise MarketEvalError(f"无法启动评测动作子进程: {e}")
        duration_ms = int((time.perf_counter() - started) * 1000)
        return _parse_cli_output(proc.returncode, proc.stdout, proc.stderr, label, duration_ms)

    import tempfile

    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            child = subprocess.Popen(
                argv, stdout=out_f, stderr=err_f, env=env, cwd=str(package_root)
            )
        except OSError as e:
            raise MarketEvalError(f"无法启动评测动作子进程: {e}")

        returncode = None
        deadline = started + limit
        last_tick = started
        while True:
            returncode = child.poll()
            if returncode is not None:
                break
            now = time.perf_counter()
            if now >= deadline:
                child.kill()
                child.wait(timeout=30)
                raise MarketEvalError(f"评测动作执行超时（>{limit:.0f}s，{label}）")
            if now - last_tick >= tick_seconds:
                last_tick = now
                try:
                    on_tick(int(now - started))
                except Exception as e:  # 回调失败（心跳/日志）不得影响评测
                    logger.warning(f"[插件评测] tick 回调失败（忽略）: {e}")
            time.sleep(min(1.0, max(0.1, deadline - now)))

        duration_ms = int((time.perf_counter() - started) * 1000)
        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read().decode("utf-8", "replace")
        stderr = err_f.read().decode("utf-8", "replace")
    return _parse_cli_output(returncode, stdout, stderr, label, duration_ms)


def _parse_cli_output(
    returncode: int, stdout: str, stderr: str, label: str, duration_ms: int
) -> "tuple[Any, int]":
    """退出码与 stdout JSON 的统一校验（两条执行路径共用同一口径与报错文案）"""
    if returncode != 0:
        raise MarketEvalError(
            f"评测动作执行失败（退出码 {returncode}，{label}）："
            f"{(stderr or stdout or '').strip()[-600:]}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise MarketEvalError(
            f"评测动作输出不是合法 JSON（{e}）：{(stdout or '').strip()[:300]}"
        )
    return payload, duration_ms


def run_market_plugin_eval(
    target_path: str,
    depth: str = "quick",
    evaluator: Optional[Dict[str, Any]] = None,
    market_id: Optional[int] = None,
    timeout: Optional[float] = None,
    concurrency: Optional[int] = None,
) -> Dict[str, Any]:
    """执行市场版 plugin-eval，返回 {"payload", "evaluator", "duration_ms"}

    payload 即 plugin-eval CLI 的 JSON（composite/layers/elo/skill_results…）；
    evaluator 是评测动作自身的版本锚点。任何不可用/失败都抛 MarketEvalError
    （调用方按行失败，不做静默回退——否则分数口径会悄悄换掉）。
    """
    from app.services.claude_code_config_service import ClaudeCodeConfigService

    info = evaluator or resolve_evaluator(market_id)
    package_root = Path(info["package_root"])
    limit = timeout if timeout is not None else _timeout_for(depth)

    cc_env = {}
    if depth != "quick":  # quick 纯静态，不需要模型凭据
        cc_env = ClaudeCodeConfigService.get_sdk_env() or {}
        if not ClaudeCodeConfigService.get_active_config():
            raise MarketEvalError(
                "未配置 Claude Code 全局配置，standard 深度的 judge 层无法执行"
                "（请在「模型配置」页保存 Claude Code 配置，或改用 quick 深度）"
            )

    effective_concurrency = _concurrency(concurrency)
    argv = build_argv(package_root, target_path, depth, effective_concurrency)
    env = build_env(package_root, cc_env)
    payload, duration_ms = _run_cli(argv, env, package_root, limit, f"depth={depth}")
    if not isinstance(payload, dict) or "composite" not in payload:
        raise MarketEvalError(f"评测动作输出缺少 composite 字段：{str(payload)[:300]}")

    if info.get("stale"):
        logger.warning(
            f"[插件评测] 评测器为本地副本（同步失败：{info.get('refresh_error')}）："
            f"{info.get('source')}"
        )
    logger.info(
        f"[插件评测] 评测动作 plugin-eval {info.get('version') or '?'}"
        f"@{info.get('commit') or '?'} 完成（depth={depth}, {duration_ms}ms）"
    )
    return {"payload": payload, "evaluator": info, "duration_ms": duration_ms}


def run_market_plugin_trace_eval(
    plugin_dir: str,
    thread_path: str,
    artifact_dirs: Optional[List[str]] = None,
    evaluator: Optional[Dict[str, Any]] = None,
    market_id: Optional[int] = None,
    timeout: Optional[float] = None,
    concurrency: Optional[int] = None,
    on_tick: Optional[Callable[[int], None]] = None,
    judgement: str = DEFAULT_TRACE_MODE,
    depth: Optional[str] = None,
    history_only: bool = False,
) -> Dict[str, Any]:
    """按会话 trace 做评测，返回 {"payload", "evaluator", "duration_ms"}

    ``history_only=False`` 时 payload 为 plugin-eval 的完整 JSON：``composite``/``layers``
    （设计质量综合分与各层得分）与 ``real_data``/``history``/``history_generation``
    （真实数据测评指标）同时存在，两套分数相互独立（plugin-eval 口径：真实数据分不并入
    综合分、徽章与排名）。``history_only=True`` 只出历史线，``composite`` 为 null。
    因此校验点是 ``real_data``——历史规则选不中该插件的任务时真实数据分为未测，
    这种「定位不到」不算失败，由调用方按未测行处理。
    历史评测的抽取与判定都经 Claude Code 通道，未配置全局配置时直接报错。
    """
    from app.services.claude_code_config_service import ClaudeCodeConfigService

    info = evaluator or resolve_evaluator(market_id)
    package_root = Path(info["package_root"])
    limit = timeout if timeout is not None else _trace_timeout()

    cc_env = ClaudeCodeConfigService.get_sdk_env() or {}
    if judgement != "offline" and not ClaudeCodeConfigService.get_active_config():
        raise MarketEvalError(
            "未配置 Claude Code 全局配置，trace 评测的静态评审与历史判定无法执行"
            "（请在「模型配置」页保存 Claude Code 配置，或改用离线判定）"
        )

    effective_concurrency = _concurrency(concurrency)
    effective_depth = depth or (TRACE_OFFLINE_DEPTH if judgement == "offline" else TRACE_DEPTH)
    argv = build_trace_argv(
        package_root,
        plugin_dir,
        thread_path,
        artifact_dirs,
        effective_concurrency,
        judgement,
        effective_depth,
        history_only=history_only,
    )
    env = build_env(package_root, cc_env)
    payload, duration_ms = _run_cli(
        argv,
        env,
        package_root,
        limit,
        f"trace phase={TRACE_PHASE} depth={effective_depth}"
        + ("（仅历史线）" if history_only else ""),
        on_tick=on_tick,
    )
    if not isinstance(payload, dict) or "real_data" not in payload:
        raise MarketEvalError(f"评测动作输出缺少 real_data 字段：{str(payload)[:300]}")
    if not history_only and "composite" not in payload:
        raise MarketEvalError(f"设计质量评测输出缺少 composite 字段：{str(payload)[:300]}")

    if info.get("stale"):
        logger.warning(
            f"[插件评测] 评测器为本地副本（同步失败：{info.get('refresh_error')}）："
            f"{info.get('source')}"
        )
    logger.info(
        f"[插件评测] trace 评测完成（plugin={Path(plugin_dir).name}，"
        f"depth={effective_depth}，{'仅历史线' if history_only else '设计+历史'}，"
        f"并发 {effective_concurrency}，{duration_ms}ms）"
    )
    return {"payload": payload, "evaluator": info, "duration_ms": duration_ms}
