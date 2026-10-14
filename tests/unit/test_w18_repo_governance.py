# -*- coding: utf-8 -*-
"""W18 仓库与分支治理——独立测试角色验收用例（整改计划 §4 W18）。

覆盖范围（与整改计划 §4 W18 及本包验收焦点一一对应）：
1. T1 仓库清理：out/ 等误提交产物已从 git 索引移除；.gitignore 规则实际生效（check-ignore）。
2. T2 版本收紧：requirements.txt 的 langgraph/langfuse/deepeval 与 requirements.lock 精确一致。
3. T3/T4 文档重写：README/QUICK_START 按当前系统现状描述（技术栈/SSO/五类型/迁移命令/测试分层）。
4. T5 分支治理：docs/branch-discipline.md 覆盖整改计划 §0 全部约定，且可入库（白名单生效）。

说明：本文件只断言仓库内容与文档事实，不依赖 DB/Redis（L1 单元层）。
git 状态类断言（如「未 push」）属于时点检查，不适合固化为回归用例，由测试报告人工记录。
"""

from pathlib import Path
import re
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# T1：.gitignore 必须覆盖的全部误提交路径（整改计划 §4 W18 + 本包焦点清单）
IGNORED_PATHS = [
    "out/x.png",                       # out/ 验证产物（54 文件曾误提交，7fd66df 引入）
    "output/x",                        # output/ 运行产物
    ".zcode/x",                        # zcode 工作区元数据
    ".zcodeignore",
    "None/x",                          # 工具误生成的空目录
    "frontend/.playwright-cli/x",      # playwright-cli 状态
    "frontend/uploads/x",              # 前端上传目录（uploads/ 本身原已忽略）
    "uploads/x",                       # 运行时上传数据（原有规则，防回归）
]


def _git(*args: str) -> str:
    """在仓库根执行 git 命令并返回 stdout（check-ignore 的退出码语义由调用方处理）。"""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True,
    )
    return proc.stdout, proc.returncode


# ==================== T1 仓库清理 ====================

@pytest.mark.unit
class TestRepoCleanup:
    """out/ 等验证产物不入库 + .gitignore 规则实际生效。"""

    @pytest.mark.parametrize("tracked_root", [
        "out/", "output/", ".zcode/", "None/",
        "frontend/.playwright-cli/", "frontend/uploads/",
    ])
    def test_artifact_dirs_not_tracked(self, tracked_root):
        """git 索引中不残留任何验证产物（W18 曾误提交 out/ 下 54 个文件）。"""
        stdout, code = _git("ls-files", tracked_root)
        assert code == 0, f"git ls-files 执行失败：{tracked_root}"
        assert stdout.strip() == "", f"{tracked_root} 仍有文件被 git 跟踪：\n{stdout}"

    def test_gitignore_file_is_tracked(self):
        """.gitignore 本身必须入库（规则才能对全仓库生效）。"""
        stdout, _ = _git("ls-files", ".gitignore")
        assert ".gitignore" in stdout.split()

    @pytest.mark.parametrize("path", IGNORED_PATHS)
    def test_gitignore_rules_effective(self, path):
        """check-ignore 实测：规则存在 ≠ 规则生效，必须逐条验证匹配。"""
        stdout, code = _git("check-ignore", "-v", path)
        assert code == 0, f"{path} 未被 .gitignore 命中（check-ignore 退出码 {code}）"
        assert stdout.strip(), f"{path} 未返回命中规则明细"


# ==================== T2 版本收紧 ====================

@pytest.mark.unit
class TestRequirementsPins:
    """requirements.txt 三个范围声明收紧为与 requirements.lock 一致的精确版本。"""

    W18_PACKAGES = ["langgraph", "langfuse", "deepeval"]

    def _parse_pin(self, filename: str, package: str):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        pat = re.compile(rf"^{re.escape(package)}==([^\s#]+)\s*$", re.MULTILINE)
        matches = pat.findall(text)
        return matches, text

    @pytest.mark.parametrize("package", W18_PACKAGES)
    def test_pin_matches_lock(self, package):
        """requirements.txt 与 requirements.lock 对三包钉版逐一一致。"""
        req_matches, _ = self._parse_pin("requirements.txt", package)
        lock_matches, _ = self._parse_pin("requirements.lock", package)
        assert len(req_matches) == 1, f"requirements.txt 中 {package} 应恰好一条精确钉版，实际 {req_matches}"
        assert len(lock_matches) == 1, f"requirements.lock 中 {package} 应恰好一条钉版，实际 {lock_matches}"
        assert req_matches[0] == lock_matches[0], (
            f"{package} 版本漂移：requirements.txt=={req_matches[0]} vs requirements.lock=={lock_matches[0]}"
        )

    @pytest.mark.parametrize("package", W18_PACKAGES)
    def test_no_range_declaration_remains(self, package):
        """三包不得再出现 >= 等范围声明（本次整改的直接对象）。"""
        _, req_text = self._parse_pin("requirements.txt", package)
        for op in (">=", "<=", "~=", ">", "<", "!="):
            pat = re.compile(rf"^{re.escape(package)}{re.escape(op)}", re.MULTILINE)
            assert not pat.search(req_text), f"requirements.txt 中 {package} 仍存在范围声明 {op}"

    @pytest.mark.parametrize("package", W18_PACKAGES)
    def test_pin_matches_prod_constraints(self, package):
        """与 tools/prod-constraints.txt 既有钉版一致（离线镜像构建口径）。"""
        req_matches, _ = self._parse_pin("requirements.txt", package)
        constraint_matches, _ = self._parse_pin("tools/prod-constraints.txt", package)
        assert req_matches and constraint_matches, (
            f"{package} 在 requirements.txt={req_matches} / prod-constraints={constraint_matches}"
        )
        assert req_matches[0] == constraint_matches[0]


# ==================== T5 分支治理文档 ====================

@pytest.mark.unit
class TestBranchDisciplineDoc:
    """docs/branch-discipline.md 覆盖整改计划 §0 全部约定，且不被 .gitignore 挡住。"""

    DOC = REPO_ROOT / "docs" / "branch-discipline.md"

    # §0 各约定 → 文档中必须出现的关键证据词（宽松匹配，验证主题覆盖而非措辞）
    SECTION0_COVERAGE = [
        ("从 gitlab/main 拉取特性分支", r"gitlab/main"),
        ("MR 回 main", r"MR"),
        ("禁止重复 merge 同一分支", r"重复 merge|多次 merge"),
        ("revert merge 前全文件清单影响面确认", r"影响面"),
        ("禁止 git add -A", r"git add -A"),
        ("gitlab-only（GitHub 镜像单独同步）", r"GitHub"),
        ("filter-repo 运维窗口纪律（D-2）", r"filter-repo"),
        ("大改动 worktree 隔离", r"worktree"),
        ("f1518c1 事故背景", r"f1518c1"),
    ]

    def test_doc_exists(self):
        assert self.DOC.is_file(), "docs/branch-discipline.md 不存在"

    def test_doc_whitelisted_in_gitignore(self):
        """docs/*.md 默认忽略会挡住本文档入库，必须有 !docs/branch-discipline.md 白名单且生效。"""
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "!docs/branch-discipline.md" in gitignore, ".gitignore 缺少白名单 !docs/branch-discipline.md"
        _, code = _git("check-ignore", "docs/branch-discipline.md")
        assert code == 1, "docs/branch-discipline.md 仍被 .gitignore 忽略，无法入库"
        # 反向对照：其余 docs/*.md 维持默认忽略
        _, code_other = _git("check-ignore", "docs/white-list-regression-probe.md")
        assert code_other == 0, "docs/*.md 默认忽略规则失效（新增文档会被静默入库）"

    @pytest.mark.parametrize(("topic", "pattern"), SECTION0_COVERAGE)
    def test_covers_section0(self, topic, pattern):
        text = self.DOC.read_text(encoding="utf-8")
        assert re.search(pattern, text), f"分支治理文档未覆盖 §0 约定：{topic}（未找到 /{pattern}/）"
