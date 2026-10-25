# 2026-10-25 P0 迭代：性能并发四项 + 使用门槛五项

上游输入：plans/2026-10-25-性能与并发提升分析.md、plans/2026-10-25-降低使用门槛改进分析.md。
门槛：全量后端 pytest 5214 passed / 0 failed（真实退出码验证）；前端 pnpm build 通过。

## 性能并发 P0（perf(perf): 提交 1）

1. **P0-1 行循环并发化**：一致性/成功率/二分分类/插件/插件 trace 五处逐行 `for` 循环改为 `_row_worker` + `ThreadPoolExecutor`（行并发取 task.concurrency 钳制 1~16，插件线保守 min(,4)）；软超时/池中止异常从 future 原样上抛；行线程内 bind_judge_context 与 judge_pool 协同；一致性 repeat_count 次调用 asyncio.gather 并发。
2. **P0-2 SDK 客户端复用**：model_caller 的 openai/anthropic 客户端按 (endpoint, api_key) 进程级缓存（线程安全）；httpx.AsyncClient 按事件循环弱引用缓存（跨 loop 隔离防 "Event loop is closed"）；同步散装调用统一进程级 requests.Session（RAG API/webhook）。单测 test_model_client_cache.py 钉实例同一性。
3. **P0-3 补索引**：迁移 d41f8b2c6a90——evaluation_tasks (team_code,status)/(status)/(created_at) + task_execution_stats (created_at)，消除任务提交配额对账与列表查询的全表扫描（生产大库建议手工 CONCURRENTLY，见迁移注释）。
4. **P0-4 落库与进度降频**：persist_row_result 改进程内有界缓冲（N=20 行/500ms 批量 upsert 单 commit，停止/软超时/收尾显式 flush_pending_rows；flush 前崩溃最多重评 ≤20 行）；_update_progress 心跳同款节流（1s 或 total//100 行步长）+ 末行 force 刷新，WS 推送从每行一次降为秒级。

## 使用门槛 P0（feat(onboarding): 提交 2）

1. **P0-A 自动迁移**：entrypoint api 分支 alembic upgrade head（advisory lock 防并发，失败 fail-fast）+ compose `migrate` 一次性服务（api depends_on completed_successfully）双保险；run_local_dev 启动时未迁移给出命令提示。
2. **P0-B demo 一键起**：`AIEVO_DEMO_MODE` 显式开关（默认关，不动企业 fail-fast 基线）——开启且未配 AUTH_JWT_SECRET 时首启自动生成写持久卷并高亮提示；.env.example 增 demo 模式完整示例段；README/QUICK_START 增「一分钟演示」。
3. **P0-C 遗留清理**：删除 OpenCompass 遗留 scripts/setup.sh、quick_start.py、scripts/README.md；insert_mock_data.py 加用途注释。
4. **P0-D 一键演示**：`POST /demo/seed`（幂等，超管或空库）与 `/demo/seed-and-run`（seed+走既有 run 入口）；内置 `/demo/mock-llm/chat/completions` OpenAI 兼容演示端点（固定演示 token，无状态白名单），配置了 LITELLM_BASE_URL/KEY 则演示模型/裁判走 LiteLLM（mimo-V2.6）；Home.vue 步骤卡按统计动态打勾（4 步含查看结果）+「一键体验演示评测」横幅 + 空状态引导。
5. **P0-E CLI 可安装**：GitHub Action/GitLab/Jenkins 三模板 pip 安装改「PyPI 优先、git 子目录回退」双通道；新增 publish-cli.yml（tag/workflow_dispatch + PYPI_API_TOKEN，发布动作留给仓库所有者）；cli 构建产物进 .gitignore。

## 合并后治理补齐（同提交 2）

- judge-pool 三个写端点（PUT /config、release、release-all）接 `audited` 平台审计依赖（M4 纪律静态断言要求）；demo mock 端点登记审计白名单（无状态探针）。
- conftest 注入 langchain vertexai 自守桩：ragas 0.4.3 顶层硬 import 已被 langchain-community 0.4.2 移除的模块，本地/CI 无 google 依赖时放行 ragas 导入（产品代码 ragas 路径本就是惰性可选，不受影响）。
- 测试侧适配：w3_mocks 的 update_progress mock 透传 P0-4 新增 force 参数；row_level_persistence 幂等用例按有界缓冲契约显式 flush；judge_pool_task_concurrency 的 update_task mock 补 patch M2/M5 校验器（MagicMock 自动属性与合并后语义冲突）。

## 执行说明

两个实现子代理启动后被账号并发限额中断（部分完成：性能项 P0-1/2/4 主体、门槛项部署面与 demo 后端），剩余部分（P0-3 迁移、Home.vue、P0-E、测试与门槛修复）由主会话接手完成。另：本次发现此前用 zsh PIPESTATUS 读退出码的写法在 zsh 下恒为 0，合并门槛一度「假绿」——本次全量以独立进程退出码复核，真实全绿。
