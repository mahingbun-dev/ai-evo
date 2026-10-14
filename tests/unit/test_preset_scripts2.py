"""二期预置脚本与扩展点单元测试（设计文档 §九.3 / §十二）

覆盖：
1. xml_diff：一致/节点增删/属性增删改/文本变化、一致率口径、ignore_whitespace、
   max_diffs 截断、产物 diff_full.json、非法 XML/缺字段 fail
2. log_grep：关键字与正则两类模式、命中统计与样例行截断、大小写开关、
   fail_on_no_match、非法正则/缺文件 fail、产物 grep_full.json
3. db_assert：SELECT/WITH 白名单（多语句/非查询拒绝）、row_count/scalar/first_row/
   contains_row 四类断言、通过率统计、fail_fast、SQL 执行异常、断言清单缺失字段
4. 扩展点规范化：EXTENSION_POINT_* 常量、evaluation/result 观察管线
   （run_result_observers 只读保证、observations 附加、未注册观察者/异常降级）、
   execute_batch 聚合完成后挂接（未配置时零行为变化）
"""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.scripts_builtin import db_assert
from app.scripts_builtin.db_assert import run as db_assert_run
from app.scripts_builtin.db_assert import validate_readonly_sql
from app.scripts_builtin.log_grep import run as log_grep_run
from app.scripts_builtin.xml_diff import run as xml_diff_run
from app.services import pre_judge_runner
from app.services.evaluation_executor import EvaluationExecutor
from app.services.pre_judge_script_service import (
    BUILTIN_SCRIPT_DEFS,
    EXTENSION_POINT_PRE_JUDGE,
    EXTENSION_POINT_RESULT,
    PreJudgeScriptService,
)

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- 工具 ----------------

def _write(tmp_path: Path, name: str, content: str) -> str:
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


# ==================== 1. xml_diff ====================

class TestXmlDiff:
    def _xml_a(self):
        return '<root a="1"><item id="1"><name>A</name></item><item id="2"/></root>'

    def _xml_b_same(self):
        return '<root a="1"><item id="1"><name>A</name></item><item id="2"/></root>'

    def test_identical(self, tmp_path):
        pa = _write(tmp_path, "a.xml", self._xml_a())
        pb = _write(tmp_path, "b.xml", self._xml_b_same())
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb}, "params": {}})
        assert out["status"] == "ok"
        assert out["output"]["identical"] is True
        assert out["output"]["diff_count"] == 0
        assert out["output"]["consistency_rate"] == 1.0
        assert out["summary"] == "两份 XML 完全一致"

    def test_node_added_and_removed(self, tmp_path):
        pa = _write(tmp_path, "a.xml", "<root><keep/><gone/></root>")
        pb = _write(tmp_path, "b.xml", "<root><keep/><fresh/></root>")
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb}, "params": {}})
        types = sorted(d["type"] for d in out["output"]["diffs"])
        assert types == ["node_added", "node_removed"]
        removed = next(d for d in out["output"]["diffs"] if d["type"] == "node_removed")
        assert removed["tag"] == "gone"
        added = next(d for d in out["output"]["diffs"] if d["type"] == "node_added")
        assert added["tag"] == "fresh"

    def test_attribute_and_text_changes(self, tmp_path):
        pa = _write(tmp_path, "a.xml", '<root v="1"><n>old</n></root>')
        pb = _write(tmp_path, "b.xml", '<root v="2" extra="x"><n>new</n></root>')
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb}, "params": {}})
        types = sorted(d["type"] for d in out["output"]["diffs"])
        assert types == ["attribute_added", "attribute_changed", "text_changed"]
        changed = next(d for d in out["output"]["diffs"] if d["type"] == "attribute_changed")
        assert (changed["attribute"], changed["a"], changed["b"]) == ("v", "1", "2")

    def test_consistency_rate_metric(self, tmp_path):
        """一致率 = 1 - 差异事件数 / 两棵树节点总数"""
        pa = _write(tmp_path, "a.xml", self._xml_a())  # 4 节点
        pb = _write(tmp_path, "b.xml", self._xml_b_same().replace("<name>A</name>", "<name>B</name>"))
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb}, "params": {}})
        assert out["output"]["total_nodes"] == 8
        assert out["output"]["diff_count"] == 1
        assert out["output"]["consistency_rate"] == round(1 - 1 / 8, 4)

    def test_ignore_whitespace(self, tmp_path):
        pa = _write(tmp_path, "a.xml", "<root><n>  hello   world  </n></root>")
        pb = _write(tmp_path, "b.xml", "<root><n>hello world</n></root>")
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb}, "params": {}})
        assert out["output"]["identical"] is True
        # 关闭空白归一：文本差异暴露
        out2 = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb},
                             "params": {"ignore_whitespace": False}})
        assert [d["type"] for d in out2["output"]["diffs"]] == ["text_changed"]

    def test_max_diffs_truncates_output_only(self, tmp_path):
        """同标签子元素配对：5 处属性差异，max_diffs=2 只截断标准输出展示"""
        pa = _write(tmp_path, "a.xml", '<root><n v="1"/><n v="2"/><n v="3"/><n v="4"/><n v="5"/></root>')
        pb = _write(tmp_path, "b.xml", '<root><n v="9"/><n v="9"/><n v="9"/><n v="9"/><n v="9"/></root>')
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb},
                            "params": {"max_diffs": 2}})
        assert out["output"]["diff_count"] == 5
        assert len(out["output"]["diffs"]) == 2
        assert out["output"]["diff_truncated"] is True
        # 全量明细落盘产物
        detail = json.loads(Path(out["files"]["diff_full.json"]).read_text(encoding="utf-8"))
        assert len(detail["diffs"]) == 5

    def test_missing_field_and_invalid_xml_fail(self, tmp_path):
        assert xml_diff_run({"fields": {}, "params": {}})["status"] == "fail"
        pa = _write(tmp_path, "a.xml", "<root><a></root>")
        pb = _write(tmp_path, "b.xml", "<root/>")
        out = xml_diff_run({"fields": {"xml_a": pa, "xml_b": pb}, "params": {}})
        assert out["status"] == "fail"
        assert "合法 XML" in out["error"]

    def test_registered_in_defs(self):
        """统一子进程执行：种子元数据 exec_type=script、entrypoint=main.py（迁移落库源）"""
        assert "xml_diff" in BUILTIN_SCRIPT_DEFS
        assert BUILTIN_SCRIPT_DEFS["xml_diff"]["exec_type"] == "script"
        assert BUILTIN_SCRIPT_DEFS["xml_diff"]["entrypoint"] == "main.py"


# ==================== 2. log_grep ====================

class TestLogGrep:
    LOG = "INFO start\nERROR db timeout\nWARN slow query detected\nERROR panic now\nINFO done\n"

    def test_keyword_and_regex_stats(self, tmp_path):
        log = _write(tmp_path, "app.log", self.LOG)
        out = log_grep_run({"fields": {"log_file": log},
                            "params": {"keywords": ["ERROR"], "patterns": [r"slow.*query"]}})
        assert out["status"] == "ok"
        assert out["output"]["total_lines"] == 5
        assert out["output"]["total_hits"] == 3
        by_label = {p["pattern"]: p for p in out["output"]["patterns"]}
        assert by_label["ERROR"]["type"] == "keyword"
        assert by_label["ERROR"]["match_count"] == 2
        assert by_label["ERROR"]["hit_rate"] == round(2 / 5, 4)
        assert [s["line_no"] for s in by_label["ERROR"]["sample_lines"]] == [2, 4]
        assert by_label[r"slow.*query"]["type"] == "regex"
        assert by_label[r"slow.*query"]["match_count"] == 1
        assert "db timeout" in by_label["ERROR"]["sample_lines"][0]["line"]

    def test_case_insensitive_by_default(self, tmp_path):
        log = _write(tmp_path, "app.log", "error x\nError y\nok\n")
        out = log_grep_run({"fields": {"log_file": log}, "params": {"keywords": ["error"]}})
        assert out["output"]["patterns"][0]["match_count"] == 2
        # 大小写敏感后只命中小写行
        out2 = log_grep_run({"fields": {"log_file": log},
                             "params": {"keywords": ["error"], "case_sensitive": True}})
        assert out2["output"]["patterns"][0]["match_count"] == 1

    def test_no_match_ok_by_default(self, tmp_path):
        log = _write(tmp_path, "app.log", "clean\n")
        out = log_grep_run({"fields": {"log_file": log}, "params": {"keywords": ["ERROR"]}})
        assert out["status"] == "ok"
        assert out["output"]["total_hits"] == 0
        assert "零命中" in out["summary"]

    def test_fail_on_no_match(self, tmp_path):
        log = _write(tmp_path, "app.log", "clean\n")
        out = log_grep_run({"fields": {"log_file": log},
                            "params": {"keywords": ["ERROR"], "fail_on_no_match": True}})
        assert out["status"] == "fail"
        assert "fail_on_no_match" in out["error"]

    def test_sample_lines_truncation(self, tmp_path):
        log = _write(tmp_path, "app.log", "hit\n" * 10)
        out = log_grep_run({"fields": {"log_file": log},
                            "params": {"keywords": ["hit"], "max_samples": 3}})
        p = out["output"]["patterns"][0]
        assert p["match_count"] == 10
        assert len(p["sample_lines"]) == 3
        assert p["sample_truncated"] is True
        # 产物保留全量（上限 1000）
        detail = json.loads(Path(out["files"]["grep_full.json"]).read_text(encoding="utf-8"))
        assert len(detail["patterns"][0]["lines"]) == 10

    def test_invalid_inputs_fail(self, tmp_path):
        assert log_grep_run({"fields": {}, "params": {}})["status"] == "fail"
        log = _write(tmp_path, "app.log", "x\n")
        out = log_grep_run({"fields": {"log_file": log}, "params": {"patterns": ["([bad"]}})
        assert out["status"] == "fail"
        assert "非法正则" in out["error"]
        out2 = log_grep_run({"fields": {"log_file": str(tmp_path / "ghost.log")},
                             "params": {"keywords": ["e"]}})
        assert out2["status"] == "fail"
        assert "不存在" in out2["error"]

    def test_registered_in_defs(self):
        assert "log_grep" in BUILTIN_SCRIPT_DEFS
        assert BUILTIN_SCRIPT_DEFS["log_grep"]["exec_type"] == "script"


# ==================== 3. db_assert ====================

class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def fetchmany(self, n):
        batch, self._rows = self._rows[:n], self._rows[n:]
        return batch


class FakeSession:
    """最小 DB 连接替身：按 SQL 文本回配行；error_on 命中时抛异常"""

    def __init__(self, rows_by_sql=None, error_on=None):
        self.executed: list = []
        self._rows_by_sql = rows_by_sql or {}
        self._error_on = error_on

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
        if self._error_on and self._error_on in sql:
            raise RuntimeError("db boom")
        return FakeResult(self._rows_by_sql.get(sql, []))


class FakeEngine:
    """engine 替身：connect() 返回连接替身（FakeSession 同接口）"""

    def __init__(self, conn):
        self._conn = conn
        self.disposed = False

    def connect(self):
        return self._conn

    def dispose(self):
        self.disposed = True


class TestValidateReadonlySql:
    def test_select_and_cte_allowed(self):
        assert validate_readonly_sql("SELECT * FROM t") == (True, "")
        assert validate_readonly_sql("  select 1") == (True, "")
        assert validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x") == (True, "")
        assert validate_readonly_sql("-- comment\nSELECT 1") == (True, "")

    def test_non_select_rejected(self):
        for bad in ("DELETE FROM t", "UPDATE t SET a=1", "INSERT INTO t VALUES (1)",
                    "DROP TABLE t", "TRUNCATE t"):
            allowed, reason = validate_readonly_sql(bad)
            assert not allowed
            assert "SELECT" in reason
        allowed, reason = validate_readonly_sql("")
        assert not allowed
        assert "SQL 为空" in reason

    def test_multi_statement_rejected(self):
        allowed, reason = validate_readonly_sql("SELECT 1; DROP TABLE t")
        assert not allowed
        assert "多条语句" in reason
        # 结尾分号（无后续内容）允许
        assert validate_readonly_sql("SELECT 1;") == (True, "")

    def test_column_named_update_not_rejected(self):
        """防误伤：列名/表名含敏感词不拦截（只看首关键字与多语句）"""
        assert validate_readonly_sql("SELECT update_time FROM t") == (True, "")


class TestDbAssertRun:
    def _assertions_file(self, tmp_path, assertions) -> str:
        return _write(tmp_path, "assertions.json", json.dumps({"assertions": assertions}, ensure_ascii=False))

    def _stdin(self, tmp_path, assertions, params=None):
        # W1-3：只读 DSN 由平台经 stdin 注入（db_dsn），脚本自建 engine
        return {"fields": {"assertions": self._assertions_file(tmp_path, assertions)},
                "db_dsn": "sqlite:///:memory:", "params": params or {}}

    def _patched(self, rows_by_sql=None, error_on=None):
        session = FakeSession(rows_by_sql=rows_by_sql, error_on=error_on)
        engine = FakeEngine(session)
        return session, patch.object(db_assert, "_create_engine", lambda dsn: engine)

    def test_row_count_assert(self, tmp_path):
        sql = "SELECT id FROM orders WHERE status = 'failed'"
        rows = [{"id": 1}, {"id": 2}, {"id": 3}]
        session, patcher = self._patched(rows_by_sql={sql: rows})
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "失败订单数", "sql": sql, "expected": {"row_count": 3}},
                {"name": "行数不符", "sql": sql, "expected": {"row_count": 99}},
            ]))
        assert out["status"] == "fail"  # 第二条断言未通过
        assert out["output"]["passed"] == 1 and out["output"]["failed"] == 1
        assert out["output"]["consistency_rate"] == 0.5
        assert out["output"]["results"][0]["passed"] is True
        second = out["output"]["results"][1]
        assert second["passed"] is False
        assert "row_count 期望 99，实际 3" in second["error"]
        # 只读事务语句与查询都到达会话
        assert any("READ ONLY" in s for s in session.executed)
        assert any("SELECT id FROM orders" in s for s in session.executed)

    def test_scalar_first_row_contains_row(self, tmp_path):
        rows = [{"cnt": 5}, {"cnt": 9}]
        sql = "SELECT count(*) AS cnt FROM t"
        session, patcher = self._patched(rows_by_sql={sql: rows})
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "计数=5", "sql": sql, "expected": {"scalar": 5}},
                {"name": "计数字符串宽容", "sql": sql, "expected": {"scalar": "5"}},
                {"name": "首行子集", "sql": sql, "expected": {"first_row": {"cnt": 5}}},
                {"name": "包含行", "sql": sql, "expected": {"contains_row": {"cnt": 9}}},
                {"name": "包含行不匹配", "sql": sql, "expected": {"contains_row": {"cnt": 42}}},
            ]))
        assert out["output"]["passed"] == 4
        failed = out["output"]["results"][4]
        assert failed["passed"] is False
        assert "contains_row" in failed["error"]

    def test_empty_result_first_row_fails(self, tmp_path):
        sql = "SELECT * FROM empty_table"
        session, patcher = self._patched(rows_by_sql={sql: []})
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "空结果", "sql": sql, "expected": {"first_row": {"a": 1}}},
            ]))
        assert out["output"]["results"][0]["passed"] is False
        assert "结果为空" in out["output"]["results"][0]["error"]

    def test_non_select_sql_rejected_per_assertion(self, tmp_path):
        session, patcher = self._patched()
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "写操作", "sql": "DELETE FROM t", "expected": {"row_count": 0}},
                {"name": "多语句", "sql": "SELECT 1; DROP TABLE t", "expected": {"row_count": 0}},
            ]))
        assert all(not r["passed"] for r in out["output"]["results"])
        assert "只读校验拒绝" in out["output"]["results"][0]["error"]
        assert "多条语句" in out["output"]["results"][1]["error"]
        # 被拒 SQL 未到达会话执行
        assert session.executed == []

    def test_sql_execution_error_recorded(self, tmp_path):
        session, patcher = self._patched(error_on="boom_table")
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "坏查询", "sql": "SELECT * FROM boom_table", "expected": {"row_count": 0}},
            ]))
        assert out["output"]["results"][0]["passed"] is False
        assert "SQL 执行失败" in out["output"]["results"][0]["error"]

    def test_fail_fast_stops_after_first_failure(self, tmp_path):
        sql = "SELECT 1 AS v"
        rows = [{"v": 1}]
        session, patcher = self._patched(rows_by_sql={sql: rows})
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "首条失败", "sql": sql, "expected": {"scalar": 999}},
                {"name": "不应执行", "sql": sql, "expected": {"scalar": 1}},
            ], params={"fail_fast": True}))
        assert len(out["output"]["results"]) == 1
        assert out["output"]["results"][0]["passed"] is False

    def test_max_rows_caps_fetch(self, tmp_path):
        sql = "SELECT id FROM big"
        rows = [{"id": i} for i in range(50)]
        session, patcher = self._patched(rows_by_sql={sql: rows})
        with patcher:
            out = db_assert_run(self._stdin(tmp_path, [
                {"name": "截断行数", "sql": sql, "expected": {"row_count": 50}},
            ], params={"max_rows": 10}))
        # 超限行被丢弃（防拖库），row_count 类断言据实报失败
        assert out["output"]["results"][0]["actual"]["row_count"] == 10
        assert out["output"]["results"][0]["actual"]["truncated"] is True

    def test_bad_assertion_files_fail(self, tmp_path):
        assert db_assert_run({"fields": {}, "params": {}})["status"] == "fail"
        out = db_assert_run({"fields": {"assertions": str(tmp_path / "ghost.json")}, "params": {}})
        assert out["status"] == "fail" and "不存在" in out["error"]
        bad_json = _write(tmp_path, "bad.json", "{oops")
        out2 = db_assert_run({"fields": {"assertions": bad_json}, "params": {}})
        assert out2["status"] == "fail" and "解析失败" in out2["error"]
        no_sql = self._assertions_file(tmp_path, [{"name": "缺sql", "expected": {"row_count": 0}}])
        out3 = db_assert_run({"fields": {"assertions": no_sql}, "params": {}})
        assert out3["status"] == "fail" and "缺少 sql" in out3["error"]

    def test_missing_db_dsn_fails_clearly(self, tmp_path):
        """W1-3：断言清单合法但未注入 db_dsn（只读账号未配置）→ 明确报错，不回退平台库"""
        assertions = self._assertions_file(tmp_path, [
            {"name": "行数", "sql": "SELECT 1", "expected": {"row_count": 1}}])
        out = db_assert_run({"fields": {"assertions": assertions}, "params": {}})
        assert out["status"] == "fail"
        assert "db_dsn" in out["error"] and "DB_ASSERT_READONLY_DSN" in out["error"]

    def test_registered_in_defs(self):
        assert "db_assert" in BUILTIN_SCRIPT_DEFS
        assert BUILTIN_SCRIPT_DEFS["db_assert"]["exec_type"] == "script"


class TestDbAssertViaPipeline:
    def test_inline_assertions_via_runner_subprocess(self, tmp_path, monkeypatch):
        """集成（真实子进程）：inline 断言 JSON 落地为本地路径后由 db_assert 消费。

        W1-3：子进程环境变量白名单（无 DATABASE_URL），只读 DSN 由平台经
        stdin.db_dsn 注入（Settings.DB_ASSERT_READONLY_DSN → sqlite 临时库），
        db_assert 自建 engine 执行——端到端验证 DSN 定向注入链路可用。
        """
        import sqlite3
        from types import SimpleNamespace as _NS

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.db.session import Base
        from app.services import pre_judge_script_service as script_service
        from app.services.pre_judge_script_service import ensure_builtin_scripts_migrated

        upload_base = tmp_path / "pre_judge_scripts"
        monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", upload_base)
        monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")

        # 断言目标库：sqlite 临时文件库（子进程经 stdin.db_dsn 自建 engine 访问）
        db_file = tmp_path / "assert_db.sqlite"
        conn = sqlite3.connect(db_file)
        conn.execute("CREATE TABLE t (id INTEGER, ok INTEGER)")
        conn.execute("INSERT INTO t VALUES (7, 0)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(pre_judge_runner, "get_settings",
                            lambda: _NS(DB_ASSERT_READONLY_DSN=f"sqlite:///{db_file}"))

        # 主进程侧 runner.SessionLocal：内存库放迁移后的脚本记录
        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            ensure_builtin_scripts_migrated(db)
        monkeypatch.setattr(pre_judge_runner, "SessionLocal", Session)

        sql = "SELECT id FROM t WHERE ok = false"
        task = SimpleNamespace(id="task_dbx", pre_scripts=[{
            "script_id": "builtin:db_assert",
            "input_mapping": {"assertions": {"field": "assertions", "value_type": "inline"}},
            "params": {},
            "output_var": "db_check",
        }])
        item = {"id": "d1", "data": {"assertions": {"assertions": [
            {"name": "行数", "sql": sql, "expected": {"row_count": 1}},
        ]}}}
        out = pre_judge_runner.run_pre_scripts(task, payload={}, dataset_item=item)
        output = out["script_outputs"]["db_check"]
        assert out["pre_script_results"][0]["status"] == "ok"
        assert output["passed"] == 1 and output["total"] == 1
        assert output["consistency_rate"] == 1.0


# ==================== 4. 扩展点规范化与 evaluation/result ====================

class TestExtensionPointConstants:
    def test_mount_point_names(self):
        assert EXTENSION_POINT_PRE_JUDGE == "evaluation/pre-judge"
        assert EXTENSION_POINT_RESULT == "evaluation/result"
        # pre_judge_runner 再导出（调用方统一从 runner 取）
        assert pre_judge_runner.EXTENSION_POINT_PRE_JUDGE == "evaluation/pre-judge"
        assert pre_judge_runner.EXTENSION_POINT_RESULT == "evaluation/result"

    def test_example_noop_registered(self):
        """空实现示例已注册（本期唯一观察者），作为编写模板"""
        assert "example_noop" in pre_judge_runner.RESULT_OBSERVER_REGISTRY


class TestRunResultObservers:
    def _task(self, observers=None):
        return SimpleNamespace(id="task_obs_x", result_observers=observers)

    def test_no_observers_noop(self):
        """未配置观察者：results 原样返回、不新增 observations 键（回归兼容）"""
        results = [{"idx": 0, "success": True}]
        out = pre_judge_runner.run_result_observers(self._task(None), results)
        assert out is results
        assert "observations" not in results[0]

    def test_example_noop_appends_observations(self):
        """空实现示例：只读回读摘要，产出附加到每条 result.observations"""
        results = [{"idx": 0, "success": True, "average_score": 8},
                   {"idx": 1, "success": False, "error": "x"}]
        task = self._task([{"observer": "example_noop", "params": {}}])
        pre_judge_runner.run_result_observers(task, results)
        assert len(results[0]["observations"]) == 1
        entry = results[0]["observations"][0]
        assert entry["extension_point"] == "evaluation/result"
        assert entry["observer"] == "example_noop"
        assert entry["status"] == "ok"
        assert "第 0 条" in entry["summary"]
        assert results[1]["observations"][0]["summary"].endswith("average_score=None")

    def test_observer_readonly_snapshot(self):
        """只读保证：观察者修改 stdin.result 快照不影响原 result"""
        def _mutator(stdin):
            stdin["result"]["injected"] = True
            return {"status": "ok", "output": {}, "files": {}, "summary": "",
                    "finish_reason": "completed", "error": None}

        pre_judge_runner.register_result_observer("test_mutator", _mutator)
        try:
            results = [{"idx": 0, "success": True}]
            pre_judge_runner.run_result_observers(
                self._task([{"observer": "test_mutator"}]), results)
            assert "injected" not in results[0]
            assert results[0]["observations"][0]["status"] == "ok"
        finally:
            pre_judge_runner.RESULT_OBSERVER_REGISTRY.pop("test_mutator", None)

    def test_unknown_observer_fails_without_breaking(self):
        results = [{"idx": 0}]
        pre_judge_runner.run_result_observers(
            self._task([{"observer": "ghost_observer"}]), results)
        entry = results[0]["observations"][0]
        assert entry["status"] == "fail"
        assert "未注册" in entry["error"]

    def test_observer_exception_degrades_to_fail(self):
        def _boom(stdin):
            raise RuntimeError("observer boom")

        pre_judge_runner.register_result_observer("test_boom", _boom)
        try:
            results = [{"idx": 0}]
            pre_judge_runner.run_result_observers(
                self._task([{"observer": "test_boom"}]), results)
            entry = results[0]["observations"][0]
            assert entry["status"] == "fail"
            assert "observer boom" in entry["error"]
        finally:
            pre_judge_runner.RESULT_OBSERVER_REGISTRY.pop("test_boom", None)

    def test_multiple_observers_and_invalid_steps(self):
        """多观察者按序执行；非法编排项跳过并告警"""
        pre_judge_runner.register_result_observer(
            "test_counter", lambda stdin: {
                "status": "ok", "output": {"seen": stdin["result"]["idx"]},
                "files": {}, "summary": "", "finish_reason": "completed", "error": None})
        try:
            results = [{"idx": 3}]
            pre_judge_runner.run_result_observers(
                self._task(["bad_step", {"observer": "test_counter"}, {"observer": "example_noop"}]),
                results)
            observers = [o["observer"] for o in results[0]["observations"]]
            assert observers == ["test_counter", "example_noop"]
            assert results[0]["observations"][0]["output"] == {"seen": 3}
        finally:
            pre_judge_runner.RESULT_OBSERVER_REGISTRY.pop("test_counter", None)


# ==================== 5. execute_batch 挂接 ====================

def _make_task(pre_scripts=None, result_observers=None):
    return type("Task", (), {
        "id": "task_obs_batch",
        "evaluation_type": "judge",
        "judge_engine": "claude_agent",
        "judge_count": 1,
        "model_config": {"provider": "openai"},
        "model_provider": None,
        "max_score": 10,
        "evaluation_metrics": [{"name": "维度A"}],
        "judge_variable_config": {},
        "evaluation_targets": None,
        "runtime_variables": {},
        "variable_mappings": {},
        "variable_sources": {},
        "enable_user_import": False,
        "user_import_template": None,
        "input_field": "user_input",
        "reference_field": "answer",
        "pre_scripts": pre_scripts,
        "result_observers": result_observers,
    })()


def _mock_judge_stack():
    pb = patch("app.services.prompt_builder.PromptBuilder")
    cc = patch("app.services.claude_code_judge_agent.ClaudeCodeJudgeAgent")
    ws = patch("app.services.claude_code_judge_agent.write_session_file",
               return_value="/tmp/fake_session.md")
    lg = patch("app.services.judge_langgraph_agent.JudgeLangGraphAgent")
    return pb, cc, ws, lg


class TestExecuteBatchResultObserverHook:
    def _run_batch(self, task, tmp_path):
        executor = EvaluationExecutor(max_workers=1, task_id="task_obs_batch")
        payload = {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg, \
             patch("app.services.evaluation_task_service.EvaluationTaskService"), \
             patch("app.services.model_caller.ModelCallerSync") as mock_caller, \
             patch("app.services.pre_judge_runner.SCRIPT_RUN_BASE", tmp_path / "script_runs"):
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "模型输出", "chain_results": []}
            return executor.execute_batch([payload], task)

    def test_batch_appends_observations_when_configured(self, tmp_path):
        task = _make_task(result_observers=[{"observer": "example_noop"}])
        results = self._run_batch(task, tmp_path)
        assert results[0]["success"] is True
        observations = results[0]["observations"]
        assert len(observations) == 1
        assert observations[0]["extension_point"] == "evaluation/result"
        assert observations[0]["observer"] == "example_noop"

    def test_batch_without_observers_unchanged(self, tmp_path):
        """回归：未配置 result_observers 的任务批次结果不含 observations 键"""
        task = _make_task()
        results = self._run_batch(task, tmp_path)
        assert results[0]["success"] is True
        assert "observations" not in results[0]

    def test_batch_observer_pipeline_error_not_fatal(self, tmp_path):
        """观察管线自身异常只告警，不阻断 execute_batch 返回结果"""
        task = _make_task(result_observers=[{"observer": "example_noop"}])
        executor = EvaluationExecutor(max_workers=1, task_id="task_obs_batch")
        payload = {"idx": 0, "user_input": "q", "_dataset_item": {"id": "d1", "data": {}}}
        pb, cc, ws, lg = _mock_judge_stack()
        with pb as mock_pb, cc as mock_cc, ws, lg, \
             patch("app.services.evaluation_task_service.EvaluationTaskService"), \
             patch("app.services.model_caller.ModelCallerSync") as mock_caller, \
             patch("app.services.pre_judge_runner.SCRIPT_RUN_BASE", tmp_path / "script_runs"), \
             patch("app.services.pre_judge_runner.run_result_observers",
                   side_effect=RuntimeError("pipeline boom")):
            mock_pb.return_value.build_user_prompt_from_judge_vars.return_value = "user prompt"
            mock_pb.return_value._build_system_prompt.return_value = "system prompt"
            mock_cc.return_value.run_parallel.return_value = [
                {"score": 9, "feedback": "ok", "dimension_scores": {}}]
            mock_caller.return_value.call.return_value = {
                "success": True, "content": "模型输出", "chain_results": []}
            results = executor.execute_batch([payload], task)
        assert len(results) == 1 and results[0]["success"] is True
        assert "observations" not in results[0]


# ==================== 6. 预置脚本服务注册回归 ====================

class TestPresetRegistryRegression:
    def test_builtin_list_contains_all_presets(self):
        """列表元数据完整：一期 allure + 二期三预置，契约字段齐备"""
        service_defs = BUILTIN_SCRIPT_DEFS
        assert set(service_defs) >= {"allure_vs_cases", "xml_diff", "log_grep", "db_assert"}
        for name, definition in service_defs.items():
            # 统一子进程执行后 DEFS 是迁移源：exec_type=script、entrypoint=main.py
            assert definition["id"] == f"builtin:{name}"
            assert definition["exec_type"] == "script"
            assert definition["entrypoint"] == "main.py"
            assert "is_builtin" not in definition
            assert definition["input_fields"] and definition["output_schema"]

    def test_db_script_list_includes_presets(self, tmp_path, monkeypatch):
        """统一落库：迁移后 list_scripts（仅 DB 记录）包含预置脚本（前端任务编排下拉可见）"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db.session import Base
        from app.services import pre_judge_script_service as script_service
        from app.services.pre_judge_script_service import ensure_builtin_scripts_migrated

        monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", tmp_path / "pre_judge_scripts")
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as db:
            ensure_builtin_scripts_migrated(db)
            service = PreJudgeScriptService(db)
            items = service.list_scripts()
        names = {item["name"] for item in items}
        assert {"allure_vs_cases", "xml_diff", "log_grep", "db_assert"} <= names
        assert all("is_builtin" not in item for item in items)
