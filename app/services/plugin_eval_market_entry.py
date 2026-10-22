"""市场版 plugin-eval 子进程入口（评测动作的外部加载器）

用法::

    python plugin_eval_market_entry.py <plugin-eval 包根目录> score <目标> [参数...]

与 ``pre_judge_entry.py`` 同构：把市场克隆下来的 plugin-eval 源码目录（``<包根>/src``）
插到 ``sys.path`` 首位，再按其 typer 入口执行。不用 ``python -c``：子进程里能看到
真实 traceback，参数拼装也能被单测直接断言。

退出码：0 正常；2 用法/源码目录问题；3 加载源码失败（依赖缺失或源码损坏）；
其余为被测 CLI 自己的退出码（``score --threshold`` 未达标为 1）。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(
            "用法: plugin_eval_market_entry.py <plugin-eval 包根目录> <子命令> [参数...]",
            file=sys.stderr,
        )
        return 2

    package_root = Path(args[0]).expanduser()
    src_dir = package_root / "src"
    if not src_dir.is_dir():
        print(f"[market-entry] plugin-eval 源码目录不存在: {src_dir}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(src_dir))
    try:
        from plugin_eval.cli import app
    except Exception as e:  # 依赖缺失（版本不满足）或源码损坏
        print(f"[market-entry] 加载 plugin_eval.cli 失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    sys.argv = ["plugin-eval", *args[1:]]
    try:
        app()
    except SystemExit as e:  # typer/click 的正常退出路径（含 --help、参数错误、Exit code）
        return int(e.code or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
