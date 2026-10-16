"""容器模式挂载树执行期放宽（relax_tree_for_sandbox_uid）单测

CI 根因回归：沙箱 uid 10001 非属主，secure_mkdir 0o750 创建口径下沙箱
容器无法遍历/读取工作区（docker run 退出码 125 / PermissionError）。
relax_tree_for_sandbox_uid 只在 driver 受理前追加 others r-x（目录）/r（文件），
绝不追加 w（entry wrapper 只读消费，stdout 出 JSON 不落盘）。
"""
import os
import stat
from pathlib import Path

import pytest

from app.services.script_container import relax_tree_for_sandbox_uid

pytestmark = pytest.mark.unit


def _mode(path) -> int:
    """路径权限位（st_mode 归一到 rwx 三元组）"""
    return stat.S_IMODE(os.stat(path).st_mode)


class TestRelaxTreeForSandboxUid:
    def test_adds_others_rx_without_write(self, tmp_path):
        """0o700/0o600/0o755 树：目录加 0o005、文件加 0o004，写位不增加，x 位保留"""
        run = tmp_path / "run"
        run.mkdir()
        os.chmod(run, 0o700)
        nested = run / "scripts" / "smoke"
        nested.mkdir(parents=True)
        os.chmod(nested, 0o700)
        plain = nested / "main.py"
        plain.write_text("print(1)\n", encoding="utf-8")
        os.chmod(plain, 0o600)
        exec_file = run / "tool.py"
        exec_file.write_text("pass\n", encoding="utf-8")
        os.chmod(exec_file, 0o755)
        d750 = run / "secure"
        d750.mkdir()
        os.chmod(d750, 0o750)  # secure_mkdir 的 D6 口径

        relax_tree_for_sandbox_uid(run)

        # 目录：others r-x 到位（含既有 0o750 口径的 D6 目录）
        assert _mode(run) & 0o005 == 0o005
        assert _mode(nested) & 0o005 == 0o005
        assert _mode(d750) & 0o005 == 0o005
        # 文件：others r 到位
        assert _mode(plain) & 0o004 == 0o004
        # 写位（含 others w）一个都没加
        for path in (run, nested, d750, plain, exec_file):
            assert _mode(path) & 0o022 == 0
        # 可执行文件的 x 位保留（u/g/x 三元都不动，只 OR 不减）
        assert _mode(exec_file) & 0o111 == 0o111
        assert _mode(plain) & 0o111 == 0  # 0o600 文件不会被加 x

    def test_missing_or_empty_roots_skipped(self, tmp_path):
        """不存在 / None / 空路径 / 无参：不抛错，同调用里的有效树仍处理"""
        target = tmp_path / "run"
        target.mkdir()
        os.chmod(target, 0o700)

        relax_tree_for_sandbox_uid(
            tmp_path / "nope",
            None,  # type: ignore[arg-type]
            Path(""),
            target,  # 同调用夹一颗有效树
        )
        relax_tree_for_sandbox_uid()

        assert _mode(target) & 0o005 == 0o005  # 有效树仍被放宽

    def test_multiple_roots(self, tmp_path):
        """多棵树一次调用全放宽"""
        roots = []
        for i in range(2):
            sub = tmp_path / f"run{i}"
            sub.mkdir()
            os.chmod(sub, 0o700)
            (sub / "x.py").write_text("1\n", encoding="utf-8")
            os.chmod(sub / "x.py", 0o600)
            roots.append(sub)

        relax_tree_for_sandbox_uid(*roots)

        for sub in roots:
            assert _mode(sub) & 0o005 == 0o005
            assert _mode(sub / "x.py") & 0o004 == 0o004

    def test_relax_covers_mount_root_above_subtrees(self, tmp_path):
        """debug 链布局：挂载根（公共祖先父目录）下两棵子树，relax(父) 连父一起放宽

        build_container_paths 的挂载源是 run_dir/cwd 的 commonpath——debug 链
        cwd 在 run_dir 之外时挂载根比 run_dir 高（如 uploads/script_runs/debug）。
        以父目录为根调用后：父目录本身 0o005 位到位、深层文件 0o004 位到位、
        无 w 位增加（CI EACCES 回归：uid 10001 在 /workspace 顶层即被 0o700
        父目录挡住）。
        """
        parent = tmp_path / "mount-root"
        parent.mkdir()
        os.chmod(parent, 0o700)
        trees = []
        for name in ("run_dir", "debug_cwd"):
            sub = parent / name
            sub.mkdir()
            os.chmod(sub, 0o750)
            mid = sub / "scripts"
            mid.mkdir()
            os.chmod(mid, 0o750)
            deep = mid / "pkg"  # 深两层子目录
            deep.mkdir()
            os.chmod(deep, 0o750)
            deep_file = deep / "main.py"
            deep_file.write_text("print(1)\n", encoding="utf-8")
            os.chmod(deep_file, 0o600)
            trees.append((sub, mid, deep, deep_file))

        relax_tree_for_sandbox_uid(parent)

        # 挂载根本身（0o700 父目录）others r-x 到位——CI 实测漏掉它即 EACCES
        assert _mode(parent) & 0o005 == 0o005
        for sub, mid, deep, deep_file in trees:
            assert _mode(sub) & 0o005 == 0o005
            assert _mode(mid) & 0o005 == 0o005
            assert _mode(deep) & 0o005 == 0o005
            assert _mode(deep_file) & 0o004 == 0o004  # 深层文件 others r 到位
        # 无 w 位增加（父目录 + 两棵树全部条目）
        for path in [parent] + [p for quad in trees for p in quad]:
            assert _mode(path) & 0o022 == 0
