import { ref, onMounted, onUnmounted } from 'vue'
import { taskApi } from '@/api'

/**
 * 任务进度轮询 Composable
 * @param {Object} options - 配置项
 * @param {number} options.interval - 轮询间隔(ms)，默认8000
 * @param {Function} options.onTaskUpdate - 任务更新回调 (taskId, task) => void
 * @param {Function} options.onTaskComplete - 任务完成回调 (taskId, task) => void
 * @param {Function} options.onTaskFailed - 任务失败回调 (taskId, task) => void
 * @param {Function} options.onPollingError - 轮询错误回调 (taskId, error) => void
 */
export function useTaskPolling(options = {}) {
  const {
    interval = 8000,
    onTaskUpdate,
    onTaskComplete,
    onTaskFailed,
    onPollingError
  } = options

  // 轮询定时器 Map: taskId -> timerId
  const pollingTimers = new Map()

  // 重试计数器 Map: taskId -> retryCount
  const retryCount = new Map()

  // 最大重试次数
  const MAX_RETRIES = 3

  // 终端状态列表
  const TERMINAL_STATUS = ['completed', 'failed', 'stopped']

  /**
   * 获取任务最新状态
   */
  const fetchTaskStatus = async (taskId) => {
    try {
      const task = await taskApi.get(taskId)
      retryCount.set(taskId, 0) // 重置重试计数

      // 触发更新回调
      onTaskUpdate?.(taskId, task)

      // 检查是否为终端状态
      if (TERMINAL_STATUS.includes(task.status)) {
        stopPolling(taskId)

        // 触发完成/失败回调
        if (task.status === 'completed') {
          onTaskComplete?.(taskId, task)
        } else if (task.status === 'failed') {
          onTaskFailed?.(taskId, task)
        }
      }
    } catch (error) {
      handlePollingError(taskId, error)
    }
  }

  /**
   * 处理轮询错误
   */
  const handlePollingError = (taskId, error) => {
    const currentRetries = retryCount.get(taskId) || 0

    if (currentRetries < MAX_RETRIES) {
      // 重试次数未满，增加重试计数并立即重试
      retryCount.set(taskId, currentRetries + 1)
      setTimeout(() => fetchTaskStatus(taskId), 1000)
    } else {
      // 达到最大重试次数，停止轮询
      stopPolling(taskId)
      retryCount.delete(taskId)
      onPollingError?.(taskId, error)
    }
  }

  /**
   * 开始轮询单个任务
   */
  const startPolling = (taskId) => {
    if (pollingTimers.has(taskId)) {
      return // 已经在轮询中
    }

    // 立即执行一次
    fetchTaskStatus(taskId)

    // 设置定时器
    const timerId = setInterval(() => {
      fetchTaskStatus(taskId)
    }, interval)

    pollingTimers.set(taskId, timerId)
  }

  /**
   * 停止轮询单个任务
   */
  const stopPolling = (taskId) => {
    const timerId = pollingTimers.get(taskId)
    if (timerId) {
      clearInterval(timerId)
      pollingTimers.delete(taskId)
      retryCount.delete(taskId)
    }
  }

  /**
   * 批量开始轮询
   */
  const startPollingAll = (taskIds) => {
    taskIds.forEach(id => startPolling(id))
  }

  /**
   * 批量停止轮询
   */
  const stopPollingAll = (taskIds) => {
    taskIds.forEach(id => stopPolling(id))
  }

  /**
   * 停止所有轮询
   */
  const stopAll = () => {
    pollingTimers.forEach((timerId) => {
      clearInterval(timerId)
    })
    pollingTimers.clear()
    retryCount.clear()
  }

  /**
   * 检查任务是否正在轮询
   */
  const isPolling = (taskId) => {
    return pollingTimers.has(taskId)
  }

  /**
   * 获取当前正在轮询的任务ID列表
   */
  const getPollingTaskIds = () => {
    return Array.from(pollingTimers.keys())
  }

  /**
   * 页面可见性变化处理
   */
  const handleVisibilityChange = () => {
    if (document.visibilityState === 'visible') {
      // 页面变为可见时，立即同步所有轮询任务的状态
      pollingTimers.forEach((_, taskId) => {
        fetchTaskStatus(taskId)
      })
    }
  }

  // 组件挂载时添加可见性监听
  onMounted(() => {
    document.addEventListener('visibilitychange', handleVisibilityChange)
  })

  // 组件卸载时清理
  onUnmounted(() => {
    document.removeEventListener('visibilitychange', handleVisibilityChange)
    stopAll()
  })

  return {
    startPolling,
    stopPolling,
    startPollingAll,
    stopPollingAll,
    stopAll,
    isPolling,
    getPollingTaskIds
  }
}
