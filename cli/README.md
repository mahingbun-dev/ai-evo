# aievo-cli

AI-Evo 评测平台命令行工具：触发评测、等待门禁判定，并以约定退出码供 CI 卡发布
（评测即门禁）。独立 pip 包，仅依赖 `typer` + `httpx`。

## 安装

```bash
pip install aievo-cli        # 平台发布后；源码交付时随离线包安装 wheel
```

开发环境（本仓库）直接用源码：`pip install -e cli/`。

## 快速上手

```bash
# 1. 保存平台地址与 API Key（开发者设置 → API Keys 创建，需 tasks:run + tasks:read）
aievo config add https://aievo.example.com aievo_sk_xxxx

# 2. 触发评测并等待门禁判定（CI 主命令）
aievo run task_xxx --wait --timeout 7200 --json

# 3. 读取结果
aievo results task_xxx -o json     # 完整结果对象
aievo results task_xxx -o csv      # 行级明细（idx/success/row_status/average_score/feedback）

# 4. 上传数据集（需 datasets:write）
aievo datasets upload ./set.xlsx --name 回归集
aievo datasets upload ./set.json  --name 回归集
```

## 退出码矩阵（`aievo run --wait`）

| 场景 | 退出码 |
|---|---|
| 门禁判定 `passed`（所有阈值达标） | 0 |
| 门禁判定 `failed`（质量不达标） | 1 |
| 门禁判定 `error`（指标不可用等） | 2 |
| 等待超时（`--timeout` 秒内未出判定） | 2 |
| 任务 failed/stopped 收尾且无判定 | 2 |
| 任务未启用门禁（gate_config 为空） | 2 |
| 认证失败（Key 无效/吊销）、scope 不足、网络错误 | 2 |
| 不带 `--wait`：触发成功即退出 | 0 |

CI 集成模板（GitHub Actions / GitLab / Jenkins）见仓库 `ci-templates/`。

## 命令参考

### `aievo config add <url> <api-key> [--profile 名]`
保存连接配置到 `~/.aievo/config.toml`（权限 0600）。`--profile` 支持多环境
（默认 `default`，可用环境变量 `AIEVO_PROFILE` 切换，各命令也有 `--profile`）。
`aievo config list` 查看（Key 打码显示）。

### `aievo run <task-id> [--wait] [--timeout N] [--interval N] [--json]`
- 触发 `POST /api/v1/evaluation/tasks/{id}/run`（scope：`tasks:run`）；
- `--wait`：轮询 `GET .../gate`（scope：`tasks:read`）直到出判定/超时/异常终态，
  按上表退出；
- `--json`：stdout 输出单 JSON 对象（`verdict`/`checks`/`gate_result` 可机读），
  人类可读进度走 stderr，不污染机读流。

### `aievo results <task-id> [-o json|csv] [--execution N]`
读取最新（或指定版本）评测结果（scope：`results:read`）。

### `aievo datasets upload <file> --name <名称> [--description 描述]`
创建数据集并导入：`.json` 走 JSON 导入（对象数组或 `{"data": [...]}`），
`.xlsx/.xls` 走 Excel 导入（scope：`datasets:write`）。
