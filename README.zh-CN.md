# AgentEvo — 私有化部署的 Agent 质量工程平台

私有化部署的 Agent 质量工程平台，定位「评测即门禁」——模型 / Agent 的变更先过评测、再进发布。当前已可用：模型配置、数据集管理、评测前处理脚本、9 种评测任务类型（AI 裁判员 / Agent 长会话 / 一致性 / 成功率 / RAG 检索 / 二分分类 / 响应耗时 / 插件 / 安全红队）+ persona 用户模拟的仿真评测（经仿真场景运行）、终态校验器（脚本 / 数据库 / 文件 / 回调断言，确定性校验优先，M2 起）、轨迹匹配（agent 会话工具序列断言：严格 / 乱序 / 子序列 / 禁止多余四模式 + 期望 / 禁用调用，聚合出轨迹匹配率门禁指标，M2 起）、pass^k 统计（裁判类任务行级多 Trial 2-5 + 一致性/成功率重复次数，τ-bench 口径的 pass@1 / pass^k / 分数标准误，M2 起）、A/B 显著性检验（多轮同输入配对：Wilcoxon 符号秩 + 配对 t + Cliff's delta + McNemar 精确检验，M2 起）、人工金分标注与裁判-人工一致性报告（M8 起）、结果聚合与统计分析、A/B 对比测试；门禁闭环已随 M1 落地：任务级阈值门禁（结果页判定横幅 + 执行历史门禁列）、按 scope 授权的 API Key、Webhook 通知、开发者设置控制台、CI 命令行 `aievo-cli`（退出码 0=通过 / 1=门禁失败 / 2=错误，GitHub/GitLab/Jenkins 模板见 `ci-templates/`）。FastAPI + Vue 3 + Celery，一个 Docker Compose 栈即可部署。

![首页](docs/assets/00-home.png)

[![CI](https://github.com/mahingbun-dev/ai-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/mahingbun-dev/ai-evo/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.128-teal)
![Vue 3](https://img.shields.io/badge/Vue-3-brightgreen)

[English](README.md)

## 功能总览

| 模块 | 能力 |
|---|---|
| 模型配置 | OpenAI / Anthropic 兼容接口、自定义 HTTP（curl 导入）、运行时变量注入、连通性流式测试 |
| 数据集管理 | Excel 导入（动态字段）、Agent 会话文件导入、媒体字段、字段级校验、数据集版本化（打版快照、版本间行级 diff、恢复、任务跟随最新/锁定版本运行）；OTel 兼容 trace 导入（M8 起：`POST /ingest/otel-traces` 收 OTLP/HTTP JSON / Phoenix 导出 / Langfuse 导出，一 trace 一行会话数据进既有 trace 评测链路——平台与既有 tracing 平台共存，不是 tracing 后端） |
| 脚本库 | 评测前处理脚本（zip / 文件夹包）、`model.py` 契约声明、在线文件树编辑、脚手架模板 |
| 评测任务 | AI 裁判员 / Agent 长会话 / 一致性 / 成功率 / RAG 检索 / 二分分类 / 响应耗时 / 插件 / 安全红队（双通道：内置攻击样本库+变异增强，确定性检测器+裁判 rubric，M3 起）9 种任务类型，批量运行、实时进度（WebSocket）；仿真评测（M7 起，经仿真场景运行、不建任务）——persona 用户模拟器、故障注入（工具超时/错误响应/污染数据/矛盾指令/上下文超长）、终态校验出 pass^k、trial 回放与失败转数据集；评测模板版本化（打版、字段级 diff、恢复，M5 起）；任务级 cron 定时（5 段表达式、北京时间、可选时段限制——经既有运行入口投递，团队配额与任务互斥照常生效；扫描循环在 api 进程内，Redis 锁 + `next_run_at` CAS 认领保证多 api 单写，不引 Celery beat，M5 起） |
| 裁判引擎 | Claude Code 裁判（默认）、通用 LLM 裁判图与 DeepSeek Agent 裁判（deepseek-harness-sdk，M8 接通）三种可用；「与 Claude Code 对拍」出双引擎裁判一致性报告（同批既有输出逐行分数差/容差内一致率，M8）；评测维度动态配置 |
| 结果与分析 | 评分明细、历史对比、Excel/JSON/CSV 导出、趋势统计、双模型 A/B 并排对比；A/B 模板版本对照（同任务两模板版本的任务化实验，M5 起）；周报（每周一 08:00 北京时间聚合上周覆盖度/提效/门禁通过率生成 markdown，Statistics 归档可回看，推送订阅 `weekly.report.ready` 事件的 webhook，SMTP 邮件可选，M5 起）；免登录只读报告分享（M5 起：token 链接带有效期/行级范围/可吊销，`/share/:token` 只读页）；人工标注闭环（M8 起：结果页标注模式——待标队列失败行/低分行优先、快捷键打金分，一行结果一条标注；Statistics 裁判-人工一致性报告——Pearson/Spearman 相关性 + 分带 Cohen's κ 与分带混淆矩阵） |
| 管理 | 团队与成员启停/角色管理（三角色 owner/engineer/viewer——写操作 owner+engineer，viewer 全站只读；超管保留全局）、审计日志（全部写端点由声明式 `audited` 依赖统一留痕，注册表静态断言把关覆盖面；csv/xlsx 导出为企业功能位 audit_export——license 授权后可用，未授权 403+upgrade_required 且前端按钮锁定，另有企业端点旁路静态断言看守）、离线 License 管理（M6：超管上传 Ed25519 签名 license 开通企业档位与功能位——审计导出/报告白标水印；验签失败/过期只降级开源版并醒目告警，不阻断核心评测；生成工具 `tools/license/gen.py`）、用量计量（M6：每次执行的模型 token 用量——被测与裁判调用统一计量——按月度运行数与团队 token 聚合，`GET /admin/usage/summary` 供 license 续约对账；超额只告警不阻断）、内置使用文档 |

本地账号密码登录：首个注册用户自动成为超级管理员；密码 PBKDF2-HMAC-SHA256 哈希存储，会话为 HS256 JWT（`Authorization: Bearer`）。企业统一身份：`AUTH_METHODS`（`local,oidc,ldap` 任意组合，单选 `oidc`/`ldap` 即纯企业身份部署——关闭本地登录注册）开启 OIDC SSO（授权码 + PKCE S256）与 LDAP/AD 只读认证（search+bind，滤值 RFC 4515 转义、空密码直拒）；超管在「开发者设置」配置 IdP / 目录（含连通性自检）；首次企业登录自动开户（JIT）或按邮箱映射既有账号，目录组与 SSO 组共用 groups→团队映射。

## 快速开始（本地开发）

依赖：Python 3.12+、Node.js 18+（pnpm）、Docker（跑 PostgreSQL / Redis）。

```bash
# 1. 启动本地基础设施容器
docker compose -f docker-compose.dev.yml up -d

# 2. 安装后端依赖（建议 venv）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置环境（.env 不入库）
cp .env.example .env
#   编辑 .env：DATABASE_URL / REDIS_URL / AUTH_JWT_SECRET

# 4. 建表（数据库迁移）
export AIEVO_DATABASE_URL="$DATABASE_URL"
alembic upgrade head

# 5. 启动后端（默认 8100）
python scripts/run_local_dev.py

# 6. 启动前端（另一终端，/api 代理到 8100）
cd frontend && pnpm install && pnpm dev   # https://localhost:3000
```

打开 `https://localhost:3000` → 注册账号（首个用户自动成为超级管理员）→ 「模型配置」添加被测模型与裁判模型 → 「数据集管理」导入 Excel → 「评测任务」新建并执行。

## Docker Compose 部署

镜像在线构建（`Dockerfile.online`：node 阶段构建前端，python 阶段安装依赖）：

```bash
cp .env.example .env   # 设置 DATABASE_URL / REDIS_URL / AUTH_JWT_SECRET / IMAGE_TAG
docker compose up -d --build
```

- `api` 在 8100 端口同时承载 REST API 与前端构建产物（`AIEVO_PORT` 可改端口）
- `worker` 执行 Celery 评测任务；`logs/` 与 `uploads/` 经卷持久化
- 首次 `up -d` 时 `volume-init` 一次性服务会自动把 `uploads/`、`logs/` 卷属主
  迁移为容器运行用户（uid 10002），无需手工 chown（`docker compose ps` 里它显示
  exited(0) 属正常）
- `.env` 里设 `COMPOSE_PROFILES=bundled-db` 可一并启动内置 PostgreSQL
- 评测前处理脚本执行模式 `SCRIPT_EXEC_MODE`：默认 `local` **仅限开发机**
  （脚本与平台进程同环境直跑），生产/企业部署禁止 local——container 沙箱通道
  就绪后须显式设 `SCRIPT_EXEC_MODE=container`，现状与步骤见
  [docs/build-pipeline.md](docs/build-pipeline.md) 第九节

完整环境变量清单见 [docs/build-pipeline.md](docs/build-pipeline.md) 4.4.1 与 `.env.example`；
离线/CI 构建流水线（锁定 wheel、内置 Claude Code CLI）见 [docs/build-pipeline.md](docs/build-pipeline.md)。

## 评测类型

| 类型 | 一句话说明 | 需要裁判模型？ |
|---|---|---|
| AI 裁判员 | 裁判模型按评测维度对模型输出打分 | 是 |
| Agent 长会话 | 直接对导入的 Agent 会话记录评分 | 是 |
| 一致性 | 同一输入多次调用，测输出稳定性 | 可选 |
| 成功率 | 统计调用成功比例 | 否 |
| RAG 检索 | 精确率/召回率等 6 指标 | 部分指标 |
| 二分分类 | 标签比对，算准确率/灵敏度/特异度 | 否 |
| 响应耗时 | 逐条响应时间对照可配阈值（均值 / p50 / p90 / p99） | 否 |
| 插件评测 | 导入插件包做静态检测（7 项子分），标准模式追加裁判层 rubric 评审 | 仅标准模式 |
| 安全评测 | 双通道红队：内置攻击样本库（提示注入/越狱/敏感内容/事实性断言，每类 ≥30 条）攻击被测模型，确定性检测器（中文 PII/敏感词）+ 裁判 rubric 双通道判定，支持变异增强与 deepeval 指标开关、合规证据报告导出（markdown / xlsx） | 是（rubric 通道） |

对话评测不单独成类型——多轮口径并入「Agent 长会话」评测配置。

## 文档

- [完整使用文档（含截图）](docs/user_guide.md)
- [快速开始](QUICK_START.md)
- 后端启动后访问 `/api-docs`（Swagger UI）

## 安全说明

- `AUTH_JWT_SECRET` 必须显式配置强随机值（`openssl rand -hex 32`）：缺失时服务启动
  直接失败（fail-fast）；「由 DATABASE_URL 派生」的回退需显式设
  `AUTH_JWT_ALLOW_DERIVED=true`，仅限本地开发机，企业部署严禁开启
- 分享链接（M5）token 为 192bit 随机、库内只存 sha256 哈希（明文仅创建时返回
  一次）；公开报告端点免登录但有 Redis 限流（10 req/min/IP），过期/吊销/不存在的
  token 响应完全一致（404 同体，不可枚举）——专项测试见
  `tests/security/test_share_links.py`
- `SCRIPT_EXEC_MODE=local`（前处理脚本直跑）仅限开发机，企业部署禁止；合规通道为
  `container` 沙箱模式，见 [docs/build-pipeline.md](docs/build-pipeline.md) 第九节
- anc-hub 端点默认严格校验 TLS 证书；自签名内网端点可显式设 `AIEVO_HUB_TLS_INSECURE=1` 跳过
- 本平台按设计会调用用户配置的模型端点（含内网网关）——请部署在你控制的信任边界内

## License

[Apache-2.0](LICENSE)
