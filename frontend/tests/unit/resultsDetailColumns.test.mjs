/**
 * 结果页「详细结果」表格列口径单元测试
 * 文件: frontend/tests/unit/resultsDetailColumns.test.mjs
 * 运行: node --test frontend/tests/unit/resultsDetailColumns.test.mjs
 *
 * 口径：序号/状态/评分/裁判数/反馈为固定列，其余列由待评测内容字段动态生成；
 * 模型输出/原始响应/会话摘要/会话来源不再作为详细结果列展示。
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const vueSrc = readFileSync(join(here, '../../src/views/Results.vue'), 'utf-8')

// 详细结果表格模板区（从区块注释到该表结束）
const detailTableSrc = (() => {
  const start = vueSrc.indexOf('<!-- 详细结果表格')
  const end = vueSrc.indexOf('</el-table>', start)
  assert.ok(start !== -1 && end !== -1, '未找到详细结果表格模板区')
  return vueSrc.slice(start, end)
})()

describe('详细结果表格列口径', () => {
  it('固定列按 序号/状态/评分/裁判数/反馈 顺序出现', () => {
    const labels = ['序号', '状态', '评分', '裁判数', '反馈'].map(label =>
      detailTableSrc.indexOf(`label="${label}"`)
    )
    labels.forEach((pos, i) => {
      assert.notEqual(pos, -1, `缺少固定列: ${['序号', '状态', '评分', '裁判数', '反馈'][i]}`)
      if (i > 0) assert.ok(pos > labels[i - 1], '固定列顺序应为 序号→状态→评分→裁判数→反馈')
    })
  })

  it('反馈列之后为动态待评测内容列', () => {
    const feedbackPos = detailTableSrc.indexOf('label="反馈"')
    const dynamicPos = detailTableSrc.indexOf('getInputColumns(resultItems)')
    assert.notEqual(dynamicPos, -1, '详细结果表格应保留动态输入列')
    assert.ok(dynamicPos > feedbackPos, '动态列应排在反馈之后')
  })

  it('不再展示 模型输出/原始响应/会话摘要/会话来源 列', () => {
    for (const label of ['模型输出', '原始响应', '会话摘要', '会话来源']) {
      assert.ok(!detailTableSrc.includes(`label="${label}"`), `详细结果表格不应再包含列: ${label}`)
    }
    assert.ok(!detailTableSrc.includes('formatSessionSource'), '会话来源列已移除，不应残留格式化调用')
  })

  it('动态列沿用 getInputColumns 口径（数据集标准字段仍被排除）', () => {
    const structuredBlock = vueSrc.slice(
      vueSrc.indexOf('const STRUCTURED_KEYS = new Set(['),
      vueSrc.indexOf('])', vueSrc.indexOf('const STRUCTURED_KEYS = new Set(['))
    )
    for (const key of ['user_input', 'ref_answer', 'question', 'ground_truth', 'model_answer', 'contexts']) {
      assert.ok(structuredBlock.includes(`'${key}'`), `动态列口径应继续排除数据集标准字段: ${key}`)
    }
  })
})
