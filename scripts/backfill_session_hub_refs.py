"""存量数据补登记：历史会话导入（from-hub）只解析未物化执行包

背景：from-hub 会话导入早期版本只把文件下载到临时目录解析，未调用
ensure_unpacked 登记 + 解包到持久区，导致数据行 session_file_id 引用的
执行包在文件抽屉/评测执行时报「执行包未登记」。

本脚本幂等补齐：登记缺失走 ensure_unpacked（已登记命中零下载），
ref_count 按活跃数据行逐行多重补到所需值（与释放端粒度一致）。

用法（须在仓库根目录执行）：
    .venv/bin/python scripts/backfill_session_hub_refs.py --dry-run  # 只看计划
    .venv/bin/python scripts/backfill_session_hub_refs.py            # 执行
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal
from app.services.hub_file_service import backfill_refs_for_hub_file_datasets


def main():
    parser = argparse.ArgumentParser(description="存量会话数据集执行包补登记（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只统计与展示计划，不做任何写操作")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = backfill_refs_for_hub_file_datasets(db, dry_run=args.dry_run)
    finally:
        db.close()

    print("需要的引用计数（file_id -> 逐行多重总数）:")
    print(json.dumps(result["needed"], ensure_ascii=False, indent=2))
    if args.dry_run:
        print("\n[dry-run] 未做任何写操作")
        return
    print("\n物化结果:")
    for item in result["materialized"]:
        print(f"  file_id={item['file_id']} cached={item['cached']}")
    print("\n补齐的引用:")
    for fid, add in result["refilled"].items():
        print(f"  file_id={fid} +{add}")
    if result["errors"]:
        print("\n失败（未阻断其余补登记）:")
        for fid, msg in result["errors"].items():
            print(f"  file_id={fid} 错误: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
