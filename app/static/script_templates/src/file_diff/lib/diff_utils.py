"""多文件对比依赖示例：包根即 sys.path[0]，可直接 from lib import diff_utils"""
import difflib
from pathlib import Path
from typing import Dict, Optional


def read_text(path: Optional[str], limit: int = 2 * 1024 * 1024) -> str:
    """按 UTF-8 读文本（替换非法字节，2MB 截断），读不了返回空串"""
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit]


def line_diff_report(text_a: str, text_b: str, preview_lines: int = 30) -> Dict:
    """逐行差异对比：返回 identical / diff_line_count / 差异预览 / 双侧行数

    diff_line_count 为 unified diff 中以 +/- 开头的变更行数（不含 @@ 分块头）。
    """
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()
    unified = list(difflib.unified_diff(
        lines_a, lines_b, fromfile="a", tofile="b", lineterm=""))
    changed = [line for line in unified if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    preview = "\n".join(unified[:preview_lines])
    return {
        "identical": lines_a == lines_b,
        "diff_line_count": len(changed),
        "diff_preview": preview,
        "line_count_a": len(lines_a),
        "line_count_b": len(lines_b),
    }
