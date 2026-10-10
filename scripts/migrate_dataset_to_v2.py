#!/usr/bin/env python3
"""
数据集迁移脚本 V2

将旧表 (evaluation_data) 中的数据迁移到新表 (evaluation_data_v2) 的 data JSON 字段。
V2 格式为完全扁平的 JSON，所有字段和 *_is_media 标志在同一层级。

迁移规则：
  - question     → data["user_input"]
  - ref_answer   → data["answer"]
  - meta_data 中的 user_input_is_media / answer_is_media → data 中的同名字段
  - meta_data 中的其他字段（如 files、multi_* 等）→ data 中的同名字段

用法:
    python scripts/migrate_dataset_to_v2.py --dataset-id <id>              # 迁移指定数据集
    python scripts/migrate_dataset_to_v2.py --all                         # 迁移所有数据集
    python scripts/migrate_dataset_to_v2.py --dataset-id <id> --dry-run   # 仅预览迁移计划
    python scripts/migrate_dataset_to_v2.py --all --force                 # 强制迁移（先清理已有 V2 数据）

备份与还原:
    python scripts/migrate_dataset_to_v2.py --dataset-id <id> --backup              # 迁移前自动备份
    python scripts/migrate_dataset_to_v2.py --all --backup                           # 迁移前自动备份所有
    python scripts/migrate_dataset_to_v2.py --restore --backup-dir backups/migrate_20260420_xxx  # 从备份还原
    python scripts/migrate_dataset_to_v2.py --restore --backup-dir backups/migrate_20260420_xxx --dataset-id <id>  # 还原指定数据集
"""
import argparse
import sys
import os
import uuid
import json
import shutil
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings

settings = Settings()
from app.models.dataset_models import EvaluationDataset, EvaluationData, EvaluationDataV2


def get_db_session():
    """获取数据库会话"""
    engine = create_engine(settings.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    return Session()


# ==================== 备份与还原 ====================

def create_backup(dataset_ids: list, db_session, backup_root: str = "backups") -> str:
    """创建数据集备份

    备份内容：
    1. evaluation_data_v2 表中对应数据集的数据（JSON 文件）
    2. evaluation_data 表中对应数据集的数据（JSON 文件）
    3. evaluation_datasets 表中数据集元信息

    Args:
        dataset_ids: 要备份的数据集 ID 列表
        db_session: 数据库会话
        backup_root: 备份根目录

    Returns:
        备份目录路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(backup_root, f"migrate_{timestamp}")
    os.makedirs(backup_dir, exist_ok=True)

    print(f"创建备份目录: {backup_dir}")

    manifest = {
        "created_at": datetime.now().isoformat(),
        "datasets": [],
        "total_v1": 0,
        "total_v2": 0,
    }

    for dataset_id in dataset_ids:
        # 获取数据集元信息
        dataset = db_session.query(EvaluationDataset).filter(
            EvaluationDataset.id == dataset_id
        ).first()

        if not dataset:
            print(f"  跳过不存在的数据集: {dataset_id}")
            continue

        dataset_dir = os.path.join(backup_dir, dataset_id)
        os.makedirs(dataset_dir, exist_ok=True)

        # 备份 V1 数据
        v1_items = db_session.query(EvaluationData).filter(
            EvaluationData.dataset_id == dataset_id,
            EvaluationData.is_active == True
        ).order_by(EvaluationData.index).all()

        v1_data = []
        for item in v1_items:
            meta = item.meta_data
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, ValueError):
                    meta = None
            v1_data.append({
                "id": item.id,
                "dataset_id": item.dataset_id,
                "index": item.index,
                "question": item.question,
                "ref_answer": item.ref_answer,
                "meta_data": meta,
                "is_active": item.is_active,
            })

        v1_path = os.path.join(dataset_dir, "evaluation_data.json")
        with open(v1_path, "w", encoding="utf-8") as f:
            json.dump(v1_data, f, ensure_ascii=False, indent=2)

        # 备份 V2 数据
        v2_items = db_session.query(EvaluationDataV2).filter(
            EvaluationDataV2.dataset_id == dataset_id
        ).order_by(EvaluationDataV2.index).all()

        v2_data = []
        for item in v2_items:
            v2_data.append({
                "id": item.id,
                "dataset_id": item.dataset_id,
                "index": item.index,
                "data": item.data,
                "is_active": item.is_active,
            })

        v2_path = os.path.join(dataset_dir, "evaluation_data_v2.json")
        with open(v2_path, "w", encoding="utf-8") as f:
            json.dump(v2_data, f, ensure_ascii=False, indent=2)

        # 备份数据集元信息
        dataset_meta = {
            "id": dataset.id,
            "name": dataset.name,
            "description": dataset.description,
            "question_count": dataset.question_count,
            "created_by": dataset.created_by,
            "team_code": dataset.team_code,
            "is_active": dataset.is_active,
        }
        meta_path = os.path.join(dataset_dir, "dataset_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(dataset_meta, f, ensure_ascii=False, indent=2)

        manifest["datasets"].append({
            "id": dataset_id,
            "name": dataset.name,
            "v1_count": len(v1_data),
            "v2_count": len(v2_data),
        })
        manifest["total_v1"] += len(v1_data)
        manifest["total_v2"] += len(v2_data)

        print(f"  已备份: {dataset.name} ({dataset_id}) — V1: {len(v1_data)} 条, V2: {len(v2_data)} 条")

    # 写入清单文件
    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n备份完成: {backup_dir}")
    print(f"  数据集: {len(manifest['datasets'])} 个")
    print(f"  V1 数据: {manifest['total_v1']} 条")
    print(f"  V2 数据: {manifest['total_v2']} 条")

    return backup_dir


def restore_backup(backup_dir: str, db_session, dataset_id: str = None) -> int:
    """从备份还原数据集

    还原逻辑：
    1. 读取 manifest.json 确认备份内容
    2. 删除对应数据集在 V1 和 V2 表中的所有数据（硬删除）
    3. 从备份 JSON 文件重新写入

    Args:
        backup_dir: 备份目录路径
        db_session: 数据库会话
        dataset_id: 指定还原的数据集 ID（None 则还原全部）

    Returns:
        还原的数据条数
    """
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"错误: 备份清单文件不存在: {manifest_path}")
        return 0

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"从备份还原: {backup_dir}")
    print(f"  备份时间: {manifest.get('created_at', '未知')}")
    print(f"  包含数据集: {len(manifest['datasets'])} 个")

    target_datasets = manifest["datasets"]
    if dataset_id:
        target_datasets = [d for d in target_datasets if d["id"] == dataset_id]
        if not target_datasets:
            print(f"错误: 备份中不包含数据集 {dataset_id}")
            return 0

    total_restored = 0

    for ds_info in target_datasets:
        ds_id = ds_info["id"]
        ds_name = ds_info["name"]
        dataset_dir = os.path.join(backup_dir, ds_id)

        if not os.path.exists(dataset_dir):
            print(f"  跳过: 备份目录不存在 {dataset_dir}")
            continue

        print(f"\n  还原数据集: {ds_name} ({ds_id})")

        # 还原数据集元信息
        meta_path = os.path.join(dataset_dir, "dataset_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                dataset_meta = json.load(f)

            dataset = db_session.query(EvaluationDataset).filter(
                EvaluationDataset.id == ds_id
            ).first()

            if dataset:
                dataset.name = dataset_meta.get("name", dataset.name)
                dataset.description = dataset_meta.get("description", dataset.description)
                dataset.question_count = dataset_meta.get("question_count", dataset.question_count)
                dataset.is_active = dataset_meta.get("is_active", True)
                print(f"    已更新数据集元信息")
            else:
                # 数据集不存在，重新创建
                dataset = EvaluationDataset(
                    id=ds_id,
                    name=dataset_meta["name"],
                    description=dataset_meta.get("description"),
                    question_count=dataset_meta.get("question_count", 0),
                    created_by=dataset_meta.get("created_by"),
                    team_code=dataset_meta.get("team_code", "DEFAULT"),
                    is_active=dataset_meta.get("is_active", True),
                )
                db_session.add(dataset)
                print(f"    已重建数据集元信息")

        # 还原 V1 数据（硬删除后重新插入）
        v1_path = os.path.join(dataset_dir, "evaluation_data.json")
        if os.path.exists(v1_path):
            with open(v1_path, "r", encoding="utf-8") as f:
                v1_data = json.load(f)

            # 硬删除 V1 表中该数据集的所有数据
            deleted_v1 = db_session.query(EvaluationData).filter(
                EvaluationData.dataset_id == ds_id
            ).delete(synchronize_session=False)
            print(f"    已清除 V1 表 {deleted_v1} 条旧数据")

            # 重新插入备份数据
            for item in v1_data:
                meta = item.get("meta_data")
                if meta is not None and not isinstance(meta, str):
                    meta = json.dumps(meta, ensure_ascii=False) if meta else None
                db_session.add(EvaluationData(
                    id=item["id"],
                    dataset_id=item["dataset_id"],
                    index=item["index"],
                    question=item.get("question", ""),
                    ref_answer=item.get("ref_answer", ""),
                    meta_data=meta,
                    is_active=item.get("is_active", True),
                ))
            print(f"    已还原 V1 数据: {len(v1_data)} 条")
            total_restored += len(v1_data)

        # 还原 V2 数据（硬删除后重新插入）
        v2_path = os.path.join(dataset_dir, "evaluation_data_v2.json")
        if os.path.exists(v2_path):
            with open(v2_path, "r", encoding="utf-8") as f:
                v2_data = json.load(f)

            # 硬删除 V2 表中该数据集的所有数据
            deleted_v2 = db_session.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == ds_id
            ).delete(synchronize_session=False)
            print(f"    已清除 V2 表 {deleted_v2} 条旧数据")

            # 重新插入备份数据
            for item in v2_data:
                db_session.add(EvaluationDataV2(
                    id=item["id"],
                    dataset_id=item["dataset_id"],
                    index=item["index"],
                    data=item.get("data", {}),
                    is_active=item.get("is_active", True),
                ))
            print(f"    已还原 V2 数据: {len(v2_data)} 条")
            total_restored += len(v2_data)

        db_session.commit()

    print(f"\n还原完成: 共 {total_restored} 条数据")
    return total_restored


# ==================== 迁移 ====================

def migrate_dataset(dataset_id: str, db_session, dry_run: bool = False, force: bool = False) -> int:
    """迁移单个数据集

    Args:
        dataset_id: 数据集ID
        db_session: 数据库会话
        dry_run: 是否仅预览
        force: 是否强制迁移（先清理已有 V2 数据）

    Returns:
        迁移的数据条数
    """
    # 获取数据集信息
    dataset = db_session.query(EvaluationDataset).filter(
        EvaluationDataset.id == dataset_id,
        EvaluationDataset.is_active == True
    ).first()

    if not dataset:
        print(f"数据集不存在: {dataset_id}")
        return 0

    # 获取旧表数据
    old_items = db_session.query(EvaluationData).filter(
        EvaluationData.dataset_id == dataset_id,
        EvaluationData.is_active == True
    ).order_by(EvaluationData.index).all()

    if not old_items:
        print(f"数据集 {dataset_id} 没有需要迁移的 V1 数据")
        return 0

    # 检查 V2 表是否已有数据
    existing_v2_count = db_session.query(EvaluationDataV2).filter(
        EvaluationDataV2.dataset_id == dataset_id,
        EvaluationDataV2.is_active == True
    ).count()

    if existing_v2_count > 0:
        if force:
            if not dry_run:
                # 清理已有 V2 数据
                db_session.query(EvaluationDataV2).filter(
                    EvaluationDataV2.dataset_id == dataset_id
                ).delete(synchronize_session=False)
                db_session.commit()
                print(f"已清理 {existing_v2_count} 条已有 V2 数据")
            else:
                print(f"[DRY RUN] 将清理 {existing_v2_count} 条已有 V2 数据")
        else:
            print(f"数据集 {dataset_id} 已有 {existing_v2_count} 条 V2 数据，跳过（使用 --force 强制重新迁移）")
            return 0

    print(f"数据集: {dataset.name} ({dataset_id})")
    print(f"需要迁移的 V1 数据: {len(old_items)} 条")

    if dry_run:
        print("\n[DRY RUN] 预览迁移计划:")
        for item in old_items[:5]:  # 只显示前5条
            new_data = _build_v2_data(item)
            print(f"  - {item.id}: {json.dumps(new_data, ensure_ascii=False)[:200]}")
        if len(old_items) > 5:
            print(f"  ... 还有 {len(old_items) - 5} 条数据")
        return len(old_items)

    # 执行迁移
    migrated_count = 0
    for item in old_items:
        new_data = _build_v2_data(item)

        # 创建 V2 数据记录
        new_item = EvaluationDataV2(
            id=f"data2_{uuid.uuid4().hex[:12]}",
            dataset_id=dataset_id,
            data=new_data,
            index=item.index,
            is_active=True,
        )
        db_session.add(new_item)
        migrated_count += 1

        if migrated_count % 100 == 0:
            print(f"已迁移: {migrated_count}/{len(old_items)}")

    # 更新数据集的 question_count
    dataset.question_count = migrated_count

    db_session.commit()
    print(f"迁移完成: {migrated_count} 条数据")

    return migrated_count


def _try_parse_json_string(value: str) -> dict:
    """尝试将 JSON 字符串解析为字典，如果不是 JSON 字符串则返回 None"""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value.startswith("{"):
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _build_v2_data(item: EvaluationData) -> dict:
    """将 V1 数据项构造为 V2 扁平 data JSON

    V1 的 question/ref_answer 可能是普通字符串，也可能是 JSON 字符串（多字段场景）。
    如果是 JSON 字符串，需要解析并扁平化到 data 中。
    """
    new_data = {}

    # 处理 question → user_input（可能是 JSON 字符串）
    question = item.question or ""
    question_parsed = _try_parse_json_string(question)
    if question_parsed:
        # JSON 字符串 → 扁平化合并
        for k, v in question_parsed.items():
            if k not in new_data:
                new_data[k] = v
    else:
        new_data["user_input"] = question

    # 处理 ref_answer → answer（可能是 JSON 字符串）
    ref_answer = item.ref_answer or ""
    answer_parsed = _try_parse_json_string(ref_answer)
    if answer_parsed:
        # JSON 字符串 → 扁平化合并（不覆盖已有的 key）
        for k, v in answer_parsed.items():
            if k not in new_data:
                new_data[k] = v
    else:
        new_data["answer"] = ref_answer

    # 如果最终没有 user_input 和 answer 字段，且也没有其他字段，设置默认空值
    if not new_data:
        new_data["user_input"] = ""
        new_data["answer"] = ""

    # 从 meta_data 中获取其他字段，合并到 data JSON 中
    if item.meta_data:
        meta = item.meta_data
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, ValueError):
                meta = {}
        if isinstance(meta, dict):
            for k, v in meta.items():
                if k not in new_data:
                    new_data[k] = v

    return new_data


# ==================== 回滚 ====================

def rollback_dataset(dataset_id: str, db_session, dry_run: bool = False) -> int:
    """回滚数据集迁移（从 V2 读回旧格式）

    Args:
        dataset_id: 数据集ID
        db_session: 数据库会话
        dry_run: 是否仅预览

    Returns:
        回滚的数据条数
    """
    # 获取数据集信息
    dataset = db_session.query(EvaluationDataset).filter(
        EvaluationDataset.id == dataset_id,
        EvaluationDataset.is_active == True
    ).first()

    if not dataset:
        print(f"数据集不存在: {dataset_id}")
        return 0

    # 获取 V2 表数据
    v2_items = db_session.query(EvaluationDataV2).filter(
        EvaluationDataV2.dataset_id == dataset_id,
        EvaluationDataV2.is_active == True
    ).order_by(EvaluationDataV2.index).all()

    if not v2_items:
        print(f"数据集 {dataset_id} V2 表中没有数据")
        return 0

    print(f"数据集: {dataset.name} ({dataset_id})")
    print(f"需要回滚的数据: {len(v2_items)} 条")

    if dry_run:
        print("\n[DRY RUN] 预览回滚计划:")
        for item in v2_items[:5]:  # 只显示前5条
            data = item.data or {}
            question = data.get("user_input", "")
            ref_answer = data.get("answer", "")
            print(f"  - {item.id}: user_input={question[:50]}, answer={ref_answer[:50]}")
        if len(v2_items) > 5:
            print(f"  ... 还有 {len(v2_items) - 5} 条数据")
        return len(v2_items)

    # 执行回滚
    rolled_back_count = 0
    for item in v2_items:
        data = item.data or {}

        # 提取 is_media 字段到 meta_data
        meta_data = {}
        for key, value in data.items():
            if key.endswith("_is_media"):
                meta_data[key] = value
            elif key.endswith("_files") or key == "files":
                meta_data[key] = value

        # 恢复旧表数据
        old_item = EvaluationData(
            id=f"data_{uuid.uuid4().hex[:12]}",
            dataset_id=dataset_id,
            index=item.index,
            question=data.get("user_input", ""),
            ref_answer=data.get("answer", ""),
            meta_data=meta_data if meta_data else None,
            is_active=True,
        )
        db_session.add(old_item)
        rolled_back_count += 1

        if rolled_back_count % 100 == 0:
            print(f"已回滚: {rolled_back_count}/{len(v2_items)}")

    db_session.commit()
    print(f"回滚完成: {rolled_back_count} 条数据")

    return rolled_back_count


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="数据集迁移脚本 V2")
    parser.add_argument("--dataset-id", type=str, help="指定数据集ID")
    parser.add_argument("--all", action="store_true", help="迁移所有数据集")
    parser.add_argument("--rollback", action="store_true", help="回滚（从 V2 读回旧格式）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览迁移计划，不实际执行")
    parser.add_argument("--force", action="store_true", help="强制迁移（先清理已有 V2 数据）")
    parser.add_argument("--backup", action="store_true", help="迁移前自动备份")
    parser.add_argument("--restore", action="store_true", help="从备份还原")
    parser.add_argument("--backup-dir", type=str, help="指定备份目录路径（用于 --restore）")
    parser.add_argument("--backup-root", type=str, default="backups", help="备份根目录（默认 backups/）")

    args = parser.parse_args()

    # 还原模式
    if args.restore:
        if not args.backup_dir:
            parser.print_help()
            print("\n错误: --restore 需要指定 --backup-dir")
            sys.exit(1)
        if not os.path.exists(args.backup_dir):
            print(f"错误: 备份目录不存在: {args.backup_dir}")
            sys.exit(1)

        db_session = get_db_session()
        try:
            restore_backup(args.backup_dir, db_session, args.dataset_id)
        finally:
            db_session.close()
        return

    # 迁移/回滚模式
    if not args.dataset_id and not args.all:
        parser.print_help()
        print("\n错误: 必须指定 --dataset-id 或 --all")
        sys.exit(1)

    db_session = get_db_session()

    try:
        # 收集需要操作的数据集 ID
        if args.all:
            datasets = db_session.query(EvaluationDataset).filter(
                EvaluationDataset.is_active == True
            ).all()
            dataset_ids = [d.id for d in datasets]
        else:
            dataset_ids = [args.dataset_id]

        # 备份
        if args.backup and not args.dry_run:
            print("=" * 60)
            print("步骤 1: 创建备份")
            print("=" * 60)
            backup_dir = create_backup(dataset_ids, db_session, args.backup_root)
            print()

        # 执行迁移或回滚
        print("=" * 60)
        if args.rollback:
            print("执行: 回滚")
        else:
            print("执行: 迁移")
        print("=" * 60)

        if args.all:
            print(f"找到 {len(datasets)} 个数据集\n")
            total = 0
            for dataset in datasets:
                if args.rollback:
                    count = rollback_dataset(dataset.id, db_session, args.dry_run)
                else:
                    count = migrate_dataset(dataset.id, db_session, args.dry_run, args.force)
                total += count
                print()
            print(f"总计: {total} 条数据")
        else:
            if args.rollback:
                rollback_dataset(args.dataset_id, db_session, args.dry_run)
            else:
                migrate_dataset(args.dataset_id, db_session, args.dry_run, args.force)

        # 提示备份还原方式
        if args.backup and not args.dry_run:
            print()
            print("=" * 60)
            print(f"如需还原，执行:")
            print(f"  python scripts/migrate_dataset_to_v2.py --restore --backup-dir {backup_dir}")
            print("=" * 60)

    finally:
        db_session.close()


if __name__ == "__main__":
    main()
