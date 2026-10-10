# 预判脚本模板：file_diff（多文件对比）

对 inputs 执行包内两份文本文件做逐行差异对比（unified diff），输出是否一致、差异行数与差异预览。对比对象由 `params.rel_path_a` / `params.rel_path_b` 指定（相对 `inputs.root`，缺省 `a.txt` / `b.txt`）。

## 入口约定

- 包根必须有 `main.py`；平台经统一 wrapper 以等价于 `python main.py` 的方式执行（自动适配 `def run(stdin)` 函数式与 `if __name__ == "__main__"` 传统式两种入口），**包根即 sys.path[0]**，同目录模块/包（如本模板 `lib/`）可直接 `from lib import diff_utils`。

## stdin 契约（平台注入的 JSON，需一次性读完）

| 键 | 类型 | 说明 |
|---|---|---|
| `task_id` / `data_id` | string | 任务 ID 与数据项 ID |
| `fields` | object | inline/local 通道字段：**值为平台落盘后的文件绝对路径** |
| `inputs` | object | hub 执行包通道字段，两种模式：配了 `path_pattern` → `{root, files:[{path,rel_path,size,type}]}`；未配 → `{root, index_path}`（索引 JSON，含包内全部文件清单） |
| `model_output` | object\|null | 本条数据的模型输出 |
| `session` | object\|null | agent 会话摘要 |
| `params` | object | 任务编排中配置的脚本参数（本模板读 `rel_path_a` / `rel_path_b`） |
| `upstream` | object | 前序脚本结果 |
| `_run_dir` | string | 本次运行产物目录，**脚本自产文件只写这里** |

## 路径三原则（上传时强制扫描，违反即拒绝）

1. **只用 stdin 传入的路径**：`fields` / `inputs` 携带所有输入路径；源码禁止硬编码 `/Users/`、`/home/`、`C:\` 等绝对路径与 `uploads/` 等平台内部目录。
2. **产物只写 `_run_dir`**。
3. **输出引用文件一律 `rel_path`**（相对 `inputs.root`），不要输出宿主机绝对路径。

## 输出契约

stdout **最后一个非空行**必须是 JSON 对象：

```json
{"status": "ok", "outputs": {...}, "message": "人类可读摘要"}
```

- 任一对比文件不存在时输出 `status=skip`（不视为失败）。
- 平台同时识别 `files` / `summary` / `finish_reason` / `error`；本模板额外回写 `output` 与 `outputs` 同值以兼容旧结果链路。

## 依赖声明与风险声明（上传时校验）

- 本模板只用标准库（`difflib`），无需依赖声明。自定义脚本需要第三方包时填 JSON 数组（如 `["requests>=2.31"]`），平台逐包校验评测执行环境，缺失即拒绝并附 `pip install` 命令；**平台不自动安装**。
- 源码命中风险扫描（`import subprocess/socket/ctypes/multiprocessing`、`os.system/eval/exec` 等）时必须填写风险声明，否则拒存。

## 超时

- 单次执行默认 **120 秒**；上传时的契约冒烟需在 10 秒内完成。

## 本模板演示

- `lib/diff_utils.py`：`read_text`（UTF-8 容错读取）/ `line_diff_report`（unified diff 统计）。
- outputs：`identical`、`diff_line_count`、`diff_preview`、`line_counts`。
