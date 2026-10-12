"""输入/输出契约声明（model.py）

平台约定：脚本包导入与本文件在线保存时自动解析以下两个模型，同步脚本的
「输入字段」与「输出说明」（以本文件为准，覆盖手工填写值）。

- PreScriptInput：声明 fields 的输入字段
    - 字段名 = 输入字段名；description = 字段说明
    - json_schema_extra={"value_type": "inline|local|oss|hub_file"}
      声明取值通道（缺省 inline）
- PreScriptOutput：声明标准输出 outputs 的键（说明按「类型：描述」生成）
"""
from pydantic import BaseModel, Field


class PreScriptInput(BaseModel):
    """脚本的输入字段声明（平台解析此模型填充「输入字段」）

    hub_pkg 是本模板选的字段名：编排配置时把数据集的 hub 执行包字段
    （hub_file_id）映射到该脚本字段名（字段名可自行修改，stdin.inputs 的键随编排
    映射走）；执行时平台把执行包镜像进单条数据工作区，stdin.inputs[字段名] 即
    {root, files}/{root, index_path} 两种形态之一（是否配 path_pattern 决定形态，
    path_pattern 在编排的输入映射里配置）。脚本应遍历 stdin.inputs 消费，
    不要写死字段名（main.py 即示范）。
    """

    hub_pkg: dict = Field(
        ...,
        description="anc-hub 执行包（hub_file 通道，平台注入 stdin.inputs['hub_pkg']）："
                    "配 path_pattern 时为 {root, files:[{path,rel_path,size,type}]}，"
                    "未配时为 {root, index_path}",
        json_schema_extra={"value_type": "hub_file"},
    )


class PreScriptOutput(BaseModel):
    """脚本标准输出 outputs 的键声明（平台解析此模型填充「输出说明」）"""

    input_fields: list = Field(..., description="消费的输入字段名清单（= 编排映射的脚本字段名，排序）")
    fields: dict = Field(..., description="各输入字段消费明细：{字段名: {mode: files|index, attempt_tops, file_count}}")
    manifest_summary: dict = Field(..., description="execution-manifest.json 摘要（跨字段去重后首个：execution_id 与用例通过/失败计数）")
    sample_rel_paths: list = Field(..., description="样例文件 rel_path 清单（相对 inputs.root，含 attempt 前缀，最多 5 条）")
