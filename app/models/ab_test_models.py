"""
A/B 测试会话数据模型
"""
from sqlalchemy import Column, String, DateTime, JSON, Float, Integer
from sqlalchemy.sql import func
from app.db.session import Base


class ABTestSession(Base):
    """A/B 测试会话记录表"""
    __tablename__ = "ab_test_sessions"

    id = Column(String, primary_key=True, index=True, comment="主键，格式：abtest_{uuid12}")
    name = Column(String, nullable=True, comment="可选备注名")

    # 会话模式（M5 QE5-2）：model=手工会话式双模型对比（存量，默认）；
    # template=模板版本对照（任务化实验：同源任务 × 两模板版本 → 两个变体任务，
    # 对照结果取各变体任务最新执行记录，历史天然可留存可比）
    mode = Column(String, default="model", server_default="model",
                  comment="会话模式：model-双模型对比（存量默认）/template-模板版本对照（M5）")

    # 模型信息（冗余存储方便列表展示；template 模式为空，对照对象是模板版本）
    # M5 QE5-2：放宽非空约束——template 模式会话无模型对（存量 model 行不受影响）
    model_a_id = Column(String, nullable=True, comment="模型A的配置ID（template模式为空）")
    model_b_id = Column(String, nullable=True, comment="模型B的配置ID（template模式为空）")
    model_a_name = Column(String, nullable=True, comment="模型A名称（冗余）")
    model_b_name = Column(String, nullable=True, comment="模型B名称（冗余）")

    # ==================== 模板版本对照模式（M5 QE5-2） ====================
    # 源任务：变体任务由此任务复制（仅模板派生字段被版本内容覆盖）
    source_task_id = Column(String, nullable=True, comment="模板对照：源评测任务ID")
    template_id = Column(String, nullable=True, comment="模板对照：评测模板ID")
    template_version_a = Column(Integer, nullable=True, comment="模板对照：A 侧版本号")
    template_version_b = Column(Integer, nullable=True, comment="模板对照：B 侧版本号")
    task_a_id = Column(String, nullable=True, comment="模板对照：A 侧变体评测任务ID")
    task_b_id = Column(String, nullable=True, comment="模板对照：B 侧变体评测任务ID")

    # 输入变量（{var: {value, type}} 格式，与WS协议一致）
    variables = Column(JSON, nullable=True, comment="共享变量配置")

    # 执行结果（{success, content, request_details[]}）
    result_a = Column(JSON, nullable=True, comment="模型A的完整执行结果")
    result_b = Column(JSON, nullable=True, comment="模型B的完整执行结果")

    # 状态与耗时
    status = Column(String, default="completed", comment="状态：completed/failed")
    duration_a = Column(Float, nullable=True, comment="模型A耗时（秒）")
    duration_b = Column(Float, nullable=True, comment="模型B耗时（秒）")

    # 显著性统计（M2 QE2-3）：多轮（同输入行配对）时由 ab_statistics 计算，
    # 含耗时配对检验（Wilcoxon/配对 t/Cliff's delta）与成功比例 McNemar；
    # 单轮/旧记录为 NULL。见 app/services/ab_statistics.py
    significance = Column(JSON, nullable=True, comment="显著性统计（M2）：{duration_paired, success_mcnemar, rounds}，单轮为NULL")

    # W19 归属：创建人 user_code。历史行为 NULL——读对所有登录用户可见，
    # 删除仅创建人或超管（与 access_control 团队口径互补的轻量归属）。
    created_by = Column(String, nullable=True, comment="创建人 user_code（历史行为 NULL）")

    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
