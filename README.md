# AgentEvo — AI 评测平台

对 LLM / Agent 的响应质量做系统化、可量化评测的自研平台。覆盖「准备 → 评测 → 分析」完整流程：模型配置、数据集管理、评测前处理脚本、9 种评测类型、结果聚合与统计分析、A/B 对比测试。

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

完整功能说明与截图见 [docs/user_guide.md](docs/user_guide.md)。

## 快速开始（本地开发）

依赖：Python 3.12+、Node.js 18+（pnpm）、PostgreSQL、Redis。

```bash
# 1. 安装后端依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置环境（.env 不入库）
cp .env.example .env
#   编辑 .env：DATABASE_URL / REDIS_URL / AUTH_JWT_SECRET（生产必填，见下文）

# 3. 建表（数据库迁移；连接串经 AIEVO_DATABASE_URL 或 alembic_local.ini 提供）
alembic upgrade head

# 4. 启动后端（默认 8100）
python scripts/run_local_dev.py

# 5. 启动前端（另一终端）
cd frontend && pnpm install && pnpm dev   # https://localhost:3000，/api 代理到 8100
```

打开 `https://localhost:3000` → 注册账号（**首个注册用户自动成为超级管理员**）→ 登录使用。

## 登录与鉴权

- 本地账号密码登录，注册即用；密码 PBKDF2-HMAC-SHA256 哈希存储，会话为 HS256 JWT（`Authorization: Bearer`），WebSocket 握手经 `?token=` 传递
- 关键环境变量（见 `.env.example`）：
  - `AUTH_JWT_SECRET` — JWT 签名密钥，**生产必须显式配置**（缺省时基于 `DATABASE_URL` 派生，仅限本地开发）
  - `AUTH_REGISTER_ENABLED` — 注册开关（`false` 时仅已建账号可登录）
  - `AUTH_TOKEN_EXPIRE_HOURS` — 登录态有效期（默认 168 小时）
- 管理能力：超级管理员可在「团队管理」禁用/启用用户账号

## 部署（Docker Compose）

```bash
# 服务器上准备 .env（参考 .env.example），然后：
docker compose up -d    # api + worker + redis；前端构建产物由镜像内 FastAPI 托管
```

构建与离线部署细节见 [docs/build-pipeline.md](docs/build-pipeline.md)。

**可选集成**：「从 anc-hub 导入」执行包功能对接 MCP 中台——可部署开源的 [plugin-hub](https://github.com/mahingbun-dev/plugin-hub) 并把 `ANC_HUB_MCP_ENDPOINT` 指向其 MCP 面；对接契约与内部 anc-hub 完全一致。

## 项目结构

```
├── app/            # FastAPI 后端（api/ services/ models/ middleware/ tasks/）
├── frontend/       # Vue 3 + Vite + Element Plus 前端
├── alembic/        # 数据库迁移
├── scripts/        # 本地开发与运维脚本
├── tests/          # pytest 单元/E2E 测试
├── tools/          # 构建/离线包工具
├── docker-compose.yml
└── docs/           # 使用文档（user_guide.md）与部署手册
```

## 开发

```bash
# 后端单测
pytest tests/ -q --ignore=tests/e2e

# 本地后端（热重载）
python scripts/run_local_dev.py --reload

# 前端（Vite dev，自签 HTTPS）
cd frontend && pnpm dev
```

接口文档：后端启动后访问 `/api-docs`（Swagger UI）与 `/docs`（内置使用文档页）。

## License

仅供学习与内部工程实践参考。
