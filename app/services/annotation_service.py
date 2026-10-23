"""人工标注闭环服务（M8 QE8-1）

三个职责（plans/2026-10-22-Agent质量工程平台开发方案.md §M8）：
1. 标注队列：按 (task_id, execution_number) 取行级结果，失败行/低分行
   优先排序（人工注意力先投给最可疑的行）；
2. 金分提交：一行结果一条标注，幂等覆盖（标注修正）；
3. 裁判-人工一致性报告：Pearson/Spearman（scipy，仓库已直依赖钉版）+
   分带 Cohen's kappa（纯实现，分带=得分率 <60% 低 / <80% 中 / 其余高，
   分带阈值与满分按各任务自身 max_score 归一）。

一致性报告作为裁判可信度证据（销售物料）：报告口径只吃
status=completed 且金分非空的标注；裁判分缺失行（judge_failed）单独
计数不计入相关性样本。
"""
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.annotation_models import ResultAnnotation
from app.models.task_models import EvaluationResult, EvaluationTask

# 分带阈值（得分率 = 分数 / 该任务 max_score）：[0,0.6) 低 / [0.6,0.8) 中 / [0.8,1] 高
BAND_EDGES: Tuple[float, float] = (0.6, 0.8)
BANDS: Tuple[str, str, str] = ("low", "mid", "high")
BAND_LABELS: Dict[str, str] = {"low": "低", "mid": "中", "high": "高"}

_FAILED_STATUSES = ("failed", "judge_failed")


class AnnotationError(ValueError):
    """标注业务校验失败（API 层按 status_code 映射 400/404）"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------- 队列

def _judge_score(row: EvaluationResult) -> Optional[float]:
    """行裁判聚合分（aggregate.average_score；无裁判分的行返回 None）"""
    agg = row.aggregate if isinstance(row.aggregate, dict) else {}
    score = agg.get("average_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return None


def _annotation_payload(a: ResultAnnotation) -> Dict[str, Any]:
    return {
        "gold_score": a.gold_score,
        "status": a.status,
        "annotator": a.annotator,
        "notes": a.notes,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def get_annotation_queue(
    db: Session, task: EvaluationTask, execution_number: int
) -> Dict[str, Any]:
    """标注队列：失败行优先，其次低分升序（人工注意力先投给最可疑的行）

    返回全量行（含已标注行——前端按 annotation 是否为空跳转下一个待标行，
    已标注行可回看/修正）；行数=单次执行数据集规模（通常数十~数百），排序
    在 Python 侧完成（aggregate 是 JSON 列，跨方言排序不可移植）。
    """
    rows = (
        db.query(EvaluationResult)
        .filter(
            EvaluationResult.task_id == task.id,
            EvaluationResult.execution_number == execution_number,
        )
        .all()
    )
    annotations = {
        a.result_row_id: a
        for a in db.query(ResultAnnotation).filter(
            ResultAnnotation.task_id == task.id,
            ResultAnnotation.execution_number == execution_number,
        )
    }

    def sort_key(row: EvaluationResult):
        score = _judge_score(row)
        if row.status in _FAILED_STATUSES:
            # 失败行最优先（执行失败/裁判失败），组内按数据序
            return (0, 0.0, row.data_index)
        if score is not None:
            # 低分优先（升序）
            return (1, score, row.data_index)
        return (2, 0.0, row.data_index)

    items = []
    for rank, row in enumerate(sorted(rows, key=sort_key), start=1):
        annotation = annotations.get(row.id)
        agg = row.aggregate if isinstance(row.aggregate, dict) else {}
        items.append({
            "priority_rank": rank,
            "result_row_id": row.id,
            "data_index": row.data_index,
            "row_status": row.status,
            "judge_score": _judge_score(row),
            "feedback": agg.get("feedback"),
            "input_data": row.input_data,
            "model_output": row.model_output,
            "annotation": _annotation_payload(annotation) if annotation else None,
        })

    done = sum(1 for i in items if i["annotation"])
    return {
        "task_id": task.id,
        "execution_number": execution_number,
        "max_score": task.max_score or 10,
        "total": len(items),
        "annotated": done,
        "pending": len(items) - done,
        "items": items,
    }


# ---------------------------------------------------------------- 提交

def submit_annotation(
    db: Session,
    task: EvaluationTask,
    *,
    result_row_id: int,
    gold_score: Optional[float],
    status: str = "completed",
    notes: Optional[str] = None,
    annotator: Optional[str] = None,
) -> Dict[str, Any]:
    """提交/修正一行标注：一行结果一条标注，重复提交幂等覆盖"""
    if status not in ("completed", "skipped"):
        raise AnnotationError("status 只支持 completed / skipped")
    max_score = task.max_score or 10
    if status == "completed":
        if gold_score is None:
            raise AnnotationError("已打分标注必须提供 gold_score")
        if not (0 <= gold_score <= max_score):
            raise AnnotationError(f"gold_score 须在 0~{max_score}（该任务最大分值）之间")
    else:
        gold_score = None  # 跳过行不留分（半吊子金分会污染一致性样本）

    row = db.query(EvaluationResult).filter(EvaluationResult.id == result_row_id).first()
    if row is None:
        raise AnnotationError("行级结果不存在", 404)
    if row.task_id != task.id:
        raise AnnotationError("该行结果不属于当前任务", 404)

    annotation = (
        db.query(ResultAnnotation)
        .filter(ResultAnnotation.result_row_id == result_row_id)
        .first()
    )
    if annotation is None:
        annotation = ResultAnnotation(
            result_row_id=result_row_id,
            task_id=row.task_id,
            execution_number=row.execution_number,
            data_index=row.data_index,
        )
        db.add(annotation)
    annotation.gold_score = gold_score
    annotation.status = status
    annotation.annotator = annotator
    annotation.notes = notes
    db.commit()
    db.refresh(annotation)
    return _annotation_payload(annotation)


# ---------------------------------------------------------------- 一致性报告

def _band(score: float, max_score: int) -> str:
    """得分率分带：<60% 低 / <80% 中 / 其余高（按各任务自身满分归一）"""
    ratio = score / (max_score or 10)
    if ratio < BAND_EDGES[0]:
        return "low"
    if ratio < BAND_EDGES[1]:
        return "mid"
    return "high"


def _pearson(xs: List[float], ys: List[float]) -> Optional[Dict[str, float]]:
    """Pearson r（scipy；样本不足/零方差返回 None 而非 nan）"""
    if len(xs) < 2 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    from scipy.stats import pearsonr

    result = pearsonr(xs, ys)
    return {"r": round(float(result.statistic), 4),
            "p": round(float(result.pvalue), 4)}


def _spearman(xs: List[float], ys: List[float]) -> Optional[Dict[str, float]]:
    """Spearman ρ（scipy；样本不足/零方差返回 None）"""
    if len(xs) < 2 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    from scipy.stats import spearmanr

    result = spearmanr(xs, ys)
    return {"rho": round(float(result.statistic), 4),
            "p": round(float(result.pvalue), 4)}


def _cohen_kappa(labels_a: List[str], labels_b: List[str]) -> Optional[float]:
    """Cohen's kappa（未加权，纯实现——sklearn 非仓库依赖）

    labels_a/b 为等长分带标签序列（a=裁判带，b=人工带）；类别全集为并集，
    pe=1（单一带别/全同类）时 kappa 无定义返回 None。
    """
    n = len(labels_a)
    if n == 0:
        return None
    categories = sorted(set(labels_a) | set(labels_b))
    po = sum(1 for a, b in zip(labels_a, labels_b) if a == b) / n
    pe = 0.0
    for c in categories:
        pa = labels_a.count(c) / n
        pb = labels_b.count(c) / n
        pe += pa * pb
    if pe >= 1.0:
        return None
    return round((po - pe) / (1 - pe), 4)


def _confusion_matrix(labels_a: List[str], labels_b: List[str]) -> List[List[int]]:
    """3x3 混淆矩阵（行=裁判带，列=人工带，带别顺序按 BANDS）"""
    matrix = [[0] * len(BANDS) for _ in BANDS]
    for a, b in zip(labels_a, labels_b):
        if a in BANDS and b in BANDS:
            matrix[BANDS.index(a)][BANDS.index(b)] += 1
    return matrix


def _collect_pairs(
    db: Session, task_id: Optional[str], execution_number: Optional[int]
) -> Tuple[List[Dict[str, Any]], int, int]:
    """(task_id, execution_number) 过滤下的一致性样本对

    Returns: (pairs, skipped_count, excluded_no_judge)
    - pairs: [{task_id, task_name, data_index, judge_score, gold_score, max_score}]
    - skipped_count: skipped 标注数（未打分，不进样本）
    - excluded_no_judge: 已打分但行无裁判分（judge_failed 等）数
    """
    query = db.query(ResultAnnotation, EvaluationResult).join(
        EvaluationResult, ResultAnnotation.result_row_id == EvaluationResult.id
    )
    if task_id:
        query = query.filter(ResultAnnotation.task_id == task_id)
        if execution_number:
            query = query.filter(ResultAnnotation.execution_number == execution_number)

    tasks = {t.id: t for t in db.query(EvaluationTask).all()}
    pairs: List[Dict[str, Any]] = []
    skipped = 0
    no_judge = 0
    for annotation, row in query.all():
        if annotation.status != "completed" or annotation.gold_score is None:
            skipped += 1
            continue
        judge_score = _judge_score(row)
        if judge_score is None:
            no_judge += 1
            continue
        task = tasks.get(row.task_id)
        pairs.append({
            "task_id": row.task_id,
            "task_name": task.name if task else row.task_id,
            "data_index": row.data_index,
            "judge_score": judge_score,
            "gold_score": float(annotation.gold_score),
            "max_score": (task.max_score if task else None) or 10,
        })
    return pairs, skipped, no_judge


def _report_for_pairs(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一组样本对的度量：相关性 + 分带 kappa + 混淆矩阵"""
    judge_scores = [p["judge_score"] for p in pairs]
    gold_scores = [p["gold_score"] for p in pairs]
    judge_bands = [_band(p["judge_score"], p["max_score"]) for p in pairs]
    gold_bands = [_band(p["gold_score"], p["max_score"]) for p in pairs]
    n = len(pairs)
    agreement = sum(1 for a, b in zip(judge_bands, gold_bands) if a == b)
    mae = (
        sum(abs(j - g) for j, g in zip(judge_scores, gold_scores)) / n
        if n else None
    )
    return {
        "sample_count": n,
        "pearson": _pearson(judge_scores, gold_scores),
        "spearman": _spearman(judge_scores, gold_scores),
        "cohen_kappa": _cohen_kappa(judge_bands, gold_bands),
        "band_agreement_rate": round(agreement / n, 4) if n else None,
        "mae": round(mae, 4) if mae is not None else None,
        "confusion": {
            "bands": list(BANDS),
            "band_labels": {b: BAND_LABELS[b] for b in BANDS},
            "band_edges": list(BAND_EDGES),
            "matrix": _confusion_matrix(judge_bands, gold_bands),
        },
    }


def judge_agreement_report(
    db: Session,
    task_id: Optional[str] = None,
    execution_number: Optional[int] = None,
) -> Dict[str, Any]:
    """裁判-人工一致性报告（Statistics 展示；裁判可信度证据）

    task_id 为空=全任务聚合（附 by_task 逐任务摘要）；指定任务时
    execution_number 可选锁定单次执行。
    """
    pairs, skipped, no_judge = _collect_pairs(db, task_id, execution_number)
    report = _report_for_pairs(pairs)
    report.update({
        "task_id": task_id,
        "execution_number": execution_number,
        "skipped_count": skipped,
        "excluded_no_judge": no_judge,
    })

    if task_id:
        return report

    # 全任务聚合：逐任务摘要（带别各自归一到该任务 max_score）
    by_task: Dict[str, Dict[str, Any]] = {}
    for p in pairs:
        entry = by_task.setdefault(p["task_id"], {
            "task_id": p["task_id"], "task_name": p["task_name"],
            "max_score": p["max_score"], "pairs": [],
        })
        entry["pairs"].append(p)
    report["by_task"] = [
        {"task_id": t["task_id"], "task_name": t["task_name"],
         "max_score": t["max_score"], **_report_for_pairs(t["pairs"])}
        for t in sorted(by_task.values(), key=lambda t: t["task_id"])
    ]
    return report
