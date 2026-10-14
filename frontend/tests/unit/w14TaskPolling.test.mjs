/**
 * W14 任务轮询 Composable 行为单元测试
 * 文件: frontend/tests/unit/w14TaskPolling.test.mjs
 * 运行: node --test frontend/tests/unit/w14TaskPolling.test.mjs
 *
 * 覆盖 useTaskPolling.js 的 W14 轮询优化（行为级，受控环境求值）：
 * - 默认走轻端点 taskApi.getStatus（status/progress/completed_questions/total），
 *   载荷归一化 total → total_questions，调用方合并逻辑无感
 * - 轻端点 404（旧后端）→ 本次会话回退全量 taskApi.get，后续 tick 不再打轻端点
 * - 终端状态停止并触发完成/失败回调
 * - 连续 3 次失败 → 降频（4 倍间隔）继续轮询不永久停摆，onPollingError 仅提示一次；
 *   降频后任一次成功恢复常规间隔
 * - stopPolling 后残留的快速重试不把已停任务拉回错误处理循环
 *
 * 手法：与本目录既有测试一致——源码受控求值（stub vue 与 @/api），
 * 定时器用捕获式假时钟手动驱动，Promise 用 setImmediate flush。
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const composableSrc = readFileSync(join(here, '../../src/composables/useTaskPolling.js'), 'utf-8')

// ---------------- 受控加载 ----------------

function loadUseTaskPolling() {
  const src = composableSrc
    .replace("import { onMounted, onUnmounted } from 'vue'", 'const { onMounted, onUnmounted } = __vue')
    .replace("import { taskApi } from '@/api'", 'const { taskApi } = __api')
    .replace('export function useTaskPolling', 'function useTaskPolling')
  return new Function('__vue', '__api', `${src}\nreturn useTaskPolling`)
}

const vueStub = {
  onMounted: () => {},   // 可见性监听不在本套测试范围
  onUnmounted: () => {}
}

// ---------------- 假时钟 ----------------

function installClock() {
  const state = { seq: 0, intervals: [], timeouts: [], cleared: [] }
  const real = {
    si: globalThis.setInterval, ci: globalThis.clearInterval,
    st: globalThis.setTimeout, ct: globalThis.clearTimeout
  }
  globalThis.setInterval = (fn, ms) => {
    const id = ++state.seq
    state.intervals.push({ id, fn, ms })
    return id
  }
  globalThis.clearInterval = (id) => {
    state.cleared.push(id)
    const i = state.intervals.findIndex(t => t.id === id)
    if (i >= 0) state.intervals.splice(i, 1)
  }
  globalThis.setTimeout = (fn, ms) => {
    const id = ++state.seq
    state.timeouts.push({ id, fn, ms })
    return id
  }
  globalThis.clearTimeout = (id) => {
    state.cleared.push(id)
    const i = state.timeouts.findIndex(t => t.id === id)
    if (i >= 0) state.timeouts.splice(i, 1)
  }
  return {
    state,
    restore() {
      globalThis.setInterval = real.si
      globalThis.clearInterval = real.ci
      globalThis.setTimeout = real.st
      globalThis.clearTimeout = real.ct
    }
  }
}

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve()
  await new Promise(r => setImmediate(r))
}

// ---------------- API stub ----------------

const makeErr = (status, message = '请求失败') => {
  const e = new Error(message)
  if (status) e.response = { status }
  return e
}

function makeApi({ statusImpl, getImpl } = {}) {
  const calls = { getStatus: [], get: [] }
  const taskApi = {
    getStatus: async (id) => {
      calls.getStatus.push(id)
      if (!statusImpl) throw makeErr(null, 'getStatus 未实现')
      return statusImpl(id)
    },
    get: async (id) => {
      calls.get.push(id)
      if (!getImpl) throw makeErr(null, 'get 未实现')
      return getImpl(id)
    }
  }
  return { calls, taskApi }
}

function makeHooks() {
  const hooks = { update: [], complete: [], failed: [], error: [] }
  const options = () => ({
    interval: 8000,
    onTaskUpdate: (id, t) => hooks.update.push({ id, t }),
    onTaskComplete: (id, t) => hooks.complete.push({ id, t }),
    onTaskFailed: (id, t) => hooks.failed.push({ id, t }),
    onPollingError: (id, e) => hooks.error.push({ id, e })
  })
  return { hooks, options }
}

// ---------------- 用例 ----------------

describe('W14 useTaskPolling：轻端点切换', () => {
  it('默认走轻端点 getStatus，载荷归一化 total → total_questions', async () => {
    const clock = installClock()
    try {
      const api = makeApi({
        statusImpl: () => ({ status: 'running', progress: 30, completed_questions: 3, total: 9 })
      })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      await flush()

      assert.deepEqual(api.calls.getStatus, ['t1'])
      assert.deepEqual(api.calls.get, [])
      assert.deepEqual(hooks.update[0].t, {
        status: 'running', progress: 30, completed_questions: 3, total_questions: 9
      })
    } finally {
      clock.restore()
    }
  })

  it('轻端点 404 → 回退全量 detail，且后续 tick 不再打轻端点', async () => {
    const clock = installClock()
    try {
      const api = makeApi({
        statusImpl: () => { throw makeErr(404, '任务不存在') },
        getImpl: () => ({ id: 't1', name: '任务A', status: 'running', progress: 10, completed_questions: 1, total_questions: 5 })
      })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      await flush()

      // 首次：轻端点 404 → 全量兜底成功
      assert.equal(api.calls.getStatus.length, 1)
      assert.equal(api.calls.get.length, 1)
      assert.equal(hooks.update[0].t.name, '任务A')  // 全量载荷原样透传

      // 第二个 tick：轻端点已记忆不可用，直接走全量
      clock.state.intervals[0].fn()
      await flush()
      assert.equal(api.calls.getStatus.length, 1)
      assert.equal(api.calls.get.length, 2)
    } finally {
      clock.restore()
    }
  })
})

describe('W14 useTaskPolling：终端状态与停止', () => {
  it('completed 停止轮询并触发 onTaskComplete', async () => {
    const clock = installClock()
    try {
      const api = makeApi({
        statusImpl: () => ({ status: 'completed', progress: 100, completed_questions: 9, total: 9 })
      })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      await flush()

      assert.equal(hooks.complete.length, 1)
      assert.equal(hooks.complete[0].t.total_questions, 9)
      assert.equal(clock.state.intervals.length, 0)      // 定时器已清
      assert.equal(c.isPolling('t1'), false)
    } finally {
      clock.restore()
    }
  })

  it('failed 触发 onTaskFailed；startPolling 幂等（不叠加定时器）', async () => {
    const clock = installClock()
    try {
      const api = makeApi({
        statusImpl: () => ({ status: 'failed', progress: 40, completed_questions: 3, total: 9 })
      })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      c.startPolling('t1')
      await flush()

      assert.equal(hooks.failed.length, 1)
      assert.equal(clock.state.intervals.length, 0)
      assert.equal(clock.state.cleared.length, 1)  // 第二次 startPolling 未注册新定时器
    } finally {
      clock.restore()
    }
  })
})

describe('W14 useTaskPolling：连续失败降频（不永久停摆）', () => {
  it('3 次失败后降频 4 倍间隔继续轮询，onPollingError 仅提示一次', async () => {
    const clock = installClock()
    try {
      const api = makeApi({ statusImpl: () => { throw makeErr(null, '网络抖动') } })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      await flush()

      // 失败 1：1s 快速重试入队
      assert.equal(clock.state.timeouts.length, 1)
      assert.equal(clock.state.timeouts[0].ms, 1000)
      assert.equal(hooks.error.length, 0)

      // 失败 2：仍快速重试
      clock.state.timeouts[0].fn()
      await flush()
      assert.equal(clock.state.timeouts.length, 2)
      assert.equal(hooks.error.length, 0)

      // 失败 3：触发降频——常规间隔换 32s，提示一次，轮询不停止
      clock.state.timeouts[1].fn()
      await flush()
      assert.equal(hooks.error.length, 1)
      assert.equal(clock.state.intervals.length, 1)
      assert.equal(clock.state.intervals[0].ms, 32000)
      assert.equal(c.isPolling('t1'), true)

      // 降频后再失败：不再重复提示、不再排快速重试
      const timeoutsBefore = clock.state.timeouts.length
      clock.state.intervals[0].fn()
      await flush()
      assert.equal(hooks.error.length, 1)
      assert.equal(clock.state.timeouts.length, timeoutsBefore)
      assert.equal(clock.state.intervals.length, 1)
      assert.equal(clock.state.intervals[0].ms, 32000)
    } finally {
      clock.restore()
    }
  })

  it('降频后一次成功即恢复常规间隔', async () => {
    const clock = installClock()
    try {
      let fail = true
      const api = makeApi({
        statusImpl: () => {
          if (fail) throw makeErr(null, '网络抖动')
          return { status: 'running', progress: 50, completed_questions: 5, total: 9 }
        }
      })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      await flush()
      clock.state.timeouts[0].fn()
      await flush()
      clock.state.timeouts[1].fn()
      await flush()
      assert.equal(clock.state.intervals[0].ms, 32000)  // 已降频

      fail = false
      clock.state.intervals[0].fn()
      await flush()

      assert.equal(hooks.error.length, 1)               // 降频提示没有重复
      assert.equal(clock.state.intervals.length, 1)
      assert.equal(clock.state.intervals[0].ms, 8000)   // 恢复常规间隔
      assert.equal(hooks.update[0].t.status, 'running')
    } finally {
      clock.restore()
    }
  })

  it('stopPolling 后残留的快速重试不复活错误处理', async () => {
    const clock = installClock()
    try {
      const api = makeApi({ statusImpl: () => { throw makeErr(null, '网络抖动') } })
      const { hooks, options } = makeHooks()
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi: api.taskApi })
      const c = useTaskPolling(options())

      c.startPolling('t1')
      await flush()
      const stale = clock.state.timeouts[0]             // 失败 1 排下的 1s 快速重试
      c.stopPolling('t1')

      stale.fn()
      await flush()

      assert.equal(hooks.error.length, 0)               // 在途错误被吞掉
      assert.equal(clock.state.intervals.length, 0)
      assert.equal(clock.state.timeouts.length, 1)      // 未新增重试
    } finally {
      clock.restore()
    }
  })
})
