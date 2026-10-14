"""match_files 对 '**' 结尾模式的跨 Python 版本语义回归（W8 独立测试角色复核）。

背景（提交 a1e57e8，W8 唯一实现代码改动 app/services/hub_file_service.py）：
pathlib.Path.glob 的裸 '**'（模式以 '**' 结尾）在 Python 3.12 不匹配文件、
3.13 起才包含文件——生产运行 3.12 时 fs 版 match_files 与索引版
match_files_in_index 的「同语义」承诺破裂（'**' 类模式漏配）。
修复：'**' 结尾的模式统一走 walk + _parts_match（fs/index/各 Python 版本
三方同语义），其余模式保持原生 glob。

本文件直接断言行为（不依赖与索引版对比的间接等价），作为 3.12 语义修复的
独立回归资产。缺陷现象在 3.12 容器实测复现（python:3.12-slim，amd64）：
  from pathlib import Path
  (root/'logs'/'run.log').parent.mkdir(parents=True); (root/'logs'/'run.log').write_text('x')
  list(Path(root).glob('**'))            # 3.12: 只含目录 logs/，不含文件 run.log
  list(Path(root).glob('logs/**'))       # 3.12: 空（3.14: 含 logs/run.log）
本地解释器是 3.14（glob('**') 已含文件），无法在旧实现上跑红本用例；
3.12 旧实现必红由上述容器现象与 a1e57e8 提交的 CI 等价容器记录支撑。

pytestmark=unit：纯 tmp_path 文件树 + 纯函数，无网络/无 DB。
"""
from pathlib import Path

import pytest

from app.services.hub_file_service import (
    _parts_match,
    build_file_index,
    match_files,
    match_files_in_index,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def tree(tmp_path):
    """含嵌套层级与隐藏目录的真实文件树。"""
    root = tmp_path / "tree"
    for rel in [
        "top.py",
        "logs/run.log",
        "a/b/c.log",
        ".hdir/deep.log",
    ]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return root


class TestDoubleStarSuffixSemantics:
    def test_bare_double_star_matches_all_files(self, tree):
        """'**' 必须命中全部文件（3.12 原生 glob 只给目录，修复后不得漏）。"""
        assert match_files(str(tree), "**") == [
            ".hdir/deep.log",
            "a/b/c.log",
            "logs/run.log",
            "top.py",
        ]

    def test_dir_then_double_star_matches_nested_files(self, tree):
        """'logs/**' / '.hdir/**' 必须命中目录下文件（3.12 原生 glob 恒空）。"""
        assert match_files(str(tree), "logs/**") == ["logs/run.log"]
        assert match_files(str(tree), ".hdir/**") == [".hdir/deep.log"]

    def test_deep_nesting_under_double_star(self, tree):
        """'a/**' 命中多层嵌套文件（walk+_parts_match 递归语义）。"""
        assert match_files(str(tree), "a/**") == ["a/b/c.log"]

    def test_dot_slash_prefix_double_star(self, tree):
        """'./**' 归一化后等价 '**'（parts 过滤 '.'，仍走部件匹配器）。"""
        assert match_files(str(tree), "./**") == match_files(str(tree), "**")

    def test_trailing_slash_after_double_star_empty(self, tree):
        """'**/' 恒空：尾随 '/' 只匹配目录，仅文件结果必为空（与索引版同口径）。"""
        assert match_files(str(tree), "**/") == []
        assert match_files(str(tree), "logs/**/") == []

    def test_middle_double_star_stays_native_glob(self, tree):
        """中段 '**'（不以 '**' 结尾）保持原生 glob 语义：仍跨层命中。"""
        assert match_files(str(tree), "**/deep.log") == [".hdir/deep.log"]
        assert match_files(str(tree), "**/*.log") == [
            ".hdir/deep.log",
            "a/b/c.log",
            "logs/run.log",
        ]


class TestDoubleStarMatchesIndexVersion:
    def test_suffix_patterns_equivalent_to_index(self, tree):
        """'**' 结尾核心模式集合：fs 版与索引版逐模式相等（三方同语义承诺）。"""
        index = build_file_index(str(tree))
        for pattern in ("**", "logs/**", ".hdir/**", "a/**", "./**", "**/"):
            assert match_files_in_index(index, pattern) == match_files(str(tree), pattern), (
                f"pattern={pattern!r} 索引版与 fs 版不一致"
            )


class TestDoubleStarWithAutoTopDirPrefix:
    @pytest.fixture
    def attempt_root(self, tmp_path):
        """解包根下仅含一个顶层目录 attempt-1/（触发单顶层目录自动拼接）。"""
        root = tmp_path / "unpack" / "fid"
        for rel in ["attempt-1/logs/run.log", "attempt-1/a/b/c.log", "attempt-1/top.py"]:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        return root

    def test_auto_prefix_retry_uses_parts_match(self, attempt_root):
        """自动拼接后的 retried 匹配同样走部件匹配器：'logs/**' 拼成
        'attempt-1/logs/**' 命中（3.12 下旧实现该分支 root.glob 恒空）。"""
        assert match_files(str(attempt_root), "logs/**") == ["attempt-1/logs/run.log"]

    def test_bare_double_star_no_auto_prefix_needed(self, attempt_root):
        """'**' 原始即命中全部（含 attempt-1 前缀），不触发自动拼接。"""
        assert match_files(str(attempt_root), "**") == [
            "attempt-1/a/b/c.log",
            "attempt-1/logs/run.log",
            "attempt-1/top.py",
        ]


class TestPartsMatchUnit:
    def test_double_star_matches_zero_or_more_components(self):
        """"**" 匹配零个或多个组件（部件匹配器单元断言）——'**' 可吞任意
        深度的路径，也可匹配零组件后由后续模式接上。"""
        assert _parts_match(("a", "b.py"), ("**", "b.py"))
        assert _parts_match(("x", "y", "b.py"), ("**", "b.py"))
        assert _parts_match(("a", "b.py"), ("**",))       # '**' 吞全部剩余组件
        assert _parts_match((), ("**",))                  # '**' 匹配零组件
        assert not _parts_match(("a", "b.py"), ("**", "c.py"))
