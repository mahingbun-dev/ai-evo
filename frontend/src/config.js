/**
 * 前端配置模块
 * 使用 Vite 环境变量配置
 * 开发环境: .env 文件
 * 生产环境: 通过 Vite 构建时的环境变量注入
 */

// 获取 API 基础地址
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || ''

// 获取 WebSocket 地址
// 如果未配置，则使用当前页面的协议和主机
const wsUrl = import.meta.env.VITE_WS_URL

// 构建完整的 WebSocket URL
function getWebSocketUrl(path) {
  let url
  if (wsUrl) {
    // 使用配置的 WebSocket 地址
    url = `${wsUrl}${path}`
  } else {
    // 使用当前页面的协议和主机（兼容开发和生产环境）
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    url = `${protocol}//${host}${path}`
  }
  // WS 握手阶段无法自定义 header，登录 token 以 query 参数携带，端点内校验
  const token = localStorage.getItem('ai_evo_token') || ''
  if (token) {
    url += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token)
  }
  return url
}

export default {
  // API 基础地址
  apiBaseUrl,
  // WebSocket 地址
  wsUrl,
  // 获取 WebSocket URL 的方法
  getWebSocketUrl,
  // 开发环境标识
  isDev: import.meta.env.DEV,
  // 生产环境标识
  isProd: import.meta.env.PROD
}
