"""输入/输出契约声明（model.py）

平台约定：脚本包导入与本文件在线保存时自动解析以下两个模型，同步脚本的
「输入字段」与「输出说明」（以本文件为准，覆盖手工填写值）。

- PreScriptInput：声明 fields 的输入字段（字段名 = 输入字段名）
- PreScriptOutput：声明标准输出 outputs 的键（说明按「类型：描述」生成）

本模板的对比对象由 params 通道（rel_path_a / rel_path_b）指定，输入文件来自
inputs 通道，均由平台固定注入 stdin，不属于输入字段声明范围，PreScriptInput
保留空壳。
"""
from pydantic import BaseModel, Field


class PreScriptInput(BaseModel):
    """脚本的输入字段声明（本模板无 fields 输入字段，对比对象走 params 通道）"""


class PreScriptOutput(BaseModel):
    """脚本标准输出 outputs 的键声明（平台解析此模型填充「输出说明」）"""

    rel_path_a: str = Field(..., description="对比文件 A 的相对路径（相对 inputs.root）")
    rel_path_b: str = Field(..., description="对比文件 B 的相对路径（相对 inputs.root）")
    identical: bool = Field(..., description="两份文件内容是否完全一致")
    diff_line_count: int = Field(..., description="差异行数（unified diff 中 +/- 变更行）")
    diff_preview: str = Field(..., description="差异预览（unified diff 文本，最多 30 行）")
    line_counts: dict = Field(..., description="双侧行数（{'a': 行数, 'b': 行数}）")
