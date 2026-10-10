"""内置预判脚本：allure_vs_cases

将 anc-runner 用例文件的预期结果与 Allure 报告的实际执行结果逐条比对，
产出硬事实供裁判做可信度判断（设计：docs/design/pre-judge-scripts.md 第八节）。

标准输入（stdin）：
    fields.cases_file        anc-runner cases.json 或 testpoints.md 的本地路径
    inputs.allure_results    hub 执行包输入（缺陷B3 起 hub_file 通道注入 inputs；
                             兼容旧编排仍在 fields 传递的路径字符串）。支持三种值：
                             {root, files:[...]} / {root, index_path}（执行包通道），
                             或 allure-results 目录 / 目录 zip / 单个 *-result.json 路径
    params.fuzzy_threshold 可选，0~1，启用第四级模糊匹配（difflib 相似度阈值）
    _run_dir               运行器提供的产物目录（缺省回退 tempfile 目录）

标准输出：{status, output, files, summary, finish_reason, error}
    output.consistency_rate 一致率（一致用例数 / 用例总数，含未匹配缺失）
    output.mismatched_count 不一致条数（含未匹配缺失）
    output.summary_text     人类可读摘要
    output.per_case         逐条比对明细 [{case, expected, priority, actual_status, match, match_state, match_type, evidence}]
    output.p0_skipped       P0 用例被 skipped 清单（执行不完整硬信号）
    files.per_case_full.json 大明细（含未匹配 allure 清单）

匹配策略（依次降级）：exact name → fullName 包含 → 归一化（去空白/小写）→
可选模糊阈值 params.fuzzy_threshold。
"""
import json
import re
import tempfile
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allure 结果状态
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_BROKEN = "broken"
STATUS_SKIPPED = "skipped"

# 比对结论三态（Results 页：一致绿 / 不一致红 / 缺失灰）
MATCH_STATE_CONSISTENT = "consistent"
MATCH_STATE_MISMATCH = "mismatch"
MATCH_STATE_MISSING = "missing"

_EVIDENCE_MAX_LEN = 500
_NORMALIZE_RE = re.compile(r"\s+")
_ZIP_RESULT_RE = re.compile(r"[^/]+-result\.json$")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_MD_SEPARATOR_RE = re.compile(r"^[-*|]?\s*[-=]{3,}\s*$")


def _allure_input_paths(value: Any) -> List[Path]:
    """把 hub 执行包输入值展开为 *-result.json 文件路径列表（缺陷B3 兼容）

    - {root, files: [...]}：取 files 明细中 rel_path 以 -result.json 结尾的项，
      拼接 root 绝对路径
    - {root, index_path}：读索引 JSON（[{path, rel_path, ...}]），同上筛选拼接
    - 其它（str 路径等）返回空列表，由 load_allure_input 走字符串路径分支
    """
    if not isinstance(value, dict):
        return []
    root = str(value.get("root") or "").rstrip("/")
    candidates: List[str] = []
    files = value.get("files")
    if isinstance(files, list):
        candidates = [str(f.get("rel_path") or "") for f in files if isinstance(f, dict)]
    elif value.get("index_path"):
        try:
            index_raw = json.loads(Path(str(value["index_path"])).read_text(encoding="utf-8"))
            candidates = [
                str(item.get("rel_path") or "")
                for item in index_raw if isinstance(item, dict)
            ]
        except (OSError, json.JSONDecodeError):
            candidates = []
    return [Path(f"{root}/{rel}") for rel in candidates if rel.endswith("-result.json")]


def load_allure_input(value: Any) -> List[Dict[str, Any]]:
    """加载 allure 结果（allure_vs_cases 输入入口）：路径字符串或 hub 结构化值

    - str：委托 load_allure_results（目录 / 目录 zip / 单个 *-result.json）
    - dict：hub 执行包输入（{root, files} / {root, index_path}），按
      _allure_input_paths 展开逐个解析
    """
    if isinstance(value, dict):
        results: List[Dict[str, Any]] = []
        for path in _allure_input_paths(value):
            parsed = _parse_result_json(path.read_text(encoding="utf-8", errors="replace"))
            if parsed:
                results.append(parsed)
        return results
    return load_allure_results(str(value))


def run(stdin: Dict[str, Any]) -> Dict[str, Any]:
    """标准入口：(标准输入 dict) -> 标准输出 dict"""
    fields = stdin.get("fields") or {}
    params = stdin.get("params") or {}
    cases_path = fields.get("cases_file")
    # hub_file 通道值注入 stdin.inputs（缺陷B3）；回退 fields 兼容旧编排/调试直连
    allure_input = (stdin.get("inputs") or {}).get("allure_results") or fields.get("allure_results")

    if not cases_path:
        return _fail("缺少输入字段 cases_file")
    if not allure_input:
        return _fail("缺少输入字段 allure_results")

    try:
        cases = parse_cases_file(cases_path)
    except Exception as e:  # noqa: BLE001 - 脚本边界：任何解析失败都转标准输出 fail
        return _fail(f"解析用例文件失败: {e}")
    if not cases:
        return _fail("用例文件为空或未解析到任何用例")

    try:
        allure_results = load_allure_input(allure_input)
    except Exception as e:  # noqa: BLE001
        return _fail(f"解析 allure 结果失败: {e}")
    if not allure_results:
        return _fail("allure 结果为空或未解析到任何 *-result.json")

    fuzzy_threshold = params.get("fuzzy_threshold")
    if fuzzy_threshold is not None:
        try:
            fuzzy_threshold = float(fuzzy_threshold)
            if not 0 < fuzzy_threshold <= 1:
                fuzzy_threshold = None
        except (TypeError, ValueError):
            fuzzy_threshold = None

    per_case, unmatched_allure = match_and_compare(cases, allure_results, fuzzy_threshold)

    total = len(per_case)
    mismatched = sum(1 for c in per_case if not c["match"])
    consistent = total - mismatched
    rate = round(consistent / total, 4) if total else 0.0
    summary_text = f"{consistent}/{total} 用例结果与预期一致，{mismatched} 条不一致"
    p0_skipped = [c["case"] for c in per_case
                  if str(c.get("priority") or "").upper() == "P0" and c.get("actual_status") == STATUS_SKIPPED]

    output: Dict[str, Any] = {
        "consistency_rate": rate,
        "mismatched_count": mismatched,
        "summary_text": summary_text,
        "per_case": per_case,
    }
    if p0_skipped:
        output["p0_skipped"] = p0_skipped

    # 大明细落盘并注册进 files（裁判 agent 用 Read 工具读全量，不进 prompt）
    detail_path = _write_detail(stdin.get("_run_dir"), per_case, unmatched_allure, total, consistent, rate)
    files: Dict[str, str] = {}
    if detail_path:
        files["per_case_full.json"] = str(detail_path)

    return {
        "status": "ok",
        "output": output,
        "files": files,
        "summary": summary_text,
        "finish_reason": "completed",
        "error": None,
    }


# ==================== 用例解析 ====================

def parse_cases_file(path: str) -> List[Dict[str, Any]]:
    """按扩展名分派解析用例文件，统一返回 [{name, expected, priority}]"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"用例文件不存在: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".md":
        return parse_testpoints_md(text)
    return parse_cases_json(text)


def parse_cases_json(text: str) -> List[Dict[str, Any]]:
    """解析 anc-runner cases.json：顶层列表，或 {cases|data|testpoints: [...]} 包装"""
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("cases", "data", "testpoints"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("cases.json 结构不合法：期望列表或含 cases/data/testpoints 数组的对象")

    cases: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id") or item.get("title") or item.get("case_name")
        if not name:
            continue
        cases.append({
            "name": str(name),
            "expected": _as_text(item.get("expected") or item.get("expected_result") or item.get("expect")),
            "priority": str(item.get("priority") or item.get("level") or ""),
        })
    return cases


def parse_testpoints_md(text: str) -> List[Dict[str, Any]]:
    """解析 testpoints.md：标题行=用例名，「预期」小节/行内标签=预期结果

    约定：H1（单个 #）视为文档标题跳过，H2~H6 为用例标题。
    支持形态：
        ## 用例A            ## 用例A
        **预期结果**：xxx    ### 预期
        - xxx               xxx
    遇到「前置/步骤/实际结果」小节标题时结束预期收集。
    """
    cases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    in_expected = False

    for line in text.splitlines():
        stripped = line.strip()
        heading = _MD_HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(0)) - len(heading.group(0).lstrip("#"))
            title = heading.group(1).strip()
            if level == 1:
                # H1 = 文档标题，不是用例
                continue
            if current is not None and _is_expected_label(title):
                # 「### 预期结果」小节标题：归属当前用例，不开新用例
                in_expected = True
                continue
            if current is not None and _is_other_section_label(title):
                # 「### 前置 / 步骤 / 实际结果」小节：结束预期收集，不开新用例
                in_expected = False
                continue
            if current is not None:
                cases.append(_flush_md_case(current))
            current = {"name": title, "expected_lines": []}
            in_expected = False
            continue

        if current is None or not stripped or _MD_SEPARATOR_RE.match(stripped):
            continue

        if _is_other_section_label(stripped):
            in_expected = False
            continue

        if _is_expected_label(stripped):
            # 行内标签形态：**预期结果**：xxx
            in_expected = True
            rest = re.sub(r"^[#*\-\s]*", "", stripped)
            rest = re.split(r"[：:]", rest, maxsplit=1)[1].strip() if re.match(r"^[^：:]*[：:]", rest) else ""
            if rest:
                current["expected_lines"].append(rest)
            continue

        if in_expected:
            current["expected_lines"].append(re.sub(r"^[-*\s]+", "", stripped))

    if current is not None:
        cases.append(_flush_md_case(current))

    return [c for c in cases if c["name"]]


def _flush_md_case(current: Dict[str, Any]) -> Dict[str, Any]:
    expected = "\n".join(current["expected_lines"]).strip()
    name = current["name"]
    priority = ""
    p_match = re.search(r"\[?(P\d)\]?", name, re.IGNORECASE)
    if p_match:
        priority = p_match.group(1).upper()
    return {"name": name, "expected": expected, "priority": priority}


# 其他小节标签（与「预期」并列的固定词，精确匹配标签部分，避免误吞
# 「步骤一：登录」这类真实用例标题）
_OTHER_SECTION_LABELS = {
    "前置", "前置条件", "步骤", "测试步骤", "实际结果", "执行结果",
    "precondition", "step", "steps", "actual", "actual result",
}


def _is_expected_label(text: str) -> bool:
    plain = text.lstrip("#* -").lower()
    return plain.startswith("预期") or plain.startswith("expected")


def _is_other_section_label(text: str) -> bool:
    """小节标签判断：取首个冒号前的标签部分精确匹配（兼容「实际结果：xxx」行）"""
    plain = text.strip().lstrip("#* ").strip()
    label = re.split(r"[：:]", plain, maxsplit=1)[0].strip().lower()
    return label in _OTHER_SECTION_LABELS


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ==================== allure 结果解析 ====================

def load_allure_results(path: str) -> List[Dict[str, Any]]:
    """加载 allure 结果：目录 / 目录 zip / 单个 *-result.json，返回 [{name, fullName, status, statusDetails}]"""
    p = Path(path)
    if p.is_dir():
        results: List[Dict[str, Any]] = []
        for f in sorted(p.glob("*-result.json")):
            parsed = _parse_result_json(f.read_text(encoding="utf-8", errors="replace"))
            if parsed:
                results.append(parsed)
        return results
    if zipfile.is_zipfile(str(p)):
        results = []
        with zipfile.ZipFile(str(p)) as zf:
            for info in zf.infolist():
                # 兼容带目录前缀的打包形态（如 allure-results/0-result.json）
                basename = info.filename.rsplit("/", 1)[-1]
                if info.is_dir() or not _ZIP_RESULT_RE.fullmatch(basename):
                    continue
                parsed = _parse_result_json(zf.read(info).decode("utf-8", errors="replace"))
                if parsed:
                    results.append(parsed)
        return results
    if p.is_file() and p.name.endswith("-result.json"):
        parsed = _parse_result_json(p.read_text(encoding="utf-8", errors="replace"))
        return [parsed] if parsed else []
    raise ValueError("allure_results 仅支持：allure-results 目录、目录 zip、或单个 *-result.json")


def _parse_result_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or raw.get("fullName") or ""
    return {
        "name": str(name),
        "fullName": str(raw.get("fullName") or ""),
        "status": str(raw.get("status") or "").lower(),
        "statusDetails": raw.get("statusDetails") or {},
    }


# ==================== 匹配与比对 ====================

def match_and_compare(
    cases: List[Dict[str, Any]],
    allure_results: List[Dict[str, Any]],
    fuzzy_threshold: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """逐用例按四级策略匹配并比对，返回（per_case 明细, 未匹配的 allure 结果列表）

    每条 allure 结果至多被一个用例消费；未匹配上的 allure 结果进入明细文件供人工复核。
    """
    available = list(range(len(allure_results)))
    per_case: List[Dict[str, Any]] = []

    for case in cases:
        idx, match_type = _find_match(str(case["name"]), allure_results, available, fuzzy_threshold)
        if idx is None:
            per_case.append({
                "case": case["name"],
                "expected": case.get("expected", ""),
                "priority": case.get("priority", ""),
                "actual_status": None,
                "match": False,
                "match_state": MATCH_STATE_MISSING,
                "match_type": "none",
                "evidence": "allure 结果中未找到匹配用例（缺失）",
            })
            continue

        available.remove(idx)
        per_case.append(_compare(case, allure_results[idx], match_type))

    unmatched_allure = [allure_results[i] for i in available]
    return per_case, unmatched_allure


def _find_match(
    case_name: str,
    allure_results: List[Dict[str, Any]],
    available: List[int],
    fuzzy_threshold: Optional[float],
) -> Tuple[Optional[int], str]:
    """四级匹配：exact → fullName 包含 → 归一化 → 可选模糊阈值"""
    normalized = normalize_name(case_name)

    # 1. exact name
    for i in available:
        if allure_results[i]["name"] == case_name:
            return i, "exact"
    # 2. fullName 包含
    for i in available:
        full_name = allure_results[i]["fullName"]
        if full_name and case_name in full_name:
            return i, "full_name_contains"
    # 3. 归一化（去空白/大小写）后与 name 或 fullName 相等
    for i in available:
        r = allure_results[i]
        if normalize_name(r["name"]) == normalized or (r["fullName"] and normalize_name(r["fullName"]) == normalized):
            return i, "normalized"
    # 4. 可选模糊阈值
    if fuzzy_threshold is not None:
        best_i, best_ratio = None, 0.0
        for i in available:
            candidate = allure_results[i]["name"] or allure_results[i]["fullName"]
            if not candidate:
                continue
            ratio = SequenceMatcher(None, normalized, normalize_name(candidate)).ratio()
            if ratio > best_ratio:
                best_i, best_ratio = i, ratio
        if best_i is not None and best_ratio >= fuzzy_threshold:
            return best_i, f"fuzzy({best_ratio:.2f})"
    return None, "none"


def _compare(case: Dict[str, Any], result: Dict[str, Any], match_type: str) -> Dict[str, Any]:
    """单条比对：产出硬事实（预期通过但失败 / P0 skipped / 断言文本佐证）"""
    name = case["name"]
    expected = case.get("expected", "")
    priority = str(case.get("priority") or "").upper()
    status = result["status"]
    message = _detail_message(result)

    evidence_parts: List[str] = []
    if match_type != "exact":
        evidence_parts.append(f"匹配方式: {match_type}")

    if status == STATUS_PASSED:
        match = True
        match_state = MATCH_STATE_CONSISTENT
        evidence_parts.append("allure 状态 passed，与预期执行成功一致")
    elif status in (STATUS_FAILED, STATUS_BROKEN):
        match = False
        match_state = MATCH_STATE_MISMATCH
        evidence_parts.append(f"用例预期执行成功，allure 实际状态 {status}（agent 谎报或断言失效）")
        # 字符串包含级比对：断言文本中出现 expected 原文 → 佐证失败点即预期断言
        if expected:
            if expected in message:
                evidence_parts.append("statusDetails 断言文本包含 expected 原文，佐证失败点即预期断言")
            else:
                evidence_parts.append("statusDetails 断言文本未包含 expected 原文，失败点可能与预期断言无关")
    elif status == STATUS_SKIPPED:
        if priority == "P0":
            match = False
            match_state = MATCH_STATE_MISMATCH
            evidence_parts.append("P0 用例被 skipped，执行不完整")
        else:
            match = True
            match_state = MATCH_STATE_CONSISTENT
            evidence_parts.append(f"非 P0 用例被 skipped（状态 skipped，不计入不一致）")
    else:
        match = False
        match_state = MATCH_STATE_MISMATCH
        evidence_parts.append(f"allure 状态未知: {status or '(空)'}")

    if message:
        evidence_parts.append(f"statusDetails: {_truncate(message)}")

    return {
        "case": name,
        "expected": expected,
        "priority": priority,
        "actual_status": status or None,
        "match": match,
        "match_state": match_state,
        "match_type": match_type,
        "evidence": "；".join(evidence_parts),
    }


def normalize_name(text: str) -> str:
    """归一化：去全部空白 + 小写"""
    return _NORMALIZE_RE.sub("", text or "").lower()


def _detail_message(result: Dict[str, Any]) -> str:
    details = result.get("statusDetails") or {}
    if not isinstance(details, dict):
        return ""
    return str(details.get("message") or details.get("trace") or "")


def _truncate(text: str, limit: int = _EVIDENCE_MAX_LEN) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _write_detail(
    run_dir: Any,
    per_case: List[Dict[str, Any]],
    unmatched_allure: List[Dict[str, Any]],
    total: int,
    consistent: int,
    rate: float,
) -> Optional[Path]:
    """大明细写入运行目录（运行器提供 _run_dir），缺省回退临时目录"""
    detail = {
        "stats": {"total": total, "consistent": consistent, "consistency_rate": rate},
        "per_case": per_case,
        "unmatched_allure_results": [
            {"name": r["name"], "fullName": r["fullName"], "status": r["status"]}
            for r in unmatched_allure
        ],
    }
    try:
        if run_dir:
            base = Path(str(run_dir))
        else:
            base = Path(tempfile.mkdtemp(prefix="allure_vs_cases_"))
        base.mkdir(parents=True, exist_ok=True)
        target = base / "per_case_full.json"
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
