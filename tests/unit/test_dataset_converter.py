"""
数据集格式转换器单元测试

测试 AI-EVO 数据项与 DeepEval Golden 格式的双向转换。
"""
import pytest
from deepeval.dataset import Golden


# ============================================================
# 基本转换测试: ai_evo_to_golden
# ============================================================
class TestAiEvoToGoldenBasic:
    """测试 ai_evo_to_golden 基本转换功能"""

    def test_basic_conversion(self):
        """最基本的字段映射: user_input -> input, ref_answer -> expected_output"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "什么是机器学习？",
            "ref_answer": "机器学习是人工智能的一个分支",
        }
        golden = ai_evo_to_golden(item)

        assert isinstance(golden, Golden)
        assert golden.input == "什么是机器学习？"
        assert golden.expected_output == "机器学习是人工智能的一个分支"

    def test_conversion_with_model_answer(self):
        """包含 model_answer 的转换: model_answer -> actual_output"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "什么是深度学习？",
            "ref_answer": "深度学习是机器学习的子集",
            "model_answer": "深度学习使用多层神经网络",
        }
        golden = ai_evo_to_golden(item)

        assert golden.input == "什么是深度学习？"
        assert golden.expected_output == "深度学习是机器学习的子集"
        assert golden.actual_output == "深度学习使用多层神经网络"

    def test_conversion_with_empty_optional_fields(self):
        """可选字段为空时应正常转换"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "测试问题",
            "ref_answer": "测试答案",
            "model_answer": None,
        }
        golden = ai_evo_to_golden(item)

        assert golden.input == "测试问题"
        assert golden.expected_output == "测试答案"
        assert golden.actual_output is None

    def test_conversion_without_ref_answer(self):
        """没有 ref_answer 时，expected_output 应为 None"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "测试问题",
        }
        golden = ai_evo_to_golden(item)

        assert golden.input == "测试问题"
        assert golden.expected_output is None


# ============================================================
# 上下文转换测试
# ============================================================
class TestAiEvoToGoldenWithContext:
    """测试包含上下文字段的转换"""

    def test_conversion_with_context(self):
        """包含 context 字段的转换"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "什么是RAG？",
            "ref_answer": "RAG是检索增强生成",
            "context": ["RAG全称Retrieval-Augmented Generation", "由Meta提出"],
        }
        golden = ai_evo_to_golden(item)

        assert golden.context == ["RAG全称Retrieval-Augmented Generation", "由Meta提出"]

    def test_conversion_with_retrieval_context(self):
        """包含 retrieval_context 字段的转换"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "什么是向量数据库？",
            "ref_answer": "向量数据库存储高维向量",
            "retrieval_context": ["向量数据库用于存储和检索向量嵌入"],
        }
        golden = ai_evo_to_golden(item)

        assert golden.retrieval_context == ["向量数据库用于存储和检索向量嵌入"]

    def test_conversion_with_both_contexts(self):
        """同时包含 context 和 retrieval_context"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "什么是嵌入？",
            "ref_answer": "嵌入是文本的向量表示",
            "context": ["嵌入将文本映射到高维空间"],
            "retrieval_context": ["常用的嵌入模型有OpenAI Ada等"],
        }
        golden = ai_evo_to_golden(item)

        assert golden.context == ["嵌入将文本映射到高维空间"]
        assert golden.retrieval_context == ["常用的嵌入模型有OpenAI Ada等"]


# ============================================================
# 自定义字段保留测试
# ============================================================
class TestAiEvoToGoldenCustomFields:
    """测试 preserve_custom_fields 参数"""

    def test_custom_fields_preserved(self):
        """preserve_custom_fields=True 时保留自定义字段到 additional_metadata"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "测试问题",
            "ref_answer": "测试答案",
            "category": "技术",
            "difficulty": "hard",
            "source": "internal",
        }
        golden = ai_evo_to_golden(item, preserve_custom_fields=True)

        assert golden.additional_metadata is not None
        assert golden.additional_metadata["category"] == "技术"
        assert golden.additional_metadata["difficulty"] == "hard"
        assert golden.additional_metadata["source"] == "internal"
        # 核心映射字段不应出现在 additional_metadata 中
        assert "user_input" not in golden.additional_metadata
        assert "ref_answer" not in golden.additional_metadata

    def test_custom_fields_not_preserved_by_default(self):
        """默认不保留自定义字段"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "测试问题",
            "ref_answer": "测试答案",
            "category": "技术",
        }
        golden = ai_evo_to_golden(item)

        assert golden.additional_metadata is None

    def test_custom_fields_with_model_answer_excluded(self):
        """model_answer 不应作为自定义字段被保留"""
        from app.services.dataset_converter import ai_evo_to_golden

        item = {
            "user_input": "测试问题",
            "ref_answer": "测试答案",
            "model_answer": "模型输出",
            "category": "技术",
        }
        golden = ai_evo_to_golden(item, preserve_custom_fields=True)

        assert golden.additional_metadata is not None
        assert "model_answer" not in golden.additional_metadata
        assert "category" in golden.additional_metadata


# ============================================================
# 反向转换测试: golden_to_ai_evo
# ============================================================
class TestGoldenToAiEvo:
    """测试 golden_to_ai_evo 反向转换功能"""

    def test_basic_reverse_conversion(self):
        """基本反向转换: input -> user_input, expected_output -> ref_answer"""
        from app.services.dataset_converter import golden_to_ai_evo

        golden = Golden(
            input="什么是NLP？",
            expected_output="NLP是自然语言处理",
        )
        item = golden_to_ai_evo(golden)

        assert item["user_input"] == "什么是NLP？"
        assert item["ref_answer"] == "NLP是自然语言处理"

    def test_reverse_conversion_with_actual_output(self):
        """包含 actual_output 的反向转换"""
        from app.services.dataset_converter import golden_to_ai_evo

        golden = Golden(
            input="什么是NLP？",
            expected_output="NLP是自然语言处理",
            actual_output="NLP处理人类语言",
        )
        item = golden_to_ai_evo(golden)

        assert item["user_input"] == "什么是NLP？"
        assert item["ref_answer"] == "NLP是自然语言处理"
        assert item["model_answer"] == "NLP处理人类语言"

    def test_reverse_conversion_with_context(self):
        """包含上下文的反向转换"""
        from app.services.dataset_converter import golden_to_ai_evo

        golden = Golden(
            input="什么是BERT？",
            expected_output="BERT是预训练语言模型",
            context=["BERT由Google提出"],
            retrieval_context=["BERT使用Transformer架构"],
        )
        item = golden_to_ai_evo(golden)

        assert item["context"] == ["BERT由Google提出"]
        assert item["retrieval_context"] == ["BERT使用Transformer架构"]

    def test_reverse_conversion_with_additional_metadata(self):
        """additional_metadata 应展开为顶级字段"""
        from app.services.dataset_converter import golden_to_ai_evo

        golden = Golden(
            input="测试",
            expected_output="答案",
            additional_metadata={"category": "AI", "difficulty": "easy"},
        )
        item = golden_to_ai_evo(golden)

        assert item["category"] == "AI"
        assert item["difficulty"] == "easy"


# ============================================================
# 批量转换测试
# ============================================================
class TestBatchConversion:
    """测试批量转换功能"""

    def test_batch_ai_evo_to_golden(self):
        """批量 AI-EVO -> Golden 转换"""
        from app.services.dataset_converter import batch_ai_evo_to_golden

        items = [
            {"user_input": "问题1", "ref_answer": "答案1"},
            {"user_input": "问题2", "ref_answer": "答案2"},
            {"user_input": "问题3", "ref_answer": "答案3"},
        ]
        goldens = batch_ai_evo_to_golden(items)

        assert len(goldens) == 3
        assert all(isinstance(g, Golden) for g in goldens)
        assert goldens[0].input == "问题1"
        assert goldens[1].input == "问题2"
        assert goldens[2].input == "问题3"

    def test_batch_golden_to_ai_evo(self):
        """批量 Golden -> AI-EVO 转换"""
        from app.services.dataset_converter import batch_golden_to_ai_evo

        goldens = [
            Golden(input="问题A", expected_output="答案A"),
            Golden(input="问题B", expected_output="答案B"),
        ]
        items = batch_golden_to_ai_evo(goldens)

        assert len(items) == 2
        assert items[0]["user_input"] == "问题A"
        assert items[1]["user_input"] == "问题B"

    def test_batch_empty_list(self):
        """空列表批量转换应返回空列表"""
        from app.services.dataset_converter import batch_ai_evo_to_golden, batch_golden_to_ai_evo

        assert batch_ai_evo_to_golden([]) == []
        assert batch_golden_to_ai_evo([]) == []

    def test_batch_with_custom_fields(self):
        """批量转换支持 preserve_custom_fields"""
        from app.services.dataset_converter import batch_ai_evo_to_golden

        items = [
            {"user_input": "问题1", "ref_answer": "答案1", "tag": "A"},
            {"user_input": "问题2", "ref_answer": "答案2", "tag": "B"},
        ]
        goldens = batch_ai_evo_to_golden(items, preserve_custom_fields=True)

        assert goldens[0].additional_metadata["tag"] == "A"
        assert goldens[1].additional_metadata["tag"] == "B"


# ============================================================
# 往返转换一致性测试
# ============================================================
class TestRoundTripConsistency:
    """测试往返转换的一致性"""

    def test_ai_evo_to_golden_and_back(self):
        """AI-EVO -> Golden -> AI-EVO 应保持核心字段一致"""
        from app.services.dataset_converter import ai_evo_to_golden, golden_to_ai_evo

        original = {
            "user_input": "什么是Transformer？",
            "ref_answer": "Transformer是注意力机制模型",
            "model_answer": "Transformer使用自注意力",
        }
        golden = ai_evo_to_golden(original)
        restored = golden_to_ai_evo(golden)

        assert restored["user_input"] == original["user_input"]
        assert restored["ref_answer"] == original["ref_answer"]
        assert restored["model_answer"] == original["model_answer"]

    def test_round_trip_with_context(self):
        """包含上下文的往返转换一致性"""
        from app.services.dataset_converter import ai_evo_to_golden, golden_to_ai_evo

        original = {
            "user_input": "什么是RAG？",
            "ref_answer": "检索增强生成",
            "context": ["RAG结合检索和生成"],
            "retrieval_context": ["相关文档片段"],
        }
        golden = ai_evo_to_golden(original)
        restored = golden_to_ai_evo(golden)

        assert restored["user_input"] == original["user_input"]
        assert restored["ref_answer"] == original["ref_answer"]
        assert restored["context"] == original["context"]
        assert restored["retrieval_context"] == original["retrieval_context"]

    def test_round_trip_with_custom_fields(self):
        """包含自定义字段的往返转换一致性"""
        from app.services.dataset_converter import ai_evo_to_golden, golden_to_ai_evo

        original = {
            "user_input": "测试问题",
            "ref_answer": "测试答案",
            "category": "技术",
            "priority": 1,
        }
        golden = ai_evo_to_golden(original, preserve_custom_fields=True)
        restored = golden_to_ai_evo(golden)

        assert restored["user_input"] == original["user_input"]
        assert restored["ref_answer"] == original["ref_answer"]
        assert restored["category"] == original["category"]
        assert restored["priority"] == original["priority"]

    def test_golden_to_ai_evo_and_back(self):
        """Golden -> AI-EVO -> Golden 应保持核心字段一致"""
        from app.services.dataset_converter import golden_to_ai_evo, ai_evo_to_golden

        original = Golden(
            input="什么是CNN？",
            expected_output="卷积神经网络",
            actual_output="CNN用于图像处理",
            context=["CNN使用卷积操作"],
        )
        item = golden_to_ai_evo(original)
        restored = ai_evo_to_golden(item)

        assert restored.input == original.input
        assert restored.expected_output == original.expected_output
        assert restored.actual_output == original.actual_output
        assert restored.context == original.context


# ============================================================
# 数据验证测试
# ============================================================
class TestValidation:
    """测试数据验证功能"""

    def test_validate_golden_valid(self):
        """有效 Golden 数据验证通过"""
        from app.services.dataset_converter import validate_golden

        golden = Golden(input="有效输入", expected_output="有效输出")
        result = validate_golden(golden)

        assert result.is_valid is True
        assert result.errors == []

    def test_validate_golden_missing_input(self):
        """缺少 input 的 Golden 应验证失败"""
        from app.services.dataset_converter import validate_golden

        # Golden 的 input 是必填字段，用空字符串模拟
        golden = Golden(input="", expected_output="输出")
        result = validate_golden(golden)

        assert result.is_valid is False
        assert len(result.errors) > 0

    def test_validate_ai_evo_item_valid(self):
        """有效 AI-EVO 数据项验证通过"""
        from app.services.dataset_converter import validate_ai_evo_item

        item = {"user_input": "有效问题", "ref_answer": "有效答案"}
        result = validate_ai_evo_item(item)

        assert result.is_valid is True
        assert result.errors == []

    def test_validate_ai_evo_item_missing_user_input(self):
        """缺少 user_input 的数据项应验证失败"""
        from app.services.dataset_converter import validate_ai_evo_item

        item = {"ref_answer": "只有答案"}
        result = validate_ai_evo_item(item)

        assert result.is_valid is False
        assert len(result.errors) > 0

    def test_validate_ai_evo_item_empty_user_input(self):
        """user_input 为空字符串应验证失败"""
        from app.services.dataset_converter import validate_ai_evo_item

        item = {"user_input": "", "ref_answer": "答案"}
        result = validate_ai_evo_item(item)

        assert result.is_valid is False

    def test_validate_ai_evo_item_not_dict(self):
        """非字典类型输入应验证失败"""
        from app.services.dataset_converter import validate_ai_evo_item

        result = validate_ai_evo_item("not a dict")

        assert result.is_valid is False
        assert len(result.errors) > 0

    def test_validate_golden_returns_errors_list(self):
        """验证结果应包含详细的错误信息列表"""
        from app.services.dataset_converter import validate_golden

        golden = Golden(input="", expected_output=None)
        result = validate_golden(golden)

        assert hasattr(result, "is_valid")
        assert hasattr(result, "errors")
        assert isinstance(result.errors, list)

    def test_validate_ai_evo_item_returns_errors_list(self):
        """验证结果应包含详细的错误信息列表"""
        from app.services.dataset_converter import validate_ai_evo_item

        item = {}
        result = validate_ai_evo_item(item)

        assert hasattr(result, "is_valid")
        assert hasattr(result, "errors")
        assert isinstance(result.errors, list)
