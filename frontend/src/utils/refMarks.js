/**
 * @ 引用标记工具（共享设计契约 v3 第「一」「七」节）
 *
 * 标记语法（与后端 app/services/reference_mark_resolver.py 的
 * REFERENCE_MARK_PATTERN 完全同源）：
 *   {{ref:target:<target_id>|<显示名>}}   引用某个待评测数据（evaluation_targets 行）
 *   {{ref:var:<变量key>|<变量名>}}         引用某个裁判变量（judge_variable_config 解析结果）
 *
 * 供 RefTextarea 组件与单元测试（frontend/tests/unit/refMarks.test.mjs）复用：
 * - detectCaretAtMark：光标前最近「孤立 @」检测（触发浮动下拉的依据）
 * - insertMark：把「@ + 查询段」替换为标记并补一个空格，返回新光标位置
 * - extractReferenceMarks：提取文本中的全部标记（kind/key/name/raw）
 * - buildReferenceMark：按 kind + key + 显示名拼装标记原文
 */

// 标记正则源串：{{ref:(target|var):<key>[|<名称>]}}，名称段可整体省略（允许空名）。
// 注意与后端保持逐字符一致（后端为 Python raw string，此处为 JS 正则源串，二者字面相同）
export const REFERENCE_MARK_SOURCE = '\\{\\{ref:(target|var):([^|}]+)(?:\\|([^}]*))?\\}\\}'

/** 单字符空白判断（与后端解析口径一致，\s 含空格/换行/制表等） */
function isWhitespaceChar(ch) {
  return /\s/.test(ch)
}

/**
 * 提取文本中的全部引用标记（与后端 extract_reference_marks 口径一致）
 *
 * @param {string} text 待提取文本
 * @returns {Array<{kind: 'target'|'var', key: string, name: string, raw: string}>}
 *          按出现顺序排列；非字符串输入返回空数组
 */
export function extractReferenceMarks(text) {
  if (typeof text !== 'string' || !text) return []
  const marks = []
  const re = new RegExp(REFERENCE_MARK_SOURCE, 'g')
  let m
  while ((m = re.exec(text)) !== null) {
    marks.push({
      kind: m[1],        // 'target' | 'var'
      key: m[2],         // target_id 或变量 key
      name: (m[3] || '').trim(), // 名称段省略时为空串；两侧空白对齐后端 strip
      raw: m[0],         // 标记原文（含花括号）
    })
  }
  return marks
}

/**
 * 拼装引用标记原文
 *
 * @param {'target'|'var'} kind 标记种类
 * @param {string} key target_id 或变量 key
 * @param {string} [name] 显示名（空/全空白时省略「|名称」段）
 * @returns {string} 形如 {{ref:target:t1|标准答案}} 的标记原文；kind/key 缺失返回空串
 */
export function buildReferenceMark(kind, key, name) {
  if (!kind || key === undefined || key === null) return ''
  const k = String(key)
  const n = (name || '').trim()
  return `{{ref:${kind}:${k}${n ? `|${n}` : ''}}}`
}

/**
 * 检测光标前最近的「孤立 @」触发点
 *
 * 「孤立」定义：@ 的前一个字符不是 ASCII 字母/数字、也不是另一个 @/＠——
 * 保留邮箱（a@b.com）与 @@ 连打的防误触；中文等 CJK 字符后允许直接触发
 * （中文输入习惯不会先补空格）。@ 到光标之间不得出现空白（查询段不允许
 * 跨空白，空格即关闭下拉）。
 * 触发符兼容半角 @（U+0040）与全角 ＠（U+FF20，中文输入法状态下的 Shift+2），
 * 二者均为单码元，命中后的查询段/替换算术一致。
 *
 * @param {string} text 当前输入全文
 * @param {number|null} [caretIndex] 光标位置（缺省/非法时按文末处理）
 * @returns {{atIndex: number, query: string}|null}
 *          命中返回 @ 下标与查询段文本；未命中返回 null
 */
const MARK_TRIGGER_CHARS = ['@', '＠']
// @ 前紧邻这些字符时不触发：ASCII 字母数字（邮箱 a@b）与触发符自身（@@ 连打）
const MARK_BLOCKING_PREV = /[A-Za-z0-9@＠]/

export function detectCaretAtMark(text, caretIndex) {
  if (typeof text !== 'string' || text.length === 0) return null
  const caret = Math.min(
    Math.max(Number.isInteger(caretIndex) ? caretIndex : text.length, 0),
    text.length
  )
  if (caret <= 0) return null
  // 光标往前找最近一段连续非空白（查询段的边界）
  let runStart = caret
  while (runStart > 0 && !isWhitespaceChar(text[runStart - 1])) runStart--
  // 半角 @ 与全角 ＠ 取光标前最近的一个
  let at = -1
  for (const ch of MARK_TRIGGER_CHARS) {
    at = Math.max(at, text.lastIndexOf(ch, caret - 1))
  }
  if (at < runStart) return null // 当前非空白段内没有 @
  if (at > 0 && MARK_BLOCKING_PREV.test(text[at - 1])) return null // 非孤立 @（邮箱中间 @、@@ 连打）
  return { atIndex: at, query: text.slice(at + 1, caret) }
}

/**
 * 在光标处插入引用标记：用「标记 + 一个空格」替换「@ + 查询段」
 *
 * @param {string} text 插入前全文
 * @param {{atIndex: number, query: string}|null} detection detectCaretAtMark 的检测结果
 * @param {string} mark 标记原文（{{ref:...}}）
 * @returns {{text: string, caretIndex: number}} 插入后的全文与新光标位置（落在补出的空格之后）
 */
export function insertMark(text, detection, mark) {
  if (
    typeof text !== 'string' ||
    !detection ||
    typeof detection.atIndex !== 'number' ||
    typeof mark !== 'string' ||
    !mark
  ) {
    // 非法入参原样返回，不抛错（组件侧触发条件失效时只是关闭下拉）
    return { text: typeof text === 'string' ? text : '', caretIndex: typeof text === 'string' ? text.length : 0 }
  }
  const query = detection.query || ''
  const caretIndex = detection.atIndex + 1 + query.length
  const newText = text.slice(0, detection.atIndex) + mark + ' ' + text.slice(caretIndex)
  return { text: newText, caretIndex: detection.atIndex + mark.length + 1 }
}
