# 告警与健康检查（W13）

> 整改计划 §3 W13 交付物。覆盖：告警 webhook、离线告警兜底与升级路径、
> 日志轮转、深健康检查 `GET /api/v1/health/deep`、compose healthcheck、
> `/api/v1/auth/status` 状态语义修正。

## 1. 告警

### 1.1 触发点

| 触发点 | 级别 | 位置 |
|---|---|---|
| 评测任务 Celery 执行失败（`on_failure`） | error | `app/tasks/celery_tasks.py` |
| 部署 verify 校验失败（容器未就绪 / 深健康探活不过） | error | `tools/build-service/build_service.py` |

### 1.2 配置：`ALERT_WEBHOOK_URL`

- 环境变量，**缺省为空 = 禁用 webhook**（不产生任何网络请求）。
- 配置后，上述触发点会向该地址 POST 一条 JSON（`Content-Type: application/json`）：

```json
{
  "level": "error",
  "title": "评测任务失败",
  "details": {"celery_task_id": "...", "evaluation_task_id": 123, "error": "..."},
  "source": "celery",
  "timestamp": "2026-10-13T10:00:00+0800"
}
```

- 主应用在部署目录 `.env` 里配置即可；build-service 是独立容器（不挂 `.env`），
  需在 `docker run` 时加 `-e ALERT_WEBHOOK_URL=...`（见 `tools/build-service/Dockerfile` 顶部运行示例）。
- 5 秒超时；非 2xx 或异常只记 warning，**不影响业务主流程与部署流程**。

### 1.3 离线兜底与升级路径

无论 webhook 是否配置/成功，每条告警都同步落一份 JSON Line 审计记录：

- 主应用：`<部署目录>/logs/alerts.log`（容器内 `/app/logs/alerts.log`，已挂宿主卷）
- build-service：`<workspace>/build-service/data/logs/alerts.log`

**离线环境暂无 IM 出口时的升级路径**：接入企业 IM（钉钉/企微/飞书）群机器人后，
把机器人 webhook 地址填入 `ALERT_WEBHOOK_URL` 并重启对应服务即可生效，
**无需改任何代码**——报文契约（level/title/details）即为此设计；
历史告警可在 `logs/alerts.log` 中按 JSON Line 追溯。

## 2. 日志轮转（W13）

- 主应用文件日志（`logs/auth_middleware.log`）由 `FileHandler` 改为
  `RotatingFileHandler`（10MB × 10 个备份，上限约 110MB），`app/main.py`。
- auth 链路（`app.middleware.auth_middleware` / `app.services.auth_service`）日志级别
  DEBUG → INFO（此前每个请求都刷 SSO 请求/响应明细）。
- compose 三服务（api / worker / hub-plugin）补 docker 日志轮转：
  `logging: driver: json-file, max-size: 50m, max-file: 3`（单服务上限约 150MB）。

## 3. 深健康检查 `GET /api/v1/health/deep`

浅探活（进程活着）之上的四个真实依赖探针，四探针并发执行（单探针超时 3s，
接口总耗时 ≈ 单探针耗时，小于 build-service verify 的 5s HTTP 超时）：

| 探针 | 内容 | 失败表现 |
|---|---|---|
| `database` | `SELECT 1` | `down` + error |
| `redis` | Redis ping | `down` + error |
| `migrations` | `alembic_version` 表内容与迁移 heads 比对 | `mismatch`（漂移）/ `down`（表不存在等） |
| `uploads` | uploads 目录写删临时文件 | `down` + error |

- 全绿 → `200 {"status": "healthy", "checks": {...}}`；
  任一失败 → `503 {"status": "degraded", "checks": {...}}`（明细在 `checks`）。
- 该路径在鉴权白名单内（`auth_middleware.WHITELIST_PREFIXES` 的 `/api/v1/health`），
  探活请求不带业务 cookie。
- **部署验收语义**：`build_service.HEALTH_URL` 已从恒 200 的 `/api/v1/auth/status`
  切换为本端点——部署 verify 现在能真正拦住「容器起来了但 DB/Redis/迁移版本不对」。
  注意：引入新迁移的版本部署后，须按 `docs/build-pipeline.md` 执行
  `docker exec -it ai-evo-api alembic upgrade head`，否则深健康报 `mismatch`、verify 失败。

### 3.1 compose healthcheck

三服务均补 `healthcheck`（interval 60s，连续 3 次失败标记 unhealthy）：

- **api**：python urllib 请求本机深健康，非 200 即失败（镜像内无 curl）。
- **worker**：`celery -A app.celery_app inspect ping -d celery@$HOSTNAME`——
  定点问本 worker，无响应 = 僵死/丢注册；Redis 不可达同样失败。
- **hub-plugin**：hubkit 是 gRPC 服务、无可 GET 的健康路由，退化为 TCP 端口探活
  （`127.0.0.1:9500` 可连接即存活；host 网络下即宿主回环）。

## 4. `/api/v1/auth/status` 状态语义修正（W13）

修复「恒 200」问题，未登录 / 凭证无效 / SSO 不可达时**如实反映**：

| 场景 | 旧行为 | 新行为 |
|---|---|---|
| 未登录（无 cookie） | 200 `is_authenticated: false` | **401** `{"is_authenticated": false, "user": null, "message": "未提供认证信息"}` |
| cookie 无效 / SSO 校验失败 | 200 `is_authenticated: false` | **401** `{"is_authenticated": false, "user": null, "message": "认证失败，请重新登录"}` |
| 已登录 | 200 | 200（不变） |

`is_authenticated` 字段保留，前端逻辑兼容；401 会触发前端 axios 拦截器跳 SSO 登录
（`frontend/src/api/index.js`），与既有未登录处理路径一致。
部署冒烟清单（`docs/整改开发计划-20261013.md` §6.3）中
「`GET /api/v1/auth/status` → 200」自 W13 起改为：
**带登录态访问 → 200；匿名访问 → 401**（401 同样证明服务可达）。

## 5. 运维速查

```bash
# 深健康（部署后冒烟第一件事）
curl -s http://127.0.0.1:8100/api/v1/health/deep | python3 -m json.tool

# 容器健康状态（compose healthcheck 结果）
docker compose ps

# 最近告警记录
tail -n 20 logs/alerts.log

# 配置告警 webhook（主应用：部署目录 .env 追加一行后重启）
echo 'ALERT_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx' >> .env
```
