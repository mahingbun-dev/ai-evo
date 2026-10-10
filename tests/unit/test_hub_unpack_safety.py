"""
anc-hub 执行包安全解包单元测试（阶段A：验收 A2 / A6）

zip fixture 全部在测试内用 zipfile 现场合成，不依赖网络：
- unpack_zip_safely：.. 穿越 / 绝对路径 / 盘符拒绝；文件数、单文件、总解压量超限拒绝（中文错误）；
  仅解第一层，嵌套 zip 原样保留不解压
- hardlink_tree：同卷 os.link 生效（nlink 增加）；链接失败降级 shutil.copy2 且内容一致
- match_files：pathlib glob 语义、相对包根、.. 穿越拒绝
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.services import hub_file_service
from app.services.hub_file_service import (
    HubFileError,
    hardlink_tree,
    match_files,
    unpack_zip_safely,
)


# ---------------------------------------------------------------------------
# fixture 工具：现场合成小样本 zip
# ---------------------------------------------------------------------------

def make_zip_bytes(members: dict) -> bytes:
    """members: {成员名: 文本内容或 bytes}，合成 zip 字节流"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(name, data)
    return buf.getvalue()


def write_zip(path, members: dict) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(make_zip_bytes(members))
    return str(path)


def make_nested_zip_bytes(inner: dict) -> bytes:
    """合成嵌套 zip 内容（内层 zip 字节，作为外层成员原样写入）"""
    return make_zip_bytes(inner)


def list_files(root) -> list:
    root = Path(root)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# A2: 路径穿越与绝对路径拒绝
# ---------------------------------------------------------------------------

class TestUnpackRejectsUnsafeMembers:
    def test_reject_dotdot_traversal(self, tmp_path):
        """成员名含 .. 穿越：拒绝且目标目录不落任何文件"""
        zip_path = write_zip(tmp_path / "evil.zip", {
            "normal.txt": "ok",
            "../escape.txt": "evil",
        })
        dest = tmp_path / "dest"
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(dest))
        assert "穿越" in str(exc.value)
        assert not dest.exists() or list_files(dest) == []
        # 包根之外没有被写出的文件
        assert not (tmp_path / "escape.txt").exists()

    def test_reject_deep_dotdot_traversal(self, tmp_path):
        """深层 .. 穿越（dir/../../escape）同样拒绝"""
        zip_path = write_zip(tmp_path / "evil2.zip", {
            "a/b/../../../deep-escape.txt": "evil",
        })
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(tmp_path / "dest"))
        assert "穿越" in str(exc.value)
        assert not (tmp_path / "deep-escape.txt").exists()

    def test_reject_backslash_dotdot_traversal(self, tmp_path):
        """反斜杠形式的 .. 穿越同样拒绝（恶意包用 \\ 逃逸检查）"""
        zip_path = write_zip(tmp_path / "evil3.zip", {
            "..\\..\\win-escape.txt": "evil",
        })
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(tmp_path / "dest"))
        assert "穿越" in str(exc.value) or "绝对路径" in str(exc.value)
        assert not (tmp_path / "win-escape.txt").exists()

    def test_reject_absolute_posix_path(self, tmp_path):
        """绝对路径成员（/etc/evil）拒绝"""
        zip_path = write_zip(tmp_path / "abs.zip", {"/etc/abs-evil.txt": "evil"})
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(tmp_path / "dest"))
        assert "绝对路径" in str(exc.value)

    def test_reject_windows_drive_path(self, tmp_path):
        """Windows 盘符成员（C:/evil）拒绝"""
        zip_path = write_zip(tmp_path / "drive.zip", {"C:/win/evil.txt": "evil"})
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(tmp_path / "dest"))
        assert "绝对路径" in str(exc.value)


# ---------------------------------------------------------------------------
# A2: 超限拒绝（阈值逻辑用 patch 收敛上限构造超限，并断言生产阈值常量）
# ---------------------------------------------------------------------------

class TestUnpackRejectsLimits:
    def test_production_limits_configured(self):
        """生产阈值与验收要求一致：总 2GB / 文件数 20000 / 单文件 500MB"""
        assert hub_file_service.UNPACK_MAX_TOTAL == 2 * 1024 * 1024 * 1024
        assert hub_file_service.UNPACK_MAX_FILES == 20000
        assert hub_file_service.UNPACK_MAX_SINGLE == 500 * 1024 * 1024

    def test_reject_too_many_files(self, tmp_path, monkeypatch):
        """文件数超上限：拒绝（中文错误），一个文件都不落盘"""
        monkeypatch.setattr(hub_file_service, "UNPACK_MAX_FILES", 3)
        members = {f"f{i}.txt": "x" for i in range(4)}
        zip_path = write_zip(tmp_path / "many.zip", members)
        dest = tmp_path / "dest"
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(dest))
        assert "文件数超过上限" in str(exc.value)
        assert not dest.exists() or list_files(dest) == []

    def test_reject_single_file_too_large(self, tmp_path, monkeypatch):
        """单文件超上限：拒绝（错误带成员名）"""
        monkeypatch.setattr(hub_file_service, "UNPACK_MAX_SINGLE", 10)
        zip_path = write_zip(tmp_path / "big.zip", {"small.txt": "abc", "big.bin": "x" * 11})
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(tmp_path / "dest"))
        assert "big.bin" in str(exc.value)
        assert "单文件上限" in str(exc.value)

    def test_reject_total_too_large(self, tmp_path, monkeypatch):
        """总解压量超上限：拒绝（中文错误）"""
        monkeypatch.setattr(hub_file_service, "UNPACK_MAX_TOTAL", 10)
        zip_path = write_zip(tmp_path / "total.zip", {"a.txt": "x" * 6, "b.txt": "x" * 6})
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(zip_path, str(tmp_path / "dest"))
        assert "总解压量" in str(exc.value)
        assert "拒绝解压" in str(exc.value)

    def test_not_a_zip_rejects(self, tmp_path):
        """非 zip 文件：中文报错"""
        p = tmp_path / "fake.zip"
        p.write_bytes(b"not a zip at all")
        with pytest.raises(HubFileError) as exc:
            unpack_zip_safely(str(p), str(tmp_path / "dest"))
        assert "不是有效的 zip 文件" in str(exc.value)


# ---------------------------------------------------------------------------
# A2: 正常解包 / 仅解第一层
# ---------------------------------------------------------------------------

class TestUnpackHappyPath:
    def test_unpack_normal_members(self, tmp_path):
        """正常成员解出：内容正确、目录结构保留、计数正确"""
        zip_path = write_zip(tmp_path / "ok.zip", {
            "execution-manifest.json": json.dumps({"execution_id": "e1"}),
            "logs/run.log": "line1\nline2\n",
            "empty_dir_marker/keep.txt": "keep",
        })
        dest = tmp_path / "dest"
        stats = unpack_zip_safely(zip_path, str(dest))
        assert stats["file_count"] == 3
        assert stats["total_size"] == sum(
            (dest / p).stat().st_size for p in list_files(dest)
        )
        assert (dest / "logs" / "run.log").read_text(encoding="utf-8") == "line1\nline2\n"

    def test_nested_zip_kept_as_is(self, tmp_path):
        """仅解第一层：嵌套 zip 原样落盘不解压，内容字节一致"""
        inner = {"inner.txt": "inner-content", "sub/deep.json": "{}"}
        outer = {
            "top.txt": "top",
            "artifacts/nested.zip": make_nested_zip_bytes(inner),
        }
        zip_path = write_zip(tmp_path / "nested.zip", outer)
        dest = tmp_path / "dest"
        stats = unpack_zip_safely(zip_path, str(dest))

        assert stats["file_count"] == 2
        files = list_files(dest)
        assert files == ["artifacts/nested.zip", "top.txt"]
        # 嵌套 zip 未被解压：包内没有 inner.txt / sub/deep.json
        assert "artifacts/inner.txt" not in files
        assert "artifacts/sub/deep.json" not in files
        # 嵌套 zip 字节一致（原样保留）
        with open(dest / "artifacts" / "nested.zip", "rb") as f:
            assert f.read() == outer["artifacts/nested.zip"]
        # 且嵌套 zip 本身仍是合法 zip，成员与原内层一致（未被解压展开）
        with zipfile.ZipFile(dest / "artifacts" / "nested.zip") as zf:
            assert sorted(zf.namelist()) == ["inner.txt", "sub/deep.json"]


# ---------------------------------------------------------------------------
# A6: hardlink_tree 硬链接与降级复制
# ---------------------------------------------------------------------------

class TestHardlinkTree:
    def _make_src(self, tmp_path):
        src = tmp_path / "src"
        (src / "a").mkdir(parents=True)
        (src / "a" / "one.txt").write_text("content-one", encoding="utf-8")
        (src / "b").mkdir(parents=True)
        (src / "b" / "two.json").write_text('{"k": 2}', encoding="utf-8")
        (src / "root.bin").write_bytes(b"\x00\x01\x02")
        return src

    def test_hardlink_same_volume_nlink_increases(self, tmp_path):
        """同卷硬链接生效：目标文件 nlink 增加（=2）、内容一致、零复制"""
        src = self._make_src(tmp_path)
        dst = tmp_path / "dst"
        stats = hardlink_tree(str(src), str(dst))

        assert stats == {"linked": 3, "copied": 0}
        for rel in ["a/one.txt", "b/two.json", "root.bin"]:
            target = dst / rel
            assert target.is_file()
            # 硬链接：同一 inode，nlink 由 1 变 2
            assert target.stat().st_nlink == 2, f"{rel} 应有 2 个硬链接"
            assert (src / rel).stat().st_nlink == 2
            assert target.read_bytes() == (src / rel).read_bytes()
        # 硬链接共享 inode：改一侧另一侧可见（同文件）
        assert target.stat().st_ino == (src / "root.bin").stat().st_ino

    def test_hardlink_fallback_to_copy2(self, tmp_path, monkeypatch):
        """os.link 抛 OSError 时降级 shutil.copy2：内容一致、独立副本"""
        src = self._make_src(tmp_path)

        def broken_link(src_path, dst_path, **kwargs):
            raise OSError("cross-device link")

        monkeypatch.setattr(hub_file_service.os, "link", broken_link)
        dst = tmp_path / "dst-copy"
        stats = hardlink_tree(str(src), str(dst))

        assert stats == {"linked": 0, "copied": 3}
        for rel in ["a/one.txt", "b/two.json", "root.bin"]:
            target = dst / rel
            assert target.is_file()
            assert target.read_bytes() == (src / rel).read_bytes()
            assert target.stat().st_nlink == 1, "copy2 产物应为独立文件"
            assert target.stat().st_ino != (src / rel).stat().st_ino

    def test_empty_or_missing_src(self, tmp_path):
        """源目录不存在：返回零计数不抛错"""
        stats = hardlink_tree(str(tmp_path / "nope"), str(tmp_path / "dst"))
        assert stats == {"linked": 0, "copied": 0}


# ---------------------------------------------------------------------------
# 共享匹配器 match_files（阶段B执行 / 阶段D预览复用）
# ---------------------------------------------------------------------------

class TestMatchFiles:
    @pytest.fixture
    def unpack_root(self, tmp_path):
        root = tmp_path / "pkg"
        (root / "cases").mkdir(parents=True)
        (root / "logs").mkdir(parents=True)
        (root / "cases" / "a.jsonl").write_text("{}", encoding="utf-8")
        (root / "cases" / "b.json").write_text("{}", encoding="utf-8")
        (root / "logs" / "run.log").write_text("x", encoding="utf-8")
        (root / "README.md").write_text("doc", encoding="utf-8")
        return root

    def test_glob_semantics_relative_to_root(self, unpack_root):
        """pathlib glob 语义、相对包根、按路径排序、只含文件"""
        assert match_files(str(unpack_root), "**/*.jsonl") == ["cases/a.jsonl"]
        assert match_files(str(unpack_root), "cases/*") == ["cases/a.jsonl", "cases/b.json"]
        assert match_files(str(unpack_root), "*.md") == ["README.md"]
        assert match_files(str(unpack_root, ), "**/*") == [
            "README.md", "cases/a.jsonl", "cases/b.json", "logs/run.log",
        ]

    def test_leading_slash_tolerated(self, unpack_root):
        """前导 / 的模式按相对根处理（宽容），不逃出包根"""
        assert match_files(str(unpack_root), "/*.md") == ["README.md"]

    def test_dotdot_pattern_rejected(self, unpack_root):
        """模式含 .. 穿越：中文报错"""
        with pytest.raises(HubFileError) as exc:
            match_files(str(unpack_root), "../**")
        assert "穿越" in str(exc.value)

    def test_empty_pattern_rejected(self, unpack_root):
        with pytest.raises(HubFileError):
            match_files(str(unpack_root), "  ")

    def test_missing_root_returns_empty(self, tmp_path):
        assert match_files(str(tmp_path / "nope"), "**/*") == []
