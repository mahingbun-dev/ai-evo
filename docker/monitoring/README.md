# monitoring 编排（ARCH-2-3，设计 §2.3 D7 / §3 Phase 2 表 2-3 行）

Prometheus + Grafana + alertmanager 三件套，compose profile `monitoring`
可选启用——既有部署不设 profile 时行为不变（离线部署机未预载镜像不受影响）：

```bash
# .env 持久启用（与其他 profile 同机制，如 COMPOSE_PROFILES=monitoring,sandbox）
COMPOSE_PROFILES=monitoring
# 或一次性
docker compose --profile monitoring up -d
```

## 文件

| 文件 | 用途 |
|---|---|
| `prometheus.yml` | 抓取目标（api:8100 + worker/worker-ingest:9108）+ 告警评估 |
| `alert.rules` | 最小告警：队列积压（AiEvoQueueBacklog）、任务失败率（AiEvoTaskFailureRateHigh） |
| `alertmanager.yml` | 占位 receiver（不外发；接 W13 通道时部署侧覆盖） |
| `grafana/` | 数据源预置 + dashboard 占位 README |

## 指标暴露拓扑（代码侧单一事实源：app/celery_app.py ARCH-2-3 注释块）

- **api 容器** `GET /metrics`（:8100）：`aievo_celery_queue_depth`（api 进程
  每 15s Redis llen 刷新）+ 运行时指标；端点与 /health 同层匿名可达。
- **worker 容器** `:9108`（compose 注入 `PROMETHEUS_WORKER_METRICS_PORT`）：
  `aievo_celery_task_duration_seconds`（直方图）、`aievo_celery_task_failures_total`
  、`aievo_sandbox_start_latency_seconds`；prefork 子进程埋点经
  `PROMETHEUS_MULTIPROC_DIR`（tmpfs）聚合。

## 镜像 tag（内网源）

默认 tag 均为官方源稳定版（`prom/prometheus:v2.53.5`、
`grafana/grafana:11.6.0`、`prom/alertmanager:v0.28.1`），内网镜像源缺货时经
`.env` 的 `PROMETHEUS_IMAGE` / `GRAFANA_IMAGE` / `ALERTMANAGER_IMAGE` 换源
换 tag（与既有 `POSTGRES_IMAGE` 同机制）。

## 告警演练（表 2-3 验收）

1. 起栈后向 eval 队列灌 > 50 条任务并停掉 worker（`docker compose stop worker`）；
2. 等 10 分钟 `for` 窗口，Prometheus UI（:9090 → Alerts）确认
   `AiEvoQueueBacklog` firing，alertmanager UI（:9093）收到分组告警。
