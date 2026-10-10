"""输入/输出契约声明（model.py）

平台约定：脚本包导入与本文件在线保存时自动解析以下两个模型，同步脚本的
「输入字段」与「输出说明」（以本文件为准，覆盖手工填写值）。

- PreScriptInput：声明 fields 的输入字段
    - 字段名 = 输入字段名；description = 字段说明
    - json_schema_extra={"value_type": "inline|local|oss|hub_file"}
      声明取值通道（缺省 inline）
- PreScriptOutput：声明标准输出 outputs 的键（说明按「类型：描述」生成）

inputs / params / upstream 通道由平台固定注入 stdin，不属于输入字段声明范围。
"""
from pydantic import BaseModel, Field


class PreScriptInput(BaseModel):
    """脚本的输入字段声明（平台解析此模型填充「输入字段」）"""

    note: str | None = Field(
        None,
        description="可选备注文件（inline/local 通道，值为平台落地后的本地文件路径）",
        json_schema_extra={"value_type": "local"},
    )


class PreScriptOutput(BaseModel):
    """脚本标准输出 outputs 的键声明（平台解析此模型填充「输出说明」）"""

    inputs_root: str = Field(..., description="inputs.hub_pkg.root（执行包根目录）")
    file_count: int = Field(..., description="root/files 模式下的文件数")
    files: list = Field(..., description="文件清单（每项含 rel_path/size/type）")
    index_file_count: int = Field(..., description="root/index_path 模式下的索引条目数")
    note_preview: str = Field(..., description="note 备注文件内容预览（前 120 字符）")
    params: dict = Field(..., description="任务编排传入的脚本参数原样回显")
    upstream_keys: list = Field(..., description="前序脚本结果键名排序清单")
