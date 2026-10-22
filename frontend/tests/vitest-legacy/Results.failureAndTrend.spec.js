/**
 * 失败数据状态列/失败详情弹窗 + 裁判维度理由 tooltip + 多次执行聚合展示
 * 测试文件: frontend/tests/vitest-legacy/Results.failureAndTrend.spec.js
 *
 * 沿用 Results.latency.spec.js 的源码字符串断言模式：直接读取
 * Results.vue 与 src/api/index.js 源码，对关键实现契约做断言，
 * 防止后续重构悄悄破坏这些行为（合并失败行/idx 轴/安全转义/兜底链等）。
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join, dirname } from 'node:path'

const dir = dirname(fileURLToPath(import.meta.url))
const resultsVue = readFileSync(join(dir, '../../src/views/Results.vue'), 'utf-8')
const apiIndex = readFileSync(join(dir, '../../src/api/index.js'), 'utf-8')

// 提取单个箭头函数源码块（从声明行到第一个行首 `}`）
const extractArrowFn = (source, signature) => {
  const start = source.indexOf(signature)
  if (start === -1) return null
  const end = source.indexOf('\n}', start)
  if (end === -1) return null
  return source.slice(start, end)
}

describe('Results.failureAndTrend', () => {
  it('safeGetItems 合并 failed_items 并按 idx 升序稳定排序', () => {
    const body = extractArrowFn(resultsVue, 'const safeGetItems = (resultData) => {')
    expect(body).toBeTruthy()
    // 失败行合并进明细（concat），非空数组才合并
    expect(body).toContain('Array.isArray(resultData.failed_items)')
    expect(body).toContain('concat(resultData.failed_items)')
    // idx 升序：缺失 idx 排最后，同 idx 以 order 兜底保持合并前相对顺序（稳定）
    expect(body).toContain('a.order - b.order')
    expect(body).toContain('Number.MAX_SAFE_INTEGER')
    // 原有回退顺序保持：items → results → detail_results
    const itemsIdx = body.indexOf('resultData.items')
    const resultsIdx = body.indexOf('resultData.results')
    const detailIdx = body.indexOf('resultData.detail_results')
    expect(itemsIdx).toBeGreaterThan(-1)
    expect(resultsIdx).toBeGreaterThan(itemsIdx)
    expect(detailIdx).toBeGreaterThan(resultsIdx)
  })

  it('主表明细表序号列使用 detailSeqNo 口径并新增失败状态列', () => {
    // 序号列以数据自带 idx 为准（合并失败行后 $index 错位）；idx 全行存在且唯一
    // 才按 idx+1，历史脏数据（全行 idx=0）整体回退 $index（utils/seqno.js 口径）
    expect(resultsVue).toContain('detailSeqNo(row, $index, seqNoFromIdx)')
    expect(resultsVue).toContain("import { canUseIdxAsSeqNo, detailSeqNo } from '@/utils/seqno'")
    // 状态列四种判定
    expect(resultsVue).toContain('row.success === false')
    expect(resultsVue).toContain("row.row_status === 'judge_failed'")
    expect(resultsVue).toContain("row.row_status === 'empty_input'")
    // 四种状态文案
    expect(resultsVue).toContain('执行失败')
    expect(resultsVue).toContain('裁判失败')
    expect(resultsVue).toContain('空输入')
    expect(resultsVue).toContain('成功')
    // 失败 tag（danger/warning）点击打开失败详情弹窗
    expect(resultsVue).toContain('@click="openFailureDetail(row)"')
  })

  it('failureDialog 弹窗：失败阶段判定、分区内容与安全转义', () => {
    // 弹窗状态与打开函数
    expect(resultsVue).toContain('const failureDialog = reactive({')
    expect(resultsVue).toContain('const openFailureDetail')
    // 失败阶段判定链：前处理脚本失败 → 模型调用失败（error 前缀）→ 手动停止（error 含任务已停止）→ 裁判评测失败 → 执行异常
    expect(resultsVue).toContain("row.row_status === 'pre_script_failed'")
    expect(resultsVue).toContain("startsWith('模型调用失败')")
    expect(resultsVue).toContain("includes('任务已停止')")
    expect(resultsVue).toContain("'前处理脚本失败'")
    expect(resultsVue).toContain("'模型调用失败'")
    expect(resultsVue).toContain("'手动停止'")
    expect(resultsVue).toContain("'裁判评测失败'")
    expect(resultsVue).toContain("'执行异常'")
    // 安全红线：错误信息必须经 renderContent 转义后 v-html，不落原文
    expect(resultsVue).toContain('v-html="renderContent(failureDialog.row.error')
    // 裁判失败行展示各裁判 fail_reason（js.judge_failed 为真），无则回退 row.error
    expect(resultsVue).toContain('failureJudgeFailReasons')
    expect(resultsVue).toContain('js.judge_failed')
    expect(resultsVue).toContain('js.fail_reason')
    // 数据输入：排除结构化字段与下划线开头的内部字段；
    // 用户输入/参考答案等带中文标签的标准字段是失败现场关键上下文，需放行展示
    expect(resultsVue).toContain('!STRUCTURED_KEYS.has(k)')
    expect(resultsVue).toContain("!k.startsWith('_')")
    expect(resultsVue).toContain('(!STRUCTURED_KEYS.has(k) || VARIABLE_LABEL_MAP[k])')
    // 模型输出长文本点击经 openDetail 看全文；脚本产物提示详见上方专区
    expect(resultsVue).toContain("openDetail('模型输出', failureDialog.row.model_output)")
    expect(resultsVue).toContain('详见上方脚本产物区')
    // on_fail=abort / 脚本自定义报错的行不写 row_status，靠 error 文本「前处理脚本」归因
    const stageFn = extractArrowFn(resultsVue, 'const getFailureStage = (row) => {')
    expect(stageFn).toBeTruthy()
    expect(stageFn).toContain("includes('前处理脚本')")
  })

  it('裁判详情弹窗 dimensionFeedbacks 兜底链与维度 tooltip', () => {
    // 取值链：js.dimension_feedbacks → js.judge_details?.dimension_details 派生 reasoning → null
    expect(resultsVue).toMatch(/js\.dimension_feedbacks/)
    expect(resultsVue).toMatch(/js\.judge_details\?\.dimension_details/)
    expect(resultsVue).toContain('detail.reasoning')
    // openJudgeDetail 填充 dimensionFeedbacks
    expect(resultsVue).toContain('judgeDetailDialog.dimensionFeedbacks = getDimensionFeedbacks(js)')
    // 各维度评分行 tooltip：有理由显示理由；没有则回退整体反馈（旧版数据）文案
    expect(resultsVue).toContain('getDimensionTooltip(name)')
    expect(resultsVue).toContain('旧版数据无分维度理由。该裁判整体反馈：')
    expect(resultsVue).toContain('暂无理由数据')
  })

  it('resultApi.dimensionTrend 封装多次执行维度趋势端点', () => {
    expect(apiIndex).toContain("dimensionTrend: (id, limit = 50) => request.get('/evaluation/results/' + id + '/dimension-trend', { params: { limit } })")
  })

  it('多次执行聚合展示区块：条件渲染、三张卡与数据拉取', () => {
    // v-if 与通用图表区同条件（排除专属类型）且至少 2 次执行
    expect(resultsVue).toContain('executionTrend.length >= 2')
    // 三张卡：雷达 / 折线 / 波动表
    expect(resultsVue).toContain('id="trendRadarChart"')
    expect(resultsVue).toContain('id="trendLineChart"')
    expect(resultsVue).toContain('多次执行聚合展示')
    expect(resultsVue).toContain('多次执行维度对比')
    expect(resultsVue).toContain('维度分数趋势')
    expect(resultsVue).toContain('跨执行波动')
    expect(resultsVue).toContain('trendVolatilityRows')
    // 缺失维度走 getTrendDimensionAverage 返回 null 断口（禁止补 0）
    expect(resultsVue).toContain('const getTrendDimensionAverage')
    expect(resultsVue).toContain('connectNulls: false')
    // onTaskChange 拉取趋势：失败静默置空（旧后端无端点时聚合区整体隐藏）
    expect(resultsVue).toMatch(/resultApi\.dimensionTrend\(taskId, 50\)\.catch\(\(\) => \[\]\)/)
    // 轮询观察到任务完成后同步重拉趋势（onTaskUpdate 完成分支），聚合区不停留旧态
    expect(resultsVue).toMatch(/resultApi\.dimensionTrend\(taskId, 50\)\.then\(async trend =>/)
    // 切换任务时重置为 []
    expect(resultsVue).toContain('executionTrend.value = []')
    // 波动幅度 > 1.0 用 #E6A23C 强调
    expect(resultsVue).toContain('row.range > 1.0')
    expect(resultsVue).toContain('#E6A23C')
  })

  it('renderTrendRadarChart 显式按 execution_number 排序（不依赖后端倒序传输）', () => {
    const body = extractArrowFn(resultsVue, 'const renderTrendRadarChart = (trend) => {')
    expect(body).toBeTruthy()
    // 后端 dimension-trend 按 execution_number 倒序返回：雷达图先显式升序整理再 reverse，
    // 保证"最近一次排最前"与 legend 默认勾选最近 5 次不受传输顺序影响
    expect(body).toContain("sort((a, b) => (a?.execution_number ?? 0) - (b?.execution_number ?? 0))")
    expect(body).toContain('.reverse()')
    expect(body).toContain('TREND_LEGEND_VISIBLE_COUNT')
  })

  it('renderLineChart 只统计已评分行且 x 轴使用 idx', () => {
    const body = extractArrowFn(resultsVue, 'const renderLineChart = (items) => {')
    expect(body).toBeTruthy()
    // 合并进来的失败行（average_score 为 undefined）不参与趋势
    expect(body).toContain('average_score !== undefined')
    // x 轴用数据自带 idx + 1，防合并失败行后错位；idx 可信度按全量明细行判定
    // （历史脏数据全行 idx=0 时回退位置序号，utils/seqno.js 口径）
    expect(body).toContain('detailSeqNo(item, i, seqNoFromIdx.value)')
    // x 轴与 data 均来自过滤后的 scoredItems
    expect(body).toContain('scoredItems')
  })

  it('renderCharts 在通用类型分支末尾挂载 renderTrendCharts', () => {
    expect(resultsVue).toContain('const renderTrendCharts')
    expect(resultsVue).toContain('nextTick(() => renderTrendCharts())')
  })

  it('失败数量统计卡可点击查看失败原因汇总', () => {
    // 卡片：有失败行才有可点击态与 hover 暗示；点击打开汇总弹窗
    expect(resultsVue).toContain("'stat-card-clickable': (result.result.failed_count || 0) > 0")
    expect(resultsVue).toContain("'点击查看失败原因汇总'")
    expect(resultsVue).toContain('@click="openFailureSummary"')
    // 打开守卫：无失败行不响应；总数与主表失败行同源
    const openFn = extractArrowFn(resultsVue, 'const openFailureSummary = () => {')
    expect(openFn).toBeTruthy()
    expect(openFn).toContain("r && r.success === false")
    expect(openFn).toContain('if (failedRows.length === 0) return')
    // 分组：按 error 文案归组、空文案兜底、按条数降序；pos 为合并后表格行序
    const groupFn = extractArrowFn(resultsVue, 'const failureReasonGroups = computed(() => {')
    expect(groupFn).toBeTruthy()
    expect(groupFn).toContain("'暂无错误信息'")
    expect(groupFn).toContain("sort((a, b) => b.count - a.count)")
    expect(groupFn).toContain('pos: i + 1')
    // 弹窗：原因文本经 renderContent 转义后 v-html；行号点击打开该条失败详情
    expect(resultsVue).toContain('title="失败原因汇总"')
    expect(resultsVue).toContain('v-html="renderContent(group.reason)"')
    expect(resultsVue).toContain('@click="openFailureDetail(r.row)"')
  })
})
