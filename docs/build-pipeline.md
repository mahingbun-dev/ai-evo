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
         ├─ 校验 .offline-snapshot.json 与锁文件哈希是否一致
         │    └─ 不一致 → 从 GitLab Packages 下载离线快照
         ├─ docker run ai-evo-builder（挂载 docker.sock）
         │    ├─ pnpm install --offline && pnpm build   → frontend/dist
         │    └─ docker build -t ai-evo:<短sha>         → 宿主 daemon
         └─ deploy：同步 docker-compose.yml → 写 .env 的 IMAGE_TAG
                     → docker compose up -d → 校验容器与接口
                     → 同步 frontend/dist 到 nginx 静态根（宿主机目录）
```

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
| `/apps/qa-code/ai-evo/build-service/data` | `releases/` 版本记录、`logs/` 任务日志 |

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

项目里已有名为 `ci/cd` 的 Deploy Token（`gitlab+deploy-token-173`），其 scope 是
`read_repository` + `read_package_registry` + `write_package_registry`：

- **上传离线快照**（开发机）用它 —— 因为含 `write_package_registry`；
- **下载离线快照**（服务器）也用它 —— 用 `read_package_registry`。

该 token 的鉴权方式（已实测通过，上传返回 201、回读内容一致）：

```
用户名：gitlab+deploy-token-173
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
                                                     # 确认有 DATABASE_URL 与
                                                     # REDIS_URL=redis://redis:6379/0
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

### 4.5 首次流水线

在 GitLab 上发起一次 Web 触发（CI/CD → Pipelines → Run pipeline，选 `main` 分支），
两个 job 手动点击执行。第一次会同步离线快照（数百 MB），耗时较长。

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

**不随部署自动执行**——`DATABASE_URL` 指向多人共用的测试库，自动迁移一旦碰上破坏性变更
会直接打断别人。需要迁移时手动执行：

```bash
docker exec -it ai-evo-api alembic upgrade head
```

### 部署时的任务中断行为

`worker` 服务设置了 `stop_grace_period: 900s`。部署触发 `docker compose up -d` 时：

- Celery 收到 SIGTERM 后进入 warm shutdown，**跑完当前任务再退出**；
- 排队中的任务保留在 Redis 队列里，由新 worker 接手；
- 若当前任务超过 900s 仍未结束，会被强制杀掉。

因此部署耗时可能较长（有长任务在执行时）。CI 的 deploy job 等待上限是 2400s。

对比旧流程：`scripts/do_deploy.sh` 会先 `DEL celery` 清空队列，正在执行和排队的任务全丢。
该脚本已标注为仅应急使用。

### 与 worker 自愈脚本的关系

部署机上 crontab 每 5 分钟跑一次 `scripts/ensure_worker.sh`，发现 `ai-evo-worker`
不在运行就 `docker compose up -d worker` —— 这原本会与上面的优雅退出打架：
worker 优雅退出期间容器处于 Stopping、不出现在 `docker ps` 里，自愈脚本会误判为
「挂了」并插入一次 compose up，与外层部署的 compose 撞车。

处理方式：build-service 在 `compose up` 到校验结束之间写
`/apps/qa-code/ai-evo/.deploy-in-progress`，`ensure_worker.sh` 见到该标记就跳过。

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
| 上传快照报 401 | Deploy Token **只认 Basic 鉴权**，用 `PRIVATE-TOKEN` 头会返回 401（实测确认）。脚本已改用 Basic，用户名默认 `gitlab+deploy-token-173`，可用 `GITLAB_UPLOAD_USER` 覆盖。 |
| 快照解出来文件数翻倍、多出 `._xxx` 文件 | macOS 的 `bsdtar` 默认附带 AppleDouble 元数据。脚本已用 `COPYFILE_DISABLE=1` 关闭，不要绕过脚本手工 `tar` 打包。 |
| 部署报「部署目录缺少 .env」/「缺少必需变量: DATABASE_URL」 | build-service 的前置护栏拦住了。按第 4.4 节把 `.env` 建好再部署——这是为了不在缺配置时覆盖 compose、把线上服务搞坏。 |
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

