"""
docs_api 单元测试：read_only 容器下用户文档读/写的落点行为

背景（评审阻断项）：api/worker read_only 后 /app/docs 属镜像只读层，
PUT 原地写 user_guide.md 必 500。修复口径：管理员更新写 uploads 卷下的
覆盖副本，GET 覆盖副本优先、无覆盖回退镜像内置默认文档。
"""

import pytest

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


@pytest.fixture
def guide_paths(tmp_path, monkeypatch):
    """把 docs_api 的三个落点重定向到 tmp_path（docs 只读层也用临时目录模拟）"""
    from app.api import docs_api

    bundled = tmp_path / "bundled" / "user_guide.md"
    bundled.parent.mkdir()
    bundled.write_text("# 内置默认文档", encoding="utf-8")

    uploads_root = tmp_path / "uploads"
    monkeypatch.setattr(docs_api, "USER_GUIDE_PATH", bundled)
    monkeypatch.setattr(docs_api, "UPLOADS_ROOT", uploads_root)
    monkeypatch.setattr(docs_api, "USER_GUIDE_OVERRIDE_PATH", uploads_root / "user_guide.md")
    return docs_api, bundled, uploads_root


class TestUserGuideStorage:
    def test_put_writes_uploads_override_not_bundled(self, guide_paths):
        """PUT 必须写 uploads 卷覆盖副本，不得触碰镜像内置 docs 路径"""
        docs_api, bundled, uploads_root = guide_paths
        resp = docs_api.update_user_guide(docs_api.UpdateDocRequest(content="# 管理员版"))
        assert resp == {"message": "文档更新成功"}
        assert (uploads_root / "user_guide.md").read_text(encoding="utf-8") == "# 管理员版"
        # 内置默认文档保持原样（read_only 层本来也写不进）
        assert bundled.read_text(encoding="utf-8") == "# 内置默认文档"

    def test_get_falls_back_to_bundled_when_no_override(self, guide_paths):
        """无覆盖副本时 GET 回退内置默认文档"""
        docs_api, bundled, _ = guide_paths
        assert docs_api.get_user_guide() == {"content": "# 内置默认文档"}

    def test_get_prefers_override_after_put(self, guide_paths):
        """管理员更新后 GET 返回覆盖副本内容"""
        docs_api, _, _ = guide_paths
        docs_api.update_user_guide(docs_api.UpdateDocRequest(content="# 管理员版"))
        assert docs_api.get_user_guide() == {"content": "# 管理员版"}

    def test_get_404_when_bundled_missing(self, guide_paths):
        """覆盖副本与内置默认都不存在时 GET 404"""
        docs_api, bundled, _ = guide_paths
        bundled.unlink()
        with pytest.raises(Exception) as ei:
            docs_api.get_user_guide()
        assert ei.value.status_code == 404

    def test_put_500_when_uploads_unwritable(self, guide_paths, tmp_path, monkeypatch):
        """写面不可写（模拟只读挂载）时 PUT 报 500 而非裸异常"""
        import os
        docs_api, _, _ = guide_paths
        ro_root = tmp_path / "ro_uploads"
        ro_root.mkdir()
        os.chmod(ro_root, 0o500)
        monkeypatch.setattr(docs_api, "UPLOADS_ROOT", ro_root)
        monkeypatch.setattr(docs_api, "USER_GUIDE_OVERRIDE_PATH", ro_root / "user_guide.md")
        try:
            with pytest.raises(Exception) as ei:
                docs_api.update_user_guide(docs_api.UpdateDocRequest(content="x"))
            assert ei.value.status_code == 500
        finally:
            os.chmod(ro_root, 0o700)
