"""PromptBuilder.from_config 单元测试"""
import pytest
from unittest.mock import MagicMock
from app.services.prompt_builder import PromptBuilder
from app.services.judge_variable_resolver import EvaluationTarget

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestPromptBuilderFromConfig:
    """PromptBuilder.from_config 测试"""

    def test_full_config(self):
        config = {
            "judge_prompt": "请评分",
            "evaluation_scene": "自定义场景",
            "scene_desc": "自定义描述",
            "evaluation_metrics": [{"name": "准确性", "weight": 1}],
            "max_score": 5,
            "score_details": "详细评分标准",
            "steps": ["步骤1", "步骤2"],
            "judge_variable_config": {"user_input": {"source": "dataset_field"}},
            "evaluation_targets": [],
            "runtime_variables": {"my_var": {"value": "hello"}},
        }
        pb = PromptBuilder.from_config(config)
        assert pb.judge_prompt == "请评分"
        assert pb.evaluation_scene == "自定义场景"
        assert pb.scene_desc == "自定义描述"
        assert pb.evaluation_metrics == [{"name": "准确性", "weight": 1}]
        assert pb.max_score == 5
        assert pb.score_details == "详细评分标准"
        assert pb.steps == ["步骤1", "步骤2"]
        assert pb.judge_variable_config == {"user_input": {"source": "dataset_field"}}
        assert pb.evaluation_targets == []
        assert pb.runtime_variables == {"my_var": {"value": "hello"}}
        assert pb.task is None

    def test_minimal_config(self):
        pb = PromptBuilder.from_config({})
        assert pb.judge_prompt == ""
        assert pb.evaluation_scene == "一致性评测"
        assert pb.scene_desc == "评估同一问题多次回复的一致性质量"
        assert pb.evaluation_metrics == []
        assert pb.max_score == 10
        assert pb.score_details == ""
        assert pb.steps == []
        assert pb.judge_variable_config == {}
        assert pb.evaluation_targets is None
        assert pb.runtime_variables == {}
        assert pb.task is None

    def test_judge_variable_config_list_format(self):
        config = {"judge_variable_config": [{"name": "var1", "source": "model_output"}]}
        pb = PromptBuilder.from_config(config)
        assert isinstance(pb.judge_variable_config, list)
        assert pb.judge_variable_config[0]["name"] == "var1"

    def test_judge_variable_config_none(self):
        pb = PromptBuilder.from_config({"judge_variable_config": None})
        assert pb.judge_variable_config == {}

    def test_build_consistency_prompts(self):
        pb = PromptBuilder.from_config({"evaluation_metrics": [{"name": "准确性"}], "max_score": 10})
        system, user = pb.build_consistency_prompts(["output1", "output2"])
        assert "一致性" in system
        assert "输出 1" in user

    def test_build_system_prompt(self):
        pb = PromptBuilder.from_config({"scene_desc": "测试场景", "evaluation_metrics": [{"name": "准确性"}]})
        prompt = pb._build_system_prompt(payload={}, model_output="", chain_results=[])
        assert "测试场景" in prompt
        assert "准确性" in prompt

    def test_resolve_judge_variable_manual(self):
        config = {
            "runtime_variables": {"my_var": {"value": "hello"}},
            "judge_variable_config": [
                {"name": "my_var", "source": "manual", "prompt_type": "user", "key": "my_var"}
            ],
        }
        pb = PromptBuilder.from_config(config)
        value, _ = pb._resolve_judge_variable(
            var_config={"name": "my_var", "source": "manual", "key": "my_var"},
            payload={},
            model_output="",
        )
        assert value == "hello"

    def test_resolve_judge_variable_manual_missing(self):
        pb = PromptBuilder.from_config({})
        value, _ = pb._resolve_judge_variable(
            var_config={"name": "missing_var", "source": "manual", "key": "missing_var"},
            payload={},
            model_output="",
        )
        assert value == ""

    def test_build_user_prompt_from_judge_vars(self):
        config = {
            "judge_variable_config": {
                "model_out": {
                    "name": "模型输出",
                    "source": "model_output",
                    "prompt_type": "user",
                    "key": "model_out",
                }
            },
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={},
            model_output="测试输出内容",
        )
        assert "模型输出" in user_prompt
        assert "测试输出内容" in user_prompt

    def test_build_user_prompt_list_format_model_output(self):
        """列表格式+model_output: 列表格式含 source: model_output 的变量 → user_prompt 包含变量名和 model_output 内容"""
        config = {
            "judge_variable_config": [
                {"key": "model_out", "name": "模型输出", "source": "model_output", "prompt_type": "user"},
            ],
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={},
            model_output="这是模型输出内容",
        )
        assert "模型输出" in user_prompt
        assert "这是模型输出内容" in user_prompt

    def test_build_user_prompt_list_format_dataset_field(self):
        """列表格式+dataset_field: 列表格式含 source: dataset_field 的变量 → user_prompt 包含数据集字段值"""
        config = {
            "judge_variable_config": [
                {"key": "user_input", "name": "用户输入", "source": "dataset_field", "prompt_type": "user",
                 "source_config": {"field": "user_input"}},
            ],
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={"user_input": "请帮我写一个排序算法"},
            model_output="",
        )
        assert "用户输入" in user_prompt
        assert "请帮我写一个排序算法" in user_prompt

    def test_build_user_prompt_list_format_mixed_prompt_type(self):
        """列表格式+mixed_prompt_type: 含 prompt_type system 和 user 的变量 → 只有 user 出现在 user_prompt"""
        config = {
            "judge_variable_config": [
                {"key": "model_out", "name": "模型输出", "source": "model_output", "prompt_type": "user"},
                {"key": "metrics", "name": "评分标准", "source": "manual", "prompt_type": "system"},
            ],
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={},
            model_output="模型回复",
        )
        assert "模型输出" in user_prompt
        assert "评分标准" not in user_prompt

    def test_build_user_prompt_empty_list(self):
        """空列表 → 返回空字符串"""
        config = {"judge_variable_config": []}
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(payload={}, model_output="")
        assert user_prompt == ""

    def test_build_user_prompt_list_format_manual(self):
        """列表格式+manual: 列表格式含 source: manual 的变量 → user_prompt 包含 runtime_variables 中的值"""
        config = {
            "runtime_variables": {"自定义变量": {"value": "hello_world"}},
            "judge_variable_config": [
                {"key": "my_var", "name": "自定义变量", "source": "manual", "prompt_type": "user"},
            ],
        }
        pb = PromptBuilder.from_config(config)
        user_prompt = pb.build_user_prompt_from_judge_vars(
            payload={},
            model_output="",
        )
        assert "自定义变量" in user_prompt
        assert "hello_world" in user_prompt


class TestPromptBuilderEvaluationTargets:
    """PromptBuilder evaluation_targets 从 task 对象读取的测试"""

    def test_evaluation_targets_from_task_object(self):
        """测试 evaluation_targets 从 task 对象读取"""
        # 模拟 task 对象，包含 evaluation_targets 属性
        mock_task = MagicMock()
        mock_task.evaluation_targets = [
            {"name": "测试目标", "source": "model_output", "source_config": {}}
        ]
        mock_task.judge_prompt = ""
        mock_task.evaluation_scene = ""
        mock_task.scene_desc = ""
        mock_task.evaluation_metrics = []
        mock_task.max_score = 10
        mock_task.score_details = ""
        mock_task.steps = []
        mock_task.judge_variable_config = {}
        mock_task.runtime_variables = {}

        pb = PromptBuilder(mock_task)
        assert pb.evaluation_targets == [
            {"name": "测试目标", "source": "model_output", "source_config": {}}
        ]

    def test_evaluation_targets_parameter_overrides_task(self):
        """测试参数传入的 evaluation_targets 优先于 task 对象"""
        mock_task = MagicMock()
        mock_task.evaluation_targets = [{"name": "task_target"}]
        mock_task.judge_prompt = ""
        mock_task.evaluation_scene = ""
        mock_task.scene_desc = ""
        mock_task.evaluation_metrics = []
        mock_task.max_score = 10
        mock_task.score_details = ""
        mock_task.steps = []
        mock_task.judge_variable_config = {}
        mock_task.runtime_variables = {}

        param_targets = [{"name": "param_target"}]
        pb = PromptBuilder(mock_task, evaluation_targets=param_targets)
        assert pb.evaluation_targets == param_targets

    def test_evaluation_targets_none_when_not_set(self):
        """测试当 task 没有 evaluation_targets 时返回 None"""
        mock_task = MagicMock()
        mock_task.evaluation_targets = None
        mock_task.judge_prompt = ""
        mock_task.evaluation_scene = ""
        mock_task.scene_desc = ""
        mock_task.evaluation_metrics = []
        mock_task.max_score = 10
        mock_task.score_details = ""
        mock_task.steps = []
        mock_task.judge_variable_config = {}
        mock_task.runtime_variables = {}

        pb = PromptBuilder(mock_task)
        assert pb.evaluation_targets is None

    def test_inject_evaluation_targets_with_model_output(self):
        """测试 evaluation_targets 从 model_output 来源注入"""
        config = {
            "scene_desc": "测试场景",
            "evaluation_targets": [
                {
                    "id": "target_1",
                    "name": "模型输出",
                    "display_name": "模型输出",
                    "source": "model_output",
                    "source_config": {},
                    "required": True
                }
            ]
        }
        pb = PromptBuilder.from_config(config)
        system_prompt = pb._build_system_prompt(
            payload={},
            model_output="这是模型的输出内容",
            chain_results=[]
        )
        assert "待评测目标内容" in system_prompt
        assert "模型输出" in system_prompt
        assert "这是模型的输出内容" in system_prompt

    def test_inject_evaluation_targets_with_dataset_field(self):
        """测试 evaluation_targets 从 dataset_field 来源注入"""
        config = {
            "scene_desc": "测试场景",
            "evaluation_targets": [
                {
                    "id": "target_1",
                    "name": "用户问题",
                    "display_name": "用户问题",
                    "source": "dataset_field",
                    "source_config": {"field": "question"},
                    "required": True
                }
            ]
        }
        pb = PromptBuilder.from_config(config)
        system_prompt = pb._build_system_prompt(
            payload={"_dataset_item": {"question": "什么是Python？"}},
            model_output="",
            chain_results=[]
        )
        assert "待评测目标内容" in system_prompt
        assert "用户问题" in system_prompt
        assert "什么是Python？" in system_prompt

    def test_inject_evaluation_targets_empty_content_skipped(self):
        """测试当目标内容为空时不注入"""
        config = {
            "scene_desc": "测试场景",
            "evaluation_targets": [
                {
                    "id": "target_1",
                    "name": "空内容目标",
                    "display_name": "空内容目标",
                    "source": "model_output",
                    "source_config": {},
                    "required": True
                }
            ]
        }
        pb = PromptBuilder.from_config(config)
        system_prompt = pb._build_system_prompt(
            payload={},
            model_output="",  # 空的模型输出
            chain_results=[]
        )
        # 因为内容为空，所以不应该有 "## 待评测目标内容" 部分
        # 注意：##规范 部分包含 "对待评测目标内容进行评测" 文字，所以用更精确的匹配
        assert "## 待评测目标内容" not in system_prompt


class TestPreviewChainResults:
    """预览 chain_results 生成测试"""

    def test_build_preview_chain_results_with_chain_result_targets(self):
        """测试为 chain_result 类型目标生成模拟数据"""
        from app.api.judge_config_api import _build_preview_chain_results

        targets = [
            EvaluationTarget(
                id="target_1",
                name="文章",
                display_name="文章",
                source="chain_result",
                source_config={"chain_index": 0, "var_names": ["content"]},  # chain_index=0
                required=True
            )
        ]

        preview_chain_results = _build_preview_chain_results(targets)

        # chain_index=0，所以只需要创建 1 个元素（索引 0）
        assert len(preview_chain_results) == 1
        assert preview_chain_results[0]["chain_index"] == 0
        assert "预览模拟数据" in preview_chain_results[0]["content"]
        assert "content" in preview_chain_results[0]["variables"]

    def test_build_preview_chain_results_with_multiple_targets(self):
        """测试多个 chain_result 目标生成模拟数据（需要创建连续数组）"""
        from app.api.judge_config_api import _build_preview_chain_results

        targets = [
            EvaluationTarget(
                id="target_1",
                name="文章",
                display_name="文章",
                source="chain_result",
                source_config={"chain_index": 0},  # 第一个请求
                required=True
            ),
            EvaluationTarget(
                id="target_2",
                name="摘要",
                display_name="摘要",
                source="chain_result",
                source_config={"chain_index": 1},  # 第二个请求
                required=True
            )
        ]

        preview_chain_results = _build_preview_chain_results(targets)

        # 因为 chain_index 是数组索引，需要创建从 0 到最大索引的连续数组
        assert len(preview_chain_results) == 2
        assert preview_chain_results[0]["chain_index"] == 0
        assert preview_chain_results[1]["chain_index"] == 1

    def test_build_preview_chain_results_without_chain_result_targets(self):
        """测试没有 chain_result 目标时返回空列表"""
        from app.api.judge_config_api import _build_preview_chain_results

        targets = [
            EvaluationTarget(
                id="target_1",
                name="模型输出",
                display_name="模型输出",
                source="model_output",
                source_config={},
                required=True
            )
        ]

        preview_chain_results = _build_preview_chain_results(targets)

        assert len(preview_chain_results) == 0

    def test_inject_evaluation_targets_with_chain_result_and_preview_data(self):
        """测试 chain_result 类型目标在有预览数据时正确注入"""
        config = {
            "scene_desc": "测试场景",
            "evaluation_targets": [
                {
                    "id": "target_1",
                    "name": "文章",
                    "display_name": "文章",
                    "source": "chain_result",
                    # 产品语义为 1-based（judge_variable_resolver/binary_classification_evaluator
                    # 均做 chain_index-1 转换，前端 Tasks.vue 请求树节点 value 即序号）：1 表示第一个请求
                    "source_config": {"chain_index": 1, "var_names": ["content"]},
                    "required": True
                }
            ]
        }
        pb = PromptBuilder.from_config(config)

        # 模拟预览数据（链结果数组；resolver 以 chain_index-1=0 取第一个元素）
        preview_chain_results = [
            {
                "chain_index": 0,
                "name": "请求1",
                "content": "这是一篇示例文章内容",
                "variables": {"content": "这是一篇示例文章内容"}
            }
        ]

        system_prompt = pb._build_system_prompt(
            payload={},
            model_output="",
            chain_results=preview_chain_results
        )

        assert "## 待评测目标内容" in system_prompt
        assert "文章" in system_prompt
        assert "这是一篇示例文章内容" in system_prompt


class TestPromptBuilderOverrides:
    """PromptBuilder overrides（@ 引用标记解析覆盖件）优先级测试（契约 v3 §三）"""

    def test_from_config_overrides_take_precedence(self):
        """from_config + overrides → score_details / evaluation_metrics 用 overrides 值"""
        config = {
            "scene_desc": "测试场景",
            "score_details": "原始评分标准",
            "evaluation_metrics": [{"name": "原始维度", "weight": 1}],
        }
        overrides = {
            "score_details": "解析后的评分标准",
            "evaluation_metrics": [{"name": "解析后维度", "weight": 2}],
        }
        pb = PromptBuilder.from_config(config, overrides=overrides)
        assert pb.score_details == "解析后的评分标准"
        assert pb.evaluation_metrics == [{"name": "解析后维度", "weight": 2}]

    def test_from_config_overrides_partial(self):
        """from_config + 部分 overrides → 未覆盖字段回退 config 值"""
        config = {
            "score_details": "原始评分标准",
            "evaluation_metrics": [{"name": "原始维度", "weight": 1}],
        }
        pb = PromptBuilder.from_config(config, overrides={"score_details": "只改评分标准"})
        assert pb.score_details == "只改评分标准"
        assert pb.evaluation_metrics == [{"name": "原始维度", "weight": 1}]

    def test_from_config_without_overrides_keeps_config(self):
        """from_config 不传 overrides → 保持 config 值（存量行为不变）"""
        config = {
            "score_details": "原始评分标准",
            "evaluation_metrics": [{"name": "原始维度", "weight": 1}],
        }
        pb = PromptBuilder.from_config(config)
        assert pb.score_details == "原始评分标准"
        assert pb.evaluation_metrics == [{"name": "原始维度", "weight": 1}]

    def test_init_overrides_take_precedence_over_task(self):
        """构造函数 + overrides → 优先于 task 同名字段"""
        mock_task = MagicMock()
        mock_task.judge_prompt = ""
        mock_task.evaluation_scene = ""
        mock_task.scene_desc = ""
        mock_task.evaluation_metrics = [{"name": "task维度", "weight": 1}]
        mock_task.max_score = 10
        mock_task.score_details = "task评分标准"
        mock_task.steps = []
        mock_task.judge_variable_config = {}
        mock_task.runtime_variables = {}

        overrides = {
            "score_details": "覆盖评分标准",
            "evaluation_metrics": [{"name": "覆盖维度", "weight": 3}],
        }
        pb = PromptBuilder(mock_task, overrides=overrides)
        assert pb.score_details == "覆盖评分标准"
        assert pb.evaluation_metrics == [{"name": "覆盖维度", "weight": 3}]

    def test_build_system_prompt_uses_overrides_content(self):
        """_build_system_prompt 用 overrides 的维度与评分标准渲染，task 原值不出现"""
        config = {
            "scene_desc": "测试场景",
            "score_details": "原始评分标准",
            "evaluation_metrics": [{"name": "原始维度", "weight": 1}],
        }
        overrides = {
            "score_details": "【引用内容】\n解析后的标准",
            "evaluation_metrics": [{"name": "解析后维度", "weight": 2, "description": "解析后定义"}],
        }
        pb = PromptBuilder.from_config(config, overrides=overrides)
        prompt = pb._build_system_prompt(payload={}, model_output="", chain_results=[])

        assert "解析后维度" in prompt
        assert "解析后的标准" in prompt
        assert "原始维度" not in prompt
        assert "原始评分标准" not in prompt


class TestSystemPromptStepsContract:
    """系统提示词「步骤合并」契约测试（契约 v3 §四）"""

    def _build_prompt(self, metrics=None, steps=None, score_details=""):
        config = {
            "scene_desc": "测试场景",
            "evaluation_metrics": metrics if metrics is not None else [
                {"name": "准确性", "weight": "高", "description": "内容准确度"},
                {"name": "完整性", "weight": 2, "description": "覆盖完整"},
            ],
            "steps": steps if steps is not None else ["旧步骤1", "旧步骤2"],
            "score_details": score_details,
        }
        pb = PromptBuilder.from_config(config)
        return pb._build_system_prompt(payload={}, model_output="", chain_results=[])

    def test_scoring_steps_section_removed(self):
        """删除「## 评分步骤」段：即使 task.steps 有值也不再输出"""
        prompt = self._build_prompt(steps=["步骤A", "步骤B"])
        assert "## 评分步骤" not in prompt
        assert "步骤A" not in prompt
        assert "步骤B" not in prompt

    def test_metrics_rendered_as_ordered_steps(self):
        """评测维度段改为有序步骤形式：第N步 {name} (权重: x, 最高分: y) + 维度定义"""
        prompt = self._build_prompt()
        assert "### 第1步 准确性 (权重: 高(3), 最高分: 10)" in prompt
        assert "内容准确度" in prompt
        assert "### 第2步 完整性 (权重: 2, 最高分: 10)" in prompt
        assert "覆盖完整" in prompt

    def test_norm_section_emphasizes_sequential_scoring(self):
        """规范段强调「按维度顺序逐步分析并评分」"""
        prompt = self._build_prompt()
        assert "按维度顺序逐步分析并评分" in prompt

    def test_score_details_section_kept(self):
        """评分标准段保留"""
        prompt = self._build_prompt(score_details="9-10分：优秀")
        assert "## 评分标准" in prompt
        assert "9-10分：优秀" in prompt

    def test_output_format_unchanged_with_metrics(self):
        """输出格式要求（dimension_scores JSON）保持不变"""
        prompt = self._build_prompt()
        assert "## 输出格式要求" in prompt
        assert "dimension_scores" in prompt
        assert '"准确性": <该维度得分(0-10)>' in prompt
        assert '"完整性": <该维度得分(0-10)>' in prompt
        assert "综合评分将由系统按权重自动计算" in prompt

    def test_output_format_without_metrics(self):
        """无维度时输出格式走单分支（无 dimension_scores）"""
        prompt = self._build_prompt(metrics=[])
        assert "## 输出格式要求" in prompt
        assert "dimension_scores" not in prompt


class TestSystemPromptTargetsInjection:
    """include_targets_content 开关（CC 引擎 argv 单参数 128KB 修复）

    claude-agent-sdk 以 --system-prompt argv 传 system_prompt，待评数据
    超 Linux 单参数 131072B 上限会 execve 失败；CC 引擎调用方传
    include_targets_content=False 跳过数据注入，数据改由 executor 分流。
    """

    @staticmethod
    def _pb():
        return PromptBuilder.from_config({
            "scene_desc": "测试场景",
            "evaluation_metrics": [{"name": "准确性", "weight": 1}],
            "evaluation_targets": [
                {"name": "输出", "source": "model_output", "display_name": "输出"}
            ],
        })

    def _build(self, **overrides):
        kwargs = {"payload": {}, "model_output": "待评内容ABC", "chain_results": []}
        kwargs.update(overrides)
        return self._pb()._build_system_prompt(**kwargs)

    def test_default_includes_targets_content(self):
        prompt = self._build()
        assert "## 待评测目标内容" in prompt
        assert "待评内容ABC" in prompt

    def test_false_skips_targets_content(self):
        prompt = self._build(include_targets_content=False)
        assert "## 待评测目标内容" not in prompt
        assert "待评内容ABC" not in prompt
        # 评分指令部分完整保留
        assert "测试场景" in prompt
        assert "准确性" in prompt
        assert "输出格式要求" in prompt

    def test_false_without_targets_identical(self):
        """无 targets 时开关不影响输出（graph 引擎零回归）"""
        pb = PromptBuilder.from_config({"evaluation_metrics": [{"name": "准确性"}]})
        default = pb._build_system_prompt(payload={}, model_output="x", chain_results=[])
        skipped = pb._build_system_prompt(
            payload={}, model_output="x", chain_results=[], include_targets_content=False
        )
        assert default == skipped
