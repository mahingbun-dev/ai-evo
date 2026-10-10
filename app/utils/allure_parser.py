import json
import os
from pathlib import Path
from typing import Any, Dict, List

class AllureParser:
    """
    Allure 结果解析器，用于从 allure-results 目录提取测试结果和附件
    """
    @staticmethod
    def parse_results(results_dir: str | Path) -> List[Dict[str, Any]]:
        results_path = Path(results_dir)
        if not results_path.exists():
            return []

        test_results = []
        # 查找所有以 -result.json 结尾的文件
        for result_file in results_path.glob("*-result.json"):
            try:
                with open(result_file, "r", encoding="utf-8") as f:
                    result_data = json.load(f)
                
                # 递归处理步骤和附件
                processed_result = AllureParser._process_item(result_data, results_path)
                test_results.append(processed_result)
            except Exception as e:
                # 忽略解析失败的文件
                continue
        
        return test_results

    @staticmethod
    def _process_item(item: Dict[str, Any], results_path: Path, exclude_attachments: List[str] = ["text/plain"], need_attachments: List[str] = []) -> Dict[str, Any]:
        """
        处理测试项（测试用例或步骤），递归提取附件内容
        """
        processed = {
            "name": item.get("name"),
            "status": item.get("status"),
            "start": item.get("start"),
            "stop": item.get("stop"),
            "parameters": item.get("parameters", []),
        }

        # 处理附件
        if "attachments" in item:
            processed["attachments"] = []
            for att in item["attachments"]:
                source_path = results_path / att.get("source", "")
                if exclude_attachments and att.get("type") in exclude_attachments:
                    continue
                if need_attachments and att.get("type") not in need_attachments:
                    continue
                att_content = None
                if source_path.exists():
                    try:
                        with open(source_path, "r", encoding="utf-8") as f:
                            if att.get("type") == "application/json":
                                att_content = json.load(f)
                            else:
                                att_content = f.read()
                    except Exception:
                        att_content = "[无法读取附件内容]"
                
                processed["attachments"].append({
                    "name": att.get("name"),
                    "type": att.get("type"),
                    "content": att_content
                })

        # 递归处理步骤
        if "steps" in item:
            processed["steps"] = [
                AllureParser._process_item(step, results_path) 
                for step in item["steps"]
            ]

        return processed
