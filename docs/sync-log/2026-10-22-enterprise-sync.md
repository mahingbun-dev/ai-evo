# 企业线同步变更说明（2026-10-22）

本次同步把内部 GitLab 线 2026-10-14 之后 7 天的全部迭代合入 GitHub main。
源区间共 101 个提交（含 merge），收敛为单个同步提交；131 个变更文件（+22172/−794），
其中 13 个与既有本地改动重叠、16 个与上次同步的适配改写重叠，均按三方合并调和。

## 一、时间线对比

| | GitHub main（同步前） | 企业 GitLab main |
|---|---|---|
| 10-12 | 脚手架单套 hub_package 模板 | 同内容（同源） |
| 10-14 | sync: 合入企业线 2026-10-12 之后全部迭代（W1–W19b） | 同内容（同源，基线 9139ac6+3587741） |
| 10-14 ~ 10-16 | enterprise-eval-arch 架构线（Phase 0–3：沙箱上电/编排可观测/规模化/agent 拉起）+ 沙箱挂载/CVE/镜像修复（GitHub 线独有，不在企业线） | 数据集弹窗加宽、hub 插件 v1.2.0 版本号修复、CI 串行与 test 阶段调整 |
| 10-15 | — | 插件市场 + 插件评测类型、字段元数据自描述、迁移门禁、CC 裁判 E2BIG、F 哨兵 |
| 10-16 | Merge PR #1/#2（GitHub 线基线 = `0086e02`） | 插件 Trace 评测多轮、结果页四项体验、裁判空值规则与维度最高分、build-service 前端-only 快速通道与 worker 非阻塞换代、hub 插件 v1.3.0、ensure_super_admin 422 修复 |
| 10-17 ~ 10-21 | —（GitHub 线无更新） | MCP 插件工具 call_tool 转发、镜像清理超时修复、裁判数据集输出来源、Trace 双线拆分、评分等级删除、详细结果动态列 |
| 10-22 | **本次同步落点** | 源头 `bf4fd7a` |

GitHub 线独有的架构线改动（沙箱 executor/egress、Prometheus 指标、trivy 门禁、
volume-init、AUTH_JWT_SECRET fail-fast 等）全部保留，不受本次同步影响。

## 二、功能变更分组

### 1. 插件市场与插件评测类型（新功能）
- 插件市场：git 克隆方式选择被测插件，市场列表可展开插件内的 skill 级评测对象；
  评测期懒加载同步、被测插件版本锚点、评测动作实时加载市场版 plugin-eval。
- 插件评测类型（`plugin`）：移植 plugin-eval 三层评测思路。
- git 运行时离线包进镜像（插件市场 git 克隆依赖）。

### 2. 插件 Trace 评测（新功能，多轮迭代）
- 会话 Trace 口径：按插件历史真实执行评测（区别于设计质量静态评审）。
- 报告口径：方向分项/关键步骤/失败项、得分与样本量同屏（部分样本标签、保守下界）、
  设计质量综合分与真实数据分合并同屏、未测阶段不再显示 0。
- 工程保障：模型调用并发可配（`PLUGIN_EVAL_CONCURRENCY`）、长行执行期
  心跳/进度/超时上限、线程产物镜像去重、未定位任务写明未测原因。

### 3. hub 插件服务 v1.2.0 / v1.3.0
- v1.2.0：`script_id` 契约变更补升版本号（修复中台 VERSION_CONFLICT 持续拒注册）。
- v1.3.0：执行状态/历史/单次执行明细查询与结果文档导出（markdown 人读报告 +
  json 无损结构，同版本同格式幂等复用 file_id）。
- MCP 插件工具改经 `call_tool` 转发，修复 anc-hub 文件查询/下载全量失败。

### 4. 评测裁判（judge）能力
- CC 裁判 `system_prompt` 超 argv 128KB 上限（E2BIG）自动降级并入首条消息。
- 输出协议 F 哨兵：裁判输出字符串 `"F"` 表示维度不适用，均分只统计适用行。
- 空值判定规则注入 + 维度最高分全链路贯通；CC 总分重算只按实际涉及维度微平均。
- AI 裁判员支持模型输出来自数据集字段（`model_output_source=dataset_field`，
  跳过模型调用直接评已有输出）。

### 5. 结果页与前端体验
- 失败详情弹窗、分维度裁判理由 tooltip、多次执行聚合展示、失败统计卡点击汇总。
- 结果页加载提速（history 轻量模式 + 单版端点）、雷达图分值直显、加载反馈。
- 详细结果序号修复 + 雷达 tooltip 遮挡修复。
- 评测维度权重下拉数值兼容；评分标准等级支持删除。
- 详细结果表格固定列 + 动态待评测内容列。
- 数据集：预览横向滚动、编辑弹窗左右分栏、字段元数据改产出方自描述。

### 6. 部署与 CI（build-service）
- 前端-only 变更快速通道：变更全部落在 `frontend/` 时跳过镜像构建与容器重建。
- worker 非阻塞优雅换代：部署不再被长任务阻塞（SIGTERM + 短等 + 后台 watcher）。
- 镜像清理超时不再判部署失败；`latest_deployed_commit` 捕获 UnicodeDecodeError。
- 迁移检查工具（`tools/check_migrations.py`）与空库全量升级修复；迁移链
  唯一 head（见下）。

### 7. 安全与稳定性修复
- `ensure_super_admin` 依赖注解 `Any→Request`，修复超管门禁端点全量 422。
- 历史遗留重复迁移 `14f63ba90774` 幂等化（空库全量升级不再 DuplicateColumn）。

## 三、同步调和说明（与本地既有改动的合并）

- **冲突三方合并**：`app/main.py`、`Dockerfile`、`docker-compose.yml`、
  `docs/build-pipeline.md`、`tools/build-service/*` 等重叠文件按
  base=3587741 / ours=GitHub main / theirs=企业线 三方合并；
  GitHub 线架构改动（trivy 扫描、scan_image、ARCH-* 小节等）全部保留，
  企业线新能力（快速通道、worker 换代等）并入。
- **上次同步适配的保留**：hub 插件 Bearer 鉴权形态（非企业 SSO Cookie）、
  前端 OSS 地址 `VITE_OSS_BASE_URL`/`VITE_OSS_TENANT_CODE` 配置化、
  本地 JWT 登录链路相关测试改写——均未被企业线版本覆盖。
- **迁移链**：同步后迁移链恰好一个 head（`a7c2f9e4b1d3`）。企业线自带的
  merge 节点 `6951565fd619` 恰好把此前 GitHub 线的两个分叉头
  （`0a66a8f097df`、`1a2b3c4d5e6f`）接顺，38 个祖先节点全可达、无断链。
- **CI**：企业线的 `.gitlab-ci.yml` 改动不带入（本仓库 CI 为 GitHub Actions）；
  迁移检查工具 `tools/check_migrations.py` 作为独立工具带入。

## 四、脱敏说明

同步内容中的企业内部信息按下表替换，其余技术内容原样保留：

| 类别 | 处理 |
|---|---|
| 企业 GitLab 域名/组织 | 企业私有 GitLab 域名与组织路径替换为 `gitlab.example.com`、`demo-group` 等示例值 |
| 企业系统访问域名 | 结果页/OSS 域名替换为 `ai-evo.example.com`；OSS 地址本就走构建时环境变量注入 |
| 平台 SSO Cookie 名 | 代码文案改引用配置 `HUB_SSO_COOKIE_NAME`（行为不变，名由配置决定）；测试桩统一 `PLATFORM_SSO` |
| 部署 Token 用户名 | `gitlab+deploy-token-173` → `gitlab+deploy-token-1`（示例） |
| git 凭证测试桩 | 疑似真实样式口令替换为 `bot-secret-001` 等明显假值 |
| 业务系统名（anc-tms 等） | 作为评测样例数据保留（无地址/凭证，与仓库既有 `anc-hub` 等概念名同口径） |

脱敏后全树扫描企业域名、组织名、SSO Cookie 名、样例口令与内网 IP 关键词，字面零命中。

## 五、验证结论

同步调和后经独立测试 agent 全量验证（含与干净基线 0086e02 的逐用例对照），
首轮抓出 5 类同步引入问题（前端冲突标记残留、迁移文件三方拼接错误、
脱敏后测试-源码口径失配 ×3），全部修复后复验：

- 后端单测：**3824 passed / 6 skipped / 4 failed**（`pytest -m unit`）。
  4 个失败（`tests/security/test_env_whitelist.py` ×3、`test_rce_chain.py` ×1，
  契约冒烟脚本执行超时）在同步前基线上逐字复现，属既有环境问题，与本同步无关。
- 集成测试（无外部依赖部分）：与基线失败清单 diff 为零差异
  （依赖 PG/Redis 的用例在本机 ERROR，两边数量与分布完全一致）。
- 空库全量升级：`test_w6_alembic_env::test_upgrade_head_on_fresh_db` 通过
  （修复三方拼接引入的 `import sqlalchemy as sa` 缺失后）。
- 迁移链静态检查：39 个迁移文件恰好一个 head（`a7c2f9e4b1d3`），无断链。
- 前端 vitest：7 文件 33 用例全过；`node --test` 契约测试全过。
- 前端构建：`vite build` 成功（仅 chunk 体积警告，与基线一致）。

### 同步引入问题的修复记录

| 问题 | 修复 |
|---|---|
| `index.js` 残留三方合并标记 → build 失败 | 删除标记行 |
| `14f63ba90774_upgrade.py` 三方拼接后 `sa` 未导入 → 空库升级 NameError | 补回 `import sqlalchemy as sa`，docstring 改为准确描述 |
| export 端点测试与脱敏后源码口径失配（恒 401） | 测试补 `HUB_SSO_COOKIE_NAME` 配置注入，断言对齐新文案 |
| `test_arch_3_3_image_scan` 缺 `ensure_git_bin` 桩 | `_wire` 夹具补桩（与增量自带测试同款） |
| `test_deploy_frontend_only` 读 `.gitlab-ci.yml`（本仓库线无此文件） | 文件缺席时 skip（企业 CI 线专属断言） |

CI 轮次补充修复（本地无 PG/无镜像扫描环境覆盖不到的面）：

| 问题 | 修复 |
|---|---|
| `test_migration_w9` 三方合并残留 `W9_HEAD` 未定义引用（CI 带 PG 实测抓出） | 对齐源版本 `upgrade head` |
| trivy 新披露 CVE：Pillow 12.3.0（CRITICAL×3+HIGH 若干）、aiohttp 3.14.3（HIGH×2）——依赖版本本同步未动，系漏洞库时效致基线同样扫红 | 主镜像与 script-runner 镜像 Pillow→13.0.0、aiohttp→3.14.4；`RUNNER_IMAGE_PACKAGES` 静态清单门禁同步 |

最终 CI：Backend tests / Docker build / Frontend build / trivy 四项全绿。
