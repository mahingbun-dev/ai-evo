"""内置预判脚本：db_assert

SQL 查询 + 期望断言 JSON 的只读比对（设计：docs/design/pre-judge-scripts.md
§四 builtin 类型 / §十二 二期预置清单）：按平台注入的只读连接串
（stdin.db_dsn ← Settings.DB_ASSERT_READONLY_DSN，仅内置脚本记录获得）
自建 SQLAlchemy engine 执行查询，与期望断言逐条比对，输出通过率与
差异明细硬事实供裁判判断。

W1-3 整改：不再 import 平台 SessionLocal（脚本子进程环境变量白名单后
拿不到平台 DATABASE_URL，平台凭据也绝不下发）；数据库访问凭据只经
stdin 定向注入本脚本进程。

标准输入（stdin）：
    fields.assertions  断言清单 JSON 文件本地路径：
                        {"assertions": [
                          {"name": "订单落库", "sql": "SELECT ...", "expected": {...}},
                        ]}
    db_dsn             平台注入的只读数据库连接串（内置记录编排时注入；
                        缺失即明确报错，不回退平台库）
    params.fail_fast   可选，默认 false：首条断言失败后停止后续断言
    params.max_rows    可选，默认 1000：单条查询最多取回的行数（防全表拉爆）

期望断言 expected（可组合多键，全部满足才算通过）：
    {"row_count": 3}             结果行数恰好等于 N
    {"scalar": 5}                首行首列的值等于期望（常配 SELECT count(*)/聚合）
    {"first_row": {"k": "v"}}    首行包含给定键且值相等（行内其余键不参与比对）
    {"contains_row": {"k": "v"}} 任意一行包含给定键且值相等

安全边界（只读执行）：
1. SQL 白名单校验：仅允许单条 SELECT / WITH 开头语句；分号后还有内容
   （多语句）直接拒绝；
2. 只读事务：连接上尽力 SET TRANSACTION READ ONLY（PG/MySQL 支持，
   不支持的平台跳过）——主防线是上面的 SQL 校验；连接串本身应为只读账号
   （平台 DB_ASSERT_READONLY_DSN）；
3. 行数上限 max_rows，超限行丢弃（只影响 row_count 类断言的准确性，防拖库）。

标准输出：{status, output, files, summary, finish_reason, error}
    output.total / passed / failed / consistency_rate
    output.results  [{name, sql, passed, expected, actual, error}]
    files.db_assert_full.json  全量断言明细
"""
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAX_ROWS_DEFAULT = 1000

# 只读白名单：语句首关键字
_SELECT_RE = re.compile(r"\s*(select|with)\b", re.IGNORECASE)
# 首个 SQL 行注释/块注释的剥离
_LEADING_COMMENTS_RE = re.compile(r"(\s|--[^\n]*\n|/\*.*?\*/)+", re.IGNORECASE | re.DOTALL)

_EXPECTED_KEYS = ("row_count", "scalar", "first_row", "contains_row")


def run(stdin: Dict[str, Any]) -> Dict[str, Any]:
    """标准入口：(标准输入 dict) -> 标准输出 dict"""
    fields = stdin.get("fields") or {}
    params = stdin.get("params") or {}
    assertions_path = fields.get("assertions")
    if not assertions_path:
        return _fail("缺少输入字段 assertions（断言清单 JSON 文件路径）")

    try:
        assertions = _load_assertions(assertions_path)
    except _AssertionLoadError as e:
        return _fail(str(e))
    if not assertions:
        return _fail("断言清单为空（未解析到任何断言）")

    # W1-3：只读连接串由平台经 stdin 注入（未注入即明确失败，不回退平台库）
    dsn = str(stdin.get("db_dsn") or "").strip()
    if not dsn:
        return _fail(
            "缺少只读数据库连接（db_dsn）：平台未配置 DB_ASSERT_READONLY_DSN，"
            "或本脚本不是内置脚本记录（只读凭据仅注入内置 db_assert）"
        )

    fail_fast = bool(params.get("fail_fast", False))
    try:
        max_rows = max(1, int(params.get("max_rows") or _MAX_ROWS_DEFAULT))
    except (TypeError, ValueError):
        max_rows = _MAX_ROWS_DEFAULT

    results: List[Dict[str, Any]] = []
    engine = _create_engine(dsn)
    try:
        for assertion in assertions:
            entry = _run_assertion(assertion, max_rows, engine)
            results.append(entry)
            if fail_fast and not entry["passed"]:
                break
    finally:
        engine.dispose()

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    rate = round(passed / total, 4) if total else 0.0
    summary_text = f"{passed}/{total} 条数据库断言通过，{failed} 条失败"

    detail_path = _write_detail(stdin.get("_run_dir"), results, total, passed, rate)
    files: Dict[str, str] = {}
    if detail_path:
        files["db_assert_full.json"] = str(detail_path)

    return {
        "status": "ok" if failed == 0 else "fail",
        "output": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "consistency_rate": rate,
            "results": [
                {k: v for k, v in r.items() if k != "rows"} for r in results
            ],
        },
        "files": files,
        "summary": summary_text,
        "finish_reason": "completed" if failed == 0 else "assertion_failed",
        "error": None if failed == 0 else f"{failed} 条断言未通过",
    }


# ==================== 断言清单加载 ====================

class _AssertionLoadError(Exception):
    pass


def _load_assertions(path: Any) -> List[Dict[str, Any]]:
    p = Path(str(path))
    if not p.is_file():
        raise _AssertionLoadError(f"断言清单文件不存在: {path}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise _AssertionLoadError(f"断言清单 JSON 解析失败: {e}")
    if isinstance(raw, dict):
        raw = raw.get("assertions")
    if not isinstance(raw, list):
        raise _AssertionLoadError("断言清单结构不合法：期望 {assertions: [...]} 或数组")
    cleaned: List[Dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        sql = str(item.get("sql") or "").strip()
        if not sql:
            raise _AssertionLoadError(f"断言清单第 {i} 条缺少 sql")
        expected = item.get("expected")
        if not isinstance(expected, dict) or not expected:
            raise _AssertionLoadError(f"断言清单第 {i} 条缺少 expected 断言对象")
        cleaned.append({
            "name": str(item.get("name") or f"assertion_{i}"),
            "sql": sql,
            "expected": expected,
        })
    return cleaned


# ==================== SQL 只读校验 ====================

def validate_readonly_sql(sql: str) -> Tuple[bool, str]:
    """只读 SQL 白名单校验：单条 SELECT/WITH 语句，禁止多语句

    返回 (是否允许, 拒绝原因)；允许时原因为空串。
    """
    stripped = _LEADING_COMMENTS_RE.sub(lambda m: "\n" if "\n" in m.group(0) else " ", sql or "").strip()
    if not stripped:
        return False, "SQL 为空"
    if not _SELECT_RE.match(stripped):
        return False, "仅允许 SELECT / WITH 查询语句（只读）"
    # 分号后还有非空白内容 → 多语句，拒绝
    parts = stripped.split(";")
    tail = "".join(
        _LEADING_COMMENTS_RE.sub(" ", part).strip() for part in parts[1:]
    )
    if tail:
        return False, "检测到多条语句（分号后还有内容），仅允许单条 SELECT"
    return True, ""


def _rows_from_result(result: Any, max_rows: int) -> Tuple[List[Dict[str, Any]], bool]:
    """把查询结果行转为 dict 列表（最多 max_rows 行），返回 (rows, 是否被截断)"""
    mappings = result.mappings().fetchmany(max_rows)
    truncated = result.mappings().fetchmany(1) != []
    return [dict(row) for row in mappings], truncated


# ==================== 断言执行 ====================

def _create_engine(dsn: str) -> Any:
    """按 stdin.db_dsn 自建 SQLAlchemy engine（W1-3：凭据来自 stdin 定向注入，
    不再 import 平台 SessionLocal；延迟导入便于测试替换执行通道）"""
    from sqlalchemy import create_engine

    return create_engine(dsn)


def _run_assertion(assertion: Dict[str, Any], max_rows: int, engine: Any) -> Dict[str, Any]:
    """执行单条断言：校验 SQL → 只读执行 → 比对期望"""
    name = assertion["name"]
    sql = assertion["sql"]
    expected = assertion["expected"]

    allowed, reason = validate_readonly_sql(sql)
    if not allowed:
        return {"name": name, "sql": sql, "passed": False,
                "expected": expected, "actual": None, "error": f"SQL 被只读校验拒绝: {reason}"}

    unknown_keys = [k for k in expected if k not in _EXPECTED_KEYS]
    if unknown_keys:
        return {"name": name, "sql": sql, "passed": False, "expected": expected,
                "actual": None,
                "error": f"不支持的断言键: {unknown_keys}（支持 {', '.join(_EXPECTED_KEYS)}）"}

    try:
        with engine.connect() as conn:
            _begin_readonly_transaction(conn)
            result = conn.execute(_text(sql))
            rows, truncated = _rows_from_result(result, max_rows)
    except Exception as e:  # noqa: BLE001 - 断言边界：DB 异常转该条失败
        return {"name": name, "sql": sql, "passed": False,
                "expected": expected, "actual": None, "error": f"SQL 执行失败: {e}"}

    actual: Dict[str, Any] = {
        "row_count": len(rows),
        "truncated": truncated,
        "first_row": _row_public(rows[0]) if rows else None,
    }
    failed_checks = _check_expected(expected, rows)
    return {
        "name": name,
        "sql": sql,
        "passed": not failed_checks,
        "expected": expected,
        "actual": actual,
        "error": "；".join(failed_checks) if failed_checks else None,
    }


def _text(sql: str):
    """sqlalchemy.text 包装（延迟导入，便于测试替换执行通道）"""
    from sqlalchemy import text as sa_text

    return sa_text(sql)


def _begin_readonly_transaction(conn: Any) -> None:
    """尽力开启只读事务（PG/MySQL 支持；不支持的平台静默跳过）"""
    try:
        conn.execute(_text("SET TRANSACTION READ ONLY"))
    except Exception:  # noqa: BLE001 - 平台不支持只读事务时跳过（主防线是 SQL 白名单）
        pass


def _check_expected(expected: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[str]:
    """逐键比对期望，返回失败描述列表（空列表=全部通过）"""
    failures: List[str] = []

    if "row_count" in expected:
        want = expected["row_count"]
        if len(rows) != want:
            failures.append(f"row_count 期望 {want}，实际 {len(rows)}")

    if "scalar" in expected:
        want = expected["scalar"]
        first_cell = _first_cell(rows)
        if not _values_equal(first_cell, want):
            failures.append(f"scalar 期望 {_show(want)}，实际 {_show(first_cell)}")

    if "first_row" in expected:
        want = expected["first_row"] or {}
        if not rows:
            failures.append("first_row 期望有数据行，实际结果为空")
        else:
            mismatches = _subset_mismatches(want, rows[0])
            if mismatches:
                failures.append(f"first_row 不匹配: {mismatches}")

    if "contains_row" in expected:
        want = expected["contains_row"] or {}
        if not any(not _subset_mismatches(want, row) for row in rows):
            failures.append(f"contains_row 无任何行包含期望键值: {want}")

    return failures


def _subset_mismatches(want: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """want 键值子集与行比对：返回 {键: {expected, actual}} 不匹配清单"""
    mismatches: Dict[str, Dict[str, Any]] = {}
    for key, expected_value in want.items():
        if key not in row:
            mismatches[key] = {"expected": expected_value, "actual": "<缺失>"}
        elif not _values_equal(row[key], expected_value):
            mismatches[key] = {"expected": expected_value, "actual": row[key]}
    return mismatches


def _first_cell(rows: List[Dict[str, Any]]) -> Any:
    if not rows:
        return None
    first = rows[0]
    for value in first.values():
        return value
    return None


def _values_equal(actual: Any, expected: Any) -> bool:
    """宽容相等：数值跨 int/float/Decimal 比较，其余按 JSON 序列化字符串比较"""
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    if isinstance(actual, str) and isinstance(expected, (int, float)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    if isinstance(expected, str) and isinstance(actual, (int, float)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return _show(actual) == _show(expected)


def _row_public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _show(v) for k, v in row.items()}


def _show(value: Any) -> Any:
    """非基本类型转可读字符串（Decimal/datetime 等），供明细展示与比对"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ==================== 产物与错误 ====================

def _write_detail(
    run_dir: Any,
    results: List[Dict[str, Any]],
    total: int,
    passed: int,
    rate: float,
) -> Optional[Path]:
    """全量断言明细写入运行目录（运行器提供 _run_dir），缺省回退临时目录"""
    detail = {
        "stats": {"total": total, "passed": passed, "consistency_rate": rate},
        "results": results,
    }
    try:
        base = Path(str(run_dir)) if run_dir else Path(tempfile.mkdtemp(prefix="db_assert_"))
        base.mkdir(parents=True, exist_ok=True)
        target = base / "db_assert_full.json"
        target.write_text(json.dumps(detail, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 - 明细落盘失败不影响断言结论输出
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
