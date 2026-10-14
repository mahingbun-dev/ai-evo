# Grafana dashboard（占位）

ARCH-2-3（设计 §2.3 D7 / §3 Phase 2 表 2-3 行）本批只接编排：Grafana 容器
+ Prometheus 数据源预置（`provisioning/datasources/`，启动即免手工添加）。
dashboard JSON 属后续迭代——首版看板直接在 Grafana UI 里按下面的指标清单
 Explore/拼板，成熟后导出 JSON 放到本目录并在
 `provisioning/dashboards/` 加载入配置（当前未建，建目录时参照 datasources
 的 provisioning 结构）。

## 首版看板建议的指标清单（与代码同源）

| 面板 | PromQL | 来源 |
|---|---|---|
| 队列深度（三队列） | `aievo_celery_queue_depth` | api 进程 Redis llen 刷新（app/main.py lifespan） |
| 任务时长 P50/P95 | `histogram_quantile(0.5/0.95, sum by (le, queue) (rate(aievo_celery_task_duration_seconds_bucket[15m])))` | worker（celery signals） |
| 任务失败率 | `sum by (job) (rate(aievo_celery_task_failures_total[15m])) / clamp_min(sum by (job) (rate(aievo_celery_task_duration_seconds_count[15m])), 1e-9)` | worker（与 alert.rules 同式） |
| 沙箱启动延迟 | `histogram_quantile(0.95, sum by (le) (rate(aievo_sandbox_start_latency_seconds_bucket[15m])))` | worker（script_container 打点，driver 标签） |
| 抓取健康 | `up` | Prometheus 自身 |

访问：compose 起 monitoring profile 后 http://<host>:3000（默认 admin/admin，
`GRAFANA_ADMIN_PASSWORD` 可覆盖）。
