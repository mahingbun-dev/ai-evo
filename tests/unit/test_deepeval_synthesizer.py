"""
DeepEval Synthesizer 集成单元测试

测试数据合成能力，包括从上下文、文档、从零生成测试数据，
以及将 Golden 列表转换为 AI-EVO 格式。

注意：Synthesizer 需要 LLM API key，测试时使用 mock。
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from deepeval.dataset import Golden


# ============================================================
# 辅助工具
# ============================================================
def _make_golden(input_text="测试问题", expected_output="测试答案", context=None):
    """创建一个测试用 Golden 对象"""
    kwargs = {"input": input_text, "expected_output": expected_output}
    if context is not None:
        kwargs["context"] = context
    return Golden(**kwargs)


def _make_goldens(n=3, context=None):
    """创建 n 个测试用 Golden 对象"""
    return [
        _make_golden(
            input_text=f"问题{i+1}",
            expected_output=f"答案{i+1}",
            context=context,
        )
        for i in range(n)
    ]


# ============================================================
# 测试: synthesize_from_contexts - 从上下文生成
# ============================================================
class TestSynthesizeFromContexts:
    """测试从上下文生成测试数据"""

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_basic_synthesize_from_contexts(self, MockSynthesizer):
        """基本功能：从上下文列表生成 Golden"""
        from app.services.deepeval_synthesizer import synthesize_from_contexts

        mock_instance = MockSynthesizer.return_value
        expected_goldens = _make_goldens(2)
        mock_instance.generate_goldens_from_contexts.return_value = expected_goldens

        contexts = [["上下文1", "上下文2"], ["上下文3", "上下文4"]]
        result = synthesize_from_contexts(contexts, num_questions=2)

        MockSynthesizer.assert_called_once()
        mock_instance.generate_goldens_from_contexts.assert_called_once_with(
            contexts=contexts,
            max_goldens_per_context=2,
            include_expected_output=True,
        )
        assert len(result) == 2
        assert all(isinstance(g, Golden) for g in result)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_contexts_returns_golden_list(self, MockSynthesizer):
        """返回值应为 Golden 列表"""
        from app.services.deepeval_synthesizer import synthesize_from_contexts

        mock_instance = MockSynthesizer.return_value
        expected_goldens = _make_goldens(3)
        mock_instance.generate_goldens_from_contexts.return_value = expected_goldens

        contexts = [["context A", "context B"]]
        result = synthesize_from_contexts(contexts, num_questions=3)

        assert isinstance(result, list)
        assert len(result) == 3
        for g in result:
            assert isinstance(g, Golden)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_contexts_empty_contexts(self, MockSynthesizer):
        """空上下文列表应返回空列表"""
        from app.services.deepeval_synthesizer import synthesize_from_contexts

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_contexts.return_value = []

        result = synthesize_from_contexts([], num_questions=2)

        assert result == []

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_contexts_with_model_param(self, MockSynthesizer):
        """应支持传入 model 参数指定 LLM"""
        from app.services.deepeval_synthesizer import synthesize_from_contexts

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_contexts.return_value = _make_goldens(1)

        contexts = [["测试上下文"]]
        synthesize_from_contexts(contexts, num_questions=1, model="gpt-4")

        MockSynthesizer.assert_called_once_with(model="gpt-4")

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_contexts_passes_include_expected_output(self, MockSynthesizer):
        """应正确传递 include_expected_output 参数"""
        from app.services.deepeval_synthesizer import synthesize_from_contexts

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_contexts.return_value = _make_goldens(1)

        contexts = [["上下文"]]
        synthesize_from_contexts(
            contexts, num_questions=1, include_expected_output=False
        )

        mock_instance.generate_goldens_from_contexts.assert_called_once_with(
            contexts=contexts,
            max_goldens_per_context=1,
            include_expected_output=False,
        )


# ============================================================
# 测试: synthesize_from_docs - 从文档生成
# ============================================================
class TestSynthesizeFromDocs:
    """测试从文档生成测试数据"""

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_basic_synthesize_from_docs(self, MockSynthesizer):
        """基本功能：从文档路径生成 Golden"""
        from app.services.deepeval_synthesizer import synthesize_from_docs

        mock_instance = MockSynthesizer.return_value
        expected_goldens = _make_goldens(2)
        mock_instance.generate_goldens_from_docs.return_value = expected_goldens

        docs = ["/path/to/doc1.pdf", "/path/to/doc2.pdf"]
        result = synthesize_from_docs(docs, num_questions=2)

        MockSynthesizer.assert_called_once()
        mock_instance.generate_goldens_from_docs.assert_called_once_with(
            document_paths=docs,
            max_goldens_per_context=2,
            include_expected_output=True,
        )
        assert len(result) == 2
        assert all(isinstance(g, Golden) for g in result)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_docs_returns_golden_list(self, MockSynthesizer):
        """返回值应为 Golden 列表"""
        from app.services.deepeval_synthesizer import synthesize_from_docs

        mock_instance = MockSynthesizer.return_value
        expected_goldens = _make_goldens(4)
        mock_instance.generate_goldens_from_docs.return_value = expected_goldens

        docs = ["/path/to/doc.txt"]
        result = synthesize_from_docs(docs, num_questions=4)

        assert isinstance(result, list)
        assert len(result) == 4
        for g in result:
            assert isinstance(g, Golden)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_docs_empty_docs(self, MockSynthesizer):
        """空文档列表应返回空列表"""
        from app.services.deepeval_synthesizer import synthesize_from_docs

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_docs.return_value = []

        result = synthesize_from_docs([], num_questions=2)

        assert result == []

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_docs_with_model_param(self, MockSynthesizer):
        """应支持传入 model 参数指定 LLM"""
        from app.services.deepeval_synthesizer import synthesize_from_docs

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_docs.return_value = _make_goldens(1)

        docs = ["/path/to/doc.pdf"]
        synthesize_from_docs(docs, num_questions=1, model="gpt-4")

        MockSynthesizer.assert_called_once_with(model="gpt-4")

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_docs_passes_include_expected_output(self, MockSynthesizer):
        """应正确传递 include_expected_output 参数"""
        from app.services.deepeval_synthesizer import synthesize_from_docs

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_docs.return_value = _make_goldens(1)

        docs = ["/path/to/doc.pdf"]
        synthesize_from_docs(
            docs, num_questions=1, include_expected_output=False
        )

        mock_instance.generate_goldens_from_docs.assert_called_once_with(
            document_paths=docs,
            max_goldens_per_context=1,
            include_expected_output=False,
        )


# ============================================================
# 测试: synthesize_from_scratch - 从零生成
# ============================================================
class TestSynthesizeFromScratch:
    """测试从零开始生成测试数据"""

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_basic_synthesize_from_scratch(self, MockSynthesizer):
        """基本功能：从主题生成 Golden"""
        from app.services.deepeval_synthesizer import synthesize_from_scratch

        mock_instance = MockSynthesizer.return_value
        expected_goldens = _make_goldens(3)
        mock_instance.generate_goldens_from_scratch.return_value = expected_goldens

        result = synthesize_from_scratch(topic="机器学习", num_questions=3)

        MockSynthesizer.assert_called_once()
        mock_instance.generate_goldens_from_scratch.assert_called_once_with(
            num_goldens=3,
        )
        assert len(result) == 3
        assert all(isinstance(g, Golden) for g in result)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_scratch_returns_golden_list(self, MockSynthesizer):
        """返回值应为 Golden 列表"""
        from app.services.deepeval_synthesizer import synthesize_from_scratch

        mock_instance = MockSynthesizer.return_value
        expected_goldens = _make_goldens(5)
        mock_instance.generate_goldens_from_scratch.return_value = expected_goldens

        result = synthesize_from_scratch(topic="深度学习", num_questions=5)

        assert isinstance(result, list)
        assert len(result) == 5
        for g in result:
            assert isinstance(g, Golden)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_scratch_with_model_param(self, MockSynthesizer):
        """应支持传入 model 参数指定 LLM"""
        from app.services.deepeval_synthesizer import synthesize_from_scratch

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_scratch.return_value = _make_goldens(1)

        synthesize_from_scratch(topic="NLP", num_questions=1, model="gpt-4")

        MockSynthesizer.assert_called_once_with(model="gpt-4")

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_scratch_zero_questions(self, MockSynthesizer):
        """请求 0 个问题时应返回空列表"""
        from app.services.deepeval_synthesizer import synthesize_from_scratch

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_scratch.return_value = []

        result = synthesize_from_scratch(topic="测试", num_questions=0)

        assert result == []


# ============================================================
# 测试: convert_to_ai_evo_format - 格式转换
# ============================================================
class TestConvertToAiEvoFormat:
    """测试将 Golden 列表转换为 AI-EVO 格式"""

    def test_basic_convert_to_ai_evo_format(self):
        """基本功能：将 Golden 列表转换为 AI-EVO 格式"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        goldens = [
            Golden(input="问题1", expected_output="答案1"),
            Golden(input="问题2", expected_output="答案2"),
        ]
        result = convert_to_ai_evo_format(goldens)

        assert len(result) == 2
        assert result[0]["user_input"] == "问题1"
        assert result[0]["ref_answer"] == "答案1"
        assert result[1]["user_input"] == "问题2"
        assert result[1]["ref_answer"] == "答案2"

    def test_convert_to_ai_evo_format_preserves_context(self):
        """转换应保留 context 字段"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        goldens = [
            Golden(
                input="什么是RAG？",
                expected_output="检索增强生成",
                context=["RAG结合检索和生成"],
            ),
        ]
        result = convert_to_ai_evo_format(goldens)

        assert result[0]["context"] == ["RAG结合检索和生成"]

    def test_convert_to_ai_evo_format_empty_list(self):
        """空列表应返回空列表"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        result = convert_to_ai_evo_format([])
        assert result == []

    def test_convert_to_ai_evo_format_single_item(self):
        """单个 Golden 应正确转换"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        goldens = [Golden(input="单个问题", expected_output="单个答案")]
        result = convert_to_ai_evo_format(goldens)

        assert len(result) == 1
        assert result[0]["user_input"] == "单个问题"
        assert result[0]["ref_answer"] == "单个答案"

    def test_convert_to_ai_evo_format_multiple_items(self):
        """多个 Golden 应全部正确转换"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        goldens = [
            Golden(input=f"问题{i}", expected_output=f"答案{i}")
            for i in range(10)
        ]
        result = convert_to_ai_evo_format(goldens)

        assert len(result) == 10
        for i, item in enumerate(result):
            assert item["user_input"] == f"问题{i}"
            assert item["ref_answer"] == f"答案{i}"

    def test_convert_to_ai_evo_format_with_additional_metadata(self):
        """additional_metadata 应展开为顶级字段"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        goldens = [
            Golden(
                input="测试问题",
                expected_output="测试答案",
                additional_metadata={"category": "AI", "difficulty": "easy"},
            ),
        ]
        result = convert_to_ai_evo_format(goldens)

        assert result[0]["category"] == "AI"
        assert result[0]["difficulty"] == "easy"

    def test_convert_to_ai_evo_format_uses_batch_golden_to_ai_evo(self):
        """应使用 DatasetConverter.batch_golden_to_ai_evo 进行转换"""
        from app.services.deepeval_synthesizer import convert_to_ai_evo_format

        goldens = _make_goldens(2)

        with patch("app.services.deepeval_synthesizer.batch_golden_to_ai_evo") as mock_batch:
            mock_batch.return_value = [
                {"user_input": "问题1", "ref_answer": "答案1"},
                {"user_input": "问题2", "ref_answer": "答案2"},
            ]
            result = convert_to_ai_evo_format(goldens)

            mock_batch.assert_called_once_with(goldens)
            assert len(result) == 2


# ============================================================
# 测试: 集成场景
# ============================================================
class TestIntegrationScenarios:
    """测试端到端集成场景"""

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_full_workflow_contexts_to_ai_evo(self, MockSynthesizer):
        """完整流程：从上下文生成 -> 转换为 AI-EVO 格式"""
        from app.services.deepeval_synthesizer import (
            synthesize_from_contexts,
            convert_to_ai_evo_format,
        )

        mock_instance = MockSynthesizer.return_value
        goldens = [
            Golden(input="什么是Python？", expected_output="Python是编程语言", context=["Python由Guido创建"]),
            Golden(input="什么是Java？", expected_output="Java是面向对象语言", context=["Java由Sun开发"]),
        ]
        mock_instance.generate_goldens_from_contexts.return_value = goldens

        # Step 1: 从上下文生成
        contexts = [["Python由Guido创建"], ["Java由Sun开发"]]
        generated = synthesize_from_contexts(contexts, num_questions=2)

        # Step 2: 转换为 AI-EVO 格式
        ai_evo_items = convert_to_ai_evo_format(generated)

        assert len(ai_evo_items) == 2
        assert ai_evo_items[0]["user_input"] == "什么是Python？"
        assert ai_evo_items[0]["ref_answer"] == "Python是编程语言"
        assert ai_evo_items[0]["context"] == ["Python由Guido创建"]
        assert ai_evo_items[1]["user_input"] == "什么是Java？"
        assert ai_evo_items[1]["ref_answer"] == "Java是面向对象语言"

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_full_workflow_scratch_to_ai_evo(self, MockSynthesizer):
        """完整流程：从零生成 -> 转换为 AI-EVO 格式"""
        from app.services.deepeval_synthesizer import (
            synthesize_from_scratch,
            convert_to_ai_evo_format,
        )

        mock_instance = MockSynthesizer.return_value
        goldens = [
            Golden(input="问题A", expected_output="答案A"),
            Golden(input="问题B", expected_output="答案B"),
            Golden(input="问题C", expected_output="答案C"),
        ]
        mock_instance.generate_goldens_from_scratch.return_value = goldens

        # Step 1: 从零生成
        generated = synthesize_from_scratch(topic="数据结构", num_questions=3)

        # Step 2: 转换为 AI-EVO 格式
        ai_evo_items = convert_to_ai_evo_format(generated)

        assert len(ai_evo_items) == 3
        for i, item in enumerate(ai_evo_items):
            assert item["user_input"] == f"问题{chr(65+i)}"  # A, B, C
            assert item["ref_answer"] == f"答案{chr(65+i)}"


# ============================================================
# 测试: 错误处理
# ============================================================
class TestErrorHandling:
    """测试错误处理场景"""

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_contexts_handles_llm_error(self, MockSynthesizer):
        """LLM 调用失败时应正确抛出异常"""
        from app.services.deepeval_synthesizer import synthesize_from_contexts

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_contexts.side_effect = RuntimeError("API key invalid")

        with pytest.raises(RuntimeError, match="API key invalid"):
            synthesize_from_contexts([["test"]], num_questions=1)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_docs_handles_llm_error(self, MockSynthesizer):
        """LLM 调用失败时应正确抛出异常"""
        from app.services.deepeval_synthesizer import synthesize_from_docs

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_docs.side_effect = RuntimeError("Rate limit exceeded")

        with pytest.raises(RuntimeError, match="Rate limit exceeded"):
            synthesize_from_docs(["/path/to/doc.pdf"], num_questions=1)

    @patch("app.services.deepeval_synthesizer.Synthesizer")
    def test_synthesize_from_scratch_handles_llm_error(self, MockSynthesizer):
        """LLM 调用失败时应正确抛出异常"""
        from app.services.deepeval_synthesizer import synthesize_from_scratch

        mock_instance = MockSynthesizer.return_value
        mock_instance.generate_goldens_from_scratch.side_effect = RuntimeError("Model not found")

        with pytest.raises(RuntimeError, match="Model not found"):
            synthesize_from_scratch(topic="test", num_questions=1)
