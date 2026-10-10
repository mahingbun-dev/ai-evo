#!/usr/bin/env python3
"""校验 .offline-snapshot.json 与锁文件哈希的一致性

本地与 CI 共用：锁文件（requirements.txt 变更 → requirements.lock、
frontend/pnpm-lock.yaml）变更后若没有重跑 tools/prepare-offline.sh 并
上传快照，仓库里的 .offline-snapshot.json 记录的哈希就会与锁文件脱节，
main 构建会因此失败。本脚本提前到 push 阶段拦截，并给出修复指引。

CI 用法（check-snapshot job）：python3 tools/check-snapshot.py
本地用法：.venv/bin/python tools/check-snapshot.py（或任意 python3）

修复指引（不一致时）：
  1. ./tools/prepare-offline.sh                # 开发机，需联网 + Docker
  2. GITLAB_UPLOAD_TOKEN=<deploy token> ./tools/upload-offline-snapshot.sh
  3. git add requirements.lock .offline-snapshot.json && git commit
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META_FILE = ROOT / ".offline-snapshot.json"
# 与 build-service 的 check_offline_snapshot 保持同一组配对
PAIRS = [
    ("requirements_lock_hash", "requirements.lock"),
    ("pnpm_lock_hash", "frontend/pnpm-lock.yaml"),
]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    if not META_FILE.exists():
        print(f"FAIL: 缺少 {META_FILE.relative_to(ROOT)}")
        print("请执行 tools/prepare-offline.sh 生成快照元数据并提交")
        return 1

    meta = json.loads(META_FILE.read_text(encoding="utf-8"))
    problems = []
    for key, rel in PAIRS:
        path = ROOT / rel
        if not path.exists():
            problems.append(f"缺少锁文件 {rel}（快照记录了 {key}）")
            continue
        actual = sha256_file(path)
        if meta.get(key) != actual:
            problems.append(
                f"{rel} 的哈希与快照元数据不一致:\n"
                f"    元数据 {meta.get(key)}\n"
                f"    实际   {actual}"
            )

    if not problems:
        print(f"OK: .offline-snapshot.json 与锁文件一致（快照版本 "
              f"{hashlib.sha256(META_FILE.read_bytes()).hexdigest()[:12]}）")
        return 0

    print("FAIL: 改了依赖但离线快照没有重新生成/上传，main 构建将失败：")
    for p in problems:
        print(f"  - {p}")
    print("\n修复步骤（开发机）：")
    print("  1. ./tools/prepare-offline.sh")
    print("  2. GITLAB_UPLOAD_TOKEN=<deploy token> ./tools/upload-offline-snapshot.sh")
    print("  3. git add requirements.lock .offline-snapshot.json && git commit")
    return 1


if __name__ == "__main__":
    sys.exit(main())
