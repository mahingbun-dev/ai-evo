"""内置预判脚本：xml_diff

两份 XML 的结构化差异比对（设计：docs/design/pre-judge-scripts.md §四 builtin
类型 / §十二 二期预置清单）：节点增删、属性增删改、文本变化，输出差异清单与
一致率硬事实供裁判判断。

标准输入（stdin）：
    fields.xml_a            基准 XML 文件本地路径
    fields.xml_b            待比对 XML 文件本地路径
    params.ignore_whitespace 可选，默认 true：文本与属性值比对前去首尾空白并折叠连续空白
    params.max_diffs        可选，默认 200：标准输出 diffs 截断上限（全量写产物文件）

标准输出：{status, output, files, summary, finish_reason, error}
    output.identical         两份 XML 是否完全一致（布尔）
    output.consistency_rate  一致率 = 1 - 差异事件数 / 两棵树节点总数（保留 4 位）
    output.diff_count        差异事件总数
    output.total_nodes       两棵树节点总数（含根，不含文本节点）
    output.diffs             差异清单（按 max_diffs 截断）
    files.diff_full.json     全量差异明细（裁判 agent 用 Read 读全量）

差异事件类型（type 字段）：
    node_added / node_removed         节点增删（path 指向所在父路径，tag 为节点标签）
    attribute_added / attribute_removed / attribute_changed
    text_changed                      元素文本变化（{a, b} 双侧原文）

匹配策略：两棵树按文档序同步下沉，同层子节点按标签分组后按下标一一配对；
多余侧即增/删。一致率口径：每个差异事件计 1 个差异单元，分母为两棵树节点总数。
"""
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

_WS_RE = re.compile(r"\s+")

_MAX_DEFAULT = 200


def run(stdin: Dict[str, Any]) -> Dict[str, Any]:
    """标准入口：(标准输入 dict) -> 标准输出 dict"""
    fields = stdin.get("fields") or {}
    params = stdin.get("params") or {}
    path_a = fields.get("xml_a")
    path_b = fields.get("xml_b")

    if not path_a:
        return _fail("缺少输入字段 xml_a")
    if not path_b:
        return _fail("缺少输入字段 xml_b")

    ignore_ws = bool(params.get("ignore_whitespace", True))
    try:
        max_diffs = max(1, int(params.get("max_diffs") or _MAX_DEFAULT))
    except (TypeError, ValueError):
        max_diffs = _MAX_DEFAULT

    try:
        root_a = _parse_xml(path_a, "xml_a")
        root_b = _parse_xml(path_b, "xml_b")
    except _XmlParseError as e:
        return _fail(str(e))

    diffs: List[Dict[str, Any]] = []
    _diff_element(root_a, root_b, "", diffs, ignore_ws)

    total_nodes = _count_nodes(root_a) + _count_nodes(root_b)
    diff_count = len(diffs)
    identical = diff_count == 0
    rate = 1.0 if identical else round(max(0.0, 1.0 - diff_count / total_nodes), 4) if total_nodes else 0.0
    summary_text = (
        "两份 XML 完全一致" if identical
        else f"发现 {diff_count} 处差异（节点/属性/文本），一致率 {rate:.0%}"
    )

    detail_path = _write_detail(stdin.get("_run_dir"), diffs, total_nodes, rate, identical)
    files: Dict[str, str] = {}
    if detail_path:
        files["diff_full.json"] = str(detail_path)

    return {
        "status": "ok",
        "output": {
            "identical": identical,
            "consistency_rate": rate,
            "diff_count": diff_count,
            "total_nodes": total_nodes,
            "diffs": diffs[:max_diffs],
            "diff_truncated": diff_count > max_diffs,
        },
        "files": files,
        "summary": summary_text,
        "finish_reason": "completed",
        "error": None,
    }


# ==================== 解析 ====================

class _XmlParseError(Exception):
    pass


def _parse_xml(path: Any, field: str) -> ET.Element:
    p = Path(str(path))
    if not p.is_file():
        raise _XmlParseError(f"输入字段 {field} 的文件不存在: {path}")
    try:
        return ET.parse(str(p)).getroot()
    except ET.ParseError as e:
        raise _XmlParseError(f"输入字段 {field} 不是合法 XML: {e}")


def _count_nodes(element: ET.Element) -> int:
    return 1 + sum(_count_nodes(child) for child in element)


# ==================== 差异比对 ====================

def _diff_element(
    a: ET.Element,
    b: ET.Element,
    parent_path: str,
    diffs: List[Dict[str, Any]],
    ignore_ws: bool,
) -> None:
    """同步比对两个已配对元素：属性 → 文本 → 子元素（按标签分组配对）"""
    path = f"{parent_path}/{a.tag}"
    _diff_attributes(a, b, path, diffs)
    _diff_text(a, b, path, diffs, ignore_ws)
    _diff_children(a, b, path, diffs, ignore_ws)


def _diff_attributes(a: ET.Element, b: ET.Element, path: str, diffs: List[Dict[str, Any]]) -> None:
    attrs_a, attrs_b = a.attrib or {}, b.attrib or {}
    for key in sorted(set(attrs_b) - set(attrs_a)):
        diffs.append({"type": "attribute_added", "path": path, "tag": a.tag,
                      "attribute": key, "b": attrs_b[key]})
    for key in sorted(set(attrs_a) - set(attrs_b)):
        diffs.append({"type": "attribute_removed", "path": path, "tag": a.tag,
                      "attribute": key, "a": attrs_a[key]})
    for key in sorted(set(attrs_a) & set(attrs_b)):
        if str(attrs_a[key]) != str(attrs_b[key]):
            diffs.append({"type": "attribute_changed", "path": path, "tag": a.tag,
                          "attribute": key, "a": attrs_a[key], "b": attrs_b[key]})


def _diff_text(
    a: ET.Element,
    b: ET.Element,
    path: str,
    diffs: List[Dict[str, Any]],
    ignore_ws: bool,
) -> None:
    text_a, text_b = a.text or "", b.text or ""
    if _norm(text_a, ignore_ws) != _norm(text_b, ignore_ws):
        diffs.append({"type": "text_changed", "path": path, "tag": a.tag,
                      "a": text_a.strip(), "b": text_b.strip()})


def _diff_children(
    a: ET.Element,
    b: ET.Element,
    path: str,
    diffs: List[Dict[str, Any]],
    ignore_ws: bool,
) -> None:
    """子元素按标签分组后按下标配对：同标签多余侧计增/删，配对侧递归下沉"""
    groups_a = _group_by_tag(list(a))
    groups_b = _group_by_tag(list(b))
    for tag in sorted(set(groups_a) | set(groups_b)):
        list_a, list_b = groups_a.get(tag, []), groups_b.get(tag, [])
        paired = min(len(list_a), len(list_b))
        for node in list_a[paired:]:
            diffs.append({"type": "node_removed", "path": path, "tag": tag,
                          "a": _snippet(node)})
        for node in list_b[paired:]:
            diffs.append({"type": "node_added", "path": path, "tag": tag,
                          "b": _snippet(node)})
        for idx in range(paired):
            _diff_element(list_a[idx], list_b[idx], path, diffs, ignore_ws)


def _group_by_tag(children: List[ET.Element]) -> Dict[str, List[ET.Element]]:
    groups: Dict[str, List[ET.Element]] = {}
    for child in children:
        groups.setdefault(child.tag, []).append(child)
    return groups


def _norm(text: str, ignore_ws: bool) -> str:
    text = text or ""
    return _WS_RE.sub(" ", text).strip() if ignore_ws else text


def _snippet(element: ET.Element, limit: int = 200) -> str:
    """增删节点的可读片段（序列化截断，供差异清单直接阅读）"""
    try:
        text = ET.tostring(element, encoding="unicode")
    except Exception:  # noqa: BLE001 - 序列化失败回退标签名
        text = f"<{element.tag} .../>"
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[:limit] + "..."


# ==================== 产物与错误 ====================

def _write_detail(
    run_dir: Any,
    diffs: List[Dict[str, Any]],
    total_nodes: int,
    rate: float,
    identical: bool,
) -> Optional[Path]:
    """全量差异明细写入运行目录（运行器提供 _run_dir），缺省回退临时目录"""
    detail = {
        "stats": {"identical": identical, "diff_count": len(diffs),
                  "total_nodes": total_nodes, "consistency_rate": rate},
        "diffs": diffs,
    }
    try:
        base = Path(str(run_dir)) if run_dir else Path(tempfile.mkdtemp(prefix="xml_diff_"))
        base.mkdir(parents=True, exist_ok=True)
        target = base / "diff_full.json"
        target.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 - 明细落盘失败不影响比对结论输出
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
