# gVisor 可选运行时档（runsc 加固档）：安装、开启与适用场景（ARCH-3-4）

本文档描述沙箱容器的**可选 gVisor 运行时档**：宿主机安装 runsc、注册 docker
daemon、平台侧经 `SANDBOX_RUNTIME` 开启与回退。设计依据：
`plans/2026-10-15-企业级评测系统架构设计.md` §3 Phase 3 表 3-4 行
（「（可选）宿主机安装 gVisor(runsc)，沙箱加 `--runtime=runsc` 加固档，
高危脚本专用；验收：沙箱逃逸测试集在 runsc 档复跑通过」）。

> 前置阅读：`docs/build-pipeline.md`（沙箱面部署与 `SCRIPT_EXEC_MODE` 灰度
> 政策）、`docs/deploy-scale.md`（评测机拓扑——runsc 装在**评测机/主控机的
> docker daemon 宿主机**上，即 executor 所在机器）。

---

## 一、它加固什么，不加固什么

既有沙箱位（runc 档即生效，见 `executor/sandbox_params.py`）：非 root、
资源上限（memory/cpus/pids）、只读根文件系统 + tmpfs、cap-drop ALL、
no-new-privileges、最小 env、网络档位、bind mount 白名单。这些约束的是
**容器内载荷的权限面**。

runsc（gVisor）在其上再收窄一层：用**应用层内核**（sentinel/gofer）拦截并
重放容器内全部系统调用，载荷触达的是 gVisor 实现的 Linux API 面，而不是宿主
内核本身。针对的威胁是**容器内载荷利用宿主内核漏洞逃逸**（内核 RCE 类、
namespaces/cgroup 经典逃逸面）——对「执行不可信第三方脚本」的平台，这是
runc 档覆盖不到的残余面。

不改变的边界：gVisor **不是**全量默认档（见 §四 适用场景），也不放松任何
既有约束——`--runtime runsc` 是叠加位，白名单仅 `runc|runsc`，其余运行时
名（kata 等）一律拒绝（`SandboxParamError` / executor 400 / Settings 启动
即拒）。

## 二、宿主机安装 runsc（Linux 评测机/主控机）

> macOS Docker Desktop **不支持**注册自定义容器运行时，本机开发不适用本档
> （缺省 runc 即可，`SANDBOX_RUNTIME` 保持不配或 `runc`）。以下步骤面向
> Linux 部署机；离线环境用方式 B。

### 方式 A：官方软件源（可出网机器）

```bash
# Debian/Ubuntu（root）
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases/release/debian/x86_64 gvisor main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list
sudo apt-get update && sudo apt-get install -y runsc

# RHEL/CentOS（root）
curl -fsSL https://gvisor.dev/archive.key | sudo tee /etc/pki/rpm-gpg/gvisor.asc
cat <<'EOF' | sudo tee /etc/yum.repos.d/gvisor.repo
[gvisor]
name=gvisor
baseurl=https://storage.googleapis.com/gvisor/releases/release/rpm/x86_64
enabled=1
gpgcheck=1
gpgkey=https://gvisor.dev/archive.key
EOF
sudo yum install -y runsc
```

### 方式 B：离线 deb/rpm（企业内网，走既有 LAN 交付流程）

在可出网机器下载对应包，经既有离线交付通道（同 `docs/build-pipeline.md`
的镜像 tar 流程）传到部署机后安装：

```bash
# 下载（可出网机器；版本号以 gvisor.dev 官方 release 页为准）
curl -LO https://storage.googleapis.com/gvisor/releases/release/<版本>/runsc.deb
# 安装（部署机；rpm 环境同理换 runsc.rpm + yum localinstall）
sudo apt-get install -y ./runsc.deb
```

### 注册 docker daemon 并重启

```bash
sudo runsc install          # 写 /etc/docker/daemon.json 的 runtimes 段
sudo systemctl restart docker
```

`daemon.json` 生效段（`runsc install` 自动写入，手工配置等价）：

```json
{
  "runtimes": {
    "runsc": { "path": "/usr/bin/runsc" }
  }
}
```

### 验证安装

```bash
docker info | grep -A2 runsc            # Runtimes 列表出现 runsc
docker run --rm --runtime=runsc hello-world
dmesg 不应有 runsc 崩溃记录；容器内 cat /proc/version 呈 gVisor 字样
```

## 三、平台侧开启与回退

平台配置入口是 `SANDBOX_RUNTIME`（`app/core/config.py`，默认 `runc`）：

```bash
# 部署机 .env（或 .env.scale，评测机模板同字段）
SANDBOX_RUNTIME=runsc
# 然后重启 worker（改 env 重启即切换；环境变量实时读优先于 .env）
docker compose restart worker        # 评测机：docker compose -f docker-compose.scale.yml restart worker-eval
```

生效链路（ARCH-3-4）：

- worker docker-CLI 通道（`EXECUTOR_URL` 未配置的单机形态）：
  `sandbox_runtime()`（`app/services/script_container.py`）→
  `SandboxRunSpec.runtime` → `docker run ... --runtime runsc`；
- executor 通道：请求字段 `runtime`（`executor/app.py.SandboxRunRequest`，
  默认 `runc`）经白名单校验（`executor/sandbox_params.validate_container_runtime`）
  后拼 `--runtime runsc`。**worker 侧 HTTP 客户端透传该字段属后续批次接线**
  （`app/services/executor_client.py`），接线前 executor 通道按缺省 `runc`
  执行——需要 runsc 生效的评测机在客户端接线落地前请走 docker-CLI 通道，
  或由接线批次一并启用；
- 非法值（非 `runc|runsc`）三层拒绝：Settings 启动即拒（`field_validator`）、
  worker 读取抛 `SandboxDriverError` 转脚本 fail 条目、executor 请求 400
  （拒绝即审计 WARNING，不触碰 docker CLI）。

**回退**：`.env` 改回 `SANDBOX_RUNTIME=runc`（或删除该行）重启 worker。
缺省 runc 不拼 `--runtime` 旗标（runc 即 docker daemon 缺省运行时），默认
行为与历史逐字节一致。

**验收口径**（设计 3-4 行）：沙箱逃逸测试集在 runsc 档复跑通过——

```bash
.venv/bin/python -m pytest tests/security/test_sandbox_escape.py -m sandbox_e2e
# 前置：部署机 docker daemon 已注册 runsc、SANDBOX_RUNTIME=runsc 已生效
```

## 四、适用场景：高危脚本专用，不建议全量默认

设计定位（§2.3「不做清单」与 §3 表 3-4 行）：seccomp/cap_drop/非 root/
read-only 是当前威胁模型的合理档位，gVisor 是**可选**加固档。建议开启的
场景：

- **不可信第三方脚本占比高的环境**：脚本库开放团队自上传、来源不受控
  （外包/社区脚本），且合规要求对「容器逃逸」有明确残余风险接受条款；
- **逃逸测试集/红蓝对抗环境的常驻评测机**：需要验证「即便容器内载荷拿到
  代码执行也摸不到宿主内核」的机器；
- **被测 agent 沙箱（ARCH-3-2，exec_type=claude_agent/deepseek_agent）所在
  评测机**：agent 工具调用链复杂、指令注入后行为不可预测，属高危载荷。

不建议全量默认的原因：

1. **性能损耗**：gVisor 拦截系统调用，纯计算脚本损耗小（~个位数百分比），
   但 **IO/文件密集与短命进程密集的脚本损耗显著**（数倍量级）——前处理
   脚本恰以文件解析为主，全量开启会直接拖慢评测吞吐；
2. **兼容面**：个别依赖奇异系统调用/性能计数器的库在 runsc 下行为异常，
   每批新 runner 镜像依赖都要在 runsc 档过一遍冒烟；
3. **运维面**：runsc 版本升级需跟随 gvisor.dev release 单独管理，属于
   部署机又一独立组件。

多机形态（`docs/deploy-scale.md`）下的建议做法：**分级评测机**——标记为
「高危」的评测机装 runsc 并配 `SANDBOX_RUNTIME=runsc`，其余保持 runc；
任务路由到哪台机器由队列拓扑决定（与 3-1 扩容机制一致）。

## 五、故障排查

| 现象 | 定位 |
|---|---|
| `docker run` 报 `unknown runtime` / 退出码 125 | daemon 未注册 runsc：`docker info` 查 Runtimes，重做 §二「注册 docker daemon 并重启」 |
| 脚本 fail 条目含 `SANDBOX_RUNTIME 非法` | `.env`/环境变量拼错（仅允许 `runc|runsc`），修正后重启 worker |
| runsc 档脚本普遍超时变慢 | 属预期性能损耗（§四）：提高该类脚本 `timeout_seconds` 或改回 runc 档 |
| executor 请求 400 `容器运行时 ... 不在白名单` | 调用方传了 `kata` 等未开放运行时；本批白名单仅 `runc|runsc`，扩展须改 `executor/sandbox_params.CONTAINER_RUNTIMES`（单一事实源，两侧同步） |
