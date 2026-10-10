"""
评测任务数据模型
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, Text, Float, ForeignKey
from sqlalchemy.sql import func
from app.db.session import Base


class EvaluationTask(Base):
    """评测任务表"""
    __tablename__ = "evaluation_tasks"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="任务名称")
    description = Column(String, nullable=True, comment="任务描述")

    # 评测类型：judge(AI裁判员)/consistency(一致性)/success_rate(成功率)/rag(RAG检索评测)
    evaluation_type = Column(String, nullable=False, comment="评测类型")
    status = Column(String, default="pending", comment="状态：pending/running/completed/failed/stopped")

    # 被评测模型配置
    model_config = Column(JSON, nullable=True, comment="模型参数配置")
    model_provider = Column(String, nullable=True, comment="模型提供商")
    model_endpoint = Column(String, nullable=True, comment="模型端点（内部Agent用）")

    # 评测配置
    dataset_id = Column(String, ForeignKey("evaluation_datasets.id", ondelete="SET NULL"), nullable=True)

    # AI裁判员评测配置
    judge_model = Column(String, nullable=True, comment="裁判员模型（软退场：历史字段，新任务不再写入）")
    judge_engine = Column(String, default="claude_agent", server_default="claude_agent", comment="裁判引擎：claude_agent(Claude Code Agent)/graph(裁判图 HTTP 直调)，历史任务空值按 graph 处理")
    judge_prompt = Column(Text, nullable=True, comment="评测prompt模板")

    # 评测指令配置
    evaluation_scene = Column(String, nullable=True, comment="评测场景")
    scene_desc = Column(Text, nullable=True, comment="场景描述")
    evaluation_metrics = Column(JSON, nullable=True, comment="评测维度配置")
    max_score = Column(Integer, default=10, comment="最大分值")
    score_details = Column(Text, nullable=True, comment="分值标准")
    steps = Column(JSON, nullable=True, comment="评分步骤")

    # 一致性评测配置
    consistency_config = Column(JSON, nullable=True, comment="一致性评测配置")

    # 成功率评测配置
    success_rate_config = Column(JSON, nullable=True, comment="成功率评测配置")

    # RAG评测配置
    rag_config = Column(JSON, nullable=True, comment="RAG评测配置")

    # 耗时评测配置
    latency_config = Column(JSON, nullable=True, comment="耗时评测配置")

    # 二分分类评测配置
    binary_metrics_config = Column(JSON, nullable=True, comment="二分分类评测配置")

    # 裁判数量配置
    judge_count = Column(Integer, default=6, comment="裁判数量")

    # 多裁判配置
    multi_judge_config = Column(JSON, nullable=True, comment="多裁判配置")

    # 并行执行配置
    parallel_config = Column(JSON, nullable=True, comment="并行执行配置")

    # 变量注入配置
    runtime_variables = Column(JSON, nullable=True, comment="运行时变量（手动输入）")
    variable_source = Column(String, default="manual", comment="变量来源：manual-手动输入，dataset-从数据集选择")
    variable_mappings = Column(JSON, nullable=True, comment="变量到数据集字段的映射")
    variable_sources = Column(JSON, nullable=True, comment="变量数据来源：manual-手动输入，dataset-数据集字段")
    variable_types = Column(JSON, nullable=True, comment="变量类型：string/number/boolean")
    enable_user_import = Column(Boolean, default=False, comment="是否启用user_import注入")
    user_import_template = Column(Text, nullable=True, comment="user_import模板格式")

    # 裁判模型变量配置
    judge_variable_config = Column(JSON, nullable=True, comment="裁判模型变量配置：数据来源和prompt类型")

    # 评测目标配置（多评测目标）
    evaluation_targets = Column(JSON, nullable=True, comment="评测目标配置列表")
    validation_enabled = Column(Boolean, default=True, comment="是否启用空值校验")

    # 评测前处理脚本编排（pre-judge pipeline）
    # [{script_id, input_mapping: {字段: {field, value_type}}, params, output_var, on_fail}]
    pre_scripts = Column(JSON, nullable=True, comment="评测前处理脚本编排列表（串行执行，输出进入裁判变量体系 script_output）")

    # 结果
    result = Column(JSON, nullable=True, comment="评测结果")
    total_cost = Column(Float, nullable=True, comment="消耗成本")

    # 执行统计
    progress = Column(Integer, default=0, comment="进度百分比")
    total_questions = Column(Integer, default=0, comment="总题目数")
    completed_questions = Column(Integer, default=0, comment="已完成题目数")

    created_by = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True, comment="完成时间")
    execution_count = Column(Integer, default=0, comment="累计执行次数")

    # 团队归属（V2 新增）
    team_code = Column(String, nullable=True, server_default="DEFAULT", comment="所属团队代码")

    # Celery 任务ID（用于停止任务）
    celery_task_id = Column(String, nullable=True, comment="Celery任务ID，用于停止任务")

    # 乐观锁版本号（用于解决状态更新竞态问题）
    version = Column(Integer, default=0, nullable=False, comment="乐观锁版本号")

    # 数据集快照ID（V2 新增）
    dataset_snapshot_id = Column(String, nullable=True, comment="数据集快照ID")

    # 字段映射配置（V2 新增，支持自由数据集字段）
    input_field = Column(String, nullable=True, server_default="user_input", comment="指向 data 中的用户输入字段名")
    reference_field = Column(String, nullable=True, server_default="answer", comment="指向 data 中的参考答案字段名")

    # 关联的评测模板ID
    template_id = Column(String, nullable=True, comment="关联的评测模板ID")


class TaskExecutionLog(Base):
    """任务执行日志表"""
    __tablename__ = "task_execution_logs"

    id = Column(String, primary_key=True, index=True)
    task_id = Column(String, ForeignKey("evaluation_tasks.id", ondelete="CASCADE"))
    level = Column(String, nullable=False, comment="日志级别：info/warning/error")
    message = Column(Text, nullable=False, comment="日志内容")
    details = Column(JSON, nullable=True, comment="详细信息")

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TaskExecutionRecord(Base):
    """任务执行历史记录表"""
    __tablename__ = "task_execution_records"

    id = Column(String, primary_key=True, index=True)
    task_id = Column(String, ForeignKey("evaluation_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_number = Column(Integer, nullable=False, comment="第几次执行，从1开始")
    status = Column(String, nullable=False, default="running", comment="状态：running/completed/failed/stopped")
    result = Column(JSON, nullable=True, comment="评测结果")
    total_cost = Column(Float, nullable=True, comment="消耗成本")
    error_message = Column(Text, nullable=True, comment="失败错误信息")
    remark = Column(String, nullable=True, comment="备注")
    started_at = Column(DateTime(timezone=True), server_default=func.now(), comment="执行开始时间")
    completed_at = Column(DateTime(timezone=True), nullable=True, comment="执行完成时间")

    # 多裁判独立记录字段
    judge_index = Column(Integer, nullable=True, comment="裁判序号")
    evaluation_target_id = Column(String, nullable=True, comment="评测目标ID")
    evaluation_target_name = Column(String, nullable=True, comment="评测目标名称")
    validation_message = Column(Text, nullable=True, comment="空值校验信息")
    estimated_manual_hours = Column(Float, nullable=True, comment="人工执行预计耗时（小时）")
