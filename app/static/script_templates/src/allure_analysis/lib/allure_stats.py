"""Allure 结果解析依赖示例：包根即 sys.path[0]，可直接 from lib import allure_stats"""
import json
from pathlib import Path
from typing import Dict, List, Optional


def parse_result(path: Optional[str]) -> Optional[Dict]:
    """解析单个 allure *-result.json，返回 {name, status, message, start_ms, stop_ms}

    解析失败返回 None（坏文件不计入统计，不中断整体流程）。
    """
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    details = data.get("statusDetails") if isinstance(data.get("statusDetails"), dict) else {}
    return {
        "name": str(data.get("name") or data.get("fullName") or ""),
        "status": str(data.get("status") or "unknown"),
        "message": str(details.get("message") or "")[:300],
        "start_ms": data.get("start") or 0,
        "stop_ms": data.get("stop") or 0,
    }


def count_by_status(rows: List[Dict]) -> Dict[str, int]:
    """按 status 汇总计数（passed/failed/broken/skipped/unknown/...）"""
    counts: Dict[str, int] = {}
    for row in rows:
        status = row.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def passed_rate(rows: List[Dict]) -> float:
    """passed 占比（0~1，四舍五入 4 位；无数据返回 0.0）"""
    if not rows:
        return 0.0
    passed = sum(1 for row in rows if row.get("status") == "passed")
    return round(passed / len(rows), 4)


def failed_brief(rows: List[Dict], limit: int = 50) -> List[Dict]:
    """failed 用例摘要（name + 失败信息前 200 字符）"""
    brief = []
    for row in rows:
        if row.get("status") == "failed":
            brief.append({"name": row.get("name") or "", "message": (row.get("message") or "")[:200]})
        if len(brief) >= limit:
            break
    return brief


def broken_brief(rows: List[Dict], limit: int = 50) -> List[Dict]:
    """broken（基础设施损坏）用例摘要"""
    brief = []
    for row in rows:
        if row.get("status") == "broken":
            brief.append({"name": row.get("name") or "", "message": (row.get("message") or "")[:200]})
        if len(brief) >= limit:
            break
    return brief
