"""输入/输出契约声明（model.py）

平台约定：脚本包导入与本文件在线保存时自动解析以下两个模型，同步脚本的
「输入字段」与「输出说明」（以本文件为准，覆盖手工填写值）。

- PreScriptInput：声明 fields 的输入字段（字段名 = 输入字段名）
- PreScriptOutput：声明标准输出 outputs 的键（说明按「类型：描述」生成）

本模板的输入全部来自 inputs 通道（hub 执行包，平台固定注入 stdin），
不声明输入字段，PreScriptInput 保留空壳。
"""
from pydantic import BaseModel, Field


class PreScriptInput(BaseModel):
    """脚本的输入字段声明（本模板无 fields 输入字段，输入走 inputs 通道）"""


class PreScriptOutput(BaseModel):
    """脚本标准输出 outputs 的键声明（平台解析此模型填充「输出说明」）"""

    result_file_count: int = Field(..., description="发现的 *-result.json 文件数")
    parsed_count: int = Field(..., description="成功解析的结果条数")
    status_counts: dict = Field(..., description="测试状态分布（passed/failed/broken 等计数）")
    passed_rate: float = Field(..., description="通过率（passed / 已解析总数）")
    failed: list = Field(..., description="failed 用例清单（含 name 与失败信息摘要）")
    broken: list = Field(..., description="broken 用例清单（含 name 与损坏信息摘要）")
