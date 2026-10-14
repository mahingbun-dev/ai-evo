/**
 * Task 6: 前端 - 评测类型选择和配置区
 * 测试文件: frontend/tests/vitest-legacy/Tasks.latency.spec.js
 *
 * W8 修复（整改计划 §3 W8「6 个既有前端测试修复到可运行」）：
 * 原文件因 package.json 无 test 脚本从未运行过，且断言为
 * `expect(true).toBe(true)` 占位（文件头注释自述「已通过 grep 验证」）。
 * 现随 vitest 接入（package.json test:unit + vitest.config.js）真正执行，
 * 并把原本口头「grep 验证」的内容固化为对 Tasks.vue 真实源码的断言。
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, dirname } from 'node:path'

const tasksVue = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '../../src/views/Tasks.vue'),
  'utf-8'
)

// 测试场景
describe('Tasks.latency', () => {
  /**
   * test_latency_option_exists: 验证评测类型下拉框中存在 latency 选项
   */
  it('test_latency_option_exists', () => {
    expect(tasksVue).toContain('value="latency"')
    expect(tasksVue).toContain('响应耗时评测')
  });

  /**
   * test_latency_config_form_renders: 验证当选择 latency 类型时，耗时配置区正确渲染
   */
  it('test_latency_config_form_renders', () => {
    // latency 配置区由 evaluation_type === 'latency' 条件渲染
    expect(tasksVue).toContain("form.evaluation_type === 'latency'")
    // 配置项绑定 latency_config（阈值 / 次数 / 计分方式）
    expect(tasksVue).toContain('form.latency_config.threshold')
    expect(tasksVue).toContain('form.latency_config.repeat_count')
    expect(tasksVue).toContain('form.latency_config.score_mode')
  });

  /**
   * test_latency_config_defaults: 验证 form.latency_config 有正确的默认值
   */
  it('test_latency_config_defaults', () => {
    // Tasks.vue 表单默认值（与后端 latency 评测约定的缺省一致）
    expect(tasksVue).toContain('latency_config: {')
    expect(tasksVue).toContain('threshold: 10.0')
    expect(tasksVue).toContain('repeat_count: 1')
    expect(tasksVue).toContain("score_mode: 'inverse'")
  });
});
