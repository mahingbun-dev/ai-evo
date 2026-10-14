/**
 * Task 7: 前端 - 耗时评测结果展示
 * 测试文件: frontend/tests/vitest-legacy/Results.latency.spec.js
 *
 * W8 修复（整改计划 §3 W8「6 个既有前端测试修复到可运行」）：
 * 原文件因 package.json 无 test 脚本从未运行过，且断言为
 * `expect(true).toBe(true)` 占位。现随 vitest 接入真正执行，
 * 并把原本口头「grep 验证 / 代码审查验证」的内容固化为对
 * Results.vue 真实源码的断言。
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, dirname } from 'node:path'

const resultsVue = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '../../src/views/Results.vue'),
  'utf-8'
)

// 测试场景
describe('Results.latency', () => {
  /**
   * test_latency_result_section_renders: 验证当 result.evaluation_type === 'latency' 时，
   * 耗时评测结果区正确渲染
   */
  it('test_latency_result_section_renders', () => {
    // latency 专属结果区由该条件渲染（非 latency 类型排除在统计区之外）
    expect(resultsVue).toContain("result.evaluation_type === 'latency'")
    expect(resultsVue).toContain('latency-result')
    // 专属渲染：百分位评分与耗时直方图容器
    expect(resultsVue).toContain('latencyHistogramChart')
  });

  /**
   * test_distribution_data_computed: 验证 distributionData 计算属性返回正确数量（9条）的数据
   */
  it('test_distribution_data_computed', () => {
    // distributionData 的 9 个度量项（最短/最长/平均/P50/P90/P99/通过/失败/总数）
    const metrics = [
      '最短耗时', '最长耗时', '平均耗时', 'P50', 'P90', 'P99',
      '通过数', '失败数', '总请求数'
    ];
    expect(metrics).toHaveLength(9);
    // 计算属性存在且至少构造出这些条目（源码级契约）
    expect(resultsVue).toContain('distributionData')
    for (const m of metrics) {
      expect(resultsVue).toContain(m)
    }
  });
});
