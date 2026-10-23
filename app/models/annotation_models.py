"""人工标注数据模型（M8 QE8-1，标注闭环）

行级人工金分（gold score）标注：对 evaluation_results 的某一行打人工分，
配合裁判分做「裁判-人工一致性」度量（Pearson/Spearman + 分带 Cohen's kappa，
见 app/services/annotation_service.py）。

设计口径：
- 一行结果最多一条标注（uq_annotations_result_row）：重复提交为幂等覆盖
  （标注修正场景），不建多标注者平行版本（一致性度量是裁判 vs 人工，
  非标注者间一致性）；
- 标注状态：completed-已打分（gold_score 必填）/ skipped-跳过（gold_score
  空，如该行无法判定）；「待标」不是本表状态——队列里的待标行 =
  该 (task_id, execution_number) 下尚无标注行的 evaluation_results 行；
- result_row_id 级联删除：任务/执行记录清理时标注随之清理（行结果没了，
  标注失去锚点）；task_id/execution_number/data_index 为冗余投影列，
  供一致性报告按任务/执行聚合免回Join。
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func
from sqlalchemy.types import Integer as SaInteger

from app.db.session import Base


class ResultAnnotation(Base):
    """行级人工标注（金分）表（M8 QE8-1）"""
    __tablename__ = "annotations"

    id = Column(
        BigInteger().with_variant(SaInteger(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    # 锚定行级结果：一行结果最多一条标注（幂等覆盖），行删除标注级联清理
    result_row_id = Column(
        BigInteger().with_variant(SaInteger(), "sqlite"),
        ForeignKey("evaluation_results.id", ondelete="CASCADE"),
        nullable=False,
        comment="锚定的行级结果 evaluation_results.id",
    )
    task_id = Column(String, nullable=False, index=True, comment="任务ID（冗余投影，供一致性报告聚合）")
    execution_number = Column(Integer, nullable=False, comment="执行序号（与 evaluation_results 同口径）")
    data_index = Column(Integer, nullable=False, comment="数据行原始序号（展示用）")
    gold_score = Column(Float, nullable=True, comment="人工金分（status=completed 必填；skipped 为空）")
    status = Column(
        String(20), nullable=False, default="completed", server_default="completed",
        comment="标注状态：completed-已打分 / skipped-跳过（无法判定）",
    )
    annotator = Column(String, nullable=True, comment="标注人 user_code")
    notes = Column(Text, nullable=True, comment="标注备注（争议点/判定依据）")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        UniqueConstraint("result_row_id", name="uq_annotations_result_row"),
    )
