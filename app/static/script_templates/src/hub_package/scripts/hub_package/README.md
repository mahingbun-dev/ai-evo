# 预判脚本模板：hub_package（anc-hub 执行包按层级消费）

面向消费 anc-hub 执行包（hub_file 通道）的脚本：演示执行包解包后的真实目录层级、
`inputs` 两种模式的消费方式，以及不硬编码 attempt 目录名定位关键文件的写法。

> **模板定位**：本目录是脚手架自带的只读参考模板——新脚本请在 `scripts/` 下另建
> 目录（可复制本包起步）开发，不要直接修改本包；工作流见工作区根目录 `AGENTS.md`。

## 入口约定

- 包根必须有 `main.py`；平台经统一 wrapper 以等价于 `python main.py` 的方式执行（自动适配 `def run(stdin)` 函数式与 `if __name__ == "__main__"` 传统式两种入口），**包根即 sys.path[0]**，同目录模块/包（如本模板 `lib/`）可直接 `from lib import helpers`。

## 运行时工作区目录结构（每条数据评完即清）

平台执行脚本时会把「脚本包副本 + 输入副本」组装进单条数据工作区，脚本进程的
cwd 是工作区直下 `{script_id}/` 目录（与 `scripts/` 平级，stdout/stderr 日志落那里）。
anc-hub 执行包在该工作区中的真实层级如下
（绝对路径都经 stdin 传入，源码里禁止自行拼接）：

```
uploads/script_runs/{task_id}/{data_id}/            ← 单条数据工作区（_run_dir）
├── scripts/{script_key}/                           ← 脚本包副本（从副本执行）
│   ├── main.py
│   ├── model.py
│   └── lib/...
├── inputs/{字段名}/                                 ← hub 执行包整目录副本（inputs.root 即此目录）
│   └── {execution_id}-attempt-1/                   ← zip 原始最外层，平台原样保留
│       ├── execution-manifest.json                 ← 执行清单（用例结果统计）
│       ├── acceptance-map.json / case_list.json / handoff.json / execution-context.json
│       ├── claude-thread.zip                       ← 嵌套 zip 平台不解压，原样保留
│       ├── reports/allure_results/*-result.json    ← 评测脚本最常消费（附 screenshots/、logs/、allure_html/）
│       ├── segments/{uuid}/（pytest 产物 + allure-data/ + runtime/）  ← 分段执行产物
│       └── action-files/（.xlsx 导出） actions/ scripts/ source/ ...
├── inputs/{字段名}_index.json                       ← 未配 path_pattern 时的索引文件（inputs.index_path）
└── outputs/                                        ← 脚本产物目录（文件产物只写这里，平台保证存在）
```

> **真实体量提醒**：真实执行包有数百个文件、数十 MB（`reports/allure_results/`
> 一项常过百），模板 `inputs/` 样例只保留每类目录 1~3 个小文件示意形状。脚本
> 遍历**永远以 stdin 传入的文件清单/索引为准**，不要对着样例猜文件清单。

脚本进程 cwd：`uploads/script_runs/{task_id}/{data_id}/{script_id}/`（与 `scripts/`
平级，stdout/stderr 日志落盘在该目录）。

要点：

- **`inputs.root` 是解包根，即 attempt 目录的上一级**，不是 attempt 目录本身；
  `rel_path` 一律带 `{execution_id}-attempt-1/` 前缀（如
  `62ed5009-...-attempt-1/reports/allure_results/r1-result.json`）。
- 不同执行包的 `{execution_id}` 不同——**不要在源码里硬编码 attempt 目录名**，
  按本模板方式从 `rel_path` 首段推导、按后缀定位关键文件（`execution-manifest.json` 等）。
- 脚本对输入副本的读写只影响本条数据这一份副本（严格拷贝语义），同任务其它数据条、
  任务级镜像与持久解包区都不受影响；整份工作区随该条评测结束清理。

## path_pattern 写法规则（编排输入映射中配置）

`path_pattern` 相对解包根书写，命中结果即 `inputs.{字段}.files` 数组：

1. **写全路径最稳**：`{execution_id}-attempt-1/reports/allure_results/*.json`
   （通配符语义为 pathlib glob）。
2. **省略 attempt 层通常也能命中**：当解包根下**仅有一个顶层目录**、原始 pattern
   未命中、且 pattern 首段不是该目录名时，平台自动以 `{顶层目录}/{pattern}` 重试一次
   （单顶层自动拼接）。如 `reports/allure_results/*.json` 在上面的目录形态下可命中。
3. **边界**：解包根下混有多个顶层条目（attempt 目录之外还有根级文件/目录）时自动
   拼接不生效，必须写全路径；`..` 穿越一律拒绝。
4. `required`（缺省 true）：命中 0 文件时该脚本按 fail 处理走 on_fail；配
   `required=false` 则以空 `files` 数组继续执行。

## stdin 契约（平台注入的 JSON，需一次性读完）

| 键 | 类型 | 说明 |
|---|---|---|
| `task_id` / `data_id` | string | 任务 ID 与数据项 ID |
| `fields` | object | inline/local 通道字段：**值为平台落盘后的文件绝对路径** |
| `inputs` | object | hub 执行包通道字段，两种模式：配了 `path_pattern` → `{root, files:[{path,rel_path,size,type}]}`（文件对象数组）；未配 → `{root, index_path}`（索引 JSON 文件，含包内全部文件清单，脚本自行遍历） |
| `model_output` | object\|null | 本条数据的模型输出 |
| `session` | object\|null | agent 会话摘要（`session_summary` / `session_stats` / `session_source`） |
| `params` | object | 任务编排中配置的脚本参数 |
| `upstream` | object | 前序脚本结果（键为脚本名或 `output_var`） |
| `_run_dir` | string | 本次运行的工作区根（绝对路径），**脚本自产文件只写其下 `outputs/`** |

## model.py 声明 hub_file 字段

`hub_pkg` 字段声明为 `value_type=hub_file`（字段名可自行修改，`stdin.inputs` 的键
随编排映射走）：编排配置时把数据集的执行包字段（`hub_file_id`）映射到该脚本字段；
执行时平台把执行包镜像进工作区并注入 `stdin.inputs[字段名]`。`path_pattern` /
`required` 在编排的输入映射里配置，不写在 model.py 中。**脚本应遍历
`stdin.inputs` 消费，不要写死字段名**（main.py 即示范）。

## 路径三原则（上传时强制扫描，违反即拒绝）

1. **只用 stdin 传入的路径**：`fields` / `inputs` 携带所有输入路径；源码禁止硬编码
   `/Users/`、`/home/`、`C:\` 等绝对路径与 `uploads/` 等平台内部目录。
2. **文件产物只写 `{_run_dir}/outputs/`**：平台组装工作区时保证该目录存在（脚本
   侧仍建议防御式创建，`lib/helpers.py` 的 `ensure_outputs_dir` 即示范）；写到
   别处的文件平台不保证清理，也不会被发现。
3. **输出引用文件一律 `rel_path`**：相对 `inputs.root` 的路径；不要把宿主机绝对路径写进输出。

## 输出契约

stdout **最后一个非空行**必须是 JSON 对象：

```json
{"status": "ok", "outputs": {...}, "message": "人类可读摘要"}
```

- `status` ∈ `ok` / `skip` / `fail`；`outputs` 为业务产出（裁判可见）；`message` 为摘要。
- **`files` 为产物文件映射**（`{文件名: 绝对路径}`）：写到 `outputs/` 的文件产物
  在这里注册，裁判与下游才能发现；`main.py` 演示了落盘 + 注册的完整写法。
- 平台同时识别 `files` / `summary` / `finish_reason` / `error`；本模板额外回写 `output`/`summary` 与 `outputs`/`message` 同值以兼容旧结果链路。

## 依赖声明与风险声明（上传时校验）

- **依赖声明**：需要第三方包时填 JSON 数组（如 `["requests>=2.31"]`），平台逐包校验评测执行环境，缺失即拒绝并附完整 `pip install` 命令；**平台不自动安装**。
- **风险声明**：源码命中风险扫描（`import subprocess/socket/ctypes/multiprocessing`、`os.system/eval/exec` 等）时，必须填写风险声明文本说明用途，否则拒存。

## 超时

- 单次执行默认 **120 秒**（脚本配置 `timeout_seconds`）；上传时的契约冒烟需在 10 秒内完成。

## 本模板演示

- `lib/helpers.py`：`attempt_top_of`（rel_path 首段推导顶层目录）/ `group_by_attempt`
  （按 attempt 分组）/ `find_by_suffix`（按后缀定位文件，不依赖 attempt 名）/
  `read_manifest_summary`（解析 execution-manifest.json，兼容 list/dict 两种
  case_results 形态）/ `read_index`（root/index_path 模式索引读取）。
- **泛化消费**：main.py 遍历 `stdin.inputs` 全部字段（不写死字段名），每个字段按
  mode（files/index）还原文件清单后按 attempt 分组统计；两种模式输出结构一致。
- 定位 `execution-manifest.json`（跨字段去重）并产出
  `{execution_id, case_total, case_passed, case_failed}` 摘要。
