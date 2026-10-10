"""
裁判模型空值校验服务
用于检查评测目标内容是否为空，并在失败时构造零分结果
"""
from typing import Dict, Any, List, Tuple


class JudgeValidationService:
    """空值校验服务"""

    def validate_targets(
        self,
        resolved_targets: Dict[str, Dict[str, Any]],
        validation_enabled: bool = True
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        校验所有评测目标的内容是否为空

        Args:
            resolved_targets: 解析后的评测目标字典，格式为 {target_id: {"name": str, "content": str, "is_empty": bool, "required": bool}}
            validation_enabled: 是否启用空值校验

        Returns:
            (is_valid: bool, validation_results: List[Dict]) - 校验结果和详细信息
        """
        if not validation_enabled:
            return True, []

        validation_results = []
        is_valid = True

        for target_id, target_info in resolved_targets.items():
            name = target_info.get("name", target_id)
            content = target_info.get("content", "")
            is_empty = target_info.get("is_empty", False)
            required = target_info.get("required", True)

            if required and is_empty:
                is_valid = False
                validation_results.append({
                    "target_id": target_id,
                    "target_name": name,
                    "status": "error",
                    "message": f"必填评测目标「{name}」内容为空",
                    "content": content
                })
            elif is_empty and not required:
                validation_results.append({
                    "target_id": target_id,
                    "target_name": name,
                    "status": "warning",
                    "message": f"可选评测目标「{name}」内容为空",
                    "content": content
                })
            else:
                validation_results.append({
                    "target_id": target_id,
                    "target_name": name,
                    "status": "ok",
                    "message": f"评测目标「{name}」校验通过",
                    "content": content
                })

        return is_valid, validation_results

    def build_zero_score_result(
        self,
        judge_model: str,
        validation_results: List[Dict[str, Any]],
        question: str = "",
        ref_answer: str = "",
        model_answer: str = ""
    ) -> Dict[str, Any]:
        """
        校验失败时构造零分结果

        Args:
            judge_model: 裁判模型名称
            validation_results: 校验结果详情
            question: 用户问题
            ref_answer: 参考答案
            model_answer: 模型回答

        Returns:
            零分结果的字典格式
        """
        error_messages = [
            f"「{r['target_name']}」: {r['message']}"
            for r in validation_results
            if r["status"] in ("error", "warning")
        ]

        return {
            "success": True,
            "scores": {
                "综合评分": 0,
                "校验状态": "failed",
                "校验失败原因": "\n".join(error_messages) if error_messages else "评测目标内容为空"
            },
            "overall_score": 0.0,
            "reasoning": f"空值校验失败: {'; '.join(error_messages) if error_messages else '评测目标内容为空'}",
            "judge_model": judge_model,
            "question": question,
            "ref_answer": ref_answer,
            "model_answer": model_answer,
            "validation_message": "\n".join(error_messages)
        }
