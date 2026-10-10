# 预判脚本模板：basic（stdin 契约回显）

最简可用脚手架：读 stdin 契约输入，遍历 inputs 文件数组，按输出契约回显。适合作为自定义脚本的起点。

## 入口约定

- 包根必须有 `main.py`；平台经统一 wrapper 以等价于 `python main.py` 的方式执行（自动适配 `def run(stdin)` 函数式与 `if __name__ == "__main__"` 传统式两种入口），**包根即 sys.path[0]**，同目录模块/包（如本模板 `lib/`）可直接 `from lib import helpers`。
- 单文件形态（无第三方依赖时）也可只上传一个 `.py`，两者契约一致。

## stdin 契约（平台注入的 JSON，需一次性读完）

| 键 | 类型 | 说明 |
|---|---|---|
| `task_id` / `data_id` | string | 任务 ID 与数据项 ID |
| `fields` | object | inline/local 通道字段：**值为平台落盘后的文件绝对路径**（inline 内容会写成临时文件） |
| `inputs` | object | hub 执行包通道字段，两种模式：配了 `path_pattern` → `{root, files:[{path,rel_path,size,type}]}`（文件对象数组）；未配 → `{root, index_path}`（索引 JSON 文件，含包内全部文件清单，脚本自行遍历） |
| `model_output` | object\|null | 本条数据的模型输出 |
| `session` | object\|null | agent 会话摘要（`session_summary` / `session_stats` / `session_source`） |
| `params` | object | 任务编排中配置的脚本参数 |
| `upstream` | object | 前序脚本结果（键为脚本名或 `output_var`） |
| `_run_dir` | string | 本次运行的产物目录，**脚本自产文件只写这里** |

## 路径三原则（上传时强制扫描，违反即拒绝）

1. **只用 stdin 传入的路径**：`fields` / `inputs` 携带所有输入路径；源码禁止硬编码 `/Users/`、`/home/`、`C:\` 等绝对路径与 `uploads/` 等平台内部目录。
2. **产物只写 `_run_dir`**：平台随任务统一清理，写到别处的文件不会被发现。
3. **输出引用文件一律 `rel_path`**：相对 `inputs.root` 的路径；不要把宿主机绝对路径写进输出。

## 输出契约

stdout **最后一个非空行**必须是 JSON 对象：

```json
{"status": "ok", "outputs": {...}, "message": "人类可读摘要"}
```

- `status` ∈ `ok` / `skip` / `fail`；`outputs` 为业务产出（裁判可见）；`message` 为摘要。
- 平台同时识别 `files`（产物文件映射）/ `summary` / `finish_reason` / `error`；本模板额外回写 `output`/`summary` 与 `outputs`/`message` 同值以兼容旧结果链路。

## 依赖声明与风险声明（上传时校验）

- **依赖声明**：需要第三方包时填 JSON 数组（如 `["requests>=2.31"]`），平台逐包校验评测执行环境，缺失即拒绝并附完整 `pip install` 命令；**平台不自动安装**。
- **风险声明**：源码命中风险扫描（`import subprocess/socket/ctypes/multiprocessing`、`os.system/eval/exec` 等）时，必须填写风险声明文本说明用途，否则拒存。

## 超时

- 单次执行默认 **120 秒**（脚本配置 `timeout_seconds`）；上传时的契约冒烟需在 10 秒内完成。

## 本模板演示

- `lib/helpers.py`：包内同目录依赖 import 示例（`file_brief` / `read_index` / `read_text_safe`）。
- 遍历 `inputs.hub_pkg.files`（root/files 模式）与 `inputs.hub_pkg_all.index_path`（root/index_path 模式）。
- `fields.note` 读取 inline 字段落盘文件。
