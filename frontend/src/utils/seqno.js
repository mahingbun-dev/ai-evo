/**
 * 详细结果序号口径工具
 *
 * 背景（缺陷）：judge/agent 默认执行路径曾有 payload 缺 idx 的回归
 * （执行器以 payload.get("idx", 0) 取行号而 VariableInjector 从不写入 idx），
 * 历史脏数据全部行 idx=0 入库；结果页若仍按 idx+1 展示序号则全列显示 1。
 *
 * 口径：数据自带 idx 在全部行上存在（整数）且互不重复才作为展示序号
 * （对齐数据集行位与图表 x 轴）；否则整体回退为合并后位置序号，
 * 不与 idx 序号混排同一列。
 */

/**
 * 判断数据行列表能否用数据自带 idx 作为展示序号
 * @param {Array<{idx?: number}>} items 合并后的明细行（含失败行）
 * @returns {boolean} true = 所有行都有唯一整数 idx
 */
export const canUseIdxAsSeqNo = (items) => {
  if (!Array.isArray(items) || items.length === 0) return false
  const seen = new Set()
  for (const item of items) {
    const idx = item?.idx
    if (typeof idx !== 'number' || !Number.isInteger(idx) || seen.has(idx)) {
      return false
    }
    seen.add(idx)
  }
  return true
}

/**
 * 行展示序号：idx 可信时用 idx+1，否则回退为位置序号（1 起密集编号）
 * @param {{idx?: number}} item 数据行
 * @param {number} index 行在列表中的下标（0 起）
 * @param {boolean} usable canUseIdxAsSeqNo 的判定结果
 * @returns {number}
 */
export const detailSeqNo = (item, index, usable) =>
  usable && item && typeof item.idx === 'number' ? item.idx + 1 : index + 1
