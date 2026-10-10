"""
测试 model_caller.py 中 chain_results 构建逻辑
"""
import unittest
import sys
import os
import json

# 将项目根目录添加到 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.model_caller import ModelCaller
from app.services.judge_client import _sanitize_json_string, _try_fix_truncated_json


class TestChainResultsConstruction(unittest.TestCase):
    """测试 chain_results 构建逻辑"""

    def test_chain_results_includes_request_vars(self):
        """
        测试当 request_details 中包含 request_vars 时，
        chain_result.variables 应该包含解析出的变量
        """
        # 构造模拟的 result，包含 request_details
        result = {
            "success": True,
            "request_details": [
                {
                    "order": 1,
                    "name": "预处理",
                    "vars_before": {},
                    "vars_after": {"prd_content": "", "message": ""},
                    "output_var": "prd_content",
                    "output_vars": None,
                    "output_value": "PRD文档内容",
                    "request_vars": {"prd_content": "PRD文档内容", "message": "消息内容"}
                },
                {
                    "order": 2,
                    "name": "生成测试用例",
                    "vars_before": {"prd_content": "PRD文档内容"},
                    "vars_after": {"prd_content": "PRD文档内容"},
                    "output_var": None,
                    "output_vars": ["testcase", "testcases"],
                    "output_value": None,
                    "request_vars": {"testcase": "生成的测试用例内容", "testcases": "所有测试用例"}
                }
            ]
        }

        # 手动模拟 _build_chain_results 的逻辑
        chain_results = []
        for idx, detail in enumerate(result["request_details"]):
            vars_after = detail.get("vars_after", {})
            output_var_name = detail.get("output_var", "")
            output_vars = detail.get("output_vars")
            request_vars = detail.get("request_vars", {})

            # 合并变量
            merged_vars = {**vars_after, **request_vars}

            # 将 output_var 也添加到 merged_vars 中
            output_value = detail.get("output_value", "")
            if output_var_name and output_value:
                merged_vars[output_var_name] = output_value

            # 如果有 output_vars（多变量模式），添加到 merged_vars
            if output_vars and isinstance(output_vars, list):
                for var_name, var_value in request_vars.items():
                    merged_vars[var_name] = var_value

            chain_result = {
                "chain_index": idx,
                "order": detail.get("order", idx + 1),
                "name": detail.get("name", f"请求{idx + 1}"),
                "output_var": output_var_name,
                "output_vars": output_vars,
                "content": output_value,
                "variables": merged_vars,
                "all_results": merged_vars
            }
            chain_results.append(chain_result)

        # 验证第一个请求
        self.assertEqual(chain_results[0]["name"], "预处理")
        self.assertIn("prd_content", chain_results[0]["variables"])
        self.assertEqual(chain_results[0]["variables"]["prd_content"], "PRD文档内容")

        # 验证第二个请求（SSE多变量模式）
        self.assertEqual(chain_results[1]["name"], "生成测试用例")
        self.assertIn("testcase", chain_results[1]["variables"])
        self.assertIn("testcases", chain_results[1]["variables"])
        self.assertEqual(chain_results[1]["variables"]["testcase"], "生成的测试用例内容")
        self.assertEqual(chain_results[1]["variables"]["testcases"], "所有测试用例")

    def test_chain_results_without_request_vars(self):
        """
        测试当 request_vars 为空时，chain_result.variables 应该使用 vars_after
        """
        result = {
            "success": True,
            "request_details": [
                {
                    "order": 1,
                    "name": "普通请求",
                    "vars_after": {"var1": "value1", "var2": "value2"},
                    "output_var": None,
                    "output_vars": None,
                    "output_value": None,
                    "request_vars": {}
                }
            ]
        }

        chain_results = []
        for idx, detail in enumerate(result["request_details"]):
            vars_after = detail.get("vars_after", {})
            output_var_name = detail.get("output_var", "")
            output_vars = detail.get("output_vars")
            request_vars = detail.get("request_vars", {})

            merged_vars = {**vars_after, **request_vars}

            output_value = detail.get("output_value", "")
            if output_var_name and output_value:
                merged_vars[output_var_name] = output_value

            if output_vars and isinstance(output_vars, list):
                for var_name, var_value in request_vars.items():
                    merged_vars[var_name] = var_value

            chain_result = {
                "chain_index": idx,
                "order": detail.get("order", idx + 1),
                "name": detail.get("name", f"请求{idx + 1}"),
                "output_var": output_var_name,
                "output_vars": output_vars,
                "content": output_value,
                "variables": merged_vars,
                "all_results": merged_vars
            }
            chain_results.append(chain_result)

        self.assertEqual(chain_results[0]["name"], "普通请求")
        self.assertEqual(chain_results[0]["variables"]["var1"], "value1")
        self.assertEqual(chain_results[0]["variables"]["var2"], "value2")

    def test_chain_results_sse_with_data_field(self):
        """
        测试 SSE 请求的 data_field 变量是否正确传递
        """
        result = {
            "success": True,
            "request_details": [
                {
                    "order": 1,
                    "name": "生成测试用例",
                    "vars_after": {},
                    "output_var": None,
                    "output_vars": None,
                    "output_value": None,
                    "request_vars": {"TestCasesGenerated": '{"test1": "case1"}', "testcase": "单条用例"}
                }
            ]
        }

        chain_results = []
        for idx, detail in enumerate(result["request_details"]):
            vars_after = detail.get("vars_after", {})
            output_var_name = detail.get("output_var", "")
            output_vars = detail.get("output_vars")
            request_vars = detail.get("request_vars", {})

            merged_vars = {**vars_after, **request_vars}

            output_value = detail.get("output_value", "")
            if output_var_name and output_value:
                merged_vars[output_var_name] = output_value

            if output_vars and isinstance(output_vars, list):
                for var_name, var_value in request_vars.items():
                    merged_vars[var_name] = var_value

            chain_result = {
                "chain_index": idx,
                "order": detail.get("order", idx + 1),
                "name": detail.get("name", f"请求{idx + 1}"),
                "output_var": output_var_name,
                "output_vars": output_vars,
                "content": output_value,
                "variables": merged_vars,
                "all_results": merged_vars
            }
            chain_results.append(chain_result)

        self.assertIn("TestCasesGenerated", chain_results[0]["variables"])
        self.assertIn("testcase", chain_results[0]["variables"])


class TestResponseParserMultiple(unittest.TestCase):
    """测试 ResponseParser.parse_multiple 对 SSE events 格式的解析"""

    def test_parse_multiple_with_sse_events_format(self):
        """
        测试 parse_multiple 能否正确解析 SSE events 格式
        SSE events: [{"event": "xxx", "data": {...}}, ...]
        """
        from app.services.response_parser import ResponseParser

        # SSE events 格式的响应
        sse_events = [
            {"event": "start", "data": {"status": "processing"}},
            {"event": "data", "data": {"TestCasesGenerated": '{"items": []}', "testcase": "用例1"}},
            {"event": "end", "data": {"status": "completed"}}
        ]

        # 多变量配置
        output_vars = [
            {"name": "TestCasesGenerated", "parse_type": "json_path", "parse_rule": {"content": "$.TestCasesGenerated"}},
            {"name": "testcase", "parse_type": "json_path", "parse_rule": {"content": "$.testcase"}}
        ]

        # 合并所有 events 的 data
        merged_data = {}
        for event in sse_events:
            if isinstance(event, dict) and "data" in event:
                event_data = event.get("data", {})
                if isinstance(event_data, dict):
                    merged_data.update(event_data)
        response_data = merged_data

        # 解析
        result = ResponseParser.parse_multiple(response_data, output_vars)

        self.assertIn("TestCasesGenerated", result)
        self.assertIn("testcase", result)
        self.assertEqual(result["TestCasesGenerated"], '{"items": []}')
        self.assertEqual(result["testcase"], "用例1")

    def test_parse_multiple_with_simple_format(self):
        """
        测试简单格式 output_vars 的解析（字符串数组）
        """
        from app.services.response_parser import ResponseParser

        # 普通 JSON 响应
        response_data = {
            "TestCasesGenerated": '{"items": ["case1", "case2"]}',
            "testcase": "单条测试用例"
        }

        # 简单格式（字符串数组）
        output_vars = ["TestCasesGenerated", "testcase"]

        # 转换为完整格式
        parsed_output_vars = [
            {"name": v, "parse_type": "json_path", "parse_rule": {"content": f"$.{v}"}}
            for v in output_vars
        ]

        result = ResponseParser.parse_multiple(response_data, parsed_output_vars)

        self.assertIn("TestCasesGenerated", result)
        self.assertIn("testcase", result)


class TestChainResultVariableExtraction(unittest.TestCase):
    """测试从 chain_result 中提取变量的逻辑"""

    def test_extract_sse_variable_by_name(self):
        """
        测试通过请求名称（而非变量名）提取 SSE 请求的内容
        """
        chain_results = [
            {
                "chain_index": 0,
                "order": 1,
                "name": "预处理",
                "variables": {"prd_content": "PRD内容", "message": "消息"}
            },
            {
                "chain_index": 1,
                "order": 2,
                "name": "生成测试用例",
                "variables": {"TestCasesGenerated": '{"items": []}', "testcase": "用例1"}
            }
        ]

        var_names = ["生成测试用例"]

        # 尝试通过请求名称匹配
        target_idx = None
        for idx, cr in enumerate(chain_results):
            cr_name = cr.get("name", "")
            for var_name in var_names:
                if var_name in cr_name or cr_name in var_name:
                    target_idx = idx
                    break
            if target_idx is not None:
                break

        self.assertIsNotNone(target_idx)
        self.assertEqual(target_idx, 1)

        # 当 var_names 不在 variables 中时，应该使用 content
        chain_result = chain_results[target_idx]
        self.assertNotIn("生成测试用例", chain_result["variables"])

        # 但 content 可能为空，需要特殊处理
        # 实际情况下，应该返回整个请求的输出内容


class TestFullChainResultsBuildProcess(unittest.TestCase):
    """完整测试 chain_results 构建流程"""

    def test_build_chain_results_from_request_details(self):
        """
        完整测试：从 request_details 构建 chain_results 的完整流程
        模拟 model_caller.py 中的实际逻辑
        """
        # 模拟请求链执行后的 request_details
        request_details = [
            {
                "order": 1,
                "name": "预处理",
                "vars_before": {},
                "vars_after": {"prd_content": "", "message": ""},
                "output_var": "prd_content",
                "output_vars": None,
                "output_value": "PRD文档内容",
                "request_vars": {"prd_content": "PRD文档内容", "message": "消息内容"}
            },
            {
                "order": 2,
                "name": "生成测试点",
                "vars_before": {"prd_content": "PRD文档内容"},
                "vars_after": {"prd_content": "PRD文档内容", "test_points": ""},
                "output_var": "test_points",
                "output_vars": None,
                "output_value": "测试点内容",
                "request_vars": {"test_points": "测试点内容"}
            },
            {
                "order": 3,
                "name": "生成测试用例",
                "vars_before": {"prd_content": "PRD文档内容", "test_points": "测试点内容"},
                "vars_after": {"prd_content": "PRD文档内容", "test_points": "测试点内容"},
                "output_var": None,
                "output_vars": ["TestCasesGenerated", "testcase"],
                "output_value": None,
                "request_vars": {"TestCasesGenerated": '{"items": ["case1", "case2"]}', "testcase": "用例1"}
            }
        ]

        # 模拟 model_caller.py 中的构建逻辑
        chain_results = []
        for idx, detail in enumerate(request_details):
            vars_after = detail.get("vars_after", {})
            output_var_name = detail.get("output_var", "")
            output_vars = detail.get("output_vars")
            request_vars = detail.get("request_vars", {})

            # 合并变量：vars_after + request_vars（request_vars 优先级更高）
            merged_vars = {**vars_after, **request_vars}

            # 将 output_var 也添加到 merged_vars 中
            output_value = detail.get("output_value", "")
            if output_var_name and output_value:
                merged_vars[output_var_name] = output_value

            # 如果有 output_vars（多变量模式），添加到 merged_vars
            if output_vars and isinstance(output_vars, list):
                for var_name, var_value in request_vars.items():
                    merged_vars[var_name] = var_value

            chain_result = {
                "chain_index": idx,
                "order": detail.get("order", idx + 1),
                "name": detail.get("name", f"请求{idx + 1}"),
                "output_var": output_var_name,
                "output_vars": output_vars,
                "content": output_value,
                "variables": merged_vars,
                "all_results": merged_vars
            }
            chain_results.append(chain_result)

        # 验证第一个请求
        self.assertEqual(chain_results[0]["name"], "预处理")
        self.assertIn("prd_content", chain_results[0]["variables"])
        self.assertEqual(chain_results[0]["variables"]["prd_content"], "PRD文档内容")
        self.assertIn("message", chain_results[0]["variables"])

        # 验证第二个请求
        self.assertEqual(chain_results[1]["name"], "生成测试点")
        self.assertIn("test_points", chain_results[1]["variables"])
        self.assertEqual(chain_results[1]["variables"]["test_points"], "测试点内容")

        # 验证第三个请求（SSE多变量）
        self.assertEqual(chain_results[2]["name"], "生成测试用例")
        self.assertIn("TestCasesGenerated", chain_results[2]["variables"])
        self.assertIn("testcase", chain_results[2]["variables"])
        self.assertEqual(chain_results[2]["variables"]["TestCasesGenerated"], '{"items": ["case1", "case2"]}')
        self.assertEqual(chain_results[2]["variables"]["testcase"], "用例1")

        # 验证所有 variables 都包含前置变量
        self.assertIn("prd_content", chain_results[2]["variables"])
        self.assertIn("test_points", chain_results[2]["variables"])

    def test_sse_events_parsing_with_merged_data(self):
        """
        测试 SSE events 的多变量解析逻辑
        验证合并所有 events 的 data
        """
        # 模拟 SSE events
        sse_events = [
            {"event": "start", "data": {"status": "processing"}},
            {"event": "data", "data": {"partial_result": "部分结果1"}},
            {"event": "data", "data": {"TestCasesGenerated": '{"final": true}', "testcase": "最终用例"}},
            {"event": "end", "data": {"status": "completed"}}
        ]

        # 合并所有 events 的 data
        merged_data = {}
        for event in sse_events:
            if isinstance(event, dict) and "data" in event:
                event_data = event.get("data", {})
                if isinstance(event_data, dict):
                    merged_data.update(event_data)

        # 验证合并结果
        self.assertIn("status", merged_data)
        self.assertIn("partial_result", merged_data)
        self.assertIn("TestCasesGenerated", merged_data)
        self.assertIn("testcase", merged_data)
        # 后面的 event 应该覆盖前面的
        self.assertEqual(merged_data["testcase"], "最终用例")


class TestJudgeClientJsonParsing(unittest.TestCase):
    """测试 judge_client.py 中的 JSON 解析逻辑"""

    def test_sanitize_json_string(self):
        """测试 _sanitize_json_string 函数"""
        # 带真实换行符的 JSON 字符串
        json_with_newlines = '{"feedback": "第一行\\n第二行\\r第三行"}'
        sanitized = _sanitize_json_string(json_with_newlines)
        # 应该能够成功解析
        data = json.loads(sanitized)
        self.assertEqual(data["feedback"], "第一行\n第二行\r第三行")

    def test_fix_truncated_json_feedback(self):
        """测试 _try_fix_truncated_json 修复 feedback 字段截断"""
        # 被截断的 JSON（feedback 字符串值没有结束引号）
        truncated = '''{
  "score": 5.3,
  "dimension_scores": {
    "维度1：需求覆盖度（Coverage）": 4.5,
    "维度2：用例质量（Quality）": 5.5
  },
  "feedback": "部分内容被截断'''

        fixed = _try_fix_truncated_json(truncated)
        # 应该能够成功解析
        data = json.loads(fixed)
        self.assertEqual(data["score"], 5.3)
        self.assertEqual(data["feedback"], "部分内容被截断")

    def test_fix_truncated_json_missing_braces(self):
        """测试 _try_fix_truncated_json 修复缺少结束括号"""
        # 缺少结束括号的 JSON
        truncated = '''{
  "score": 8.5,
  "feedback": "测试反馈"'''

        fixed = _try_fix_truncated_json(truncated)
        # 应该能够成功解析
        data = json.loads(fixed)
        self.assertEqual(data["score"], 8.5)
        self.assertEqual(data["feedback"], "测试反馈")

    def test_fix_truncated_json_with_dimension_scores(self):
        """测试 _try_fix_truncated_json 修复带 dimension_scores 的截断 JSON"""
        truncated = '''{
  "score": 5.3,
  "dimension_scores": {
    "维度1：需求覆盖度（Coverage）": 4.5,
    "维度2：用例质量（Quality）": 5.5,
    "维度3：测试深度（Depth）": 4.8,
    "维度4：与历史测试用例对比（Comparison）": 6.0,
    "维度5：可维护性（Maintainability）": 5.8
  },
  "feedback": "测试反馈内容'''

        fixed = _try_fix_truncated_json(truncated)
        data = json.loads(fixed)
        self.assertEqual(data["score"], 5.3)
        self.assertEqual(data["feedback"], "测试反馈内容")
        self.assertEqual(data["dimension_scores"]["维度1：需求覆盖度（Coverage）"], 4.5)

    def test_valid_json_not_modified(self):
        """测试完整的 JSON 不会被修改"""
        valid_json = '''{
  "score": 9.5,
  "dimension_scores": {
    "维度1": 9.0,
    "维度2": 10.0
  },
  "feedback": "完整的反馈内容"
}'''

        fixed = _try_fix_truncated_json(valid_json)
        data = json.loads(fixed)
        self.assertEqual(data["score"], 9.5)
        self.assertEqual(data["feedback"], "完整的反馈内容")

    def test_parse_real_truncated_response(self):
        """测试解析真实场景中的截断响应（简单场景）

        注意：真实场景中 feedback 内部包含引号的情况需要更复杂的解析逻辑，
        此测试用例只验证简单截断场景。
        """
        # 简单截断场景测试
        truncated_response = '''```json
{
  "score": 5.3,
  "dimension_scores": {
    "维度1：需求覆盖度（Coverage）": 4.5,
    "维度2：用例质量（Quality）": 5.5
  },
  "feedback": "功能覆盖不足：遗漏必填字段校验逻辑；场景覆盖不全面"
}
```'''

        # 提取 JSON
        import re
        code_block_match = re.search(r'```json\s*\n(.*?)\n```', truncated_response, re.DOTALL)
        if code_block_match:
            json_str = code_block_match.group(1).strip()
            sanitized = _sanitize_json_string(json_str)
            fixed = _try_fix_truncated_json(sanitized)
            try:
                data = json.loads(fixed)
                self.assertEqual(data["score"], 5.3)
                self.assertIn("feedback", data)
                self.assertIn("dimension_scores", data)
                print(f"成功解析! score={data['score']}, feedback={data['feedback'][:50]}...")
            except json.JSONDecodeError as e:
                self.fail(f"JSON解析失败: {e}")


if __name__ == "__main__":
    unittest.main()
