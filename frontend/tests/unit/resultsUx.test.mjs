/**
 * 结果展示页体验优化 缺陷修复单元测试
 * 文件: frontend/tests/unit/resultsUx.test.mjs
 * 运行: node --test frontend/tests/unit/resultsUx.test.mjs
 *
 * 三项用户反馈的锚点回归（源码锚点风格与 W14 系列一致）：
 * 1. 选任务/切版本无加载反馈 → 结果区 v-loading + 空态占位（min-height 保证可见）
 * 2. 结果展示慢 → 历史接口轻量模式（include_result=false）+ 单版全量端点
 *    （/history/{n}）+ 旧后端回退（version.result 内嵌时不再请求单版）
 * 3. 雷达图悬停全量罗列指标且溢出 → tooltip 关闭、分值经 axisName.formatter
 *    直显图上；渲染统一走 initChart（dispose 旧实例）
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const vueSrc = readFileSync(join(here, '../../src/views/Results.vue'), 'utf-8')
const apiSrc = readFileSync(join(here, '../../src/api/index.js'), 'utf-8')

describe('结果页加载反馈', () => {
  it('结果区与空态占位区都挂 v-loading（选任务/切版本有可见反馈）', () => {
    assert.match(vueSrc, /v-if="result" v-loading="resultLoading"/, '有结果时切版本应显示加载遮罩')
    assert.match(vueSrc, /class="result-empty-placeholder"[\s\S]{0,200}v-loading="resultLoading"/,
      '无结果时空态占位区应显示加载遮罩（替代无反馈白屏）')
    assert.match(vueSrc, /result-empty-placeholder \{[\s\S]{0,120}min-height:\s*\d+px/,
      '占位区需 min-height 保证 spinner 可见')
    assert.match(vueSrc, /const resultLoading = ref\(false\)/, 'resultLoading 响应式状态存在')
  })

  it('加载函数设置/复位 loading（onTaskChange 与 loadResultByVersion）', () => {
    const onTaskChange = vueSrc.slice(vueSrc.indexOf('const onTaskChange'), vueSrc.indexOf('const loadResultByVersion'))
    assert.match(onTaskChange, /resultLoading\.value = true/, 'onTaskChange 开启 loading')
    assert.match(onTaskChange, /finally\s*{[\s\S]*resultLoading\.value = false/, 'onTaskChange finally 复位 loading')

    const loadByVersion = vueSrc.slice(vueSrc.indexOf('const loadResultByVersion'), vueSrc.indexOf('const loadResult = async'))
    assert.match(loadByVersion, /resultLoading\.value = true/, 'loadResultByVersion 开启 loading')
    assert.match(loadByVersion, /finally\s*{[\s\S]*resultLoading\.value = false/, 'loadResultByVersion finally 复位 loading')
  })
})

describe('结果展示慢：轻量历史 + 单版全量', () => {
  it('历史请求两处（onTaskChange/轮询刷新）都走 include_result=false 轻量模式', () => {
    const calls = vueSrc.match(/resultApi\.history\([^)]*\)/g) || []
    assert.ok(calls.length >= 2, `历史接口应至少 2 处调用，实际 ${calls.length}`)
    for (const call of calls) {
      assert.match(call, /include_result:\s*false/, `历史调用须为轻量模式: ${call}`)
    }
  })

  it('版本加载走单版端点，旧后端（历史内嵌 result）不重复请求', () => {
    assert.match(vueSrc, /if\s*\(!version\.result\)\s*{[\s\S]{0,200}resultApi\.historyVersion\(/,
      '仅当轻量历史不含 result 时才请求单版端点（旧后端兼容回退）')
    assert.match(apiSrc, /historyVersion:\s*\(id,\s*executionNumber\)\s*=>\s*request\.get\(`\/evaluation\/results\/\$\{id\}\/history\/\$\{executionNumber\}`\)/,
      'api 层导出单版结果方法')
  })
})

describe('雷达图分值直显', () => {
  it('通用雷达与 RAG 雷达都关闭全量 tooltip、改用 axisName.formatter 直显分值', () => {
    const tooltips = vueSrc.match(/tooltip:\s*\{\s*show:\s*false\s*\}/g) || []
    assert.ok(tooltips.length >= 2, `两个雷达图都应关闭 tooltip，实际 ${tooltips.length} 处`)
    const formatters = vueSrc.match(/formatter:\s*\(name\)\s*=>\s*{[\s\S]{0,200}scoreByName\.get\(name\)/g) || []
    assert.ok(formatters.length >= 2, '两个雷达图都应通过 formatter 把分值拼进指标名')
  })

  it('图表渲染统一走 initChart（渲染前 dispose 旧实例）', () => {
    assert.match(vueSrc, /const initChart = \(dom\) =>\s*{\s*const existing = echarts\.getInstanceByDom\(dom\)\s*if \(existing\) existing\.dispose\(\)/)
    const inits = vueSrc.match(/initChart\(chartDom\)/g) || []
    // 8 个图表渲染函数：单次执行雷达/折线/占比饼/分数分布 + 多次执行雷达/折线（!69）+ 耗时直方图 + RAG 雷达
    assert.equal(inits.length, 8, `8 个图表渲染函数都应使用 initChart，实际 ${inits.length}`)
    assert.ok(!/=\s*echarts\.init\(chartDom\)/.test(vueSrc), '不再直接 echarts.init(chartDom)')
  })
})
