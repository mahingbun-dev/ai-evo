/**
 * @ 引用标记机制单元测试（共享设计契约 v3 第「一」「七」节）
 * 文件: frontend/tests/unit/refMarks.test.mjs
 * 运行: node --test frontend/tests/unit/refMarks.test.mjs
 *
 * 覆盖：
 * - detectCaretAtMark：光标前最近「孤立 @」触发检测（行首/空白后触发、
 *   邮箱式中间 @ 与 @@ 连打不触发、查询段跨空白即失效）
 * - insertMark：用「标记 + 空格」替换「@ + 查询段」，返回新光标位置
 * - extractReferenceMarks / buildReferenceMark：标记提取与拼装，
 *   口径与后端 app/services/reference_mark_resolver.py 逐字符一致
 * - RefTextarea.vue / api/index.js 契约锚点（源码断言，沿用本目录既有风格）
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  REFERENCE_MARK_SOURCE,
  detectCaretAtMark,
  insertMark,
  extractReferenceMarks,
  buildReferenceMark,
} from '../../src/utils/refMarks.js'

const here = dirname(fileURLToPath(import.meta.url))
const componentSrc = readFileSync(join(here, '../../src/components/RefTextarea.vue'), 'utf-8')
const apiSrc = readFileSync(join(here, '../../src/api/index.js'), 'utf-8')
// 后端解析器源码（工作区根在 frontend 的上两级；独立部署前端目录时跳过该锚点）
const resolverPath = join(here, '../../../app/services/reference_mark_resolver.py')
const resolverSrc = existsSync(resolverPath) ? readFileSync(resolverPath, 'utf-8') : null

// ---------------- detectCaretAtMark ----------------

describe('detectCaretAtMark：孤立 @ 触发检测', () => {
  it('行首 @ 且光标紧跟 → 触发，查询段为空串', () => {
    assert.deepEqual(detectCaretAtMark('@', 1), { atIndex: 0, query: '' })
    assert.deepEqual(detectCaretAtMark('@用户', 1), { atIndex: 0, query: '' })
  })

  it('空白后的 @ 触发，@ 与光标之间为查询段', () => {
    // '评分参考 @回答'：@ 在下标 5（'评分参考'4 字 + 空格）
    assert.deepEqual(detectCaretAtMark('评分参考 @回答', 8), { atIndex: 5, query: '回答' })
    assert.deepEqual(detectCaretAtMark('a @b', 4), { atIndex: 2, query: 'b' })
  })

  it('换行/制表等空白同样视为孤立边界', () => {
    assert.deepEqual(detectCaretAtMark('第一行\n@答案', 9), { atIndex: 4, query: '答案' })
    assert.deepEqual(detectCaretAtMark('\t@x', 3), { atIndex: 1, query: 'x' })
  })

  it('邮箱式中间 @（前一个字符是 ASCII 字母/数字）不触发', () => {
    assert.equal(detectCaretAtMark('a@b', 3), null)
    assert.equal(detectCaretAtMark('user@example.com', 17), null)
  })

  it('中文等 CJK 字符后直接打 @ 也触发（中文输入习惯无前置空格）', () => {
    // '中文@答案'：@ 在下标 2，前字符是 CJK「文」
    assert.deepEqual(detectCaretAtMark('中文@答案', 5), { atIndex: 2, query: '答案' })
    assert.deepEqual(detectCaretAtMark('请对照＠', 4), { atIndex: 3, query: '' })
    // 标点后同样触发
    assert.deepEqual(detectCaretAtMark('句子。＠关键', 7), { atIndex: 3, query: '关键' })
  })

  it('@@ 连打不触发（第二个 @ 的前一个字符是 @，非孤立）', () => {
    assert.equal(detectCaretAtMark('@@', 2), null)
    assert.equal(detectCaretAtMark(' @@x', 4), null)
  })

  it('查询段跨空白后失效（光标前的最近非空白段内没有 @）', () => {
    assert.equal(detectCaretAtMark('@ab cd', 6), null)
    assert.equal(detectCaretAtMark('@ab\ncd', 6), null)
  })

  it('查询段内的空白之前仍触发（光标停在 @ 查询段中间）', () => {
    // 光标在 '@ab ' 的空格处（caret=3）：最近非空白段是 '@ab'，仍触发
    assert.deepEqual(detectCaretAtMark('@ab ', 3), { atIndex: 0, query: 'ab' })
  })

  it('光标越界时按文末处理；caretIndex 缺省等同文末', () => {
    assert.deepEqual(detectCaretAtMark('x @y', 999), { atIndex: 2, query: 'y' })
    assert.deepEqual(detectCaretAtMark('x @y', null), { atIndex: 2, query: 'y' })
    assert.deepEqual(detectCaretAtMark('x @y', undefined), { atIndex: 2, query: 'y' })
  })

  it('空文本 / 光标在开头 / 全文无 @ 均不触发', () => {
    assert.equal(detectCaretAtMark('', 0), null)
    assert.equal(detectCaretAtMark('', null), null)
    assert.equal(detectCaretAtMark('@x', 0), null)
    assert.equal(detectCaretAtMark('abc', 3), null)
    assert.equal(detectCaretAtMark(null, 1), null)
    assert.equal(detectCaretAtMark(undefined, 0), null)
  })
})

// ---------------- 全角 ＠（中文输入法） ----------------

describe('全角 ＠（中文输入法）兼容触发', () => {
  it('行首/空白后的全角 ＠ 触发，查询段口径与半角一致', () => {
    assert.deepEqual(detectCaretAtMark('＠', 1), { atIndex: 0, query: '' })
    // '评分参考 ＠回答'：＠ 在下标 5（'评分参考'4 字 + 空格）
    assert.deepEqual(detectCaretAtMark('评分参考 ＠回答', 8), { atIndex: 5, query: '回答' })
  })

  it('全角 ＠ 的孤立性规则与半角一致（ASCII 字母数字前不触发，CJK 前触发）', () => {
    assert.equal(detectCaretAtMark('a＠b', 4), null)
    // 中文后直接全角 ＠ 触发，后文进入查询段
    assert.deepEqual(detectCaretAtMark('中文＠中文', 5), { atIndex: 2, query: '中文' })
  })

  it('半角 @ 与全角 ＠ 并存时取离光标最近者', () => {
    const text = 'a @b ＠c' // @ 在 2，＠ 在 5
    assert.deepEqual(detectCaretAtMark(text, text.length), { atIndex: 5, query: 'c' })
    assert.deepEqual(detectCaretAtMark(text, 4), { atIndex: 2, query: 'b' })
  })

  it('insertMark 按实际命中下标整体替换「＠ + 查询段」', () => {
    const text = '请看 ＠'
    const d = detectCaretAtMark(text, text.length)
    assert.deepEqual(d, { atIndex: 3, query: '' })
    const mark = '{{ref:target:t1|名}}'
    const r = insertMark(text, d, mark)
    assert.equal(r.text, `请看 ${mark} `)
  })
})

// ---------------- insertMark ----------------

describe('insertMark：光标处插入标记并补空格', () => {
  const mark = '{{ref:target:t1|标准答案}}'

  it('替换「@ + 查询段」为「标记 + 空格」，光标落在空格之后', () => {
    const text = '评分参考 @答案'
    const d = detectCaretAtMark(text, text.length)
    assert.deepEqual(d, { atIndex: 5, query: '答案' })
    const r = insertMark(text, d, mark)
    assert.equal(r.text, `评分参考 ${mark} `)
    assert.equal(r.caretIndex, `评分参考 ${mark} `.length)
  })

  it('空查询段（刚敲下 @）同样替换整个 @', () => {
    const r = insertMark('参考 @', { atIndex: 3, query: '' }, mark)
    assert.equal(r.text, `参考 ${mark} `)
    assert.equal(r.caretIndex, 3 + mark.length + 1)
  })

  it('文本中段插入不破坏后文', () => {
    const text = '请参考 @ans 后续内容'
    const d = detectCaretAtMark(text, 8)
    assert.deepEqual(d, { atIndex: 4, query: 'ans' })
    const r = insertMark(text, d, '{{ref:var:k1|变量A}}')
    assert.equal(r.text, '请参考 {{ref:var:k1|变量A}}  后续内容')
  })

  it('detection 为 null 或入参非法时原样返回', () => {
    assert.deepEqual(insertMark('abc', null, mark), { text: 'abc', caretIndex: 3 })
    assert.deepEqual(insertMark('abc', {}, mark), { text: 'abc', caretIndex: 3 })
    assert.deepEqual(insertMark('abc', { atIndex: 0, query: '' }, ''), { text: 'abc', caretIndex: 3 })
    assert.deepEqual(insertMark(null, { atIndex: 0, query: '' }, mark), { text: '', caretIndex: 0 })
  })

  it('与 detectCaretAtMark 联动：插入后光标处再检测为 null（下拉即关闭）', () => {
    const text = '对比 @标准'
    const r = insertMark(text, detectCaretAtMark(text, text.length), mark)
    assert.equal(detectCaretAtMark(r.text, r.caretIndex), null)
    // 光标回退到标记内部时也不误触发（标记内的字符流里没有孤立 @）
    assert.equal(detectCaretAtMark(r.text, r.caretIndex - 1), null)
  })
})

// ---------------- extractReferenceMarks / buildReferenceMark ----------------

describe('extractReferenceMarks：标记提取（与后端口径一致）', () => {
  it('提取 target / var 双标记，字段齐、按出现顺序排列', () => {
    const marks = extractReferenceMarks('对比 {{ref:target:t1|标准答案}} 与 {{ref:var:v1|会话轮次}}')
    assert.equal(marks.length, 2)
    assert.deepEqual(marks[0], { kind: 'target', key: 't1', name: '标准答案', raw: '{{ref:target:t1|标准答案}}' })
    assert.deepEqual(marks[1], { kind: 'var', key: 'v1', name: '会话轮次', raw: '{{ref:var:v1|会话轮次}}' })
  })

  it('名称段可省略（省略后 name 为空串）', () => {
    const marks = extractReferenceMarks('{{ref:target:abc}}')
    assert.deepEqual(marks, [{ kind: 'target', key: 'abc', name: '', raw: '{{ref:target:abc}}' }])
    // 竖线后留空同样是空名
    assert.deepEqual(extractReferenceMarks('{{ref:var:kv|}}'), [{ kind: 'var', key: 'kv', name: '', raw: '{{ref:var:kv|}}' }])
  })

  it('名称两侧空白被 trim（对齐后端 (name or "").strip()）', () => {
    const marks = extractReferenceMarks('{{ref:var:k| 变量 A }}')
    assert.equal(marks[0].name, '变量 A')
  })

  it('非字符串 / 无标记输入返回空数组', () => {
    assert.deepEqual(extractReferenceMarks(''), [])
    assert.deepEqual(extractReferenceMarks(null), [])
    assert.deepEqual(extractReferenceMarks(undefined), [])
    assert.deepEqual(extractReferenceMarks(123), [])
    assert.deepEqual(extractReferenceMarks('普通文本，没有标记'), [])
  })

  it('key 段不允许 | 与 }（不完整的标记不提取；名称段允许再含 |）', () => {
    // 名称捕获为 [^}]*，可再含 |（与后端正则一致）
    assert.deepEqual(extractReferenceMarks('{{ref:target:a|b|c}}'), [
      { kind: 'target', key: 'a', name: 'b|c', raw: '{{ref:target:a|b|c}}' },
    ])
    assert.deepEqual(extractReferenceMarks('{{ref:var:'), [])
    assert.deepEqual(extractReferenceMarks('{{ref:target:a b}}'), [
      // key 捕获 [^|}]+ 允许空格（与后端一致，不做额外收紧）
      { kind: 'target', key: 'a b', name: '', raw: '{{ref:target:a b}}' },
    ])
  })
})

describe('buildReferenceMark：标记拼装', () => {
  it('标准形态：kind + key + 显示名', () => {
    assert.equal(buildReferenceMark('target', 't1', '标准答案'), '{{ref:target:t1|标准答案}}')
    assert.equal(buildReferenceMark('var', 'v1', '会话轮次'), '{{ref:var:v1|会话轮次}}')
  })

  it('名称为空/全空白时省略「|名称」段', () => {
    assert.equal(buildReferenceMark('target', 't1', ''), '{{ref:target:t1}}')
    assert.equal(buildReferenceMark('var', 'v1', '  '), '{{ref:var:v1}}')
    assert.equal(buildReferenceMark('var', 'v1', undefined), '{{ref:var:v1}}')
  })

  it('kind/key 缺失返回空串（不产出非法标记）', () => {
    assert.equal(buildReferenceMark('', 't1', '名'), '')
    assert.equal(buildReferenceMark('target', undefined, '名'), '')
    assert.equal(buildReferenceMark('target', null, '名'), '')
  })

  it('与 extractReferenceMarks 闭环：build 产物可被原样提取回读', () => {
    const mark = buildReferenceMark('target', 'target_mig_1_ab12cd34', '脚本摘要')
    const marks = extractReferenceMarks(`前缀 ${mark} 后缀`)
    assert.equal(marks.length, 1)
    assert.equal(marks[0].raw, mark)
    assert.equal(marks[0].kind, 'target')
    assert.equal(marks[0].key, 'target_mig_1_ab12cd34')
    assert.equal(marks[0].name, '脚本摘要')
  })
})

// ---------------- 前后端正则一致性 ----------------

describe('前后端标记口径一致性', () => {
  it('REFERENCE_MARK_SOURCE 与后端 REFERENCE_MARK_PATTERN 逐字符一致', () => {
    if (!resolverSrc) return // 前端目录独立部署时跳过（无后端源码可比对）
    const m = resolverSrc.match(/REFERENCE_MARK_PATTERN = re\.compile\(r"([^"]+)"\)/)
    assert.ok(m, '后端 REFERENCE_MARK_PATTERN 定义形态变化，请同步修正本断言')
    assert.equal(REFERENCE_MARK_SOURCE, m[1], '前后端标记正则必须一致，否则前端插入的标记后端解析不到')
  })
})

// ---------------- RefTextarea.vue 契约锚点 ----------------

describe('RefTextarea.vue 契约锚点（契约「七」props/emit/交互）', () => {
  it('复用 utils/refMarks 的检测/插入/拼装工具（不在组件内复制实现）', () => {
    assert.ok(
      componentSrc.includes("from '../utils/refMarks.js'"),
      '组件必须从 utils/refMarks.js 导入工具函数'
    )
    for (const fn of ['detectCaretAtMark', 'insertMark', 'buildReferenceMark']) {
      assert.ok(new RegExp(`\\b${fn}\\b`).test(componentSrc), `组件须使用 ${fn}`)
    }
  })

  it('props 契约字段齐全：modelValue/targets/vars/placeholder/rows/disabled/isSingleLine', () => {
    const propsBlock = componentSrc.slice(
      componentSrc.indexOf('defineProps'),
      componentSrc.indexOf('defineEmits')
    )
    for (const name of ['modelValue', 'targets', 'vars', 'placeholder', 'rows', 'disabled', 'isSingleLine']) {
      assert.ok(new RegExp(`\\b${name}\\s*:`).test(propsBlock), `props 缺少 ${name}`)
    }
  })

  it('emit update:modelValue（v-model 契约）', () => {
    assert.ok(componentSrc.includes("defineEmits(['update:modelValue'])"))
    assert.ok(componentSrc.includes("emit('update:modelValue'"))
  })

  it('多行 textarea 与单行 input 双形态；下拉分组为「待评测数据/裁判变量」', () => {
    assert.ok(/<textarea[\s\S]*ref="fieldRef"/.test(componentSrc), '须有 textarea 多行形态')
    assert.ok(/<input[\s\S]*ref="fieldRef"/.test(componentSrc), '须有 input 单行形态')
    assert.ok(componentSrc.includes('>待评测数据</div>'))
    assert.ok(componentSrc.includes('>裁判变量</div>'))
    // 选项点击不抢焦点（mousedown.prevent），避免 blur 关闭吃掉点击
    assert.ok(componentSrc.includes('@mousedown.prevent'))
  })

  it('插入后补一个空格（由 insertMark 保证）并恢复光标', () => {
    assert.ok(componentSrc.includes('setSelectionRange(caretIndex, caretIndex)'))
  })

  it('下拉 Teleport 到 body + fixed 定位（防 el-collapse / el-dialog 祖先裁剪）', () => {
    assert.ok(componentSrc.includes('<Teleport to="body">'), '下拉须 Teleport 到 body')
    assert.ok(componentSrc.includes("position: 'fixed'"), '定位须为 fixed（内联样式按输入框矩形计算）')
  })
})

// ---------------- api/index.js 契约锚点 ----------------

/** 从 from 下标起做引号感知的花括号配对，返回完整对象字面量源码（沿用本目录既有手法） */
function extractBalanced(src, from) {
  let depth = 0
  let quote = null
  for (let i = from; i < src.length; i++) {
    const ch = src[i]
    if (quote) {
      if (ch === '\\') { i++; continue }
      if (ch === quote) quote = null
      continue
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; continue }
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) return src.slice(from, i + 1)
    }
  }
  throw new Error('花括号未闭合：源码提取失败')
}

/** 提取 api/index.js 的对象字面量并用受控 request 求值 */
function buildApi(exportName, requestStub) {
  const mark = `export const ${exportName} = `
  const idx = apiSrc.indexOf(mark)
  if (idx < 0) throw new Error(`未在 api/index.js 中定位到 ${exportName}`)
  const literal = extractBalanced(apiSrc, idx + mark.length)
  return new Function('request', `return (${literal})`)(requestStub)
}

/** axios 实例形态的 request 桩：记录全部调用 */
function recordingRequest(captured) {
  const make = (method) => (url, data, config) => {
    captured.push({ method, url, data, config })
    return Promise.resolve({ data: {} })
  }
  return { get: make('get'), post: make('post'), put: make('put'), delete: make('delete') }
}

describe('api/index.js 迁移端点契约（契约「六」）', () => {
  it('taskApi.migrateConfig(id) POST /evaluation/tasks/{id}/migrate-config', () => {
    const captured = []
    const api = buildApi('taskApi', recordingRequest(captured))
    assert.equal(typeof api.migrateConfig, 'function', 'taskApi 缺少 migrateConfig')
    api.migrateConfig('task-1')
    assert.equal(captured.length, 1)
    assert.equal(captured[0].method, 'post')
    assert.equal(captured[0].url, '/evaluation/tasks/task-1/migrate-config')
    assert.equal(captured[0].data, undefined, '迁移端点无请求体')
  })

  it('taskApi.migrateAllLegacy() POST /evaluation/tasks/migrate-legacy-all', () => {
    const captured = []
    const api = buildApi('taskApi', recordingRequest(captured))
    assert.equal(typeof api.migrateAllLegacy, 'function', 'taskApi 缺少 migrateAllLegacy')
    api.migrateAllLegacy()
    assert.equal(captured[0].method, 'post')
    assert.equal(captured[0].url, '/evaluation/tasks/migrate-legacy-all')
  })

  it('instructionApi.migrateAllLegacy() POST /instruction/configs/migrate-legacy-all', () => {
    const captured = []
    const api = buildApi('instructionApi', recordingRequest(captured))
    assert.equal(typeof api.migrateAllLegacy, 'function', 'instructionApi 缺少 migrateAllLegacy')
    api.migrateAllLegacy()
    assert.equal(captured[0].method, 'post')
    assert.equal(captured[0].url, '/instruction/configs/migrate-legacy-all')
  })

  it('迁移端点与后端路由对齐（前缀 + 路径逐字一致）', () => {
    const taskApiPy = join(here, '../../../app/api/evaluation_task_api.py')
    const instructionApiPy = join(here, '../../../app/api/instruction_config_api.py')
    if (!existsSync(taskApiPy) || !existsSync(instructionApiPy)) return
    const taskRouter = readFileSync(taskApiPy, 'utf-8')
    const instructionRouter = readFileSync(instructionApiPy, 'utf-8')
    assert.ok(taskRouter.includes('APIRouter(prefix="/evaluation/tasks"'))
    assert.ok(taskRouter.includes('@router.post("/migrate-legacy-all"'))
    assert.ok(taskRouter.includes('@router.post("/{task_id}/migrate-config"'))
    assert.ok(instructionRouter.includes('APIRouter(prefix="/instruction"'))
    assert.ok(instructionRouter.includes('@router.post("/configs/migrate-legacy-all"'))
  })
})
