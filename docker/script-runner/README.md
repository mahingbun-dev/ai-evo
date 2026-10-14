# ai-evo-script-runner 镜像（W15 脚本执行容器化隔离）

前处理脚本在 `SCRIPT_EXEC_MODE=container` 模式下的执行镜像：`docker run --rm`
容器内隔离运行（非 root、`--memory 1g --cpus 0.5 --pids-limit 128`、根文件
系统只读 + tmpfs、剥离平台 env、默认 `--network none`、挂载仅本次
`script_runs` 工作目录）。实现见 `app/services/script_container.py`。

## 镜像定位（基于平台镜像裁剪）

- 同基 `python:3.12-slim`（与平台主镜像一致，解释器行为同源）；
- 只装「脚本常用依赖集合」`packages.txt`（版本与 `requirements.txt` /
  `requirements.lock` 同源钉死；numpy 锁 2.2.6 的原因同主 Dockerfile——
  部署 VM 缺 X86_V2 指令集）；
- **不含**平台代码（`app/` 主体）、平台重依赖（langfuse / deepeval /
  langgraph / anthropic 等）与任何凭证；仅 COPY 统一执行 wrapper
  `app/services/pre_judge_entry.py` → 镜像内 `/opt/runner/pre_judge_entry.py`；
- 容器内脚本无 PYTHONPATH 注入，`import app.*` 必然失败（这是 W15 的验收
  断言之一，见 `tests/integration/test_w15_container_probe.py`）。

## 构建（开发机 / CI）

```bash
# 仓库根为构建上下文（wrapper 直接从源码 COPY）
docker build --platform linux/amd64 \
  -f docker/script-runner/Dockerfile \
  -t ai-evo-script-runner:latest .

# 自检：确认镜像内已装集合与代码静态清单一致（依赖校验的事实源）
docker run --rm --network none ai-evo-script-runner:latest \
  python -c "import requests, pandas, openpyxl; print('deps ok')"

# 本机冒烟：容器内跑一个 run(stdin) 脚本
docker run --rm -i --network none ai-evo-script-runner:latest \
  python /opt/runner/pre_judge_entry.py /tmp hello \
  <<< '{"task_id":"smoke"}'   # /tmp 不存在时报「脚本入口不存在」属预期
```

## 离线预载（D-3 运维事项，W15 开工前置）

部署机（CentOS 7，无外网）不联网构建，一律开发机构建后离线导入：

```bash
# 开发机：构建并导出
docker build --platform linux/amd64 \
  -f docker/script-runner/Dockerfile -t ai-evo-script-runner:latest .
docker save ai-evo-script-runner:latest | gzip > ai-evo-script-runner.tar.gz

# 传输到部署机后：
gunzip -c ai-evo-script-runner.tar.gz | docker load
docker images | grep ai-evo-script-runner   # 确认 tag 与 SCRIPT_RUNNER_IMAGE 一致
```

镜像 tag 变更通过环境变量 `SCRIPT_RUNNER_IMAGE` 指向（默认
`ai-evo-script-runner:latest`），不必改代码。

## 依赖集合同步纪律

`packages.txt` ↔ `RUNNER_IMAGE_PACKAGES`（`app/services/script_container.py`）
必须逐行一致：前者是镜像构建事实，后者是 `container` 模式下依赖声明校验的
静态清单。`tests/unit/test_w15_container_isolation.py::TestRunnerImagePackages`
做一致性断言——改任一侧必须同步另一侧并重建镜像、重新离线预载。

扩依赖流程：`packages.txt` 加行（版本沿用 requirements.txt 钉死值）→
同步 `RUNNER_IMAGE_PACKAGES` → 单测门禁 → 重建镜像 → 部署机离线导入 →
灰度放量。

## 运行时边界与运维注意

| 项 | 值 | 说明 |
|---|---|---|
| 运行用户 | `10001:10001`（`SCRIPT_RUNNER_USER` 可配） | 非 root；**Linux 部署机**上 bind mount 的宿主目录需对该 uid 可写（`chown -R 10001` 挂载目录，或把 `SCRIPT_RUNNER_USER` 配成目录属主 uid:gid）。macOS Docker Desktop 的 virtiofs 对任意 uid 可写，无需处理 |
| 资源 | memory 1g（swap 同额禁扩展）、cpus 0.5、pids 128 | 超限 OOM/限流，forkbomb 被 pids-limit 卡死，不影响宿主 |
| 文件系统 | `--read-only`；可写面仅工作区 bind mount + `/tmp`、`/home/runner` 两块 tmpfs（各 64m，nosuid,noexec） | 根文件系统不可变 |
| 网络 | 默认 `--network none` | 编排 step 声明 `network: true` 时改 bridge（脚本访问被测系统的任务级开关，默认关；受限内网网段精细化属后续迭代） |
| env | 仅 `PYTHONIOENCODING/PYTHONUNBUFFERED/LANG/HOME` | 平台 env（DATABASE_URL/REDIS_URL/SSO/LANGFUSE 等）不进容器 |
| 挂载 | 仅本次 `script_runs` 工作目录 → `/workspace` | 上传脚本库、hub 执行包镜像区、平台代码均不在容器内 |
| PYTHONPATH | 不注入 | 脚本无法 `import app.*` 平台代码 |

## 已知边界（如实说明）

- bridge 网络档位使用 docker 默认网络池，尚未做「受限内网网段/出网白名单」
  精细化——被测系统访问场景先以任务级开关 + 网络侧防火墙策略兜底；
- Linux 部署机 uid 权限适配（见上表）需在部署清单里固化，属 D-3 落地项；
- 镜像内依赖集合的 CI 自动重建未在本包范围（镜像构建产物不入库）。
