# 构建与部署流水线

本文档描述 ai-evo 的 GitLab CI/CD 流程，以及服务器首次初始化、日常运维与故障排查。

> 目标环境：CentOS 7 部署机（**不能下载任何东西**，无外网、不能装软件），
> 因此整套流程围绕「离线构建」设计。

---

## 一、为什么要这么绕

原先的发版链路是：

```
开发机（Mac, buildx 模拟 amd64）
  → docker build → docker save 打成 461MB 的 ai-evo-deploy.tar.gz
  → 手工上传服务器 → docker load → docker compose up -d
```

问题：全程手工、强依赖开发机、产物还提交进了 git（`.git` 已经 1.3GB）。

新链路把构建搬到部署机上，但部署机既不能装 Node/pnpm/docker CLI，也不能 `pip install`。
所以做法是：

1. **工具链烘焙进镜像** —— `ai-evo-builder` 里预装 Node/pnpm/docker CLI，一次性构建后导出到服务器；
2. **依赖变成离线快照** —— Python wheel 与 pnpm store 在开发机生成，通过 GitLab Packages 分发；
3. **CI 不构建** —— GitLab runner 只调用服务器上常驻的 `build-service`，然后轮询状态。

第 3 点是关键：如果让 CI job 直接跑构建，job 超时/被取消时构建容器会残留在服务器上，
并发流水线还会互相踩。把构建交给常驻服务后，CI 侧变成一段极薄的 curl + 轮询。

---

## 二、架构

```
GitLab push main
   │
   └─> runner（tags: anc-build，本身就在部署机上）
         │  curl POST /api/v1/build  (X-Build-Token)
         │  然后轮询 GET /api/v1/jobs/{id}
         ▼
   build-service 容器（常驻，:8181）
         │
         ├─ git checkout 到隔离构建工作区 /apps/qa-code/ai-evo-build
         ├─ 变更分类：相对「线上已部署 commit」（data/deployed_commit）diff，
         │    变更全部落在 frontend/ 内 → frontend-only 模式（2026-10-16 起）
         ├─ 校验 .offline-snapshot.json 与锁文件哈希是否一致
         │    └─ 不一致 → 从 GitLab Packages 下载离线快照
         ├─ docker run ai-evo-builder（挂载 docker.sock）
         │    ├─ pnpm install --offline && pnpm build   → frontend/dist
         │    └─ docker build -t ai-evo:<短sha>         → 宿主 daemon
         ├─ trivy 镜像扫描（ARCH-3-3）：HIGH+ 且有修复版本 → 构建失败
         │    （不写 release 记录，有漏洞的镜像不可部署/回滚，见第十一节）
         └─ deploy：同步 docker-compose.yml → 写 .env 的 IMAGE_TAG
                     → docker compose up -d → 校验容器与接口
                     → 同步 frontend/dist 到 nginx 静态根（宿主机目录）
```

### 前端-only 快速通道（2026-10-16 起）

变更相对**线上已部署 commit** 全部落在 `frontend/` 内时，走快速通道：

- **构建**：只跑 `pnpm install --offline && pnpm build`（跳过应用镜像构建、
  跳过 claude/git bin 就位），release 记录 `mode=frontend`、tag 为 `frontend-<短sha>`；
- **部署**：只把 dist 同步到 nginx 静态根（备份+原子切换，复用既有
  `sync_frontend`），**容器零重启、数据库不动、api/worker 全程在线**，
  整条流水线从十几分钟缩到三几分钟。

判定在 build-service 端做（CI 零改动），基准是「线上已部署 commit」而不是
MR diff——语义是「相对线上正在运行的版本改了什么」，能兜住「前序后端 MR
部署失败后、紧跟的前端 MR 把后端变更漏掉不部署」的场景。「已部署 commit」
记录在 `build-service/data/deployed_commit`，每次部署成功（含前端-only 与
回滚）后写入；releases 记录在 build 成功时就写入、不能当基线用。

- 无基线记录 / 基线 commit 不在仓库历史（如 force push）/ diff 失败 →
  一律按全量处理。误判为 full 只是多做一次镜像构建，误判为 frontend 会
  漏部署后端变更，所以判定偏保守。
- Web 手动触发构建时传 `force_mode=full` 可强制全量（调试判定问题用）。
- 回滚不受影响：显式指定 tag 的回滚一律走全量路径；前端应急回退也可
  手工恢复 `frontend/dist.bak-<时间戳>`（保留最近 3 份）。

**为什么 builder 容器要挂 `/var/run/docker.sock`**：镜像在 builder 容器里构建，但通过挂载的
socket 直接 build 进宿主机 daemon，省掉了 `docker save` / 传输 / `docker load` 这一整条链路。
代价是这个容器对宿主 docker 有完全控制权——它是一次性专用环境，可接受，但需要知晓。

---

## 三、服务器目录约定

| 路径 | 用途 |
|---|---|
| `/apps/qa-code/ai-evo` | 生产工作区 + compose 运行目录（`docker-compose.yml`、`.env`） |
| `/apps/qa-code/ai-evo/frontend/dist` | **nginx 的前端静态根**（`eval-frontend.conf` 的 `root`），deploy 时由 build-service 同步 |
| `/apps/qa-code/ai-evo-build` | 隔离构建工作区，build-service 独占（git checkout / clean 都在这里） |
| `/apps/qa-code/ai-evo-build-cache` | 跨构建复用的缓存目录 |
| `/apps/qa-code/ai-evo/build-service/config.toml` | build-service 配置（含凭据，权限 600） |
| `/apps/qa-code/ai-evo/build-service/data` | `releases/` 版本记录、`logs/` 任务日志、`deployed_commit` 已部署基线 |

> ⚠️ **前端有两个位置，别只更新一个**：nginx 直接托管宿主机上的
> `/apps/qa-code/ai-evo/frontend/dist`，而镜像里也有一份 dist 由容器的 FastAPI 托管。
> 用户实际访问的是 nginx 那份。首次部署时只更新了容器内的，导致「流水线成功但页面上
> 看不到新功能」。build-service 的 `[deploy].frontend_root` 配的就是这个宿主机路径，
> 部署时会把构建产物同步过去（同步前自动备份旧 dist）。
>
> ⚠️ **nginx 配置不由 CI 管理**：`/etc/nginx/conf.d/eval-frontend.conf` 是服务器本地文件，
> 改了路由（比如新增 WebSocket 路径）需要手工修改并 `nginx -t && nginx -s reload`。
> 静态文件目录不需要 reload（每次请求都读磁盘），所以只换前端产物无需干预 nginx。

构建工作区与生产工作区**必须分开**：生产工作区里有运行中的 compose 和 `.env`，
在上面 `git checkout --force` + `git clean -fdx` 会直接搞坏线上环境。

### 与 anc 项目共用一台部署机

同一台机器上还跑着 anc 项目的构建流水线，两者必须互不干扰。已经处理过的冲突：

| 资源 | anc | ai-evo | 说明 |
|---|---|---|---|
| build-service 端口 | `8180` | `8181` | anc 的 build-service 常驻 8180 |
| build-service 镜像名 | `build-service:latest` | `ai-evo-build-service:latest` | **镜像名相同会让 `docker load` 顶掉 anc 的 tag，anc 容器一旦重启就会启动错误的镜像、打断它的流水线** |
| builder 镜像名 | `anc-builder:latest` | `ai-evo-builder:latest` | 不冲突 |
| runner tag | `anc-build` | 复用同一个 | 同机，无需新注册 |
| hub 插件端口 | anc-hub 插件家族 `9000/9100/9200/9300/9400` | `hub-plugin` 用 `9500` | ai-evo 侧口子沿用 anc-hub 插件段位分配，见下文「hub-plugin 服务」 |

**不要**把本项目的镜像改成 `build-service:latest` 这类通用名——那是给 anc 用的。

---

## 四、首次初始化

### 4.1 准备 GitLab 侧的凭据

**Deploy Token（一个就够）**

项目里已有名为 `ci/cd` 的 Deploy Token（`gitlab+deploy-token-1`），其 scope 是
`read_repository` + `read_package_registry` + `write_package_registry`：

- **上传离线快照**（开发机）用它 —— 因为含 `write_package_registry`；
- **下载离线快照**（服务器）也用它 —— 用 `read_package_registry`。

该 token 的鉴权方式（已实测通过，上传返回 201、回读内容一致）：

```
用户名：gitlab+deploy-token-1
密码：  <token 值>
```

> 脚本里用户名是**默认值**，可用 `GITLAB_UPLOAD_USER` 覆盖。
> 注意 Deploy Token 只支持 Basic 鉴权，用 `PRIVATE-TOKEN` 头会 401。

> 若该 token 被吊销，重新创建时请勾选上面三个 scope，并同步更新服务器
> `config.toml` 的 `[gitlab].deploy_token_user/pass`。

**还需确认两项**

1. **runner 的 `anc-build` tag 对本项目可见**
   已确认：本项目可用 runner 为 `anvx-15-54-build`（id 1987，IP <runner-host>，
   与部署机同机），其 `tag_list` 是 `["anc-build"]`，`run_untagged` 为 **false**。

   > ⚠️ 因为 `run_untagged: false`，**`.gitlab-ci.yml` 里的 `tags: [anc-build]` 不能删**——
   > 删掉之后 job 没有任何 runner 会接，会一直停在 pending。

2. **CI/CD Variable**：GitLab → 项目 → Settings → CI/CD → Variables
   新增 `BUILD_API_TOKEN`，值与服务器 config.toml 里的 `[auth].token` 一致
   （建议 32 位以上随机串），勾选 Masked 与 Protected（`main` 是保护分支，可以勾）。

### 4.2 生成离线快照（开发机，需联网 + Docker）

```bash
./tools/prepare-offline.sh
```

产出 `requirements.lock`、`.wheels/`、`frontend/.pnpm-store/`、`.offline-snapshot.json`。
然后上传到 GitLab Packages：

```bash
GITLAB_UPLOAD_TOKEN=<4.1 里的 Deploy Token 值> ./tools/upload-offline-snapshot.sh
```

把 `requirements.lock` 与 `.offline-snapshot.json` 提交进仓库（其余产物已被 `.gitignore` 忽略）。

> **防忘机制**：CI 在每个分支的 push 上都跑 `check-snapshot` job（`tools/check-snapshot.py`，
> 校验锁文件哈希与 `.offline-snapshot.json` 一致），脱节时 pipeline 直接红灯并输出修复步骤，
> 把问题拦在 push 阶段而不是 main 构建失败后。服务器侧 build-service 以快照元数据的
> 内容哈希为版本号（内容寻址）：`.offline-snapshot.json` 提交进仓库后，构建时发现
> 工作区快照版本不符会**自动**从 GitLab Packages 同步新快照，无需登服务器。

### 4.2.1 查项目 ID

`config.toml` 的 `[gitlab].project_id` 必须是**数字 ID**：Deploy Token 没有
`read_project` 权限，用项目路径查询 API 会返回 `404 Project Not Found`
（实测确认，换成 `PRIVATE-TOKEN` 头则返回 401）。两种查法：

**方式一：GitLab 页面**

打开 `https://gitlab.example.com/group/project` →
左侧 **Settings → General** → 展开 **Naming, topics, avatar** 区块 → 顶部显示 `Project ID: xxxxx`

**方式二：用 api 令牌查询**（需要 4.1 里第 1 个令牌）

```bash
curl -sS -H "PRIVATE-TOKEN: <api令牌>" \
  "https://gitlab.example.com/api/v4/projects/group%2Fproject" \
  | python3 -c "import json,sys;print('项目 ID =', json.load(sys.stdin)['id'])"
```

### 4.3 构建两个基础镜像（开发机，一次性）

```bash
./tools/build-and-export-infra.sh
```

产出 `infra-images.tar.gz`（含 `ai-evo-builder:latest`、`ai-evo-build-service:latest`
与**基础镜像 `python:3.12-slim`**）。这三个镜像低频变更——只有升级 Node/pnpm/docker CLI、
改 build-service 代码，或应用 Dockerfile 的 `FROM` 变更时才需要重建。

> ⚠️ **基础镜像必须一起带过去**：`docker build` 的 `FROM python:3.12-slim` 要求该镜像
> 已存在于本地 daemon，而部署机拉不到外网。漏了它，构建会卡在 `FROM` 拉取上。
> 它虽然不显眼，但确实是构建的依赖。

上传到 GitLab Packages 中转（部署机只能从内网 GitLab 下载）：

```bash
GITLAB_UPLOAD_TOKEN=<4.1 里的 Deploy Token 值> ./tools/upload-infra-images.sh
```

### 4.4 在服务器上初始化

把 `infra-images.tar.gz` 传到服务器，然后：

```bash
# 1. 加载基础镜像
tar xzf infra-images.tar.gz
docker load -i infra-images.tar

# 2. 准备目录
mkdir -p /apps/qa-code/ai-evo-build
mkdir -p /apps/qa-code/ai-evo-build-cache
mkdir -p /apps/qa-code/ai-evo/build-service/data

# 3. 写 build-service 配置
cd /apps/qa-code/ai-evo/build-service
cp config.example.toml config.toml   # 从仓库里拷出来
vi config.toml                        # 填 auth.token / gitlab.project_id /
                                      # deploy_token_user / deploy_token_pass
chmod 600 config.toml

# 4. 修正应用的环境变量
#    服务器上已有 .env（含 PROJECT_NAME / API_V1_STR 等应用配置），
#    不要拿 .env.example 覆盖它，否则会丢掉这些配置。需要做两件事：
#      a) 修格式：docker compose 的 env_file 解析不接受 `KEY = value`（等号前空格）
#      b) 补 REDIS_URL：docker-compose.yml 已改为从 env_file 读，environment 段不再提供
cd /apps/qa-code/ai-evo
cp -a .env ".env.bak.$(date +%Y%m%d%H%M%S)"          # 先备份
grep -nE '^[A-Za-z_][A-Za-z0-9_]* +=' .env           # 列出格式有问题的行
vi .env                                              # 改成 KEY=value；
                                                     # 确认有 DATABASE_URL、
                                                     # REDIS_URL=redis://redis:6379/0
                                                     # 与 AUTH_JWT_SECRET（见下）；
                                                     # 变量清单见 4.4.1
chmod 600 .env
docker compose config >/dev/null && echo ".env 格式校验通过"
```

> ⚠️ **`.env` 的格式要求**：docker compose 的 `env_file` 解析比 python-dotenv 严格得多。
> `KEY = value`（等号前有空格）、行尾空格等写法会让 `docker compose up -d` 直接报
> `invalid env file ... contains whitespaces` 而启动失败。
> 部署机上现有的 `.env` 实测就有 **3 行**是这种写法，必须修掉。
>
> ⚠️ **`.env` 必须在首次部署前就备好**。部署流程是「同步 compose → 写 IMAGE_TAG →
> `compose up`」：如果 `.env` 不存在，`IMAGE_TAG` 那一步会新建一个只含 IMAGE_TAG 的
> 文件，`compose up` 随即失败，而 compose 已经被仓库版本覆盖——等于把正在运行的服务
> 搞坏。build-service 已加前置校验（`validate_env_file`），缺 `DATABASE_URL` 或
> `REDIS_URL` 时会在动任何文件之前直接失败。
>
> ⚠️ **`AUTH_JWT_SECRET` 必须显式配置（ARCH-0-2）**：JWT 签名密钥已不再从
> `DATABASE_URL` 派生。`.env` 缺这项时 api 容器启动即 fail-fast（该校验在应用层；
> build-service 的前置护栏只查 `DATABASE_URL`/`REDIS_URL`，缺密钥会在容器启动阶段
> 暴露为 crash loop，被部署校验「容器未全部 running」拦截）。生成方式见
> 下文「JWT 密钥配置与轮换」。

```bash
# 5. 启动 build-service
#    必须用 --network host：部署后的健康检查访问 http://127.0.0.1:8100/...，
#    那是宿主机上 api 容器发布的端口；默认 bridge 网络下 <runner-host> 指向容器自己，
#    探活必然失败（实测确认）。用宿主网络后无需再 -p 映射。
docker run -d --name ai-evo-build-service --restart unless-stopped \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /apps/qa-code/ai-evo:/apps/qa-code/ai-evo \
  -v /apps/qa-code/ai-evo-build:/apps/qa-code/ai-evo-build \
  -v /apps/qa-code/ai-evo-build-cache:/apps/qa-code/ai-evo-build-cache \
  -v /apps/qa-code/ai-evo/build-service/config.toml:/app/config.toml:ro \
  ai-evo-build-service:latest

# 6. 验证
curl -s http://localhost:8181/api/v1/health
# 期望: {"status":"ok","time":...}
```

### 4.4.1 部署 .env 变量清单（ARCH-0-4）

服务器 `.env` 由部署方维护（不入库、600 权限），compose 经 `env_file` 整体注入
api/worker 容器。部署相关变量一览（✅ 必填、⛔ 严禁设置；标注 ARCH-* 的条目为
2026-10 架构改造新增变量或本次收口强调项）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL 连接串（build-service 前置护栏也校验它） |
| `REDIS_URL` | ✅ | compose 网络内通常为 `redis://redis:6379/0` |
| `AUTH_JWT_SECRET` | ✅ | JWT 签名密钥（ARCH-0-2）。缺失即拒启，生成与轮换见「JWT 密钥配置与轮换」 |
| `AUTH_JWT_ALLOW_DERIVED` | ⛔ | **企业部署严禁开启**（ARCH-0-2）。仅开发机 `.env_local` 可显式设 true 保留派生回退 |
| `SCRIPT_EXEC_MODE` | 见「九」 | 前处理脚本执行模式（ARCH-0-4；ARCH-1-4 起缺省 `container`，`local` 仅开发机显式 env）——灰度与回退步骤见第九节 9.1 |
| `SCRIPT_RUNNER_IMAGE` | 可选 | `container` 模式 runner 镜像（默认 `ai-evo-script-runner:latest`），离线预载见 `docker/script-runner/README.md`；自 ARCH-1-5 支持 digest 钉扎 `<repo>@sha256:<64hex>`（取法与通道限制见 11.4，ARCH-3-3 对齐口径） |
| `SCRIPT_RUNNER_USER` | 可选 | `container` 模式容器内运行用户（默认 `10001:10001`）；Linux 部署机需与挂载目录属主匹配 |
| `IMAGE_TAG` | 自动 | CI 部署时自动写入当前短 sha；手工回滚即改回旧值 |
| `TMPFS_TMP_SIZE` / `TMPFS_HOME_SIZE` | 可选 | `/tmp`、`/home/app` 两个 tmpfs 的容量上限（ARCH-0-1，默认各 1g，只按实际写入占内存） |
| `LOGS_VOLUME` / `UPLOADS_VOLUME` | 可选 | 日志/上传卷形态切换（默认 bind 到 compose 目录下；群晖等经代理工具执行 compose 的场景设为命名卷 `logs` / `uploads`） |
| `SANDBOX_WORKSPACE_HOST_ROOT` | 命名卷部署必填 | 沙箱 bind 源宿主根：docker daemon 按**宿主路径**解析沙箱 `-v` 挂载源，而 worker 视角工作区路径是 `/app/uploads/script_runs/...`——uploads 用 bind 且宿主/容器路径一致时可留空（缺省 = 现状）；`UPLOADS_VOLUME=uploads`（命名卷形态，见上行）时宿主不存在该路径，daemon 会静默新建空目录（沙箱空工作区、stdout/stderr 日志回不来），必填 uploads 命名卷的宿主真实根，如 `/volume1/docker/ai-evo/uploads-data`（映射与白名单单一事实源 `executor/sandbox_params.resolve_mount_source`） |
| `AIEVO_PORT` | 可选 | api 对外端口（默认 8100） |
| `COMPOSE_PROFILES` | 可选 | 逗号组合：`bundled-db` 一并启动内置 PostgreSQL；`sandbox` 启动沙箱面 executor + egress 与 aievo-sandbox-egress 内网（ARCH-1-3，见第十节；生产 CI 部署默认都不设） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_PORT` | 可选 | 仅 `bundled-db` profile 生效 |
| `EXECUTOR_TOKEN` | 沙箱面必填 | worker↔executor 共享鉴权 token（ARCH-1-3/D6）。留空 = executor fail-closed；`openssl rand -hex 32` 生成，仅存 `.env` |
| `EXECUTOR_URL` | 沙箱面必填 | worker 侧 HTTP 驱动（ARCH-1-2）访问地址，启用沙箱面后设 `http://executor:8300`（compose 内网服务名）；留空 = 驱动不可用 |
| `DOCKER_GROUP_GID` | 沙箱面必填 | 部署机 docker 组 gid（ARCH-1-3）：executor 以补充组访问 docker.sock。查法 `stat -c %g /var/run/docker.sock`，默认 999 仅 Debian 系常见值 |
| `EXECUTOR_IMAGE_TAG` / `EGRESS_IMAGE` | 可选 | 沙箱面镜像 tag/源覆盖（默认 `latest` / `ubuntu/squid:latest`）；离线部署机需预载，见第十节 |
| `EGRESS_SQUID_CONF` / `EGRESS_WHITELIST_FILE` | 可选 | egress 策略/域名白名单文件路径覆盖（默认仓库 `docker/egress/` 模板，企业清单用后者指向服务器自有文件，见 10.4） |
| `WORKER_EGRESS_PROXY` / `WORKER_NO_PROXY` | 可选 | worker 外呼（模型测连/OSS）走 egress 代理的灰度开关（ARCH-1-3）：留空不走；启用设 `http://egress:3128`，内网例外经 `WORKER_NO_PROXY` 追加 |
| `BACKUP_DIR` / `RETENTION_DAYS` / `UPLOADS_RETENTION_DAYS` | 可选 | 备份输出与保留策略（见「备份与恢复」） |
| `REMOTE_DATABASE_URL` / `LOCAL_DATABASE_URL` | 可选 | `scripts/sync_database.py` 手动运维用 |
| `AIEVO_HUB_ADDR` / `AIEVO_HUB_ADVERTISE_ADDR` / `AIEVO_API_BASE` | 可选 | hub-plugin 拓扑变化时覆盖（同机默认回环直连） |
| `NODE_IMAGE` / `PY_IMAGE` / `PIP_INDEX_URL` / `NPM_REGISTRY` | 可选 | 自建在线构建（`Dockerfile.online`）的构建期换源参数 |

应用层可选项（`AUTH_TOKEN_EXPIRE_HOURS`、`AUTH_REGISTER_ENABLED`、`OSS_*`、
`LANGFUSE_*`、`ANC_HUB_MCP_ENDPOINT`、`ANC_HUB_BEARER_TOKEN`、`HUB_SSO_COOKIE_NAME`、
`UPLOAD_MAX_BYTES`、`DB_ASSERT_READONLY_DSN`、`DEEPEVAL_*` 等）的完整清单见
`.env.example` 与 `app/core/config.py`。

### 4.5 首次流水线

在 GitLab 上发起一次 Web 触发（CI/CD → Pipelines → Run pipeline，选 `main` 分支），
两个 job 手动点击执行。第一次会同步离线快照（数百 MB），耗时较长。

### 4.6 首次部署的卷属主迁移（volume-init，ARCH-0-1）

主镜像以 uid/gid **10002**（app 用户）+ `cap_drop ALL` 运行（设计 0-1），进程自身
无法 chown；而 `uploads/`、`logs/` 两个写面是运行时挂卷，属主不跟随镜像——
docker 自动创建的缺失 bind 目录是 root:root，历史部署的卷内容也是 root 写入的。
因此**无需（也不应）手工 chown**：每次 `docker compose up -d` 时由 compose 的
`volume-init` 一次性服务在主服务之前自动完成迁移：

- 它以 root 运行但只保留 `CHOWN` / `DAC_OVERRIDE` 两个能力，无网络、根文件系统
  只读、不挂 `.env`（凭证与本服务无关）——迁移逻辑在 `docker-entrypoint.sh` 的
  volume-init 分支；
- 把两个卷的属主递归纠正为 `10002:10002`；已全部是 10002 时只读遍历、零写入秒退
  （幂等，每次 up 都跑一遍，代价可忽略）；
- api/worker 通过 `depends_on: service_completed_successfully` 等它成功退出再启动；
- **`docker compose ps` 里 `ai-evo-volume-init` 显示 `exited(0)` 属正常**，一次性
  服务本该退出，不是故障、不需要重启它。

历史部署升级到本版本同样自动迁移：旧 root 属主内容会在下一次 `compose up -d` 时
被纠正；`uploads/` 内容较多时递归 chown 需要时间，建议低峰期执行首次升级。

**SMB/NFS 等禁止 chown 的卷**：volume-init 对 chown 失败只告警不阻断（这类卷按
挂载 uid 映射本就可能可写）；真实可写性由 api/worker 的启动预检把关——不可写时
容器立即退出并打印修复命令，而不是起来之后在第一次写日志/上传时抛
PermissionError。手动修复等价于预检提示的两条命令：

```bash
# bind 挂载目录（宿主执行）
chown -R 10002:10002 /apps/qa-code/ai-evo/uploads /apps/qa-code/ai-evo/logs

# 命名卷（经临时容器执行）
docker run --rm -v <卷名>:/d alpine chown -R 10002:10002 /d
```

---

## 五、日常运维

### 触发部署

push 到 `main` 自动触发；也可以在 GitLab 上 Web 手动触发（调试用）。

### 查看进度与日志

构建过程中 GitLab job 日志会实时打印 build-service 汇报的阶段与进度。
失败时 after_script 会自动把 build-service 侧的任务日志尾部打回 GitLab 日志。

也可以直接在服务器上查：

```bash
# 最近的任务（container 日志里有 job_id）
docker logs --tail 100 ai-evo-build-service

# 某个任务的完整日志
cat /apps/qa-code/ai-evo/build-service/data/logs/<job_id>.log
```

### 回滚

镜像按 commit 短 sha 打 tag，回滚就是切回旧 tag，不需要重新构建。

```bash
# 1. 看有哪些可回滚的版本
curl -s -H "X-Build-Token: $TOKEN" http://localhost:8181/api/v1/releases | python3 -m json.tool

# 2. 确认目标镜像还在宿主机上
curl -s -H "X-Build-Token: $TOKEN" http://localhost:8181/api/v1/images | python3 -m json.tool

# 3. 回滚
curl -s -X POST http://localhost:8181/api/v1/rollback \
  -H "X-Build-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"tag": "<目标短sha>"}'
```

手工回滚等价于：把 `/apps/qa-code/ai-evo/.env` 的 `IMAGE_TAG` 改成目标 sha，
然后在 `/apps/qa-code/ai-evo` 执行 `docker compose up -d`。

> **注意**：回滚只切镜像，**不会回滚数据库**。如果新旧版本之间执行过 alembic 迁移，
> 回滚镜像后需要自行确认 schema 兼容性。

### 旧镜像清理

每次构建产生一个新 tag 的镜像（约 500MB），不清理的话几十次部署就会占满磁盘。
build-service 在**每次部署成功后**清理超期镜像，保留策略是取并集：

- 最近 `keep_releases` 个构建的 tag（默认 5）
- `.env` 里当前正在使用的 tag
- `releases/` 记录里出现过的 tag

另外 docker 自身会拒绝删除正在被容器使用的镜像，这是最后一道保险。
清理动作会记入任务日志（`[prune] ...`）。把 `keep_releases` 设为 0 可关闭清理。

### 数据库迁移

**部署自动执行**（2026-10-15 起）：build-service 在部署流程里 compose up 之后、verify 之前，
在 api 容器内执行 `alembic upgrade head`（重试 3 次，失败终止部署）。此前是「不随部署自动
执行」，结果 2026-10-14（W19 f1a9c5e3b7d2）与 2026-10-15（plugin_config b3c9d7e2f1a4）两次
带新迁移的 MR 合入后，health/deep 的 migrations 探针比对 alembic_version 落后于代码 heads
判 503，部署 verify 必红一次，都得手动补迁移再重跑 job——所以把升级放进部署流程。

自动迁移只覆盖「拉齐到代码 head」这一步；**破坏性变更（drop/rename）仍需人工评估**：
`DATABASE_URL` 指向多人共用的测试库，这类迁移不要写进常规 MR，先与使用方约窗口。

配套的 CI 门禁分两档，按流水线来源切换：

- **合并前**（MR / 非 main 分支 push）：`check-migrations` job（check 阶段、一次性 PG15
  容器）跑 `tools/check_migrations.py` 完整检查——迁移链分叉/断链、空库全量升级跑不通两项
  **阻断**；模型漂移（`alembic check`，改模型忘写迁移）当前为**告警不阻断**（仓库有漂移存量：
  模型声明 JSON 而迁移建的是 JSONB、列注释、索引命名——json 列迁 jsonb 的 P1 遗留，
  清理后把 `check_migrations.py` 的 `DRIFT_BLOCKING` 改 `True` 收紧为阻断）。
- **合并后**（main、Web 手动触发）：`check-migrations-static` job 跑
  `tools/check_migrations.py --static-only`——纯标准库解析 `alembic/versions/*.py`，只查
  「恰好一个 head、`down_revision` 无断链、revision 不重复」，秒级、不起容器、不装依赖。
  完整检查在合并前已通过，main 上重跑全量只会把分钟级等待串行压在 build 之前；合并后新增的
  风险只有并发合并造成的链分叉，静态检查即可覆盖。迁移本身能否在空库跑通，由部署内的
  `alembic upgrade head` 兜底（失败终止部署）。

本地可同样执行完整检查：

```bash
docker run --rm -d --name mig-check -p 127.0.0.1:55432:5432 \
  -e POSTGRES_PASSWORD=ci -e POSTGRES_DB=migcheck postgres:15-alpine
.venv/bin/python tools/check_migrations.py \
  --database-url postgresql+psycopg2://postgres:ci@127.0.0.1:55432/migcheck
docker rm -f mig-check
```

需要绕过自动迁移手动执行时（如应急），命令不变：

```bash
docker exec -it ai-evo-api alembic upgrade head
```

W6-2 起 `alembic` 的目标库**优先读容器内 `DATABASE_URL`**（`docker exec` 自动继承容器 env，
即跟随部署目录 `.env`）；`alembic.ini` 内只保留占位符，不再硬编码任何库地址——「拿着 ini
里写死的共享 dev 库地址、在错误环境执行迁移」的风险就此消除。

### build-service 自身代码的更新

build-service 的代码（`tools/build-service/build_service.py`）烤在 `ai-evo-build-service:latest`
镜像里，随 ai-evo 仓库演进。2026-10-15 起容器以绑定挂载方式运行该文件：

```
-v /apps/qa-code/ai-evo-build/tools/build-service/build_service.py:/app/build_service.py:ro
```

构建工作区 `/apps/qa-code/ai-evo-build` 在每次构建时 checkout 到目标 commit，因此
**改 build-service 代码的生效流程 = 合入 main → 流水线构建（工作区已同步）→
`docker restart ai-evo-build-service`**，无需重新做镜像/导出/上传。重启前确认没有
构建或部署任务在执行（`GET /api/v1/health` 正常、无 running 任务），重启后用
`docker exec ai-evo-build-service grep -c "新函数名" /app/build_service.py` 确认新代码已加载。

### 备份与恢复

整改前平台零备份：`scripts/` 下没有 pg_dump；`uploads/hub_supplements/`（人工补传区）
无法从 anc-hub 幂等重建，宿主卷一旦损坏，补传文件全部丢失且必须重新人工上传。
W6-1 起交付两个脚本（与 `ensure_worker.sh` 一样，**`scripts/` 不由 CI 同步，
改完需手工同步到部署机**）：

| 脚本 | 用途 |
|---|---|
| `scripts/backup.sh db` | `pg_dump -Fc` 全量备份 PostgreSQL（连接串经 `DATABASE_URL` 注入，口令不落任何文件） |
| `scripts/backup.sh uploads` | `tar -czf` 全量归档 `uploads/hub_supplements/`（每周） |
| `scripts/restore.sh` | 恢复演练：`pg_restore` 到专用临时库 + 校验 `alembic_version` 与源库一致 |

两类备份默认保留 14 天（`RETENTION_DAYS` / `UPLOADS_RETENTION_DAYS` 可调），输出到
部署目录 `./backups/`（`BACKUP_DIR` 可调），过期文件在每次备份成功后自动清理。

**crontab 落地步骤**（部署机手工操作，一次性）：

```bash
# 0) 同步脚本到部署机（scripts/ 不由 CI 同步）
scp scripts/backup.sh scripts/restore.sh <部署机>:/apps/qa-code/ai-evo/scripts/
chmod +x /apps/qa-code/ai-evo/scripts/{backup,restore}.sh

# 1) 确认部署机有 postgresql-client（pg_dump/pg_restore/psql）
command -v pg_dump || echo "需安装 postgresql-client（部署机无外网，走离线包）"

# 2) 编辑 crontab：每日 02:00 备份 DB（从 .env 注入 DATABASE_URL，口令不进 crontab），
#    每周日 03:00 归档补传区（不需要 DATABASE_URL）
crontab -e
#   0 2 * * * cd /apps/qa-code/ai-evo && set -a && . ./.env && set +a && ./scripts/backup.sh db >> logs/backup.log 2>&1
#   0 3 * * 0 cd /apps/qa-code/ai-evo && ./scripts/backup.sh uploads >> logs/backup.log 2>&1

# 3) 次日核对：logs/backup.log 无报错、backups/db/ 出现当日 .dump（非空且 PGDMP 头）
```

**恢复演练步骤**（每季度至少一次，也是备份有效性的唯一证明）：

```bash
cd /apps/qa-code/ai-evo && set -a && . ./.env && set +a

# 1) 取最近一次 DB 备份
ls -lt backups/db/ | head

# 2) 在同实例创建专用演练临时库（严禁把目标写成共享/生产库；
#    restore.sh 会 --clean --if-exists 清空重建目标库同名对象）
psql "postgresql://<user>:<password>@<host>:5432/postgres" \
  -c "CREATE DATABASE ai_evo_restore_drill"

# 3) 恢复 + 自动比对 alembic_version（第 3 个参数为源库 DSN，不一致则非零退出）
./scripts/restore.sh backups/db/db_<最新时间戳>.dump \
  "postgresql://<user>:<password>@<host>:5432/ai_evo_restore_drill" "$DATABASE_URL"

# 4) 清理演练库
psql "postgresql://<user>:<password>@<host>:5432/postgres" \
  -c "DROP DATABASE ai_evo_restore_drill"
```

演练通过的标准：`restore.sh` 输出 `✓ alembic_version 比对一致`。若失败，优先排查
备份链路（`logs/backup.log`、`.dump` 文件大小），再排查演练库权限。

**补传区归档的恢复**（只在补传文件丢失时用）：

```bash
tar -tzf backups/hub_supplements/hub_supplements_<时间戳>.tar.gz   # 先核对清单
tar -xzf backups/hub_supplements/hub_supplements_<时间戳>.tar.gz -C /apps/qa-code/ai-evo/uploads/
```

**口令轮换（依赖 D-1，与共享库各使用方协调窗口后执行）**：

W6-3 已把明文口令从 `alembic.ini`、`alembic_local.ini`（已删除）、
`scripts/sync_database.py`、`app/core/config.py` 四处清除，全部改为读环境变量
（`DATABASE_URL` / `REMOTE_DATABASE_URL` / `LOCAL_DATABASE_URL`）。但 git 历史仍可
翻到旧口令，轮换后才彻底作废：

1. 与共享 dev 库的各使用方约好变更窗口；
2. DBA 执行 `ALTER USER ... WITH PASSWORD '<新口令>'`；
3. 更新部署机 `/apps/qa-code/ai-evo/.env` 的 `DATABASE_URL`（以及各使用方自己的配置）；
4. `docker compose up -d` 重启 api/worker，跑一遍冒烟（登录 + 建任务）确认连通；
5. 通知全组：仓库 git 历史里的两个旧口令已作废，入库残留变为无害字符串。

### JWT 密钥配置与轮换（AUTH_JWT_SECRET，ARCH-0-2）

整改前 JWT 签名密钥在未配置时从 `DATABASE_URL` 派生（config.py 旧逻辑）——数据库
口令与登录态凭证耦合：轮换 DB 口令会静默作废全员登录态，且拿到 DSN 的任何一方
都能本地伪造用户 token。现改为**密钥独立、缺失即拒启**：

- **必填**：`.env` 缺 `AUTH_JWT_SECRET` 时 api 启动直接 fail-fast（uvicorn 日志
  可见「AUTH_JWT_SECRET 未配置」，报错自带生成示例），部署校验拦 crash loop。
- **生成**（部署机无外网也没关系，openssl 是基础工具）：

  ```bash
  openssl rand -hex 32   # 输出写入 .env 的 AUTH_JWT_SECRET= 行
  chmod 600 .env         # .env 含 DB 口令与 JWT 密钥，保持 600
  docker compose up -d   # 重启生效
  ```

- **首次升级到本版本前必须先补密钥**，否则升级后服务起不来。步骤：
  生成 → 写入部署机 `/apps/qa-code/ai-evo/.env` → 再触发部署/重启。
- **轮换**：换成新生成的值后 `docker compose up -d`。全部已签发 token 立即失效
  （用户需重新登录，WebSocket 连接同理），worker 在跑的长任务不受影响（任务链路
  不校验用户 JWT）。建议低峰期执行并提前公告，这与设计验收「轮换演练一次」对应。
- **本地开发**：开发机可在 `.env_local` 显式设 `AUTH_JWT_ALLOW_DERIVED=true`
  保留「由 DATABASE_URL 派生」的回退（启动日志会告警留痕）。该开关仅限开发机，
  企业部署的 `.env` 严禁开启——开启后缺密钥不再拒启，等于退回耦合状态。


### 部署时的任务中断行为

`worker` 服务设置了 `stop_grace_period: 900s`。部署触发 `docker compose up -d` 时：

worker 不再参与 `docker compose up`（只重建 api/hub-plugin），由 build-service
的 `swap_worker` 单独非阻塞换代：

```
compose up -d api [hub-plugin] → 迁移 → verify → 前端同步（旧 worker 全程未动）
→ docker update --restart=no ai-evo-worker        # 防 warm shutdown 退出后被 restart policy 复活
→ docker kill --signal=TERM ai-evo-worker         # 立即返回：celery 停止接新任务、跑完当前任务自行退出
→ 短等 drain_wait_sec（默认 120s）
    ├─ 退出 → docker compose up -d worker          # fast path，换代完成
    └─ 未退出 → 部署立即成功结束，后台 watcher 接管：
          按容器 ID 轮询旧容器退出 → compose up -d worker → 校验新容器 running
          超过 drain_force_kill_sec（默认 7500s）→ docker rm -f 强杀 + 拉起 + 告警
```

行为要点：

- **流水线不再被长任务阻塞**：deploy job 等待上限 900s；旧 worker 收到 TERM 后
  继续跑完当前任务（celery warm shutdown），跑完后由 watcher 拉起新 worker（秒级）。
- **任务零丢失**：drain 期间新任务只在 Redis 队列排队（旧 worker 停止接单），
  api 正常服务；新 worker 就绪后立即接手。worker 容量空窗 = 旧任务剩余时长 +
  秒级拉起，可接受。
- **强制回收**：watcher 超过 7500s（2h celery task_time_limit + 缓冲）会
  `docker rm -f` 旧容器并告警；正常情况下长任务到点被 celery 自杀、worker
  自然退出，轮不到触发。
- **兜底链**：watcher 挂掉（如 build-service 容器重启）→ cron 的
  `ensure_worker.sh`（每 5 分钟）在旧容器退出后拉起新 worker；watcher 拉起
  失败也发告警（webhook / `alerts.log`）。watcher 与 cron 并发 compose up
  幂等，最坏一方报错、下一轮自愈。
- watcher 日志在服务器 `build-service/data/logs/worker-drain-*.log`；
  GitLab 部署日志里出现「worker 换代转后台」即代表走了后台路径，此时 job
  已成功、worker 换代稍后自动完成，无需人工干预。

`stop_grace_period: 7200s` **保持不变**：部署编排不再依赖它，它退化为手工
`compose up/stop` 运维操作时的保护伞（手工重启仍等任务跑完再退出）。

对比旧流程：整条 `compose up -d` 会被 warm shutdown 阻塞最长 7200s，期间
部署流水线一直挂着。更早的 `scripts/do_deploy.sh` 会先 `DEL celery` 清空队列，
正在执行和排队的任务全丢，仅应急使用。

### 与 worker 自愈脚本的关系

部署机上 crontab 每 5 分钟跑一次 `scripts/ensure_worker.sh`，发现 `ai-evo-worker`
不在运行就 `docker compose up -d worker`。

- 部署的 compose up（api/hub-plugin）到校验结束之间，build-service 写
  `/apps/qa-code/ai-evo/.deploy-in-progress`，`ensure_worker.sh` 见到该标记就跳过；
- worker drain 期间旧容器仍在 `docker ps` 里（warm shutdown 进行中），
  自愈脚本判断「在运行」自然不会介入；
- 旧容器退出后，自愈脚本与后台 watcher 都能拉起新 worker（compose 幂等，
  并发触发无害）——脚本零改动，天然构成换代兜底。

> ⚠️ **服务器上的 `scripts/` 目录不由 CI 同步**——build-service 只同步
> `docker-compose.yml` 并维护 `.env`。改了 `scripts/` 下的文件，需要手工同步到
> `/apps/qa-code/ai-evo/scripts/`，否则线上跑的还是旧版本。

### hub-plugin 服务（anc-hub 插件）

compose 第四个服务 `hub-plugin`（与 api/worker 共用镜像，`SERVICE_TYPE=hub-plugin`），
把 ai-evo 核心能力注册为 anc-hub MCP 工具，详见 `docs/hub-plugin.md`。部署相关要点：

- **端口**：`network_mode: host` 监听 `:9500`，对齐 anc-hub 插件家族端口段
  （中台插件自 9000 起按段位分配、逐插件 +100），
  上线前在部署机 `ss -tlnp` 确认空闲。
- **`build-service/config.toml` 的 `[deploy].services` 需手工加 `"hub-plugin"`**
  （默认只有 `api`、`worker`）。该文件在服务器本地、不入库，CI 管不到；不加则
  `verify_deploy` 不校验该容器（部署不失败，但缺了部署校验与守护覆盖）。
- **首次部署该服务的顺序**：插件注册到 hub 需拨通 ai-evo api 才自报健康
  （`/api/v1/auth/status`），而 `compose up -d` 全家一起起——不满足也不怕，
  插件每 5s 重试注册，api 就绪后自动补上；确认方式见插件容器日志的
  `"msg":"已注册到中台"`。
- 与 `ensure_worker.sh` 无关：该脚本只盯 `ai-evo-worker`；hub-plugin 容器靠
  `restart: unless-stopped` 与 hubkit 骨架的自注册重试自愈。


---

## 六、依赖变更流程

改动 `requirements.txt` 或 `frontend/pnpm-lock.yaml` 之后：

```bash
# 1. 重新生成快照（开发机，需联网）
./tools/prepare-offline.sh

# 2. 上传新快照（版本号由 .offline-snapshot.json 的内容哈希决定，天然内容寻址）
GITLAB_UPLOAD_TOKEN=<令牌> ./tools/upload-offline-snapshot.sh

# 3. 提交 requirements.lock 与 .offline-snapshot.json 的变更
git add requirements.lock .offline-snapshot.json && git commit
```

**为什么必须提交这两个文件**：build-service 会比对
`.offline-snapshot.json` 里记录的锁文件哈希与构建工作区里的实际哈希。
不一致就拒绝构建（或自动去 Packages 拉新快照），避免「改了依赖却忘了重跑快照」
导致构建出的镜像与预期依赖不符。

### 改完依赖后如何验证「真的能离线构建」

**必须断网验证**——带网的容器里 pnpm/pip 少东西时能偷偷联网拿到，测试会假通过
（这一点踩过坑：pnpm 的供应链复核就是带网时看不出来的）。

```bash
# 镜像构建（最关键，覆盖 pip 安装与前端产物打包）
docker build --network=none --platform linux/amd64 -t ai-evo:offline-check .

# 前端离线安装与构建（可直接用 builder 镜像，它自带 pnpm）
docker run --rm --network=none --platform linux/amd64 \
  -v "$PWD/frontend:/workspace/frontend" -w /workspace/frontend \
  --entrypoint bash ai-evo-builder:latest \
  -c "pnpm install --offline --frozen-lockfile --config.trustLockfile=true \
      --store-dir /workspace/frontend/.pnpm-store && pnpm build"
```

两者都通过，才算快照是完整的。

### 升级依赖

`requirements.lock` 由 `tools/prod-constraints.txt` 约束生成，该文件是从**现网运行的镜像**
`pip freeze` 出来的完整依赖集。这样设计是为了让「换部署流水线」和「升依赖」两件事解耦——
首次接入 CI 时不会顺带升级上百个包。

要**有意**升级某个依赖：改掉 `tools/prod-constraints.txt` 里对应的行（或删掉该行），
重新跑 `prepare-offline.sh` 生成新 lock，review 版本差异，再单独验证。

### numpy 的特殊约束

`tools/offline-constraints.txt` 把 numpy 锁在 `2.2.6`，这是**硬性要求**而非保守选择：

- 部署 VM 的 CPU 缺少 X86_V2 指令集；
- numpy 2.3.5 起只发布 `manylinux_2_28` wheel（编译时启用 x86-64-v2 指令），装上会直接崩溃；
- 2.2.6 是最后一个提供 `manylinux2014`（基线指令集）wheel 的版本。

原 Dockerfile 里那段 `pip download --platform manylinux2014_x86_64` + `--force-reinstall`
的临时特判，现在被这个约束 + 锁文件取代，行为一致但不再需要联网。

---

## 七、故障排查

| 现象 | 原因与处理 |
|---|---|
| GitLab job 一直 pending | runner 的 `anc-build` tag 对本项目不可见。检查项目 Runners 列表。 |
| job 报 401 | `BUILD_API_TOKEN` 与服务器 `config.toml` 的 `[auth].token` 不一致。 |
| job 报 409 | 已有构建/部署在执行。build-service 串行执行，等前一个结束。 |
| 构建报「离线快照缺失或与锁文件不一致」 | 改了依赖但没重跑 `prepare-offline.sh`，或没上传快照。也可能是 `[gitlab].deploy_token_*` 没配。 |
| 构建在 8% 附近十几秒就失败、日志无「从 GitLab Packages 同步」一行，builder 里 `pnpm install --offline` 缺新依赖 | 工作区残留的是**上一次同步的旧快照**：锁文件哈希与提交里的新元数据匹配，但 `.pnpm-store` 内容还是旧的。`check_offline_snapshot()` 现已用 `.snapshot-version` 标记（sync 时写入、clean 时保留）判断工作区快照版本，版本不符自动重新同步。**部署机上的 build-service 镜像必须更新到含此修复的版本**，否则仍复现。 |
| CI 的 `check-snapshot` job 失败 | push 的提交里锁文件与 `.offline-snapshot.json` 脱节（改了依赖没重跑快照流程），main 构建必然失败，已在 push 阶段拦截。按 job 输出（或 `tools/check-snapshot.py` 本地执行）的三步修复：`prepare-offline.sh` → `upload-offline-snapshot.sh` → 提交 `requirements.lock` 与 `.offline-snapshot.json`。 |
| 构建报「commit xxx 不在 origin/main 历史中」 | 触发的 sha 不在目标分支上（例如从别的分支 Web 触发却选了 main）。 |
| 同上，但**只有第二次及以后**的构建才出现 | `git fetch <url> <branch>` 不会更新 remote-tracking 引用（只写 FETCH_HEAD），若把 repo_url 直接当 remote 名用，后续按 `<remote>/<branch>` 判断祖先关系必然失败。`ensure_build_ws()` 现已统一返回 `origin`；若再出现，先确认构建工作区里 `git remote -v` 的 origin 地址与 `config.toml` 的 `repo_url` 一致。 |
| 流水线成功、容器也换了新镜像，但**页面上看不到新功能** | 前端没同步到 nginx 静态根。nginx 托管的是宿主机 `/apps/qa-code/ai-evo/frontend/dist`，不是容器里那份。确认 `config.toml` 的 `[deploy].frontend_root` 配了、且部署日志里有 `[frontend] 已同步前端到 ...`。另外先让浏览器强刷一次排除本地缓存。 |
| 构建报 `ERR_PNPM_IGNORED_BUILDS` / `pnpm build` 依赖状态检查失败 | pnpm 11 默认禁止依赖执行构建脚本，且把「有脚本但未表态」当错误。仓库里 `frontend/pnpm-workspace.yaml` 已用 `allowBuilds` 把 esbuild / vue-demi 显式声明为 `false`（与开发机现状一致：开发机这两个包同样在 ignoredBuilds 里，`pnpm dev` 一直正常）。**不要删掉这个文件**，也不要只靠 `--config.strictDepBuilds=false`——实测那样 pnpm 仍会自己生成占位文件、构建照旧失败。 |
| 构建卡在「离线安装前端依赖」，日志刷 `registry.npmjs.org ... error (23)` | pnpm 11 的**供应链复核**（`minimumReleaseAge` / `trustPolicy`）在 `install` 时会对锁文件每个条目向 registry 查询，**`--offline` 不阻止这一步**，断网即报 `ERR_PNPM_MINIMUM_RELEASE_AGE_VIOLATION`。`build-in-container.sh` 已传 `--config.trustLockfile=true` 跳过复核（锁文件经 MR 评审、且快照生成时在开发机已复核过），**不要删这个参数**。注意用命令行开关而非写进 `pnpm-workspace.yaml`，避免削弱开发机上的默认保护。 |
| 构建报「pnpm store 中未找到 @esbuild/linux-x64」 | 快照是在非 linux 环境下生成的。`prepare-offline.sh` 已在 linux 容器内执行该步骤，不要绕过它手工 `pnpm fetch`。 |
| `docker compose up -d` 报 `invalid env file ... contains whitespaces` | `.env` 里有 `KEY = value`（等号前空格）等写法。docker 的 env_file 解析比 python-dotenv 严格，见第 4.4 节。 |
| 上传快照报 401 | Deploy Token **只认 Basic 鉴权**，用 `PRIVATE-TOKEN` 头会返回 401（实测确认）。脚本已改用 Basic，用户名默认 `gitlab+deploy-token-1`，可用 `GITLAB_UPLOAD_USER` 覆盖。 |
| 快照解出来文件数翻倍、多出 `._xxx` 文件 | macOS 的 `bsdtar` 默认附带 AppleDouble 元数据。脚本已用 `COPYFILE_DISABLE=1` 关闭，不要绕过脚本手工 `tar` 打包。 |
| 部署报「部署目录缺少 .env」/「缺少必需变量: DATABASE_URL」 | build-service 的前置护栏拦住了。按第 4.4 节把 `.env` 建好再部署——这是为了不在缺配置时覆盖 compose、把线上服务搞坏。 |
| api 容器反复退出，日志报「AUTH_JWT_SECRET 未配置」（ARCH-0-2） | JWT 密钥缺失的应用层 fail-fast（build-service 护栏不查这一项）。按「JWT 密钥配置与轮换」一节：`openssl rand -hex 32` 生成后写入 `.env` 的 `AUTH_JWT_SECRET=`，`docker compose up -d` 重启。 |
| `docker compose ps` 里 `ai-evo-volume-init` 显示 `Exited (0)` | 正常现象：一次性卷属主迁移服务（ARCH-0-1，见 4.6），迁移完成即退出，不是故障、不需要处理。 |
| api/worker 启动即退出，日志报「以下挂卷目录不可写」（ARCH-0-1） | 卷属主非 10002 且 volume-init 的 chown 失败（SMB/NFS 等禁止 chown 的卷）。按日志提示修复：宿主 `chown -R 10002:10002 <uploads/logs 目录>`，命名卷经 alpine 容器执行，详见 4.6。 |
| container 模式任务失败，worker 日志报找不到 docker / 连不上 Docker daemon | ARCH-1-4 起缺省即 container：沙箱面未启用（未设 `COMPOSE_PROFILES=sandbox`、未配 `EXECUTOR_URL`）时 worker 容器内无 docker 通道，任务明确失败（fail-loud）。按第十节启用沙箱面；过渡期可在 `.env` 显式设 `SCRIPT_EXEC_MODE=local` 重启回退（口径见 9.1）。 |
| `docker compose up -d` 卡很久 | worker 正在跑长任务，等它优雅退出。超过 900s 会强杀。 |
| 部署校验报「容器在 180s 内未全部 running」 | 登录服务器 `docker compose logs <service>` 看启动失败原因。 |
| 部署校验报「HTTP 探活失败」 | api 容器起来了但接口不通。先看 `docker logs ai-evo-api`。**另一种常见原因**：build-service 没有用 `--network host` 启动——默认 bridge 网络下容器内的 `127.0.0.1:8100` 指向它自己，永远连不上，必须用宿主网络。 |
| 构建卡在 `FROM python:3.12-slim` | 部署机上缺少基础镜像，而它拉不到外网。基础镜像已包含在 `infra-images.tar.gz` 里，重新下载加载即可。 |
| 磁盘被镜像占满 | build-service 每次部署成功后会清理超期镜像，见「旧镜像清理」。若仍不够，调小 `keep_releases`。 |

---

## 八、已知约束

- **`.git` 不会因为移除构建产物而变小**。`ai-evo-deploy.tar.gz`（461MB）等文件仍在 git 历史里。
  彻底瘦身需要 `git filter-repo` 重写历史 + 全员重新克隆，属独立事项。本次只做了「止血」——
  仓库不再继续膨胀。
- **builder 容器挂载宿主机 docker.sock**，等于把宿主 docker 控制权交给它。属既定架构，
  该容器只用于构建，不应改造成长期运行的服务。
- **本地直接 `docker build` 需要先构建前端产物**：`frontend/dist` 已不入库，
  本地要出包需先 `cd frontend && pnpm build`。日常前端调试仍用 `pnpm dev`，不受影响。
- **构建缓存目录在宿主机上而非 docker 卷**：卷落在 docker root 分区，服务器上余量有限。

### 7.1 应用镜像的离线 claude CLI（重要）

部署机无外网，应用镜像里 claude CLI（CC 裁判引擎依赖）**不能在线安装**——
Dockerfile 里旧的「apt install curl + 在线跑 install.sh」在服务器必然失败
（退出码 100，2026-10-09 main 构建实测踩坑）。现行机制：

- 产物：`tools/build-claude-bin.sh` 在开发机 linux/amd64 容器内预装 claude
  并打包上传 GitLab Packages（包名 `ai-evo-claude-bin`，内容寻址版本号）；
  版本与 sha256 记录在 `tools/claude-bin.json`（入库）。
- 构建时：build-service 按 `claude-bin.json` 下载解压到工作区 `claude-local/`
  （工作区标记 `.claude-bin-version`，已就位则跳过下载），Dockerfile 离线 COPY。
- 升级 claude：`./tools/build-claude-bin.sh --upload`（需 `GITLAB_UPLOAD_TOKEN`，
  走代理时加 `PROXY=http://host.docker.internal:7897`）→ 提交更新的
  `claude-bin.json` → 服务器构建自动换新版本。

---

## 九、脚本执行模式：local 仅限开发机，企业部署禁止（ARCH-0-4；ARCH-1-4 缺省切 container）

评测前处理脚本是平台内唯一的用户代码执行面，`SCRIPT_EXEC_MODE` 决定其执行通道：

| 维度 | `local`（仅开发机显式 env） | `container`（W15 沙箱，ARCH-1-4 起代码默认） |
|---|---|---|
| 运行位置 | worker 容器内子进程直跑 | `docker run --rm` 独立容器 |
| 运行身份与资源 | 与 worker 同身份：ARCH-0-1 后为 uid 10002 + `cap_drop ALL` + 只读根文件系统，可写面收敛 uploads/logs 两卷与 tmpfs；**无 mem/cpu/pids 上限** | 镜像内独立非 root 用户，mem 1g / cpu 0.5 / pids 128 |
| 网络 | 继承 worker 网络（compose bridge，可触达内网） | 默认 `--network none` |
| 平台 import 面 | **PYTHONPATH 注入项目根，脚本可 `import app.*` 拿到平台代码与 DB session**（W1-3 的环境变量白名单只挡 env 泄漏，挡不住 import 面） | 裁剪镜像不含平台代码、无 PYTHONPATH 注入 |

**政策：`SCRIPT_EXEC_MODE=local` 仅限开发机；企业部署禁止以 local 模式执行前处理脚本。**
理由：local 通道里用户代码与平台控制面在同一进程边界内——恶意或失陷脚本可经
import 面拿到 DB session、无资源限制地占死 worker、经 worker 网络触达内网。
ARCH-0-1 的非 root 化收紧了文件系统爆炸半径，但不改变「同边界」这一本质。
compose 里 worker 段的 ARCH-0-1 注释对 local 的描述（过渡期最低隔离）即指此现状。

**企业部署的落地方式（ARCH-1-4 起缺省即合规态）**：

1. 合规状态是 `SCRIPT_EXEC_MODE=container`，且自 ARCH-1-4（设计 §3 Phase 1
   表 1-4 行）起就是**代码默认值**（`app/core/config.py`）——漏配落在
   container，不再有「忘配即 local」的敞口；`local` 仅显式 env 可用（开发机
   `.env_local`、部署机 `.env` 显式声明）。compose 的 worker 段已显式写
   `SCRIPT_EXEC_MODE=${SCRIPT_EXEC_MODE:-container}`：`environment` 优先级
   高于 `env_file`，插值缺省即 container，`.env` 显式设值可覆盖（即回退
   开关，见 9.1）。
2. container 模式有两个前置（与 ARCH-1-4 之前相同）：
   - **部署机离线预载 runner 镜像**（D-3 运维事项）：开发机
     `docker build -f docker/script-runner/Dockerfile` + `docker save` 传输 +
     部署机 `docker load`，步骤见 `docker/script-runner/README.md`「离线预载」；
     换 tag 经 `SCRIPT_RUNNER_IMAGE` 指向，不必改代码；
   - **worker 侧执行通道**：compose 的沙箱面（executor 服务 + egress 出口，
     `COMPOSE_PROFILES=sandbox`）已随 ARCH-1-1/1-3 就位，worker 容器本身
     **永不挂 docker.sock**——container 模式经 worker→executor 的 HTTP 调用
     （条目 1-2 的 SandboxDriver HTTP 驱动 + `EXECUTOR_TOKEN`，启用步骤见
     第十节）。**缺省切换后的行为变化**：沙箱面未启用（未设
     `COMPOSE_PROFILES=sandbox`、未配 `EXECUTOR_URL`）时，含前处理脚本的
     任务会因 worker 容器内无 docker 通道而**明确失败**（fail-loud），不再
     静默落 local 执行；脚本上传校验链的冒烟、debug 试跑与任务执行同开关
     分流，所在容器（api/worker）同口径。
3. **灰度与回退**：缺省切换不等于一步到位全量——按 9.1 的「内测团队 →
   全量」路径推进，任一环节出问题改 env 重启即回退。

### 9.1 灰度与回退（ARCH-1-4）

缺省切 container 后的放量路径（设计 §3 Phase 1 表 1-4 行验收口径）：

1. **前置就绪**：按第十节 10.2 启用沙箱面（`COMPOSE_PROFILES=sandbox` +
   `EXECUTOR_TOKEN` + `EXECUTOR_URL=http://executor:8300` + 镜像预载），
   确认 `ai-evo-executor` / `ai-evo-egress` 两个容器 healthy。
2. **第一步：内测团队**：内测团队所在部署机保持缺省 container，跑通至少
   一个**含前处理脚本的真实评测**；按 W15 逃逸测试集口径抽查（import 平台
   代码、读 `/proc/1/environ`、fork 炸弹、写越界路径、超长运行均被沙箱
   拦截——单测门禁 `tests/unit/test_w15_container_isolation.py`，实机探针
   `tests/integration/test_w15_container_probe.py`）；观察一个完整业务周期
   的沙箱失败率与任务延迟（executor `/health`、worker 日志）。
3. **第二步：全量**：内测无回归后，其余部署机同步骤启用沙箱面；worker env
   显式 `SCRIPT_EXEC_MODE=container` 与代码缺省一致，无需逐机改 env。
4. **回退（过渡期止损）**：任一环节出问题，`.env` 显式设
   `SCRIPT_EXEC_MODE=local` 后 `docker compose up -d worker api` 重启——
   模式是环境变量实时读，**改 env 重启即回退**，不需要改 compose、回滚
   镜像或代码。回退只是止损：local 的同边界风险（上文政策段）在回退期间
   重新敞开，问题定位修复后应尽快删掉该行恢复 container。

开发机本地开发要继续用 local：`.env_local` 显式写 `SCRIPT_EXEC_MODE=local`
（缺省已是 container，不再隐式 local）；要灰度验证 container 时删掉该行或
shell export 覆盖（环境变量实时读，重启 worker 生效）。W15 门禁
`tests/integration/test_w15_container_probe.py` 与
`tests/unit/test_w15_container_isolation.py` 在开发机有 docker 时可复跑验证隔离语义。

---

## 十、沙箱面部署：executor 与 egress 出口（ARCH-1-3）

设计依据：`plans/2026-10-15-企业级评测系统架构设计.md` §2.2 拓扑 / §2.3 D1-D2 /
§3 Phase 1 条目 1-3。compose 新增两个服务与一个内网，**整体挂在 profile
`sandbox` 后面**——部署机不设 `COMPOSE_PROFILES` 时行为与原部署完全一致
（不会因为没预载新镜像而在 `up` 时失败），这是灰度总开关。

### 10.1 拓扑与角色

| 组件 | 形态 | 说明 |
|---|---|---|
| `executor` | `ai-evo-executor` 镜像（`docker/Dockerfile.executor`，ARCH-1-1） | compose 内**唯一**挂 `/var/run/docker.sock` 的角色；非 root（uid 10002 + 补充组 = 部署机 docker 组 gid）；不发布宿主端口，仅 compose 内网 `http://executor:8300`；健康探活 `/health`。**不挂 env_file**：DB/Redis 凭证不进该容器，只经 `${EXECUTOR_TOKEN}` 插值注入共享 token（未配置 = fail-closed 拒绝全部沙箱请求） |
| `egress` | `ubuntu/squid`（Canonical 官方镜像，内网镜像源可拉；`squid:alpine` 非官方、内网源不可拉，等价选型） | 全部服务端外呼的统一出口：域名白名单（`docker/egress/squid.conf` + 清单文件）+ 内网目的地址一律拒绝（SSRF 收口）；不发布宿主端口 |
| `aievo-sandbox-egress` | compose bridge 网络，`internal: true`，网络名**钉死**为裸名 | 沙箱 `egress` 档唯一可达网段：internal 网络没有出网网关/NAT，沙箱直连外网在设计上不可达，唯一出路是同网段的 egress 代理（白名单 + access 审计）。网络名与 `executor/sandbox_params.py` `NET_PROFILES["egress"]` 必须逐字一致（compose 默认加项目前缀，已用 `name:` 钉死；`tests/unit/test_compose_topology.py` 断言同步） |

### 10.2 启用步骤（部署机）

1. **预载镜像**（离线机）：开发机 `docker pull ubuntu/squid:latest` +
   `docker save` 传输 + 部署机 `docker load`；executor 镜像
   `docker build -t ai-evo-executor:latest -f docker/Dockerfile.executor .`（或
   经 `EXECUTOR_IMAGE_TAG` 指向 CI 构建的 tag——build-service 接线 executor
   镜像构建属后续条目，手工预载先行）。
2. **同步沙箱面配置文件**：`docker/egress/squid.conf` 与
   `docker/egress/whitelist.txt` 必须与 `docker-compose.yml` 同级可寻址
   （compose 按相对路径 `./docker/egress/…` 挂载）。离线部署目录若只同步了
   `docker-compose.yml`，把 `docker/egress/` 目录一并放过去；或用 `.env` 的
   `EGRESS_SQUID_CONF` / `EGRESS_WHITELIST_FILE` 指向自有路径。
3. **核对 sock 组 gid**：`stat -c %g /var/run/docker.sock`，写入 `.env` 的
   `DOCKER_GROUP_GID`（默认 999 只是 Debian 系常见值；群晖等自定 gid 必查）。
4. **生成 token 与接线地址**：`openssl rand -hex 32` 写入 `.env` 的
   `EXECUTOR_TOKEN`，并设 `EXECUTOR_URL=http://executor:8300`（worker 侧
   ARCH-1-2 HTTP 驱动经 .env 的 env_file 注入读取，compose 无需再加行）。
5. `.env` 设 `COMPOSE_PROFILES=sandbox`，`docker compose up -d`；确认
   `ai-evo-executor` / `ai-evo-egress` 两个容器 healthy。
6. （可选灰度）worker 外呼切 egress：`.env` 设
   `WORKER_EGRESS_PROXY=http://egress:3128` 后 `docker compose up -d worker`。
   留空 = 不走 proxy，与既有行为一致。

沙箱面启用并配好 `EXECUTOR_URL` 后，ARCH-1-4 的缺省 `container`（worker env
显式声明同值，见第九节）即有完整执行通道——本节步骤是第九节 9.1 灰度
路径第 1 步「前置就绪」的全部内容。

### 10.3 SCRIPT_NET_PROFILE 网络档位 ↔ compose 网络（与条目 1-2 的对应关系）

沙箱网络档位的**单一事实源**是 `executor/sandbox_params.py` 的
`NET_PROFILES`（executor 受理请求时按此白名单校验并映射为 docker
`--network` 取值；worker 侧由 ARCH-1-2 的 SandboxDriver 以 `SCRIPT_NET_PROFILE`
环境变量表达同一批档位，缺省 `none`，映射见
`script_container.WORKER_NET_PROFILE_TO_DOCKER`）：

| 档位（SCRIPT_NET_PROFILE） | executor 通道 docker `--network` | 前置 | 出网能力 |
|---|---|---|---|
| `none`（默认，离线档） | `none` | 无 | 零出网——沙箱连 compose 内网都不可达 |
| `egress`（白名单档） | `aievo-sandbox-egress`（即本节钉死的 compose 网络） | `COMPOSE_PROFILES=sandbox` 已启用（网络随 egress 服务创建） | 仅白名单域名经 egress 代理出网（内网目的地址被 proxy 拒绝；直连路由不存在） |
| `open`（直出档） | **executor 通道拒绝**（`NET_PROFILES` 无此键 → 400，设计 D2：仅超管脚本 + 审批，Phase 1 未开放） | —— | worker docker-CLI 通道（开发机直跑 docker 的过渡通道）映射默认 bridge 直出；企业 executor 通道无此能力 |

注意：沙箱容器在 `egress` 档内**不注入平台 env**（含 proxy 地址）——脚本要
出网需自带 `http://egress:3128`（沙箱 env 白名单的扩展属后续条目）；平台侧
worker 的模型测连/OSS 外呼走的是第 6 步的 `WORKER_EGRESS_PROXY`，两者同一
出口、同一份域名白名单。

### 10.4 域名白名单运维

- 仓库模板 `docker/egress/whitelist.txt` 只是示例（常见模型厂商/OSS/pip 源），
  **不是审核过的企业清单**；企业部署在服务器维护自己的清单文件，`.env` 设
  `EGRESS_WHITELIST_FILE` 指向它（dstdomain 语法：`.example.com` 匹配
  example.com 及全部子域，一行一条）。
- 改清单 / 改 `docker/egress/squid.conf` 后 `docker compose restart egress`
  生效（squid 配置不热加载）。
- 审计：`docker exec ai-evo-egress tail -n 50 /var/log/squid/access.log`
  （每次放行/拒绝一行，含客户端、域名、判定）。
- SSRF 回归验证（设计 §3 条目 1-3 验收口径）：从 worker 容器内以
  `HTTP_PROXY=http://egress:3128` 访问内网地址（如 `http://127.0.0.1:8100/`、
  `http://192.168.x.x/`）应得 403；访问非白名单域名同为 403。策略语义由
  `tests/unit/test_compose_topology.py` 静态断言（deny 内网段先于 allow 白名单、
  末条 deny all）持续看守。


---

## 十一、镜像漏洞扫描与 digest 钉扎（ARCH-3-3）

设计依据：`plans/2026-10-15-企业级评测系统架构设计.md` §3 Phase 3 表 3-3 行——
「CI 加 trivy 镜像扫描；script-runner 镜像 digest 钉扎进配置」。两条产线的
门禁口径完全一致：**trivy 扫描主镜像与 script-runner 镜像，HIGH 及以上
（`HIGH,CRITICAL`）且有修复版本的 CVE 卡门禁（exit-code 1）**。

### 11.1 两条产线的扫描接入点

| 产线 | 接入点 | 行为 |
|---|---|---|
| GitHub CI | `.github/workflows/ci.yml` 的 `image-scan` job | `docker-image` job 构建主镜像后 `docker save` 成 artifact；scan job load 回来 + 现场构建 script-runner 镜像（`docker/script-runner/Dockerfile`），trivy 各扫一遍，HIGH+ fail |
| 企业 GitLab 线 | `build-service` 构建钩子（`tools/build-service/build_service.py` 的 `scan_image`，`config.toml` 的 `[scan]` 段） | 构建产物落 daemon 之后、写 release 记录**之前**扫描；不过 → 构建任务 failed，有漏洞的镜像不进可部署/可回滚列表 |

为什么 GitLab 线扫在 build-service 里：GitLab runner 只做 curl + 轮询
（见第一节），CI 侧没有构建现场；镜像只存在于部署机 daemon 里，扫描必须
在产物所在地跑——build-service 就是企业线的等价构建钩子。

`ignore-unfixed` 的取舍（两线同参）：Debian slim 基座常态带着「无修复版本」
的 HIGH CVE，一律卡会把门禁变成永久红灯；这类由镜像基座升级
（`python:3.12-slim` 的 `FROM` 变更时）解决。**有修复版本的 HIGH/CRITICAL
仍然卡**——11.3 的门禁演练用的就是这类。

### 11.2 离线扫描器镜像的预载（部署机一次性 + 周期更新）

部署机不能下载任何东西，扫描器（trivy + 漏洞库）必须预烘焙成镜像运过去：

```bash
# 开发机（联网）：构建含离线漏洞库的扫描器镜像。
# DB 烘焙进镜像后，扫描时 --skip-db-update --offline-scan 零外呼。
cat > Dockerfile.trivy <<'EOF'
FROM aquasec/trivy:latest
RUN trivy image --download-db-only
EOF
docker build --platform linux/amd64 -t ai-evo-trivy:latest -f Dockerfile.trivy .

# 传输 + 部署机加载（与 runner 镜像同一离线预载流程，见 docker/script-runner/README.md）
docker save ai-evo-trivy:latest | gzip > ai-evo-trivy.tar.gz
# scp 到部署机后：
gunzip -c ai-evo-trivy.tar.gz | docker load
```

- **上线顺序**：先预载 `ai-evo-trivy`，再部署含扫描门禁的新版
  `ai-evo-build-service` 镜像。门禁 fail-closed（与 `AUTH_JWT_SECRET` /
  `EXECUTOR_TOKEN` 同整改纪律）：`[scan].enabled` 缺省 `true`，扫描器镜像
  缺失时构建同样失败（报错文案带本节指引），不会静默放行。
- **漏洞库更新**：库烘焙在镜像里、随镜像更新。建议每月在开发机重跑上面
  的构建（`--download-db-only` 拉新库）并重新预载，节奏与基座镜像升级对齐。
- **配置**：`build-service/config.toml` 的 `[scan]` 段（`enabled` / `image` /
  `severity` / `timeout_sec`，模板与注释见 `tools/build-service/config.example.toml`）。
  `enabled=false` 仅限扫描器未就绪的过渡期，需安全负责人知情，修完预载即恢复。

### 11.3 门禁演练（验收口径：高危 CVE 卡门禁演练触发一次）

门禁必须证明「真的会拦」，否则形同虚设。演练方式（开发机即可，不动部署机）：

```bash
# 找一个带「有修复版本 HIGH CVE」的镜像——最现成的是数月前构建的旧版
# ai-evo 应用镜像（pip 依赖老化必然命中）；没有旧 tag 就专门钉旧依赖构建一个。
# 命令与 build-service scan_image 完全同参（同口径）：
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  ai-evo-trivy:latest image \
  --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed \
  --skip-db-update --offline-scan ai-evo:<旧tag>
echo "exit=$?"   # 期望非 0，且表格式输出里可见 HIGH/CRITICAL 条目
```

GitHub CI 线等价演练：临时把 `image-scan` job 的 `severity` 加宽一档
（如 `MEDIUM,HIGH,CRITICAL`）推一个分支，确认 job 红灯后还原。
演练记录（日期、命中的 CVE、处置动作）归档到安全检查单。

### 11.4 script-runner 镜像 digest 钉扎（SCRIPT_RUNNER_IMAGE@sha256）

`SCRIPT_RUNNER_IMAGE`（默认浮动 tag `ai-evo-script-runner:latest`，`.env`/
环境变量可覆盖）自 ARCH-1-5 起支持 digest 钉扎，ARCH-3-3 对齐口径：默认
tag、env 覆盖、钉扎可选（文法校验见 `app/core/config.py` 的
`validate_runner_image_ref`，格式错在配置解析层 fail-fast）：

```bash
# 浮动引用（缺省）
SCRIPT_RUNNER_IMAGE=ai-evo-script-runner:latest
# digest 钉扎：镜像内容寻址——同 tag 被覆盖/缓存投毒对钉扎引用无效
SCRIPT_RUNNER_IMAGE=harbor.internal:5000/ai-evo/script-runner@sha256:<64 位十六进制>
```

钉扎值的取法（离线机注意）：**digest 只有经 registry 中转才有**——
`docker build` / `docker load` 出来的本地镜像 `RepoDigests` 为空，取不到
digest。企业有内网 registry（如 harbor）时：

```bash
# 开发机：推到内网 registry，读回 RepoDigests
docker tag ai-evo-script-runner:latest harbor.internal:5000/ai-evo/script-runner:2026-10-15
docker push harbor.internal:5000/ai-evo/script-runner:2026-10-15
docker inspect --format '{{json .RepoDigests}}' \
  harbor.internal:5000/ai-evo/script-runner:2026-10-15
# 取 "repo@sha256:..." 中的 sha256 段写入部署机 .env：
#   SCRIPT_RUNNER_IMAGE=harbor.internal:5000/ai-evo/script-runner@sha256:<64hex>
docker compose up -d worker   # 改 env 重启即生效（镜像引用环境变量实时读）
```

- 换版本：push 新 tag → 取新 digest → 改 `.env` 重启；registry 里旧 digest
  长期保留即回滚目标（改回旧 digest 行再重启）。
- **通道现状**：worker docker-CLI 通道（无 executor 的形态）原样把引用交给
  `docker run`，daemon 原生按 digest 内容寻址校验；**executor 通道（第十节
  沙箱面）的镜像白名单尚未放开 digest 引用**（`executor/sandbox_params.py`
  对含 `@` 的引用仍拒绝），企业 executor 形态暂以「registry 固化 tag +
  registry 侧不可变 tag 策略」替代，白名单放开属后续条目（放开时两侧同步，
  见该文件注释）。
- 与镜像扫描的关系：扫描保证「构建出的镜像没有高危漏洞」，钉扎保证
  「部署跑的镜像就是扫过的那份内容」——把部署侧的镜像漂移面（同 tag
  被覆盖）收掉，两者合起来才是供应链闭环。
