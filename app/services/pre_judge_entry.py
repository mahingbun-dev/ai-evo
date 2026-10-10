"""评测前处理脚本子进程执行 wrapper（统一子进程执行方案）

CLI 用法：python pre_judge_entry.py <script_dir> [entry]

执行语义（由 _execute_script_subprocess 以
[SCRIPT_PYTHON, pre_judge_entry.py, script_dir, entry] 调起）：
- 入口为 script_dir 内的相对路径 entry（缺省 main.py：脚本包固定入口约定）；
  历史绝对路径 entrypoint 以其文件名在父目录（作为 script_dir）下直执；
  缺失时打印可读错误到 stderr 并以非零码退出
- 用 ast 静态解析入口源码顶层是否定义 ``def run(``：
  - 有 → sys.path.insert(0, script_dir)（文件夹兄弟模块 import 支持），
    importlib 以 spec_from_file_location 加载入口，读 stdin 全量 JSON，
    调 ``run(stdin_dict)``，把返回 dict 以 json.dumps(ensure_ascii=False,
    default=str) 打印为 stdout 最后一行（标准输出协议）
  - 无 → runpy.run_path(entry, run_name="__main__")（stdin 未被消费、
    直通，兼容传统 __main__ 风格脚本：脚本自己 json.load(sys.stdin) 并打印）
- wrapper 自身异常打印可读错误到 stderr 并以非零码退出（用户脚本内部
  sys.exit 的退出码原样透传）
"""
import ast
import importlib.util
import json
import runpy
import sys
import traceback
from pathlib import Path
from typing import Optional


def has_toplevel_run(source: str) -> bool:
    """静态解析源码顶层是否定义 def run（仅模块直接子级，不含嵌套函数）"""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
        for node in tree.body
    )


def _load_entry_module(entry_py: Path):
    """以 spec_from_file_location 加载入口（不占用 __main__ 名义）"""
    module_name = "pre_judge_main"
    spec = importlib.util.spec_from_file_location(module_name, entry_py)
    if spec is None or spec.loader is None:  # pragma: no cover - 理论路径
        raise ImportError(f"无法加载脚本入口: {entry_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not 1 <= len(argv) <= 2:
        print("用法: python pre_judge_entry.py <script_dir> [entry]", file=sys.stderr)
        return 2
    script_dir = Path(argv[0])
    entry_rel = argv[1] if len(argv) == 2 else "main.py"
    entry_py = script_dir / entry_rel
    if not entry_py.is_file():
        print(f"脚本入口不存在: {entry_py}（脚本包必须包含根级 main.py）", file=sys.stderr)
        return 2

    source = entry_py.read_text(encoding="utf-8", errors="replace")

    if has_toplevel_run(source):
        # 文件夹兄弟模块 import：脚本目录进 sys.path 首位（与直接 python main.py 等价）
        sys.path.insert(0, str(script_dir))
        module = _load_entry_module(entry_py)
        stdin_dict = json.load(sys.stdin)
        result = module.run(stdin_dict)  # noqa: attr defined by user script
        if not isinstance(result, dict):
            print(
                f"run() 返回值必须是 dict（标准输出协议），实际类型: {type(result).__name__}",
                file=sys.stderr,
            )
            return 3
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    # 传统 __main__ 风格：stdin 未被消费、直通（脚本自行读取与输出）。
    # 脚本目录进 sys.path 首位：与 `python main.py` 直执行等价（sys.path[0] 即
    # 脚本目录），多文件包内同目录模块/包（如 wms_support/）可直接 import
    sys.path.insert(0, str(script_dir))
    runpy.run_path(str(entry_py), run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - wrapper 边界：异常转可读 stderr + 非零退出
        print(f"[pre_judge_entry] 脚本执行异常: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
