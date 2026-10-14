"""W8 CI 测试门禁基建的回归资产（整改计划 §3 W8「测试摸底」+ 独立测试角色验证）。

验证对象：tools/check_test_markers.py 的分类完整性语义——
「pytest -m unit 门禁只选择带 marker 的用例，未打标用例会被门禁静默漏跑」
这一假绿洞必须被该检查脚本堵住（未打标 → 退出码 2 → CI 红）。

三个层面：
1. 行为级：临时目录构造未打标/已打标样本，子进程跑脚本断言退出码与输出；
2. 全量级：本仓库 pytest 视角下 `-m "not unit and not integration and not e2e"`
   选择集恒为空（三分类闭合，与 check_test_markers 的进程内收集互为备份）；
3. 门禁隔离：e2e 集合与 unit 集合不相交（e2e 真模型/真 SSO，CI 永不跑）。

pytestmark 说明：本文件自身是纯单元语义（临时目录 + 进程内收集），标 unit。
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "tools" / "check_test_markers.py"

pytestmark = pytest.mark.unit

PYTESTINI = "[pytest]\ntestpaths = .\n"

MARKED_TEST = '''\
import pytest
pytestmark = pytest.mark.unit

def test_marked_ok():
    assert True
'''

UNMARKED_TEST = '''\
def test_unmarked_missing_marker():
    assert True
'''


def _run_check(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess:
    """在临时目录跑 check_test_markers.py（脚本以 cwd 为收集根）。"""
    (tmp_path / "pytest.ini").write_text(PYTESTINI, encoding="utf-8")
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCheckTestMarkersBehavior:
    def test_unmarked_file_makes_gate_red(self, tmp_path):
        """存在未打标用例 → 退出码 2，stderr 指名该用例（假绿洞被堵）。"""
        proc = _run_check(tmp_path, {"test_unmarked_demo.py": UNMARKED_TEST})
        assert proc.returncode == 2, (
            f"未打标用例必须使分类检查失败（堵住 -m unit 门禁静默漏跑）。\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
        assert "test_unmarked_demo.py::test_unmarked_missing_marker" in proc.stderr
        # 统计输出必须显示该用例落入"未分类"
        assert "未分类=1" in proc.stdout

    def test_marked_file_passes_check(self, tmp_path):
        """对照：全部用例已打标 → 退出码 0 且输出 OK。"""
        proc = _run_check(tmp_path, {"test_marked_demo.py": MARKED_TEST})
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        assert "unit=1" in proc.stdout
        assert "未分类=0" in proc.stdout
        assert "OK" in proc.stdout

    def test_mixed_files_report_only_unmarked(self, tmp_path):
        """打标与未打标混存：只未打标的被点名，不误伤已打标用例。"""
        proc = _run_check(
            tmp_path,
            {
                "test_marked_demo.py": MARKED_TEST,
                "test_unmarked_demo.py": UNMARKED_TEST,
            },
        )
        assert proc.returncode == 2
        assert "test_unmarked_demo.py::test_unmarked_missing_marker" in proc.stderr
        assert "test_marked_demo.py" not in proc.stderr


class TestRepoClassificationClosure:
    """本仓库三分类闭合性（pytest 选择器视角，与进程内收集互为备份）。"""

    @staticmethod
    def _collect(expr: str) -> list[str]:
        import pytest as _pytest

        items: list = []

        class _Grab:
            def pytest_collection_finish(self, session):
                items.extend(session.items)

        rc = _pytest.main(
            [
                "--collect-only",
                "-q",
                "--continue-on-collection-errors",
                "-p",
                "no:cacheprovider",
                "-W",
                "ignore",
                "-m",
                expr,
                "tests",
            ],
            plugins=[_Grab()],
        )
        # rc=5 (NO_TESTS_COLLECTED) 是合法结果：-m 表达式选择集为空且无收集
        # 错误时返回（如 CI 3.12 环境 protobuf 可导入、无收集错误）。本地 3.14
        # 因三个 protobuf 文件收集错误恒为 rc=2；两者都表示「收集完成」。
        assert rc in (0, 1, 2, 5), f"pytest 收集调用异常 rc={rc}"
        return [item.nodeid for item in items]

    def test_no_test_outside_three_classification(self):
        """`-m "not unit and not integration and not e2e"` 恒空——任何新测试文件
        忘记打标时，本用例在 -m unit 门禁内变红（第二道防线）。"""
        selected = self._collect("not unit and not integration and not e2e")
        assert selected == [], (
            "以下用例未打 unit/integration/e2e 任何 marker（门禁会静默漏跑）:\n"
            + "\n".join(selected[:20])
        )

    def test_e2e_never_selected_by_unit_gate(self):
        """e2e 与 unit 集合不相交：真模型/真 SSO 用例永不进 CI 门禁。"""
        unit_ids = set(self._collect("unit"))
        e2e_ids = set(self._collect("e2e"))
        assert e2e_ids, "e2e 集合不应为空（test_deepeval_e2e.py 4 用例）"
        overlap = sorted(unit_ids & e2e_ids)
        assert not overlap, f"以下用例同时打了 unit 与 e2e:\n{overlap}"
