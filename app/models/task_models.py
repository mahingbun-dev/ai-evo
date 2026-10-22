"""
评测任务数据模型
"""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, Text, Float, ForeignKey, BigInteger, UniqueConstraint
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

    # 模型输出来源（judge 评测：模型输出不一定来自被评测模型调用）
    # model_call-调用被评测模型（默认，存量行为）/ dataset_field-直接取数据集字段 /
    # none-不使用模型输出
    model_output_source = Column(
        String(32), nullable=True, server_default="model_call",
        comment="模型输出来源：model_call/dataset_field/none（空值按 model_call 处理）",
    )
    model_output_field = Column(
        String, nullable=True,
        comment="模型输出字段名（model_output_source=dataset_field 时生效）",
    )

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

    # 插件评测配置（depth: quick/standard；plugin_path/skill_content 为任务级评测对象兜底来源）
    plugin_config = Column(JSON, nullable=True, comment="插件评测配置")

    # 裁判数量配置
    judge_count = Column(Integer, default=6, comment="裁判数量")

    # 裁判模型配置快照（W4，P0-C：创建时对 task.judge_model 指向的 ModelConfig
    # 深拷贝快照，口径字段+掩码凭据，严禁明文 api_key；执行时快照优先）
    judge_model_config = Column(JSON, nullable=True, comment="裁判模型配置快照（W4 创建时写入，仅掩码凭据）")

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

    # 门禁配置（M1 QE1-2，评测即门禁）：{enabled, mode: all|any, thresholds:
    # [{metric, op, value}]}，metric 取 total_score/pass_rate/dimension:<维度名>
    # （verification_pass_rate 随 M2 校验器接线）。空=未启用门禁（存量任务行为不变）
    gate_config = Column(
        JSON, nullable=True,
        comment="门禁配置（M1）：{enabled, mode: all|any, thresholds:[{metric,op,value}]},空=未启用",
    )

    # 终态校验器配置（M2 QE2-1，确定性终态校验优先）：[{type: script|db_assert|
    # file_assert|http_assert, name, on_fail: continue|abort, config: {...}}]。
    # 评测流水线在模型调用/被测 agent 执行之后运行（步骤1.6），输出
    # verification_result；聚合出 verification_pass_rate（接入门禁 metric）。
    # 空=未配置校验器（存量任务行为不变）。见 app/services/verifier_runner.py
    verifiers = Column(
        JSON, nullable=True,
        comment="终态校验器配置（M2）：[{type: script|db_assert|file_assert|http_assert, name, on_fail, config}],空=未配置",
    )

    # 轨迹断言配置（M2 QE2-2，agent 类型）：{mode: strict|unordered|subset|
    # superset, expected: [{tool, args_contains?, min_calls?}], forbidden:
    # [{tool, args_contains?}]}。会话（session_messages）的工具调用序列做
    # 确定性匹配，输出 trajectory_match（独立于裁判维度计分）；聚合出
    # trajectory_match_rate（接入门禁 metric）。空=未配置（行为不变）。
    # 见 app/services/trajectory_matcher.py
    trajectory_config = Column(
        JSON, nullable=True,
        comment="轨迹断言配置（M2，agent）：{mode, expected[], forbidden[]},空=未配置",
    )

    # 行级多 trial 次数（M2 QE2-3，judge 类型）：2-5，>1 时同一数据行
    # 重复跑「模型调用+裁判+校验」k 次合并为一行，行级输出 pass@1/pass^k
    # 与分数标准差/标准误（语义对齐 τ-bench pass^k，见 app/services/pass_k.py）。
    # success_rate/consistency/latency 的多次重复走各自 repeat_count 通道。
    # 空/1=不开启（存量任务行为不变）。
    trials = Column(Integer, nullable=True, comment="行级多 trial 次数（M2，judge）：2-5，空/1=不开启")

    # 裁判输入消毒关闭声明（M2 QE2-4，judge/agent）：prompt_guard 默认开启
    # （待评数据 <eval_data> 定界 + 免责指令）；置 true 为显式关闭（审计可见：
    # 每次执行的开关状态随 judge_config_snapshot 落执行记录）。
    judge_guard_disabled = Column(Boolean, nullable=True, default=False, comment="裁判输入消毒显式关闭（M2）：true=关闭定界+免责指令，默认false=开启")

    # 裁判金丝雀自检开关（M2 QE2-4，judge/agent）：true 时混入约 2%（至少
    # 1 行）空/乱码合成行直送裁判，裁判给高分（≥满分70%）则结果页标记
    # 「裁判可被欺骗风险」徽标；canary 行不计入均分（见 app/services/judge_canary.py）。
    judge_canary = Column(Boolean, nullable=True, default=False, comment="裁判金丝雀自检（M2）：true=混入空/乱码探针行，默认false=关闭")


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

    # W3-1 迁移新增（expand-only，一次迁移减少次数）：
    # config_snapshot 供 W4（配置快照归因锚点），heartbeat_at 供 W10（部署期收尸 reaper）
    config_snapshot = Column(JSON, nullable=True, comment="本次执行使用的模型/裁判配置快照（W4 写入，禁止落明文 api_key）")
    heartbeat_at = Column(DateTime(timezone=True), nullable=True, comment="执行心跳时间（W10 收尸 reaper 判断依据）")

    # 导出登记（插件 task_execution_export 幂等复用）：{format: {file_id, filename, size, sha256, uploaded_at}}
    exported_files = Column(JSON, nullable=True, comment="已上传 anc-hub file-store 的导出文档登记（按格式幂等复用 file_id）")

    # 门禁判定结果（M1 QE1-2）：{verdict: passed|failed|error, checks:
    # [{metric, op, value, actual, passed}], evaluated_at}。失败/停止收尾由
    # TaskExecutionRecordService 统一落 error verdict；空=该次执行未启用门禁
    gate_result = Column(
        JSON, nullable=True,
        comment="门禁判定（M1）：{verdict: passed|failed|error, checks:[...], evaluated_at},空=未启用",
    )

    # 裁判治理快照（M2 QE2-4）：本次裁决所用裁判配置——引擎/裁判模型摘要
    # （掩码凭据）/prompt 模板/维度定义/输入消毒与金丝雀开关状态。空=该次
    # 执行未落快照（存量记录）。见 config_snapshot.build_judge_config_snapshot
    judge_config_snapshot = Column(
        JSON, nullable=True,
        comment="裁判治理快照（M2）：{engine, judge_model, prompt_template, metrics, guard, canary}",
    )


class EvaluationResult(Base):
    """评测行级结果表（W3 行级结果落库，P0-E）

    逐行 write-through：每行评测终态（成功/失败/跳过）即时幂等 upsert 入本表，
    任务停止/worker 强杀/软超时后已完成行零丢失；断点续跑按
    (task_id, execution_number) 的 completed/judge_failed 行跳过。

    列语义（W3-1，库列为 JSONB，ORM 用通用 JSON 保持 SQLite 单测可建表）：
    - status: completed/judge_failed/failed/skipped/pending
    - input_data: 行输入侧平铺字段（查询投影，事实源为 row_result）
    - judge_results: 裁判明细（judge_scores）
    - aggregate: 行聚合分（average_score/feedback/dimension_scores/success）
    - row_result: 全量行结果原文——聚合/续跑重建 result JSON 的事实源
      （计划 DDL 之外的增列：无它则 result JSON 无法与旧行为同构重建）
    """
    __tablename__ = "evaluation_results"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    task_id = Column(String, ForeignKey("evaluation_tasks.id"), nullable=False)
    execution_number = Column(Integer, nullable=False, comment="第几次执行，与 task_execution_records.execution_number 同口径")
    data_index = Column(Integer, nullable=False, comment="数据项在本次执行数据集中的原始序号")
    status = Column(String, nullable=False, comment="状态：completed/judge_failed/failed/skipped/pending")
    input_data = Column(JSON, nullable=True, comment="行输入侧平铺字段（查询投影）")
    model_output = Column(Text, nullable=True, comment="被评测模型输出")
    model_raw_response = Column(Text, nullable=True, comment="模型原始响应")
    judge_results = Column(JSON, nullable=True, comment="裁判明细（judge_scores）")
    aggregate = Column(JSON, nullable=True, comment="行聚合分")
    row_result = Column(JSON, nullable=True, comment="全量行结果原文（聚合/续跑重建事实源）")
    # 终态校验器结果投影（M2 QE2-1）：{status: pass|fail|error, results: [逐校验器
    # 条目], passed/failed/errored/total, aborted}。事实源仍是 row_result
    # （verification_result 键同构），本列供行级查询投影
    verification_result = Column(
        JSON, nullable=True,
        comment="终态校验器结果（M2）：{status: pass|fail|error, results:[...], passed, total}",
    )
    # 轨迹匹配结果投影（M2 QE2-2）：{mode, status: pass|fail|error, matched,
    # missing[], unexpected[], forbidden_hits[], timeline}。事实源仍是
    # row_result（trajectory_match 键同构），本列供行级查询投影
    trajectory_match = Column(
        JSON, nullable=True,
        comment="轨迹匹配结果（M2）：{mode, status, matched, missing, unexpected, forbidden_hits, timeline}",
    )
    error = Column(Text, nullable=True, comment="行执行错误信息")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("task_id", "execution_number", "data_index", name="uq_eval_results_task_exec_data"),
    )
