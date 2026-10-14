import { defineConfig } from 'vitest/config'

// W8（整改计划 §3 W8）：前端单测运行配置。
//
// 仅收集 tests/vitest-legacy/（vitest 运行时遗留测试的隔离区，见该目录
// README）——不要把 tests/unit/*.test.mjs 卷进来：那是 node:test 门控目录
// （`node --test tests/unit/` + pytest 桥接 tests/unit/test_results_frontend_prescript.py），
// vitest 拉起 node:test 风格文件只会产生假结果。
//
// 路径别名沿用 vite.config.js 的 resolve.alias（vitest 默认读取它），
// `@/composables/...` 等导入在测试里可直接解析。
export default defineConfig({
  test: {
    include: ['tests/vitest-legacy/**/*.{test,spec}.js'],
    environment: 'node',
  },
})
