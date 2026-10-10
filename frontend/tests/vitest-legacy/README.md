# vitest 遗留测试（隔离区）

本目录存放 6 个以 `vitest` 为运行时编写的遗留测试文件，从 `tests/unit/` 迁出，原因如下：

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

保留这些文件以备后续引入 vitest 时恢复，但它们不再位于 node:test 门控目录 `tests/unit/` 内，
避免门控 `node --test tests/unit/` 拉起注定失败的文件。

若将来要在本项目启用 vitest：`pnpm add -D vitest @vue/test-utils`，将本目录文件移回并补 `vitest.config.js`。
