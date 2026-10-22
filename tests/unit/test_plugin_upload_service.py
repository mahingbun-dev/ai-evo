"""
插件评测上传服务单元测试（zip 落盘 / SKILL.md 内联 / 校验拒绝）
"""
import io
import zipfile

import pytest

from app.services import plugin_upload_service as svc
from app.services.plugin_upload_service import save_plugin_upload

pytestmark = pytest.mark.unit


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    root = tmp_path / "plugin_eval"
    monkeypatch.setattr(svc, "_PLUGIN_UPLOAD_DIR", str(root))
    return root


def _zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


GOOD_SKILL_MD = """---
name: demo-checker
description: Use when the user asks to check project demo files. Use proactively for demo hygiene.
---

# Demo Checker
"""


class TestZipUpload:
    """zip 插件包上传"""

    def test_skill_zip_unpacked(self, upload_root):
        data = _zip_bytes({
            "demo-checker/SKILL.md": GOOD_SKILL_MD,
            "demo-checker/references/details.md": "# details",
        })
        result = save_plugin_upload("demo-checker.zip", data)
        assert result["kind"] == "plugin_dir"
        assert result["target_type"] == "skill"
        assert result["file_count"] == 2
        # 唯一顶层目录已剥离：解包根级直接是 SKILL.md
        import os
        assert os.path.exists(os.path.join(result["path"], "SKILL.md"))
        assert os.path.exists(os.path.join(result["path"], "references", "details.md"))

    def test_plugin_zip_unpacked(self, upload_root):
        data = _zip_bytes({
            "my-plugin/.claude-plugin/plugin.json": '{"name": "my-plugin"}',
            "my-plugin/skills/a/SKILL.md": GOOD_SKILL_MD,
            "my-plugin/agents/b.md": "# agent",
        })
        result = save_plugin_upload("my-plugin.zip", data)
        assert result["kind"] == "plugin_dir"
        assert result["target_type"] == "plugin"
        import os
        assert os.path.exists(os.path.join(result["path"], ".claude-plugin", "plugin.json"))

    def test_wrapped_outer_folder_drills_down(self, upload_root):
        """外层再包一层文件夹的 zip：唯一一级子目录是对象根时自动下钻"""
        data = _zip_bytes({
            "outer/demo-checker/SKILL.md": GOOD_SKILL_MD,
        })
        result = save_plugin_upload("wrapped.zip", data)
        assert result["kind"] == "plugin_dir"
        import os
        assert os.path.exists(os.path.join(result["path"], "SKILL.md"))

    def test_root_object_missing_rejected(self, upload_root):
        data = _zip_bytes({
            "some/dir/SKILL.md": GOOD_SKILL_MD,
            "other/file.txt": "x",
        })
        result = save_plugin_upload("broken.zip", data)
        assert "error" in result
        assert "SKILL.md" in result["error"]

    def test_zip_slip_rejected(self, upload_root):
        data = _zip_bytes({"../evil.txt": "boom"})
        result = save_plugin_upload("evil.zip", data)
        assert "error" in result
        assert "非法路径" in result["error"]

    def test_bad_zip_rejected(self, upload_root):
        result = save_plugin_upload("bad.zip", b"not a zip")
        assert "error" in result

    def test_empty_zip_rejected(self, upload_root):
        data = _zip_bytes({})
        result = save_plugin_upload("empty.zip", data)
        assert "error" in result


class TestMdUpload:
    """SKILL.md 单文件上传"""

    def test_md_content_returned(self, upload_root):
        result = save_plugin_upload("SKILL.md", GOOD_SKILL_MD.encode("utf-8"))
        assert result["kind"] == "skill_md"
        assert result["name"] == "SKILL"
        assert result["content"].startswith("---")

    def test_md_too_large_rejected(self, upload_root):
        big = b"x" * (svc.MAX_SKILL_MD_SIZE + 1)
        result = save_plugin_upload("SKILL.md", big)
        assert "error" in result

    def test_empty_md_rejected(self, upload_root):
        result = save_plugin_upload("SKILL.md", b"   \n  ")
        assert "error" in result


class TestCommon:
    """通用校验"""

    def test_unknown_extension_rejected(self, upload_root):
        result = save_plugin_upload("plugin.exe", b"data")
        assert "error" in result
        assert "仅支持" in result["error"]

    def test_no_source_fails(self):
        result = save_plugin_upload("", b"")
        assert "error" in result
