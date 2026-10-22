"""变异增强冒烟测试（M3 QE3-1）

- mock 裁判模型返回变体 JSON：变体数量、溯源字段（mutated_from/is_mutation）、
  原样本不被修改；
- 裁判失败（抛异常）/ 返回不可解析：跳过该样本变体不抛错（增强项不阻断任务）；
- count 越界收敛到 1-3。
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.redteam.mutator import (
    MAX_VARIANTS_PER_SAMPLE,
    mutate_samples,
    parse_mutation_response,
)

pytestmark = pytest.mark.unit


def _samples(n=2):
    return [
        {
            "id": f"pi-00{i}",
            "category": "prompt_injection",
            "prompt": f"攻击样本 {i}",
            "expected_behavior": "refuse",
        }
        for i in range(1, n + 1)
    ]


class TestMutateSamples:
    @patch("app.services.judge_client.JudgeClient")
    def test_variants_generated_with_lineage(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.return_value = '{"variants": ["改写变体A", "改写变体B"]}'
        mock_judge_cls.return_value = mock_client
        task = MagicMock()

        variants = mutate_samples(task, _samples(), count=2)

        assert len(variants) == 4  # 2 样本 × 2 变体
        by_source = {}
        for v in variants:
            assert v["is_mutation"] is True
            assert v["prompt"].startswith("改写变体")
            by_source.setdefault(v["mutated_from"], []).append(v)
        assert set(by_source) == {"pi-001", "pi-002"}
        assert all(len(vs) == 2 for vs in by_source.values())

    @patch("app.services.judge_client.JudgeClient")
    def test_original_samples_not_mutated(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.return_value = '{"variants": ["v"]}'
        mock_judge_cls.return_value = mock_client
        samples = _samples(1)

        mutate_samples(MagicMock(), samples, count=1)

        assert samples[0]["prompt"] == "攻击样本 1"
        assert "mutated_from" not in samples[0]

    @patch("app.services.judge_client.JudgeClient")
    def test_judge_failure_skips_not_raises(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.side_effect = RuntimeError("裁判不可用")
        mock_judge_cls.return_value = mock_client

        variants = mutate_samples(MagicMock(), _samples(), count=1)

        assert variants == []

    @patch("app.services.judge_client.JudgeClient")
    def test_unparseable_reply_skips(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.return_value = "不是 JSON 的闲聊"
        mock_judge_cls.return_value = mock_client

        assert mutate_samples(MagicMock(), _samples(), count=1) == []

    @patch("app.services.judge_client.JudgeClient")
    def test_count_clamped(self, mock_judge_cls):
        mock_client = MagicMock()
        mock_client.compress.return_value = '{"variants": ["a", "b", "c", "d", "e"]}'
        mock_judge_cls.return_value = mock_client

        variants = mutate_samples(MagicMock(), _samples(1), count=99)

        assert len(variants) <= MAX_VARIANTS_PER_SAMPLE
        assert MAX_VARIANTS_PER_SAMPLE == 3

    def test_parse_mutation_response_shapes(self):
        assert parse_mutation_response('{"variants": ["x", "y"]}') == ["x", "y"]
        assert parse_mutation_response('{"variants": []}') == []
        assert parse_mutation_response("garbage") == []
        assert parse_mutation_response('{"no_variants": 1}') == []
        assert parse_mutation_response('{"variants": ["a", "", "  "]}') == ["a"]
