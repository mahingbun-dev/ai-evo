# 预判脚本脚手架：hub 执行包（运行时工作区骨架）

下载解压后，你看到的就是脚本在评测执行时所处的**真实目录世界**：
`scripts/hub_package/` 是**参考模板（只读）**，演示消费 anc-hub 执行包的标准写法；
你开发的脚本放在 `scripts/{你的脚本名}/`（与模板平级，可并存多个，每个目录是独立
脚本包，分别打包上传）。`inputs/` 是平台注入的输入样例（hub 执行包）。

## 目录骨架（= 运行时工作区形态）

```
<解压根>/                                            ← 单条数据工作区（_run_dir，产物只写这里）
├── scripts/
│   ├── hub_package/                                 ← 参考模板（只读范本，不要修改、不要上传）
│   │   ├── main.py                                  ← 入口示范（平台以等价 python main.py 执行）
│   │   ├── model.py                                 ← 契约声明示范（pydantic）
│   │   ├── lib/helpers.py                           ← 同目录依赖示范
│   │   └── README.md                                ← 契约详情（范本，新脚本照此写自己的）
│   └── {你的脚本名}/                                 ← 你开发的脚本包（独立打包上传）
│       ├── main.py  model.py                        ← 最小结构（写法抄 hub_package）
│       └── lib/ requirements.txt README.md          ← 可选
├── inputs/
│   ├── hub_pkg/                                     ← {script_field}：执行包整目录副本（inputs.root 即此目录）
│   │   └── 62ed5009-2f58-40bd-9e43-8bac7a9eacbf-attempt-1/   ← zip 原始最外层，原样保留
│   │       ├── execution-manifest.json              ← 执行清单（用例结果统计）
│   │       ├── acceptance-map.json  case_list.json  execution-context.json
│   │       ├── handoff.json  report-receipt.json  self-check.json/.md
│   │       ├── clarifications.json/.html  failure-analysis.json  case_list.json.lock
│   │       ├── claude-thread.zip                    ← 嵌套 zip 平台不解压，原样保留
│   │       ├── action-files/（.xlsx 导出）  actions/  scripts/  source/
│   │       ├── reports/（allure_results/*.json、screenshots/*.png、logs/）← 评测脚本最常消费
│   │       └── segments/{uuid}/（pytest 产物 + allure-data/ + runtime/）  ← 分段执行产物
│   └── hub_pkg_index.json                           ← 未配 path_pattern 时的索引文件（inputs.index_path）
├── outputs/                                         ← 脚本产物目录（文件产物只写这里，平台保证存在）
├── sample_stdin.json                                ← 本地试跑样例（平台运行时由 stdin 注入）
├── README.md                                        ← 本文件（骨架说明）
└── AGENTS.md                                        ← AI agent 开发/打包指引（agent 优先读它）
```

> **真实体量提醒**：真实执行包有数百个文件、数十 MB（`reports/allure_results/` 一项
> 就常过百），本样例只保留每类目录 1~3 个小文件示意形状。脚本遍历**永远以 stdin
> 传入的文件清单/索引为准**，不要对着样例猜文件清单。

> 注：真实执行时脚本进程的 cwd 是工作区直下 `{script_id}/`（与 `scripts/` 平级，
> stdout/stderr 日志落那里）；`inputs/` 下的路径由 stdin 传入，源码禁止自行拼接。

## 三步上手

1. **本地试跑**（无需平台，模板即示例）：
   ```bash
   cd <解压根>
   python scripts/hub_package/main.py < sample_stdin.json
   ```
2. **开发你的脚本**：以 `scripts/hub_package/` 为范本——在 `scripts/` 下新建
   `{你的脚本名}/`（可直接拷贝模板起步），写 `main.py` + `model.py`（契约细节见
   `scripts/hub_package/README.md`；`lib/` 同目录 import 写法照抄即可）。**不要
   修改 `hub_package/` 本身**——它是只读模板。
3. **导入平台**：对你自己的 `scripts/{你的脚本名}/` **文件夹整体压缩**为 zip
   （zip 根为一层目录，平台可识别），或把该文件夹**内的文件**直接打包（main.py 在
   包根），走脚本库「上传包」导入；或直接用「新建脚本」在线编辑。`scripts/hub_package/`、
   `inputs/`、`outputs/`、`sample_stdin.json`、`README.md`、`AGENTS.md` 是参考与指引文件，
   不要打进上传包。

> AI agent 开发：优先读 `AGENTS.md`（含硬性规则、本地验证与打包步骤）。

## 脚本运行时看到什么（stdin 契约速览）

- **字段名机制**：`stdin.inputs` 的键 = 你在 `model.py` 声明、任务编排映射的脚本
  字段名（本模板样例叫 `hub_pkg`，可改名）。脚本应**遍历 `stdin.inputs` 消费**，
  不要写死字段名（`main.py` 即示范）。
- `inputs.{字段}`：配了 path_pattern → `{root, files:[{path,rel_path,size,type}]}`；
  未配 → `{root, index_path}`（索引条目 `{path: 相对 root 的 posix 路径, type, size,
  is_nested_zip}`，path 与 files 的 rel_path 同值）。
- **`root` 是解包根（attempt 目录的上一级）**；`rel_path` 一律带
  `{execution_id}-attempt-1/` 前缀，源码不要硬编码 attempt 目录名——按 rel_path
  首段推导、按后缀定位文件（本模板 `lib/helpers.py` 即示范）。
- `path_pattern` 相对解包根书写：写全路径（`{...}-attempt-1/reports/...`）最稳；
  解包根下仅一个顶层目录且 pattern 首段不是它时，平台自动以 `{顶层目录}/{pattern}`
  重试一次（单顶层自动拼接）；解包根混有多个顶层条目时自动拼接不生效。
- **产物只写 `{_run_dir}/outputs/`**（平台保证该目录存在，脚本侧可防御式创建）；
  落盘后在 stdout 的 `files` 键注册绝对路径；输出引用输入文件一律用 `rel_path`；
  stdout 最后一行 JSON：`{status: ok|skip|fail, outputs, message, ...}`。

完整契约、风险/依赖声明、超时说明见 `scripts/hub_package/README.md`。
