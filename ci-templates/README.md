# CI 模板：评测即门禁

把 AI-Evo 评测结果接入 CI/CD，让**门禁判定卡发布**：评测不达标，流水线就红。
三个平台各一份最小模板，全部基于 `aievo-cli` 的统一退出码约定。

## 退出码约定（所有模板一致）

| 退出码 | 含义 | CI 行为 |
|---|---|---|
| 0 | 门禁通过（所有阈值达标） | 步骤通过 |
| 1 | 门禁失败（质量不达标） | 步骤失败，卡合并/发版 |
| 2 | 错误（认证失败 / 网络异常 / 等待超时 / 任务异常收尾） | 步骤失败，按基础设施故障处理 |

> 区分 1 与 2：1 说明被测系统质量问题，修复后重跑；2 说明门禁链路本身故障
> （Key 失效、平台不可达、评测超时），先修链路再重跑。

## 前置准备

1. 平台「开发者设置 → API Keys」创建一枚 Key，至少勾选 `tasks:run` + `tasks:read`
   （如需 `aievo datasets upload` 另加 `datasets:write`）。
2. 目标评测任务已配置**门禁阈值**（任务编辑表单的「门禁配置」），否则 `--wait`
   会以退出码 2 结束（无门禁语义可判）。
3. CI 密钥管理中保存 `aievo_sk_` 明文 Key（只存凭据系统，不进代码库）。

## GitHub Actions

```yaml
# .github/workflows/gate.yml
name: aievo-gate
on: [pull_request]

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4   # composite action 与仓库同库时需要
      - uses: ./ci-templates/github # 同库引用；跨仓库改为 aievo/gate@v1
        with:
          aievo-url: ${{ vars.AIEVO_URL }}
          aievo-api-key: ${{ secrets.AIEVO_API_KEY }}
          task-id: task_xxx
          timeout: '7200'
```

模板：[github/action.yml](github/action.yml)（composite action）。

## GitLab CI

```yaml
include:
  - local: 'ci-templates/gitlab/aievo-gate.yml'

aievo:gate:demo:
  extends: .aievo_gate_template
  variables:
    AIEVO_TASK_ID: "task_xxx"
# AIEVO_URL / AIEVO_API_KEY 配在 CI/CD Variables（后者勾选 Masked）
```

模板：[gitlab/aievo-gate.yml](gitlab/aievo-gate.yml)。

## Jenkins

在声明式流水线中加入门禁 stage，Key 走 `withCredentials`：

```groovy
stage('AI-Evo 评测门禁') {
    steps {
        withCredentials([string(credentialsId: 'aievo-api-key', variable: 'AIEVO_API_KEY')]) {
            sh '''
                pip install --quiet aievo-cli
                aievo config add "$AIEVO_URL" "$AIEVO_API_KEY"
                aievo run "$AIEVO_TASK_ID" --wait --json
            '''
        }
    }
}
```

模板：[jenkins/Jenkinsfile.gate](jenkins/Jenkinsfile.gate)。

## 常用命令速查

```bash
aievo config add <url> <api-key> [--profile 名字]   # 保存连接（明文 Key 落 ~/.aievo/config.toml，权限 0600）
aievo run <task-id> --wait --timeout 7200 --json    # 触发并等待门禁判定（CI 主命令）
aievo results <task-id> -o json|csv                 # 读结果（csv 为行级明细）
aievo datasets upload <file> --name <名称>          # 建数据集并导入（.json/.xlsx/.xls）
```

超时建议：任务评测时长 × 2，最小 600s；`--interval` 默认 5s，一般无需调整。
