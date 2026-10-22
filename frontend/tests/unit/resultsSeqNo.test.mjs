/**
 * 详细结果序号口径 + ECharts tooltip confine 单元测试
 * 文件: frontend/tests/unit/resultsSeqNo.test.mjs
 * 运行: node --test frontend/tests/unit/resultsSeqNo.test.mjs
 *
 * 背景（缺陷）：
 * - 序号全为 1：judge/agent 默认执行路径 payload 缺 idx（执行器
 *   payload.get("idx", 0) 兜底），历史脏数据全部行 idx=0 入库；结果页
 *   序号列/分数趋势图 x 轴按 idx+1 展示 → 全列 1、坐标轴全为「1」。
 *   修复口径：idx 全行存在且唯一才按 idx+1，否则整体回退位置序号。
 * - 雷达 tooltip 遮挡：多次执行维度对比雷达 tooltip 未设 confine，
 *   9 维度 + 标题的 tooltip 悬停靠下的点时向上溢出图表容器被卡片遮挡。
 *
 * 覆盖：
 * - canUseIdxAsSeqNo：健康数据/全 0 脏数据/缺 idx/非整数/空列表
 * - detailSeqNo：idx 序号与位置序号两口径，不混排
 * - Results.vue 契约锚点：序号列、分数趋势图 x 轴、脚本产物下拉接线；
 *   趋势雷达/趋势折线/分数分布/占比饼图/耗时直方图 tooltip confine
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { canUseIdxAsSeqNo, detailSeqNo } from '../../src/utils/seqno.js'

const here = dirname(fileURLToPath(import.meta.url))
const resultsSrc = readFileSync(join(here, '../../src/views/Results.vue'), 'utf-8')

// ---------------- canUseIdxAsSeqNo：idx 序号可用性判定 ----------------

describe('canUseIdxAsSeqNo', () => {
  it('健康数据（0..N-1 唯一整数）可用 idx 序号', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: 0 }, { idx: 1 }, { idx: 2 }]), true)
  })

  it('乱序但唯一仍可用（合并排序后由 safeGetItems 保证顺序）', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: 2 }, { idx: 0 }, { idx: 1 }]), true)
  })

  it('历史脏数据全行 idx=0（重复）不可用——否则序号全为 1', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: 0 }, { idx: 0 }, { idx: 0 }]), false)
  })

  it('部分行缺 idx 不可用（两种口径不混排一列）', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: 0 }, {}, { idx: 2 }]), false)
  })

  it('非整数 idx 不可用（防御性：字符串/浮点一律回退位置序号）', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: '0' }, { idx: '1' }]), false)
    assert.equal(canUseIdxAsSeqNo([{ idx: 0.5 }, { idx: 1.5 }]), false)
  })

  it('idx 为 null/undefined 视同缺失', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: null }, { idx: 1 }]), false)
    assert.equal(canUseIdxAsSeqNo([{ idx: undefined }, { idx: 1 }]), false)
  })

  it('空列表与非列表输入不可用', () => {
    assert.equal(canUseIdxAsSeqNo([]), false)
    assert.equal(canUseIdxAsSeqNo(null), false)
    assert.equal(canUseIdxAsSeqNo(undefined), false)
  })

  it('单行数据 idx=0 可用（唯一即可信，一行的序号 1 是正确的）', () => {
    assert.equal(canUseIdxAsSeqNo([{ idx: 0 }]), true)
  })
})

// ---------------- detailSeqNo：两口径取值 ----------------

describe('detailSeqNo', () => {
  it('idx 可信时用 idx+1（对齐数据集行位）', () => {
    assert.equal(detailSeqNo({ idx: 4 }, 1, true), 5)
    assert.equal(detailSeqNo({ idx: 0 }, 0, true), 1)
  })

  it('idx 不可信时回退位置序号（1 起密集编号）', () => {
    assert.equal(detailSeqNo({ idx: 0 }, 0, false), 1)
    assert.equal(detailSeqNo({ idx: 0 }, 2, false), 3)
  })

  it('usable=true 但单行缺 idx 时该行也回退位置序号（防御）', () => {
    assert.equal(detailSeqNo({}, 3, true), 4)
    assert.equal(detailSeqNo(null, 3, true), 4)
  })
})

// ---------------- Results.vue 契约锚点：三处接线 + confine ----------------

describe('Results.vue 序号口径接线契约', () => {
  it('序号列使用 detailSeqNo（不再裸读 row.idx）', () => {
    assert.ok(
      resultsSrc.includes('detailSeqNo(row, $index, seqNoFromIdx)'),
      '详细结果序号列未接线 detailSeqNo'
    )
    assert.ok(
      !resultsSrc.includes("row.idx !== undefined ? row.idx + 1 : $index + 1"),
      '序号列残留旧的内联 idx 判断'
    )
  })

  it('seqNoFromIdx 按全量明细行（resultItems）判定', () => {
    assert.ok(
      resultsSrc.includes('canUseIdxAsSeqNo(resultItems.value)'),
      'seqNoFromIdx 必须基于合并后的全量明细行判定（过滤子集会掩盖重复 idx）'
    )
  })

  it('分数趋势图 x 轴接线 detailSeqNo', () => {
    assert.ok(
      resultsSrc.includes('detailSeqNo(item, i, seqNoFromIdx.value)'),
      '分数趋势图 x 轴未接线 detailSeqNo（脏数据时坐标轴全为 1）'
    )
  })

  it('脚本产物下拉接线 detailSeqNo', () => {
    assert.ok(
      resultsSrc.includes('detailSeqNo(entry, i, seqNoFromIdx)'),
      '脚本产物数据条目下拉未接线 detailSeqNo'
    )
  })
})

describe('ECharts tooltip confine 契约（防溢出图表容器被卡片遮挡）', () => {
  const sites = [
    ['多次执行维度对比雷达', /tooltip: \{ trigger: 'item', confine: true \}[\s\S]{0,120}radar: \{/],
    ['多次执行维度分数趋势折线', /tooltip: \{ trigger: 'axis', confine: true \},\n\s*color: TREND_SERIES_COLORS/],
    ['单次执行分数趋势折线', /tooltip: \{ trigger: 'axis', confine: true \},\n\s*xAxis: \{\n\s*type: 'category',\n\s*data: scoredItems\.map/],
    ['成功失败占比饼图', /tooltip: \{ trigger: 'item', confine: true \},\n\s*series: \[\{\n\s*type: 'pie'/],
    ['分数分布直方图', /tooltip: \{ trigger: 'axis', confine: true \},\n\s*xAxis: \{\n\s*type: 'category',\n\s*data: Object\.keys\(buckets\)/],
    ['耗时分布直方图', /trigger: 'axis',\n\s*confine: true,\n\s*formatter: \(params\) => \{\n\s*const p = params\[0\]\n\s*return `\$\{p\.name\}<br\/>请求数: \$\{p\.value\}`/],
  ]
  for (const [name, re] of sites) {
    it(`${name} tooltip 设置 confine`, () => {
      assert.ok(re.test(resultsSrc), `${name} tooltip 缺少 confine: true`)
    })
  }
})
