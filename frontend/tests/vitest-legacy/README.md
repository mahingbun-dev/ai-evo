# vitest 遗留测试（隔离区）

本目录存放 6 个以 `vitest` 为运行时编写的遗留测试文件，从 `tests/unit/` 迁出。

## 历史状态（W8 之前）

1. **本项目的 `package.json` / `pnpm-lock.yaml` 从未声明过 `vitest` 依赖**，
   `frontend/node_modules` 中也不存在 vitest（主仓与 worktree 均已核实）。
   因此这些文件自「初始化项目提交」以来从未在任何门控中真正执行过。
2. **门控命令为 `node --test tests/unit/`**（Node 原生测试运行器），
   vitest 风格的 `import { test, expect, vi } from 'vitest'` 与 `vi.mock`
   模块拦截在 node:test 下无法解析和运行（实测报 `ERR_MODULE_NOT_FOUND: Cannot find package 'vitest'`）。
3. 多个文件的头注释本身已写明其状态：
   - `DatasetsViewData.test.js`：「需先安装依赖 `npm install vitest @vue/test-utils --save-dev`」（从未安装）；
   - `Tasks.latency.spec.js` / `Results.latency.spec.js`：「由于 frontend/package.json 中未定义 test 脚本，
     这些测试无法通过 npm run test 运行，仅作为人工验证参考」（且断言多为 `expect(true).toBe(true)` 占位）。

## W8 之后的现状（整改计划 §3 W8「前端」项）

W8 已接入 vitest 并让本目录 6 个文件全部可运行、纳入 CI 门禁
（`.gitlab-ci.yml` 的 `test-frontend` job，`pnpm test:unit`）：

- `package.json` devDependencies 增加 vitest（^3.2.7），scripts 增加 `test:unit: vitest run`；
- 根 `vitest.config.js` 只收集本目录——**不要**把 node:test 门控目录 `tests/unit/`
  卷进 vitest（两套运行器互不兼容，vitest 拉起 node:test 文件只会产生假结果）；
  路径别名沿用 vite.config.js 的 resolve.alias；
- 修复内容：
  - `judgeConfig.test.js`：断言对齐现状（`validateConfig` 导出已随后端裁判配置
    API 重构移除，改为校验现存的 getTemplate / previewPrompt 与 URL 契约）；
  - `Tasks.latency.spec.js` / `Results.latency.spec.js`：原为
    `expect(true).toBe(true)` 占位（仅人工验证参考），已固化为对
    Tasks.vue / Results.vue 真实源码的断言（latency 选项 / 条件渲染 /
    默认配置 / distributionData 九项指标）；
  - 其余 3 个文件（DatasetsViewData / TasksWebSocket / useTaskWebSocket）原样可运行。

本目录文件继续保留在 `tests/vitest-legacy/` 而不搬回 `tests/unit/`：
`tests/unit/` 仍是 node:test 的门控目录（`node --test tests/unit/` +
pytest 桥接 `tests/unit/test_results_frontend_prescript.py`）。
