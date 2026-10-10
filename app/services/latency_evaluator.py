"""
模型响应耗时评测服务
"""
import time
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime


@dataclass
class LatencyRecord:
    """单次耗时记录"""
    index: int
    duration: float
    timestamp: str
    passed: bool
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass
class LatencyResult:
    """耗时评测结果"""
    total_count: int
    passed_count: int
    failed_count: int
    pass_rate: float
    avg_duration: float
    min_duration: float
    max_duration: float
    p50_duration: float
    p90_duration: float
    p99_duration: float
    score: float
    threshold: float
    records: List[LatencyRecord] = field(default_factory=list)


class LatencyEvaluator:
    """耗时评测器"""

    def __init__(self, threshold: float = 10.0, score_mode: str = "inverse"):
        """
        初始化

        Args:
            threshold: 响应时间阈值（秒）
            score_mode: 评分模式，"inverse"表示时间越短分数越高
        """
        self.threshold = threshold
        self.score_mode = score_mode

    def evaluate(
        self,
        durations: List[float],
        input_tokens: Optional[List[int]] = None,
        output_tokens: Optional[List[int]] = None,
    ) -> LatencyResult:
        """
        评估耗时数据

        Args:
            durations: 响应时间列表（秒）
            input_tokens: 输入token数列表
            output_tokens: 输出token数列表

        Returns:
            LatencyResult: 评测结果
        """
        if not durations:
            return LatencyResult(
                total_count=0, passed_count=0, failed_count=0,
                pass_rate=0.0, avg_duration=0.0, min_duration=0.0,
                max_duration=0.0, p50_duration=0.0, p90_duration=0.0,
                p99_duration=0.0, score=0.0, threshold=self.threshold
            )

        total_count = len(durations)
        passed_count = sum(1 for d in durations if d <= self.threshold)
        failed_count = total_count - passed_count
        pass_rate = passed_count / total_count if total_count > 0 else 0.0

        avg_duration = statistics.mean(durations)
        min_duration = min(durations)
        max_duration = max(durations)

        sorted_durations = sorted(durations)
        p50_duration = self._percentile(sorted_durations, 50)
        p90_duration = self._percentile(sorted_durations, 90)
        p99_duration = self._percentile(sorted_durations, 99)

        # 计算评分：inverse模式下，时间越短分数越高
        score = self._calculate_score(avg_duration)

        # 构建单次记录
        records = []
        for i, duration in enumerate(durations):
            records.append(LatencyRecord(
                index=i + 1,
                duration=duration,
                timestamp=datetime.now().isoformat(),
                passed=duration <= self.threshold,
                input_tokens=input_tokens[i] if input_tokens and i < len(input_tokens) else None,
                output_tokens=output_tokens[i] if output_tokens and i < len(output_tokens) else None,
            ))

        return LatencyResult(
            total_count=total_count,
            passed_count=passed_count,
            failed_count=failed_count,
            pass_rate=pass_rate,
            avg_duration=avg_duration,
            min_duration=min_duration,
            max_duration=max_duration,
            p50_duration=p50_duration,
            p90_duration=p90_duration,
            p99_duration=p99_duration,
            score=score,
            threshold=self.threshold,
            records=records,
        )

    def _percentile(self, sorted_data: List[float], percentile: int) -> float:
        """计算百分位数"""
        if not sorted_data:
            return 0.0
        n = len(sorted_data)
        idx = int(n * percentile / 100)
        idx = min(idx, n - 1)
        return sorted_data[idx]

    def _calculate_score(self, avg_duration: float) -> float:
        """
        计算百分制评分

        inverse模式：时间越短分数越高
        - avg_duration <= threshold: 100分
        - avg_duration >= threshold * 3: 0分
        - 中间按线性插值
        """
        if self.score_mode != "inverse":
            return 50.0  # 默认50分

        max_duration = self.threshold * 3
        if avg_duration <= self.threshold:
            return 100.0
        elif avg_duration >= max_duration:
            return 0.0
        else:
            # 线性插值: 100 - 100 * (avg_duration - threshold) / (max_duration - threshold)
            score = 100.0 * (1 - (avg_duration - self.threshold) / (max_duration - self.threshold))
            return max(0.0, min(100.0, score))