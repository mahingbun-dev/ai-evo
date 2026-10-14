# 多机扩容部署：评测机 compose 模板（ARCH-3-1）

本文档描述评测平台的**多机横向扩容**：把 `worker-eval + executor` 成对部署到
一台或多台「评测机」，与主控机组成双机/多机拓扑。设计依据：
`plans/2026-10-15-企业级评测系统架构设计.md` §3 Phase 3 表 3-1 行
（「多机扩容：worker+executor 成对部署到评测机（同镜像、连同一 Redis/PG），
script_runs 用本机盘（热 IO 不走网络），仅脚本库/数据集缓存走共享存储」）。

> 前置阅读：`docs/build-pipeline.md`（主控机的离线构建/部署流水线、沙箱面
> 部署第十节、卷属主迁移 4.6 节）。评测机是主控机拓扑的**增量**，不是替代。

---

## 一、拓扑与角色

```
                 ┌─────────────── 主控机（docker-compose.yml，现状不动） ───────────────┐
                 │  api :8100   worker(-Q eval)   worker-ingest   executor   egress    │
                 │  中心 PostgreSQL :5432          中心 Redis :6379（broker/配额/结果） │
                 │  uploads/（共享存储导出源：脚本库 + 数据集缓存 + 归档）               │
                 └─────────▲──────────────────────────────▲───────────────────────────┘
                 NFS/共享存储（读写同一份）          中心 Redis/PG（局域网直连）
                           │                              │
   ┌───────────────────────┴──────────────────────────────┴─────────────────────────┐
   │ 评测机 N 台（docker-compose.scale.yml，每台一份相同模板）                         │
   │  worker-eval（CELERY_WORKER_QUEUES=eval）+ executor（成对）+ egress + volume-init│
   │  /app/uploads           ← 共享存储（嵌套挂载的外层）                              │
   │  /app/uploads/script_runs ← ★本机盘（嵌套挂载遮蔽共享面，热 IO 不走网络）         │
   └────────────────────────────────────────────────────────────────────────────────┘
```

| 角色 | 所在机器 | 说明 |
|---|---|---|
| `api` / `worker` / `worker-ingest` | 主控机 | 现状不变（`docs/build-pipeline.md`）。主控机 worker 与评测机 worker-eval **同消费 eval 队列**，经中心 Redis 竞争分流 |
| 中心 PostgreSQL / Redis | 主控机（或独立 DB 机） | 评测机经 `.env.scale` 的 `DATABASE_URL` / `REDIS_URL` 占位直连（发布与鉴权要求见 §4.3） |
| `worker-eval` + `executor` | **每台评测机**，成对 | 成对是硬约束：executor 起的沙箱容器挂载的工作区，必须与 worker 眼中 `/app/uploads/script_runs` 是**同一份本机盘目录**；跨机拆对会让沙箱落盘与 worker 读盘漂移。executor 仍是本机 compose 内**唯一**挂 `docker.sock` 的角色（ARCH-1-3 信任边界不变） |
| `egress` | 每台评测机（profile `sandbox`） | 沙箱 `egress` 档在本机闭环：本机 daemon 上的 `aievo-sandbox-egress` 内网 + 本机 squid 白名单（与主控机同一份 `docker/egress/` 模板）。各评测机 daemon 相互独立，同名网络互不相干 |
| `volume-init` | 每台评测机 | 卷属主迁移一次性服务（ARCH-0-1 同款），挂载视图与 worker-eval 完全一致（含嵌套 script_runs） |

**评测机不跑的东西**：api、worker-ingest（ingest/maint 车道留主控机——hub 导入
高峰不占评测吞吐，这也是 ARCH-2-1 队列拆分的意义）、build-service、监控三件套、
本地 PostgreSQL/Redis。评测机是纯 eval 执行面。

---

## 二、队列语义（与 ARCH-2-1 一致）

- 队列拆分的单一事实源在 `app/celery_app.py`（`TASK_ROUTES` / `TASK_QUEUE_NAMES`
  = `eval|ingest|maint`）。**worker 的消费队列绑定不走 `-Q` 命令行**
  （`docker-entrypoint.sh` 的 worker 分支固定 celery 命令），由
  `CELERY_WORKER_QUEUES` 环境变量在配置面表达——`docker-compose.scale.yml` 的
  worker-eval 固定 `CELERY_WORKER_QUEUES=eval`（与主控机 worker 同队列），
  拼错队列名容器启动即 fail-fast（`_resolve_task_queues` 校验），不会静默
  消费错队列。
- **任务分流**：同一 eval 队列的多个消费者（主控机 worker + 各评测机
  worker-eval）经中心 Redis broker 竞争消费，任务自动落到空闲 worker；
  eval 队列内的优先级路由（ARCH-2-2，人工调试 > 批量）对全部机器生效。
- **拔机接手**（设计 3-1 验收口径之二）：评测机掉线时，其已 ACK 未完成的
  任务经 broker 可见性超时（`visibility_timeout=7800s`，≥ 单任务 2h 上限，
  W10-1）重投，由其余 worker 接手；排队中的消息本就留在中心 Redis。优雅
  停止（`docker compose stop worker-eval`，7200s 优雅窗口）则跑完当前任务
  再退，不产生重投。

---

## 三、共享存储边界（设计 3-1 的核心约定）

`uploads/` 树按 IO 特征一刀两断：

| 子树 | 存放 | 理由 |
|---|---|---|
| `uploads/pre_judge_scripts/`（脚本库）、`uploads/datasets/` `hub_files/` `media/`（数据集/媒体缓存）、`uploads/archives/`（任务级归档，ARCH-2-4） | **共享存储**（NFS 或对象存储） | 只读为主、低频写；评测机要读到与主控机同一份脚本包与数据集；归档由执行任务的 worker 写回共享面，主控机 api 可读 |
| `uploads/script_runs/`（每次执行的工作区，ARCH-2-4/D5） | **本机盘** | 每行数据的前处理脚本热 IO（解包、读写产物、沙箱 stdout/stderr 落盘），走网络会把评测时延放大一个数量级，且任务结束即清/归档、无需跨机共享 |

**实现方式（嵌套挂载遮蔽）**：评测机 compose 对 worker-eval 与 executor 统一：

```yaml
volumes:
  - ${UPLOADS_VOLUME:?…}:/app/uploads                      # 外层：共享存储
  - ${SCRIPT_RUNS_VOLUME:-./script_runs}:/app/uploads/script_runs   # 内层：本机盘遮蔽
```

docker 按更深挂载点优先解析，`/app/uploads/script_runs` 即本机盘目录——
executor 的沙箱参数白名单（`executor/sandbox_params.py` `WORKSPACE_ROOT` =
`/app/uploads/script_runs` 子树 realpath 校验）无需任何代码改动。

运维要点：

1. **`UPLOADS_VOLUME` 无缺省、必填**（compose `:?` fail-loud）：漏配直接起不来。
   缺省本地目录会让脚本库/数据集缓存与主控机**静默分叉**（任务找不到脚本包），
   比 fail-loud 危险得多。
2. **NFS 属主**：平台进程固定 uid/gid 10002（ARCH-0-1）。NFS 建议 `sec=sys`
   且两端 uid 一致；volume-init 会递归纠属主（首次较慢，低峰执行），NFS 禁止
   chown 时只告警不阻断，真实可写性由 worker-eval 启动预检把关（语义与主控机
   4.6 节一致）。SMB/CIFS 需按挂载 uid 映射（`uid=10002`）。
3. **script_runs 必须落本机盘**：缺省 bind `./script_runs`；若评测机部署目录
   本身在 NFS 上，显式设 `SCRIPT_RUNS_VOLUME=script_runs` 用命名卷（docker 卷根
   `/var/lib/docker/volumes` 在本机盘）。本机盘水位需自行监控（工作区评完即清、
   任务级归档后删除，常态占用低，长任务积压时才涨）。
4. **同一台评测机内严禁混用**：worker-eval 与 executor 的两层挂载由同一份
   compose 变量驱动，天然一致；不要给其中任何一个服务单独改挂载。

> 远期（设计「远期：K8s 驱动」章节的扩展点 2）：`uploads/script_runs/` 的读写
> 已收敛在 `app/services/workspace.py` 的 `WorkspaceBackend` 接口，届时整体切
> 对象存储（MinIO）只换该模块 backend 实现、DB 存引用，本节「本机盘遮蔽」
> 届时整体退役；worker/api 零改动。

---

## 四、网络与安全边界（与 ARCH-1-3 一致）

1. **docker.sock 只给 executor**：评测机上 executor 仍是唯一挂 sock 的角色
   （无凭证、无 env_file、参数白名单、`EXECUTOR_TOKEN` fail-closed），
   worker-eval 永不挂。每台评测机独立核对 sock 组 gid
   （`stat -c %g /var/run/docker.sock` → `DOCKER_GROUP_GID`）。
2. **沙箱网络档位不变**：`SCRIPT_NET_PROFILE` 语义与主控机完全一致
   （档位表见 `docs/build-pipeline.md` 10.3）：`none` 默认零出网；`egress` 档
   加入本机 daemon 上钉死裸名的 `aievo-sandbox-egress` internal 网络，唯一
   出路是本机 egress 代理。`tests/unit/test_compose_scale_topology.py` 断言
   该网络名与 `NET_PROFILES["egress"]` 逐字同步。
3. **`SCRIPT_EXEC_MODE=container` 缺省**（ARCH-1-4）：评测机模板同款接线；
   runner 镜像需按 §5 预载。
4. **中心面的局域网暴露（本节是新增攻击面，必须做完再上量）**：
   - **PostgreSQL**：主控机 `DATABASE_URL` 指向的实例须监听评测机网段
     （bundled-db profile 默认已发布 `5432`；外部 PG 按其既有暴露面确认），
     并用防火墙把源限定为评测机 IP 清单。
   - **Redis**：主控机 compose 的 `redis` 服务默认**不发布端口**。多机扩容时
     在主控机部署目录放一份 `docker-compose.override.yml`（部署侧文件，不入库）：

     ```yaml
     services:
       redis:
         ports:
           - "6379:6379"
         command: ["redis-server", "--requirepass", "<强口令>"]
     ```

     redis 镜像默认**无鉴权**，绝不能只加 ports 不加口令就暴露到局域网；
     同时主控机 `.env` 的 `REDIS_URL` 须同步改为
     `redis://:<强口令>@redis:6379/0`（celery broker 对带口令 URL 原生支持）。
     评测机侧 `REDIS_URL` 指向 `redis://:<强口令>@<主控机IP>:6379/0`。
     防火墙把 6379 源限定为评测机 IP 清单（口令 + 限源双保险）。
   - 中心 PG/Redis 走局域网直连（非 HTTP 协议，不受 worker 侧
     `WORKER_EGRESS_PROXY` 影响）；评测机 worker 自身外呼（模型测连/OSS）
     是否走本机 egress 由 `WORKER_EGRESS_PROXY` 灰度开关决定，缺省直出，
     与主控机口径一致。

---

## 五、镜像与文件交付（沿用 build-service / LAN tar 流程）

评测机**不跑 build-service**（那是主控机的活，见 `docs/build-pipeline.md`
§二/§四）。镜像沿用同一条离线交付链，只是终点从「GitLab → 主控机」延长为
「主控机 → 评测机」：

1. **镜像清单**（全部经 `docker save` → 内网 scp/U 盘 → `docker load` 预载，
   与 `infra-images.tar.gz` 的 LAN 交付机制一致）：
   - `ai-evo:<tag>`——主控机 build-service 构建出的主镜像原样导出：
     `docker save ai-evo:${IMAGE_TAG} | gzip > ai-evo.tar.gz`；
   - `ai-evo-executor:<tag>`（`docker/Dockerfile.executor`）；
   - `ai-evo-script-runner:<tag>`（container 模式 runner，离线预载步骤见
     `docker/script-runner/README.md`；digest 钉扎经 `SCRIPT_RUNNER_IMAGE`）；
   - `ubuntu/squid:<tag>`（egress 代理）。
2. **版本一致性**：评测机 `.env.scale` 的 `IMAGE_TAG` / `EXECUTOR_IMAGE_TAG` /
   `SCRIPT_RUNNER_IMAGE` 与主控机 `.env` 同值。同一 eval 队列的消费者必须同
   版本——两机 tag 不一致等于混跑两个版本的任务行为面。升级时先主控机后
   评测机（或反向）都行，但**升级窗口内两机都在线时以新 tag 为准尽快对齐**，
   回滚同理（改 tag + 重启即回滚，与主控机 §五 回滚口径一致）。
3. **部署目录文件**：评测机部署目录（建议 `/apps/qa-code/ai-evo-eval`，与
   主控机目录分开，避免误用主控机 `.env`）需要：
   `docker-compose.scale.yml`、`.env.scale`（从 `.env.scale.example` 复制填写）、
   `docker/egress/`（squid.conf + whitelist.txt，compose 按相对路径挂载；
   企业清单同主控机 §10.4 运维）。`docker-entrypoint.sh` / 应用代码都在镜像内，
   无需同步源码。
4. **NFS 挂载**：主控机导出 uploads 树，评测机挂到 `UPLOADS_VOLUME` 指定路径
   （§3 运维要点 2）。

---

## 六、部署步骤（单台评测机）

```bash
# 0. 前置：docker 组 gid、NFS 挂载、镜像预载（§4.1/§5）已完成
mkdir -p /apps/qa-code/ai-evo-eval && cd /apps/qa-code/ai-evo-eval
# 1. 放置 docker-compose.scale.yml 与 docker/egress/（§5.3），然后：
cp .env.scale.example .env.scale && chmod 600 .env.scale
vim .env.scale        # 填：中心 DATABASE_URL/REDIS_URL、AUTH_JWT_SECRET、
                      #     UPLOADS_VOLUME（NFS 挂载点）、EVAL_NODE_NAME、
                      #     EXECUTOR_TOKEN（与主控机同值）、DOCKER_GROUP_GID
# 2. 干跑校验（不出网、不起容器，插值与 env_file 都能解析即通过）：
docker compose --env-file .env.scale -f docker-compose.scale.yml config >/dev/null && echo OK
# 3. 上电（volume-init 先纠属主，worker-eval/executor/egress 随后）：
docker compose --env-file .env.scale -f docker-compose.scale.yml up -d
# 4. 验证：
docker compose --env-file .env.scale -f docker-compose.scale.yml ps   # 三个服务 healthy/running
docker logs ai-evo-worker-eval 2>&1 | grep -E "sync|ready"            # 注册到中心 Redis、队列含 eval
```

主控机侧（一次性，§4.4）：放 `docker-compose.override.yml` 发布带口令的
Redis、确认 PG 监听与防火墙限源，然后 `docker compose up -d redis`。

日常运维：

- **升级**：评测机 `docker load` 新镜像 → 改 `.env.scale` 的 `IMAGE_TAG` →
  `docker compose --env-file .env.scale -f docker-compose.scale.yml up -d`
  （stop 的 7200s 优雅窗口内跑完当前任务再换容器）。
- **扩容/缩容**：加一台评测机 = 重复 §六；减一台 = `docker compose stop` 后
  关机，任务接手机制见 §二（compose 形态下的 worker 扩缩容即由此覆盖，
  无需 K8s/KEDA——设计「不做清单」）。
- **保活**：主控机的 `scripts/ensure_worker.sh` cron 点名 `ai-evo-worker`
  容器名，**不覆盖**评测机的 `ai-evo-worker-eval`；评测机按同款思路自备
  cron（点名换成 `ai-evo-worker-eval`），或依赖 `restart: unless-stopped`
  自愈（容器进程崩溃自动拉起，覆盖绝大多数故障面）。

---

## 七、验收（设计 3-1 行的验收口径）

1. **双机压测吞吐近线性**：主控机 + 1 台评测机在线，投递批量评测任务
   （同一批任务集分别在「仅主控机」与「双机」各跑一轮），对比总完成时间——
   期望接近减半（偏差主要来自中心 PG/Redis 与模型 API 的共享瓶颈）。观察
   `docker logs` 中两机 worker 各自领走的任务量大致对半分流。
2. **拔掉一台评测机，任务被另一台接手**：评测机上有任务执行中时
   `docker compose --env-file .env.scale -f docker-compose.scale.yml stop`
   （或直接断电模拟硬掉线）：优雅 stop 场景当前任务跑完再退；硬掉线场景
   未完成任务经 `visibility_timeout`（7800s）重投，由主控机 worker 接手。
   验证：任务最终态无 stranded，中心 Redis 队列被消化到空。
3. **共享存储边界生效抽查**：评测机跑一个含前处理脚本的任务，确认
   `script_runs/` 产物落**本机盘**（评测机上 `ls ./script_runs/<task>/`），
   任务归档出现在共享面 `uploads/archives/` 且主控机可见；脚本包/数据集
   从共享面读取（评测机本地无 `uploads/pre_judge_scripts` 独立副本）。

---

## 八、已知边界与不做清单

- 评测机是**无状态执行面**：本地 `script_runs` 在硬掉线后残留（任务重投后
  在接手机器上重建工作区），残留目录由任务清理逻辑与定期维护收口，属预期。
- 中心 Redis/PG 是多机拓扑的共享单点：可用性要求高时按企业既有 DB/缓存
  高可用方案兜底（平台侧不自带，与单机形态口径一致）。
- 不引入 K8s（2026-10-15 决策）：compose 形态的多机 worker 已覆盖扩缩容
  需求；K8s 预留扩展点见设计「远期：K8s 驱动」章节（SandboxDriver 接口 +
  工作区路径抽象），接入时本模板退役、worker/api 零改动。
