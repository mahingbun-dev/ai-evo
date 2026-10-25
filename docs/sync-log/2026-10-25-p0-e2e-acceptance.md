# 2026-10-25 P0 迭代端到端验收记录

对 `perf(p0)`（ee97c58）+ `feat(onboarding)`（9498a00）+ 前端重设计（aed16bb）
做全链路 E2E 验收（真实 API + celery solo worker + PG/Redis + 浏览器 + CLI），
过程中发现并修复 4 个真问题，全部通过后推送。

## 验收环境

- API：`scripts/run_local_dev.py`（:8100，静态托管 frontend/dist）
- Worker：`celery -A app.celery_app worker --pool=solo`（macOS/py3.13 必须 solo）
- 账号：redesign_check（super_admin）；前端经 ：8100 托管产物 + Ego Lite 浏览器

## 验收结论

| 面 | 结果 |
|---|---|
| demo 一键评测全链路（seed→run→mock 裁判→结果页均分/维度分） | ✅ 均分 8，维度 内容质量/表达规范 各 8，行状态 completed |
| 分享链接匿名只读报告（免登录+独立布局） | ✅ 匿名打开、无侧边栏、数据只读 |
| API Key 创建 + CLI（install/config/run） | ✅ exit 2=任务未配门禁（严格语义正确） |
| CLI 门禁退出码 | ✅ 失败门禁（≥99.9 vs 实际 8）exit 1 + 判定输出；通过门禁（≥7）exit 0 |
| 浏览器冒烟：开发者设置 5 tab（API Keys/Webhooks/SSO-OIDC/LDAP/License） | ✅ 全部渲染、表格/表单/告警内容完整 |
| 浏览器冒烟：审计日志/并发池/仿真/统计分析 5 tab/模板/数据集 | ✅ 渲染正常，审计含 demo 任务记录，统计有真实数据 |
| 单测 | ✅ 相关 21 用例绿（demo/task_detail/audit_coverage） |

## E2E 发现并修复的问题

| # | 问题 | 修复 |
|---|---|---|
| 1 | demo 模型解析配置用 `{"path": "choices.0.message.content"}`，ResponseParser 实际读 `parse_type/parse_rule` → 裁判评分全部 parse_failed、得分 0 | 种子改正典形状 `{"parse_type":"json_path","parse_rule":{"content":"$.choices[0].message.content"}}`；存量 DB 行同步修；加 ResponseParser 回归钉 |
| 2 | 软删过的 demo 任务再 seed 返回 `created:{}` 且复用旧配置快照 | 种子加「软删行复活」分支：重建模型配置快照 |
| 3 | 匿名分享页渲染完整应用侧边栏（应独立只读布局） | App.vue `isLoginPage` 计入 `/share/` 前缀 |
| 4 | 门禁配置读写双断：① PUT 只带 `gate_config` 被 400 拦（校验器要求 evaluation_targets，而 demo 种子漏配该行）；② 任务详情响应模型无 `gate_config` 字段，编辑弹窗回填恒 undefined | 种子补 `DEMO_EVALUATION_TARGETS`（含存量 active 行回填）；`TaskDetailResponse` 补 `gate_config` 字段；双回归钉 |

## 修复涉及文件

- `app/services/demo_seed_service.py`：解析配置正典形状、软删复活、evaluation_targets 常量+回填
- `app/api/evaluation_task_api.py`：TaskDetailResponse 增 gate_config
- `frontend/src/App.vue`：分享页独立布局
- `tests/unit/test_demo_api_mock.py`：4 个 E2E 回归钉
