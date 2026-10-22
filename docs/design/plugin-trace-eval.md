# 插件评测 · 会话 Trace 口径（两条线：设计质量 + 历史数据）

> 状态：已实现（2026-10-16，2026-10-21 拆分两条线）。口径由用户确认：**被测插件从 trace 自动识别、
> 结果分「设计质量线」与「历史数据线」两条独立线、深度固定不再选、不可评/未测的行不失败**。
> 入口：新建/编辑任务 → 评测类型「插件评测」→ 评测对象来源「会话 Trace（数据集）」+ 选择数据集。

## 一、这个口径做什么

同一批会话 trace（Claude Code 原始执行记录）交给**市场版 plugin-eval**，结果按两条线展示，
与本地 `score <插件> --thread <jsonl> --depth standard` 的报告结构一致（测评总览 /
Dimension Scores / Skill 明细 / Anti-Patterns / 真实数据测评指标 / 历史执行与生成可信度）：

- **设计质量线**（0-100）：静态层 + 逐 skill 的 4 项 rubric LLM 评审，产徽章、维度分与反模式。
  只与插件目录有关、与线程无关，**同一插件在任务内只评测一次**，其余行复用同一结果
  （逐行只追加 `--history-only` 的历史线）
- **历史数据线**（0-100）= 通过 / (通过 + 失败) × 100，逐行独立
- **判定有效率**（coverage）= 有效判定 / 已抽取检查项
- **关键步骤覆盖率**（key 范围）：被检查项审到的关键步骤实例占比
- 逐检查项明细（阶段 / 方向 / 检查项 / 判定 / 理由 / 预期）与报告「Skill 明细」表
- 被引用证据原文（evidence_id → 来源 + 500 字摘录），可在页面离线复核

两条线**相互独立**：真实数据分不并入综合分、徽章与排名（plugin-eval 明确定义）。
评的对象也不同——设计质量评「当前插件代码」，历史数据评「历史那次执行」。

## 二、命令行契约（ai-evo → 市场 plugin-eval）

```
# 首次（该插件本任务内第一次出现）：设计质量 + 历史线
python plugin_eval_market_entry.py <plugin-eval 包目录> score <市场内被测插件目录> \
  --thread <原始会话 jsonl> [--artifacts <执行包镜像目录>]... \
  --depth standard --history-phase all --history-scope key \
  --output json --concurrency 4

# 同一插件的后续行：只跑历史线，设计质量复用首次结果
... score <同一插件目录> --thread <原始会话 jsonl> ... --history-only ...
```

- **首次不带 `--history-only`**，后续行带 `--history-only`（`composite` 为 null）：
  设计质量线按「插件名 + 版本 + 市场路径 + 判定方式」缓存，同一插件只跑一次静态层与 judge，
  避免 N 行重复评同一份插件代码
- 校验点仍是 `real_data`——规则选不中任务时真实数据分为未测，不影响设计质量线；
  首次评测另校验 `composite` 字段存在
- `--depth standard`：静态层 + 逐 skill 的 rubric 评审（**任务表单不再暴露深度选择**，
  trace 的深度由服务端固定；引擎已知目标类型为插件也不受影响——插件级 fan-out 到 skill 评审
  是 plugin-eval 自身能力）
- `--history-phase all --history-scope key`：执行 + 生成两阶段、按关键步骤实例逐个抽取
  （模型请求数随实例数增长，而不是随线程长度）
- 判定方式（任务级 `trace_judgement`）：`model`（默认）逐实例抽取 + 逐检查项模型判定；
  `offline` 追加 `--history-no-llm` **且静态深度退到 `quick`**，全程零模型调用。
  实测同一线程：模型判定 **>60 分钟**（190+ 次模型调用），离线判定 **79 秒**——
  批量数据集建议先用离线档跑通，再对关键行用模型档
- `--artifacts`：执行包里**除会话线程之外**的文件镜像（硬链接，随行清理）。不直接传整包——
  线程已由 `--thread` 给出，而 plugin-eval 会对产物目录递归解析（`.jsonl` 命中多条 extractor
  规则），整包传入会让转录被当成「本轮产物」重复解析：拖慢，且混淆证据角色（转录不是产物）。
  会话包目录里只有线程时就没有产物（该情形下产物本就为空）
- 凭据：静态评审与历史抽取/判定都经 Claude Code 通道，未配置全局配置时直接报错
  （离线档不查这条）

## 三、数据前提（硬约束）

1. **必须有原始会话 jsonl**。数据集里 `session_messages` 是尺寸控制后的会话记录
   （丢 message uuid，超预算时丢弃首尾之外的消息），**不能替代原始线程**。
   实测同一线程：原始 jsonl 离线 83.33 分 / 用截断记录重建 100.0 分——**丢掉的失败证据会让分数系统性虚高**，
   故不实现「重建线程」这条降级路径。
2. **行里要有 anc-hub 文件字段**（`type=hub_file`）。执行时 `resolve_hub_file_fields` 把 file_id
   镜像成任务内目录，线程与产物都从该目录取：
   - 嵌套 `claude[-_]thread*.zip`（执行包，包内 jsonl 即会话转录）优先
   - 其次裸 `*.jsonl`（会话包）
   - 都没有 → 该行「未测：没有原始会话文件」
3. **worker 不发起在线补拉**：hub 通道设计是导入期物化、评测期只读镜像（评测任务没有用户 4A 令牌）。
   因此数据集要由「从 anc-hub 导入」创建（会话数据 / 执行包数据），行里才带原始包。

## 四、被测插件怎么定

1. 候选集合 = 市场 plugin-eval 的 `rules/history/*.yaml` 文件名（不写死业务线清单，规则新增自动纳入）
2. 扫线程里 user 侧文本的调用形态（与 plugin-eval `invoked_skill` 同形）：
   - `<command-name>/anc-tms:run</command-name>`
   - 行首 `/anc-tms:run ...`
3. 多候选取调用次数最多者，并列取最早出现者；全部候选计数写进行结果
4. 命中后在市场清单里按条目名（必要时按各插件 manifest 的 name 兜底）定位插件目录，
   并把该市场的 commit 作为版本锚点
5. 识别不构成判定：线程是否真的属于该插件由 plugin-eval 的 task_selectors 决定

**规则覆盖决定能评多少**：市场现有 7 条业务线规则（anc-data / anc-lcrm / anc-oms /
anc-saas-b2b-erp / anc-srm / anc-tms / anc-wms）。调用 `/anc-runner:*`、`/case-writing`
这类**没有历史规则**的插件会被判「未测」——扩面属市场 plugin-eval 侧补规则。

## 五、行结果字段

| 字段 | 含义 |
|---|---|
| `evaluation_mode` | `trace`（口径标记；行全失败时聚合仍按 trace 渲染） |
| `target_name` | 识别出的被测插件名（未识别为空） |
| `average_score` | **设计质量线综合分**（静态层 + LLM 评审；与其余来源同义，未出为 null） |
| `design_reused` | 本行设计质量是否复用任务内首次评测（同一插件只评一次） |
| `badge` / `dimension_scores` / `anti_patterns` | 设计质量线的徽章、维度分（未测维度剔除）、反模式（逐层汇总到行顶层） |
| `real_data` | **历史数据线**：`{metric, status, score, coverage, counts, extraction_complete, limitations, behavior_coverage, phases}` |
| `checks` / `checks_total` | 逐检查项明细（上限 200 条，`actual` 限长、证据引用限量）；每条带 `dimension_label`、`phase`、`phase_label` |
| `trace` | 线程与规则锚点：`thread_file/thread_origin/thread_source/thread_sha256/thread_id`、`detected{plugin,counts,candidates}`、`plugin_source/plugin_path/plugin_version`、`rule{file,version,sha256}`、`phases[]`（逐阶段一节，含 `detected`/`unmeasured_reason`/`evidence`）、`history`（执行阶段，等于 `phases[0]`）、`package_dir`、`artifact_files`、`unavailable_reason` |
| `plugin_eval` | `{mode:"trace", depth(实际静态深度), composite, layers, skills[], model_usage, duration_ms, warnings, evaluator, design{evaluated,reused,model_usage,duration_ms}, raw(审计摘要) }` |

**两套分数各占一栏**：`average_score`（=综合分）与 `real_data.score`（真实数据分）都落在行里，
互不并入。`plugin_eval.skills[]` 是报告「Skill 明细」表：`{name, score, badge, confidence_label,
measured_dimensions, dimension_count, anti_pattern_count, layers[{layer,score}]}`。

**逐阶段块**（`trace.phases[]`，对应 plugin-eval `report.md` 的「历史执行/生成可信度」一节）：
`{metric, label, plugin, thread_id, status, score, coverage, counts, dimensions[], behavior_coverage,
source, rule, limitations, model_usage, check_count, key_steps[], key_step_total,
failed_checks[], failed_total}`。其中

- `dimensions[]` = 报告的「分项」表：`{key, label, score, coverage, measured, applicable, counts}`，
  `measured = passed + failed`、`applicable = measured + insufficient`（与 reporter 同口径）；
- `label` 是**方向名**，逐字对齐 plugin-eval `src/plugin_eval/reporter.py` 的 `_DIMENSION_NAMES`
  （`reporting_accuracy→报告准确性`、`procedure_adherence→流程遵循`、`error_handling→异常处理可信度`；
  生成阶段另有 `来源一致性/需求覆盖/用例质量/生成流程与报告`）。规则文件里的 `dimension` 是抽取期的内部键，
  平台只在 `checks[].dimension` 保留原键，展示与导出统一用 `dimension_label`；
- `key_steps[].call_check` 即报告的「本步调用确认」（`passed/failed/insufficient`，该步无 expected_calls 时为 null）；
- `failed_checks[]` 单独留一份（上限 100 条），不随 `checks` 的 200 条上限被截掉——失败结论要能逐条复核。

**未测行**：`success=true` + `real_data.status="unmeasured"` + `trace.unavailable_reason`；
未跑评测动作的行 `average_score` 也为 null（改静态校验等前置失败）。四种原因：① 行未提供 hub
执行包目录 ② 执行包里没有原始会话文件 ③ 线程里没有可评插件的技能调用
④ 市场未收录该插件。只有评测动作本身失败才记失败行。
另有第五种「半未测」：评测动作跑通但规则选不中该插件的任务 → **综合分照出、真实数据分为未测**，
原因写在 `trace.unavailable_reason`（「未在本次线程中定位到该插件的任务」）。

## 六、聚合 / 导出 / 前端

- 聚合（`aggregate_plugin_results`）：识别到 trace 行即出 `mode="trace"`，按两条线汇总 ——
  - `design_quality{evaluated_count, plugins[]}`（设计质量线：**按插件一条**，含 `score/badge/
    confidence_label/anti_pattern_penalty/dimensions/layers/skills/depth/model_usage/duration_ms/
    covered_rows/reused_rows/anti_pattern_count`）；同一插件无论覆盖多少行只算一个样本；
  - `composite_summary{average_score, measured_count, unmeasured_count, unit}`（`unit=plugin`；旧记录
    没有 `design` 标记时 `unit=row`，历史结果保持不变）；
  - `real_data_summary{average_score, score_label, decidable_count, applicable_count, conservative_score,
    measured_count, unmeasured_count, status_summary, average_coverage, average_behavior_coverage,
    behavior_scope, check_summary, phase_summary, direction_summary}`；
  `direction_summary[]` 是报告「分项」表的跨行口径（`{key,label,phase,phase_metric,phase_status,
  average_score,average_coverage,measured,applicable}`，逐行求和/求均）；**未测阶段不参与方向分项**
  （未测阶段没有检查项，参与会出整行 0/0 的假分项）；`phase_summary[]` 保留两阶段（含
  `status/detected/average_score/average_coverage/decidable/applicable/measured_rows/row_count`），
  未测阶段页面显示未测而不是 0；`badge_summary` 只统计真有设计质量分的插件（旧 trace 行不参与）；
  `items[]` 增 `real_data / trace / checks / unmeasured_reason / composite_score / real_data_score /
  skills / design_reused / design`。
  非 trace 任务的结构与取值完全不变（`composite_summary`/`design_quality`/`real_data_summary` 恒为 None，
  `items[]` 不加 trace 专属键）。
- 「旧口径行」的识别不靠猜：改动前落库的 trace 行**没有** `composite_score / real_data_score` 两个键，
  那时行分装的是真实数据分；前端与导出据此把综合分显示为「未出/未测」，导出另把 `复合分` 列清空并标
  `得分口径=历史口径（仅真实数据分）`。
- 导出：excel/csv 加列「得分口径 / 综合分 / 得分标签 / 真实数据分 / 可判定·适用 / 判定有效率 /
  关键步骤覆盖率（口径为 all 时为行为覆盖率）/ 通过·失败·证据不足 / 设计质量复用 / 阶段得分 / 未测阶段 /
  失败项（逐条方向+规则+对象+判定依据）/ 识别插件 / 线程文件 / 线程SHA256 /
  历史规则 / 未测原因」，并逐方向各出一列（报告准确性 / 流程遵循 / 异常处理可信度 / 来源一致性 /
  需求覆盖 / 用例质量 / 生成流程与报告，取值形如 `94.44（有效率 40.0%，18/45）`），列固定以便跨行对齐；
  未测阶段的方向列留空（不写 0/0）；非 trace 行留空字符串（列在、值为空）。
- 前端（结果页 `result.mode === 'trace'` 时的口径）：
  - **两条线分开显示**：「设计质量线（每个插件只评测一次）」含平均综合分卡 + 按插件明细表
    （插件 / 设计质量分 / 徽章 / 置信标签 / 覆盖行数 / 复用行数 / 反模式条数，展开可看维度、Skill 明细、
    模型用量与评测耗时）；「历史数据线（逐行线程）」含真实数据得分（标签随 `score_label` 变化）/
    判定有效率 / 覆盖率（列名随 `behavior_scope` 切换）三张卡；
  - **历史数据线 · 阶段汇总**表：逐阶段 状态 / 平均可信度 / 平均判定有效率 / 可判定·适用 / 出分行数；
    未测阶段显示 `—` 并给出原因（`unmeasured_reason`）；
  - 徽章分布、维度平均分、反模式汇总三块在 trace 模式下同样渲染（旧执行记录没有这些数据时自动隐藏）；
  - 「方向分项（逐行求和，按阶段）」卡（含阶段列，未测阶段不出现）；「判定分布」+「评分状态与限制」卡；
  - 明细表列=识别插件 / 设计质量分（任务级，标注首次/复用）/ 真实数据分（可判定·适用）/ 徽章 / 深度 /
    判定有效率 / 关键步骤覆盖率 / 判定（通过·失败·不足）/ 状态 / 未测原因；旧口径行在真实数据分下标
    「此列即该行的得分，无综合分」；
  - 展开行按 plugin-eval 报告的结构逐节展开：线程与规则锚点 → 设计质量综合分（综合分/徽章/置信标签/
    反模式惩罚/深度/Skill 数）→ Skill 明细表 → 维度评分 → 各层得分 → 真实数据测评指标（得分标签、测评范围、
    口径说明、抽取完整性）→ 逐阶段「历史执行/生成可信度」（项目表：插件/状态/可信度/判定有效率/关键步骤覆盖率/
    可判定·适用/通过·失败·证据不足·不适用/独立任务数/会话/规则文件·版本·SHA256）→ 分项 → 关键步骤 →
    未被检查的关键步骤 → 失败项 → 限制与未验证项 → 模型用量 → 逐检查项明细
    （检查/对象·方向·检查项·判定·预期·实际·判定依据·证据）。
  - 旧行（无 `trace.phases`）由前端用 `trace.history` 兜底渲染同一套结构。
  非 trace 渲染保持不变。

### 得分的呈现口径（得分必须与样本量同屏）

plugin-eval 的真实数据得分分母只有「通过 + 失败」，证据不足的检查**不进分母**，也不等于通过；
`doc/metrics.md` 因此明确要求「必须与判定有效率一起看：有效率低时，得分只代表少量样本」。
只把分数当头条、把有效率放到角落，就会出现「100 分 + 有效率 36%」这种必须靠翻页才能解释的结论。故：

- **标签随状态变**：任一行 `status != complete` 或 `extraction_complete=false` 时，标题从
  「真实数据得分」改为「部分样本得分」，并挂「评审不完整，不代表整条线程」（与 reporter 的
  `partial` 文案一致）。
- **样本量与分数同屏**：分数下方直接写「得分只含已判定的 `decidable_count` / `applicable_count` 项检查」，
  明细表分数旁标「（可判定/适用）」，导出加「得分标签 / 可判定·适用」两列。
- **证据不足的口径写在判定分布卡里**：明确「不计分、也不等于通过（缺证据，需补产物或人工复核）」，
  不适用项不进任何分母。
- **给一个下界作敏感性对照**：`conservative_score = 通过 ÷ (通过 + 失败 + 证据不足) × 100`。
  这是把证据不足一律按违规计的对照值，**不是 plugin-eval 指标**（口径仍是 通过 ÷ (通过 + 失败)），
  用途是让「9 项可判定的 100 分」不会被读成「25 项都合规」——本任务实测下界 36.0。

## 七、配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLUGIN_EVAL_CONCURRENCY` | 20 | 模型调用并发（`--concurrency`）。上限由 plugin-eval 的 `EvalConfig(ge=1, le=20)` 决定，超范围会被夹到 `[1,20]`（传超会被 pydantic 拒掉、整行失败）。**只作用于一行内部**，行与行仍串行；每 1 路并发 = 一个 `claude` 子进程，调高需同时看网关限流与部署机内存 |
| `PLUGIN_EVAL_TRACE_TIMEOUT_SECONDS` | 7200 | 单次 trace 评测超时（单条 26MB 线程的执行阶段模型判定实测 >60 分钟，再叠加生成阶段与逐 skill 的 rubric 评审，3600/5400s 都会把一小时以上的工作判失败） |
| `PLUGIN_EVAL_TIMEOUT_SECONDS` | 120 / 1800 | quick / 其余（静态口径）超时 |
| `PLUGIN_EVAL_SOURCE` | `./public/plugin-eval` | 评测动作在市场内的路径 |

任务级覆盖：`plugin_config.trace_concurrency`（仅 trace 来源，1~20，留空走环境变量）——创建/更新时校验，
透传到 `--concurrency`，用于单条任务临时调并发而不动部署配置。
**深度不是配置项**：trace 的静态深度由服务端按判定方式固定（model→`standard`、offline→`quick`），
任务表单不暴露深度选择（登记值写 `standard`，实际值以行结果 `plugin_eval.depth` 为准）。

**比率口径**：`coverage`（判定有效率）与 `behavior_coverage.rate`（关键步骤覆盖率）在行映射时
统一保留两位小数（plugin-eval 出长浮点，明细表与导出直接展示该值）。

**成本与时长（实测）**：一条 26MB / 4642 行线程的执行阶段产生 91 个关键步骤实例，模型判定累计 190+ 次
模型调用，**单行超过 1 小时**；离线判定 79 秒。合并口径再叠加生成阶段与逐 skill 的 rubric 评审
（anc-tms 23 个 skill → 近百次评审调用）。20 行数据集的模型判定不可能在一次执行里跑完，
依赖既有的行级 write-through 与断点续跑分轮跑完；长任务还会拖住部署（worker 优雅退出等它结束）。

**长行的执行期保障**：单行跑着的几十分钟里，运行器以 Popen 轮询并每 60 秒回调一次 `on_tick`——
任务层据此刷新执行心跳（收尸 reaper 阈值 15 分钟，否则一次部署重启就会把仍在跑的任务标成 failed），
同时按行写任务日志（「[n/N] 开始评测」「评测动作执行中（已 x 分钟）」
「完成：被测插件 X，设计质量分 A（首次评测/复用首次评测），真实数据分 B，判定通过/失败/证据不足 …」）。

## 八、不做

- 不用 `session_messages` 重建线程（实测分数虚高）
- 不在 worker 侧在线补拉原始会话（无 4A 令牌；hub 通道本就是导入期物化）
- 不补无规则插件（如 anc-runner）的历史规则（属市场 plugin-eval 侧）
- 不做 `--history-scope all`、`review/certify/compare` 子命令

## 九、验证记录（2026-10-16，本地）

- 线程：anc-hub 会话包 `21a46d7d-…jsonl__51272da5.zip`（26MB / 4642 行），识别为 `anc-tms`（2 次 `/anc-tms:run`）
- 离线（`--history-no-llm`）：任务定位成功（`phase_detected=true`、`workflows=["quick"]`），
  91 个关键步骤实例，8 项确定性检查 → 83.33 分 / 判定有效率 75%
- 对照实验：截断 `session_messages` 重建线程 → 关键步骤实例 49、**100.0 分**（漏掉失败证据）
- 单测：`tests/unit/test_plugin_eval_trace.py`（线程发现 / 候选规则 / 插件识别 / 产物镜像 /
  argv 与 env / 判定方式 / 心跳 tick / 失败路径 / 行映射 / 未测原因 / 聚合与导出）+
  `test_plugin_task_flow.py` 的 trace 校验、行执行、行内日志与心跳用例
- ver 端到端（2026-10-16）：从 anc-hub 导入会话数据集（file_id → session_file_id 通道）→
  建 trace 任务 → 20 行「无原始会话」数据集整行判未测（success=true、failed_count=0、
  unmeasured_reason 写明原因）；结果页按 trace 口径渲染（徽章分布/维度均分隐藏，明细列换为
  真实数据分/判定有效率/关键步骤覆盖率/未测原因，展开行为线程与规则锚点）

## 十、验证记录（2026-10-16，ver · 模型判定 · 并发 20）

用小会话（原 4642 行线程截断为 200 行的同一份 jsonl，`/anc-tms:run` 在第 7 行）跑模型判定，
任务 `task_71d5c5be6b18`（`PLUGIN_EVAL_CONCURRENCY=20` 生效后 `docker compose up -d worker` 重建）：

| 项 | 实测值 |
|---|---|
| 运行器 argv | `… score /app/uploads/plugin_marketplaces/2/project/anc-tms --thread …21a46d7d….jsonl --artifacts … --history-only --history-phase execution --history-scope key --output json --concurrency 20`（容器内 `/proc/<pid>/cmdline` 直读） |
| 关键步骤实例 | 7 个，覆盖 7（100%） |
| 检查项 | 19 项：通过 9 / 失败 0 / 证据不足 10 → 真实数据分 **100.0**、判定有效率 **47.37%** |
| 模型用量 | `deepseek/deepseek-flash` 1,027,593 tokens |
| 时长 | 墙钟约 15 分钟（`duration_ms` 906,878），同一会话离线判定仅 0.5 秒 |
| 结果页 | 顶部四卡（平均真实数据得分 100 / 已测未测 1-0 / 平均判定有效率 47.37% / 行数失败 1-0）+ 判定分布卡 + 明细行 `anc-tms`（100 / 47.37% / 100.0% / 9-0-10 / 部分样本） |

## 十点五、验证记录（2026-10-16，ver · 得分与样本量同屏，分支 23f9454）

同一任务 `task_71d5c5be6b18`（1 行、25 个检查项、可判定 9、证据不足 16）在部署后重载页面：

| 位置 | 渲染结果 |
|---|---|
| 得分卡 | 标签「部分样本得分」；值 100；副行「得分只含已判定的 9 / 25 项检查（16 项证据不足未计分；若按违规计，下界 36）」；警示标签「评审不完整，不代表整条线程」 |
| 判定分布卡 | 「得分分母 = 通过 9 + 失败 0 = 9 项；证据不足的 16 项不计分、也不等于通过（缺证据，需补产物或人工复核）；不适用 0 项不进任何分母」 |
| 明细表列 | 「真实数据分（可判定/适用）」 |
| 展开行指标行 | 「部分样本得分：100/100；判定有效率：36%（可判定 / 适用 9 / 25）；关键步骤覆盖率：85.7%（6 / 7）」，并给出下界「若把证据不足的检查一并按违规计，下界为 36/100」 |

该行的 `aggregate` 是改动前算的（无 `score_label/decidable_count/conservative_score`），页面按
`check_summary` 现算，取值与后端口径一致——旧记录同样能看到样本量与下界。

## 十一、验证记录（2026-10-16，ver · 报告口径展示，分支 761a913）

规则文件里的 `dimension`（`procedure_adherence` 等）是抽取期的内部键，评测方向名以 plugin-eval
报告为准，因此行结果与页面按 `report.md` 的结构与措辞重排，并用真实报告做过逐项对照。

**本地对照（映射同构）**：把 2026-10-15 那份标准深度联合报告（`anc-wms`，`report.json`）过一遍
`_row_from_trace`，两节与 `report.md` 逐项一致——

| 报告 | 行结果 |
|---|---|
| 历史执行可信度 97.0；判定有效率 33.7%；关键步骤覆盖率 100.0%（35/35）；可判定/适用 33/98；通过·失败·证据不足·不适用 32/1/65/0 | 96.97 / 33.67% / 100.0%（35/35）/ 33-98 / 32-1-65-0 |
| 执行分项：报告准确性 100.0（22.6%，7/31）、流程遵循 94.4（40.0%，18/45）、异常处理可信度 100.0（36.4%，8/22） | 方向名与四项取值全同 |
| 失败项：`auth-calls @ L1526B0-9e557d8248`，方向流程遵循，预期「凭据经 Helper 或命令替换注入使用」 | 同（`failed_checks[0]`，`dimension_label=流程遵循`） |
| 历史生成可信度 100.0；判定有效率 38.5%；关键步骤覆盖率 94.4%（17/18）；可判定/适用 20/52；通过·失败·不足 20/0/32 | 同；分项 来源一致性未测(0/7)、需求覆盖 100.0(14/14)、用例质量未测(0/5)、生成流程与报告 100.0(13.0%，3/23) |
| 未被检查的关键步骤：需求读取 1 | `behavior_coverage.uncovered_by_category={"gen-source":1}`，页面按关键步骤表还原步骤名 |

**ver 实跑（部署分支构建 `ai-evo:761a9131`）**：build-service 构建该分支 → 部署 → nginx 静态根
同步（`frontend/dist/assets/Results-BEnXo5o6.js` 与本地构建产物同名同哈希）。重跑
`task_71d5c5be6b18`（200 行小会话线程，sha256 `05e518c0…`，识别 `anc-tms`，模型判定）：

| 项 | 实测值 |
|---|---|
| 运行器 argv | `… --history-phase execution --history-scope key --concurrency 20`（容器内 `/proc/<pid>/cmdline` 直读，线程指向 `/app/uploads/hub_files/task_71d5c5be6b18/…/21a46d7d….jsonl`） |
| 行结构 | `trace.phases` 一节：`{label:"历史执行可信度", status:"partial", score:100.0, coverage:36.0, counts:9/0/16/0, dimensions:[…], key_steps:[7], key_step_total:7, failed_total:0, limitations:[4], model_usage}` |
| 方向分项 | 报告准确性 未测（0/1）、流程遵循 100.0（38.89%，7/18）、异常处理可信度 100.0（33.33%，2/6） |
| 检查项 | 25 条，逐条带 `dimension_label` 与 `phase`（`procedure_adherence→流程遵循` 18 条等） |
| 关键步骤表 | 检索与定位 1/16 次尝试/2 项、准备与绑定 3/3/9 项、委派 1/1/1、业务写与造数 1/4（本步调用确认「未见，无强制依据 1」）、查证与澄清 1/3（「已调用 1」） |
| 覆盖率 / 未被检查 | 关键步骤覆盖率 85.71%（6/7），未被检查：准备与绑定 1 |
| 模型用量 / 时长 | `deepseek/deepseek-flash` 1,333,831 tokens；墙钟约 20 分钟（`duration_ms` 1,202,056） |
| 结果页 | 四卡（平均真实数据得分 100 / 平均判定有效率 36% / 平均关键步骤覆盖率 85.71% / 已测·未测 1-0）+ 方向分项卡 + 明细行 + 展开行逐节（真实数据测评指标 → 历史执行可信度 → 分项 → 关键步骤 → 未被检查的关键步骤 → 限制与未验证项 → 模型用量 → 逐检查项明细 25 项），方向列显示中文方向名 |

## 十二、验证记录（2026-10-16，合并口径：综合分 + 真实数据一份报告）

改动：`--history-only` 去掉、`--depth standard`、`--history-phase all`（离线判定退到 `--depth quick`），
行结果同时落综合分（`average_score`/`badge`/`dimension_scores`/`anti_patterns`/`plugin_eval.skills`）
与真实数据分（`real_data`/`trace.phases`）；任务表单不再暴露深度选择。

**本地实证（真实市场插件 + 真实线程）**：`score project/anc-tms --thread trunc/t200.jsonl --depth quick
--history-phase all --history-scope key --history-no-llm --output json` 一次调用同时产出
`composite{score:30.18, badge:no_badge, confidence:Estimated}`、`layers[static 0.563, 98 条反模式]`、
`skill_results[23]`、`real_data{score:100.0, coverage:50.0, phases:[执行, 生成]}`——
证明去掉 `--history-only` 后两部分在同一次评测里都出。行映射过一遍该 payload：
行分 30.18（综合分）、`dimension_scores` 6 个已测维度（未测的 `robustness` 剔除）、
`plugin_eval.skills[23]`（`apollo-dcc` 51.45 等）、`real_data.score=100.0`、
`trace.phases` 两节（生成阶段为「未测」，因为 200 行截断线程里没有生成类关键步骤）、行 JSON 约 70KB。

**聚合/导出**：`composite_summary{30.18, 1, 0}`、`badge_summary{no_badge:1}`、
`items[].composite_score=30.18` 与 `real_data_score=100.0` 各自独立；导出同一行给出
`得分口径=合并口径（综合分 + 真实数据分）`、`综合分=30.18`、`真实数据分=100.0`、`可判定/适用=1 / 2`。
旧口径行（无 `composite_score` 键）导出为 `得分口径=历史口径（仅真实数据分）` 且 `复合分` 列清空。

**用用户本地那份标准深度报告做端到端对照**（`anc-wms`，2026-10-15 `report.json` 直接过一遍新管线）：

| 报告 `report.md` | 平台行结果 |
|---|---|
| 设计质量综合分 34.2（No Badge，Assessed）、反模式惩罚 -50% | `average_score=34.23`、`badge=no_badge`、`confidence_label=Assessed`、`anti_pattern_penalty=0.5` |
| Dimension Scores 10 项（robustness / code_template_quality 未测） | `dimension_scores` 8 项已测（0.439–0.968），两个未测维度剔除 |
| Anti-Patterns 共 55 条 | `anti_patterns` 55 条，聚合出 `anti_pattern_summary` |
| Skill 明细 14 行（`awms-app-resource` 65.5 …） | `plugin_eval.skills` 14 行（`awms-app-resource` 65.55 bronze） |
| 真实数据得分 98.1；判定有效率 35.3%；关键步骤覆盖率 98.1% | `real_data{score:98.11, coverage:35.33, behavior_coverage.rate:98.11}` |
| 历史生成 + 历史执行两节；分项 7 行（含两个未测） | `trace.phases` 两节（历史执行/历史生成），`direction_summary` 7 行（来源一致性/用例质量未测） |
| 检查项 98 + 52 = 150 | `checks_total=150`（`checks` 上限 200，未截断），行 JSON 约 390KB |

**ver 实跑（部署分支构建 `ai-evo:e442ea3a`）**：build-service 构建 e442ea3 → 部署 → 容器内
`/proc/<pid>/cmdline` 直读 argv：
`… score …/project/anc-tms --thread …/21a46d7d….jsonl --depth standard --artifacts … --history-phase all
--history-scope key --output json --concurrency 20`（无 `--history-only`）；
`frontend/dist/assets/Results-B3GS52db.js` 与本地构建产物同名（同哈希即同一份构建）。

## 十三、验证记录（2026-10-16 17:23→17:38，ver · 合并口径端到端 · 分支 `6ef8bc4` / 镜像 `ai-evo:6ef8bc4e`）

任务 `task_71d5c5be6b18`（1 行数据集，200 行截断线程，`trace_judgement=model`，并发 20）第 6 次执行：

| 项 | 实测值 |
|---|---|
| 运行器 argv（容器内 `/proc/<pid>/cmdline` 直读） | `python3.12 /app/app/services/plugin_eval_market_entry.py /app/uploads/plugin_marketplaces/2/public/plugin-eval score /app/uploads/plugin_marketplaces/2/project/anc-tms --thread /app/uploads/hub_files/task_71d5c5be6b18/7ab9c17c…/21a46d7d….jsonl --depth standard --artifacts /tmp/plugin-eval-trace-8_8mqeo6/artifacts --history-phase all --history-scope key --output json --concurrency 20`（**无 `--history-only`**） |
| 时长 | 17:23:11 → 17:38:12（约 15 分钟；静态层 + 23 个 skill 的 rubric 评审 + 执行/生成两阶段历史一次跑完） |
| 行日志 | `[1/1] 完成：被测插件 anc-tms，综合分 34.39（设计质量），真实数据分 100.0，判定通过/失败/证据不足 13/0/10` |
| 聚合 | `composite_summary{34.39, 1, 0}`、`badge_summary{no_badge:1}`、`real_data_summary{average_score:100.0, score_label:部分样本得分, average_coverage:56.52, average_behavior_coverage:100.0, measured/unmeasured:1/0, check_summary:13/0/10/0, conservative_score:56.52}`；`direction_summary` = 执行三方向（流程遵循 100/61.11%、异常处理 100/50%、报告准确性未测）+ 生成四方向全未测 |
| 行级 write-through（`evaluation_results` 第 6 轮 row 0） | `average_score=34.39`、`badge=no_badge`、`dimension_scores` 8 项、`anti_patterns` 98 条、`checks_total=23`、`plugin_eval.depth=standard`、`plugin_eval.composite.score=34.392174…`（dimensions 带 grade/weight）、`plugin_eval.layers[static]=0.5634`、`plugin_eval.skills[23]`（`apollo-dcc` 68 bronze、`bigdata-log` 71.25 silver、`run` 29.97 无徽章 …），行 JSON 88KB |
| 结果页（`https://ai-evo.example.com/results` → 选该任务） | 顶部「平均综合分（设计质量）34.39（静态层 + LLM 评审；1 行出分）」+「部分样本得分 100（仅已判定 13/23 项，下界 56.52）」+「平均判定有效率 56.52%」+「平均关键步骤覆盖率 100%」；判定分布 13/0/10/0；方向分项表（执行 3 行 + 生成 4 行未测）；徽章分布「无徽章 ×1」；维度平均分 8 行（Token 效率 0.961 … 编排适配度 0.562）；反模式汇总（DEAD_CROSS_REF 76、MISSING_TRIGGER 22）；明细行 `anc-tms｜插件｜34.39｜100（13/23）｜无徽章｜标准｜56.52%｜100.0%｜13/0/10｜部分样本` |
| 展开行 | 「设计质量综合分（静态层 + LLM 评审）」块（综合分 34.39/100、无徽章、Assessed、反模式惩罚 0.5、深度 标准、反模式 98、Skill 23、与真实数据分相互独立）+ Skill 明细 23 行（含 `static 0.540；judge 0.790` 各层得分）+ 「真实数据测评指标」+ 「历史执行可信度」（13/0/10/0、覆盖率 100.0%（7/7）、规则 `anc-tms.yaml` v1.1.0 与 SHA256） |
| 任务表单（编辑该任务） | 表单项只剩 任务名称/任务描述/评测类型/数据集/评测对象来源/判定方式/判定并发，**没有「评测深度」项**；提示「评测深度随判定方式固定，不需要选择（一份报告同时含设计质量综合分与真实数据分）」 |
| `items[].plugin_eval` | 聚合时被裁剪为空对象（页面不读它）；完整的 `plugin_eval.composite/skills/layers` 在行级 `row_result` 里 |

**本次部署的环境处置（不影响口径结论）**：主分支 CI 部署（17:14）重建了 worker；我这版分支的部署
撞上另一个同事的长任务（judge 评测 119 行，1.2 分钟/行）在旧 worker 里优雅退出中，
build-service 的全局互斥因此被占住。为不动别人的任务、也不长时间占住互斥，改为
**用同一镜像 `ai-evo:6ef8bc4e` 起一个临时 worker**（`docker run` 复刻 compose worker 的
env/卷/网络，`SERVICE_TYPE=worker`）执行本次任务，任务链路（celery → 行级写入 → 聚合 → 结果页）
与正式部署完全一致；api 侧停在 main（`0294cc67`），前端 dist 同步为分支构建产物
（`assets/index-C_KN6SJj.js` → `Results-C9uvoJj4.js`，含新口径文案）。


## 十四、两条线拆分与未测阶段表达（2026-10-21）

### 1. 设计质量线只评测一次

- 设计质量只与插件目录有关（`static + judge` 读的是插件文件），与线程无关。原实现每行都跑一次
  完整的 `score <plugin> --thread ...`，同一插件在 N 行数据集里被重复评 N 次。
- 现在任务内维护 `design_cache`，键为 `(插件名, 插件版本, 市场路径, 判定方式)`：
  - 首次命中：跑完整评测（设计 + 历史），把 `composite/layers/skill_results/设计侧模型用量` 存入快照；
  - 后续行：只跑 `--history-only`，行结果复用快照（`design_reused=true`）；
  - 不同行识别出不同插件时按插件分别缓存（每个插件各评一次）。
- 行结果新增 `design_reused` 与 `plugin_eval.design{evaluated,reused,model_usage,duration_ms}`；
  聚合新增 `design_quality{evaluated_count, plugins[]}`（按插件一条，含 `covered_rows/reused_rows`）。

### 2. 结果页分两条线

- 「设计质量线（每个插件只评测一次）」：平均综合分卡 + 按插件明细表（分数/徽章/置信标签/
  覆盖行数/复用行数/反模式条数，展开看维度、Skill 明细、模型用量、评测耗时）。
- 「历史数据线（逐行线程）」：得分（标签随 `score_label`）、判定有效率、覆盖率（列名随
  `behavior_scope` 切换）三张卡 + 「阶段汇总」表（生成/执行两行）。
- 明细表设计质量列改名「设计质量分（任务级）」并标注「首次/复用」。

### 3. 未测阶段不再显示 0

根因：plugin-eval 对「本线程没有该阶段任务」输出 `status=unmeasured`、`phase_detected=false`、
四计数全 0（报告 `reporter.py` 同样打印 0/0/0/0）。平台原样落库后，页面把 0 直接画出来，
并把这 4 个生成方向混进执行方向表，形成整行 0/0 的假分项。

修复：
- 阶段块新增 `detected`（来自 `source.phase_detected`）与 `unmeasured_reason`（取限制里含「未测」的一条）；
- `unmeasured` 阶段的得分/有效率/覆盖率/可判定·适用/四计数统一显示 `—`，并给出原因；分项/关键步骤/失败项不渲染；
- `direction_summary` 只统计已探测阶段的维度，并带 `phase/phase_metric/phase_status`；
- `real_data_summary` 新增 `phase_summary`（未测阶段保留 status 与空计数）与 `behavior_scope`；
- 逐检查项明细新增「阶段」列；反模式汇总恢复「最重严重度」；
- 引用证据原文（`evidence_id + 来源 + 500 字摘录`）随阶段块落库并可在展开行查看。

### 4. 导出同步

新增「设计质量复用 / 阶段得分 / 未测阶段」列；覆盖率列名随口径切换为「关键步骤覆盖率/行为覆盖率」；
未测阶段的方向列留空（不写 0/0）。
