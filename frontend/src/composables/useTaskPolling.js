import { onMounted, onUnmounted } from 'vue'
import { taskApi } from '@/api'

/**
 * 任务进度轮询 Composable
 * @param {Object} options - 配置项
 * @param {number} options.interval - 轮询间隔(ms)，默认8000
 * @param {number} options.degradeFactor - 降频倍数，默认4（8s → 32s）
 * @param {number} options.maxFailuresBeforeDegrade - 降频前的连续失败阈值，默认3
 * @param {Function} options.onTaskUpdate - 任务更新回调 (taskId, task) => void
 * @param {Function} options.onTaskComplete - 任务完成回调 (taskId, task) => void
 * @param {Function} options.onTaskFailed - 任务失败回调 (taskId, task) => void
 * @param {Function} options.onPollingError - 轮询错误回调 (taskId, error) => void
 *   （W14：仅在与降频切换的瞬间触发一次，由调用方负责用户提示）
 *
 * W14 轮询优化：
 * 1. 默认走轻端点 taskApi.getStatus（status/progress/completed_questions/total
 *    四字段），不再每 8s 拉全量 detail；轻端点 404（旧后端未部署）时本次会话
 *    自动回退全量 taskApi.get，调用方无感
 * 2. 连续 3 次拉取失败不再永久停摆：降频（4 倍间隔）继续重试并提示一次；
 *    任一次成功即恢复常规间隔
 */
export function useTaskPolling(options = {}) {
  const {
    interval = 8000,
    degradeFactor = 4,
    maxFailuresBeforeDegrade = 3,
    onTaskUpdate,
    onTaskComplete,
    onTaskFailed,
    onPollingError
  } = options

  // 轮询定时器 Map: taskId -> timerId
  const pollingTimers = new Map()

  // 连续失败计数 Map: taskId -> count（拉取成功即清零）
  const failureCount = new Map()

  // 已降频任务集合（降频只在阈值触发瞬间发生一次，成功后恢复）
  const degradedTasks = new Set()

  // 连续失败降频阈值
  const MAX_FAILURES = maxFailuresBeforeDegrade

  // 降频后的间隔（不低于原间隔 +1s，避免异常配置下降频失效）
  const DEGRADED_INTERVAL = Math.max(interval * degradeFactor, interval + 1000)

  // 终端状态列表
  const TERMINAL_STATUS = ['completed', 'failed', 'stopped']

  // 轻端点可用性（会话级记忆）：404 视为旧后端无此端点，回退全量 detail
  let lightStatusAvailable = true

  /**
   * 轻端点载荷归一化为任务对象形状：total → total_questions，
   * 使调用方既有的 { ...旧行, ...新数据 } 合并逻辑无需感知端点差异
   */
  const normalizeStatusPayload = (raw) => {
    if (raw && raw.total !== undefined && raw.total_questions === undefined) {
      return {
        status: raw.status,
        progress: raw.progress,
        completed_questions: raw.completed_questions,
        total_questions: raw.total
      }
    }
    return raw
  }

  /**
   * 获取任务最新状态（轻端点优先，404 回退全量 detail）
   */
  const fetchTaskStatus = async (taskId) => {
    let task = null
    try {
      if (lightStatusAvailable) {
        try {
          task = await taskApi.getStatus(taskId)
        } catch (e) {
          if (e?.response?.status === 404) {
            // 旧后端无轻端点：本次会话回退全量 detail，不再反复打 404
            lightStatusAvailable = false
          } else {
            throw e
          }
        }
      }
      if (!task) {
        task = await taskApi.get(taskId)
      }
    } catch (error) {
      handlePollingError(taskId, error)
      return
    }
    task = normalizeStatusPayload(task)

    // 拉取成功：清零连续失败计数；处于降频态则恢复常规间隔
    failureCount.delete(taskId)
    if (degradedTasks.has(taskId)) {
      degradedTasks.delete(taskId)
      reschedulePolling(taskId, interval)
    }

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
  }

  /**
   * 处理轮询错误：阈值内 1s 快速重试；达到阈值降频继续（不再永久停摆）
   */
  const handlePollingError = (taskId, error) => {
    // 轮询已被停止（含终端状态/手动停止）时，忽略在途请求的错误，
    // 避免残留的快速重试定时器把已停止的任务拉回错误处理循环
    if (!pollingTimers.has(taskId)) {
      return
    }

    const failures = (failureCount.get(taskId) || 0) + 1
    failureCount.set(taskId, failures)

    if (failures < MAX_FAILURES) {
      // 未达阈值：1s 快速重试（叠加在间隔定时器上，尽快从瞬时抖动恢复）
      setTimeout(() => fetchTaskStatus(taskId), 1000)
      return
    }

    // 达到阈值：降频重试，仅在降档瞬间提示一次（后续失败不再重复提示）
    if (!degradedTasks.has(taskId)) {
      degradedTasks.add(taskId)
      reschedulePolling(taskId, DEGRADED_INTERVAL)
      onPollingError?.(taskId, error)
    }
  }

  /**
   * 以指定间隔重建轮询定时器（间隔切换的唯一入口）
   */
  const reschedulePolling = (taskId, ms) => {
    const existing = pollingTimers.get(taskId)
    if (existing) {
      clearInterval(existing)
    }
    pollingTimers.set(taskId, setInterval(() => {
      fetchTaskStatus(taskId)
    }, ms))
  }

  /**
   * 开始轮询单个任务
   */
  const startPolling = (taskId) => {
    if (pollingTimers.has(taskId)) {
      return // 已经在轮询中
    }

    // 立即执行一次（此后 reschedulePolling 注册定时器，
    // 故 handlePollingError 的在途守卫此时已可见本任务）
    fetchTaskStatus(taskId)
    reschedulePolling(taskId, interval)
  }

  /**
   * 停止轮询单个任务
   */
  const stopPolling = (taskId) => {
    const timerId = pollingTimers.get(taskId)
    if (timerId) {
      clearInterval(timerId)
      pollingTimers.delete(taskId)
    }
    failureCount.delete(taskId)
    degradedTasks.delete(taskId)
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
    failureCount.clear()
    degradedTasks.clear()
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
