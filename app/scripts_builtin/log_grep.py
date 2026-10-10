"""内置预判脚本：log_grep

日志文件 + 正则/关键字列表的命中统计（设计：docs/design/pre-judge-scripts.md
§四 builtin 类型 / §十二 二期预置清单）：输出每个模式的命中行数、命中占比与
样例行（带行号），供裁判快速定位异常证据。

标准输入（stdin）：
    fields.log_file      日志文件本地路径（纯文本，逐行扫描）
    params.keywords      可选，关键字列表（字面量包含匹配，不做正则转义解释）
    params.patterns      可选，正则表达式列表（re.search 语义）
    params.case_sensitive 可选，默认 false：关键字/正则大小写不敏感
    params.max_samples   可选，默认 10：每个模式保留的样例行上限（全量写产物文件）
    params.fail_on_no_match 可选，默认 false：全部模式零命中时按 fail 处理

标准输出：{status, output, files, summary, finish_reason, error}
    output.total_lines       扫描总行数
    output.total_hits        命中行总和（同一行命中多个模式时分别计数）
    output.matched_line_count 命中过至少一个模式的不重复行数
    output.patterns          [{pattern, type, match_count, hit_rate, sample_lines,
                               sample_truncated}]
      sample_lines: [{line_no, line}]（行内超长截断到 500 字符）
    files.grep_full.json     全量命中行明细（每模式最多 1000 行，防超大日志撑爆产物）

keywords 与 patterns 至少给一个，两者可同时使用；非法正则按脚本 fail 处理。
"""
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_SAMPLE_LINE_MAX = 500
_DETAIL_LINES_MAX = 1000
_MAX_SAMPLES_DEFAULT = 10


def run(stdin: Dict[str, Any]) -> Dict[str, Any]:
    """标准入口：(标准输入 dict) -> 标准输出 dict"""
    fields = stdin.get("fields") or {}
    params = stdin.get("params") or {}
    log_path = fields.get("log_file")
    if not log_path:
        return _fail("缺少输入字段 log_file")

    keywords = params.get("keywords") or []
    patterns = params.get("patterns") or []
    if not isinstance(keywords, list) or not isinstance(patterns, list):
        return _fail("params.keywords 与 params.patterns 必须是字符串数组")
    if not keywords and not patterns:
        return _fail("至少提供一个关键字（params.keywords）或正则（params.patterns）")

    case_sensitive = bool(params.get("case_sensitive", False))
    try:
        max_samples = max(1, int(params.get("max_samples") or _MAX_SAMPLES_DEFAULT))
    except (TypeError, ValueError):
        max_samples = _MAX_SAMPLES_DEFAULT
    fail_on_no_match = bool(params.get("fail_on_no_match", False))

    try:
        compiled = _compile_patterns(keywords, patterns, case_sensitive)
    except re.error as e:
        return _fail(f"非法正则表达式: {e}")

    log_file = Path(str(log_path))
    if not log_file.is_file():
        return _fail(f"日志文件不存在: {log_path}")

    stats = [
        {"pattern": c["label"], "type": c["type"], "match_count": 0,
         "sample_lines": [], "detail_lines": []}
        for c in compiled
    ]
    total_lines = 0
    matched_lines = 0

    with log_file.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            total_lines += 1
            line = raw_line.rstrip("\r\n")
            hit_this_line = False
            for i, compiled_item in enumerate(compiled):
                if not compiled_item["regex"].search(line):
                    continue
                hit_this_line = True
                stat = stats[i]
                stat["match_count"] += 1
                if len(stat["sample_lines"]) < max_samples:
                    stat["sample_lines"].append(
                        {"line_no": line_no, "line": _truncate_line(line)})
                if len(stat["detail_lines"]) < _DETAIL_LINES_MAX:
                    stat["detail_lines"].append({"line_no": line_no, "line": line})
            if hit_this_line:
                matched_lines += 1

    pattern_outputs: List[Dict[str, Any]] = []
    for stat in stats:
        hit_rate = round(stat["match_count"] / total_lines, 4) if total_lines else 0.0
        pattern_outputs.append({
            "pattern": stat["pattern"],
            "type": stat["type"],
            "match_count": stat["match_count"],
            "hit_rate": hit_rate,
            "sample_lines": stat["sample_lines"],
            "sample_truncated": stat["match_count"] > len(stat["sample_lines"]),
        })

    total_hits = sum(s["match_count"] for s in stats)
    no_match = total_hits == 0
    if no_match:
        summary_text = f"扫描 {total_lines} 行，所有模式零命中"
    else:
        summary_text = f"扫描 {total_lines} 行，命中 {total_hits} 行次（去重 {matched_lines} 行）"

    detail_path = _write_detail(stdin.get("_run_dir"), stats, total_lines, total_hits, matched_lines)
    files: Dict[str, str] = {}
    if detail_path:
        files["grep_full.json"] = str(detail_path)

    if no_match and fail_on_no_match:
        return {
            "status": "fail",
            "output": {"total_lines": total_lines, "total_hits": 0,
                       "matched_line_count": 0, "patterns": pattern_outputs},
            "files": files,
            "summary": summary_text + "（fail_on_no_match=true）",
            "finish_reason": "completed",
            "error": "所有模式零命中（fail_on_no_match=true）",
        }

    return {
        "status": "ok",
        "output": {
            "total_lines": total_lines,
            "total_hits": total_hits,
            "matched_line_count": matched_lines,
            "patterns": pattern_outputs,
        },
        "files": files,
        "summary": summary_text,
        "finish_reason": "completed",
        "error": None,
    }


# ==================== 模式编译 ====================

def _compile_patterns(keywords: List[Any], patterns: List[Any], case_sensitive: bool) -> List[Dict[str, Any]]:
    """编译关键字（字面量）与正则两类模式，统一为 {label, type, regex}"""
    compiled: List[Dict[str, Any]] = []
    flags = 0 if case_sensitive else re.IGNORECASE
    for kw in keywords:
        if not isinstance(kw, str) or not kw:
            raise TypeError("keywords 必须是非空字符串数组")
        compiled.append({
            "label": kw,
            "type": "keyword",
            "regex": re.compile(re.escape(kw), flags),
        })
    for p in patterns:
        if not isinstance(p, str) or not p:
            raise TypeError("patterns 必须是非空字符串数组")
        compiled.append({
            "label": p,
            "type": "regex",
            "regex": re.compile(p, flags),
        })
    return compiled


def _truncate_line(line: str, limit: int = _SAMPLE_LINE_MAX) -> str:
    return line if len(line) <= limit else line[:limit] + "..."


# ==================== 产物与错误 ====================

def _write_detail(
    run_dir: Any,
    stats: List[Dict[str, Any]],
    total_lines: int,
    total_hits: int,
    matched_lines: int,
) -> Optional[Path]:
    """全量命中明细写入运行目录（运行器提供 _run_dir），缺省回退临时目录"""
    detail = {
        "stats": {"total_lines": total_lines, "total_hits": total_hits,
                  "matched_line_count": matched_lines},
        "patterns": [
            {"pattern": s["pattern"], "type": s["type"],
             "match_count": s["match_count"], "lines": s["detail_lines"]}
            for s in stats
        ],
    }
    try:
        base = Path(str(run_dir)) if run_dir else Path(tempfile.mkdtemp(prefix="log_grep_"))
        base.mkdir(parents=True, exist_ok=True)
        target = base / "grep_full.json"
        target.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 - 明细落盘失败不影响统计结论输出
        return None


def _fail(error: str) -> Dict[str, Any]:
    return {
        "status": "fail",
        "output": None,
        "files": {},
        "summary": "",
        "finish_reason": "error",
        "error": error,
    }
