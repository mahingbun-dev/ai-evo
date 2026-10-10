"""
评测指令动态组装服务
根据配置动态生成评测指令prompt
"""
from typing import Dict, Any, List, Optional
from app.models.template_models import EvaluationTemplate


# 默认评测指令模板
DEFAULT_EVALUATION_PROMPT_TEMPLATE = '''
<角色和任务>
你是一名公正的文本评分裁判，需要在{{evaluation_scene}}场景下（场景定义：{{scene_desc}}），按照以下原则评估"AI助手回复"的质量。

<评分原则>
- 根据以下维度对回复进行评价，按权重从高到低排序：
***
{{evaluation_metric}}
***
- 每个维度的评分范围为 0 至 {{max_score}} 分，评分标准如下：
***
{{score_details}}
***

<评分步骤>
- 我将提供用户指令、参考答案和需要评估的"AI助手回复"，请按照以下流程对"AI助手回复"进行评价：
{{steps}}

<必须遵循>
- 严格依据评分原则进行评价，每个维度必须赋予整数分值。
- 禁止忽略任何维度或添加未提及的维度。

<输出要求>
- 仅输出 JSON 格式内容，禁止任何无关说明。
- **注意：以下JSON中的"[维度X名称]"仅表示你需要在对应key位置填入实际配置的维度名称（如"准确性"、"流畅性"等），不需要把维度名称作为评测指标内容，具体评测的标准以每个维度的具体要求和内容为准。**
- JSON 输出模板：
{
 "综合评分":"[加权平均得分，取整]",
 "综合评分原因": "（总结综合评分原因）。具体表现如下：",
 "[维度1名称]": {
     "score": "[0至{{max_score}}间的整数分]",
     "analysis": "（简要描述该维度的亮点或不足）"
   },
 "[维度2名称]": {
     "score": "[0至{{max_score}}间的整数分]",
     "analysis": "（简要描述该维度的亮点或不足）"
   },
   // ... 其他维度表现
}

#需要分析的用户指令、参考答案和助手回复：
***
[用户指令]:
{{question}}
***
[参考答案]:
{{ref_answer}}
***
[AI助手回复]:
{{answer}}
***
'''


class InstructionBuilder:
    """评测指令动态组装器"""

    def __init__(self, template: Optional[str] = None):
        self.template = template or DEFAULT_EVALUATION_PROMPT_TEMPLATE

    def build(self, config: Dict[str, Any]) -> str:
        """
        根据配置动态组装评测指令prompt

        Args:
            config: 评测指令配置

        Returns:
            组装后的prompt
        """
        # 1. 组装评分维度
        metrics = config.get("evaluation_metrics", []) or []

        metrics_text = self._build_metrics(metrics)

        # 2. 组装评分标准
        score_details = self._build_score_details(metrics)

        # 3. 组装评分步骤：维度顺序即步骤，从 evaluation_metrics 派生有序维度序列
        # （契约 v3 §四：{{steps}} 占位符保留，填充内容改为派生序列；
        #   无维度配置时回退旧版 steps / 默认步骤，保证空配置链路可用）
        if metrics:
            steps_text = self._build_steps_from_metrics(metrics)
        else:
            steps = config.get("steps", [])
            if isinstance(steps, list):
                steps_text = "\n".join(f"{i+1}. {step}" for i, step in enumerate(steps))
            else:
                steps_text = str(steps)

        # 4. 获取最大分值（统一使用任务级 max_score，不再从维度级取最大值）
        max_score = config.get("max_score", 10)

        # 5. 填充模板
        prompt = self.template.replace("{{evaluation_scene}}", config.get("scene", "通用"))
        prompt = prompt.replace("{{scene_desc}}", config.get("scene_description", ""))
        prompt = prompt.replace("{{evaluation_metric}}", metrics_text or "无特定维度")
        prompt = prompt.replace("{{max_score}}", str(max_score))
        prompt = prompt.replace("{{score_details}}", score_details or "无特定评分标准")
        prompt = prompt.replace("{{steps}}", steps_text or "1. 阅读理解\n2. 评估回复\n3. 综合评分")

        return prompt

    def _build_metrics(self, metrics: List[Dict[str, Any]]) -> str:
        """组装评分维度"""
        if not metrics:
            return ""

        lines = []
        for i, metric in enumerate(metrics, 1):
            name = metric.get("name", "维度")
            weight = metric.get("weight", "中")
            description = metric.get("description", "")
            max_score = metric.get("max_score", 10)

            line = f"{i}. {name}（权重：{weight}，满分：{max_score}）"
            if description:
                line += f" - {description}"
            lines.append(line)

        return "\n".join(lines)

    def _build_score_details(self, metrics: List[Dict[str, Any]]) -> str:
        """组装评分标准"""
        if not metrics:
            return ""

        lines = []
        for metric in metrics:
            name = metric.get("name", "维度")
            criteria = metric.get("score_criteria", {})

            lines.append(f"【{name}】")
            for level, details in criteria.items():
                if isinstance(details, dict):
                    min_score = details.get("min", 0)
                    max_score = details.get("max", 10)
                    desc = details.get("desc", "")
                    lines.append(f"  {level}({min_score}-{max_score}分)：{desc}")
            lines.append("")

        return "\n".join(lines)

    def _build_steps_from_metrics(self, metrics: List[Dict[str, Any]]) -> str:
        """从评测维度派生有序步骤序列（维度顺序即步骤，契约 v3 §四）

        每个维度产出一条 "{序号}. {维度名}：{维度定义}"，与裁判图
        judge_graph_state.derive_scoring_steps 的派生格式保持一致。
        """
        lines = []
        for i, metric in enumerate(metrics, 1):
            name = metric.get("name", "")
            if not name:
                continue
            description = metric.get("description", "") or ""
            lines.append(f"{i}. {name}：{description}")
        return "\n".join(lines)

    def preview(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        预览指令各部分

        Args:
            config: 评测指令配置

        Returns:
            预览结果
        """
        metrics = config.get("evaluation_metrics", [])
        return {
            "scene": config.get("scene", "通用"),
            "scene_description": config.get("scene_description", ""),
            "metrics": [m.get("name") for m in metrics],
            "max_score": config.get("max_score", 10),
            "steps": config.get("steps", []),
            "full_prompt": self.build(config)
        }

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        验证配置有效性

        Args:
            config: 评测指令配置

        Returns:
            验证结果
        """
        errors = []
        warnings = []

        # 检查必填字段
        if not config.get("scene"):
            errors.append("评测场景不能为空")

        metrics = config.get("evaluation_metrics", [])
        if not metrics:
            warnings.append("未配置评分维度，将使用默认评分标准")

        # 检查维度配置
        for i, metric in enumerate(metrics):
            if not metric.get("name"):
                errors.append(f"第{i+1}个评分维度缺少名称")

            if "max_score" in metric:
                max_score = metric["max_score"]
                if not isinstance(max_score, (int, float)) or max_score <= 0:
                    errors.append(f"维度'{metric.get('name', i+1)}'的最大分值必须为正数")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }


class TemplateService:
    """评测模板服务"""

    @staticmethod
    def get_default_template() -> Dict[str, Any]:
        """获取默认模板配置"""
        return {
            "scene": "通用文本生成",
            "scene_description": "对AI生成的文本内容进行评估",
            "evaluation_metrics": [
                {
                    "id": "accuracy",
                    "name": "准确性",
                    "weight": "高",
                    "max_score": 10,
                    "description": "内容是否准确无误",
                    "score_criteria": {
                        "优秀": {"min": 9, "max": 10, "desc": "内容准确无误，完全符合要求"},
                        "良好": {"min": 6, "max": 8, "desc": "基本准确，有少量偏差"},
                        "较差": {"min": 0, "max": 5, "desc": "存在明显错误"}
                    }
                },
                {
                    "id": "fluency",
                    "name": "流畅性",
                    "weight": "中",
                    "max_score": 10,
                    "description": "语言表达是否流畅自然",
                    "score_criteria": {
                        "优秀": {"min": 9, "max": 10, "desc": "语言流畅，表达自然"},
                        "良好": {"min": 6, "max": 8, "desc": "基本流畅，有少量问题"},
                        "较差": {"min": 0, "max": 5, "desc": "表达不流畅"}
                    }
                }
            ],
            "max_score": 10,
            "steps": [
                "1. 阅读理解用户输入和参考答案",
                "2. 逐维度评估AI回复",
                "3. 综合评分"
            ]
        }

    @staticmethod
    def get_preset_scenes() -> List[Dict[str, str]]:
        """获取预设场景列表"""
        return [
            {"id": "text_generation", "name": "文本生成", "description": "通用文本生成任务"},
            {"id": "translation", "name": "翻译", "description": "机器翻译任务"},
            {"id": "data_extraction", "name": "数据提取", "description": "从文本中提取结构化数据"},
            {"id": "content_creation", "name": "内容创作", "description": "创意内容生成"},
            {"id": "image_recognition", "name": "图片识别", "description": "图像识别与理解"},
            {"id": "file_parsing", "name": "文件解析", "description": "文档理解与解析"},
            {"id": "qa", "name": "问答", "description": "问答系统"},
            {"id": "summary", "name": "摘要", "description": "文本摘要生成"},
        ]
