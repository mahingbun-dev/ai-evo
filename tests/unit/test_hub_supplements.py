"""
anc-hub 执行包人工补传单元测试

覆盖（对应开发计划第六节用例清单）：
- add_supplements：散文件落盘 / zip 展开保留结构 / target_dir 仅对散文件生效 /
  同名覆盖（压过原包与旧补传）/ 单文件与条目/总量上限 / 路径穿越拒绝 /
  空 zip 拒绝 / 未导入中文引导 / 同批同名后者覆盖
- merged_index：叠加视图与 is_manual 标记（file_index 保持纯原始）
- delete_supplement：正常删除 / 拒删原始执行包文件 / 删不存在的补传
- resolve_hub_file_fields：镜像含补传文件、同名以补传版为准、复用分支增量合并
- release_ref / discard_unpacked_if_unreferenced：归零联动清理补充区、未归零不清
- hub_match_preview：补传文件参与匹配（预览与执行一致）
- API 层：tree 叠加、content 回退补充区、上传/删除端点直调

DB 用 MagicMock + 真实 HubFileRecord（不连真实库）；目录全部指到 tmp_path，
_supplements_root monkeypatch 隔离，不触碰真实 uploads/。
"""
import json
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.models import HubFileRecord
from app.services import hub_file_service
from app.services.hub_file_service import (
    HubFileError,
    add_supplements,
    build_file_index,
    delete_supplement,
    discard_unpacked_if_unreferenced,
    merged_index,
    release_ref,
    resolve_hub_file_fields,
    hub_match_preview,
)
from app.api.hub_file_api import (
    delete_supplement_endpoint,
    get_hub_file_content,
    get_hub_file_tree,
    upload_supplements,
)

FID = "fid-supp-1"


# ---------------------------------------------------------------------------
# mock 工具
# ---------------------------------------------------------------------------

def make_db(first=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = first
    return db


def make_record(unpack_dir, ref_count=0):
    """真实 HubFileRecord：file_index 按解包目录现场构建，supplement_index 初始 None"""
    return HubFileRecord(
        id="hub_supp_1", file_id=FID, unpack_status="unpacked",
        unpack_dir=str(unpack_dir), ref_count=ref_count,
        file_index=build_file_index(str(unpack_dir)),
    )


@pytest.fixture
def supp_root(tmp_path, monkeypatch):
    """补传区根目录指到临时目录（隔离真实 uploads/hub_supplements）"""
    root = tmp_path / "hub_supplements"
    root.mkdir()
    monkeypatch.setattr(hub_file_service, "_hub_supplements_root", lambda supplements_root=None: str(root))
    return root


@pytest.fixture
def unpack_dir(tmp_path):
    """原始解包区：两个文件（其中 original.txt 会被补传覆盖）"""
    root = tmp_path / "hub_datasets" / FID
    root.mkdir(parents=True)
    (root / "original.txt").write_text("原始内容", encoding="utf-8")
    (root / "meta.json").write_text('{"a": 1}', encoding="utf-8")
    return root


def make_zip_bytes(members):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content if isinstance(content, bytes) else content.encode("utf-8"))
    return buf.getvalue()


def upload(name, content=b"x"):
    return (name, BytesIO(content))


# ---------------------------------------------------------------------------
# add_supplements 服务层
# ---------------------------------------------------------------------------

class TestAddSupplements:
    def test_loose_file_lands_and_index_updated(self, unpack_dir, supp_root):
        """散文件落盘补充区 + supplement_index 更新 + file_index 不动"""
        rec = make_record(unpack_dir)
        original_index = list(rec.file_index)
        db = make_db(first=rec)

        result = add_supplements(db, FID, [upload("note.txt", "补充说明".encode("utf-8"))],
                                 uploaded_by="qa-01")

        assert result["added_count"] == 1
        assert result["added"] == [{"path": "note.txt", "size": 12}]
        assert result["overwritten"] == []
        target = supp_root / FID / "note.txt"
        assert target.is_file() and target.read_bytes() == "补充说明".encode("utf-8")
        assert rec.file_index == original_index, "file_index 必须保持纯原始"
        assert [e["path"] for e in rec.supplement_index] == ["note.txt"]
        entry = rec.supplement_index[0]
        assert entry["uploaded_by"] == "qa-01"
        assert entry["type"] == "text"
        assert entry["uploaded_at"], "审计字段 uploaded_at 必须记录"

    def test_zip_expands_structure_and_target_dir_only_for_loose(self, unpack_dir, supp_root):
        """zip 展开保留目录结构；target_dir 仅对散文件生效，不影响 zip 内部结构"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        zip_bytes = make_zip_bytes({"inner/a.txt": "A", "inner/sub/b.json": '{"k":1}'})

        result = add_supplements(
            db, FID,
            [upload("补传.zip", zip_bytes), upload("loose.txt", b"L")],
            relative_paths=["补传.zip", "loose.txt"],
            target_dir="inputs",
        )

        assert result["added_count"] == 3
        paths = {e["path"] for e in result["added"]}
        assert paths == {"inner/a.txt", "inner/sub/b.json", "inputs/loose.txt"}
        assert (supp_root / FID / "inner" / "sub" / "b.json").is_file()

    def test_overwrite_reports_and_supplement_wins(self, unpack_dir, supp_root):
        """同名覆盖：压过原始包文件与旧补传文件，返回 overwritten 明细，实体为新版"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("old.txt", "旧补传".encode("utf-8"))])

        result = add_supplements(db, FID, [
            upload("original.txt", "新版原始文件".encode("utf-8")),
            upload("old.txt", "新版补传".encode("utf-8")),
        ])

        assert set(result["overwritten"]) == {"original.txt", "old.txt"}
        # 补传区实体为新版（覆盖语义）
        assert (supp_root / FID / "original.txt").read_bytes() == "新版原始文件".encode("utf-8")
        assert (supp_root / FID / "old.txt").read_bytes() == "新版补传".encode("utf-8")
        # 索引不重复：同名只保留一条
        supp_paths = [e["path"] for e in rec.supplement_index]
        assert sorted(supp_paths) == ["old.txt", "original.txt"]

    def test_same_batch_duplicate_last_wins(self, unpack_dir, supp_root):
        """同批同名后者覆盖前者（与 zip 解压语义一致）"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)

        result = add_supplements(db, FID, [
            upload("dup.txt", "第一个".encode("utf-8")), upload("dup.txt", "第二个".encode("utf-8")),
        ])

        assert result["added_count"] == 1
        assert (supp_root / FID / "dup.txt").read_bytes() == "第二个".encode("utf-8")

    def test_zip_slip_rejected(self, unpack_dir, supp_root):
        """zip 内 ../evil 条目：安全解压拒绝，中文报错，补充区零残留"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        evil = make_zip_bytes({"../evil.txt": "逃逸"})

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("evil.zip", evil)])
        assert "不安全" in str(exc.value)
        assert rec.supplement_index is None, "失败时索引不得变更"
        assert not any(supp_root.glob(f"{FID}/**")), "失败时补充区不得有残留"

    def test_empty_zip_rejected(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("empty.zip", make_zip_bytes({}))])
        assert "没有任何文件" in str(exc.value)

    def test_single_file_size_limit(self, unpack_dir, supp_root, monkeypatch):
        """散文件超过单文件上限：流式计数中断拒绝"""
        monkeypatch.setattr(hub_file_service, "MAX_SUPPLEMENT_FILE_SIZE", 10)
        rec = make_record(unpack_dir)
        db = make_db(first=rec)

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("big.txt", b"x" * 30)])
        assert "单文件上限" in str(exc.value)

    def test_entries_limit_cumulative(self, unpack_dir, supp_root, monkeypatch):
        """条目上限按补传区累计口径（已有 + 本次）"""
        monkeypatch.setattr(hub_file_service, "MAX_UNPACK_ENTRIES", 3)
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("one.txt", b"1"), upload("two.txt", b"2")])

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("three.txt", b"3"), upload("four.txt", b"4")])
        assert "文件数超过上限" in str(exc.value)

    def test_total_size_limit_cumulative(self, unpack_dir, supp_root, monkeypatch):
        """总量上限按补传区累计口径（已有 + 本次）"""
        monkeypatch.setattr(hub_file_service, "MAX_UNPACK_TOTAL_SIZE", 100)
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("base.bin", b"a" * 60)])

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("more.bin", b"b" * 60)])
        assert "总量超过上限" in str(exc.value)

    def test_target_dir_traversal_rejected(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("a.txt")], target_dir="../evil")
        assert "禁止 .. 穿越" in str(exc.value)

    def test_relative_path_traversal_rejected(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)

        with pytest.raises(HubFileError) as exc:
            add_supplements(db, FID, [upload("a.txt")], relative_paths=["../../evil.txt"])
        assert "禁止 .. 穿越" in str(exc.value)

    def test_unregistered_package_rejected(self, supp_root):
        """未导入的包：中文引导先导入（不开放补传）"""
        db = make_db(first=None)
        with pytest.raises(HubFileError) as exc:
            add_supplements(db, "fid-nope", [upload("a.txt")])
        msg = str(exc.value)
        assert "无持久解包区" in msg and "导入" in msg

    def test_not_unpacked_rejected(self, unpack_dir, supp_root):
        """登记存在但未解包完成：拒绝补传"""
        rec = make_record(unpack_dir)
        rec.unpack_status = "pending"
        with pytest.raises(HubFileError) as exc:
            add_supplements(make_db(first=rec), FID, [upload("a.txt")])
        assert "无持久解包区" in str(exc.value)

    def test_relative_paths_length_mismatch(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        with pytest.raises(HubFileError) as exc:
            add_supplements(make_db(first=rec), FID, [upload("a.txt"), upload("b.txt")],
                            relative_paths=["a.txt"])
        assert "数量不一致" in str(exc.value)

    def test_nested_zip_supplement_gets_nested_index(self, unpack_dir, supp_root):
        """补传 zip 内含嵌套 zip：解出的内层 zip 条目生成 nested_index（抽屉内层清单可见）"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        inner = make_zip_bytes({"报告.txt": "内容"})
        outer = make_zip_bytes({"bundle.zip": inner})  # 外层成员是内层 zip 的原始字节

        result = add_supplements(db, FID, [upload("pkg.zip", outer)])

        assert result["added_count"] == 1
        entry = next(e for e in rec.supplement_index if e["path"] == "bundle.zip")
        assert entry["is_nested_zip"] is True
        assert entry["nested_index"] and entry["nested_index"][0]["path"] == "报告.txt"


# ---------------------------------------------------------------------------
# merged_index / delete_supplement
# ---------------------------------------------------------------------------

class TestMergedAndDelete:
    def test_merged_index_marks_manual(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])

        merged = merged_index(rec)
        by_path = {e["path"]: e for e in merged}
        assert by_path["meta.json"].get("is_manual") is None, "原始条目不带 is_manual"
        assert by_path["note.txt"]["is_manual"] is True
        assert len(merged) == len(rec.file_index) + 1

    def test_merged_index_without_supplements_returns_base(self, unpack_dir):
        """无补传：merged 即 file_index（SimpleNamespace 旧形态兼容）"""
        rec = SimpleNamespace(file_index=[{"path": "a"}], supplement_index=None)
        assert merged_index(rec) == [{"path": "a"}]
        rec2 = SimpleNamespace(file_index=[{"path": "a"}])
        assert merged_index(rec2) == [{"path": "a"}]

    def test_delete_supplement_removes_entry_and_file(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n"), upload("keep.txt", b"k")])

        result = delete_supplement(db, FID, "note.txt")

        assert result == {"file_id": FID, "deleted": "note.txt", "remaining_count": 1}
        assert not (supp_root / FID / "note.txt").exists()
        assert (supp_root / FID / "keep.txt").is_file()
        assert [e["path"] for e in rec.supplement_index] == ["keep.txt"]

    def test_delete_original_package_file_rejected(self, unpack_dir, supp_root):
        """原始执行包内容只读：删除报中文错误"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])

        with pytest.raises(HubFileError) as exc:
            delete_supplement(db, FID, "original.txt")
        assert "原始执行包内容" in str(exc.value)
        assert (unpack_dir / "original.txt").is_file()

    def test_delete_missing_supplement_rejected(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        with pytest.raises(HubFileError) as exc:
            delete_supplement(make_db(first=rec), FID, "不存在.txt")
        assert "不存在" in str(exc.value)


# ---------------------------------------------------------------------------
# 镜像合并（resolve_hub_file_fields）
# ---------------------------------------------------------------------------

class TestMirrorMerge:
    def _data(self, fid=FID):
        return {"pkg": fid}

    def test_mirror_contains_supplements_and_overrides_original(self, unpack_dir, supp_root, tmp_path):
        """镜像含补传文件；同名以补传版为准（补传压过原包）"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [
            upload("original.txt", "补传版".encode("utf-8")),
            upload("extra.txt", "新增".encode("utf-8")),
        ])
        mirror_root = tmp_path / "mirror"

        resolved = resolve_hub_file_fields(self._data(), ["pkg"], cache_root=str(mirror_root), db=db)

        mirror = mirror_root / FID
        assert resolved == {"pkg": str(mirror)}
        assert (mirror / "original.txt").read_bytes() == "补传版".encode("utf-8"), "同名必须以补传版为准"
        assert (mirror / "extra.txt").read_bytes() == "新增".encode("utf-8")
        assert (mirror / "meta.json").is_file(), "原始包文件仍在镜像中"

    def test_reuse_branch_merges_new_supplements(self, unpack_dir, supp_root, tmp_path):
        """复用分支增量合并：镜像已建后新增的补传，同样进入本次任务镜像"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        mirror_root = tmp_path / "mirror"
        resolve_hub_file_fields(self._data(), ["pkg"], cache_root=str(mirror_root), db=db)
        assert not (mirror_root / FID / "late.txt").exists()

        add_supplements(db, FID, [upload("late.txt", "后来补的".encode("utf-8"))])
        resolved = resolve_hub_file_fields(self._data(), ["pkg"], cache_root=str(mirror_root), db=db)

        assert (mirror_root / FID / "late.txt").read_bytes() == "后来补的".encode("utf-8")

    def test_mirror_without_supplements_unchanged(self, unpack_dir, tmp_path):
        """无补传：镜像行为与原实现一致（回归）"""
        rec = make_record(unpack_dir)
        resolved = resolve_hub_file_fields(
            self._data(), ["pkg"], cache_root=str(tmp_path / "mirror"),
            db=make_db(first=rec),
        )
        mirror = tmp_path / "mirror" / FID
        assert (mirror / "original.txt").read_bytes() == "原始内容".encode("utf-8")


# ---------------------------------------------------------------------------
# 生命周期：归零联动清理补充区
# ---------------------------------------------------------------------------

class TestSupplementLifecycle:
    def test_release_zero_cleans_supplement_dir(self, unpack_dir, supp_root):
        """ref_count 归零：补充区随包清理"""
        rec = make_record(unpack_dir, ref_count=1)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])
        supp_dir = supp_root / FID
        assert supp_dir.is_dir()

        assert release_ref(db, FID) == 0
        assert not supp_dir.exists(), "归零后补充区应被清理"
        assert not unpack_dir.exists()

    def test_release_above_zero_keeps_supplement_dir(self, unpack_dir, supp_root):
        """未归零：补充区保留"""
        rec = make_record(unpack_dir, ref_count=2)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])

        assert release_ref(db, FID) == 1
        assert (supp_root / FID / "note.txt").is_file()

    def test_discard_unreferenced_cleans_supplement_dir(self, unpack_dir, supp_root):
        """导入回滚（discard）：未引用的包连同补充区一起丢弃"""
        rec = make_record(unpack_dir, ref_count=0)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])

        assert discard_unpacked_if_unreferenced(db, FID) is True
        assert not (supp_root / FID).exists()


# ---------------------------------------------------------------------------
# 匹配预览叠加（hub_match_preview）
# ---------------------------------------------------------------------------

class TestMatchPreviewMerge:
    def _db(self, record, row):
        db = MagicMock()

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", str(model))
            if name == "EvaluationDataV2":
                q.filter.return_value.order_by.return_value.all.return_value = [row]
            elif name == "HubFileRecord":
                q.filter.return_value.first.return_value = record
            return q

        db.query.side_effect = _query
        return db

    def test_match_preview_matches_supplement_file(self, unpack_dir, supp_root):
        """补传文件参与匹配预览（与执行镜像一致）"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("extra.log", b"L")],
                        relative_paths=["inputs/extra.log"])
        row = SimpleNamespace(index=0, data={"pkg": FID})

        result = hub_match_preview(self._db(rec, row), "ds-1", "pkg", "**/*.log")

        assert "inputs/extra.log" in result["sample_paths"]

    def test_match_preview_supplement_only_index_satisfies(self, unpack_dir, supp_root):
        """file_index 为空但补传非空：预览不报『无可用索引』"""
        rec = make_record(unpack_dir)
        rec.file_index = []
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("only.txt", b"t")])
        row = SimpleNamespace(index=0, data={"pkg": FID})

        result = hub_match_preview(self._db(rec, row), "ds-1", "pkg", "*.txt")
        assert result["matched_count"] == 1


# ---------------------------------------------------------------------------
# API 层直调
# ---------------------------------------------------------------------------

class TestSupplementApi:
    def _request(self, user_code="qa-api"):
        return SimpleNamespace(state=SimpleNamespace(user={"user_code": user_code}))

    def _upload_file(self, name, content):
        return SimpleNamespace(filename=name, file=BytesIO(content))

    def test_tree_returns_merged_index(self, unpack_dir, supp_root):
        """树端点返回叠加视图（补传条目带 is_manual）"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])

        result = get_hub_file_tree(FID, db=db)
        by_path = {e["path"]: e for e in result["file_index"]}
        assert by_path["note.txt"]["is_manual"] is True
        assert "is_manual" not in by_path["meta.json"]

    def test_content_falls_back_to_supplement_dir(self, unpack_dir, supp_root):
        """content 端点：解包区 miss 时回退补充区（补传文件可预览）"""
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", "补传内容".encode("utf-8"))])

        result = get_hub_file_content(FID, "note.txt", db=db)
        assert result["content"] == "补传内容"
        assert result["path"] == "note.txt"

    def test_upload_endpoint_happy_path(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)

        result = upload_supplements(
            FID, self._request(),
            files=[self._upload_file("note.txt", "通过端点".encode("utf-8"))],
            relative_paths=["note.txt"],
            target_dir="",
            db=db,
        )

        assert result["added_count"] == 1
        assert (supp_root / FID / "note.txt").read_bytes() == "通过端点".encode("utf-8")
        assert rec.supplement_index[0]["uploaded_by"] == "qa-api"

    def test_upload_endpoint_unregistered_404(self, supp_root):
        db = make_db(first=None)
        with pytest.raises(HTTPException) as exc:
            upload_supplements(
                "fid-gone", self._request(),
                files=[self._upload_file("a.txt", b"x")],
                relative_paths=[], target_dir="", db=db,
            )
        assert exc.value.status_code == 404

    def test_upload_endpoint_validation_maps_400(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        with pytest.raises(HTTPException) as exc:
            upload_supplements(
                FID, self._request(),
                files=[self._upload_file("a.txt", b"x")],
                relative_paths=[], target_dir="../evil", db=db,
            )
        assert exc.value.status_code == 400
        assert "穿越" in exc.value.detail

    def test_delete_endpoint_and_missing_400(self, unpack_dir, supp_root):
        rec = make_record(unpack_dir)
        db = make_db(first=rec)
        add_supplements(db, FID, [upload("note.txt", b"n")])

        result = delete_supplement_endpoint(FID, path="note.txt", db=db)
        assert result["deleted"] == "note.txt"

        with pytest.raises(HTTPException) as exc:
            delete_supplement_endpoint(FID, path="note.txt", db=db)
        assert exc.value.status_code == 400
