# AGENTS.md —— ai-evo 评测前处理脚本开发指引（给 AI agent）

你在一个 ai-evo 预判脚本脚手架里工作。**`scripts/hub_package/` 是参考模板（只读
范本），不要修改它**；你的任务是在 `scripts/` 下**新建自己的脚本目录**开发评测前
处理脚本，最终把该目录打包上传回 ai-evo 平台。`scripts/` 下允许并存多个脚本目录
（每个目录是一个独立的脚本包，可分别打包上传）。

开始编码前先读完本文件和 `scripts/hub_package/README.md`（完整契约），并把
`scripts/hub_package/` 的 `main.py` / `model.py` / `lib/helpers.py` 当作范本精读——
新脚本就是照着它的结构与写法写的。

## 推荐工作流

1. **读范本**：`scripts/hub_package/README.md`（契约）→ `main.py`（泛化消费 stdin
   的示范）→ `model.py`（契约声明写法）→ `lib/helpers.py`（与执行包层级打交道的
   安全工具函数）。
2. **新建脚本目录**：`mkdir scripts/{你的脚本名}/`（目录名即 script_key，建议
   小写下划线）。可以直接把 `hub_package/` 拷过去当起点再改，也可以从零写
   `main.py` + `model.py`（最小结构就这两个文件；`lib/`、`requirements.txt` 按需）。
3. **本地试跑**（见下节），通过后。
4. **打包上传**：只打包你自己的脚本目录（见「打包与上传」），`hub_package/`、
   `inputs/`、`sample_stdin.json`、根 `README.md`、本文件都不进上传包。

## 你在哪：目录即运行时世界

```
<当前目录>                                          ← 运行时单条数据工作区（_run_dir）
├── scripts/
│   ├── hub_package/                                ← 参考模板（只读，不要修改、不要上传）
│   │   ├── main.py          入口示范（stdin 读 JSON，stdout 最后一行 JSON）
│   │   ├── model.py         契约声明示范（PreScriptInput/PreScriptOutput，平台自动解析）
│   │   ├── lib/helpers.py   层级感知工具函数示范（sys.path[0]=包目录，from lib import xxx）
│   │   └── README.md        完整契约（stdin 键表/输出契约/路径三原则/path_pattern 规则）
│   └── {你的脚本名}/                                ← 你开发的脚本包（独立打包上传）
│       ├── main.py          必需，入口（def main/run + __main__ 两种入口皆可）
│       ├── model.py         必需，输入/输出契约声明
│       └── lib/ requirements.txt README.md   可选
├── inputs/hub_pkg/{execution_id}-attempt-1/        ← 执行包样例（mock，形状=真实包）
│   ├── execution-manifest.json / acceptance-map.json / case_list.json / handoff.json ...
│   ├── reports/（allure_results/*.json、screenshots/*.png、logs/）← 评测脚本最常消费
│   ├── segments/{uuid}/（pytest 产物 + allure-data/）             ← 分段执行产物
│   └── action-files/（.xlsx）  actions/  scripts/  source/  claude-thread.zip
├── inputs/hub_pkg_index.json                       ← 索引样例（条目 {path 相对,type,size,is_nested_zip}）
├── sample_stdin.json                               ← 本地试跑输入（你的脚本直接可用）
├── README.md            骨架说明（人读）
└── AGENTS.md            本文件（不要改动、不要打包）
```

> **真实体量**：真实执行包数百文件、数十 MB，样例只示意形状。脚本遍历永远以
> stdin 传入的文件清单/索引为准，不要对着样例猜清单。

## 硬性规则（违反会被平台拒绝或产生错误结果）

1. **`scripts/hub_package/` 只读**：它是给所有脚本当范本的模板，改动会被下次
   下载覆盖，也不会被上传。你的产出只放 `scripts/{你的脚本名}/`。
2. **路径三原则**（上传静态扫描强制）：源码禁止硬编码 `/Users/`、`/home/`、`C:\`、
   `uploads/` 等路径字面量；输入路径只用 stdin 传入的值；产物只写 `_run_dir`；
   输出引用文件一律用 rel_path（相对 inputs.root）。
3. **不要硬编码 attempt 目录名**：执行包 root 下第一层是 `{execution_id}-attempt-1/`，
   每个包都不同。rel_path 一律带该前缀；按 rel_path 首段推导顶层、按后缀定位文件
   （`lib/helpers.py` 的 `attempt_top_of`/`find_by_suffix` 即示范）。
4. **不写死输入字段名，两种 inputs 模式都要能工作**：`stdin.inputs` 的键 = 你在
   `model.py` 声明、编排映射的脚本字段名（样例叫 `hub_pkg`，可改名）。**遍历
   `stdin.inputs` 消费全部条目**，每个值都是：
   - 配了 path_pattern：`{root, files:[{path,rel_path,size,type}]}`
   - 未配：`{root, index_path}`（索引条目 `{path: 相对 root 的 posix 路径, type,
     size, is_nested_zip}`，path 与 files 的 rel_path 同值，`Path(root)/path` 即文件位置）
   这条是硬要求的原因：**平台上传冒烟的 stdin 键固定为 `hub_pkg`/`hub_pkg_all`**，
   与你 model.py 里声明的名字无关——只有泛化遍历的脚本才能同时通过冒烟和真实任务。
5. **输出契约**：stdout 最后一个非空行是 JSON，`{status: "ok"|"skip"|"fail",
   outputs: {...}, message: "..."}`；`outputs` 的键须与你 `model.py` 的
   `PreScriptOutput` 声明一致（平台解析声明回填「输出说明」）。
6. **model.py 是契约源**：改输入/输出后必须同步改 `PreScriptInput` /
   `PreScriptOutput`；hub_file 通道字段用 `json_schema_extra={"value_type":
   "hub_file"}` 声明（写法抄 `hub_package/model.py`）。
7. 需要第三方依赖时在脚本包内放 `requirements.txt`（JSON 数组格式亦可，平台校验
   执行环境是否可装，**不自动安装**）；`import subprocess/socket` 等风险点需按平台
   提示填写风险声明。

## 本地验证（改完必做）

```bash
# 在当前目录（解压根）执行；sample_stdin.json 对泛化遍历的脚本直接可用：
python scripts/{你的脚本名}/main.py < sample_stdin.json
```

期望：stdout 最后一行 JSON `status=ok`，`outputs` 与你 model.py 声明一致。
再自测两种 inputs 模式：把 sample_stdin 里某个字段的 `index_path` 键换成 `files`
数组形态（或反之）再跑一遍，都正常才算过。

## 打包与上传（交付步骤）

1. 只打包**你自己的脚本目录**：对 `scripts/{你的脚本名}/` **文件夹整体压缩**为 zip
   （zip 根为一层目录，平台可识别），或把该文件夹**内的文件**直接打包（zip 根即
   main.py）。两种平台都能导入；一次传一个脚本。
2. **禁止**打进上传包：`scripts/hub_package/`（参考模板）、`inputs/`、
   `sample_stdin.json`、根 `README.md`、`AGENTS.md`、`__pycache__`/`.DS_Store`。
3. 上传入口：ai-evo「脚本库」→「上传包」（zip ≤20MB）。平台会跑校验链（compile/
   路径扫描/风险/依赖/10 秒契约冒烟），任一不过会给出中文原因，按提示修复后重传。
4. 导入成功后用脚本库「试跑」再验一次，再进任务编排配置 input_mapping
   （hub_file 字段可选配 path_pattern）。
