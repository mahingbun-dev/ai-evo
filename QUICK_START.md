# 快速开始

5 分钟在本地跑通平台并完成一次评测。

## 前置条件

- Python 3.12+（建议 venv）
- Node.js 18+ 与 pnpm
- PostgreSQL 14+ 与 Redis 6+

> 只有 Docker？直接看下面的「一分钟演示」，无需本地 Python/Node。

## 一分钟演示（Docker 一键起栈）

```bash
cp .env.example .env
cat >> .env <<'EOF'
AIEVO_DEMO_MODE=true
COMPOSE_PROFILES=bundled-db
DATABASE_URL=postgresql://ai_evo:ai_evo_dev@postgres:5432/ai_evo
EOF
docker compose up -d --build
```

打开 `http://localhost:8100` 注册（首个用户自动成为超级管理员），首页点
「一键体验：加载示例数据并跑一次演示评测」即可零凭据看到第一份评测结果。
演示模式会自动生成 JWT secret（日志高亮提示），数据库迁移也自动执行；
**生产部署必须显式配置 `AUTH_JWT_SECRET` 并关闭 `AIEVO_DEMO_MODE`**。

## 1. 安装与配置

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

编辑 `.env`，至少设置：

```bash
DATABASE_URL=postgresql://<user>:<password>@127.0.0.1:5432/ai_evo
REDIS_URL=redis://127.0.0.1:6379/0
AUTH_JWT_SECRET=<python -c "import secrets; print(secrets.token_hex(32))">
```

## 2. 建表

```bash
# 连接串经环境变量提供（或编辑 alembic_local.ini，该文件不入库）
export AIEVO_DATABASE_URL="postgresql://<user>:<password>@127.0.0.1:5432/ai_evo"
alembic upgrade head
```

## 3. 启动后端

```bash
python scripts/run_local_dev.py          # 默认 http://0.0.0.0:8100
# 热重载开发：python scripts/run_local_dev.py --reload
```

注意：这个脚本只起 API 进程。评测任务是 Celery 异步任务，本地跑评测需要另开一个终端起 worker：

```bash
export $(grep -v '^#' .env | xargs)      # 载入 DATABASE_URL / REDIS_URL
celery -A app.celery_app worker --loglevel=info
```

## 4. 启动前端

```bash
cd frontend
pnpm install
pnpm dev                                  # https://localhost:3000（自签证书）
```

## 5. 注册并完成第一次评测

1. 打开 `https://localhost:3000`，切到「注册」创建账号（首个用户自动成为超级管理员）
2. 「模型配置」添加被测模型与裁判模型（支持 curl 导入）
3. 「数据集管理」用模板 Excel 导入评测数据（`数据集模板.xlsx`）
4. 「评测任务」新建 AI 裁判员评测并运行，实时进度推送
5. 「结果展示」查看评分明细与导出；「统计分析」看趋势

更完整的功能说明（含截图）：[docs/user_guide.md](docs/user_guide.md)。

## 常见问题

- **401 被踢回登录页**：token 过期或 `AUTH_JWT_SECRET` 变更，重新登录即可
- **注册入口消失**：部署方关闭了 `AUTH_REGISTER_ENABLED`，联系管理员建号
- **alembic 报连接失败**：确认 `AIEVO_DATABASE_URL` 环境变量或 alembic.ini 的连接串指向可达的 PostgreSQL
- **接口文档**：后端启动后访问 `/api-docs`
