# AgentEvo — AI 评测平台

对 LLM / Agent 的响应质量做系统化、可量化评测的自研平台。覆盖「准备 → 评测 → 分析」完整流程：模型配置、数据集管理、评测前处理脚本、9 种评测类型、结果聚合与统计分析、A/B 对比测试。FastAPI + Vue 3 + Celery，一个 Docker Compose 栈即可部署。

![首页](docs/assets/00-home.png)

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
| 裁判引擎 | Claude Code 裁判、DeepSeek Harness 裁判、通用 LLM 裁判，评测维度动态配置 |
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
- `.env` 里设 `COMPOSE_PROFILES=bundled-db` 可一并启动内置 PostgreSQL

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

## 文档

- [完整使用文档（含截图）](docs/user_guide.md)
- [快速开始](QUICK_START.md)
- 后端启动后访问 `/api-docs`（Swagger UI）

## 安全说明

- 生产必须显式配置强随机的 `AUTH_JWT_SECRET`（缺省派生密钥仅限本地开发）
- anc-hub 端点默认严格校验 TLS 证书；自签名内网端点可显式设 `AIEVO_HUB_TLS_INSECURE=1` 跳过
- 本平台按设计会调用用户配置的模型端点（含内网网关）——请部署在你控制的信任边界内

## License

[Apache-2.0](LICENSE)
