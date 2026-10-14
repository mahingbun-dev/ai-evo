"""extract_json_from_response 单元测试"""
import pytest
from app.services.response_parser import extract_json_from_response

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestExtractJsonFromResponse:
    """extract_json_from_response 测试"""

    def test_pure_json(self):
        result = extract_json_from_response('{"score": 8, "feedback": "good"}')
        assert result == {"score": 8, "feedback": "good"}

    def test_json_code_block(self):
        result = extract_json_from_response('```json\n{"score": 9}\n```')
        assert result == {"score": 9}

    def test_plain_code_block(self):
        result = extract_json_from_response('```\n{"score": 7}\n```')
        assert result == {"score": 7}

    def test_mixed_content_with_thinking(self):
        result = extract_json_from_response('思考中...\n```json\n{"score": 8}\n```\n完成')
        assert result == {"score": 8}

    def test_invalid_json(self):
        result = extract_json_from_response('not json at all')
        assert result is None

    def test_empty_string(self):
        result = extract_json_from_response('')
        assert result is None

    def test_code_block_invalid_but_outer_valid(self):
        # 外层直接可解析的场景
        result = extract_json_from_response('```json\ninvalid\n```\n{"outer": true}')
        # 先尝试代码块失败，降级到直接解析
        assert result is None  # 因为外层不是纯JSON

    def test_nested_json(self):
        data = '{"dimension_scores": {"accuracy": 9, "completeness": 8}}'
        result = extract_json_from_response(data)
        assert result == {"dimension_scores": {"accuracy": 9, "completeness": 8}}

    def test_json_with_whitespace(self):
        result = extract_json_from_response('  {"key": "value"}  ')
        assert result == {"key": "value"}

    def test_json_array(self):
        result = extract_json_from_response('[1, 2, 3]')
        assert result == [1, 2, 3]
