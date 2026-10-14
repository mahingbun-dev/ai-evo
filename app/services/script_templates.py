"""评测前处理脚本脚手架模板（from-template 端点的种子内容）

两类模板：
- single：单文件 main.py + README.md（契约速览与开发指引）
- folder：多文件工程（main.py 调 utils.py 纯函数）+ README.md + requirements.txt

模板即生成即落盘（SCRIPT_UPLOAD_BASE/{id}/），可直接在线编辑 / 试跑 / 导出。
契约（标准输入/标准输出协议，与 wrapper pre_judge_entry.py 对齐）在 main.py
顶部 docstring 内完整描述。
"""

SINGLE_MAIN_PY = '''"""评测前处理脚本（单文件模板）

标准输入（stdin，JSON 对象，run(stdin) 收到反序列化后的 dict）：
    task_id / data_id   本次运行的任务与数据项标识
    fields              输入字段映射：{字段名: 本地文件绝对路径}
                        （inline 值由平台写临时文件后传路径）
    params              任务编排传入的脚本参数 dict
    upstream            前序脚本的标准输出 {脚本名: result}
    _run_dir            本次运行的工作区根（绝对路径）——输出文件统一写到
                        该目录下的 outputs/，并把路径注册进 files

标准输出（run 返回的 dict，经 stdout 最后一行 JSON 回传）：
    {
      "status": "ok" | "skip" | "fail",     # 执行结论
      "output": {...},                      # 结构化硬事实（供裁判/下游消费）
      "files": {"文件名": "绝对路径"},        # 产物文件注册（写 _run_dir/outputs/ 下）
      "summary": "人类可读摘要",
      "finish_reason": "completed",
      "error": None,                        # status=fail 时给可读原因
    }
"""
import json
from pathlib import Path


def run(stdin: dict) -> dict:
    params = stdin.get("params") or {}
    fields = stdin.get("fields") or {}
    run_dir = Path(stdin.get("_run_dir") or ".")

    # 示例：解析 params
    upper = bool(params.get("upper", False))

    # 示例：读取 fields 输入文件（平台把 inline/local 值统一落地为本地路径）
    note = ""
    msg_path = fields.get("msg")
    if msg_path and Path(msg_path).is_file():
        note = Path(msg_path).read_text(encoding="utf-8")
    if upper:
        note = note.upper()

    # 示例：文件产物统一写 {_run_dir}/outputs/（平台保证该目录存在，防御式再兜
    # 一次底）并注册进 files，裁判与下游靠 files 发现产物
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    out_file = outputs_dir / "result.json"
    out_file.write_text(json.dumps({"note": note}, ensure_ascii=False), encoding="utf-8")

    return {
        "status": "ok",
        "output": {"note": note, "length": len(note)},
        "files": {"result.json": str(out_file)},
        "summary": f"note 长度 {len(note)}",
        "finish_reason": "completed",
        "error": None,
    }
'''

SINGLE_README_MD = '''# 评测前处理脚本（single 模板）

## 契约速览

- 入口：根级 `main.py` 顶层 `def run(stdin: dict) -> dict`
- 标准输入 `stdin` 键：`task_id` / `data_id` / `fields`（字段名 → 本地文件路径）/
  `inputs`（hub 执行包通道：配 path_pattern → `{root, files:[{path,rel_path,size,type}]}`；
  未配 → `{root, index_path}`）/
  `params` / `upstream`（前序脚本输出）/ `_run_dir`（工作区根，产物写其下 `outputs/`）
- 标准输出（run 返回值）：`{status: ok|skip|fail, output, files, summary,
  finish_reason, error}`
- 产物文件写 `{_run_dir}/outputs/` 下，并把绝对路径注册进 `files`

## 运行时工作区目录结构（每条数据评完即清）

平台执行脚本时会把「脚本包副本 + 输入副本」组装进单条数据工作区（绝对路径都经
stdin 传入，源码里禁止自行拼接）：

```
uploads/script_runs/{task_id}/{data_id}/            ← 单条数据工作区（_run_dir）
├── scripts/{script_key}/                           ← 脚本包副本（从副本执行；cwd 为工作区直下 {script_id}/，stdout/stderr 落 cwd）
├── inputs/{字段名}.{ext}                            ← inline/local 字段副本（fields 值即此路径）
├── inputs/{字段名}/                                 ← hub 执行包整目录副本（inputs.root 即此目录）
│   └── {execution_id}-attempt-1/                   ← zip 原始最外层，平台原样保留
│       ├── execution-manifest.json
│       └── reports/ logs/ ...
├── inputs/{字段名}_index.json                       ← 未配 path_pattern 时的索引文件
└── outputs/                                        ← 脚本产物目录（文件产物只写这里，平台保证存在）
```

要点：`inputs.root` 是解包根（attempt 目录的上一级），`rel_path` 一律带
`{execution_id}-attempt-1/` 前缀；不要在源码里硬编码 attempt 目录名（每个执行包的
execution_id 不同），按 rel_path 首段推导或按后缀定位文件；脚本读写只影响本条
这一份副本（严格拷贝语义），评完随工作区清理。

## 开发指引

1. 在线编辑保存后可用「试跑」给输入样例验证；
2. 需要第三方依赖时新增 `requirements.txt`（平台一期不自动安装，需在服务器
   Python 环境预装）；
3. 静态风险扫描（subprocess/os.system 等）为提示级，不拦截；
4. 返回 `status="fail"` + `error` 描述比抛异常更利于裁判读到失败事实。
'''

FOLDER_MODEL_PY = '''"""输入/输出契约声明（model.py）

平台约定：在线保存本文件后自动解析以下两个模型，同步脚本的
「输入字段」与「输出说明」（以本文件为准，覆盖手工填写值）。

- PreScriptInput：声明 fields 的输入字段
    - 字段名 = 输入字段名；description = 字段说明
    - json_schema_extra={"value_type": "inline|local|oss|hub_file"}
      声明取值通道（缺省 inline）
- PreScriptOutput：声明标准输出 output 的键（说明按「类型：描述」生成）
"""
from pydantic import BaseModel, Field


class PreScriptInput(BaseModel):
    """脚本的输入字段声明（平台解析此模型填充「输入字段」）"""

    msg: str = Field(..., description="待统计的文本内容（inline 内容或本地文件路径）")
    extra_file: str | None = Field(
        None,
        description="可选：附加文本文件路径，存在时追加统计",
        json_schema_extra={"value_type": "local"},
    )


class PreScriptOutput(BaseModel):
    """脚本标准输出 output 的键声明（平台解析此模型填充「输出说明」）"""

    text: str = Field(..., description="归一化后的文本")
    chars: int = Field(..., description="字符数")
    words: int = Field(..., description="单词数")
'''

FOLDER_MAIN_PY = '''"""评测前处理脚本（多文件工程模板）

契约说明见 README.md；输入/输出声明见 model.py——平台解析其中
PreScriptInput/PreScriptOutput 同步脚本的「输入字段」与「输出说明」。
文件夹内兄弟模块 import 与直接 `python main.py` 一致（脚本目录在 sys.path 首位）。
"""
import json
from pathlib import Path

from model import PreScriptInput, PreScriptOutput
from utils import normalize_text, stats_of


def run(stdin: dict) -> dict:
    params = stdin.get("params") or {}
    fields = stdin.get("fields") or {}
    run_dir = Path(stdin.get("_run_dir") or ".")

    # 按 model.py 声明的输入字段读取（平台已把 inline/local 值统一落地为本地路径）
    raw = ""
    msg_path = fields.get("msg")
    if msg_path and Path(msg_path).is_file():
        raw = Path(msg_path).read_text(encoding="utf-8")
    extra_path = fields.get("extra_file")
    if extra_path and Path(extra_path).is_file():
        raw += "\\n" + Path(extra_path).read_text(encoding="utf-8")

    text = normalize_text(raw, strip=params.get("strip", True))
    stats = stats_of(text)

    # 标准输出按 PreScriptOutput 声明构造（声明即使用，防止声明与实现漂移）
    output = PreScriptOutput(text=text, **stats).model_dump()

    # 文件产物统一写 {_run_dir}/outputs/（平台保证该目录存在，防御式再兜一次底）
    # 并注册进 files，裁判与下游靠 files 发现产物
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    out_file = outputs_dir / "result.json"
    out_file.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")

    return {
        "status": "ok",
        "output": output,
        "files": {"result.json": str(out_file)},
        "summary": f"字符数 {stats['chars']}，单词数 {stats['words']}",
        "finish_reason": "completed",
        "error": None,
    }
'''

FOLDER_UTILS_PY = '''"""多文件工程模板：纯函数示例（被 main.py import）"""
import re


def normalize_text(text: str, strip: bool = True) -> str:
    """空白归一：连续空白压成单空格；strip=True 时去首尾"""
    normalized = re.sub(r"\\s+", " ", text or "")
    return normalized.strip() if strip else normalized


def stats_of(text: str) -> dict:
    return {"chars": len(text), "words": len(text.split())}
'''

FOLDER_README_MD = '''# 评测前处理脚本（folder 模板）

## 结构

- `main.py`：入口（顶层 `def run(stdin: dict) -> dict`）
- `model.py`：输入/输出契约声明（pydantic，见下节）
- `utils.py`：兄弟模块示例（main.py 直接 import）
- `requirements.txt`：第三方依赖声明（一期不自动安装）

## model.py 声明约定（pydantic）

- `PreScriptInput`：声明输入字段——字段名即字段名，`Field(description=...)`
  为字段说明，`json_schema_extra={"value_type": "inline|local|oss|hub_file"}`
  声明取值通道（缺省 inline）
- `PreScriptOutput`：声明标准输出 output 的键（说明按「类型：描述」生成）
- 保存 model.py 时平台自动解析并同步脚本的「输入字段/输出说明」，
  **以 model.py 为准**（覆盖手工填写值）；删除 model.py 后恢复手工编辑

## 契约速览

- 标准输入 `stdin` 键：`task_id` / `data_id` / `fields`（字段名 → 本地文件路径）/
  `inputs`（hub 执行包通道：配 path_pattern → `{root, files:[{path,rel_path,size,type}]}`；
  未配 → `{root, index_path}`）/
  `params` / `upstream` / `_run_dir`（产物目录）
- 标准输出：`{status: ok|skip|fail, output, files, summary, finish_reason, error}`
- 产物文件写 `{_run_dir}/outputs/` 下，并把绝对路径注册进 `files`

## 运行时工作区目录结构（每条数据评完即清）

平台执行脚本时会把「脚本包副本 + 输入副本」组装进单条数据工作区（绝对路径都经
stdin 传入，源码里禁止自行拼接）：

```
uploads/script_runs/{task_id}/{data_id}/            ← 单条数据工作区（_run_dir，产物只写这里）
├── scripts/{script_key}/                           ← 脚本包副本（从副本执行；cwd 为工作区直下 {script_id}/，stdout/stderr 落 cwd）
│   └── main.py  model.py  utils.py
├── inputs/{字段名}.{ext}                            ← inline/local 字段副本（fields 值即此路径）
├── inputs/{字段名}/                                 ← hub 执行包整目录副本（inputs.root 即此目录）
│   └── {execution_id}-attempt-1/                   ← zip 原始最外层，平台原样保留
│       ├── execution-manifest.json
│       └── reports/ logs/ ...
└── inputs/{字段名}_index.json                       ← 未配 path_pattern 时的索引文件
```

要点：`inputs.root` 是解包根（attempt 目录的上一级），`rel_path` 一律带
`{execution_id}-attempt-1/` 前缀；不要在源码里硬编码 attempt 目录名（每个执行包的
execution_id 不同），按 rel_path 首段推导或按后缀定位文件；脚本读写只影响本条
这一份副本（严格拷贝语义），评完随工作区清理。

## 开发指引

- 文件夹内模块 import 与直接 `python main.py` 行为一致；
- 在线编辑支持文本文件（.py/.md/.txt/.json/.yaml 等）；
- 可整目录导出 zip（含 manifest.json），在其它环境再导入。
'''

FOLDER_REQUIREMENTS_TXT = '''# 在此声明第三方依赖，平台一期不自动安装：
# 需在服务器 Python 环境预装（如 pip install -r requirements.txt）
# 注：pydantic 属平台运行环境自带，无需在此声明
'''

SCRIPT_TEMPLATES = {
    "single": {
        "main.py": SINGLE_MAIN_PY,
        "README.md": SINGLE_README_MD,
    },
    "folder": {
        "main.py": FOLDER_MAIN_PY,
        "model.py": FOLDER_MODEL_PY,
        "utils.py": FOLDER_UTILS_PY,
        "README.md": FOLDER_README_MD,
        "requirements.txt": FOLDER_REQUIREMENTS_TXT,
    },
}

__all__ = ["SCRIPT_TEMPLATES"]
