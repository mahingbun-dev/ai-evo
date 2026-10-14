"""hub_file 输入映射与 zip 脚本包单元测试（阶段B：验收 B3 / B4 / B5 / B6 / B7）

- B3 input_mapping hub_file 通道：配 path_pattern → 输入值 {root, files:
  [{path,rel_path,size,type}]}（共享匹配器语义）；未配 → {root, index_path}，
  索引写运行目录 inputs/{field}_index.json。缺陷B3 起 hub 通道值注入
  stdin.inputs（与脚手架 README / build_smoke_stdin 冒烟契约一致），
  inline/local 仍注入 stdin.fields
- B4 path_pattern 匹配 0 文件：required=true（默认）→ 该脚本按 fail 处理走
  on_fail(continue/abort)；required=false → files: [] 继续执行
- B5 zip 脚本包：entrypoint 指向解包形态 uploads/pre_judge_scripts/{id}/main.py，
  子进程可执行且包内同目录 import（wms_support/）可用（现场合成 zip 实跑）
- B6 validate_pre_scripts_config：path_pattern 相对路径/禁 ../合法 glob、
  required 布尔、目标字段必须是数据集 hub_file 类型字段
- B7 stdin 契约：inputs/_run_dir 等原有键不动（hub_file 通道的 inputs 值为对象，
  inline/local 的 fields 值仍为路径字符串）

fake 脚本为统一子进程执行形态（DB script 记录 + 目录式落盘 + run(stdin)
函数式源码，经 wrapper 实跑；捕获的契约经脚本标准输出回传），产物目录
SCRIPT_RUN_BASE 指向 tmp_path，zip 包现场合成，不依赖网络与真实 DB。
"""
import json
import os
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import pre_judge_runner
from app.services.hub_file_service import build_file_index, unpack_zip_safely
from app.services.pre_judge_script_service import PreJudgeScriptService

pytestmark = pytest.mark.unit  # W8 分类：unit（无网络/无 DB）


# ---------------- fake 脚本（统一子进程：run(stdin) 函数式，捕获 stdin 契约） ----------------

_HUB_CAPTURE_SRC = '''
def run(stdin):
    return {
        "status": "ok",
        "output": {
            "fields": stdin.get("fields"),
            "inputs": stdin.get("inputs"),
            "stdin_keys": sorted(stdin.keys()),
        },
        "files": {},
        "summary": "captured",
        "finish_reason": "completed",
        "error": None,
    }
'''

_PLAIN_ECHO_SRC = '''
def run(stdin):
    """第二个编排步骤用：证明 fail(continue) 后续脚本继续执行"""
    return {
        "status": "ok",
        "output": {"ran": True},
        "files": {},
        "summary": "",
        "finish_reason": "completed",
        "error": None,
    }
'''


@pytest.fixture
def fake_builtins(tmp_path, monkeypatch):
    """fake 脚本落库（exec_type=script）+ 脚本目录落盘，替换 runner.SessionLocal

    StaticPool 保证多个会话共享同一内存库；SCRIPT_UPLOAD_BASE/SCRIPT_RUN_BASE
    指向临时目录（builtin 进程内类型已废除，统一子进程执行）。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base
    from app.models.pre_judge_models import PreJudgeScript
    from app.services import pre_judge_script_service as script_service

    upload_base = tmp_path / "pre_judge_scripts"
    monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", upload_base)
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", tmp_path / "script_runs")

    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        for name, source in (("fake_hub_capture", _HUB_CAPTURE_SRC),
                             ("fake_plain_echo", _PLAIN_ECHO_SRC)):
            script_id = f"pjs_{name}"
            db.add(PreJudgeScript(id=script_id, name=name, exec_type="script",
                                  entrypoint="main.py", timeout_seconds=60, is_active=True))
            pkg = upload_base / script_id
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "main.py").write_text(source, encoding="utf-8")
        db.commit()
    monkeypatch.setattr(pre_judge_runner, "SessionLocal", Session)
    yield Session


@pytest.fixture
def run_base(tmp_path, monkeypatch):
    """产物根目录指向临时目录（fake_builtins 已设置同值，这里保证独立可用）"""
    base = tmp_path / "script_runs"
    monkeypatch.setattr(pre_judge_runner, "SCRIPT_RUN_BASE", base)
    return base


# ---------------- 解包区 fixture ----------------

UNPACK_FILES = {
    "execution-manifest.json": '{"execution_id": "e-9"}',
    "allure/results.json": "[]",
    "logs/run.log": "L1\nL2\n",
    "readme.md": "# pkg",
}


def make_unpack_root(tmp_path, name="fid-pkg"):
    root = tmp_path / "hub_datasets" / name
    for rel, content in UNPACK_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _run_scripts(pre_scripts, data, payload=None):
    task = SimpleNamespace(id="task_hub_x", pre_scripts=pre_scripts)
    return pre_judge_runner.run_pre_scripts(
        task, payload=payload or {}, dataset_item={"id": "data_1", "data": data})


def _capture_output(result):
    """取第一个 fake_hub_capture 步骤捕获的 output"""
    return result["pre_script_results"][0]["output"]


# ---------------------------------------------------------------------------
# B3: hub_file 输入映射两模式
# ---------------------------------------------------------------------------

class TestHubFileInputMapping:
    def test_path_pattern_returns_root_and_files(self, fake_builtins, run_base, tmp_path):
        """配 path_pattern → 输入值 {root, files:[{path,rel_path,size,type}]}（注入 inputs）"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                        "path_pattern": "**/*.json"}}}],
            data={"pkg": str(root)},
        )
        assert out["aborted"] is False
        value = _capture_output(out)["inputs"]["pkg"]
        # 严格拷贝语义：root 是工作区 inputs/ 下的副本（非源目录），内容与源一致
        assert value["root"] != str(root)
        assert Path(value["root"]).is_relative_to(run_base / "task_hub_x" / "data_1" / "inputs")
        assert build_file_index(value["root"]) == build_file_index(str(root))
        assert [f["rel_path"] for f in value["files"]] == [
            "allure/results.json", "execution-manifest.json"]  # 排序 + 仅 json 命中
        for f in value["files"]:
            assert f["path"] == str(Path(value["root"]) / f["rel_path"])
            assert f["type"] == "text"
            assert f["size"] == (root / f["rel_path"]).stat().st_size

    def test_no_pattern_returns_root_and_index(self, fake_builtins, run_base, tmp_path):
        """未配 path_pattern → {root, index_path}，索引写运行目录 inputs/{field}_index.json"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file"}}}],
            data={"pkg": str(root)},
        )
        value = _capture_output(out)["inputs"]["pkg"]
        assert set(value.keys()) == {"root", "index_path"}
        # 严格拷贝语义：root 是工作区副本，索引内容与源一致
        assert Path(value["root"]).is_relative_to(run_base / "task_hub_x" / "data_1" / "inputs")
        assert build_file_index(value["root"]) == build_file_index(str(root))
        index_file = Path(value["index_path"])
        # 索引落在运行目录 inputs/ 下，文件名 {script_field}_index.json
        assert index_file == run_base / "task_hub_x" / "data_1" / "inputs" / "pkg_index.json"
        assert json.loads(index_file.read_text(encoding="utf-8")) == build_file_index(str(root))

    def test_index_named_by_script_field_not_dataset_field(self, fake_builtins, run_base, tmp_path):
        """索引文件名取脚本输入名（与 stdin.inputs 键一致），不取数据集字段名"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"cases": {"field": "pkg", "value_type": "hub_file"}}}],
            data={"pkg": str(root)},
        )
        value = _capture_output(out)["inputs"]["cases"]
        assert value["index_path"].endswith(f"inputs{os.sep}cases_index.json")

    def test_field_value_not_a_dir_fails(self, fake_builtins, run_base):
        """字段值仍是 file_id（未完成 hub 解析）→ 脚本按 fail，错误可定位字段"""
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file"}}}],
            data={"pkg": "fid-unresolved"},
        )
        result = out["pre_script_results"][0]
        assert result["status"] == "fail"
        assert "pkg" in result["error"] and "不可用" in result["error"]

    def test_field_missing_fails(self, fake_builtins, run_base):
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file"}}}],
            data={"note": "x"},
        )
        assert out["pre_script_results"][0]["status"] == "fail"

    def test_invalid_pattern_fails_with_chinese_error(self, fake_builtins, run_base, tmp_path):
        """pattern 含 .. 穿越 → 运行时同样拒绝（配置校验之外的双保险）"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                        "path_pattern": "../secret/*"}}}],
            data={"pkg": str(root)},
        )
        result = out["pre_script_results"][0]
        assert result["status"] == "fail"
        assert "path_pattern 非法" in result["error"]


# ---------------------------------------------------------------------------
# B4: path_pattern 匹配 0 文件的 required 语义
# ---------------------------------------------------------------------------

class TestRequiredSemantics:
    def _steps(self, on_fail="continue", required=None):
        mapping = {"field": "pkg", "value_type": "hub_file", "path_pattern": "nope/*.missing"}
        if required is not None:
            mapping["required"] = required
        return [
            {"script_id": "fake_hub_capture", "input_mapping": {"pkg": mapping},
             "on_fail": on_fail},
            {"script_id": "fake_plain_echo"},
        ]

    def test_required_default_zero_match_continue(self, fake_builtins, run_base, tmp_path):
        """required 默认 true、0 命中 → 该脚本 fail；on_fail=continue 时后续脚本继续、不 abort"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(self._steps(on_fail="continue"), data={"pkg": str(root)})
        first, second = out["pre_script_results"]
        assert first["status"] == "fail"
        assert "nope/*.missing" in first["error"] and "未匹配" in first["error"]
        assert second["status"] == "ok"  # 后续脚本继续执行
        assert out["aborted"] is False

    def test_required_default_zero_match_abort(self, fake_builtins, run_base, tmp_path):
        """on_fail=abort → 编排终止（aborted=True），后续脚本不执行"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(self._steps(on_fail="abort"), data={"pkg": str(root)})
        assert out["aborted"] is True
        assert "未匹配" in (out["error"] or "")
        assert len(out["pre_script_results"]) == 1  # 第二个脚本未执行

    def test_required_false_zero_match_continues(self, fake_builtins, run_base, tmp_path):
        """required=false、0 命中 → files: [] 正常执行（脚本自行处理空清单）"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(self._steps(required=False), data={"pkg": str(root)})
        result = out["pre_script_results"][0]
        assert result["status"] == "ok"
        value = result["output"]["inputs"]["pkg"]
        # 严格拷贝语义：root 是工作区副本（0 命中也先拷贝，脚本拿到空清单自行处理）
        assert Path(value["root"]).is_relative_to(run_base / "task_hub_x" / "data_1" / "inputs")
        assert build_file_index(value["root"]) == build_file_index(str(root))
        assert value["files"] == []

    def test_required_non_bool_fails(self, fake_builtins, run_base, tmp_path):
        """required 非布尔 → 输入组装失败按 fail 处理"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(self._steps(required="yes"), data={"pkg": str(root)})
        result = out["pre_script_results"][0]
        assert result["status"] == "fail"
        assert "required" in result["error"]


# ---------------------------------------------------------------------------
# B7: stdin 契约原有键不动
# ---------------------------------------------------------------------------

class TestStdinContractUnchanged:
    def test_original_stdin_keys_present_with_hub_channel(self, fake_builtins, run_base, tmp_path):
        """hub_file 通道接入后，stdin 原有键（fields/inputs/_run_dir 等）齐全"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                        "path_pattern": "*.md"}},
              "params": {"k": 1}}],
            data={"pkg": str(root)},
            payload={"model_output": "mo"},
        )
        keys = _capture_output(out)["stdin_keys"]
        for expected in ("task_id", "data_id", "fields", "inputs", "model_output", "session",
                         "params", "upstream", "_run_dir"):
            assert expected in keys

    def test_inline_local_still_path_strings(self, fake_builtins, run_base, tmp_path):
        """inline/local 通道输入值仍是路径字符串（不因 hub_file 通道改变）"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {
                  "note": {"field": "note", "value_type": "inline"},
                  "pkg": {"field": "pkg", "value_type": "hub_file", "path_pattern": "*.md"},
              }}],
            data={"note": "hello", "pkg": str(root)},
        )
        captured = _capture_output(out)
        fields, inputs = captured["fields"], captured["inputs"]
        assert isinstance(fields["note"], str) and fields["note"].endswith("note.txt")
        assert isinstance(inputs["pkg"], dict)  # 仅 hub_file 通道为对象，注入 inputs


# ---------------------------------------------------------------------------
# 缺陷B3：hub_file 通道结果注入 stdin.inputs（与脚手架 README / 冒烟契约一致）
# ---------------------------------------------------------------------------

class TestHubInputsChannel:
    def test_hub_value_in_inputs_not_duplicated_in_fields(self, fake_builtins, run_base, tmp_path):
        """hub_file 输入出现在 stdin.inputs 且 stdin.fields 不再重复携带"""
        root = make_unpack_root(tmp_path)
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                        "path_pattern": "*.md"}}}],
            data={"pkg": str(root)},
        )
        captured = _capture_output(out)
        assert set(captured["inputs"].keys()) == {"pkg"}
        assert captured["fields"] == {}  # fields 不重复携带 hub 通道字段

    def test_inputs_key_always_present_without_hub_channel(self, fake_builtins, run_base):
        """无 hub 通道编排的 stdin 同样带 inputs 键（空对象，契约键集稳定）"""
        out = _run_scripts(
            [{"script_id": "fake_hub_capture",
              "input_mapping": {"note": {"field": "note", "value_type": "inline"}}}],
            data={"note": "hi"},
        )
        captured = _capture_output(out)
        assert captured["inputs"] == {}
        assert set(captured["fields"].keys()) == {"note"}


# ---------------------------------------------------------------------------
# B5: zip 脚本包执行（现场合成 zip 实跑）
# ---------------------------------------------------------------------------

MAIN_PY = '''\
import json
import sys

from wms_support.util import add  # 包内同目录 import（sys.path[0] 即包根）


def main():
    stdin = json.load(sys.stdin)
    pkg = stdin["fields"]["pkg"]
    files = pkg.get("files") or []
    print(json.dumps({
        "status": "ok",
        "output": {
            "sum": add(1, 2),
            "root": pkg["root"],
            "n_files": len(files),
            "first_rel": files[0]["rel_path"] if files else None,
        },
        "files": {},
        "summary": "zip package script ok",
        "finish_reason": "completed",
        "error": None,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

WMS_SUPPORT_INIT = ""
WMS_SUPPORT_UTIL = "def add(a, b):\n    return a + b\n"


def make_script_zip_bytes() -> bytes:
    """合成 zip 脚本包：main.py + wms_support/ 同目录包"""
    members = {
        "main.py": MAIN_PY,
        "wms_support/__init__.py": WMS_SUPPORT_INIT,
        "wms_support/util.py": WMS_SUPPORT_UTIL,
        "requirements.txt": "# no external deps\n",
    }
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestZipScriptPackageExecution:
    @pytest.fixture
    def zip_upload_base(self, tmp_path, monkeypatch):
        """zip 包解包根指向 SCRIPT_UPLOAD_BASE（W1-2：entrypoint 只在包内解析，
        运行器按 SCRIPT_UPLOAD_BASE/{script_id}/ 定位脚本目录）"""
        from app.services import pre_judge_script_service as script_service

        base = tmp_path / "uploads" / "pre_judge_scripts"
        monkeypatch.setattr(script_service, "SCRIPT_UPLOAD_BASE", base)
        return base

    def _stdin(self, unpack_root, run_dir):
        return {
            "task_id": "task_zip",
            "data_id": "data_1",
            "fields": {"pkg": {
                "root": str(unpack_root),
                "files": [{
                    "path": str(unpack_root / "execution-manifest.json"),
                    "rel_path": "execution-manifest.json",
                    "size": 20,
                    "type": "text",
                }],
            }},
            "model_output": None,
            "session": None,
            "params": {},
            "upstream": {},
            "_run_dir": str(run_dir),
        }

    def test_zip_package_entrypoint_executes(self, tmp_path, zip_upload_base):
        """zip 脚本包解包到 uploads/pre_judge_scripts/{script_id}/：entrypoint 为
        包内相对路径 main.py，子进程可执行且包内 import（wms_support）可用"""
        script_id = "pjs_zippkg"
        zip_path = tmp_path / f"{script_id}.zip"
        zip_path.write_bytes(make_script_zip_bytes())
        pkg_dir = zip_upload_base / script_id
        unpack_zip_safely(str(zip_path), str(pkg_dir))  # 复用阶段A安全解包
        assert (pkg_dir / "wms_support" / "util.py").is_file()

        run_dir = tmp_path / "script_runs" / "task_zip" / "data_1"
        run_dir.mkdir(parents=True)
        script = {
            "script_id": script_id, "name": "zip包脚本", "exec_type": "script",
            "entrypoint": "main.py", "timeout_seconds": 60,
        }
        result = pre_judge_runner.execute_script(script, self._stdin(pkg_dir, run_dir),
                                                 timeout_seconds=60)
        assert result["status"] == "ok", result
        assert result["output"]["sum"] == 3          # wms_support.util.add 生效
        assert result["output"]["n_files"] == 1      # stdin.fields 传递正确
        assert result["output"]["first_rel"] == "execution-manifest.json"

    def test_relative_entrypoint_resolved_from_cwd(self, tmp_path, zip_upload_base):
        """W1-2：相对主进程 cwd 的历史兜底分支已删除——cwd 相对 entrypoint 一律拒绝，
        入口只按 SCRIPT_UPLOAD_BASE/{script_id}/ 包内解析"""
        script_id = "pjs_ziprel"
        zip_path = tmp_path / f"{script_id}.zip"
        zip_path.write_bytes(make_script_zip_bytes())
        pkg_dir = zip_upload_base / script_id
        unpack_zip_safely(str(zip_path), str(pkg_dir))
        rel_entry = os.path.relpath(str(pkg_dir / "main.py"), Path.cwd())

        run_dir = tmp_path / "script_runs" / "task_zip" / "data_1"
        run_dir.mkdir(parents=True)
        script = {
            "script_id": script_id, "name": "zip包脚本-相对入口", "exec_type": "script",
            "entrypoint": rel_entry, "timeout_seconds": 60,
        }
        result = pre_judge_runner.execute_script(script, self._stdin(pkg_dir, run_dir),
                                                 timeout_seconds=60)
        assert result["status"] == "fail", "cwd 相对 entrypoint 不应再被执行"
        # 拒绝文案按形态二选一：含 .. 穿越 → 穿越提示；不含 ..（同为包内子路径字面量）→ 入口不存在
        assert (".. 穿越" in result["error"]) or ("脚本入口不存在" in result["error"])

    def test_missing_entrypoint_file_fails(self, tmp_path, zip_upload_base):
        """包内相对 entrypoint 指向不存在的文件 → fail（错误含路径）"""
        run_dir = tmp_path / "script_runs" / "task_zip" / "data_1"
        run_dir.mkdir(parents=True)
        script = {
            "script_id": "pjs_missing", "name": "缺失入口", "exec_type": "script",
            "entrypoint": "main.py",
            "timeout_seconds": 30,
        }
        result = pre_judge_runner.execute_script(script, self._stdin(tmp_path, run_dir),
                                                 timeout_seconds=30)
        assert result["status"] == "fail"
        assert "脚本入口不存在" in result["error"]


# ---------------------------------------------------------------------------
# B6: validate_pre_scripts_config 的 path_pattern / hub_file 字段校验
# ---------------------------------------------------------------------------

class TestValidatePreScriptsConfigHub:
    FIELD_TYPES = {"pkg": "hub_file", "note": "inline"}

    def _validate(self, steps, field_types="default"):
        return PreJudgeScriptService.validate_pre_scripts_config(
            steps, script_lookup={"fake_hub_capture": {"exec_type": "builtin"}},
            dataset_field_types=self.FIELD_TYPES if field_types == "default" else field_types,
        )

    def test_valid_hub_channel_config_passes(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                      "path_pattern": "**/*.json"}},
        }])
        assert errors == []

    def test_valid_hub_channel_without_pattern_passes(self):
        """hub_file 通道未配 path_pattern（index 模式）同样合法"""
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file"}},
        }])
        assert errors == []

    def test_absolute_pattern_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                      "path_pattern": "/etc/**"}},
        }])
        assert any("相对路径" in e for e in errors)

    def test_drive_letter_pattern_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                      "path_pattern": "C:/x/*.json"}},
        }])
        assert any("相对路径" in e for e in errors)

    def test_dotdot_pattern_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                      "path_pattern": "../secret/*"}},
        }])
        assert any(".." in e and "穿越" in e for e in errors)

    def test_empty_pattern_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                      "path_pattern": "  "}},
        }])
        assert any("非空" in e for e in errors)

    def test_pattern_without_hub_file_value_type_rejected(self):
        """配 path_pattern 但 value_type 非 hub_file → 配置矛盾报错"""
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "inline",
                                      "path_pattern": "**/*.json"}},
        }])
        assert any("仅支持 hub_file 通道" in e for e in errors)

    def test_required_non_bool_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "pkg", "value_type": "hub_file",
                                      "path_pattern": "*.md", "required": "true"}},
        }])
        assert any("required" in e for e in errors)

    def test_target_field_not_hub_type_rejected(self):
        """目标字段存在但不是 hub_file 类型 → 拒绝"""
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "note", "value_type": "hub_file"}},
        }])
        assert any("不是数据集 hub_file 类型字段" in e for e in errors)

    def test_target_field_missing_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "ghost", "value_type": "hub_file"}},
        }])
        assert any("字段不存在" in e for e in errors)

    def test_field_types_none_skips_type_check(self):
        """dataset_field_types 缺省 None → 跳过目标字段类型校验（向后兼容）"""
        errors = PreJudgeScriptService.validate_pre_scripts_config([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": {"field": "ghost", "value_type": "hub_file",
                                      "path_pattern": "*.md"}},
        }])
        assert errors == []

    def test_non_hub_mapping_untouched(self):
        """非 hub 通道配置不做阶段B新增校验（存量宽松语义不变）"""
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"note": {"field": "note", "value_type": "inline"}},
        }])
        assert errors == []

    def test_invalid_mapping_item_type_rejected(self):
        errors = self._validate([{
            "script_id": "fake_hub_capture",
            "input_mapping": {"pkg": 123},
        }])
        assert any("必须是对象或字段名字符串" in e for e in errors)


# ---------------------------------------------------------------------------
# B6 生产接线：evaluation_task_service._validate_pre_scripts 传入字段类型视图
# ---------------------------------------------------------------------------

def _field_row(name, ftype):
    """DatasetFieldMetadata 行对象（get_field_metadata 读取的属性集）"""
    from types import SimpleNamespace as _NS
    return _NS(field_name=name, field_label=name, field_type=ftype,
               is_media=False, is_media_field=None, is_required=False, position=0)


def _script_row(script_id="allure_vs_cases"):
    """PreJudgeScript 行对象（_validate_script_targets 构建 script_lookup 读取的属性集）"""
    from types import SimpleNamespace as _NS
    return _NS(id=script_id, name=script_id, exec_type="script", output_schema={},
               is_active=True)


def _wiring_db(field_rows, script_rows=None):
    """按被查询表分流的 db mock：dataset_field_metadata 返回字段元数据行，
    pre_judge_scripts 返回脚本行（script_output 行引用的脚本须在视图中存在）"""
    from unittest.mock import MagicMock

    def _table_of(arg):
        return getattr(arg, "__tablename__", None) or getattr(getattr(arg, "table", None), "name", "")

    db = MagicMock()

    def _query(arg):
        chain = MagicMock()
        table = _table_of(arg)
        if table == "dataset_field_metadata":
            chain.filter.return_value.order_by.return_value.all.return_value = field_rows
        else:  # pre_judge_scripts（.all() 直查与 .filter().all() 两种形态）
            rows = script_rows if script_rows is not None else [_script_row()]
            chain.all.return_value = rows
            chain.filter.return_value.all.return_value = rows
        return chain

    db.query.side_effect = _query
    return db


class TestProductionValidateWiring:
    """API 建/改任务链路（_validate_script_targets，契约 v3 script_output 行）的
    目标字段类型校验真实生效"""

    def _validate(self, field_rows, steps):
        from app.services.evaluation_task_service import EvaluationTaskService

        service = EvaluationTaskService(_wiring_db(field_rows))
        targets = [{"source": "script_output", "source_config": cfg} for cfg in steps]
        service._validate_script_targets(targets, dataset_id="ds-1")

    def test_rejects_non_hub_field(self):
        """field=plain(string) 配 hub_file 通道+path_pattern → ValueError（不再被放行）"""
        with pytest.raises(ValueError) as ei:
            self._validate(
                [_field_row("plain", "string")],
                [{"script_id": "allure_vs_cases",
                  "input_mapping": {"cases_file": {
                      "field": "plain", "value_type": "hub_file",
                      "path_pattern": "**/*.json"}}}],
            )
        assert "不是数据集 hub_file 类型字段" in str(ei.value)

    def test_rejects_absolute_pattern(self):
        """path_pattern 绝对路径在生产链路同样被拒"""
        with pytest.raises(ValueError) as ei:
            self._validate(
                [_field_row("pkg", "hub_file")],
                [{"script_id": "allure_vs_cases",
                  "input_mapping": {"cases_file": {
                      "field": "pkg", "value_type": "hub_file",
                      "path_pattern": "/etc/**"}}}],
            )
        assert "相对路径" in str(ei.value)

    def test_accepts_hub_typed_field(self):
        """目标字段为 hub_file 类型且 pattern 合法 → 校验通过（不抛）"""
        self._validate(
            [_field_row("pkg", "hub_file")],
            [{"script_id": "allure_vs_cases",
              "input_mapping": {"cases_file": {
                  "field": "pkg", "value_type": "hub_file",
                  "path_pattern": "**/*.json"}}}],
        )

    def test_no_field_metadata_skips_type_check(self):
        """数据集无字段元数据 → 跳过类型校验（宽松兼容，存量任务不受影响）"""
        self._validate(
            [],
            [{"script_id": "allure_vs_cases",
              "input_mapping": {"cases_file": {
                  "field": "plain", "value_type": "hub_file",
                  "path_pattern": "**/*.json"}}}],
        )
