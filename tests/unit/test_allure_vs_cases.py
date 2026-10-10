"""内置预判脚本 allure_vs_cases 单元测试

覆盖：cases.json / testpoints.md 解析、allure 结果（目录/zip/单文件）加载、
四级匹配策略（exact → fullName 包含 → 归一化 → 模糊阈值）、
比对硬事实（失败、P0 skipped、断言文本佐证）、未匹配缺失、
大明细文件落盘、缺字段 fail 输出协议。
"""
import json
import zipfile

import pytest

from app.scripts_builtin import allure_vs_cases as avc


def _result(name, status, full_name="", message=""):
    return {
        "name": name,
        "fullName": full_name or name,
        "status": status,
        "statusDetails": {"message": message} if message else {},
    }


@pytest.fixture
def sample_env(tmp_path):
    """样例环境：5 条用例 + allure 结果目录（3 passed / 1 failed / 1 skipped）

    用例：login_pass(expect吻合) / search_fail / pay_p0_skip / report_normal_skip / missing_case
    """
    cases = [
        {"name": "login_success", "expected": "应显示登录成功", "priority": "P0"},
        {"name": "search_product", "expected": "应返回商品列表", "priority": "P1"},
        {"name": "pay_order", "expected": "应完成支付", "priority": "P0"},
        {"name": "report_export", "expected": "应导出报表", "priority": "P2"},
        {"name": "missing_case", "expected": "不应存在的用例", "priority": "P1"},
    ]
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    results_dir = tmp_path / "allure-results"
    results_dir.mkdir()
    results = [
        _result("login_success", "passed"),
        _result("search_product", "failed", message="AssertionError: 应返回商品列表 but was 空列表"),
        _result("pay_order", "skipped"),
        _result("report_export", "skipped"),
        _result("extra_result_not_in_cases", "passed"),
    ]
    for idx, r in enumerate(results):
        (results_dir / f"{idx}-result.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    return {"cases_file": str(cases_file), "allure_results": str(results_dir), "results_dir": results_dir, "results": results}


class TestParseCases:
    def test_parse_cases_json_list(self, tmp_path):
        f = tmp_path / "cases.json"
        f.write_text(json.dumps([{"name": "a", "expected": "x", "priority": "P0"}, {"id": "b"}]), encoding="utf-8")
        cases = avc.parse_cases_file(str(f))
        assert cases == [
            {"name": "a", "expected": "x", "priority": "P0"},
            {"name": "b", "expected": "", "priority": ""},
        ]

    def test_parse_cases_json_wrapped(self):
        text = json.dumps({"cases": [{"name": "a", "expected": "x"}]})
        assert avc.parse_cases_json(text)[0]["name"] == "a"

    def test_parse_cases_json_invalid_structure(self):
        with pytest.raises(ValueError):
            avc.parse_cases_json(json.dumps({"foo": "bar"}))

    def test_parse_testpoints_md(self, tmp_path):
        md = """# 回归测试点

## login_success
**预期结果**：应显示登录成功

## pay_order
### 预期
- 应完成支付
- 金额正确

## report_export
### 前置
已登录

### 预期结果
应导出报表
"""
        f = tmp_path / "testpoints.md"
        f.write_text(md, encoding="utf-8")
        cases = avc.parse_cases_file(str(f))
        names = [c["name"] for c in cases]
        assert names == ["login_success", "pay_order", "report_export"]
        assert cases[0]["expected"] == "应显示登录成功"
        assert cases[1]["expected"] == "应完成支付\n金额正确"
        # 「前置」小节不应混入预期
        assert "已登录" not in cases[2]["expected"]
        assert cases[2]["expected"] == "应导出报表"

    def test_parse_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            avc.parse_cases_file(str(tmp_path / "nope.json"))


class TestLoadAllureResults:
    def test_load_from_dir(self, sample_env):
        results = avc.load_allure_results(sample_env["allure_results"])
        assert len(results) == 5
        assert results[0]["status"] == "passed"

    def test_load_from_zip(self, sample_env, tmp_path):
        zip_path = tmp_path / "allure-results.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for idx, r in enumerate(sample_env["results"]):
                zf.writestr(f"allure-results/{idx}-result.json", json.dumps(r, ensure_ascii=False))
        results = avc.load_allure_results(str(zip_path))
        assert len(results) == 5
        assert {r["name"] for r in results} == {r["name"] for r in sample_env["results"]}

    def test_load_single_result_file(self, tmp_path):
        f = tmp_path / "3e9a1-result.json"
        f.write_text(json.dumps(_result("solo", "failed")), encoding="utf-8")
        results = avc.load_allure_results(str(f))
        assert len(results) == 1
        assert results[0]["name"] == "solo"

    def test_load_unsupported_type(self, tmp_path):
        other = tmp_path / "not_zip.txt"
        other.write_text("hello", encoding="utf-8")
        with pytest.raises(ValueError):
            avc.load_allure_results(str(other))

    def test_load_corrupt_json_skipped(self, tmp_path):
        d = tmp_path / "allure-results"
        d.mkdir()
        (d / "0-result.json").write_text("{broken", encoding="utf-8")
        (d / "1-result.json").write_text(json.dumps(_result("ok_case", "passed")), encoding="utf-8")
        results = avc.load_allure_results(str(d))
        assert [r["name"] for r in results] == ["ok_case"]


class TestMatchingStrategies:
    def _results(self):
        return [
            _result("login success", "passed", full_name="suite.login success"),
            _result("pay case v2", "passed", full_name="suite/pay case v2"),
        ]

    def test_exact_match(self):
        per_case, _ = avc.match_and_compare([{"name": "login success", "expected": ""}], self._results())
        assert per_case[0]["match_type"] == "exact"

    def test_full_name_contains(self):
        per_case, _ = avc.match_and_compare([{"name": "pay case", "expected": ""}],
                                            [_result("other", "passed", full_name="suite/pay case v2")])
        assert per_case[0]["match_type"] == "full_name_contains"
        assert per_case[0]["match"] is True

    def test_normalized_match(self):
        per_case, _ = avc.match_and_compare([{"name": "Login  SUCCESS", "expected": ""}],
                                            [_result("login success", "passed")])
        assert per_case[0]["match_type"] == "normalized"
        assert per_case[0]["match"] is True

    def test_fuzzy_threshold_match(self):
        results = [_result("user login with valid password", "passed")]
        per_case, _ = avc.match_and_compare(
            [{"name": "user login with valid passwords", "expected": ""}], results, fuzzy_threshold=0.9)
        assert per_case[0]["match_type"].startswith("fuzzy")

    def test_no_match_without_fuzzy(self):
        per_case, unmatched = avc.match_and_compare(
            [{"name": "完全不同的用例", "expected": ""}], self._results())
        assert per_case[0]["match"] is False
        assert per_case[0]["match_state"] == "missing"
        assert per_case[0]["actual_status"] is None
        assert len(unmatched) == 2


class TestCompareFacts:
    def test_expected_pass_but_failed(self):
        case = {"name": "a", "expected": "应返回商品列表", "priority": "P1"}
        result = _result("a", "failed", message="AssertionError: 应返回商品列表 but was []")
        item = avc._compare(case, result, "exact")
        assert item["match"] is False
        assert item["match_state"] == "mismatch"
        assert item["actual_status"] == "failed"
        # 断言文本包含 expected 原文 → 佐证失败点即预期断言
        assert "包含 expected 原文" in item["evidence"]

    def test_failure_message_without_expected_text(self):
        case = {"name": "a", "expected": "应返回商品列表", "priority": ""}
        result = _result("a", "broken", message="TimeoutError: page load 30s")
        item = avc._compare(case, result, "exact")
        assert item["match"] is False
        assert "未包含 expected 原文" in item["evidence"]

    def test_passed_consistent(self):
        item = avc._compare({"name": "a", "expected": "x", "priority": "P0"}, _result("a", "passed"), "exact")
        assert item["match"] is True and item["match_state"] == "consistent"

    def test_p0_skipped_is_mismatch(self):
        item = avc._compare({"name": "a", "expected": "x", "priority": "P0"}, _result("a", "skipped"), "exact")
        assert item["match"] is False
        assert "P0" in item["evidence"] and "skipped" in item["evidence"]

    def test_non_p0_skipped_consistent(self):
        item = avc._compare({"name": "a", "expected": "x", "priority": "P2"}, _result("a", "skipped"), "exact")
        assert item["match"] is True


class TestRunEndToEnd:
    def test_run_with_dir(self, sample_env, tmp_path):
        run_dir = tmp_path / "run"
        out = avc.run({"fields": sample_env, "params": {}, "_run_dir": str(run_dir)})

        assert out["status"] == "ok"
        assert out["finish_reason"] == "completed"
        assert out["error"] is None
        output = out["output"]
        # 5 条用例：login passed 一致、report_export 非 P0 skipped 一致；
        # search failed、pay(P0) skipped、missing_case 缺失 = 3 不一致
        assert output["consistency_rate"] == 0.4
        assert output["mismatched_count"] == 3
        assert "2/5" in output["summary_text"]
        # P0 skipped 硬信号
        assert output["p0_skipped"] == ["pay_order"]
        # per_case 字段契约
        item = next(c for c in output["per_case"] if c["case"] == "search_product")
        assert item["match"] is False and item["actual_status"] == "failed" and "evidence" in item
        # 大明细文件注册进 files 且内容完整
        detail_file = out["files"]["per_case_full.json"]
        assert detail_file.startswith(str(run_dir))
        detail = json.loads(open(detail_file, encoding="utf-8").read())
        assert detail["stats"]["total"] == 5
        # 未匹配 allure 清单
        assert any(u["name"] == "extra_result_not_in_cases" for u in detail["unmatched_allure_results"])

    def test_run_with_zip_input(self, sample_env, tmp_path):
        zip_path = tmp_path / "allure.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for idx, r in enumerate(sample_env["results"]):
                zf.writestr(f"{idx}-result.json", json.dumps(r, ensure_ascii=False))
        out = avc.run({"fields": {"cases_file": sample_env["cases_file"], "allure_results": str(zip_path)},
                       "params": {}, "_run_dir": str(tmp_path / "run2")})
        assert out["status"] == "ok"
        assert out["output"]["mismatched_count"] == 3

    def test_missing_cases_field_fails(self):
        out = avc.run({"fields": {"allure_results": "/tmp/x"}, "params": {}})
        assert out["status"] == "fail"
        assert "cases_file" in out["error"]
        assert out["finish_reason"] == "error"

    def test_missing_allure_field_fails(self):
        out = avc.run({"fields": {"cases_file": "/tmp/x"}, "params": {}})
        assert out["status"] == "fail"
        assert "allure_results" in out["error"]

    def test_empty_cases_file_fails(self, tmp_path):
        f = tmp_path / "cases.json"
        f.write_text("[]", encoding="utf-8")
        out = avc.run({"fields": {"cases_file": str(f), "allure_results": str(tmp_path)}, "params": {}})
        assert out["status"] == "fail"
        assert "为空" in out["error"]

    def test_run_without_run_dir_falls_back_to_tempfile(self, sample_env):
        out = avc.run({"fields": sample_env, "params": {}})
        assert out["status"] == "ok"
        assert out["files"]["per_case_full.json"]


class TestRunHubInputsChannel:
    """缺陷B3：hub_file 通道值注入 stdin.inputs，allure_vs_cases 兼容结构化输入

    - {root, files: [...]}（path_pattern 模式）与 {root, index_path}（索引模式）
      均可解析；旧编排（fields 路径字符串）回退兼容。
    """

    def _hub_value_files_mode(self, sample_env):
        """path_pattern 模式：files 明细只筛 rel_path 以 -result.json 结尾的项"""
        return {
            "root": str(sample_env["results_dir"]),
            "files": [
                {"path": str(sample_env["results_dir"] / f"{idx}-result.json"),
                 "rel_path": f"{idx}-result.json", "size": 10, "type": "text"}
                for idx in range(len(sample_env["results"]))
            ] + [{"path": "noise.txt", "rel_path": "noise.txt", "size": 1, "type": "text"}],
        }

    def test_run_with_inputs_files_mode(self, sample_env, tmp_path):
        """inputs.allure_results 为 {root, files}：与 fields 路径结果一致"""
        out = avc.run({
            "fields": {"cases_file": sample_env["cases_file"]},
            "inputs": {"allure_results": self._hub_value_files_mode(sample_env)},
            "params": {},
            "_run_dir": str(tmp_path / "run"),
        })
        assert out["status"] == "ok"
        assert out["output"]["consistency_rate"] == 0.4  # 与 test_run_with_dir 同基线
        assert out["output"]["mismatched_count"] == 3

    def test_run_with_inputs_index_mode(self, sample_env, tmp_path):
        """inputs.allure_results 为 {root, index_path}：读索引筛选 *-result.json"""
        index_path = tmp_path / "hub_pkg_index.json"
        index_path.write_text(json.dumps([
            {"path": str(sample_env["results_dir"] / f"{idx}-result.json"),
             "rel_path": f"{idx}-result.json", "size": 10, "type": "text"}
            for idx in range(len(sample_env["results"]))
        ] + [{"path": "noise.txt", "rel_path": "logs/run.log", "size": 1, "type": "text"}],
            ensure_ascii=False), encoding="utf-8")
        out = avc.run({
            "fields": {"cases_file": sample_env["cases_file"]},
            "inputs": {"allure_results": {
                "root": str(sample_env["results_dir"]), "index_path": str(index_path)}},
            "params": {},
            "_run_dir": str(tmp_path / "run"),
        })
        assert out["status"] == "ok"
        assert out["output"]["mismatched_count"] == 3

    def test_inputs_takes_precedence_over_fields(self, sample_env, tmp_path):
        """inputs 与 fields 同时携带时优先 inputs（新契约），fields 值被忽略"""
        out = avc.run({
            "fields": {"cases_file": sample_env["cases_file"],
                       "allure_results": "/nonexistent/path"},  # 若被采用会 fail
            "inputs": {"allure_results": self._hub_value_files_mode(sample_env)},
            "params": {},
            "_run_dir": str(tmp_path / "run"),
        })
        assert out["status"] == "ok"

    def test_fields_path_string_still_works(self, sample_env, tmp_path):
        """旧契约回归：fields 路径字符串（目录/zip/单文件）不受影响"""
        out = avc.run({"fields": sample_env, "params": {}, "_run_dir": str(tmp_path / "run")})
        assert out["status"] == "ok"
