"""hub 执行包模板：层级感知纯函数示例（被 main.py import）

执行包解包后的层级固定为：inputs.root 下唯一顶层目录 {execution_id}-attempt-1/
（zip 原始最外层，平台不解压嵌套 zip、不改名），所有 rel_path 都带该前缀。
本模块演示与该层级打交道的安全写法：从 rel_path 推导顶层目录、按顶层分组、
按后缀定位关键文件——不硬编码 attempt 目录名（不同执行包的 execution_id 不同）。
"""
import json
from pathlib import Path


def attempt_top_of(rel_path: str) -> str:
    """取 rel_path 的第一段作为执行包顶层目录名（即 {execution_id}-attempt-1）"""
    parts = Path(str(rel_path or "")).parts
    return parts[0] if parts else ""


def group_by_attempt(items: list) -> dict:
    """把文件对象数组按顶层 attempt 目录分组：{attempt_top: [rel_path, ...]}"""
    grouped: dict = {}
    for item in items or []:
        rel = str(item.get("rel_path") or "")
        if not rel:
            continue
        grouped.setdefault(attempt_top_of(rel), []).append(rel)
    return grouped


def read_index(index_path: str) -> list:
    """root/index_path 模式：读索引 JSON（生产 build_file_index 产物）

    每项: {path: 相对 inputs.root 的 posix 路径, type: text|image|archive|binary,
           size: 字节, is_nested_zip: bool[, nested_index]}——path 与 files 数组的
    rel_path 同值；读文件内容时用 Path(root) / row["path"] 拼绝对路径。
    """
    if not index_path:
        return []
    try:
        return json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def find_by_suffix(items: list, suffix: str) -> list:
    """按 rel_path 后缀在文件对象数组中定位文件（不依赖 attempt 目录名）"""
    return [item for item in items or [] if str(item.get("rel_path") or "").endswith(suffix)]


def read_manifest_summary(items: list) -> dict:
    """定位并解析 execution-manifest.json，产出执行摘要（与包层级解耦）

    case_results 兼容真实包的两种形态（与平台归一化语义一致）：
    - list：[{case_id, status}, ...]
    - dict：{"TC_001": "failed", ...}
    """
    hits = find_by_suffix(items, "execution-manifest.json")
    if not hits:
        return {}
    try:
        manifest = json.loads(Path(hits[0].get("path")).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}
    raw = manifest.get("case_results")
    if isinstance(raw, list):
        statuses = [str(i.get("status", "")).lower() if isinstance(i, dict) else "" for i in raw]
    elif isinstance(raw, dict):
        statuses = [
            v.strip().lower() if isinstance(v, str)
            else str(v.get("status", "")).lower() if isinstance(v, dict) else ""
            for v in raw.values()
        ]
    else:
        statuses = []
    return {
        "execution_id": manifest.get("execution_id"),
        "case_total": len(statuses),
        "case_passed": sum(1 for s in statuses if s in ("passed", "pass", "success")),
        "case_failed": sum(1 for s in statuses if s in ("failed", "fail", "error")),
    }


def read_text_safe(path: str, limit: int = 200) -> str:
    """安全读取文本文件前 limit 字符（文件缺失/二进制等异常返回空串）"""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, ValueError):
        return ""
