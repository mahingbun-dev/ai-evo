"""secure_filename 单元测试（整改开发计划 W1-1 测试设计）

计划用例清单（§2 W1「测试设计」）：
- `../../evil.py`、绝对路径 `/tmp/x.py`、反斜杠 `..\\evil.py`、空名、
  含控制字符 → 断言清洗结果
补充边界：`..` / `.` / 纯分隔符回退名、中文等正常字符保留、
嵌套目录成分剥离、幂等性。

独立测试角色编写；不修改实现代码。
"""
import pytest

from app.utils.secure_filename import secure_filename

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


class TestSecureFilenamePlanCases:
    """计划点名的 5 组用例"""

    def test_dotdot_traversal_stripped(self):
        """../../evil.py → 目录成分全部剥掉，仅保留文件名"""
        assert secure_filename("../../evil.py") == "evil.py"

    def test_absolute_path_stripped(self):
        """/tmp/x.py（posix 绝对路径）→ x.py"""
        assert secure_filename("/tmp/x.py") == "x.py"

    def test_backslash_traversal_stripped(self):
        """..\\evil.py（Windows 反斜杠穿越）→ evil.py"""
        assert secure_filename("..\\evil.py") == "evil.py"

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_name_falls_back(self, empty):
        """空名 / None → 回退 upload.bin（拼路径永不产生空段）"""
        assert secure_filename(empty) == "upload.bin"

    def test_control_chars_stripped(self):
        """控制字符（含 \\x00、换行）剥离，不进落盘文件名"""
        assert secure_filename("evil\x00.py") == "evil.py"
        assert secure_filename("re\nport\t.xlsx") == "report.xlsx"
        assert secure_filename("del\x7f.py") == "del.py"


class TestSecureFilenameBoundaries:
    """边界补充：清洗结果必须是「单个安全文件名」"""

    @pytest.mark.parametrize("name", [
        "..",          # 单独 .. ：不得逃出目录
        ".",
        "../",
        "..\\",
        "../../",
        "/",           # 纯分隔符
        "\\\\",
        "/..",
        "C:/",
        "D:\\",
        "\x00\x01\x02",  # 纯控制字符
    ])
    def test_path_only_names_fall_back(self, name):
        """仅含目录语义/控制字符的名字 → 回退 upload.bin"""
        assert secure_filename(name) == "upload.bin"

    def test_windows_drive_letter(self):
        """盘符绝对路径 C:\\windows\\evil.py → evil.py"""
        assert secure_filename("C:\\windows\\evil.py") == "evil.py"
        assert secure_filename("C:/windows/evil.py") == "evil.py"

    def test_nested_dirs_only_basename_kept(self):
        """多层目录成分只保留 basename（子目录语义不得进文件名）"""
        assert secure_filename("a/b/c/report.pdf") == "report.pdf"
        assert secure_filename("a\\b\\c\\report.pdf") == "report.pdf"
        assert secure_filename("../../../etc/passwd") == "passwd"

    def test_normal_name_untouched(self):
        """正常名字（含中文、空格、多点）原样保留——清洗不得误伤"""
        assert secure_filename("评测报告.xlsx") == "评测报告.xlsx"
        assert secure_filename("v1.2.3-final.zip") == "v1.2.3-final.zip"
        assert secure_filename("case 01.json") == "case 01.json"

    def test_no_path_separator_in_result(self):
        """性质断言：任意输入的清洗结果不含 / \\ .. 与控制字符"""
        samples = [
            "../../evil.py", "/tmp/x.py", "..\\evil.py", "a/../..\\b.py",
            "x\x00/y\\z..py", "C:\\x\\..\\y.py", "..", "", None,
            "正常文件.bin", "../子目录/文件.xlsx",
        ]
        for raw in samples:
            cleaned = secure_filename(raw)
            assert cleaned, f"清洗结果不得为空: {raw!r}"
            assert "/" not in cleaned, f"清洗结果不得含 /: {raw!r} -> {cleaned!r}"
            assert "\\" not in cleaned, f"清洗结果不得含 \\: {raw!r} -> {cleaned!r}"
            assert cleaned not in (".", ".."), f"清洗结果不得是目录语义: {raw!r}"
            assert not any(ord(c) < 32 or ord(c) == 127 for c in cleaned), \
                f"清洗结果不得含控制字符: {raw!r} -> {cleaned!r}"

    def test_idempotent(self):
        """幂等：再清洗一次结果不变（落盘后二次拼接安全）"""
        for raw in ["../../evil.py", "正常.bin", "..", "..\\a/b.py"]:
            once = secure_filename(raw)
            assert secure_filename(once) == once

    def test_result_safe_under_join(self, tmp_path):
        """落盘安全性（攻击面复现核心断言）：清洗后文件名 join 目标目录，
        解析结果必须仍在目标目录内"""
        target = tmp_path / "uploads"
        target.mkdir()
        for raw in ["../../evil.py", "/tmp/x.py", "..\\evil.py",
                    "../../../../tmp/evil.py"]:
            safe = secure_filename(raw)
            resolved = (target / safe).resolve()
            assert str(resolved).startswith(str(target.resolve()) + "/") or \
                resolved.parent == target.resolve(), \
                f"清洗结果 {safe!r} join 后越出目标目录: {resolved}"
