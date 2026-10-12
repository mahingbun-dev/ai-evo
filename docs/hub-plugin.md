# anc-hub 插件（hub-plugin 服务）

把 ai-evo 的核心能力注册为 anc-hub 的插件（开源部署可使用
[plugin-hub](https://github.com/mahingbun-dev/plugin-hub)，对接契约一致），
任何能接 anc-hub 的 MCP agent 都能直接对 ai-evo 发起评测闭环：

```
agent ──MCP──▶ anc-hub ──meta[hub.mas_token]──▶ ai-evo-hub-plugin(:9500)
                                                    │ Authorization: Bearer <mas_token>
                                                    ▼
                                            ai-evo api(:8100, /api/v1)
```

## 能力面（18 个 MCP 工具）

中台把 manifest 声明的工具聚合进 MCP 工具面，工具名带 `ai-evo__` 前缀。
agent 从 `tools/list` 直接选用；也可走 `invoke_plugin` 通用入口（载荷里加 `op` 字段）。

| 分组 | 工具 | 说明 |
|---|---|---|
| 数据集 | dataset_list / dataset_get / dataset_data_list | 浏览数据集与数据行 |
| 数据集 | dataset_create / dataset_update\* / dataset_delete\* | CRUD；update/delete 需人工审批 |
| 文件导入 | hub_files_query | 查询 anc-hub file-store 里的文件（找 file_id） |
| 文件导入 | dataset_import_hub_files | 按 file_ids 建/追加数据集（异步 job） |
| 文件导入 | hub_import_job_status | 查导入进度 |
| 评测任务 | model_list / task_list / task_get | 选模型、列任务、查状态与配置 |
| 评测任务 | task_create / task_update\* / task_delete\* | CRUD；update/delete 需人工审批 |
| 评测任务 | task_run / task_stop | 触发执行（异步）/停止 |
| 评测任务 | task_results | 结果摘要（通过率/平均分/维度分/top失败样例）+ 前端链接 |

\* 标记的工具 `requires_approval=true`：autonomous agent 只能提交草稿，由人在中台侧确认后执行。

### 数据集文件通道

统一走 anc-hub file-store（不内联大文件，hub 信封内联上限 4MB）：

1. agent 用 `file-store__file_upload` 分块上传数据集文件 → 得到 `file_id`；
2. agent 调 `ai-evo__dataset_import_hub_files` 传 `file_ids` → 插件转给 ai-evo 的
   `POST /api/v1/evaluation/datasets/import/hub-files`；
3. ai-evo worker 以**同一用户身份**（Bearer 即 mas_token）经 HubMcpClient 从
   file-store 拉文件解析落库，插件侧不做分块互调（互调每分钟 60 次配额，大文件分块
   下载必然撞限，详见 plugin-hub 仓库 `docs/plugin-onboarding.md` 的互调治理）。

### 鉴权与审计

- agent 必须先经 anc-hub 登录（`auth__login` 或 MCP 客户端配 Bearer 头）；
- hub 把平台登录态（masToken）以 `meta["hub.mas_token"]` 透传给插件；
- 插件组装 `Authorization: Bearer <mas_token>` 调 ai-evo——ai-evo 鉴权中间件
  校验该登录 token 并加载对应用户，操作以真实用户身份执行，
  创建任务/导出数据集自动落操作审计（含操作者）；
- 插件不持有任何凭证；无登录态返回 `login_required` 业务卡，token 失效返回
  `login_invalid`（均不判为插件故障）。

### 长任务语义

`task_run` 提交即返（celery 异步），评测耗时分钟到小时级。**插件不做轮询**——
agent 需要状态就用 `task_get` 显式查询，需要监控就由 agent 自己安排轮询节奏。
评测结果经 `task_results` 返回**摘要**（成功项明细整块省略、失败样例截断 top10），
全量明细走前端结果页。

## 部署形态

与 api/worker **共用同一镜像**（`ai-evo:<sha>`），compose 第四个服务 `hub-plugin`
以 `SERVICE_TYPE=hub-plugin` 进入 `docker-entrypoint.sh` 的第三分支
（`python -m app.hub_plugin`）。

| 项 | 值 | 说明 |
|---|---|---|
| 网络 | `network_mode: host` | 对齐中台插件端口段位约定（自 9000 起逐插件 +100） |
| 监听端口 | `:9500` | `HUB_LISTEN_ADDR`；上线前在部署机 `ss -tlnp` 确认空闲 |
| advertise | `http://127.0.0.1:9500` | hub 与插件同机，回环即可拨通 |
| `HUB_ADDR` | `http://127.0.0.1:8093` | 中台插件面（gRPC），同机直连不走 nginx TLS |
| instance_id | `ai-evo-hub-plugin-1` | 全局唯一；撞 id 会被静默顶掉（工具面悄悄消失） |
| `AIEVO_API_BASE` | `http://127.0.0.1:8100` | 经宿主端口映射调 api |

健康语义：`health()` 探测 `GET /api/v1/auth/status`（鉴权白名单），ai-evo 不可达
时自报不健康——注册期中台探测不通会拒绝注册（插件每 5s 重试，api 起来后自动补上），
运行期不可达则中台摘除实例，agent 工具面同步消失，不会留死工具。

### 服务器侧手工步骤（CI 管不到的）

1. **`build-service/config.toml` 的 `[deploy].services`**：把 `"hub-plugin"` 加进
   列表，否则 `verify_deploy` 不校验该容器（该文件在服务器本地，不入库）。
2. 部署机 `ss -tlnp | grep 9500` 确认端口空闲。

### 依赖与离线快照

vendored SDK（`hubkit/`，来源与升级方式见 `hubkit/README-vendored.md`）硬性要求
`grpcio==1.84.0`、`protobuf==7.36.2`（生成代码 import 期校验版本，低了直接抛错）。
升级路径经 `tools/prod-constraints.txt`（本次已从 grpcio 1.83.0 / protobuf 7.35.1
上调），改依赖必须重跑离线三步曲（`prepare-offline.sh` → `upload-offline-snapshot.sh`
→ 提交 lock/快照），否则 CI check-snapshot 红灯、服务器构建拒绝。

## 版本纪律

- manifest（工具清单/描述/schema/审批标记）与 `PLUGIN_VERSION` 绑定；**同版本号
  不可变更**，中台拒绝「同号不同契约」（VERSION_CONFLICT）。改契约先升
  `app/hub_plugin/manifest.py` 的 `PLUGIN_VERSION`。
- 删除/新增工具新工具名的合法字符集：`[A-Za-z0-9_-]+`。

## 本地验证（离线，不需要真中台/真 ai-evo）

```bash
# 在仓库根（需 .venv 里有 grpcio/protobuf/requests/pytest）
.venv/bin/python -m pytest tests/unit/test_hub_plugin_*.py -q
```

- `test_hub_plugin_conformance.py`：L1 契约自洽（中台注册期规则本地化）+ manifest 不变量；
- `test_hub_plugin_runtime.py`：L2 运行时自检 + mockhub 注册/调用（L3/L4 替身）；
- `test_hub_plugin_dispatch.py` / `_client.py` / `_summary.py`：分发、鉴权语义、摘要裁剪。

接入真中台的验收（L3 看插件日志「已注册到中台」；L4 经 hub ingress 真实调用一次）
见 plugin-hub 仓库 `docs/plugin-onboarding.md` 的四道关流程。
