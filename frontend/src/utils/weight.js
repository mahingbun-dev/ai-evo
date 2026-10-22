// 评测维度权重工具
// 权重契约：预设标签 high/medium/low（后端 WEIGHT_MAP 映射 3/2/1），或任意数值（如 0.05，hub 建任务常用形态）

export const WEIGHT_PRESETS = ['high', 'medium', 'low']

// 非预设权重（数值或中文标签等）需要作为动态选项回显，否则 el-select 只显示原始值且下拉无选中态
export function isCustomWeight(weight) {
  return weight !== undefined && weight !== null && weight !== '' && !WEIGHT_PRESETS.includes(weight)
}

// allow-create 输入产生字符串，数字形态必须转为 Number：
// 后端四套加权计算对字符串走 WEIGHT_MAP 标签映射（get(val, 1)），"0.05" 会被误读成 1
export function normalizeMetricWeight(weight) {
  if (typeof weight === 'string' && weight.trim() !== '' && Number.isFinite(Number(weight))) {
    return Number(weight)
  }
  return weight
}
