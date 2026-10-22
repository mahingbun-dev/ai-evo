# M0 宣传口径修正记录（2026-10-22）

对应方案 `plans/2026-10-22-Agent质量工程平台开发方案.md` M0 节（QE0-1/2/3）。
目标：文档与仓库元数据的表述和实际可用能力一一对应，消除商业尽调阶段的「宣传 > 实际」硬伤。
本里程碑为纯文档/元数据改动，不涉及前后端代码与数据库迁移，无新增验收截图（无界面行为变化）。

## 一、变更清单

### QE0-1 评测类型口径（README.md、README.zh-CN.md、docs/user_guide.md）

- 首段与总览「9 种评测类型」→「8 种 / 8 evaluation types」，类型表补齐响应耗时、插件评测两行。
- 实际可用 8 种与代码逐一核对：前端下拉 8 项可选 + safety/dialogue 置灰标注「规划中」
  （`frontend/src/views/Tasks.vue` 评测类型 select）；后端创建白名单同 8 种
  （`app/services/evaluation_task_service.py` `valid_evaluation_types`）。
- safety/dialogue 统一标注「前端置灰规划中，执行链随 M3 安全评测里程碑放行」。
- user_guide 三处残留旧口径修正：「下拉共 9 个选项」→「共 10 个选项（8 可用 + 2 置灰）」；
  评测类型详解第 7 节与 FAQ Q7 改为置灰口径；移除失实旧下拉截图引用
  （截图为早期 UI：安全评测尚可选、无插件/对话选项，待 M3 放行 safety 时重拍）。

### QE0-2 裁判引擎口径（README.md、README.zh-CN.md、docs/user_guide.md）

- 「3 种裁判引擎」→「Claude Code（默认）+ 传统裁判图可用；DeepSeek 适配就绪、任务流 M8 接通」。
- 代码核对：任务创建白名单仅 `claude_agent/graph`（`app/services/evaluation_task_service.py` 校验）；
  执行分发只有 claude_agent 分支 + graph fallthrough（`app/services/evaluation_executor.py`）；
  DeepSeek 适配器与全局配置卡现用于前处理 agent 步骤（`app/services/harness_adapter.py`）。
- user_guide 引擎表重排：两个可用引擎在前，DeepSeek 移末行标注「任务流未接通/暂不可用（M8 接通）」，
  并如实写明「当前任务表单保留选项，提交会被后端拦截」。

### QE0-3 商业定位一句话（README 首段、docs/user_guide.md、GitHub 仓库元数据）

- README 中英文标题与首段重定位为「私有化部署的 Agent 质量工程平台（评测即门禁）」，
  均带诚实标记：门禁闭环（阈值门禁、API Key、CI CLI、Webhook）随 M1 里程碑落地，
  当前可用能力照实列举（代码核对确认 app 内尚无 gate_config/gate_result/gate 端点）。
- user_guide「平台总览·这是什么」同步该定位口径。
- GitHub 元数据（gh repo edit 实测生效）：description 更新为重定位一句话（移除旧「9 eval types」）；
  topics 新增 `agent-evaluation`、`ci-gate`（`llm-evaluation` 已有）。

### 评审修复轮（本文件同日）

- 分位数口径纠错：三处文档「均值 / p50 / p95」→「均值 / p50 / p90 / p99」。
  实现事实：`app/services/latency_evaluator.py`（p50/p90/p99 字段与计算）、
  `frontend/src/views/Results.vue` 结果指标（P50/P90/P99）；仓库内 p95 零实现。
- 补本 sync-log 条目（评审指出 M0 验收要求 docs/sync-log/ 记录而首轮缺失）。

## 二、验证结论（定向，未跑全量；全量门槛由编排复跑）

| 项 | 命令 | 结果 |
|---|---|---|
| 前端类型禁用矩阵 + 后端类型白名单 | `.venv/bin/python -m pytest tests/unit/test_w12_acceptance_independent.py tests/unit/test_w12_judge_graph_cache_and_whitelist.py -q` | 28 passed |
| 裁判引擎分发三分支 | `.venv/bin/python -m pytest tests/unit/test_evaluation_judge_engine.py -q` | 10 passed |
| 仓库治理（README 与现状一致）+ user_guide 读取链路 | `.venv/bin/python -m pytest tests/unit/test_w18_repo_governance.py tests/unit/test_docs_api.py -q` | 40 passed |
| 延迟分位数实现（p50/p90/p99，钉住本轮纠错） | `.venv/bin/python -m pytest tests/unit/legacy/test_latency_evaluator.py tests/unit/legacy/test_result_aggregator_latency.py -q` | 16 passed |
| 旧口径残留扫描 | grep「9 种评测/9 个选项/三种裁判/p95」于 README*、user_guide、QUICK_START | 零命中 |
| GitHub 元数据复核 | `gh repo view mahingbun-dev/ai-evo --json description,repositoryTopics` | description 为新口径；topics 含 agent-evaluation、ci-gate |

## 三、遗留事项（非本里程碑范围，已上报主线）

- `docs/assets/intro-card.html`（未入库宣传卡片，本工作流禁改范围）仍写「9 种评测类型 / 3 种裁判引擎」，
  需物料线同步更新。
- 前端任务表单 DeepSeek Agent radio 仍可选（提交会被后端拦截并提示「不支持的裁判引擎」），
  置灰或标注属前端小改，留待 QE8-2 接通时一并处理或主线另行安排。
- 前端品牌副标题「AI 评测平台」（Login.vue / index.html）与新版定位的统一，超出 M0 文档范围。
- user_guide 评测类型下拉旧截图已移除引用，重拍需起栈走浏览器验收，建议随 M3 safety 放行一并补。
