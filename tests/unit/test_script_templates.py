"""脚手架模板单元测试（单模板：运行时工作区骨架）

- 唯一模板 zip（hub_package）存在于 app/static/script_templates/
- 下载解压即运行时工作区骨架：scripts/{script_key}/（脚本包本体：main.py+model.py+
  lib+README）+ inputs/{字段}/{execution_id}-attempt-1/（执行包样例，含 manifest/
  acceptance-map/嵌套 zip 等）+ inputs/{字段}_index.json + sample_stdin.json
- 脚本包本体过平台同一套上传校验链（compile/路径扫描/风险声明/依赖/契约冒烟），
  必须零风险、冒烟通过；model.py 须过导入强校验
- 本地试跑：解压根直接 `python scripts/hub_package/main.py < sample_stdin.json` 可跑
- 冒烟样例（build_smoke_stdin）按真实 anc-hub 执行包层级组织：解包根下唯一顶层
  {execution_id}-attempt-1/，rel_path 全部带前缀，索引与生产 build_file_index 同构
- API：templates 列表端点与 /{name}/download 下载端点（FileResponse、未知名/穿越名 404）
"""
import asyncio
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import pre_judge_script_service as pjs_service
from app.services.pre_judge_script_service import (
    SCRIPT_TEMPLATES_DIR,
    SMOKE_ATTEMPT_DIR,
    _collect_package_sources,
    build_smoke_stdin,
    validate_script_chain,
)
from app.services.pre_judge_runner import SCRIPT_PYTHON
from app.api.pre_judge_script_api import download_template, list_templates

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）

TEMPLATE_NAMES = ("hub_package",)
SAMPLE_ATTEMPT_DIR = "62ed5009-2f58-40bd-9e43-8bac7a9eacbf-attempt-1"


def _template_zip(name: str) -> Path:
    zip_path = SCRIPT_TEMPLATES_DIR / f"{name}.zip"
    assert zip_path.is_file(), f"模板 zip 缺失: {zip_path}"
    return zip_path


def _unpack(name: str, tmp_path: Path) -> Path:
    """解包模板 zip 到临时目录，返回解压根（运行时工作区骨架根）"""
    from app.services.hub_file_service import unpack_zip_safely

    target = tmp_path / name
    unpack_zip_safely(str(_template_zip(name)), str(target))
    return target


def _locate_script_package(root: Path) -> Path:
    """在骨架中定位脚本包本体（scripts/ 下唯一含 main.py 的目录，inputs/ 样例除外）"""
    entries = [p for p in root.rglob("main.py") if "inputs" not in p.parts]
    assert len(entries) == 1, f"骨架应含唯一脚本入口 main.py: {entries}"
    return entries[0].parent


def _run_entry(main_py: Path, stdin: dict, cwd: "Path | None" = None) -> dict:
    """以定制 stdin 真子进程执行入口，返回最后一行契约 JSON

    cwd 缺省为脚本所在目录（lib import 依赖 sys.path[0]，与 cwd 无关）；
    本地样例试跑传解压根（sample_stdin 的相对路径按文档用法从解压根解析）。
    """
    proc = subprocess.run(
        [SCRIPT_PYTHON, str(main_py)],
        input=json.dumps(stdin, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=str(cwd or main_py.parent),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    lines = [line for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return json.loads(lines[-1])


def _hub_files(root: Path, rel_paths: list) -> dict:
    """构造 inputs 的 root/files 模式（文件对象数组）"""
    return {
        "root": str(root),
        "files": [
            {"path": str(root / rel), "rel_path": rel,
             "size": (root / rel).stat().st_size, "type": "text"}
            for rel in rel_paths
        ],
    }


# ---------------- 模板存在性与骨架结构 ----------------

class TestTemplateStructure:
    def test_zip_exists(self):
        _template_zip("hub_package")

    def test_zip_skeleton(self, tmp_path):
        """解压根即运行时工作区骨架：scripts/{key}/ 脚本包 + inputs/ 执行包样例 + 索引"""
        root = _unpack("hub_package", tmp_path)
        pkg = root / "scripts" / "hub_package"
        assert (pkg / "main.py").is_file()
        assert (pkg / "model.py").is_file()
        assert (pkg / "README.md").is_file()
        assert (pkg / "lib" / "helpers.py").is_file()

        attempt = root / "inputs" / "hub_pkg" / SAMPLE_ATTEMPT_DIR
        for rel in (
            "execution-manifest.json", "acceptance-map.json", "case_list.json",
            "claude-thread.zip", "self-check.md", "failure-analysis.json", "clarifications.html",
            # 真实包根级文件形态补齐
            "execution-context.json", "handoff.json", "report-receipt.json",
            "self-check.json", "clarifications.json", "case_list.json.lock",
        ):
            assert (attempt / rel).is_file(), f"执行包样例缺 {rel}"
        for d in ("action-files", "actions", "scripts", "source", "reports", "segments"):
            assert (attempt / d).is_dir() and any((attempt / d).iterdir()), f"执行包样例缺 {d}/"
        # 评测脚本最常消费对象的形状（allure 结果 / 截图 / 分段产物）
        assert list((attempt / "reports" / "allure_results").glob("*-result.json"))
        assert list((attempt / "reports" / "screenshots").glob("*.png"))
        seg = next((attempt / "segments").iterdir())
        assert (seg / "pytest.log").is_file() and (seg / "allure-data").is_dir()
        # 真实形状：action-files 是 xlsx 导出、source 三件套
        assert list((attempt / "action-files").glob("*.xlsx"))

        assert (root / "inputs" / "hub_pkg_index.json").is_file()
        assert (root / "outputs").is_dir(), "骨架必须含 outputs/（脚本产物目录）"
        assert (root / "outputs" / "README.md").is_file()
        assert (root / "sample_stdin.json").is_file()
        assert (root / "README.md").is_file()
        assert "运行时工作区骨架" in (root / "README.md").read_text(encoding="utf-8")
        agents = root / "AGENTS.md"
        assert agents.is_file(), "骨架必须含 AGENTS.md（AI agent 开发/打包指引）"
        agents_text = agents.read_text(encoding="utf-8")
        for keyword in (
            "scripts/hub_package", "路径三原则", "打包", "sample_stdin.json",
            # 模板定位：hub_package 只读参考，新脚本在 scripts/ 下另建目录
            "参考模板", "不要修改", "scripts/{你的脚本名}",
        ):
            assert keyword in agents_text, f"AGENTS.md 缺少关键指引: {keyword}"

    def test_sample_index_matches_files(self, tmp_path):
        """索引与实际样例文件一致（生产 build_file_index 同构，防样例漂移）"""
        root = _unpack("hub_package", tmp_path)
        pkg_root = root / "inputs" / "hub_pkg"
        rows = json.loads((root / "inputs" / "hub_pkg_index.json").read_text(encoding="utf-8"))
        actual = {
            p.relative_to(pkg_root).as_posix()
            for p in pkg_root.rglob("*") if p.is_file()
        }
        assert {row["path"] for row in rows} == actual
        for row in rows:
            assert row["is_nested_zip"] is (row["path"].endswith(".zip"))
            assert (pkg_root / row["path"]).stat().st_size == row["size"]

    def test_sample_stdin_points_into_sample(self, tmp_path):
        """sample_stdin 的 root/index_path 指向骨架内 inputs 样例，且相对路径真实存在"""
        root = _unpack("hub_package", tmp_path)
        stdin = json.loads((root / "sample_stdin.json").read_text(encoding="utf-8"))
        hub_pkg = stdin["inputs"]["hub_pkg"]
        assert (root / hub_pkg["root"]).is_dir()
        assert (root / hub_pkg["index_path"]).is_file()

    def test_zip_model_py_passes_import_validation(self, tmp_path):
        """脚本包 model.py 必须过导入强校验且可被平台解析（hub_file 字段声明）"""
        from app.services.script_schema_parser import parse_model_schemas, validate_model_py_source

        pkg = _locate_script_package(_unpack("hub_package", tmp_path))
        source = (pkg / "model.py").read_text(encoding="utf-8")
        assert validate_model_py_source(source) is None
        parsed = parse_model_schemas(source)
        assert parsed["warnings"] == []
        assert set(parsed["input_fields"]) == {"hub_pkg"}
        assert parsed["input_fields"]["hub_pkg"]["value_type"] == "hub_file"
        assert parsed["output_schema"]

    def test_all_registered(self):
        assert set(pjs_service.SCRIPT_TEMPLATE_DEFS) == set(TEMPLATE_NAMES)


# ---------------- 脚本包本体过校验链（与上传通道同一条链） ----------------

class TestTemplatePassesValidationChain:
    def test_chain_passes(self, tmp_path):
        """scripts/{key}/ 脚本包本体过 compile/路径扫描/风险声明/依赖/契约冒烟"""
        pkg = _locate_script_package(_unpack("hub_package", tmp_path))
        sources = _collect_package_sources(pkg)
        assert "main.py" in sources
        check = validate_script_chain(sources, pkg / "main.py")
        assert check["static_check"] == []  # 模板必须零风险命中
        smoke = check["smoke_output"]
        assert isinstance(smoke, dict)
        assert smoke.get("status") in ("ok", "skip")
        assert "status" in smoke and "outputs" in smoke and "message" in smoke

    def test_chain_rejects_raw_skeleton_zip(self, tmp_path):
        """骨架 zip 不能直接当脚本包上传（README 已指引打包 scripts/ 内文件）——
        _locate_package_root 对多顶层目录报中文错误，此行为是预期防护"""
        from app.services.pre_judge_script_service import _locate_package_root

        root = _unpack("hub_package", tmp_path)
        with pytest.raises(ValueError):
            _locate_package_root(root)

    def test_folder_zip_importable(self, tmp_path):
        """按用户实际操作打包：对 scripts/{script_key} 文件夹整体压缩为 zip
        （zip 根为一层 {script_key}/ 目录），导入包根应正确定位到该目录"""
        import shutil

        from app.services.hub_file_service import unpack_zip_safely
        from app.services.pre_judge_script_service import _locate_package_root

        root = _unpack("hub_package", tmp_path)
        shutil.make_archive(str(tmp_path / "upload"), "zip",
                            root_dir=str(root / "scripts"), base_dir="hub_package")
        staging = tmp_path / "staging"
        unpack_zip_safely(str(tmp_path / "upload.zip"), str(staging))
        package_root = _locate_package_root(staging)
        assert package_root == staging / "hub_package"
        assert (package_root / "main.py").is_file()
        assert (package_root / "model.py").is_file()


# ---------------- 本地试跑（下载即可开发验证） ----------------

class TestLocalSampleRun:
    def test_sample_stdin_runs(self, tmp_path):
        """解压根直接跑 `python scripts/hub_package/main.py < sample_stdin.json`：
        泛化遍历 inputs（索引回退还原清单）→ attempt 分组 → manifest 摘要"""
        root = _unpack("hub_package", tmp_path)
        stdin = json.loads((root / "sample_stdin.json").read_text(encoding="utf-8"))
        result = _run_entry(root / "scripts" / "hub_package" / "main.py", stdin, cwd=root)
        assert result["status"] == "ok"
        assert result["outputs"]["input_fields"] == ["hub_pkg", "hub_pkg_all"]
        for field in ("hub_pkg", "hub_pkg_all"):
            info = result["outputs"]["fields"][field]
            assert info["mode"] == "index"
            assert info["attempt_tops"] == [SAMPLE_ATTEMPT_DIR]
            assert info["file_count"] == 32
        assert result["outputs"]["manifest_summary"]["case_total"] == 3
        assert result["outputs"]["manifest_summary"]["case_passed"] == 2
        # 产物规范：文件产物落解压根 outputs/（_run_dir="."，files 为相对路径）
        # 并在 files 注册；真实运行时 _run_dir 是绝对路径，files 亦为绝对路径
        summary = Path(result["files"]["manifest_summary.json"])
        if not summary.is_absolute():
            summary = root / summary
        assert summary.is_file()
        assert summary == (root / "outputs" / "manifest_summary.json")

    def test_smoke_stdin_runs(self, tmp_path):
        """平台冒烟样例（root/files 模式）同样跑通：泛化消费不写死字段名"""
        pkg = _locate_script_package(_unpack("hub_package", tmp_path))
        smoke_run_dir = tmp_path / "smoke"
        smoke_run_dir.mkdir()
        result = _run_entry(pkg / "main.py", build_smoke_stdin(smoke_run_dir))
        assert result["status"] == "ok"
        assert result["outputs"]["input_fields"] == ["hub_pkg", "hub_pkg_all"]
        assert result["outputs"]["fields"]["hub_pkg"]["mode"] == "files"
        assert result["outputs"]["fields"]["hub_pkg"]["file_count"] == 4
        assert result["outputs"]["fields"]["hub_pkg"]["attempt_tops"] == [SMOKE_ATTEMPT_DIR]
        assert result["outputs"]["manifest_summary"]["case_total"] == 2
        # 冒烟 run_dir 的 outputs/ 由 build_smoke_stdin 保证创建，产物真实落盘
        assert (smoke_run_dir / "outputs" / "manifest_summary.json").is_file()

    def test_renamed_field_consumed(self, tmp_path):
        """字段名不是 hub_pkg 时（编排映射改名场景）泛化消费照样工作"""
        pkg = _locate_script_package(_unpack("hub_package", tmp_path))
        attempt = "5a4b3c2d-1e0f-4a5b-8c7d-6e5f4a3b2c1d-attempt-1"
        pkg_root = tmp_path / "renamed_pkg"
        (pkg_root / attempt).mkdir(parents=True)
        (pkg_root / attempt / "execution-manifest.json").write_text(json.dumps({
            "execution_id": attempt.removesuffix("-attempt-1"),
            "case_results": {"TC_001": "passed"}}), encoding="utf-8")
        stdin = {"inputs": {"exec_pkg": _hub_files(pkg_root, [f"{attempt}/execution-manifest.json"])},
                 "params": {}, "upstream": {}}
        result = _run_entry(pkg / "main.py", stdin)
        assert result["status"] == "ok"
        assert result["outputs"]["input_fields"] == ["exec_pkg"]
        assert result["outputs"]["fields"]["exec_pkg"]["attempt_tops"] == [attempt]
        assert result["outputs"]["manifest_summary"]["case_total"] == 1


# ---------------- 冒烟样例结构（与真实执行包层级一致） ----------------

class TestSmokeStdinLayout:
    def test_smoke_stdin_mirrors_real_attempt_layout(self, tmp_path):
        """冒烟样例：解包根下唯一顶层 {execution_id}-attempt-1/，rel_path 全带前缀"""
        run_dir = tmp_path / "smoke_layout"
        run_dir.mkdir()
        stdin = build_smoke_stdin(run_dir)
        pkg_dir = Path(stdin["inputs"]["hub_pkg"]["root"])
        entries = [e.name for e in pkg_dir.iterdir()]
        assert entries == [SMOKE_ATTEMPT_DIR]  # 唯一顶层目录（单顶层自动拼接语义成立）
        for item in stdin["inputs"]["hub_pkg"]["files"]:
            assert item["rel_path"].startswith(f"{SMOKE_ATTEMPT_DIR}/")
        index_rows = json.loads(
            Path(stdin["inputs"]["hub_pkg_all"]["index_path"]).read_text(encoding="utf-8"))
        # 索引条目与生产 build_file_index 同构：{path: 相对路径, type, size, is_nested_zip}
        assert {row["path"] for row in index_rows} == {
            item["rel_path"] for item in stdin["inputs"]["hub_pkg"]["files"]}
        assert all(row["is_nested_zip"] is False for row in index_rows)
        assert all("rel_path" not in row for row in index_rows)

    def test_smoke_stdin_manifest_parses(self, tmp_path):
        """冒烟样例带 execution-manifest.json（dict 形态 case_results，与真实包一致）"""
        run_dir = tmp_path / "smoke_manifest"
        run_dir.mkdir()
        stdin = build_smoke_stdin(run_dir)
        manifest_path = (
            Path(stdin["inputs"]["hub_pkg"]["root"]) / SMOKE_ATTEMPT_DIR / "execution-manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert isinstance(manifest.get("case_results"), dict)
        assert manifest.get("execution_id")


# ---------------- 行为级验证（真实数据跑模板逻辑） ----------------

class TestTemplateBehavior:
    def test_hub_package_groups_by_attempt(self, tmp_path):
        """按 attempt 顶层分组 + manifest 摘要（不硬编码 attempt 名）"""
        pkg = _locate_script_package(_unpack("hub_package", tmp_path))
        attempt = "3f2a1c0d-9e8b-4a7c-b6d5-e4f3a2b1c0d9-attempt-1"
        pkg_root = tmp_path / "hub_pkg_behavior"
        (pkg_root / attempt / "reports").mkdir(parents=True)
        (pkg_root / attempt / "logs").mkdir(parents=True)
        (pkg_root / attempt / "execution-manifest.json").write_text(json.dumps({
            "execution_id": attempt.removesuffix("-attempt-1"),
            "case_results": {"TC_001": "passed", "TC_002": "failed", "TC_003": "failed"},
        }, ensure_ascii=False), encoding="utf-8")
        (pkg_root / attempt / "reports" / "r1-result.json").write_text("{}", encoding="utf-8")
        (pkg_root / attempt / "logs" / "run.log").write_text("INFO\n", encoding="utf-8")

        rels = [
            f"{attempt}/execution-manifest.json",
            f"{attempt}/reports/r1-result.json",
            f"{attempt}/logs/run.log",
        ]
        stdin = {"inputs": {"hub_pkg": _hub_files(pkg_root, rels)}, "params": {}, "upstream": {}}
        result = _run_entry(pkg / "main.py", stdin)
        assert result["status"] == "ok"
        assert result["outputs"]["fields"]["hub_pkg"]["attempt_tops"] == [attempt]
        assert result["outputs"]["fields"]["hub_pkg"]["file_count"] == 3
        assert result["outputs"]["manifest_summary"]["case_total"] == 3
        assert result["outputs"]["manifest_summary"]["case_passed"] == 1
        assert result["outputs"]["manifest_summary"]["case_failed"] == 2


# ---------------- API：templates 列表与下载 ----------------

class TestTemplatesApi:
    def test_list_templates(self):
        body = asyncio.run(list_templates())
        assert body["total"] == 1
        names = {item["name"] for item in body["items"]}
        assert names == set(TEMPLATE_NAMES)
        for item in body["items"]:
            assert item["size"] > 0
            assert item["download_path"] == f"/pre-judge-scripts/templates/{item['name']}/download"
            assert item["description"]

    def test_download_template_returns_zip(self):
        response = asyncio.run(download_template("hub_package"))
        assert response.media_type == "application/zip"
        zip_path = Path(response.path)
        assert zip_path == SCRIPT_TEMPLATES_DIR / "hub_package.zip"
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert "README.md" in names
            assert "scripts/hub_package/main.py" in names
            assert "inputs/hub_pkg_index.json" in names
            assert f"inputs/hub_pkg/{SAMPLE_ATTEMPT_DIR}/execution-manifest.json" in names

    def test_download_unknown_404(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(download_template("no_such_template"))
        assert exc.value.status_code == 404

    @pytest.mark.parametrize("bad_name", ["../hub_package", "hub_package.zip", "..", ""])
    def test_download_traversal_or_bad_name_404(self, bad_name):
        """模板名仅允许注册名（防路径穿越），非法名一律 404"""
        with pytest.raises(HTTPException) as exc:
            asyncio.run(download_template(bad_name))
        assert exc.value.status_code == 404
