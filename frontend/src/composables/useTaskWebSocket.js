/**
 * useTaskWebSocket - 任务状态 WebSocket 实时推送 Hook
 *
 * 处理 WebSocket 连接、心跳、自动重连（3 次失败后降级）和消息处理。
 */

import { ref, onUnmounted } from 'vue'

// 心跳间隔 30 秒
const HEARTBEAT_INTERVAL = 30000
// 最大重试次数
const MAX_RETRIES = 3
// 重试延迟（指数退避）
const RETRY_DELAYS = [1000, 2000, 4000]

export function useTaskWebSocket(options = {}) {
  const {
    onStatusUpdate = () => {},
    onError = () => {},
    onConnect = () => {},
    onDisconnect = () => {},
    onFallback = () => {}
  } = options

  // WebSocket 实例映射
  const wsMap = ref({})
  // 重试计数
  const retryCount = ref({})
  // 重连定时器
  const reconnectTimers = ref({})
  // 心跳定时器
  const heartbeatTimers = ref({})
  // 连接状态
  const isConnected = ref({})

  /**
   * 建立 WebSocket 连接
   */
  function connect(taskId) {
    if (wsMap.value[taskId]) {
      // 已存在连接，先断开
      disconnect(taskId)
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    // 登录 token 以 query 参数携带（WS 握手无法自定义 header）
    const token = localStorage.getItem('ai_evo_token') || ''
    const tokenQuery = token ? `?token=${encodeURIComponent(token)}` : ''
    const wsUrl = `${protocol}//${window.location.host}/ws/task/${taskId}${tokenQuery}`

    try {
      const ws = new WebSocket(wsUrl)

      ws.onopen = () => {
        console.log(`WebSocket connected: ${taskId}`)
        isConnected.value[taskId] = true
        retryCount.value[taskId] = 0
        onConnect(taskId)

        // 启动心跳
        startHeartbeat(taskId, ws)
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          handleMessage(taskId, data)
        } catch (e) {
          console.error(`WebSocket message parse error: ${taskId}`, e)
        }
      }

      ws.onerror = (error) => {
        console.error(`WebSocket error: ${taskId}`, error)
        onError(taskId, error)
      }

      ws.onclose = () => {
        console.log(`WebSocket disconnected: ${taskId}`)
        isConnected.value[taskId] = false
        stopHeartbeat(taskId)
        onDisconnect(taskId)

        // 尝试重连
        handleReconnect(taskId)
      }

      wsMap.value[taskId] = ws
    } catch (e) {
      console.error(`WebSocket connection error: ${taskId}`, e)
      handleReconnect(taskId)
    }
  }

  /**
   * 处理 WebSocket 消息
   */
  function handleMessage(taskId, data) {
    switch (data.type) {
      case 'connected':
        console.log(`WebSocket connected message: ${taskId}`)
        break
      case 'status_update':
        onStatusUpdate(taskId, data)
        break
      // anc-hub 执行包导入进度（阶段C：task_status:{job_id} 频道，payload 含
      // status/progress/success_count/fail_count/total），同样回调 onStatusUpdate，
      // 由调用方按 job 前缀（hubjob_）识别；评测任务不会推送该类型，互不影响
      case 'hub_import_progress':
        onStatusUpdate(taskId, data)
        break
      case 'pong':
        // 心跳响应，不需要特殊处理
        break
      default:
        console.warn(`Unknown WebSocket message type: ${data.type}`)
    }
  }

  /**
   * 启动心跳
   */
  function startHeartbeat(taskId, ws) {
    stopHeartbeat(taskId)

    heartbeatTimers.value[taskId] = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send('ping')
      }
    }, HEARTBEAT_INTERVAL)
  }

  /**
   * 停止心跳
   */
  function stopHeartbeat(taskId) {
    if (heartbeatTimers.value[taskId]) {
      clearInterval(heartbeatTimers.value[taskId])
      delete heartbeatTimers.value[taskId]
    }
  }

  /**
   * 处理重连逻辑
   */
  function handleReconnect(taskId) {
    const currentRetry = retryCount.value[taskId] || 0

    if (currentRetry >= MAX_RETRIES) {
      console.log(`WebSocket max retries reached for ${taskId}, falling back to polling`)
      retryCount.value[taskId] = 0
      onFallback(taskId)
      return
    }

    const delay = RETRY_DELAYS[currentRetry] || RETRY_DELAYS[RETRY_DELAYS.length - 1]
    console.log(`WebSocket reconnecting ${taskId} in ${delay}ms (attempt ${currentRetry + 1}/${MAX_RETRIES})`)

    reconnectTimers.value[taskId] = setTimeout(() => {
      retryCount.value[taskId] = currentRetry + 1
      connect(taskId)
    }, delay)
  }

  /**
   * 断开 WebSocket 连接
   */
  function disconnect(taskId) {
    // 清除重连定时器
    if (reconnectTimers.value[taskId]) {
      clearTimeout(reconnectTimers.value[taskId])
      delete reconnectTimers.value[taskId]
    }

    // 停止心跳
    stopHeartbeat(taskId)

    // 关闭 WebSocket
    if (wsMap.value[taskId]) {
      wsMap.value[taskId].close()
      delete wsMap.value[taskId]
    }

    // 重置状态
    isConnected.value[taskId] = false
    retryCount.value[taskId] = 0
  }

  /**
   * 断开所有 WebSocket 连接
   */
  function stopAll() {
    const taskIds = Object.keys(wsMap.value)
    taskIds.forEach(taskId => disconnect(taskId))
  }

  /**
   * 启动指定任务的 WebSocket 连接
   */
  function startWebSocket(taskId) {
    connect(taskId)
  }

  /**
   * 获取连接状态
   */
  function getConnectionStatus(taskId) {
    return isConnected.value[taskId] || false
  }

  // 组件卸载时断开所有连接
  onUnmounted(() => {
    stopAll()
  })

  return {
    startWebSocket,
    stopWebSocket: disconnect,
    stopAll,
    getConnectionStatus,
    isConnected
  }
}
