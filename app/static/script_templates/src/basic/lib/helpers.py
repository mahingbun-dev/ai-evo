"""基础模板同目录依赖示例：包根即 sys.path[0]，可直接 from lib import helpers"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def read_text_safe(path: Optional[str], limit: int = 65536) -> str:
    """按 UTF-8 读文本（替换非法字节），读不了返回空串（不抛异常）"""
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit]


def file_brief(item: Dict[str, Any]) -> Dict[str, Any]:
    """inputs 文件对象数组单项摘要：rel_path/size/type + 首行预览"""
    rel_path = str(item.get("rel_path") or "")
    first_line = ""
    text = read_text_safe(item.get("path"), limit=4096)
    if text:
        first_line = text.splitlines()[0][:200] if text.splitlines() else ""
    return {
        "rel_path": rel_path,
        "size": int(item.get("size") or 0),
        "type": str(item.get("type") or ""),
        "first_line": first_line,
    }


def read_index(index_path: Optional[str]) -> List[Dict[str, Any]]:
    """读 root/index_path 模式的索引 JSON（[{path, rel_path, size, type}, ...]）"""
    if not index_path:
        return []
    try:
        rows = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return rows if isinstance(rows, list) else []
