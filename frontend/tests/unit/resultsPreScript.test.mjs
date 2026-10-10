/**
 * 二期前端·预置脚本产物渲染 缺陷修复单元测试
 * 文件: frontend/tests/unit/resultsPreScript.test.mjs
 * 运行: node --test frontend/tests/unit/resultsPreScript.test.mjs
 *
 * 验证方式与验收方一致：从 Results.vue 提取「脚本产物 + 结果观察」区段的
 * 真实函数源码，在受控上下文中求值，喂以后端真实挂载形态的数据：
 * - observations 附加在每条 result 上（app/services/pre_judge_runner.py
 *   run_result_observers 写 result["observations"]），聚合后经
 *   app/services/result_aggregator.py::_format_items 平铺为
 *   result.items[i].observations（聚合顶层无该键）
 * - log_grep 命中样例嵌套在 output.patterns[i].sample_lines
 *   （app/scripts_builtin/log_grep.py）
 * - xml_diff 差异行键为 type/path/tag/attribute/a/b（app/scripts_builtin/xml_diff.py）
 * - db_assert 断言行键为 name/sql/passed/expected/actual/error
 *   （app/scripts_builtin/db_assert.py）
 * - 观察条目键为 extension_point/observer/status/output/files/summary/error/
 *   duration_ms（app/services/pre_judge_runner.py::_observer_entry）
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const vueSrc = readFileSync(join(here, '../../src/views/Results.vue'), 'utf-8')

// ---------------- 真实源码提取 ----------------

const START_MARK = '// ---------------- 脚本产物（评测前处理脚本 pre_script_results） ----------------'
const END_MARK = '\nconst loadTasks = async () => {'
const startIdx = vueSrc.indexOf(START_MARK)
const endIdx = vueSrc.indexOf(END_MARK)
if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx) {
  throw new Error('未能在 Results.vue 中定位「脚本产物/结果观察」区段（源码结构可能已变化，请修正测试锚点）')
}

/**
 * 在受控上下文中求值 Results.vue 真实区段源码，返回待测函数集合。
 * Vue 运行时（computed/ref）与展示层依赖（truncateText/safePercent/openDetail 等）
 * 以与真实实现语义一致的桩替换；openDetail 调用被记录用于断言弹窗内容。
 */
function buildApi(host) {
  const openDetailCalls = []
  const ctx = {
    computed: (fn) => fn,
    ref: (v) => ({ value: v }),
    result: { value: host },
    // 与 Results.vue 真实实现一致的展示辅助（src 内 truncateText/safePercent 原样复刻）
    truncateText: (text, maxLen = 80) => {
      if (!text) return '-'
      const str = String(text)
      return str.length > maxLen ? str.slice(0, maxLen) + '...' : str
    },
    safePercent: (val) => (val == null || isNaN(Number(val)) ? '-' : (Number(val) * 100).toFixed(1)),
    openDetail: (title, content) => openDetailCalls.push({ title, content }),
    downloadBlob: () => {},
    ElMessage: { success: () => {} },
  }
  const body = vueSrc.slice(startIdx, endIdx)
  const factory = new Function(
    ...Object.keys(ctx),
    `${body}\nreturn {
      preScriptEntries, currentPreScriptList,
      formatCellValue, pickRowField, getPreScriptKind,
      XML_DIFF_EXPECTED_KEYS, XML_DIFF_ACTUAL_KEYS, DB_ASSERT_NOTE_KEYS,
      getXmlDiffRows, getXmlDiffNote, getDiffTypeTag,
      getDbAssertRows, getAssertPassTag,
      getLogGrepSamples, getLogGrepSampleText, hasPreScriptDetailRows,
      getPreCaseRows, getPreScriptStatChips,
      observationItems, getObservationLevelTag,
      openPreScriptFullDetail,
    };`
  )
  return { api: factory(...Object.values(ctx)), openDetailCalls }
}

// ---------------- 后端真实挂载形态的样例数据 ----------------

// _observer_entry 标准条目（pre_judge_runner.py:208-227）
const obsOk = {
  extension_point: 'evaluation/result',
  observer: 'example_noop',
  status: 'ok',
  output: {},
  files: {},
  summary: '观察示例：第 0 条结果，success=True，average_score=0.9',
  error: null,
  duration_ms: 5,
}
const obsFail = {
  extension_point: 'evaluation/result',
  observer: 'example_noop',
  status: 'fail',
  output: null,
  files: {},
  summary: '',
  error: '观察脚本执行异常: boom',
  duration_ms: 3,
}
const obsUnregistered = {
  extension_point: 'evaluation/result',
  observer: 'no_such_observer',
  status: 'fail',
  output: null,
  files: {},
  summary: '',
  error: '结果观察脚本未注册: no_such_observer（可用: example_noop）',
  duration_ms: 0,
}

// log_grep 真实输出（log_grep.py:95-142）
const logGrepScript = {
  script_id: 'builtin:log_grep',
  script_name: 'log_grep',
  status: 'ok',
  output: {
    total_lines: 100,
    total_hits: 3,
    matched_line_count: 2,
    patterns: [
      {
        pattern: 'ERROR',
        type: 'keyword',
        match_count: 2,
        hit_rate: 0.02,
        sample_lines: [
          { line_no: 5, line: 'xx ERROR aa' },
          { line_no: 9, line: 'yy ERROR bb' },
        ],
        sample_truncated: false,
      },
      {
        pattern: 'timeout=\\d+',
        type: 'regex',
        match_count: 1,
        hit_rate: 0.01,
        sample_lines: [{ line_no: 12, line: 'req timeout=42' }],
        sample_truncated: false,
      },
    ],
  },
}

// xml_diff 真实差异行（xml_diff.py:138-180）
const xmlDiffScript = {
  script_id: 'builtin:xml_diff',
  script_name: 'xml_diff',
  status: 'ok',
  output: {
    identical: false,
    consistency_rate: 0.98,
    diff_count: 3,
    total_nodes: 100,
    diff_truncated: false,
    diffs: [
      { type: 'attribute_changed', path: '/config', tag: 'timeout', attribute: 'value', a: '30', b: '60' },
      { type: 'text_changed', path: '/config/retry', tag: 'retry', a: '3', b: '5' },
      { type: 'node_added', path: '/config', tag: 'cache', b: '<cache enabled="true"/>' },
    ],
  },
}

// db_assert 真实断言行（db_assert.py:208-215）
const dbAssertScript = {
  script_id: 'builtin:db_assert',
  script_name: 'db_assert',
  status: 'ok',
  output: {
    total: 2,
    passed: 1,
    failed: 1,
    consistency_rate: 0.5,
    results: [
      {
        name: 'row_count',
        sql: 'SELECT COUNT(*) FROM t',
        passed: false,
        expected: { row_count: 3 },
        actual: { row_count: 5, truncated: false, first_row: null },
        error: 'row_count 期望 3，实际 5',
      },
      {
        name: 'status_ok',
        sql: 'SELECT 1',
        passed: true,
        expected: { row_count: 1 },
        actual: { row_count: 1, truncated: false, first_row: { id: 1 } },
        error: null,
      },
    ],
  },
}

// ---------------- F1 + F5 结果观察 ----------------

describe('F1/F5 结果观察（observationItems）', () => {
  it('F1 observations 挂在 result.items[i]（后端真实挂载形态）时渲染，observer/status 映射为标题/级别', () => {
    const host = { result: { total: 1, items: [{ idx: 0, success: true, observations: [obsOk] }], failed_items: [] } }
    const { api } = buildApi(host)
    const items = api.observationItems()
    assert.equal(items.length, 1, 'items[i].observations 应被收集渲染')
    assert.equal(items[0].title, 'example_noop', 'observer 应映射为标题')
    assert.equal(items[0].level, 'ok', 'status 应映射为级别')
    assert.ok(items[0].text.includes('观察示例'), 'summary 应映射为正文')
  })

  it('F1 failed_items 中条目（on_fail 场景）的 observations 同样渲染', () => {
    const host = { result: { items: [], failed_items: [{ idx: 0, success: false, observations: [obsOk] }] } }
    const { api } = buildApi(host)
    assert.equal(api.observationItems().length, 1)
  })

  it('F1 多条数据各自携带 observations 时合并展示并在标题标注数据条目序号', () => {
    const host = { result: { items: [{ idx: 0, observations: [obsOk] }, { idx: 1, observations: [obsOk] }] } }
    const { api } = buildApi(host)
    const items = api.observationItems()
    assert.equal(items.length, 2)
    assert.ok(items[0].title.startsWith('第 1 条数据'))
    assert.ok(items[0].title.includes('example_noop'))
    assert.ok(items[1].title.startsWith('第 2 条数据'))
  })

  it('F5 fail 条目（summary 空、error 有值）不被过滤，error 映射为正文', () => {
    const host = { result: { items: [{ idx: 0, observations: [obsFail] }] } }
    const { api } = buildApi(host)
    const items = api.observationItems()
    assert.equal(items.length, 1, 'fail 条目不得被整条过滤')
    assert.equal(items[0].level, 'fail')
    assert.ok(items[0].text.includes('观察脚本执行异常: boom'))
  })

  it('F5 观察脚本未注册的 fail 条目对用户可见', () => {
    const host = { result: { items: [{ idx: 0, observations: [obsUnregistered] }] } }
    const { api } = buildApi(host)
    const items = api.observationItems()
    assert.equal(items.length, 1)
    assert.ok(items[0].text.includes('未注册'))
    assert.equal(items[0].title, 'no_such_observer')
  })

  it('F5 兼容旧结构：聚合顶层 result.observations / host.observations 仍可渲染', () => {
    const a = buildApi({ result: { items: [], observations: [{ title: 't', level: 'warning', content: 'c' }] } }).api
    const itemsA = a.observationItems()
    assert.equal(itemsA.length, 1)
    assert.equal(itemsA[0].title, 't')
    assert.equal(itemsA[0].text, 'c')

    const b = buildApi({ observations: ['纯文本观察'] }).api
    const itemsB = b.observationItems()
    assert.equal(itemsB.length, 1)
    assert.equal(itemsB[0].text, '纯文本观察')
  })

  it('F5 无观察数据时不渲染（历史任务零行为变化）', () => {
    const { api } = buildApi({ result: { items: [{ idx: 0, success: true }] } })
    assert.deepEqual(api.observationItems(), [])
  })

  it('F5 级别 tag 中文映射：ok→成功 / skip→跳过 / fail→失败', () => {
    const { api } = buildApi(null)
    assert.deepEqual(api.getObservationLevelTag('ok'), { type: 'success', label: '成功' })
    assert.deepEqual(api.getObservationLevelTag('skip'), { type: 'info', label: '跳过' })
    assert.deepEqual(api.getObservationLevelTag('fail'), { type: 'danger', label: '失败' })
    assert.deepEqual(api.getObservationLevelTag('warning'), { type: 'warning', label: '警告' })
  })
})

// ---------------- F2 log_grep ----------------

describe('F2 log_grep 命中样例', () => {
  it('命中样例从 output.patterns[i].sample_lines 平铺拾取（后端真实嵌套形态）', () => {
    const { api } = buildApi(null)
    const samples = api.getLogGrepSamples(logGrepScript)
    assert.equal(samples.length, 3, '两个模式的样例应合并为 3 行')
    const text = api.getLogGrepSampleText(logGrepScript)
    assert.ok(text.includes('L5: xx ERROR aa'), `实际样例文本：${text}`)
    assert.ok(text.includes('L12: req timeout=42'))
  })

  it('多模式样例带模式归属前缀，单模式保持原格式', () => {
    const { api } = buildApi(null)
    const text = api.getLogGrepSampleText(logGrepScript)
    assert.ok(text.includes('[ERROR] L5:'), '多模式时应标注模式名')

    const single = {
      script_id: 'builtin:log_grep',
      output: {
        patterns: [{ pattern: 'ERROR', type: 'keyword', match_count: 1, sample_lines: [{ line_no: 3, line: 'a ERROR b' }] }],
      },
    }
    const singleText = api.getLogGrepSampleText(single)
    assert.equal(singleText, 'L3: a ERROR b', '单模式不应附加前缀')
  })

  it('「查看完整明细」按钮恢复可见（hasPreScriptDetailRows）', () => {
    const { api } = buildApi(null)
    assert.equal(api.hasPreScriptDetailRows(logGrepScript), true)
  })

  it('顶层兼容键（sample_lines 等）仍生效，历史任务不受影响', () => {
    const { api } = buildApi(null)
    assert.equal(api.getLogGrepSamples({ output: { sample_lines: ['a', 'b'] } }).length, 2)
    assert.equal(api.getLogGrepSamples({ output: { matches: ['c'] } }).length, 1)
  })

  it('统计 chips 使用中文标签而非英文原键（total_hits/matched_line_count）', () => {
    const { api } = buildApi(null)
    const labels = api.getPreScriptStatChips(logGrepScript).map((c) => c.label)
    assert.ok(labels.includes('命中行次'), `chips 标签：${labels}`)
    assert.ok(labels.includes('命中行数（去重）'))
    assert.ok(labels.includes('总行数'))
    assert.ok(!labels.includes('total_hits'))
    assert.ok(!labels.includes('matched_line_count'))
  })
})

// ---------------- F3 xml_diff ----------------

describe('F3 xml_diff 差异表格', () => {
  it('预期值/实际值列拾取后端真实键 a/b', () => {
    const { api } = buildApi(null)
    const rows = api.getXmlDiffRows(xmlDiffScript)
    assert.equal(api.formatCellValue(api.pickRowField(rows[0], api.XML_DIFF_EXPECTED_KEYS)), '30')
    assert.equal(api.formatCellValue(api.pickRowField(rows[0], api.XML_DIFF_ACTUAL_KEYS)), '60')
    assert.equal(api.formatCellValue(api.pickRowField(rows[1], api.XML_DIFF_EXPECTED_KEYS)), '3')
    assert.equal(api.formatCellValue(api.pickRowField(rows[1], api.XML_DIFF_ACTUAL_KEYS)), '5')
  })

  it('说明列在行内无说明字段时按差异类型推导中文说明（含属性名）', () => {
    const { api } = buildApi(null)
    const rows = api.getXmlDiffRows(xmlDiffScript)
    assert.ok(api.getXmlDiffNote(rows[0]).includes('属性'), `实际：${api.getXmlDiffNote(rows[0])}`)
    assert.ok(api.getXmlDiffNote(rows[0]).includes('value'))
    assert.equal(api.getXmlDiffNote(rows[1]), '文本内容变化')
    assert.equal(api.getXmlDiffNote(rows[2]), '新增节点')
  })

  it('「查看完整明细」弹窗（复用同一批 pick 函数）包含 a/b 差异内容与中文说明', () => {
    const { api, openDetailCalls } = buildApi(null)
    api.openPreScriptFullDetail(xmlDiffScript)
    assert.equal(openDetailCalls.length, 1)
    const content = openDetailCalls[0].content
    assert.ok(content.includes('预期: 30'), `弹窗内容：\n${content}`)
    assert.ok(content.includes('实际: 60'))
    assert.ok(content.includes('预期: 3'))
    assert.ok(content.includes('实际: 5'))
    assert.ok(content.includes('属性值变化 value'))
  })
})

// ---------------- F4 db_assert ----------------

describe('F4 db_assert 失败断言原因', () => {
  it('失败断言的 error 映射为「说明」列', () => {
    const { api } = buildApi(null)
    const rows = api.getDbAssertRows(dbAssertScript)
    assert.equal(
      api.formatCellValue(api.pickRowField(rows[0], api.DB_ASSERT_NOTE_KEYS)),
      'row_count 期望 3，实际 5'
    )
  })

  it('通过断言说明为空（error=null 不误显），断言结果 tag 正确', () => {
    const { api } = buildApi(null)
    const rows = api.getDbAssertRows(dbAssertScript)
    assert.equal(api.formatCellValue(api.pickRowField(rows[1], api.DB_ASSERT_NOTE_KEYS)), '')
    assert.equal(api.getAssertPassTag(rows[0]).label, '失败')
    assert.equal(api.getAssertPassTag(rows[1]).label, '通过')
    assert.equal(api.hasPreScriptDetailRows(dbAssertScript), true)
  })

  it('统计 chips 使用中文标签（passed/failed → 通过断言/失败断言）', () => {
    const { api } = buildApi(null)
    const labels = api.getPreScriptStatChips(dbAssertScript).map((c) => c.label)
    assert.ok(labels.includes('通过断言'), `chips 标签：${labels}`)
    assert.ok(labels.includes('失败断言'))
    assert.ok(labels.includes('总数'))
    assert.ok(!labels.includes('passed'))
    assert.ok(!labels.includes('failed'))
  })
})

// ---------------- F6 统计 chips key ----------------

describe('F6 统计 chips 去重与 key 唯一', () => {
  it('多别名映射同一标签时只保留首个 chip（matched_count/hits 同现不产生重复）', () => {
    const { api } = buildApi(null)
    const chips = api.getPreScriptStatChips({ output: { matched_count: 3, hits: 3 } })
    assert.equal(chips.length, 1)
    assert.equal(chips[0].label, '命中行数')
  })

  it('任意 output 下 chip 的 key 与 label 均唯一（:key 防重复）', () => {
    const { api } = buildApi(null)
    const script = { output: { total: 5, total_lines: 10, passed: 1, failed: 2, identical: true, matched_count: 7, hits: 7 } }
    const chips = api.getPreScriptStatChips(script)
    assert.equal(new Set(chips.map((c) => c.key)).size, chips.length, 'chip.key 必须唯一')
    assert.equal(new Set(chips.map((c) => c.label)).size, chips.length, 'chip.label 必须唯一')
    const identicalChip = chips.find((c) => c.key === 'identical')
    assert.equal(identicalChip.value, '是', '布尔输出值应转译为 是/否')
  })
})

// ---------------- 回归：一期 allure_vs_cases 通用渲染 ----------------

describe('回归：allure_vs_cases 与历史任务兼容', () => {
  it('per_case 表格、明细按钮与一致率 chip 不受本次修复影响', () => {
    const { api } = buildApi(null)
    const script = {
      script_id: 'builtin:allure_vs_cases',
      script_name: 'allure_vs_cases',
      status: 'ok',
      summary: '86/99 用例结果与预期一致',
      output: {
        consistency_rate: 0.87,
        mismatched_count: 13,
        per_case: [{ case: 'c1', expected: '通过', actual_status: 'failed', match_state: 'mismatch', evidence: 'e' }],
      },
    }
    assert.equal(api.getPreCaseRows(script).length, 1)
    assert.equal(api.hasPreScriptDetailRows(script), true)
    const chips = api.getPreScriptStatChips(script)
    assert.ok(chips.some((c) => c.label === '一致率' && c.value === '87.0%'))
    assert.equal(new Set(chips.map((c) => c.key)).size, chips.length)
  })

  it('类别识别不受影响（xml_diff/log_grep/db_assert）', () => {
    const { api } = buildApi(null)
    assert.equal(api.getPreScriptKind(xmlDiffScript), 'xml_diff')
    assert.equal(api.getPreScriptKind(logGrepScript), 'log_grep')
    assert.equal(api.getPreScriptKind(dbAssertScript), 'db_assert')
    assert.equal(api.getPreScriptKind({ script_id: 'builtin:allure_vs_cases' }), '')
  })

  it('历史任务无 observations / 无 pre_script_results 时两区均不渲染', () => {
    const { api } = buildApi({ result: { items: [{ idx: 0, success: true, average_score: 0.9 }] } })
    assert.deepEqual(api.observationItems(), [])
    assert.equal(api.preScriptEntries().length, 0)
  })
})
