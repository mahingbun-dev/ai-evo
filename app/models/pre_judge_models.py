"""
评测前处理脚本（Pre-Judge Script）数据模型

可插拔脚本注册表：评测流水线步骤1（模型输出）与步骤2（裁判 Prompt 组装）之间
执行的确定性/agent 脚本，见 docs/design/pre-judge-scripts.md 第六节。
"""
from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.session import Base


class PreJudgeScript(Base):
    """评测前处理脚本表"""
    __tablename__ = "pre_judge_scripts"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, comment="脚本名称（全局唯一，builtin 与任务编排均按名称引用）")
    description = Column(Text, nullable=True, comment="脚本用途说明")

    # 执行类型：builtin(进程内注册脚本)/script(子进程，二期)/claude_agent/deepseek_agent
    exec_type = Column(String, nullable=False, comment="执行类型：builtin/script/claude_agent/deepseek_agent")
    # builtin: 模块.函数名（如 app.scripts_builtin.allure_vs_cases.run）
    # script: 脚本文件路径；claude_agent/deepseek_agent: 自然语言指令文本
    entrypoint = Column(Text, nullable=True, comment="builtin: 模块.函数名；script: 脚本路径；agent 类型: 指令文本")

    # agent 类型专用
    allowed_tools = Column(JSON, nullable=True, comment="CC 类型工具白名单（JSON 数组）")
    harness_extra = Column(JSON, nullable=True, comment="DSH 专用：{provider, model, profile, patches, max_tokens, reasoning_effort}")

    # 输入/输出契约（驱动前端映射表单与裁判变量选择）
    input_fields = Column(JSON, nullable=True, comment="声明所需输入字段：{名: {value_type: inline/local/oss/hub_file, description}}")
    output_schema = Column(JSON, nullable=True, comment="描述 output 键（JSON，驱动前端变量选择）")

    timeout_seconds = Column(Integer, nullable=False, default=120, server_default="120", comment="单次执行超时（秒）")
    is_active = Column(Boolean, default=True, server_default="true", comment="是否启用（停用=软删除，历史任务引用仍可追溯）")

    # 脚本文件契约（阶段E）：zip 包形态与上传阻断校验配套字段
    # 包形态：解包目录（包根含 main.py），entrypoint 指包内 main.py 绝对路径；
    # 单文件形态：package_dir 为空，entrypoint 指 {id}.py 绝对路径
    package_dir = Column(Text, nullable=True, comment="脚本包解包目录（zip 包形态，包根必须含 main.py；单文件形态为空）")
    # 依赖声明（如 ["requests>=2.31"]）：上传时逐包校验执行 venv 可用性，缺失拒绝
    declared_requirements = Column(JSON, nullable=True, comment="依赖声明（JSON 数组，逐包校验执行 venv，平台不自动安装）")
    # 风险声明文本：AST 风险扫描命中时必须携带才允许保存
    risk_acknowledged = Column(Text, nullable=True, comment="风险声明文本（AST 风险扫描命中时必填，否则拒存）")

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
