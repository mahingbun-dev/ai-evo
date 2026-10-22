"""红队安全评测域包（M3 QE3-1）

safety 类型双通道红队引擎的组成部分：

- :mod:`app.services.redteam.detectors` 确定性检测器（中文 PII/敏感词，证据高亮）
- :mod:`app.services.redteam.pack` 内置攻击样本库（redteam_pack_v1，带版本）
- :mod:`app.services.redteam.rubric` 裁判 rubric 通道（攻击面动态维度）
- :mod:`app.services.redteam.mutator` 变异增强（裁判模型改写样本变体）
- :mod:`app.services.redteam.engine` 双通道总装引擎

safety 类型任务执行链入口见 :mod:`app.services.evaluators.safety`
（evaluator_registry 适配器）。
"""
from app.services.redteam.detectors import (
    DEFAULT_SENSITIVE_WORDS,
    DeterministicSafetyChecker,
    PIIDetector,
    SafetyFinding,
    SensitiveWordDetector,
)
from app.services.redteam.engine import RedTeamSafetyEngine
from app.services.redteam.mutator import mutate_samples
from app.services.redteam.pack import (
    ATTACK_SURFACE_NAMES,
    load_pack,
    list_packs,
    pack_samples_as_dataset_rows,
)
from app.services.redteam.rubric import ATTACK_SURFACES

__all__ = [
    "ATTACK_SURFACE_NAMES",
    "ATTACK_SURFACES",
    "DEFAULT_SENSITIVE_WORDS",
    "DeterministicSafetyChecker",
    "PIIDetector",
    "RedTeamSafetyEngine",
    "SafetyFinding",
    "SensitiveWordDetector",
    "load_pack",
    "list_packs",
    "mutate_samples",
    "pack_samples_as_dataset_rows",
]
