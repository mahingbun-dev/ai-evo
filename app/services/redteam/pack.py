"""内置红队攻击样本库（M3 QE3-1）

样本库以目录形式内置于 ``app/scripts_builtin/<pack_name>/``：

- ``manifest.json``：名称/版本/类目清单/样本 schema 说明；
- ``samples/<category>.jsonl``：每类一个 JSONL 文件，每行一个攻击样本
  （id/category/prompt/expected_behavior/risk_point/severity）。

支持两种消费方式：
1. 安全评测任务 ``sample_source="pack"`` 直接引用（样本不可变，结果可复现，
   pack+version 随 safety_config 落任务）；
2. 「可导入可编辑」：经 ``POST /evaluation/redteam/packs/{name}/import-dataset``
   导入为普通数据集，之后与任何数据集一样可增删改（口径改为
   ``sample_source="dataset"``）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 样本库根目录（scripts_builtin 与内置脚本同源，随代码版本管理）
PACKS_ROOT = Path(__file__).resolve().parent.parent.parent / "scripts_builtin"

#: 样本必填字段
_SAMPLE_REQUIRED_FIELDS = ("id", "category", "prompt")

#: 类目中文名映射（四类攻击面，M3 首批）
ATTACK_SURFACE_NAMES = {
    "prompt_injection": "提示注入",
    "jailbreak": "越狱",
    "sensitive_content": "敏感内容",
    "factual_assertion": "事实性断言",
}


def _pack_dir(pack_name: str) -> Path:
    """样本库目录（pack_name 只允许 [a-z0-9_]，防路径穿越）"""
    if not pack_name or not all(c.isalnum() or c == "_" for c in pack_name) or not pack_name[0].isalpha():
        raise ValueError(f"非法样本库名称: {pack_name!r}")
    return PACKS_ROOT / pack_name


def list_packs() -> List[Dict[str, Any]]:
    """列出全部内置样本库（含版本/类目/样本数）"""
    packs: List[Dict[str, Any]] = []
    if not PACKS_ROOT.exists():
        return packs
    for child in sorted(PACKS_ROOT.iterdir()):
        if not child.is_dir() or not (child / "manifest.json").exists():
            continue
        try:
            packs.append(_load_manifest_with_counts(child.name))
        except Exception as e:  # noqa: BLE001 - 单个库损坏不影响其余库列出
            logger.warning("样本库 %s 读取失败: %s", child.name, e)
    return packs


def _load_manifest_with_counts(pack_name: str) -> Dict[str, Any]:
    pack_path = _pack_dir(pack_name)
    manifest = json.loads((pack_path / "manifest.json").read_text(encoding="utf-8"))
    categories = []
    for cat in manifest.get("categories", []):
        key = cat.get("key", "")
        cat_info = dict(cat)
        cat_info["sample_count"] = _count_samples(pack_path, key)
        categories.append(cat_info)
    manifest["categories"] = categories
    return manifest


def _count_samples(pack_path: Path, category: str) -> int:
    file = pack_path / "samples" / f"{category}.jsonl"
    if not file.exists():
        return 0
    return sum(1 for line in file.read_text(encoding="utf-8").splitlines() if line.strip())


def _load_samples_file(pack_path: Path, category: str) -> List[Dict[str, Any]]:
    file = pack_path / "samples" / f"{category}.jsonl"
    if not file.exists():
        raise ValueError(f"样本库 {pack_path.name} 缺少类目文件 samples/{category}.jsonl")
    samples: List[Dict[str, Any]] = []
    for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"样本库 {pack_path.name}/{category}.jsonl 第 {line_no} 行 JSON 解析失败: {e}")
        for field_name in _SAMPLE_REQUIRED_FIELDS:
            if not sample.get(field_name):
                raise ValueError(
                    f"样本库 {pack_path.name}/{category}.jsonl 第 {line_no} 行缺必填字段 {field_name}"
                )
        samples.append(sample)
    return samples


def load_pack(
    pack_name: str,
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """加载内置样本库

    Args:
        pack_name: 样本库名（当前内置 redteam_pack_v1）
        categories: 攻击面子集（None = 全部类目）

    Returns:
        ``{"name", "version", "categories": [...], "samples": [...]}``

    Raises:
        ValueError: 库不存在/类目不存在/样本文件损坏
    """
    pack_path = _pack_dir(pack_name)
    manifest_file = pack_path / "manifest.json"
    if not manifest_file.exists():
        raise ValueError(f"内置样本库不存在: {pack_name}（可选: {', '.join(p['name'] for p in list_packs())}）")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    available = {c["key"] for c in manifest.get("categories", [])}
    if categories is None:
        categories = sorted(available)
    unknown = [c for c in categories if c not in available]
    if unknown:
        raise ValueError(f"样本库 {pack_name} 不支持类目: {unknown}，可选: {sorted(available)}")

    samples: List[Dict[str, Any]] = []
    for category in categories:
        samples.extend(_load_samples_file(pack_path, category))

    return {
        "name": manifest.get("name", pack_name),
        "version": manifest.get("version", ""),
        "categories": categories,
        "samples": samples,
    }


def get_pack_version(pack_name: str) -> str:
    """读取样本库版本号（safety_config 落任务时快照用）"""
    manifest_file = _pack_dir(pack_name) / "manifest.json"
    if not manifest_file.exists():
        raise ValueError(f"内置样本库不存在: {pack_name}")
    return json.loads(manifest_file.read_text(encoding="utf-8")).get("version", "")


def pack_samples_as_dataset_rows(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """攻击样本 → 数据集行（「可导入可编辑」通道）

    产出 V2 数据集 data 字段结构：user_input=攻击提示词，其余样本元数据
    平铺（attack_category/attack_sample_id/expected_behavior/risk_point/
    severity），安全评测执行链从这些字段取攻击面与期望行为。
    """
    rows = []
    for sample in samples:
        rows.append({
            "user_input": sample["prompt"],
            "answer": "",
            "attack_category": sample["category"],
            "attack_sample_id": sample["id"],
            "expected_behavior": sample.get("expected_behavior", ""),
            "risk_point": sample.get("risk_point", ""),
            "severity": sample.get("severity", ""),
        })
    return rows
