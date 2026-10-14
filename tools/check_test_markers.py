#!/usr/bin/env python3
"""W8 分类完整性检查：所有收集到的测试必须带 unit / integration / e2e 之一。

整改计划 §3 W8「测试摸底」要求测试按依赖三分类，且 CI 门禁以
`pytest -m unit` 为强制项——若存在未打标测试，门禁会静默漏跑它们，
等于假绿。本脚本在 CI 的 test stage 里先于单元套件执行，堵这个洞。

判定口径与 pytest --collect-only 一致：进程内跑一次收集
（--continue-on-collection-errors，模块级 import 失败不影响其余文件
的判定），用 pytest_collection_finish 拿到全部 item 检查 marker。

退出码：
  0 —— 全部已分类；
  2 —— 存在未打标测试（打印清单，CI 直接红）。

注意：模块 import 失败（收集错误）在本脚本只作告警输出、不计失败——
它由 `pytest -m unit` 门禁本身兜底（收集错误会让门禁红）；本脚本只管
「分类完整性」这一件事。
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATION_MARKERS = {"unit", "integration", "e2e"}


class _Collector:
    """进程内收集插件：抓全部 item 与收集错误。"""

    def __init__(self) -> None:
        self.items: list = []
        self.errors: list[str] = []

    def pytest_collection_finish(self, session) -> None:  # noqa: D102
        self.items = list(session.items)

    def pytest_collectreport(self, report) -> None:  # noqa: D102
        if report.failed:
            self.errors.append(report.nodeid)


def main() -> int:
    collector = _Collector()
    rc = pytest.main(
        [
            "--collect-only",
            "-q",
            "--continue-on-collection-errors",
            "-p",
            "no:cacheprovider",
            "-W",
            "ignore",
        ],
        plugins=[collector],
    )
    # 收集已尽力完成（含模块 import 失败被 --continue-on-collection-errors 吞掉
    # 的情形，pytest 以 0/1/2 返回）；3/4/5 才是调用本身出错。
    if rc not in (0, 1, 2):
        print(f"[check_test_markers] pytest 收集调用异常退出 rc={rc}", file=sys.stderr)
        return rc if rc else 1

    for err in collector.errors:
        print(f"[check_test_markers][告警] 模块收集失败（import 错误，由 -m unit 门禁兜底）: {err}")

    unmarked = []
    counts = {m: 0 for m in sorted(CLASSIFICATION_MARKERS)}
    other = 0
    for item in collector.items:
        names = {m.name for m in item.iter_markers()}
        hit = names & CLASSIFICATION_MARKERS
        if hit:
            for m in hit:
                counts[m] += 1
        else:
            unmarked.append(item.nodeid)
            other += 1

    total = len(collector.items)
    print(
        f"[check_test_markers] 收集 {total} 个用例："
        f"unit={counts['unit']} integration={counts['integration']} e2e={counts['e2e']} 未分类={other}"
    )
    if unmarked:
        print(
            f"[check_test_markers][失败] {len(unmarked)} 个用例未打 unit/integration/e2e 任何 marker，"
            "CI 的 `pytest -m unit` 门禁会静默漏跑它们。请在测试文件加 "
            "`pytestmark = pytest.mark.<unit|integration|e2e>`（无法归类的在注释写明原因）：",
            file=sys.stderr,
        )
        for nodeid in unmarked[:50]:
            print(f"  - {nodeid}", file=sys.stderr)
        if len(unmarked) > 50:
            print(f"  ...（其余 {len(unmarked) - 50} 条略）", file=sys.stderr)
        return 2
    print("[check_test_markers] OK：全部用例已分类")
    return 0


if __name__ == "__main__":
    sys.exit(main())
