"""aievo-cli 命令入口：typer 应用。

命令与退出码约定（与 ci-templates/ 及 docs/user_guide 保持一致）：
- aievo config add <url> <api-key> [--profile]     保存平台地址与 API Key
- aievo run <task-id> [--wait --timeout N --json]  触发运行；--wait 轮询门禁判定
- aievo results <task-id> [-o json|csv]            读取结果
- aievo datasets upload <file> [--name]            创建数据集并导入

退出码：0 = 门禁通过；1 = 门禁失败（质量问题，卡发布）；2 = 错误
（认证/网络/超时/任务失败收尾等，流水线应视为基础设施故障而非质量失败）。
"""
import csv
import json as jsonlib
import sys
import time
from pathlib import Path
from typing import List, Optional

import typer

from . import EXIT_ERROR, EXIT_FAILED, EXIT_PASSED, config as cfg
from .client import AievoApiError, AievoClient

app = typer.Typer(
    help="AI-Evo 评测平台 CLI：评测即门禁（exit 0=通过 / 1=门禁失败 / 2=错误）",
    no_args_is_help=True,
)
config_app = typer.Typer(help="管理平台连接配置", no_args_is_help=True)
datasets_app = typer.Typer(help="数据集管理", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(datasets_app, name="datasets")

# 任务收尾但无门禁判定的终态（fail/stop 路径由平台自动落 error 判定，
# 兜底仍无判定视为基础设施错误）
_TERMINAL_WITHOUT_VERDICT = ("failed", "stopped")


def _profile_option(default_from_env: bool = True) -> str:
    """--profile 默认值支持 AIEVO_PROFILE 环境变量（CI 中免重复传参）"""
    import os

    return os.environ.get("AIEVO_PROFILE", cfg.DEFAULT_PROFILE)


def _build_client(profile: str) -> AievoClient:
    """按 profile 构造客户端；配置缺失时以退出码 2 终止"""
    try:
        values = cfg.get_profile(profile)
    except KeyError:
        typer.secho(
            f"未找到 profile「{profile}」，请先执行：aievo config add <url> <api-key>"
            + (f" --profile {profile}" if profile != cfg.DEFAULT_PROFILE else ""),
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_ERROR)
    return AievoClient(values["url"], values["api_key"])


def _api_error_exit(e: AievoApiError) -> None:
    """统一 API 错误输出与退出码"""
    typer.secho(f"错误：{e.message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(EXIT_ERROR)


# ==================== config ====================


@config_app.command("add")
def config_add(
    url: str = typer.Argument(..., help="平台地址，如 https://aievo.example.com"),
    api_key: str = typer.Argument(..., help="API Key（aievo_sk_ 开头）"),
    profile: str = typer.Option(cfg.DEFAULT_PROFILE, "--profile", "-p", help="配置名"),
):
    """保存平台地址与 API Key（明文落盘 ~/.aievo/config.toml，权限 0600）"""
    path = cfg.save_profile(profile, url, api_key)
    typer.echo(
        f"已保存 profile「{profile}」→ {path}（{url.rstrip('/')}，key {cfg.mask_key(api_key)}）"
    )


@config_app.command("list")
def config_list():
    """列出已保存的 profile"""
    profiles = cfg.list_profiles()
    if not profiles:
        typer.echo("尚无配置，先执行：aievo config add <url> <api-key>")
        return
    for name, values in profiles.items():
        typer.echo(f"{name}\t{values.get('url', '')}\t{cfg.mask_key(values.get('api_key', ''))}")


# ==================== run ====================


@app.command()
def run(
    task_id: str = typer.Argument(..., help="评测任务 ID"),
    wait: bool = typer.Option(False, "--wait", help="阻塞等待门禁判定并按 0/1/2 退出"),
    timeout: int = typer.Option(
        7200, "--timeout", min=1, help="--wait 最长等待秒数（默认 7200）"
    ),
    interval: int = typer.Option(5, "--interval", min=1, help="轮询间隔秒数"),
    json_mode: bool = typer.Option(False, "--json", help="以单个 JSON 对象输出最终状态"),
    profile: str = typer.Option(
        None, "--profile", "-p", help="配置名（默认取 AIEVO_PROFILE 或 default）"
    ),
):
    """触发评测任务运行；--wait 时轮询门禁端点，退出码 0=通过 / 1=失败 / 2=错误"""
    profile = profile or _profile_option()
    client = _build_client(profile)
    started_at = time.monotonic()

    try:
        task = client.run_task(task_id)
    except AievoApiError as e:
        _api_error_exit(e)

    execution_number = task.get("execution_number") or task.get("execution_count")

    # 非 --wait：触发成功即退出 0（判定交给流水线后续步骤或人工）
    try:
        if not wait:
            if json_mode:
                typer.echo(
                    jsonlib.dumps(
                        {
                            "task_id": task_id,
                            "status": task.get("status"),
                            "execution_number": execution_number,
                            "triggered": True,
                            "waited": False,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                typer.echo(
                    f"任务 {task_id} 已触发（第 {execution_number} 次执行），状态：{task.get('status')}"
                )
            raise typer.Exit(EXIT_PASSED)

        # --wait：轮询门禁端点直至出现判定/超时/异常终态
        deadline = time.monotonic() + timeout
        while True:
            try:
                gate = client.get_gate(task_id, execution_number)
            except AievoApiError as e:
                _api_error_exit(e)

            verdict = gate.get("verdict")
            if verdict in ("passed", "failed", "error"):
                _report_gate(task_id, gate, started_at, json_mode, verdict)
            if gate.get("status") in _TERMINAL_WITHOUT_VERDICT:
                _finish(
                    EXIT_ERROR,
                    f"任务以 {gate.get('status')} 收尾且未产生门禁判定",
                    task_id, gate, started_at, json_mode,
                )
            if not gate.get("gate_enabled"):
                _finish(
                    EXIT_ERROR,
                    "任务未启用门禁（gate_config 为空），无法按门禁语义退出",
                    task_id, gate, started_at, json_mode,
                )
            if time.monotonic() >= deadline:
                _finish(
                    EXIT_ERROR,
                    f"等待门禁判定超时（>{timeout}s）",
                    task_id, gate, started_at, json_mode,
                )
            time.sleep(interval)
    finally:
        client.close()


def _finish(
    exit_code: int,
    message: str,
    task_id: str,
    gate: dict,
    started_at: float,
    json_mode: bool,
) -> None:
    """统一出口：--json 时输出单对象，人类可读文本进 stderr/stdout 后退出"""
    if json_mode:
        typer.echo(
            jsonlib.dumps(
                {
                    "task_id": task_id,
                    "status": gate.get("status"),
                    "execution_number": gate.get("execution_number"),
                    "gate_enabled": gate.get("gate_enabled"),
                    "verdict": gate.get("verdict"),
                    "gate_result": gate.get("gate_result"),
                    "error": message,
                    "elapsed_seconds": round(time.monotonic() - started_at, 1),
                },
                ensure_ascii=False,
            )
        )
    else:
        typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(exit_code)


def _report_gate(task_id: str, gate: dict, started_at: float, json_mode: bool, verdict: str) -> None:
    """门禁判定出口：0=passed / 1=failed / 2=error"""
    exit_code = {"passed": EXIT_PASSED, "failed": EXIT_FAILED, "error": EXIT_ERROR}[verdict]
    gate_result = gate.get("gate_result") or {}
    if json_mode:
        typer.echo(
            jsonlib.dumps(
                {
                    "task_id": task_id,
                    "status": gate.get("status"),
                    "execution_number": gate.get("execution_number"),
                    "gate_enabled": gate.get("gate_enabled"),
                    "gate_mode": gate.get("gate_mode"),
                    "verdict": verdict,
                    "gate_result": gate_result,
                    "checks": gate_result.get("checks"),
                    "elapsed_seconds": round(time.monotonic() - started_at, 1),
                },
                ensure_ascii=False,
            )
        )
    else:
        color = {"passed": typer.colors.GREEN, "failed": typer.colors.YELLOW, "error": typer.colors.RED}[verdict]
        typer.secho(f"门禁判定：{verdict}", fg=color, err=True)
        for check in gate_result.get("checks") or []:
            typer.secho(
                f"  {check.get('metric')} {check.get('op')} {check.get('value')}"
                f" → 实际 {check.get('actual')}：{'通过' if check.get('passed') else '未通过'}",
                err=True,
            )
    raise typer.Exit(exit_code)


# ==================== results ====================


@app.command()
def results(
    task_id: str = typer.Argument(..., help="评测任务 ID"),
    output: str = typer.Option("json", "--output", "-o", help="输出格式：json | csv"),
    execution_number: Optional[int] = typer.Option(
        None, "--execution", "-e", help="指定执行版本（默认最新）"
    ),
    profile: str = typer.Option(
        None, "--profile", "-p", help="配置名（默认取 AIEVO_PROFILE 或 default）"
    ),
):
    """读取评测结果：-o json 输出完整结果对象；-o csv 输出行级明细（idx/success/row_status/average_score/feedback）"""
    if output not in ("json", "csv"):
        typer.secho(f"不支持的输出格式：{output}（仅 json | csv）", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_ERROR)

    profile = profile or _profile_option()
    client = _build_client(profile)
    try:
        if execution_number:
            body = client.get_result_version(task_id, execution_number)
        else:
            body = client.get_result(task_id)
    except AievoApiError as e:
        _api_error_exit(e)
    finally:
        client.close()

    if output == "json":
        typer.echo(jsonlib.dumps(body, ensure_ascii=False, indent=2))
        return

    # csv：从聚合结果的 items 展开行级明细；failed_items 行一并附上（success=0）
    payload = body.get("result") or {}
    items: List[dict] = list(payload.get("items") or [])
    items.extend(payload.get("failed_items") or [])
    if not items:
        typer.secho(
            f"任务 {task_id} 暂无可导出的行级结果（状态 {body.get('status')}）",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_ERROR)

    writer = csv.writer(sys.stdout)
    writer.writerow(["idx", "success", "row_status", "average_score", "feedback"])
    for item in sorted(items, key=lambda x: x.get("idx", 0)):
        writer.writerow(
            [
                item.get("idx", ""),
                item.get("success", ""),
                item.get("row_status", ""),
                item.get("average_score", ""),
                item.get("feedback", ""),
            ]
        )


# ==================== datasets ====================


@datasets_app.command("upload")
def datasets_upload(
    file: Path = typer.Argument(..., exists=True, readable=True, help="数据文件（.json / .xlsx / .xls）"),
    name: Optional[str] = typer.Option(None, "--name", help="数据集名称（默认取文件名主干）"),
    description: Optional[str] = typer.Option(None, "--description", help="数据集描述"),
    profile: str = typer.Option(
        None, "--profile", "-p", help="配置名（默认取 AIEVO_PROFILE 或 default）"
    ),
):
    """创建数据集并导入数据（json 走 import/json，Excel 走 import/excel）"""
    profile = profile or _profile_option()
    suffix = file.suffix.lower()
    if suffix not in (".json", ".xlsx", ".xls"):
        typer.secho(f"不支持的文件类型：{suffix}（仅 .json / .xlsx / .xls）", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_ERROR)

    client = _build_client(profile)
    try:
        dataset = client.create_dataset(name or file.stem, description)
        dataset_id = dataset["id"]
        if suffix == ".json":
            data = jsonlib.loads(file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                data = data["data"]
            if not isinstance(data, list):
                raise AievoApiError(400, "JSON 文件需为对象数组（或 {\"data\": [...]}）")
            count = client.import_dataset_json(dataset_id, data)
        else:
            count = client.import_dataset_excel(dataset_id, file)
    except AievoApiError as e:
        _api_error_exit(e)
    except (ValueError, OSError) as e:
        typer.secho(f"错误：读取文件失败（{e}）", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_ERROR)
    finally:
        client.close()

    typer.echo(f"数据集已创建：{dataset['name']}（id={dataset_id}），导入 {count} 条数据")
