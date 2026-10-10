"""脚手架模板单元测试（阶段E：E6）

- 三个模板 zip（basic / allure_analysis / file_diff）存在于 app/static/script_templates/
- 每个模板结构自洽：包根 main.py + model.py（契约声明）+ README.md + lib/ 同目录依赖
- 每个模板解包后跑平台同一套上传校验链（compile/路径扫描/风险声明/依赖/契约冒烟），
  必须零风险、冒烟通过（模板即规范的可执行样例）；model.py 须过导入强校验
  （模板下载→导入不断链）
- 行为级验证：allure_analysis / file_diff 以定制 stdin 真子进程跑出正确统计
- API：templates 列表端点与 /{name}/download 下载端点（FileResponse、未知名/穿越名 404）
"""
import asyncio
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import pre_judge_script_service as pjs_service
from app.services.pre_judge_script_service import (
    SCRIPT_TEMPLATES_DIR,
    _collect_package_sources,
    _locate_package_root,
    build_smoke_stdin,
    validate_script_chain,
)
from app.services.pre_judge_runner import SCRIPT_PYTHON
from app.api.pre_judge_script_api import download_template, list_templates

TEMPLATE_NAMES = ("basic", "allure_analysis", "file_diff")


def _template_zip(name: str) -> Path:
    zip_path = SCRIPT_TEMPLATES_DIR / f"{name}.zip"
    assert zip_path.is_file(), f"模板 zip 缺失: {zip_path}"
    return zip_path


def _unpack(name: str, tmp_path: Path) -> Path:
    """解包模板 zip 到临时目录，返回包根目录"""
    from app.services.hub_file_service import unpack_zip_safely

    target = tmp_path / name
    unpack_zip_safely(str(_template_zip(name)), str(target))
    return _locate_package_root(target)


def _run_entry(main_py: Path, stdin: dict) -> dict:
    """以定制 stdin 真子进程执行入口，返回最后一行契约 JSON"""
    proc = subprocess.run(
        [SCRIPT_PYTHON, str(main_py)],
        input=json.dumps(stdin, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=str(main_py.parent),
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


# ---------------- 模板存在性与结构 ----------------

class TestTemplateStructure:
    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_zip_exists(self, name):
        _template_zip(name)

    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_zip_structure(self, name, tmp_path):
        """包根含 main.py / model.py / README.md，且含 lib/ 同目录依赖示例"""
        package_root = _unpack(name, tmp_path)
        assert (package_root / "main.py").is_file()
        assert (package_root / "model.py").is_file()  # 导入通道强校验要求契约声明
        readme = package_root / "README.md"
        assert readme.is_file()
        assert "路径三原则" in readme.read_text(encoding="utf-8")  # 规范精要
        lib_dir = package_root / "lib"
        assert lib_dir.is_dir() and any(lib_dir.glob("*.py")), "模板须含 lib/ 同目录依赖示例"

    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_zip_model_py_passes_import_validation(self, name, tmp_path):
        """模板 model.py 必须过导入强校验（模板下载→导入不断链）且可被平台解析"""
        from app.services.script_schema_parser import parse_model_schemas, validate_model_py_source

        package_root = _unpack(name, tmp_path)
        source = (package_root / "model.py").read_text(encoding="utf-8")
        assert validate_model_py_source(source) is None
        parsed = parse_model_schemas(source)
        assert parsed["warnings"] == []
        if name == "basic":
            assert set(parsed["input_fields"]) == {"note"}
            assert parsed["input_fields"]["note"]["value_type"] == "local"
        else:
            assert parsed["input_fields"] is None  # 空壳输入声明
        assert parsed["output_schema"]  # 输出键声明齐全

    def test_all_three_registered(self):
        assert set(pjs_service.SCRIPT_TEMPLATE_DEFS) == set(TEMPLATE_NAMES)


# ---------------- 模板过校验链（E6 核心：与上传通道同一条链） ----------------

class TestTemplatePassesValidationChain:
    @pytest.mark.parametrize("name", TEMPLATE_NAMES)
    def test_chain_passes(self, name, tmp_path):
        """每个模板解包后通过 compile/路径扫描/风险声明/依赖/契约冒烟"""
        package_root = _unpack(name, tmp_path)
        sources = _collect_package_sources(package_root)
        assert "main.py" in sources
        check = validate_script_chain(sources, package_root / "main.py")
        assert check["static_check"] == []  # 模板必须零风险命中
        smoke = check["smoke_output"]
        assert isinstance(smoke, dict)
        assert smoke.get("status") in ("ok", "skip")
        # 契约输出字段齐全（status/outputs/message）
        assert "status" in smoke and "outputs" in smoke and "message" in smoke

    def test_basic_smoke_outputs(self, tmp_path):
        """basic 模板冒烟：遍历样例 inputs 的 2 个文件与索引"""
        package_root = _unpack("basic", tmp_path)
        smoke_run_dir = tmp_path / "smoke_basic"
        smoke_run_dir.mkdir()
        stdin = build_smoke_stdin(smoke_run_dir)
        result = _run_entry(package_root / "main.py", stdin)
        assert result["status"] == "ok"
        assert result["outputs"]["file_count"] == 2
        assert result["outputs"]["index_file_count"] == 2
        assert {item["rel_path"] for item in result["outputs"]["files"]} == {"sample.txt", "sample.json"}
        assert result["outputs"]["note_preview"].startswith("平台上传校验")


# ---------------- 行为级验证（真实数据跑模板逻辑） ----------------

class TestTemplateBehavior:
    def test_allure_analysis_counts(self, tmp_path):
        """2 条 allure 结果（1 passed / 1 failed）→ 状态分布与失败清单正确"""
        package_root = _unpack("allure_analysis", tmp_path)
        hub_root = tmp_path / "allure-results"
        hub_root.mkdir()
        (hub_root / "0-result.json").write_text(json.dumps(
            {"name": "case_ok", "fullName": "suite case_ok", "status": "passed"}), encoding="utf-8")
        (hub_root / "1-result.json").write_text(json.dumps(
            {"name": "case_bad", "fullName": "suite case_bad", "status": "failed",
             "statusDetails": {"message": "assert 1 == 2"}}), encoding="utf-8")

        stdin = {"inputs": {"hub_pkg": _hub_files(hub_root, ["0-result.json", "1-result.json"])},
                 "params": {}, "upstream": {}}
        result = _run_entry(package_root / "main.py", stdin)
        assert result["status"] == "ok"
        assert result["outputs"]["status_counts"] == {"passed": 1, "failed": 1}
        assert result["outputs"]["passed_rate"] == 0.5
        assert result["outputs"]["failed"][0]["name"] == "case_bad"

    def test_allure_analysis_skip_when_no_results(self, tmp_path):
        package_root = _unpack("allure_analysis", tmp_path)
        stdin = {"inputs": {"hub_pkg": {"root": str(tmp_path), "files": []}}, "params": {}}
        result = _run_entry(package_root / "main.py", stdin)
        assert result["status"] == "skip"

    def test_file_diff_detects_difference(self, tmp_path):
        """两份文件差 1 行 → identical=False、差异行数正确"""
        package_root = _unpack("file_diff", tmp_path)
        hub_root = tmp_path / "diff_pkg"
        hub_root.mkdir()
        (hub_root / "a.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        (hub_root / "b.txt").write_text("line1\nline2-changed\nline3\n", encoding="utf-8")

        stdin = {"inputs": {"hub_pkg": _hub_files(hub_root, ["a.txt", "b.txt"])},
                 "params": {"rel_path_a": "a.txt", "rel_path_b": "b.txt"}, "upstream": {}}
        result = _run_entry(package_root / "main.py", stdin)
        assert result["status"] == "ok"
        assert result["outputs"]["identical"] is False
        assert result["outputs"]["diff_line_count"] == 2  # -line2 / +line2-changed
        assert result["outputs"]["rel_path_a"] == "a.txt"

    def test_file_diff_identical(self, tmp_path):
        package_root = _unpack("file_diff", tmp_path)
        hub_root = tmp_path / "diff_pkg2"
        hub_root.mkdir()
        (hub_root / "a.txt").write_text("same\n", encoding="utf-8")
        (hub_root / "b.txt").write_text("same\n", encoding="utf-8")
        stdin = {"inputs": {"hub_pkg": _hub_files(hub_root, ["a.txt", "b.txt"])}, "params": {}}
        result = _run_entry(package_root / "main.py", stdin)
        assert result["status"] == "ok"
        assert result["outputs"]["identical"] is True
        assert result["outputs"]["diff_line_count"] == 0


# ---------------- API：templates 列表与下载 ----------------

class TestTemplatesApi:
    def test_list_templates(self):
        body = asyncio.run(list_templates())
        assert body["total"] == 3
        names = {item["name"] for item in body["items"]}
        assert names == set(TEMPLATE_NAMES)
        for item in body["items"]:
            assert item["size"] > 0
            assert item["download_path"] == f"/pre-judge-scripts/templates/{item['name']}/download"
            assert item["description"]

    def test_download_template_returns_zip(self):
        response = asyncio.run(download_template("basic"))
        assert response.media_type == "application/zip"
        zip_path = Path(response.path)
        assert zip_path == SCRIPT_TEMPLATES_DIR / "basic.zip"
        with zipfile.ZipFile(zip_path) as zf:
            assert "main.py" in zf.namelist()
            assert "lib/helpers.py" in zf.namelist()

    def test_download_unknown_404(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(download_template("no_such_template"))
        assert exc.value.status_code == 404

    @pytest.mark.parametrize("bad_name", ["../basic", "basic.zip", "..", ""])
    def test_download_traversal_or_bad_name_404(self, bad_name):
        """模板名仅允许注册名（防路径穿越），非法名一律 404"""
        with pytest.raises(HTTPException) as exc:
            asyncio.run(download_template(bad_name))
        assert exc.value.status_code == 404

    def test_downloaded_zip_passes_chain(self, tmp_path):
        """下载端点产出的 zip 与上传通道兼容：解包后可直接过校验链"""
        response = asyncio.run(download_template("file_diff"))
        with zipfile.ZipFile(Path(response.path)) as zf:
            zf.extractall(tmp_path / "dl")
        package_root = _locate_package_root(tmp_path / "dl")
        check = validate_script_chain(_collect_package_sources(package_root), package_root / "main.py")
        assert check["static_check"] == []
