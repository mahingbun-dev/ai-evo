// 北京时间工具函数 (UTC+8)

const BEIJING_TIMEZONE = 'Asia/Shanghai'

/**
 * 格式化时间为北京时间
 * @param {string|Date} time - ISO时间字符串或Date对象
 * @param {object} options - toLocaleString选项
 * @returns {string}
 */
export function formatTime(time, options = {}) {
  if (!time) return null
  const date = new Date(time)
  if (isNaN(date.getTime())) return null

  const defaultOptions = {
    timeZone: BEIJING_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  }

  return date.toLocaleString('zh-CN', { ...defaultOptions, ...options })
}

