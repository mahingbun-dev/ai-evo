# AgentEvo — 私有化部署的 Agent 质量工程平台

私有化部署的 Agent 质量工程平台，定位「评测即门禁」——模型 / Agent 的变更先过评测、再进发布。当前已可用：模型配置、数据集管理、评测前处理脚本、8 种评测类型、终态校验器（脚本 / 数据库 / 文件 / 回调断言，确定性校验优先，M2 起）、轨迹匹配（agent 会话工具序列断言：严格 / 乱序 / 子序列 / 禁止多余四模式 + 期望 / 禁用调用，聚合出轨迹匹配率门禁指标，M2 起）、pass^k 统计（裁判类任务行级多 Trial 2-5 + 一致性/成功率重复次数，τ-bench 口径的 pass@1 / pass^k / 分数标准误，M2 起）、A/B 显著性检验（多轮同输入配对：Wilcoxon 符号秩 + 配对 t + Cliff's delta + McNemar 精确检验，M2 起）、结果聚合与统计分析、A/B 对比测试；门禁闭环已随 M1 落地：任务级阈值门禁（结果页判定横幅 + 执行历史门禁列）、按 scope 授权的 API Key、Webhook 通知、开发者设置控制台、CI 命令行 `aievo-cli`（退出码 0=通过 / 1=门禁失败 / 2=错误，GitHub/GitLab/Jenkins 模板见 `ci-templates/`）。FastAPI + Vue 3 + Celery，一个 Docker Compose 栈即可部署。

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
| 数据集管理 | Excel 导入（动态字段）、Agent 会话文件导入、媒体字段、字段级校验 |
| 脚本库 | 评测前处理脚本（zip / 文件夹包）、`model.py` 契约声明、在线文件树编辑、脚手架模板 |
| 评测任务 | AI 裁判员 / Agent 长会话 / 一致性 / 成功率 / RAG 检索 / 二分分类等类型，批量运行、实时进度（WebSocket） |
| 裁判引擎 | Claude Code 裁判（默认）与通用 LLM 裁判图可用；DeepSeek Harness 裁判适配就绪、任务流 M8 接通；评测维度动态配置 |
| 结果与分析 | 评分明细、历史对比、Excel/JSON/CSV 导出、趋势统计、双模型 A/B 并排对比 |
| 管理 | 团队与用户启停（超级管理员）、审计日志、内置使用文档 |

本地账号密码登录：首个注册用户自动成为超级管理员；密码 PBKDF2-HMAC-SHA256 哈希存储，会话为 HS256 JWT（`Authorization: Bearer`）。

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
- `SCRIPT_EXEC_MODE=local`（前处理脚本直跑）仅限开发机，企业部署禁止；合规通道为
  `container` 沙箱模式，见 [docs/build-pipeline.md](docs/build-pipeline.md) 第九节
- anc-hub 端点默认严格校验 TLS 证书；自签名内网端点可显式设 `AIEVO_HUB_TLS_INSECURE=1` 跳过
- 本平台按设计会调用用户配置的模型端点（含内网网关）——请部署在你控制的信任边界内

## License

[Apache-2.0](LICENSE)
