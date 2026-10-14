"""评测前处理脚本（Pre-Judge Script）服务

职责（docs/design/pre-judge-scripts.md 第三/六节 + 统一子进程执行改造 +
anc-hub 执行包脚本文件契约）：
- pre_judge_scripts 表 CRUD（管理员语义简化实现，见 API 层）
- exec_type 收敛为 script / claude_agent / deepseek_agent（builtin 进程内
  类型已废除：内置脚本迁移为 script 记录，由子进程 wrapper 统一执行）
- script 类型目录式存储：uploads/pre_judge_scripts/{id}/（单文件脚本 =
  目录内 main.py）；旧单文件布局 {id}.py 在访问/执行时自动迁移
- 上传阻断校验链（阶段E，单 .py upload 与 zip upload-package 两通道统一
  关卡）：语法编译 / 路径规范扫描（禁硬编码平台内部绝对路径）/ AST 风险
  扫描命中必须风险声明 / 依赖声明逐包校验执行 venv / 契约冒烟（样例 stdin
  经 wrapper 实跑入口，stdout 最后一行必须是 JSON 对象）
- 导入导出包（zip/py，manifest.json 契约）与脚手架模板（from-template
  单/多文件模板 + app/static/script_templates/ zip 模板下载，两套共存）
- 文件树/在线编辑（文本白名单 + 防穿越 + 乐观锁）
- 任务编排 pre_scripts 配置校验（input_mapping 的 path_pattern/required
  与 hub_file 目标字段校验，阶段B）
- script 类型上传脚本运行前静态风险扫描（提示级，不拦截）
- W1 安全收敛：entrypoint 必须为包内相对路径（validate_entrypoint：
  拒绝绝对路径/.. 穿越/不存在文件，save/update/import 全走校验）；
  内置脚本记录（pjs_builtin_*）不可变（禁编辑/删除/覆盖/在线改文件，
  源码随部署强制同步），防止篡改脚本窃取注入的数据库凭据
"""
import ast
import copy
import io
import json
import logging
import posixpath
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from app.models.pre_judge_models import PreJudgeScript
from app.services.hub_file_service import HUB_FIELD_TYPE, HubFileError, unpack_zip_safely
from app.services.script_schema_parser import (
    MODEL_SCHEMA_FILENAME,
    sync_schemas_from_model,
    validate_model_py_source,
)

logger = logging.getLogger(__name__)

VALID_EXEC_TYPES = ("script", "claude_agent", "deepseek_agent")

# script 类型脚本包存放目录（相对项目根，与 uploads/script_runs 同级约定；测试可 monkeypatch）
SCRIPT_UPLOAD_BASE = Path("uploads/pre_judge_scripts")
# 单文件大小上限（2MB）
MAX_SCRIPT_FILE_SIZE = 2 * 1024 * 1024
# 导入包总量上限（10MB）
MAX_PACKAGE_TOTAL_SIZE = 10 * 1024 * 1024
# 导入包文件数上限
MAX_PACKAGE_FILE_COUNT = 200
# 目录式存储的默认入口（脚本目录内相对路径）
DEFAULT_ENTRYPOINT = "main.py"
# zip 脚本包大小上限（20MB，阶段E包上传通道）
MAX_PACKAGE_ZIP_SIZE = 20 * 1024 * 1024
# 包形态入口文件名（包根必须含该文件；与 DEFAULT_ENTRYPOINT 同值，保留两名各自语义）
PACKAGE_ENTRY_FILE = "main.py"
# 契约冒烟超时（秒）：上传校验链以样例 stdin 实跑入口一次的时限
SMOKE_TIMEOUT_SECONDS = 10

# 文件树/在线编辑的文本文件白名单：扩展名集合 + 特殊文件名（任意扩展）
TEXT_FILE_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".sh", ".xml", ".html", ".js", ".sql", ".env",
}
TEXT_FILE_BASENAMES = {"requirements.txt"}
TEXT_FILE_STEMS = {"license", "readme"}

# 内置脚本源码目录（app/scripts_builtin，从本文件位置推算）
_BUILTIN_SOURCE_DIR = Path(__file__).resolve().parent.parent / "scripts_builtin"


class PreJudgeFileError(Exception):
    """脚本文件操作错误（携带 HTTP 状态码，供 API 层映射）"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def is_text_file(rel_path: str) -> bool:
    """判断脚本目录内相对路径是否属于文本白名单（可在线编辑/列出）"""
    pure = PurePosixPath(str(rel_path or "").replace("\\", "/"))
    name = pure.name
    if not name:
        return False
    if name.lower() in TEXT_FILE_BASENAMES:
        return True
    if pure.stem.lower() in TEXT_FILE_STEMS:
        return True
    return pure.suffix.lower() in TEXT_FILE_EXTENSIONS


def is_noise_file(rel_path: str) -> bool:
    """判断相对路径是否为运行噪音/系统文件（导入导出与 file_count 一律排除）

    - __pycache__ 目录（子进程试跑 import 兄弟模块生成的 .pyc 缓存）
    - .pyc / .pyo 编译字节码、.DS_Store（macOS 目录元数据）
    """
    parts = PurePosixPath(str(rel_path or "").replace("\\", "/")).parts
    if not parts:
        return True
    if "__pycache__" in parts:
        return True
    name = parts[-1].lower()
    return name.endswith((".pyc", ".pyo")) or name == ".ds_store"


# ---------------- 静态风险扫描（提示级，不做硬拦截） ----------------

# 风险模块 import：模块根名 → 风险说明
RISKY_IMPORT_MODULES = {
    "subprocess": "子进程调用",
    "socket": "网络访问",
    "ctypes": "原生库调用",
    "multiprocessing": "多进程",
}
# 危险属性调用：(对象名, 方法名) → 风险说明
RISKY_ATTR_CALLS = {
    ("os", "system"): "系统命令执行",
    ("os", "popen"): "系统命令执行",
    ("os", "spawnl"): "系统命令执行",
    ("os", "spawnle"): "系统命令执行",
    ("os", "remove"): "文件删除",
    ("os", "unlink"): "文件删除",
    ("os", "rmdir"): "目录删除",
    ("os", "removedirs"): "目录删除",
    ("shutil", "rmtree"): "目录树删除",
    ("os", "kill"): "进程终止",
    ("os", "fork"): "进程派生",
    ("os", "execv"): "进程替换",
    ("os", "execve"): "进程替换",
}
# 危险裸调用：动态执行类 + from os/shutil import 裸名形态
RISKY_BARE_CALLS = {
    "eval": "动态求值",
    "exec": "动态执行",
    "compile": "动态编译",
    "__import__": "动态导入",
    "system": "系统命令执行",
    "popen": "系统命令执行",
    "rmtree": "目录树删除",
}


def scan_script_risks(source: str) -> List[str]:
    """对脚本源码做静态风险扫描（AST 级）

    扫描项：风险模块 import（subprocess/socket/ctypes 等）、
    os.system/os.popen/os.remove/shutil.rmtree 等危险调用、eval/exec 动态执行。
    返回中文提示列表（含行号），空列表=未发现风险点；源码语法错误也按提示返回。

    语义（阶段E起，双通道）：
    - 上传保存链路（validate_script_chain）：命中即阻断——必须携带
      risk_acknowledged 风险声明文本才允许保存（拒绝理由原样透出命中清单）
    - 运行时链路（pre_judge_runner）：仍为提示级（static_check + 日志，不拦截）
    """
    warnings: List[str] = []
    try:
        tree = ast.parse(source or "")
    except SyntaxError as e:
        return [f"第{e.lineno or '?'}行: 语法解析失败（{e.msg}），静态扫描不完整"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in RISKY_IMPORT_MODULES:
                    warnings.append(
                        f"第{node.lineno}行: import {alias.name}（{RISKY_IMPORT_MODULES[root]}，请确认必要性与输入来源）")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in RISKY_IMPORT_MODULES:
                warnings.append(
                    f"第{node.lineno}行: from {node.module} import ...（{RISKY_IMPORT_MODULES[root]}，请确认必要性与输入来源）")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in RISKY_ATTR_CALLS:
                    warnings.append(f"第{node.lineno}行: {func.value.id}.{func.attr}()（{RISKY_ATTR_CALLS[key]}）")
            elif isinstance(func, ast.Name) and func.id in RISKY_BARE_CALLS:
                warnings.append(f"第{node.lineno}行: {func.id}()（{RISKY_BARE_CALLS[func.id]}）")
    return warnings


def scan_script_dir_risks(script_dir: Path) -> List[str]:
    """扫描脚本目录内全部 .py 文件（含子目录），合并风险提示"""
    warnings: List[str] = []
    if not script_dir.is_dir():
        return warnings
    for py_file in sorted(script_dir.rglob("*.py")):
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:  # noqa: BLE001 - 单文件读取失败不影响整体
            warnings.append(f"{py_file.name}: 读取失败（{e}）")
            continue
        for warning in scan_script_risks(source):
            warnings.append(f"{py_file.name} {warning}" if len(warnings) < 50 else warning)
    return warnings


# ---------------- 上传阻断校验链（阶段E：compile/冒烟/路径扫描/风险声明/依赖校验） ----------------

# 路径规范扫描：禁止在源码硬编码的平台内部/宿主机绝对路径前缀（E3）
# 所有输入路径必须来自 stdin 契约（fields/inputs），产物只写 _run_dir
FORBIDDEN_PATH_PREFIXES = ("/Users/", "/home/", "/root/")
# Windows 盘符绝对路径（C:\、D:/ 等）
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
# 平台内部目录名（uploads/ 为平台保留目录，任何形态都不允许硬编码）
_PLATFORM_DIR_MARKS = ("uploads/", "/uploads/")
# 冒烟样例输出回显上限（字符）
_SMOKE_TAIL_CHARS = 600


def compile_script_sources(sources: Dict[str, str]) -> None:
    """逐文件语法编译校验（E1/E5）：任一文件语法错误即抛 ValueError（中文）"""
    for rel_path, source in sources.items():
        try:
            compile(source, rel_path, "exec")
        except SyntaxError as e:
            raise ValueError(f"{rel_path} 第{e.lineno or '?'}行 语法错误: {e.msg}")
        except ValueError as e:  # 源码含空字节等编码级问题
            raise ValueError(f"{rel_path} 源码无法编译: {e}")


def scan_hardcoded_paths(sources: Dict[str, str]) -> List[str]:
    """路径规范扫描（E3）：源码字符串字面量中硬编码平台内部/宿主机绝对路径

    扫描 AST 字符串常量（含 f-string 字面量分片）：
    - 以 /Users/、/home/、/root/ 开头的宿主机绝对路径
    - Windows 盘符绝对路径（C:\\、D:/ 等）
    - 硬编码平台保留目录 uploads/（任何形态：前缀或路径中间段）

    命中返回中文提示列表（含文件与行号），空列表=通过。
    规范：所有输入路径一律经 stdin（fields/inputs）传入，产物只写 _run_dir。
    """
    hits: List[str] = []
    for rel_path, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue  # 语法错误由 compile_script_sources 阻断，此处跳过避免重复报错
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value.strip()
            if not value:
                continue
            matched = None
            if any(value.startswith(prefix) for prefix in FORBIDDEN_PATH_PREFIXES):
                matched = "宿主机绝对路径"
            elif _WINDOWS_DRIVE_RE.match(value):
                matched = "Windows 盘符绝对路径"
            elif any(mark in value for mark in _PLATFORM_DIR_MARKS):
                matched = "平台保留目录 uploads/"
            if matched:
                hits.append(
                    f"{rel_path} 第{node.lineno}行: 硬编码{matched} {node.value[:80]!r}"
                    "（请改用 stdin 契约传入路径：fields/inputs 携带输入路径，产物只写 _run_dir）"
                )
    return hits


# 冒烟样例执行包的唯一顶层目录名：模拟真实 anc-hub 执行包 zip 外层的
# {execution_id}-attempt-1/ 目录（解包根下仅此一个顶层目录，rel_path 全部带此前缀）。
# 样例层级与生产一致，模板/脚本在冒烟里写惯的 path_pattern 与遍历逻辑到真实任务不变形
SMOKE_ATTEMPT_DIR = "e2a6c9d4-1b2f-4c3d-9e8f-5a6b7c8d9e0f-attempt-1"


def build_smoke_stdin(run_dir: Path) -> Dict[str, Any]:
    """构造契约冒烟用的样例 stdin（E2）：覆盖契约全键并在 run_dir 落真实样例文件

    键集：task_id/data_id/fields/inputs/model_output/session/params/upstream/_run_dir，
    与 pre_judge_runner._run_step 组装的生产 stdin 形态一致（融合后实际形态）。
    inputs 演示 hub_file 通道两种模式：path_pattern 模式 {root, files:[...]}（文件对象
    数组：path/rel_path/size/type）与 root/index_path 模式（索引 JSON 文件路径）；
    fields 携带 inline/local 通道值（值为本地路径字符串）。

    hub 样例目录按真实执行包层级组织：root 下唯一顶层目录 {execution_id}-attempt-1/，
    其内多层子目录（reports/allure_results、logs），rel_path 携带 attempt 前缀——
    与 resolve_hub_file_fields 镜像 + copy_into_workspace 拷贝后的生产形态一致。
    """
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    note_file = inputs_dir / "note.txt"
    note_file.write_text("平台上传校验：契约冒烟样例输入", encoding="utf-8")

    # 脚本产物目录（_run_dir/outputs/）：与生产 build_run_dir 同口径，冒烟环境
    # 也保证存在——按规范写产物的脚本在契约冒烟中即可真实落盘验证
    (run_dir / "outputs").mkdir(exist_ok=True)

    pkg_dir = run_dir / "hub_pkg"
    attempt_dir = pkg_dir / SMOKE_ATTEMPT_DIR
    attempt_dir.mkdir(parents=True, exist_ok=True)
    sample_files = {
        "execution-manifest.json": json.dumps({
            "execution_id": SMOKE_ATTEMPT_DIR.removesuffix("-attempt-1"),
            "case_results": {"TC_001": "passed", "TC_002": "failed"},
            "scope": ["冒烟样例分段"],
        }, ensure_ascii=False),
        "reports/allure_results/case-1-result.json": json.dumps(
            {"name": "TC_001", "status": "passed"}, ensure_ascii=False),
        "reports/allure_results/case-2-result.json": json.dumps(
            {"name": "TC_002", "status": "failed", "message": "样例失败信息"}, ensure_ascii=False),
        "logs/run.log": "INFO 冒烟样例日志行\n",
    }
    files_array = []
    for rel, text in sample_files.items():
        target = attempt_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        files_array.append({
            "path": str(target),
            "rel_path": f"{SMOKE_ATTEMPT_DIR}/{rel}",
            "size": target.stat().st_size,
            "type": "text",
        })
    index_path = inputs_dir / "hub_pkg_index.json"
    # 索引条目与生产 build_file_index 产物同构：{path: 相对 root 的 posix 路径,
    # type, size, is_nested_zip[, nested_index]}——path 与 files 数组的 rel_path 同值
    index_path.write_text(
        json.dumps(
            [{"path": item["rel_path"], "type": item["type"], "size": item["size"],
              "is_nested_zip": False}
             for item in files_array],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "task_id": "upload_smoke",
        "data_id": "smoke_sample",
        "fields": {"note": str(note_file)},
        "inputs": {
            # path_pattern 模式：文件对象数组
            "hub_pkg": {"root": str(pkg_dir), "files": files_array},
            # root/index_path 模式：脚本读索引自行遍历
            "hub_pkg_all": {"root": str(pkg_dir), "index_path": str(index_path)},
        },
        "model_output": {"text": "样例模型输出：订单已拆分为 2 个包裹。"},
        "session": {
            "session_summary": "样例会话摘要",
            "session_stats": {"turns": 3},
            "session_source": "smoke",
        },
        "params": {},
        "upstream": {},
        "_run_dir": str(run_dir),
    }


def run_smoke_test(entry_path: Path, timeout: Optional[float] = None) -> Dict[str, Any]:
    """契约冒烟（E2）：以样例 stdin 实跑入口一次，stdout 最后一行必须可解析为 JSON

    - 子进程解释器与运行链路一致（SCRIPT_PYTHON），并经统一 wrapper
      pre_judge_entry.py 调起（run(stdin) 函数式 / __main__ 传统式自动适配，
      与生产执行 _execute_script_subprocess 完全同路径），cwd 隔离到临时目录
      （样例文件也落在该目录，_run_dir 指向它，跑完即清理）
    - W15 灰度 / ARCH-1-2：SCRIPT_EXEC_MODE=container 时冒烟与生产执行同经
      get_sandbox_driver() 分流（EXECUTOR_URL → executor HTTP 通道；未配置 →
      docker-CLI 单机回退），同镜像同隔离边界；工作区建在 uploads/script_runs/
      smoke/ 下（executor bind mount 白名单子树；/tmp 过不了 WORKSPACE_ROOT
      白名单），入口包拷进工作区 scripts/smoke/ 从副本执行——上传目录不进
      挂载面，仍符合「挂载仅本次工作区」
    - 超时（默认 SMOKE_TIMEOUT_SECONDS=10s）kill 并抛中文错误
    - 退出码非 0 / stdout 最后一个非空行非 JSON / 非对象 → 抛 ValueError，
      错误信息回显 stdout/stderr 尾部输出，便于上传者定位
    - 通过则返回解析出的标准输出 JSON 对象
    """
    # 懒加载 pre_judge_runner（其模块顶层 import 本模块，顶层互引会循环）
    from app.services import script_container
    from app.services.pre_judge_runner import ENTRY_WRAPPER, SCRIPT_PYTHON, build_script_env

    limit = SMOKE_TIMEOUT_SECONDS if timeout is None else float(timeout)
    entry = Path(entry_path)

    # W15：container 模式冒烟与生产执行同通道（docker run --rm，镜像内 wrapper）
    if script_container.container_mode_enabled():
        return _run_smoke_test_in_container(entry, limit)

    # wrapper 形态：[SCRIPT_PYTHON, wrapper, script_dir, entry_rel]（与生产一致）
    script_dir = entry.parent
    entry_rel = entry.name
    with tempfile.TemporaryDirectory(prefix="pjs_smoke_") as tmp:
        run_dir = Path(tmp)
        stdin = build_smoke_stdin(run_dir)
        payload = json.dumps(stdin, ensure_ascii=False, default=str).encode("utf-8")
        try:
            proc = subprocess.run(
                [SCRIPT_PYTHON, str(ENTRY_WRAPPER), str(script_dir), entry_rel],
                input=payload,
                capture_output=True,
                cwd=str(run_dir),
                timeout=limit,
                env=build_script_env(),  # W1-3：与生产执行同一白名单环境（不继承 API 进程 env）
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                f"契约冒烟未通过：入口执行超时（>{limit:.0f} 秒）。"
                "请检查脚本是否阻塞等待输入或存在死循环"
            )
        except OSError as e:
            raise ValueError(f"契约冒烟执行失败（无法启动子进程）: {e}")

        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        _smoke_assert_returncode(proc.returncode, stdout_text, stderr_text)
        return _smoke_parse_stdout(stdout_text, stderr_text)


def _run_smoke_test_in_container(entry: Path, limit: float) -> Dict[str, Any]:
    """契约冒烟的 container 通道（W15 / ARCH-1-2）：经 SandboxDriver 跑沙箱，与生产同驱动

    与 local 冒烟的差异：
    - 工作区建在 uploads/script_runs/smoke/ 下（build_run_dir 同口径，secure mkdir）：
      /tmp 临时目录过不了 executor 的 WORKSPACE_ROOT=/app/uploads/script_runs
      bind mount 白名单（executor/sandbox_params.resolve_workspace_mount），且
      api 容器的 /tmp 对 executor 不可见——工作区必须落在两侧共享的 uploads 卷；
    - 入口包拷进工作区 scripts/smoke/ 副本（上传目录不进容器挂载面）；
    - stdin 内工作区路径重写为容器 /workspace 路径；stdout/stderr 经驱动归位
      到工作区 stdout.log/stderr.log 后读取（两驱动统一口径）；
    - 执行经 get_sandbox_driver() 分流（EXECUTOR_URL → http-executor 目标通道，
      api 容器无 docker CLI/socket 也可冒烟；未配置 → docker-CLI 单机回退）——
      与生产 execute_script 同开关同驱动同容器口径（docs/build-pipeline.md 第九节）。
    镜像不可用（CLI 退出码 125）等启动失败给出运维提示（预载属 D-3）。
    结果解析与 local 完全同一套断言（_smoke_assert_returncode/_smoke_parse_stdout）。
    """
    # 懒加载 pre_judge_runner（其模块顶层 import 本模块，顶层互引会循环）
    from app.services import script_container
    from app.services.pre_judge_runner import build_run_dir

    script_dir = entry.parent
    entry_rel = entry.name
    run_dir = build_run_dir("smoke", f"smoke_{uuid.uuid4().hex[:12]}")
    try:
        staged = run_dir / "scripts" / "smoke"
        shutil.copytree(script_dir, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        stdin = script_container.rewrite_stdin_paths(build_smoke_stdin(run_dir), str(run_dir))
        paths = script_container.build_container_paths(run_dir, run_dir, staged)
        image = script_container.runner_image()
        spec = script_container.SandboxRunSpec(
            image=image,
            user=script_container.runner_user(),
            workspace_host=paths.run_dir_host,
            workspace_container=paths.run_dir_container,
            workdir_container=paths.cwd_container,
            cwd_host=paths.cwd_host,
            net_profile="none",
            container_name=script_container._new_container_name("smoke"),
            entrypoint=[script_container.CONTAINER_PYTHON, script_container.CONTAINER_ENTRY_WRAPPER,
                        paths.staged_dir_container, entry_rel],
            stdin_payload=json.dumps(stdin, ensure_ascii=False, default=str).encode("utf-8"),
            timeout=limit,
        )
        driver = script_container.get_sandbox_driver()
        try:
            handle = driver.run(spec)
            status = driver.wait(handle, limit)
        except script_container.SandboxDriverError as e:
            # 驱动层失败（CLI 缺失/不可执行、executor 不可达/白名单拒绝等）
            raise ValueError(f"契约冒烟执行失败（{driver.name} 驱动）: {e}")

        stdout_text = _read_smoke_log(Path(paths.cwd_host) / "stdout.log")
        stderr_text = _read_smoke_log(Path(paths.cwd_host) / "stderr.log")
        if status.state == "timeout":
            raise ValueError(
                f"契约冒烟未通过：入口执行超时（>{limit:.0f} 秒）。"
                "请检查脚本是否阻塞等待输入或存在死循环"
            )
        if status.state == "failed":
            raise ValueError(
                "契约冒烟执行失败："
                + (status.error or f"沙箱执行失败（{driver.name} 驱动）")[:2000]
            )
        if status.exit_code != 0:
            tail = _smoke_tail(stdout_text, stderr_text)
            hint = script_container._docker_failure_hint(status.exit_code, stderr_text, image)
            raise ValueError(
                "契约冒烟未通过：入口脚本退出码 "
                f"{status.exit_code}。尾部输出：\n{tail}{hint}"
            )
        return _smoke_parse_stdout(stdout_text, stderr_text)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _read_smoke_log(path: Path) -> str:
    """读驱动归位后的冒烟日志文件（缺失/不可读返回空串，按空输出口径报错）"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _smoke_assert_returncode(returncode: int, stdout_text: str, stderr_text: str) -> None:
    """冒烟退出码断言（local/container 共用）：非 0 抛中文 ValueError 并回显尾部"""
    if returncode != 0:
        raise ValueError(
            "契约冒烟未通过：入口脚本退出码 "
            f"{returncode}。尾部输出：\n{_smoke_tail(stdout_text, stderr_text)}"
        )


def _smoke_parse_stdout(stdout_text: str, stderr_text: str) -> Dict[str, Any]:
    """冒烟标准输出解析（local/container 共用）：最后一行必须是 JSON 对象"""
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(
            "契约冒烟未通过：标准输出为空（未输出契约 JSON）。"
            f"尾部输出：\n{_smoke_tail(stdout_text, stderr_text)}"
        )
    try:
        stdout = json.loads(lines[-1])
    except json.JSONDecodeError as e:
        raise ValueError(
            f"契约冒烟未通过：标准输出最后一个非空行不是合法 JSON（{e.msg}）。"
            f"尾部输出：\n{_smoke_tail(stdout_text, stderr_text)}"
        )
    if not isinstance(stdout, dict):
        raise ValueError(
            "契约冒烟未通过：标准输出最后一行 JSON 必须是对象"
            f"（{type(stdout).__name__}）。尾部输出：\n{_smoke_tail(stdout_text, stderr_text)}"
        )
    return stdout


def _smoke_tail(stdout_text: str, stderr_text: str) -> str:
    """拼接冒烟失败时的尾部输出回显（stdout 优先，附 stderr）"""
    parts = []
    tail = stdout_text.strip()[-_SMOKE_TAIL_CHARS:]
    if tail:
        parts.append(f"[stdout]\n{tail}")
    err_tail = stderr_text.strip()[-_SMOKE_TAIL_CHARS:]
    if err_tail:
        parts.append(f"[stderr]\n{err_tail}")
    return "\n".join(parts) if parts else "（无输出）"


def _parse_declared_requirements(value: Any) -> List[str]:
    """归一化依赖声明：None→[]；数组→字符串列表；字符串→JSON 数组或逗号分隔"""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in text.split(",")]
        if not isinstance(parsed, list):
            raise ValueError("declared_requirements 必须是字符串数组（如 [\"requests>=2.31\"]）")
        value = parsed
    if not isinstance(value, (list, tuple)):
        raise ValueError("declared_requirements 必须是字符串数组（如 [\"requests>=2.31\"]）")
    requirements: List[str] = []
    for item in value:
        text = str(item).strip()
        if not text or text.startswith("#"):
            continue
        requirements.append(text)
    return requirements


def _requirement_base_name(requirement: str) -> str:
    """提取依赖声明的包名（剥离版本约束：requests>=2.31 → requests）"""
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    return match.group(0) if match else requirement.strip()


def check_declared_requirements(requirements: List[str]) -> None:
    """依赖声明逐包校验（E4；W15 起按执行模式分流）

    - local 模式（SCRIPT_EXEC_MODE=local，默认）：在执行 venv（SCRIPT_PYTHON）
      中查 importlib.metadata——缺失任一包即抛 ValueError，附缺失清单与完整
      pip install 命令提示；平台不自动安装（评测 venv 与生产环境一致，需管理
      员评估后安装）。
    - container 模式（SCRIPT_EXEC_MODE=container）：对照 runner 镜像「已装
      集合」静态清单（script_container.RUNNER_IMAGE_PACKAGES，与
      docker/script-runner/packages.txt 同步）纯内存校验，不再子进程探测——
      镜像不可变（--read-only + 离线预载），缺失只能扩展 packages.txt 重建
      镜像（D-3），无运行时 pip install 路径。
    """
    if not requirements:
        return
    from app.services import script_container
    if script_container.container_mode_enabled():
        missing = script_container.check_requirements_against_runner(list(requirements))
        if missing:
            raise ValueError(
                f"以下依赖在 runner 镜像（ai-evo-script-runner）内未安装: {', '.join(missing)}。"
                "容器模式下镜像不可变、无运行时安装路径："
                "请调整脚本改用镜像内已装依赖，或运维扩展 docker/script-runner/packages.txt "
                "重建镜像（镜像离线预载见其 README，属 D-3 运维事项）。"
                f"镜像内已装集合: {', '.join(sorted(script_container.runner_package_base_names()))}"
            )
        return

    names = [_requirement_base_name(item) for item in requirements]
    checker = (
        "import importlib.metadata, json, sys\n"
        "names = json.load(sys.stdin)\n"
        "missing = []\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.metadata.distribution(name)\n"
        "    except importlib.metadata.PackageNotFoundError:\n"
        "        missing.append(name)\n"
        "print(json.dumps(missing))\n"
    )
    from app.services.pre_judge_runner import SCRIPT_PYTHON
    try:
        proc = subprocess.run(
            [SCRIPT_PYTHON, "-c", checker],
            input=json.dumps(names).encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValueError(f"依赖声明校验失败（无法探测执行环境）: {e}")
    if proc.returncode != 0:
        raise ValueError(
            "依赖声明校验失败："
            + proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        )
    try:
        missing = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        missing = names  # 探测输出不可解析时按全部缺失处理，提示安装
    if missing:
        raise ValueError(
            f"以下依赖在评测执行环境中未安装: {', '.join(missing)}。"
            f"请先在执行 venv 安装（平台不自动安装）: "
            f"{SCRIPT_PYTHON} -m pip install {' '.join(missing)}"
        )


def validate_script_chain(
    sources: Dict[str, str],
    entry_path: Path,
    risk_acknowledged: Optional[str] = None,
    declared_requirements: Optional[List[str]] = None,
    smoke_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """上传阻断校验链（阶段E）：compile → 路径规范 → 风险声明 → 依赖校验 → 契约冒烟

    任一环节不通过抛 ValueError（中文，含定位信息与修复提示）。
    通过返回 {"static_check": 风险扫描命中（已声明的提示级）, "smoke_output": 冒烟标准输出}。
    """
    # ① 语法编译（E1/E5）
    compile_script_sources(sources)

    # ② 路径规范扫描（E3）：硬编码平台内部/绝对路径即阻断
    path_hits = scan_hardcoded_paths(sources)
    if path_hits:
        raise ValueError("路径规范扫描未通过（源码禁止硬编码平台内部/绝对路径）:\n" + "\n".join(path_hits))

    # ③ AST 风险扫描（E4）：命中必须携带风险声明文本，否则拒存
    static_check: List[str] = []
    for rel_path, source in sources.items():
        static_check.extend(f"{rel_path} {hit}" for hit in scan_script_risks(source))
    if static_check and not (risk_acknowledged and str(risk_acknowledged).strip()):
        raise ValueError(
            "风险扫描命中以下风险点，必须携带风险声明（risk_acknowledged，说明用途与必要性）后才允许保存:\n"
            + "\n".join(static_check)
        )

    # ④ 依赖声明逐包校验（E4）：缺失即阻断并给出 pip install 命令
    check_declared_requirements(list(declared_requirements or []))

    # ⑤ 契约冒烟（E2）：样例 stdin 经 wrapper 实跑入口，stdout 最后一行必须是 JSON 对象
    smoke_output = run_smoke_test(entry_path, timeout=smoke_timeout)

    return {"static_check": static_check, "smoke_output": smoke_output}


# ---------------- input_mapping 校验（阶段B：hub_file 通道配置） ----------------

def _validate_path_pattern(pattern: Any) -> Optional[str]:
    """校验 hub_file 通道的 path_pattern，返回中文错误信息（None=通过）

    规则：非空字符串；必须相对路径（禁绝对路径/盘符，与 pathlib glob 的
    非相对模式拒绝语义一致）；不含 .. 穿越（与 match_files 运行时防穿越同口径）。
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return "必须是非空字符串（相对解包包根的 glob 模式）"
    pat = pattern.strip()
    if pat.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", pat):
        return f"必须是相对路径（当前: {pattern}）"
    if "\x00" in pat:
        return "含非法空字符"
    parts = [p for p in re.split(r"[\\/]", pat) if p]
    if any(p == ".." for p in parts):
        return f"不允许 .. 穿越（当前: {pattern}）"
    return None


def _validate_input_mapping(
    label: str,
    mapping: Dict[str, Any],
    dataset_field_types: Optional[Dict[str, str]],
) -> List[str]:
    """input_mapping 逐项校验（阶段B：path_pattern / required / hub_file 目标字段）

    仅对配置了 hub_file 通道（value_type=hub_file 或配了 path_pattern）的项做
    阶段B新增校验，其余项保持原有宽松语义（运行时 _resolve_field 自行容错）。
    """
    errors: List[str] = []
    for script_field, field_cfg in mapping.items():
        sub = f"{label}.input_mapping.{script_field}"
        if isinstance(field_cfg, str):
            field_cfg = {"field": field_cfg}  # 容错：直接给字段名
        if not isinstance(field_cfg, dict):
            errors.append(f"{sub} 必须是对象或字段名字符串")
            continue
        field = field_cfg.get("field")
        if not field:
            errors.append(f"{sub} 缺少 field")
            continue

        value_type = str(field_cfg.get("value_type") or "inline").lower()
        pattern = field_cfg.get("path_pattern")
        if value_type != "hub_file" and pattern is None:
            continue  # 非 hub 通道不涉阶段B校验

        if value_type != "hub_file":
            errors.append(
                f"{sub} 配置了 path_pattern 但 value_type={value_type}，"
                "path_pattern 仅支持 hub_file 通道"
            )
        if pattern is not None:
            pattern_error = _validate_path_pattern(pattern)
            if pattern_error:
                errors.append(f"{sub} path_pattern {pattern_error}")
        required = field_cfg.get("required", True)
        if not isinstance(required, bool):
            errors.append(f"{sub} required 必须是布尔值，当前: {required!r}")

        if dataset_field_types is not None:
            ftype = dataset_field_types.get(str(field))
            if ftype != HUB_FIELD_TYPE:
                current = ftype if ftype is not None else "字段不存在"
                errors.append(
                    f"{sub} 的目标字段 {field} 不是数据集 hub_file 类型字段"
                    f"（当前: {current}），hub_file 通道仅支持 hub_file 类型字段"
                )
    return errors


# ---------------- 扩展点标识（设计文档 §九.3：挂载点规范化命名） ----------------
# 已启用：评测流水线步骤1.5，前处理脚本管线（run_pre_scripts，逐条数据、裁判之前）
EXTENSION_POINT_PRE_JUDGE = "evaluation/pre-judge"
# 本期预留：execute_batch 聚合完成后的结果观察（run_result_observers，只读 result）
EXTENSION_POINT_RESULT = "evaluation/result"

# 内置脚本种子元数据（exec_type 已收敛为 script：内置脚本由
# ensure_builtin_scripts_migrated 落库为普通 script 记录，源码从
# app/scripts_builtin/{name}.py 拷贝到 SCRIPT_UPLOAD_BASE/pjs_builtin_{name}/main.py，
# 由子进程 wrapper 统一执行；DEFS 仅作种子源与 builtin:xxx 历史引用的兜底解析）
BUILTIN_SCRIPT_DEFS: Dict[str, Dict[str, Any]] = {
    "allure_vs_cases": {
        "id": "builtin:allure_vs_cases",
        "name": "allure_vs_cases",
        "description": "将 anc-runner 用例文件（cases.json / testpoints.md）的预期结果与 Allure 报告实际结果逐条比对，产出一致率、不一致明细等硬事实供裁判判断可信度",
        "exec_type": "script",
        "entrypoint": "main.py",
        "allowed_tools": None,
        "harness_extra": None,
        "input_fields": {
            "cases_file": {"value_type": "local", "description": "anc-runner cases.json 或 testpoints.md 文件"},
            "allure_results": {"value_type": "hub_file", "description": "allure-results 目录 zip 或单个 *-result.json"},
        },
        "output_schema": {
            "consistency_rate": "number：一致率（0~1）",
            "mismatched_count": "integer：不一致条数（含缺失）",
            "summary_text": "string：人类可读摘要",
            "per_case": "array：逐条比对明细",
            "p0_skipped": "array：P0 用例被 skipped 清单",
        },
        "timeout_seconds": 120,
        "is_active": True,
    },
    "xml_diff": {
        "id": "builtin:xml_diff",
        "name": "xml_diff",
        "description": "两份 XML 的结构化差异比对：节点增删、属性增删改、文本变化，输出差异清单与一致率硬事实",
        "exec_type": "script",
        "entrypoint": "main.py",
        "allowed_tools": None,
        "harness_extra": None,
        "input_fields": {
            "xml_a": {"value_type": "local", "description": "基准 XML 文件"},
            "xml_b": {"value_type": "local", "description": "待比对 XML 文件"},
        },
        "output_schema": {
            "identical": "boolean：两份 XML 是否完全一致",
            "consistency_rate": "number：一致率（1 - 差异事件数/两棵树节点总数）",
            "diff_count": "integer：差异事件总数",
            "diffs": "array：差异清单（node_added/node_removed/attribute_*/text_changed）",
            "total_nodes": "integer：两棵树节点总数",
        },
        "timeout_seconds": 120,
        "is_active": True,
    },
    "log_grep": {
        "id": "builtin:log_grep",
        "name": "log_grep",
        "description": "日志文件按关键字/正则列表扫描：输出每个模式的命中行数、命中占比与样例行（带行号）",
        "exec_type": "script",
        "entrypoint": "main.py",
        "allowed_tools": None,
        "harness_extra": None,
        "input_fields": {
            "log_file": {"value_type": "local", "description": "日志文件（纯文本，逐行扫描）"},
        },
        "output_schema": {
            "total_lines": "integer：扫描总行数",
            "total_hits": "integer：命中行次总和",
            "matched_line_count": "integer：命中过至少一个模式的不重复行数",
            "patterns": "array：[{pattern, type, match_count, hit_rate, sample_lines, sample_truncated}]",
        },
        "timeout_seconds": 120,
        "is_active": True,
    },
    "db_assert": {
        "id": "builtin:db_assert",
        "name": "db_assert",
        "description": "SQL 查询 + 期望断言 JSON 只读比对：按平台注入的只读连接串（stdin.db_dsn，配置 DB_ASSERT_READONLY_DSN）自建 engine 仅执行 SELECT，输出通过率与差异明细",
        "exec_type": "script",
        "entrypoint": "main.py",
        "allowed_tools": None,
        "harness_extra": None,
        "input_fields": {
            "assertions": {"value_type": "inline", "description": "断言清单 JSON：{assertions: [{name, sql, expected}]}，expected 支持 row_count/scalar/first_row/contains_row"},
        },
        "output_schema": {
            "total": "integer：断言总数",
            "passed": "integer：通过条数",
            "failed": "integer：失败条数",
            "consistency_rate": "number：通过率（0~1）",
            "results": "array：[{name, sql, passed, expected, actual, error}]",
        },
        "timeout_seconds": 120,
        "is_active": True,
    },
}


def get_builtin_def(name: str) -> Optional[Dict[str, Any]]:
    """获取内置脚本种子元数据副本（仅兜底用；对外统一走 DB 记录）"""
    definition = BUILTIN_SCRIPT_DEFS.get(name)
    return copy.deepcopy(definition) if definition else None


# ---------------- entrypoint 校验与内置脚本不可变保护（W1-2 / W1-3） ----------------

# 内置脚本记录 id 前缀（ensure_builtin_scripts_migrated 落库约定）：
# 平台种子记录专用；用户经 API 创建的记录 id 一律为服务端生成的 pjs_{uuid}，
# 无法伪造该前缀，作为 is_builtin 身份的判定依据
BUILTIN_SCRIPT_ID_PREFIX = "pjs_builtin_"


def is_builtin_script_id(script_id: str) -> bool:
    """判断脚本记录 id 是否为平台内置脚本记录（不可变保护与 DSN 注入的口径）"""
    return str(script_id or "").startswith(BUILTIN_SCRIPT_ID_PREFIX)


def validate_entrypoint(entrypoint: Any, package_dir: Optional[Path] = None) -> Optional[str]:
    """校验 script 类型 entrypoint 必须为脚本包内相对路径（W1-2）

    规则：
    - None / 空串 → 合法（表示尚未上传脚本文件），返回 None；
    - 必须为字符串：posixpath 归一后不得以 / 开头（绝对路径）、不得含 ..
      穿越、不得含反斜杠或 Windows 盘符，且必须以 .py 结尾；
    - package_dir 存在时，入口文件必须真实存在于包内（不存在即拒绝）；
      包目录尚未创建（先建记录后上传的正常流程）时仅做结构校验，
      由上传链路与运行器（_resolve_script_entry 只认包内相对路径）兜底。

    校验通过返回归一化后的 posix 相对路径（入库统一形态）；
    不合法抛 ValueError（中文，API 层映射 400）。
    """
    if entrypoint is None or str(entrypoint).strip() == "":
        return None
    raw = str(entrypoint).strip()
    if "\\" in raw:
        raise ValueError(f"entrypoint 不允许包含反斜杠路径分隔符: {entrypoint}")
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"entrypoint 不允许 Windows 盘符绝对路径: {entrypoint}")
    normalized = posixpath.normpath(raw)
    if normalized.startswith("/"):
        raise ValueError(f"entrypoint 必须是脚本包内相对路径（不允许绝对路径）: {entrypoint}")
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"entrypoint 不允许 .. 穿越: {entrypoint}")
    if not normalized.endswith(".py"):
        raise ValueError(f"entrypoint 必须以 .py 结尾: {entrypoint}")
    if package_dir is not None and Path(package_dir).is_dir():
        if not (Path(package_dir) / normalized).is_file():
            raise ValueError(
                f"entrypoint 在脚本包内不存在: {entrypoint}（包目录: {package_dir}）"
            )
    return normalized


def _ensure_mutable_script(row: PreJudgeScript, action: str) -> None:
    """内置脚本记录不可变守卫（W1-3）：禁止编辑/删除/覆盖平台种子记录

    内置 db_assert 等脚本由平台注入数据库凭据（stdin.db_dsn），记录一旦可被
    篡改（改代码回显 DSN / 换 entrypoint 指向自建脚本）凭据即被窃取——
    服务层守卫 + API 层超管校验双防线。
    """
    if is_builtin_script_id(row.id):
        raise ValueError(
            f"内置脚本「{row.name}」为平台预置，不可{action}"
            "（内置脚本受保护，防止篡改脚本窃取注入的数据库凭据）"
        )


# ---------------- 目录式存储：布局与旧布局迁移 ----------------

def script_dir_for(script_id: str) -> Path:
    """脚本目录（SCRIPT_UPLOAD_BASE/{script_id}/，绝对或相对取决于 BASE 配置）"""
    return Path(SCRIPT_UPLOAD_BASE) / str(script_id)


def _copy_builtin_script_file(name: str, script_id: str, force: bool = False) -> None:
    """把 app/scripts_builtin/{name}.py 拷贝为脚本目录 main.py

    force=True（平台种子记录 pjs_builtin_{name}）：始终覆盖——内置脚本记录
    不可变（W1-3），源码文件由平台管理，代码更新随部署/迁移即时生效；
    force=False（同名非种子记录，如用户自建的同名脚本）：目标缺失才拷，
    不覆盖用户内容。
    """
    source = _BUILTIN_SOURCE_DIR / f"{name}.py"
    if not source.is_file():
        logger.warning(f"[PreJudgeScript] 内置脚本源码缺失: {source}")
        return
    target_dir = script_dir_for(script_id)
    target = target_dir / DEFAULT_ENTRYPOINT
    if target.is_file() and not force:
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    logger.info(f"[PreJudgeScript] 内置脚本文件就位 {script_id} <- {source}（force={force}）")


def ensure_script_layout(db, row: PreJudgeScript) -> Path:
    """确保脚本记录使用目录式布局（uploads/pre_judge_scripts/{id}/main.py）

    旧布局自动迁移：{id}.py 单文件存在且 {id}/ 目录无 main.py 时，移动为
    {id}/main.py；记录 entrypoint 指向旧文件（绝对或 {id}.py 相对形态）时
    改写为 main.py。返回脚本目录（不要求存在）。
    """
    script_id = str(row.id)
    base = Path(SCRIPT_UPLOAD_BASE)
    legacy_file = base / f"{script_id}.py"
    target_dir = base / script_id
    target_entry = target_dir / DEFAULT_ENTRYPOINT

    if legacy_file.is_file() and not target_entry.is_file():
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_file), str(target_entry))
        logger.info(f"[PreJudgeScript] 旧布局迁移 {legacy_file} -> {target_entry}")

        entrypoint = str(row.entrypoint or "")
        points_to_legacy = False
        if entrypoint:
            if entrypoint == f"{script_id}.py":
                points_to_legacy = True
            else:
                try:
                    points_to_legacy = Path(entrypoint).resolve() == legacy_file.resolve()
                except OSError:
                    points_to_legacy = False
        if points_to_legacy:
            row.entrypoint = DEFAULT_ENTRYPOINT
            try:
                db.commit()
            except Exception as e:  # noqa: BLE001 - 迁移元数据失败不影响文件布局
                db.rollback()
                logger.warning(f"[PreJudgeScript] 迁移回写 entrypoint 失败 {script_id}: {e}")
    return target_dir


def ensure_builtin_scripts_migrated(db, recreate_missing: bool = False) -> Dict[str, str]:
    """内置脚本统一迁移为子进程 script 记录（幂等）

    - recreate_missing=False（缺省，启动语义）：仅当表完全为空时才按
      BUILTIN_SCRIPT_DEFS 全量补种（保障新环境开箱可用）；表非空时不重建
      缺失记录——用户删除即真删，重启/重新部署不再复活。已存在的记录
      仍做 exec_type=builtin 历史形态迁移与文件同步
    - recreate_missing=True（手动迁移入口，如 v3 一键迁移）：缺失的内置
      脚本一律重建，保证 builtin:<name> 历史引用可解析
    - 记录存在且 exec_type=builtin（历史落库形态）：更新为 script、
      entrypoint=main.py，目标文件缺失时补拷
    - 文件同步口径（W1-3）：平台种子记录（pjs_builtin_*）源码始终同步为
      app/scripts_builtin 最新版（内置脚本不可变，代码更新随部署生效）；
      同名非种子记录（用户自建同名脚本）仅目标缺失时补拷，不覆盖用户内容

    返回 {name: script_id}（仅含实际落库/存在的脚本）。单个脚本迁移失败
    仅告警，不阻断其余条目。
    """
    migrated: Dict[str, str] = {}
    table_has_rows = db.query(PreJudgeScript.id).first() is not None
    allow_recreate = bool(recreate_missing) or not table_has_rows
    for name, def_ in BUILTIN_SCRIPT_DEFS.items():
        script_id = f"pjs_builtin_{name}"
        try:
            existing = db.query(PreJudgeScript).filter(PreJudgeScript.name == name).first()
            if existing is None:
                if not allow_recreate:
                    logger.info(
                        f"[PreJudgeScript] 内置脚本 {name} 无记录且脚本表非空，跳过补种（删除即真删）"
                    )
                    continue
                row = PreJudgeScript(
                    id=script_id,
                    name=name,
                    description=def_.get("description"),
                    exec_type="script",
                    entrypoint=DEFAULT_ENTRYPOINT,
                    allowed_tools=def_.get("allowed_tools"),
                    harness_extra=def_.get("harness_extra"),
                    input_fields=def_.get("input_fields"),
                    output_schema=def_.get("output_schema"),
                    timeout_seconds=int(def_.get("timeout_seconds") or 120),
                    is_active=True,
                )
                db.add(row)
                db.flush()
                migrated[name] = row.id
                logger.info(f"[PreJudgeScript] 内置脚本落库为 script 记录 {row.id} name={name}")
            else:
                script_id = existing.id
                migrated[name] = existing.id
                if existing.exec_type == "builtin":
                    existing.exec_type = "script"
                    existing.entrypoint = DEFAULT_ENTRYPOINT
                    logger.info(f"[PreJudgeScript] 内置脚本记录迁移 builtin -> script: {existing.id}")
            # 平台种子记录（pjs_builtin_*）源码始终同步为最新（不可变，用户不可编辑）；
            # 同名非种子记录保持「缺失才补拷」，不覆盖用户内容
            _copy_builtin_script_file(name, script_id, force=(script_id == f"pjs_builtin_{name}"))
        except Exception as e:  # noqa: BLE001 - 单条迁移失败不阻断启动
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"[PreJudgeScript] 内置脚本 {name} 迁移失败（不阻断）: {e}")
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(f"[PreJudgeScript] 内置脚本迁移提交失败（不阻断）: {e}")
    return migrated


# 兼容别名：历史调用点命名（语义已从「补种」升级为「迁移」）
def ensure_builtin_scripts_seeded(db) -> Dict[str, str]:
    """兼容别名：等价 ensure_builtin_scripts_migrated"""
    return ensure_builtin_scripts_migrated(db)


# ---------------- 导入包归一化 ----------------

def _reject_member_path(name: str) -> bool:
    """zip member / 相对路径穿越与非法形态校验"""
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or normalized.startswith("/"):
        return True
    if ".." in pure.parts:
        return True
    if len(normalized) > 1 and normalized[1] == ":":  # Windows 盘符
        return True
    return False


def normalize_package(items: List[Tuple[str, bytes]]) -> Tuple[Dict[str, bytes], str, Optional[str]]:
    """把一次导入请求的原始文件列表归一化为脚本包文件映射

    输入 items: [(相对路径或文件名, 内容), ...]（与请求的 files/relative_paths 对齐）
    输出 (files_map, source_name, error)：
    - 单 .zip：内存解包 + 穿越校验 + 限制校验 + 唯一顶层目录剥离（GitHub 式）
    - 单 .py：不再支持（明确报错，导入仅收 .zip 脚本包与文件夹形态）
    - 多文件：直接按相对路径组装（做穿越校验）
    """
    if not items:
        return {}, "", "导入包为空（未收到任何文件）"

    if len(items) == 1:
        name, content = items[0]
        lower = str(name or "").lower()
        if lower.endswith(".zip"):
            return _normalize_zip(name, content)
        if lower.endswith(".py"):
            return {}, "", "导入仅支持 .zip 脚本包或文件夹（包内必须包含 main.py 与 model.py）"
        return {}, "", f"不支持的文件类型: {name}（单文件导入仅支持 .zip 脚本包）"

    files_map: Dict[str, bytes] = {}
    for raw_path, content in items:
        rel = str(raw_path or "").replace("\\", "/").lstrip("/")
        if not rel:
            return {}, "", "存在空相对路径的文件"
        if _reject_member_path(rel):
            return {}, "", f"非法相对路径（拒绝穿越）: {raw_path}"
        files_map[rel] = content

    # 唯一公共顶层目录剥离（浏览器文件夹直选：webkitRelativePath 首段为所选
    # 文件夹名，与 zip 分支口径一致，保证 main.py 落在包根）
    source_name = "imported_package"
    tops = {name.split("/", 1)[0] for name in files_map}
    if len(tops) == 1:
        root = next(iter(tops))
        stripped = {name[len(root) + 1:]: data
                    for name, data in files_map.items() if "/" in name}
        if stripped:
            files_map = stripped
            source_name = root
    if source_name == "imported_package":
        first_parts = next(iter(files_map)).split("/")
        source_name = (first_parts[0] if len(first_parts) > 1
                       else PurePosixPath(next(iter(files_map))).stem) or "imported_package"
    return files_map, source_name, None


def _normalize_zip(zip_name: str, content: bytes) -> Tuple[Dict[str, bytes], str, Optional[str]]:
    """内存解包 zip：穿越/限制校验 + 唯一顶层目录剥离"""
    source_name = PurePosixPath(str(zip_name).replace("\\", "/")).stem or "imported_zip"
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        return {}, "", f"zip 解析失败: {e}"

    entries: Dict[str, bytes] = {}
    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > MAX_PACKAGE_FILE_COUNT:
            return {}, "", f"包内文件数超限（>{MAX_PACKAGE_FILE_COUNT}）"
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if _reject_member_path(name):
                return {}, "", f"zip 内含非法路径（拒绝穿越）: {info.filename}"
            if info.file_size > MAX_SCRIPT_FILE_SIZE:
                return {}, "", f"包内单文件超限（>{MAX_SCRIPT_FILE_SIZE // (1024 * 1024)}MB）: {name}"
            total += info.file_size
            if total > MAX_PACKAGE_TOTAL_SIZE:
                return {}, "", f"包总量超限（>{MAX_PACKAGE_TOTAL_SIZE // (1024 * 1024)}MB）"
            entries[name] = zf.read(info)

    if not entries:
        return {}, "", "zip 内没有文件"

    # 唯一顶层目录剥离（GitHub 式 zip：所有 member 共享同一顶层目录）
    tops = {name.split("/", 1)[0] for name in entries}
    if len(tops) == 1:
        root = next(iter(tops))
        stripped = {name[len(root) + 1:]: data
                    for name, data in entries.items() if "/" in name}
        if stripped:
            entries = stripped

    return entries, source_name, None


def sanitize_script_name(raw: str) -> str:
    """自动命名的名称合法化：去首尾空白，替换路径/空白危险字符"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(raw or "").strip())
    return name or "imported_script"


def _requirements_has_content(content: bytes) -> bool:
    """requirements.txt 是否声明了实际依赖（忽略注释与空行）"""
    for line in content.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


class PreJudgeScriptService:
    """pre_judge_scripts 表 CRUD 服务"""

    def __init__(self, db):
        self.db = db

    # ---------- 查询 ----------

    def list_scripts(self, include_builtin: bool = True, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """脚本列表（仅 DB 记录；include_builtin 参数废弃保留签名兼容）

        script 类型记录附加 file_count（目录内文件数，无目录为 0）与
        has_requirements（目录内是否有非空 requirements.txt）。
        """
        query = self.db.query(PreJudgeScript)
        if not include_inactive:
            query = query.filter(PreJudgeScript.is_active == True)  # noqa: E712
        rows = query.order_by(PreJudgeScript.created_at.desc()).all()
        return [self._row_to_dict(row) for row in rows]

    def get_script(self, script_id: str) -> Optional[Dict[str, Any]]:
        """按 id 查询；builtin:<name> 前缀为历史引用兼容（按名称查 DB，未命中回落种子元数据）"""
        if script_id.startswith("builtin:"):
            name = script_id[len("builtin:"):]
            row = self.db.query(PreJudgeScript).filter(PreJudgeScript.name == name).first()
            if row is not None:
                self._ensure_layout(row)
                return self._row_to_dict(row)
            return get_builtin_def(name)
        row = self._get_row(script_id)
        if row is None:
            return None
        self._ensure_layout(row)
        return self._row_to_dict(row)

    # ---------- 写操作 ----------

    def create_script(self, payload: Dict[str, Any], created_by: Optional[str] = None) -> Dict[str, Any]:
        """创建脚本；同名已停用记录自动复活（更新并重新启用），活跃同名报错"""
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("脚本名称不能为空")
        exec_type = payload.get("exec_type") or "script"
        if exec_type == "builtin":
            raise ValueError("builtin 执行类型已废除（统一为子进程 script 执行），请使用 script")
        if exec_type not in VALID_EXEC_TYPES:
            raise ValueError(f"不支持的执行类型: {exec_type}，可选值: {', '.join(VALID_EXEC_TYPES)}")

        existing = (
            self.db.query(PreJudgeScript)
            .filter(PreJudgeScript.name == name)
            .first()
        )
        if existing is not None:
            if existing.is_active:
                raise ValueError(f"脚本名称已存在: {name}")
            # 同名已停用：复活语义（更新字段并重新启用），避免唯一约束冲突
            return self.update_script(existing.id, {**payload, "is_active": True})

        # W1-2：script 类型 entrypoint 在创建时即做结构校验（包目录尚未创建，
        # 不校验存在性；后续由上传链路与运行器兜底）
        if exec_type == "script":
            payload = dict(payload)
            payload["entrypoint"] = validate_entrypoint(payload.get("entrypoint"))

        row = PreJudgeScript(
            id=f"pjs_{uuid.uuid4().hex[:12]}",
            name=name,
            description=payload.get("description"),
            exec_type=exec_type,
            entrypoint=payload.get("entrypoint"),
            allowed_tools=payload.get("allowed_tools"),
            harness_extra=payload.get("harness_extra"),
            input_fields=payload.get("input_fields"),
            output_schema=payload.get("output_schema"),
            timeout_seconds=int(payload.get("timeout_seconds") or 120),
            is_active=bool(payload.get("is_active", True)),
            declared_requirements=_parse_declared_requirements(payload.get("declared_requirements")) or None,
            risk_acknowledged=(payload.get("risk_acknowledged") or "").strip() or None,
            created_by=created_by,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        logger.info(f"[PreJudgeScript] 创建脚本 {row.id} name={name} exec_type={exec_type}")
        return self._row_to_dict(row)

    def update_script(self, script_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """更新脚本（部分字段更新；exec_type=builtin 一律拒绝）

        W1-2：script 类型 entrypoint 走 validate_entrypoint 校验（包内相对
        路径，拒绝绝对路径/.. 穿越/不存在的文件）；W1-3：内置脚本记录不可变。
        """
        row = self._get_row(script_id)
        if row is None:
            raise ValueError(f"脚本不存在: {script_id}")
        _ensure_mutable_script(row, "编辑")

        if "name" in payload:
            new_name = (payload.get("name") or "").strip()
            if not new_name:
                raise ValueError("脚本名称不能为空")
            if new_name != row.name:
                conflict = (
                    self.db.query(PreJudgeScript)
                    .filter(PreJudgeScript.name == new_name, PreJudgeScript.id != row.id)
                    .first()
                )
                if conflict is not None:
                    raise ValueError(f"脚本名称已存在: {new_name}")
                row.name = new_name
        if "exec_type" in payload:
            exec_type = payload.get("exec_type")
            if exec_type == "builtin":
                raise ValueError("builtin 执行类型已废除（统一为子进程 script 执行），请使用 script")
            if exec_type not in VALID_EXEC_TYPES:
                raise ValueError(f"不支持的执行类型: {exec_type}，可选值: {', '.join(VALID_EXEC_TYPES)}")
            row.exec_type = exec_type
        if "entrypoint" in payload:
            # agent 类型 entrypoint 是指令文本，不参与路径校验
            if row.exec_type == "script":
                row.entrypoint = validate_entrypoint(
                    payload.get("entrypoint"), script_dir_for(script_id))
            else:
                row.entrypoint = payload.get("entrypoint")
        for field in ("description", "allowed_tools", "harness_extra", "input_fields", "output_schema"):
            if field in payload:
                setattr(row, field, payload.get(field))
        if "declared_requirements" in payload:
            requirements = _parse_declared_requirements(payload.get("declared_requirements"))
            row.declared_requirements = requirements or None
        if "risk_acknowledged" in payload:
            row.risk_acknowledged = (payload.get("risk_acknowledged") or "").strip() or None
        if "timeout_seconds" in payload:
            row.timeout_seconds = int(payload.get("timeout_seconds") or 120)
        if "is_active" in payload:
            row.is_active = bool(payload.get("is_active"))

        self.db.commit()
        self.db.refresh(row)
        return self._row_to_dict(row)

    def delete_script(self, script_id: str) -> bool:
        """物理删除：DB 记录与落盘目录一并清理

        删除前校验评测任务引用（evaluation_targets 中 source=script_output 的
        source_config.script_id，含 builtin:<name> 历史形态）：被未删除任务引用
        时拒绝删除（ValueError 中文提示，引导先解除关联），历史任务编排的引用
        可追溯性由「引用存在即不可删」保证；停用语义由「停用」操作承担，
        删除不再等同停用。W1-3：内置脚本记录不可删除（不可变保护）。
        """
        row = self._get_row(script_id)
        if row is None:
            return False
        _ensure_mutable_script(row, "删除")
        ref_task_names = self._find_referencing_tasks(script_id, row.name)
        if ref_task_names:
            names = "、".join(f"「{name}」" for name in ref_task_names)
            raise ValueError(
                f"脚本「{row.name}」被 {len(ref_task_names)} 个评测任务引用，无法删除；"
                f"请先在对应任务的「待评测数据」中解除关联后再删除：{names}"
            )
        script_dir = script_dir_for(script_id)
        if script_dir.exists():
            try:
                shutil.rmtree(script_dir)
            except OSError as e:
                raise ValueError(f"脚本目录清理失败，请检查文件占用/权限后重试: {e}")
        self.db.delete(row)
        self.db.commit()
        logger.info(f"[PreJudgeScript] 物理删除脚本 {script_id} name={row.name}")
        return True

    def _find_referencing_tasks(self, script_id: str, script_name: str) -> List[str]:
        """引用该脚本的未删除评测任务名列表（已删除任务不可见不可操作，不拦截）

        引用形态两种都算：script_id 精确匹配，及 builtin:<name> 历史前缀
        （未迁移存量任务的引用形态，与任务校验的脚本视图口径一致）。
        """
        from app.models.task_models import EvaluationTask

        ref_ids = {script_id, f"builtin:{script_name}"}
        names: List[str] = []
        rows = (
            self.db.query(EvaluationTask)
            .filter(
                EvaluationTask.is_active == True,  # noqa: E712
                EvaluationTask.evaluation_targets.isnot(None),
            )
            .all()
        )
        for task in rows:
            targets = task.evaluation_targets
            if not isinstance(targets, list):
                continue
            for target in targets:
                config = target.get("source_config") if isinstance(target, dict) else None
                if (
                    isinstance(target, dict)
                    and target.get("source") == "script_output"
                    and isinstance(config, dict)
                    and config.get("script_id") in ref_ids
                ):
                    names.append(task.name)
                    break
        return names

    # ---------- script 类型：上传（单 .py / zip 包，统一目录式布局 + 阻断校验链） ----------

    def save_script_file(
        self,
        script_id: str,
        content: bytes,
        risk_acknowledged: Optional[str] = None,
        declared_requirements: Any = None,
    ) -> Dict[str, Any]:
        """保存上传的单文件 .py 脚本到 uploads/pre_judge_scripts/{script_id}/main.py

        - 仅 exec_type=script 的记录允许上传（agent 类型上传为配置错误）
        - 同 id 重复上传为覆盖语义：目录清空重建（包形态被单文件覆盖时，
          残留兄弟模块一并清理，避免 sys.path 误 import 旧内容）
        - 上传阻断校验链（validate_script_chain）：语法编译 / 路径规范扫描 /
          风险扫描命中必须 risk_acknowledged / 依赖声明逐包校验 / 契约冒烟
          （经 wrapper 实跑冒烟临时副本，与生产执行同路径），任一不通过即
          拒绝（ValueError，中文），不落盘不回写
        - 已声明的风险扫描命中仍随返回值透出（static_check）并记录日志
        """
        row = self._get_row(script_id)
        if row is None:
            raise ValueError(f"脚本不存在: {script_id}")
        _ensure_mutable_script(row, "上传覆盖")
        if row.exec_type != "script":
            raise ValueError(f"仅 exec_type=script 的脚本支持上传文件（当前: {row.exec_type}）")
        if len(content) > MAX_SCRIPT_FILE_SIZE:
            raise ValueError(f"脚本文件过大（>{MAX_SCRIPT_FILE_SIZE // (1024 * 1024)}MB）")
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("脚本文件必须是 UTF-8 编码的 Python 源码")
        requirements = _parse_declared_requirements(declared_requirements)

        base = Path(SCRIPT_UPLOAD_BASE)
        base.mkdir(parents=True, exist_ok=True)
        # 冒烟临时副本（独立临时目录内 main.py，wrapper 按目录执行；跑完即清理）
        with tempfile.TemporaryDirectory(prefix="pjs_smoke_upload_", dir=str(base)) as tmp:
            smoke_copy = Path(tmp) / DEFAULT_ENTRYPOINT
            smoke_copy.write_bytes(content)
            check = validate_script_chain(
                {DEFAULT_ENTRYPOINT: source}, smoke_copy,
                risk_acknowledged=risk_acknowledged,
                declared_requirements=requirements,
            )

        script_dir = script_dir_for(script_id)
        if script_dir.exists():
            shutil.rmtree(script_dir, ignore_errors=True)
        script_dir.mkdir(parents=True, exist_ok=True)
        target = script_dir / DEFAULT_ENTRYPOINT
        target.write_bytes(content)
        # W1-2：落盘后校验入口（与 save_script_package / import_package 同口径）
        row.entrypoint = validate_entrypoint(DEFAULT_ENTRYPOINT, script_dir)
        row.declared_requirements = requirements or None
        row.risk_acknowledged = (risk_acknowledged or "").strip() or None
        self.db.commit()
        self.db.refresh(row)

        static_check = check["static_check"]
        for warning in static_check:
            logger.warning(f"[PreJudgeScript] 脚本 {row.name}({script_id}) 静态检查提示: {warning}")
        logger.info(f"[PreJudgeScript] 脚本文件已保存 {script_id} -> {target}（{len(content)} 字节）")
        return {
            "script_id": script_id,
            "name": row.name,
            "entrypoint": DEFAULT_ENTRYPOINT,
            "package_dir": row.package_dir,
            "file_size": len(content),
            "declared_requirements": row.declared_requirements,
            "risk_acknowledged": row.risk_acknowledged,
            "static_check": static_check,
        }

    def save_script_package(
        self,
        script_id: str,
        zip_content: bytes,
        risk_acknowledged: Optional[str] = None,
        declared_requirements: Any = None,
    ) -> Dict[str, Any]:
        """保存上传的 zip 脚本包：安全解包到 uploads/pre_judge_scripts/{script_id}/（目录式布局）

        - 仅 exec_type=script 的记录允许上传；大小上限 MAX_PACKAGE_ZIP_SIZE（20MB）
        - 安全解包复用 hub 执行包通道（unpack_zip_safely：拒绝绝对路径/盘符/
          .. 穿越成员，超限拒绝，一个文件都不落盘）
        - 包根必须含 main.py（zip 只含唯一一级子目录且其内有 main.py 时，该子目录
          视为包根）；entrypoint 回写为目录内相对路径 main.py（与单文件/导入
          通道一致，运行链路 _resolve_script_entry 按脚本目录解析），包目录
          即 SCRIPT_UPLOAD_BASE/{script_id}/（package_dir 列保留定义但不再写值，
          代码一律以 entrypoint 推导目录）
        - 全部 .py 走 validate_script_chain 阻断校验链（同单文件通道），
          冒烟在暂存目录实跑包根 main.py（wrapper 同生产路径）；不通过即拒绝
          并清理暂存目录（正式目录不受影响）
        - 通过后暂存目录原子改名到正式目录（重复上传为覆盖语义），
          并清理历史单文件 {script_id}.py（旧布局孤儿）
        """
        row = self._get_row(script_id)
        if row is None:
            raise ValueError(f"脚本不存在: {script_id}")
        _ensure_mutable_script(row, "上传覆盖")
        if row.exec_type != "script":
            raise ValueError(f"仅 exec_type=script 的脚本支持上传脚本包（当前: {row.exec_type}）")
        if len(zip_content) > MAX_PACKAGE_ZIP_SIZE:
            raise ValueError(f"脚本包过大（>{MAX_PACKAGE_ZIP_SIZE // (1024 * 1024)}MB）")
        requirements = _parse_declared_requirements(declared_requirements)

        base = Path(SCRIPT_UPLOAD_BASE)
        base.mkdir(parents=True, exist_ok=True)
        upload_tag = uuid.uuid4().hex[:8]
        zip_tmp = base / f"{script_id}__pkg_{upload_tag}.zip"
        staging = (base / f"{script_id}__staging_{upload_tag}").resolve()
        zip_tmp.write_bytes(zip_content)
        try:
            try:
                unpack = unpack_zip_safely(str(zip_tmp), str(staging))
            except HubFileError as e:
                raise ValueError(f"脚本包解包失败: {e}")
            package_root = _locate_package_root(staging)
            entry = package_root / PACKAGE_ENTRY_FILE
            sources = _collect_package_sources(package_root)
            check = validate_script_chain(
                sources, entry,
                risk_acknowledged=risk_acknowledged,
                declared_requirements=requirements,
            )

            final_dir = script_dir_for(script_id).resolve()
            if final_dir.exists():
                shutil.rmtree(final_dir)
            package_root.rename(final_dir)
            # 历史单文件形态被包覆盖时清理孤儿文件
            (base / f"{script_id}.py").unlink(missing_ok=True)
            # W1-2：入口必须真实存在于包内（与 update_script / import_package 同口径）
            validate_entrypoint(DEFAULT_ENTRYPOINT, final_dir)
        finally:
            zip_tmp.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

        row.entrypoint = DEFAULT_ENTRYPOINT
        row.declared_requirements = requirements or None
        row.risk_acknowledged = (risk_acknowledged or "").strip() or None
        self.db.commit()
        self.db.refresh(row)

        static_check = check["static_check"]
        for warning in static_check:
            logger.warning(f"[PreJudgeScript] 脚本 {row.name}({script_id}) 静态检查提示: {warning}")
        logger.info(
            f"[PreJudgeScript] 脚本包已保存 {script_id} -> {final_dir}"
            f"（{unpack['file_count']} 个文件，{unpack['total_size']} 字节）"
        )
        return {
            "script_id": script_id,
            "name": row.name,
            "entrypoint": DEFAULT_ENTRYPOINT,
            "package_dir": row.package_dir,
            "file_count": unpack["file_count"],
            "total_size": unpack["total_size"],
            "declared_requirements": row.declared_requirements,
            "risk_acknowledged": row.risk_acknowledged,
            "static_check": static_check,
        }

    # ---------- 导入导出（契约 JSON，设计 §十二 二期） ----------

    # 契约携带的脚本字段（环境相关字段 id/created_by/created_at/updated_at 不导出；
    # package_dir 是服务器解包路径，环境相关同样不导出，导入端重新上传包即可）
    EXPORT_CONTRACT_FIELDS = (
        "name", "description", "exec_type", "entrypoint", "allowed_tools",
        "harness_extra", "input_fields", "output_schema", "timeout_seconds", "is_active",
        "declared_requirements", "risk_acknowledged",
    )
    EXPORT_CONTRACT_VERSION = "1.0"

    def export_contract(self, script_id: str) -> Optional[Dict[str, Any]]:
        """导出脚本契约 JSON（builtin: 前缀为历史引用兼容，按名称解析到 DB 记录）

        返回 None 表示脚本不存在。所有脚本统一导出契约字段，不再有内置专属标记。
        """
        script = self.get_script(script_id)
        if script is None:
            return None
        contract = {field: script.get(field) for field in self.EXPORT_CONTRACT_FIELDS}
        return {
            "export_version": self.EXPORT_CONTRACT_VERSION,
            "extension_point": EXTENSION_POINT_PRE_JUDGE,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "script": contract,
        }

    def import_script(
        self,
        contract: Dict[str, Any],
        overwrite: bool = False,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """从契约 JSON 的 script 节导入脚本

        - 同名活跃脚本：overwrite=True 更新该记录，否则报错（避免静默覆盖）
        - 同名停用脚本：复活语义（create_script/update_script 已处理）
        - exec_type=builtin 一律拒绝（已废除）
        - 返回 dict 附 imported_as（created/revived/updated）：导入结果语义的
          权威标志，前端据此展示文案（不再依赖列表缓存推导）
        """
        if not isinstance(contract, dict):
            raise ValueError("导入契约的 script 节必须是对象")
        name = (contract.get("name") or "").strip()
        if not name:
            raise ValueError("导入契约缺少脚本名称（script.name）")

        existing = self.db.query(PreJudgeScript).filter(PreJudgeScript.name == name).first()
        payload = {field: contract.get(field) for field in self.EXPORT_CONTRACT_FIELDS}
        payload.pop("is_builtin", None)  # is_builtin 是导出标记，不是可写字段
        payload["timeout_seconds"] = int(payload.get("timeout_seconds") or 120)
        if payload.get("is_active") is None:
            payload["is_active"] = True

        if existing is None:
            script = self.create_script(payload, created_by=created_by)
            script["imported_as"] = "created"
            return script
        if not existing.is_active:
            # 停用同名：复活语义（更新并启用）
            script = self.update_script(existing.id, {**payload, "is_active": True})
            script["imported_as"] = "revived"
            return script
        if not overwrite:
            raise ValueError(f"脚本名称已存在（overwrite=true 可覆盖导入）: {name}")
        script = self.update_script(existing.id, payload)
        script["imported_as"] = "updated"
        return script

    # ---------- 导入导出包（zip/py 多文件形态） ----------

    def import_package(
        self,
        files_map: Dict[str, bytes],
        source_name: str,
        overwrite: bool = False,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """从归一化后的包文件映射导入脚本（一次请求 = 一个包 = 一个脚本）

        契约交换通道（导出包再导入），不做上传阻断校验链冒烟（与
        upload / upload-package 上传通道分工：上传通道走 validate_script_chain）。

        - 根级 manifest.json 存在：复用契约字段建脚本（exec_type 强制 script，
          manifest 声明 builtin 则该项 error）
        - 无 manifest：自动命名（source_name 合法化），exec_type=script、entrypoint=main.py
        - 必须含根级 main.py，否则 error
        - 必须含根级 model.py 且可解析、PreScriptInput/PreScriptOutput 双声明
          齐全（字段可为空），否则 error——导入通道强校验契约完整性，与上传
          通道（不要求 model.py）分工不同
        - 所有 .py 过 scan_script_risks，结果并入 warnings；
          requirements.txt 非空（忽略注释空行）→ warnings 提示预装依赖
        - 同名冲突复用 import_script 语义：overwrite 才覆盖（覆盖=替换目录文件
          +更新元数据），否则该项 error
        """
        result: Dict[str, Any] = {
            "source": source_name, "status": "error", "script_id": None,
            "name": None, "error": None, "warnings": [],
        }

        def _fail(message: str) -> Dict[str, Any]:
            result["error"] = message
            return result

        # 运行噪音/系统文件不入库（__pycache__/.pyc/.DS_Store），与导出、file_count 口径一致
        files_map = {rel: content for rel, content in files_map.items() if not is_noise_file(rel)}
        if not files_map:
            return _fail("脚本包为空")
        if len(files_map) > MAX_PACKAGE_FILE_COUNT:
            return _fail(f"包内文件数超限（>{MAX_PACKAGE_FILE_COUNT}）")
        total = 0
        for rel, content in files_map.items():
            if len(content) > MAX_SCRIPT_FILE_SIZE:
                return _fail(f"包内单文件超限（>{MAX_SCRIPT_FILE_SIZE // (1024 * 1024)}MB）: {rel}")
            total += len(content)
        if total > MAX_PACKAGE_TOTAL_SIZE:
            return _fail(f"包总量超限（>{MAX_PACKAGE_TOTAL_SIZE // (1024 * 1024)}MB）")

        if DEFAULT_ENTRYPOINT not in files_map:
            return _fail(f"脚本包缺少根级 {DEFAULT_ENTRYPOINT}")
        if MODEL_SCHEMA_FILENAME not in files_map:
            return _fail("脚本包缺少根级 model.py（输入/输出契约声明）。"
                         "请在包内补齐 PreScriptInput/PreScriptOutput 声明后重新导入")
        model_err = validate_model_py_source(
            files_map[MODEL_SCHEMA_FILENAME].decode("utf-8", errors="replace"))
        if model_err:
            return _fail(f"model.py 校验失败：{model_err}"
                         "（导入要求 PreScriptInput 与 PreScriptOutput 声明均存在；"
                         "字段可为空；可参考脚本库 folder 模板）")

        manifest_contract: Optional[Dict[str, Any]] = None
        if "manifest.json" in files_map:
            try:
                manifest = json.loads(files_map["manifest.json"].decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return _fail(f"manifest.json 解析失败: {e}")
            if not isinstance(manifest, dict) or not isinstance(manifest.get("script"), dict):
                return _fail("manifest.json 缺少 script 节（或该节不是对象）")
            manifest_contract = dict(manifest["script"])
            if manifest_contract.get("exec_type") == "builtin":
                return _fail("manifest 声明 exec_type=builtin 已废除（统一为子进程 script 执行）")

        warnings: List[str] = []
        for rel, content in sorted(files_map.items()):
            if rel.endswith(".py"):
                warnings.extend(scan_script_risks(content.decode("utf-8", errors="replace")))
        requirements = files_map.get("requirements.txt")
        if requirements is not None and _requirements_has_content(requirements):
            warnings.append("检测到 requirements.txt 声明依赖：平台一期不自动安装，需在服务器 Python 环境预装")
        result["warnings"] = warnings

        if manifest_contract is not None:
            name = (manifest_contract.get("name") or "").strip() or sanitize_script_name(source_name)
            contract = {**manifest_contract, "name": name,
                        "exec_type": "script", "entrypoint": DEFAULT_ENTRYPOINT}
        else:
            name = sanitize_script_name(source_name)
            contract = {"name": name, "exec_type": "script",
                        "entrypoint": DEFAULT_ENTRYPOINT, "is_active": True}

        try:
            script = self.import_script(contract, overwrite=overwrite, created_by=created_by)
        except ValueError as e:
            return _fail(str(e))

        # 落盘目录（覆盖/复活语义统一：清空旧目录重建）
        script_dir = script_dir_for(script["id"])
        if script_dir.exists():
            shutil.rmtree(script_dir, ignore_errors=True)
        script_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in files_map.items():
            target = script_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        # W1-2：入口必须真实存在于包内（与 save_script_package / update_script 同口径）
        validate_entrypoint(DEFAULT_ENTRYPOINT, script_dir)
        logger.info(f"[PreJudgeScript] 导入脚本包 {script['id']} name={script.get('name')} "
                    f"files={len(files_map)} overwrite={overwrite}")

        # model.py 声明同步（恒为准）：包内含 model.py 时覆盖 manifest 契约携带的声明
        parsed = sync_schemas_from_model(script_dir, self._get_row(script["id"]), self.db)

        result.update(status="ok", script_id=script["id"], name=script["name"], error=None,
                      imported_as=script.get("imported_as"),
                      schema_sync={"input_fields": parsed["input_fields"],
                                   "output_schema": parsed["output_schema"]})
        if parsed["warnings"]:
            result["warnings"] = result.get("warnings", []) + list(parsed["warnings"])
        return result

    def export_package(self, script_id: str) -> Tuple[str, bytes]:
        """导出脚本目录为 zip 包（目录全部文件 + 根级 manifest.json 契约）

        返回 (脚本名, zip 字节)；脚本不存在抛 ValueError，非 script 类型或
        目录缺失抛 ValueError（API 层映射 4xx）。
        """
        script = self.get_script(script_id)
        if script is None:
            raise ValueError(f"脚本不存在: {script_id}")
        if script.get("exec_type") != "script":
            raise ValueError(f"仅 exec_type=script 的脚本支持导出包（当前: {script.get('exec_type')}）")
        script_dir = script_dir_for(script["id"])
        if not script_dir.is_dir():
            raise ValueError(f"脚本文件目录不存在: {script_dir}")

        manifest = {
            "export_version": self.EXPORT_CONTRACT_VERSION,
            "extension_point": EXTENSION_POINT_PRE_JUDGE,
            "script": {field: script.get(field) for field in self.EXPORT_CONTRACT_FIELDS},
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(script_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(script_dir).as_posix()
                if is_noise_file(rel):
                    continue
                if rel == "manifest.json":
                    continue  # 根级契约统一由下方 writestr 生成，避免 zip 重复条目
                zf.write(path, rel)
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return script["name"], buf.getvalue()

    # ---------- 脚手架（from-template：代码内模板，单/多文件工程） ----------

    def create_from_template(
        self,
        template: str,
        name: str,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """从脚手架模板生成脚本（记录 + 文件落盘，可直接在线编辑/试跑/导出）"""
        from app.services.script_templates import SCRIPT_TEMPLATES

        if template not in SCRIPT_TEMPLATES:
            raise ValueError(f"未知模板: {template}（可选: {', '.join(sorted(SCRIPT_TEMPLATES))}）")
        name = (name or "").strip()
        if not name:
            raise ValueError("脚本名称不能为空")
        existing = self.db.query(PreJudgeScript).filter(PreJudgeScript.name == name).first()
        if existing is not None and existing.is_active:
            raise ValueError(f"脚本名称已存在: {name}")

        script = self.create_script(
            {"name": name, "description": description,
             "exec_type": "script", "entrypoint": DEFAULT_ENTRYPOINT},
            created_by=created_by,
        )
        script_dir = script_dir_for(script["id"])
        script_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in SCRIPT_TEMPLATES[template].items():
            target = script_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        # folder 模板自带 model.py：创建即解析回填初始声明（编辑窗/编排直接可用）
        parsed = sync_schemas_from_model(script_dir, self._get_row(script["id"]), self.db)
        logger.info(f"[PreJudgeScript] 脚手架生成 {script['id']} template={template} name={name}"
                    f"（声明同步：输入 {len(parsed['input_fields'] or {})} 字段 / "
                    f"输出 {len(parsed['output_schema'] or {})} 键）")
        result = {**script, "files": sorted(SCRIPT_TEMPLATES[template].keys())}
        if parsed["input_fields"] or parsed["output_schema"]:
            refreshed = self.get_script(script["id"])
            result["input_fields"] = refreshed["input_fields"]
            result["output_schema"] = refreshed["output_schema"]
            # 声明同步触发的 updated_at bump 必须透出，否则前端创建后立即
            # 保存文件会命中乐观锁 409
            result["updated_at"] = refreshed["updated_at"]
        return result

    # ---------- 文件树 / 在线编辑 ----------

    def list_script_files(self, script_id: str) -> Dict[str, Any]:
        """列出脚本目录内的白名单文本文件（目录结构由前端构树）"""
        row = self._require_script_type(script_id)
        script_dir = script_dir_for(row.id)
        files: List[Dict[str, Any]] = []
        if script_dir.is_dir():
            for path in sorted(script_dir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(script_dir).as_posix()
                if not is_text_file(rel):
                    continue
                stat = path.stat()
                files.append({
                    "path": rel,
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "is_text": True,
                })
        return {"script_id": row.id, "files": files}

    def read_script_file(self, script_id: str, rel_path: str) -> Dict[str, Any]:
        """读取脚本目录内文本文件内容（不存在 404 / 非文本与穿越 400）"""
        row = self._require_script_type(script_id)
        target = self._resolve_safe_path(row, rel_path)
        if not target.is_file():
            raise PreJudgeFileError(f"文件不存在: {rel_path}", status_code=404)
        if not is_text_file(rel_path):
            raise PreJudgeFileError(f"仅支持读取文本白名单文件: {rel_path}")
        stat = target.stat()
        return {
            "path": str(rel_path),
            "content": target.read_text(encoding="utf-8", errors="replace"),
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "size": stat.st_size,
        }

    def update_script_file(self, script_id: str, rel_path: str, content: str, updated_at: Optional[str]) -> Dict[str, Any]:
        """在线编辑保存（乐观锁：updated_at 与 DB 记录不一致 → 409 冲突）

        仅允许修改已存在的文本文件；成功后 bump 记录 updated_at 并重跑
        风险扫描（warnings 随响应返回）。W1-3：内置脚本文件不可在线编辑。
        """
        row = self._require_script_type(script_id)
        self._require_mutable_file_target(row)
        target = self._resolve_safe_path(row, rel_path)
        if not target.is_file():
            raise PreJudgeFileError(f"文件不存在: {rel_path}（仅允许编辑已存在文件）", status_code=404)
        if not is_text_file(rel_path):
            raise PreJudgeFileError(f"仅支持编辑文本白名单文件: {rel_path}")
        if len(content.encode("utf-8")) > MAX_SCRIPT_FILE_SIZE:
            raise PreJudgeFileError(f"文件内容过大（>{MAX_SCRIPT_FILE_SIZE // (1024 * 1024)}MB）")

        if not self._timestamps_match(row.updated_at, updated_at):
            raise PreJudgeFileError(
                "文件已被他人修改（updated_at 不一致），请刷新后重试", status_code=409)

        target.write_text(content, encoding="utf-8")
        self._touch(row)
        warnings = scan_script_dir_risks(script_dir_for(row.id)) if rel_path.endswith(".py") else []
        # model.py 声明同步（model.py 恒为准）：保存的正是 model.py 时解析
        # PreScriptInput/PreScriptOutput 并回写 input_fields/output_schema；
        # 解析失败不阻断保存，警告并入响应
        schema_sync = None
        if str(rel_path).replace("\\", "/") == MODEL_SCHEMA_FILENAME:
            parsed = sync_schemas_from_model(script_dir_for(row.id), row, self.db)
            schema_sync = {"input_fields": parsed["input_fields"],
                           "output_schema": parsed["output_schema"]}
            warnings = warnings + list(parsed["warnings"])
        for warning in warnings:
            logger.warning(f"[PreJudgeScript] 脚本 {row.name}({row.id}) 在线编辑静态检查提示: {warning}")
        response = {"path": str(rel_path), "updated_at": self._row_to_dict(row)["updated_at"],
                    "warnings": warnings}
        if schema_sync is not None:
            response["schema_sync"] = schema_sync
            if parsed["input_fields"] or parsed["output_schema"]:
                logger.info(f"[PreJudgeScript] 脚本 {row.name}({row.id}) model.py 声明已同步："
                            f"输入 {len(parsed['input_fields'] or {})} 字段 / "
                            f"输出 {len(parsed['output_schema'] or {})} 键")
        return response

    def create_script_file(self, script_id: str, rel_path: str, content: str = "") -> Dict[str, Any]:
        """新建文本文件（默认空内容；已存在 409；path 必须合法且在白名单内）"""
        row = self._require_script_type(script_id)
        self._require_mutable_file_target(row)
        if _reject_member_path(rel_path):
            raise PreJudgeFileError(f"非法文件路径: {rel_path}")
        if not is_text_file(rel_path):
            raise PreJudgeFileError(f"仅支持新建文本白名单文件: {rel_path}")
        if len((content or "").encode("utf-8")) > MAX_SCRIPT_FILE_SIZE:
            raise PreJudgeFileError(f"文件内容过大（>{MAX_SCRIPT_FILE_SIZE // (1024 * 1024)}MB）")

        target = self._resolve_safe_path(row, rel_path)
        if target.exists():
            raise PreJudgeFileError(f"文件已存在: {rel_path}", status_code=409)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
        self._touch(row)
        return {"path": str(rel_path), "created": True,
                "updated_at": self._row_to_dict(row)["updated_at"]}

    def delete_script_file(self, script_id: str, rel_path: str) -> Dict[str, Any]:
        """删除脚本目录内文件（main.py 不可删；内置脚本文件不可删）"""
        row = self._require_script_type(script_id)
        self._require_mutable_file_target(row)
        if str(rel_path).replace("\\", "/") == DEFAULT_ENTRYPOINT:
            raise PreJudgeFileError(f"入口文件 {DEFAULT_ENTRYPOINT} 不可删除")
        target = self._resolve_safe_path(row, rel_path)
        if not target.is_file():
            raise PreJudgeFileError(f"文件不存在: {rel_path}", status_code=404)
        target.unlink()
        self._prune_empty_parents(script_dir_for(row.id), target)
        self._touch(row)
        return {"path": str(rel_path), "deleted": True}

    def rename_script_file(self, script_id: str, rel_path: str, new_path: str) -> Dict[str, Any]:
        """重命名/移动脚本目录内文件（main.py 不可改名；目标存在 409；内置脚本文件不可改名）"""
        row = self._require_script_type(script_id)
        self._require_mutable_file_target(row)
        if str(rel_path).replace("\\", "/") == DEFAULT_ENTRYPOINT:
            raise PreJudgeFileError(f"入口文件 {DEFAULT_ENTRYPOINT} 不可改名")
        if _reject_member_path(new_path):
            raise PreJudgeFileError(f"非法目标路径: {new_path}")
        if not is_text_file(new_path):
            raise PreJudgeFileError(f"仅支持重命名为文本白名单文件: {new_path}")
        source = self._resolve_safe_path(row, rel_path)
        if not source.is_file():
            raise PreJudgeFileError(f"文件不存在: {rel_path}", status_code=404)
        target = self._resolve_safe_path(row, new_path)
        if target.exists():
            raise PreJudgeFileError(f"目标文件已存在: {new_path}", status_code=409)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        self._prune_empty_parents(script_dir_for(row.id), source)
        self._touch(row)
        return {"path": str(rel_path), "new_path": str(new_path),
                "updated_at": self._row_to_dict(row)["updated_at"]}

    # ---------- 任务编排校验（供 evaluation_task_service 后续接入） ----------

    @staticmethod
    def validate_pre_scripts_config(
        pre_scripts: Optional[List[Dict[str, Any]]],
        script_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
        dataset_field_types: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """校验任务级脚本编排配置，返回错误信息列表（空列表=通过）

        script_lookup：可用脚本字典 {script_id: {exec_type, ...}}（调用方传入 DB 视图），
        缺省时仅做结构与枚举校验。
        dataset_field_types：数据集字段类型视图 {field_name: field_type}（阶段B），
        用于校验 hub_file 通道目标字段必须是数据集 hub_file 类型字段；
        缺省 None 时跳过该项校验（向后兼容，调用方未提供数据集视图）。
        """
        errors: List[str] = []
        if pre_scripts is None:
            return errors
        if not isinstance(pre_scripts, list):
            return ["pre_scripts 必须是数组"]
        for index, step in enumerate(pre_scripts):
            label = f"pre_scripts[{index}]"
            if not isinstance(step, dict):
                errors.append(f"{label} 必须是对象")
                continue
            script_id = step.get("script_id")
            if not script_id:
                errors.append(f"{label} 缺少 script_id")
            on_fail = step.get("on_fail") or "continue"
            if on_fail not in ("continue", "abort"):
                errors.append(f"{label} on_fail 仅支持 continue/abort，当前: {on_fail}")
            mapping = step.get("input_mapping")
            if mapping is not None and not isinstance(mapping, dict):
                errors.append(f"{label} input_mapping 必须是对象")
                mapping = None
            if script_id and script_lookup is not None and script_id not in script_lookup:
                errors.append(f"{label} 引用的脚本不存在: {script_id}")
            if isinstance(mapping, dict):
                errors.extend(_validate_input_mapping(label, mapping, dataset_field_types))
        return errors

    # ---------- 内部 ----------

    def _get_row(self, script_id: str) -> Optional[PreJudgeScript]:
        return self.db.query(PreJudgeScript).filter(PreJudgeScript.id == script_id).first()

    def _ensure_layout(self, row: PreJudgeScript) -> Path:
        if row.exec_type != "script":
            return script_dir_for(row.id)
        return ensure_script_layout(self.db, row)

    def _require_script_type(self, script_id: str) -> PreJudgeScript:
        """文件 API 专用：记录存在且 exec_type=script（否则 ValueError → 400/404）"""
        row = self._get_row(script_id)
        if row is None:
            raise PreJudgeFileError(f"脚本不存在: {script_id}", status_code=404)
        if row.exec_type != "script":
            raise PreJudgeFileError(f"仅 exec_type=script 的脚本支持文件管理（当前: {row.exec_type}）")
        self._ensure_layout(row)
        return row

    def _require_mutable_file_target(self, row: PreJudgeScript) -> None:
        """文件级写操作守卫（W1-3）：内置脚本目录文件不可在线增删改

        内置脚本源码由平台管理（迁移/启动时强制同步 app/scripts_builtin），
        在线改动会被下次同步覆盖且可能被用于窃取注入的数据库凭据。
        """
        if is_builtin_script_id(row.id):
            raise PreJudgeFileError(
                f"内置脚本「{row.name}」的文件由平台管理，不可在线新建/编辑/删除/改名（不可变保护）",
                status_code=403,
            )

    def _resolve_safe_path(self, row: PreJudgeScript, rel_path: str) -> Path:
        """把目录内相对路径解析为绝对路径（防穿越：必须落在脚本目录内）"""
        rel = str(rel_path or "").replace("\\", "/")
        if not rel or _reject_member_path(rel):
            raise PreJudgeFileError(f"非法文件路径: {rel_path}", status_code=404)
        script_dir = script_dir_for(row.id).resolve()
        target = (script_dir / rel).resolve()
        if target != script_dir and not target.is_relative_to(script_dir):
            raise PreJudgeFileError(f"非法文件路径（禁止越出脚本目录）: {rel_path}", status_code=404)
        return target

    def _touch(self, row: PreJudgeScript) -> None:
        """文件变化后 bump 记录 updated_at（乐观锁基准）"""
        row.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(row)

    @staticmethod
    def _prune_empty_parents(script_dir: Path, removed: Path) -> None:
        """删除文件后清理其下已空的子目录（不删脚本根目录）"""
        try:
            parent = removed.parent
            root = script_dir.resolve()
            while parent.resolve() != root and parent.is_relative_to(root):
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass

    @staticmethod
    def _timestamps_match(row_value: Optional[datetime], client_value: Optional[str]) -> bool:
        """乐观锁时间戳比对（客户端回传 isoformat，与 DB 记录逐秒一致才放行）"""
        if client_value in (None, ""):
            return row_value is None
        if row_value is None:
            return False
        try:
            client_dt = datetime.fromisoformat(str(client_value))
        except ValueError:
            return False

        def _utc_naive(dt: datetime) -> datetime:
            return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

        return _utc_naive(client_dt) == _utc_naive(row_value)

    @staticmethod
    def _row_to_dict(row: PreJudgeScript) -> Dict[str, Any]:
        data = {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "exec_type": row.exec_type,
            "entrypoint": row.entrypoint,
            "allowed_tools": row.allowed_tools,
            "harness_extra": row.harness_extra,
            "input_fields": row.input_fields,
            "output_schema": row.output_schema,
            "timeout_seconds": row.timeout_seconds if row.timeout_seconds is not None else 120,
            "is_active": bool(row.is_active),
            "package_dir": getattr(row, "package_dir", None),
            "declared_requirements": getattr(row, "declared_requirements", None),
            "risk_acknowledged": getattr(row, "risk_acknowledged", None),
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        if row.exec_type == "script":
            script_dir = script_dir_for(row.id)
            file_count = 0
            has_requirements = False
            if script_dir.is_dir():
                for path in script_dir.rglob("*"):
                    if not path.is_file():
                        continue
                    if is_noise_file(path.relative_to(script_dir).as_posix()):
                        continue
                    file_count += 1
                    if path.relative_to(script_dir).as_posix() == "requirements.txt":
                        try:
                            has_requirements = path.stat().st_size > 0
                        except OSError:
                            has_requirements = False
            data["file_count"] = file_count
            data["has_requirements"] = has_requirements
        return data


def _locate_package_root(staging_dir: Path) -> Path:
    """在解包暂存目录中定位包根（含 main.py 的目录）

    - main.py 在暂存根 → 暂存根即包根
    - 不在根但存在唯一一级子目录且其内有 main.py → 该子目录为包根
      （兼容 zip 打包工具自动包一层目录的常见形态）
    - 其余情况报中文错误
    """
    staging = Path(staging_dir)
    if (staging / PACKAGE_ENTRY_FILE).is_file():
        return staging
    subdirs = [p for p in staging.iterdir() if p.is_dir()] if staging.is_dir() else []
    if len(subdirs) == 1 and (subdirs[0] / PACKAGE_ENTRY_FILE).is_file():
        return subdirs[0]
    raise ValueError(
        "脚本包根目录必须包含 main.py（入口约定：python 直接执行包根 main.py，"
        "包内同目录模块可直接 import）。若上传的是多目录工程/脚手架整包，"
        "请只打包脚本包本体（含 main.py 的目录内容）后重试"
    )


def _collect_package_sources(package_root: Path) -> Dict[str, str]:
    """收集包内全部 .py 源码（相对包根 posix 路径 → 源码文本）"""
    root = Path(package_root)
    sources: Dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        sources[rel] = path.read_text(encoding="utf-8", errors="replace")
    if not sources:
        raise ValueError("脚本包内没有任何 .py 文件")
    return sources


# ---------------- 脚手架模板（阶段E：app/static/script_templates/ zip 下载） ----------------

# 模板 zip 存放目录（随代码发布，只读）
SCRIPT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "static" / "script_templates"

# 模板元数据：name → 说明（列表接口透出，引导用户按契约开发）
# 仅一套脚手架：下载即运行时工作区骨架（scripts/{script_key}/ 脚本包本体 +
# inputs/{字段}/{execution_id}-attempt-1/ 执行包样例 + 索引 + 本地试跑样例）
SCRIPT_TEMPLATE_DEFS: Dict[str, Dict[str, Any]] = {
    "hub_package": {
        "name": "hub_package",
        "title": "预判脚本脚手架（运行时工作区骨架）",
        "description": "下载即脚本运行时的真实目录世界：scripts/{script_key}/ 是脚本包本体"
                       "（main.py+model.py+lib），inputs/{字段}/{execution_id}-attempt-1/ 是"
                       " anc-hub 执行包样例（manifest/acceptance-map/嵌套 zip 等）+ 索引文件，"
                       "附本地试跑样例 stdin；三步上手：本地试跑→开发→打包 scripts 内文件上传；"
                       "文件产物统一写工作区 outputs/ 目录（平台保证存在，范本含完整示范）",
    },
}


def list_script_templates() -> List[Dict[str, Any]]:
    """列出可用脚手架模板（扫描 SCRIPT_TEMPLATES_DIR 下已发布的 {name}.zip）"""
    items: List[Dict[str, Any]] = []
    for name in sorted(SCRIPT_TEMPLATE_DEFS):
        zip_path = SCRIPT_TEMPLATES_DIR / f"{name}.zip"
        if not zip_path.is_file():
            logger.warning(f"[PreJudgeScript] 脚手架模板缺失: {zip_path}")
            continue
        definition = SCRIPT_TEMPLATE_DEFS[name]
        stat = zip_path.stat()
        items.append({
            "name": name,
            "title": definition["title"],
            "description": definition["description"],
            "filename": zip_path.name,
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "download_path": f"/pre-judge-scripts/templates/{name}/download",
        })
    return items


def get_script_template_zip(name: str) -> Path:
    """按名称定位模板 zip 文件（name 仅允许模板注册名，防路径穿越）"""
    definition = SCRIPT_TEMPLATE_DEFS.get(str(name or ""))
    if definition is None:
        raise ValueError(f"脚手架模板不存在: {name}（可用: {', '.join(sorted(SCRIPT_TEMPLATE_DEFS))}）")
    zip_path = SCRIPT_TEMPLATES_DIR / f"{definition['name']}.zip"
    if not zip_path.is_file():
        raise ValueError(f"脚手架模板文件缺失: {zip_path.name}")
    return zip_path
