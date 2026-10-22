/**
 * 评测维度权重选择（数值权重兼容）单元测试
 * 文件: frontend/tests/unit/weightSelect.test.mjs
 * 运行: node --test frontend/tests/unit/weightSelect.test.mjs
 *
 * 背景（缺陷）：
 * - 后端权重契约 = 预设标签 high/medium/low（WEIGHT_MAP→3/2/1）或数值（0.05 等，
 *   hub 插件建任务常用形态）；前端权重下拉却只有三个固定选项，且列宽 80px，
 *   数值权重回显被截断为「0.0...」，下拉无选中态、无法配置数值。
 * - 修复：下拉加「非预设值动态选项 + filterable allow-create」，allow-create
 *   产生的数字字符串必须归一化为 Number（后端加权计算对字符串走
 *   WEIGHT_MAP.get(val, 1)，"0.05" 会被误读成 1）。
 *
 * 覆盖：
 * - normalizeMetricWeight：数字字符串→Number；标签/垃圾串/空值原样透传
 * - isCustomWeight：预设三值与空值不算自定义，数值/中文标签算
 * - Tasks.vue / Templates.vue 契约锚点：动态选项、allow-create、归一化接线、列宽
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  WEIGHT_PRESETS,
  isCustomWeight,
  normalizeMetricWeight,
} from '../../src/utils/weight.js'

const here = dirname(fileURLToPath(import.meta.url))
const tasksSrc = readFileSync(join(here, '../../src/views/Tasks.vue'), 'utf-8')
const templatesSrc = readFileSync(join(here, '../../src/views/Templates.vue'), 'utf-8')

// ---------------- normalizeMetricWeight：数字字符串归一化 ----------------

describe('normalizeMetricWeight', () => {
  it('十进制数字字符串转为 Number（allow-create 输入路径）', () => {
    assert.equal(normalizeMetricWeight('0.05'), 0.05)
    assert.equal(normalizeMetricWeight('0.6'), 0.6)
    assert.equal(normalizeMetricWeight('2'), 2)
    assert.equal(normalizeMetricWeight(' 0.05 '), 0.05)
    assert.ok(Number.isFinite(normalizeMetricWeight('0.05')))
  })

  it('预设标签与中文标签原样透传（不走数值分支）', () => {
    assert.equal(normalizeMetricWeight('high'), 'high')
    assert.equal(normalizeMetricWeight('medium'), 'medium')
    assert.equal(normalizeMetricWeight('low'), 'low')
    assert.equal(normalizeMetricWeight('高'), '高')
  })

  it('非数字字符串原样透传（与既有 WEIGHT_MAP 标签映射行为兼容）', () => {
    assert.equal(normalizeMetricWeight('abc'), 'abc')
    assert.equal(normalizeMetricWeight('12abc'), '12abc')
  })

  it('空值与空白原样透传（Number("") === 0 的陷阱必须避开）', () => {
    assert.equal(normalizeMetricWeight(''), '')
    assert.equal(normalizeMetricWeight('   '), '   ')
    assert.equal(normalizeMetricWeight(null), null)
    assert.equal(normalizeMetricWeight(undefined), undefined)
  })

  it('已是 Number 的权重原样透传（任务详情回填路径）', () => {
    assert.equal(normalizeMetricWeight(0.05), 0.05)
    assert.equal(normalizeMetricWeight(0), 0)
  })
})

// ---------------- isCustomWeight：动态选项渲染条件 ----------------

describe('isCustomWeight', () => {
  it('预设三值不算自定义（走固定选项回显）', () => {
    for (const w of WEIGHT_PRESETS) {
      assert.equal(isCustomWeight(w), false, `预设值 ${w} 不应判为自定义`)
    }
  })

  it('空值不算自定义（避免渲染 label=undefined 的空选项）', () => {
    assert.equal(isCustomWeight(undefined), false)
    assert.equal(isCustomWeight(null), false)
    assert.equal(isCustomWeight(''), false)
  })

  it('数值与中文标签算自定义（需要动态选项回显选中态）', () => {
    assert.equal(isCustomWeight(0.05), true)
    assert.equal(isCustomWeight(0.6), true)
    assert.equal(isCustomWeight('0.05'), true)
    assert.equal(isCustomWeight('高'), true)
  })
})

// ---------------- 视图契约锚点：两处权重下拉同步修复 ----------------

describe('权重下拉视图契约（Tasks.vue / Templates.vue）', () => {
  for (const [name, src] of [['Tasks.vue', tasksSrc], ['Templates.vue', templatesSrc]]) {
    it(`${name}：权重下拉启用 allow-create 并接线归一化`, () => {
      assert.ok(src.includes('allow-create'), `${name} 权重下拉缺少 allow-create`)
      assert.ok(src.includes('default-first-option'), `${name} 权重下拉缺少 default-first-option`)
      assert.ok(
        src.includes('metric.weight = normalizeMetricWeight(val)'),
        `${name} @change 未接线 normalizeMetricWeight——数字字符串会以后端 WEIGHT_MAP 误读为 1`
      )
      assert.ok(
        src.includes("import { isCustomWeight, normalizeMetricWeight } from '@/utils/weight'"),
        `${name} 缺少 weight 工具导入`
      )
    })

    it(`${name}：非预设权重渲染动态选项（置于固定选项之前）`, () => {
      const dynamicOpt = src.indexOf('isCustomWeight(metric.weight)')
      const presetOpt = src.indexOf('value="high"')
      assert.ok(dynamicOpt > -1, `${name} 缺少非预设权重动态选项`)
      assert.ok(presetOpt > dynamicOpt, `${name} 动态选项应位于固定选项之前`)
    })

    it(`${name}：权重/最高分列宽加宽至 100px（80px 下 0.05 截断为 0.0...）`, () => {
      assert.ok(src.includes('flex: 0 0 100px'), `${name} .metric-field-xs 列宽未加宽`)
      assert.ok(!src.includes('flex: 0 0 80px'), `${name} 仍残留 80px 列宽`)
    })
  }
})
