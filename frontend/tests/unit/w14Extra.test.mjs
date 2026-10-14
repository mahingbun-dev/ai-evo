/**
 * W14 前端补充验证（测试角色）：开发交付的 w14ApiContract/w14TaskPolling 之外的缺口
 * 文件: frontend/tests/unit/w14Extra.test.mjs
 * 运行: node --test frontend/tests/unit/w14Extra.test.mjs
 *
 * - Results.vue（useTaskPolling 另一调用方）：终端提示任务名从 allTasks 列表行补齐
 *   （轻端点载荷不含 name——交付摘要声明，交付测试未覆盖 Results.vue）
 * - vite.config.js：/ws WebSocket 代理通道落位
 * - package.json：test:unit 门控脚本 = node --test tests/unit/
 * - useTaskPolling 行为补充：轻端点 5xx（非 404）不计为「旧后端」——不回退全量
 *   detail，直接计入失败计数（404 才是回退触发条件）
 * - Datasets.vue 锚点补充：WS total → total_count 字段映射；失败终态补拉 getJob 明细
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const read = (p) => readFileSync(join(here, p), 'utf-8')

const resultsSrc = read('../../src/views/Results.vue')
const viteSrc = read('../../vite.config.js')
const pkg = JSON.parse(read('../../package.json'))
const composableSrc = read('../../src/composables/useTaskPolling.js')
const datasetsSrc = read('../../src/views/Datasets.vue')

// ---------------- 受控加载 useTaskPolling（与 w14TaskPolling.test.mjs 同手法） ----------------

function loadUseTaskPolling() {
  const src = composableSrc
    .replace("import { onMounted, onUnmounted } from 'vue'", 'const { onMounted, onUnmounted } = __vue')
    .replace("import { taskApi } from '@/api'", 'const { taskApi } = __api')
    .replace('export function useTaskPolling', 'function useTaskPolling')
  return new Function('__vue', '__api', `${src}\nreturn useTaskPolling`)
}

const vueStub = { onMounted: () => {}, onUnmounted: () => {} }

function installClock() {
  const state = { seq: 0, intervals: [], timeouts: [] }
  const real = {
    si: globalThis.setInterval, ci: globalThis.clearInterval,
    st: globalThis.setTimeout, ct: globalThis.clearTimeout
  }
  globalThis.setInterval = (fn, ms) => { const id = ++state.seq; state.intervals.push({ id, fn, ms }); return id }
  globalThis.clearInterval = (id) => { state.intervals = state.intervals.filter(t => t.id !== id) }
  globalThis.setTimeout = (fn, ms) => { const id = ++state.seq; state.timeouts.push({ id, fn, ms }); return id }
  globalThis.clearTimeout = (id) => { state.timeouts = state.timeouts.filter(t => t.id !== id) }
  return { state, restore() { Object.assign(globalThis, real) } }
}

const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve()
  await new Promise(r => setImmediate(r))
}

// ---------------- 锚点用例 ----------------

describe('W14 补充锚点：Results.vue / vite / package.json / Datasets.vue', () => {
  it('Results.vue：三处终端提示任务名从 allTasks 列表行补齐（轻端点载荷不含 name）', () => {
    // onTaskUpdate / onTaskComplete / onTaskFailed 三处回调
    const matches = resultsSrc.match(/(?:allTasks\.value\.find|allTasks\.value\?\.find)\(t => t\.id === taskId\)\??\.name/g) || []
    assert.ok(matches.length >= 3, `终端提示应至少 3 处从 allTasks 补齐任务名，实际 ${matches.length} 处`)
    // 轮询回调里不再直接拼 task.name/updatedTask.name（轻端点无此字段 → undefined）
    for (const fn of ['onTaskUpdate', 'onTaskComplete', 'onTaskFailed']) {
      const idx = resultsSrc.indexOf(`${fn}:`)
      assert.ok(idx >= 0, `存在 ${fn} 回调`)
      const nextIdx = resultsSrc.indexOf('\n  },', idx)
      const body = resultsSrc.slice(idx, nextIdx)
      assert.doesNotMatch(body, /\b(updatedTask|task)\.name\b/, `${fn} 内不得直接引用载荷的 .name`)
    }
  })

  it('vite.config.js：dev 代理补 /ws WebSocket 通道（ws: true）', () => {
    assert.match(viteSrc, /'\/ws'\s*:\s*\{[\s\S]*?ws\s*:\s*true/)
    assert.match(viteSrc, /'\/ws'\s*:\s*\{[\s\S]*?VITE_PROXY_TARGET/)
  })

  it('package.json：test:unit 门控脚本含 vitest 与 W14 的 node --test 锚点', () => {
    // 与 W8（vitest，tests/vitest-legacy/）融合：两套都跑；node --test 只圈
    // w14 前缀文件——modelsPreScriptAdmin 等既有文件存在基线失败（与旧版
    // Scripts.vue 脱节），不得纳入 CI 门禁
    assert.equal(pkg.scripts['test:unit'], 'vitest run && node --test tests/unit/w14*.test.mjs')
  })

  it('Datasets.vue：WS total → total_count 字段映射；失败终态补拉 getJob 补 fail_detail', () => {
    // useTaskWebSocket 推送载荷字段是 total，job/面板形状是 total_count
    assert.match(datasetsSrc, /total_count:\s*data\.total/)
    // failed 终态时经 hubImportApi.getJob 补齐失败明细（WS 推送不带 fail_detail）
    assert.match(datasetsSrc, /status === 'failed'[\s\S]{0,400}?hubImportApi\.getJob\(/)
  })
})

// ---------------- 行为补充：非 404 错误不回退全量 ----------------

describe('W14 useTaskPolling 行为补充：5xx 不视为旧后端', () => {
  it('轻端点 500 → 不回退全量 get，计入失败计数（1s 快速重试）', async () => {
    const clock = installClock()
    try {
      const calls = { getStatus: [], get: [] }
      const err = new Error('服务器内部错误')
      err.response = { status: 500 }
      const taskApi = {
        getStatus: async (id) => { calls.getStatus.push(id); throw err },
        get: async (id) => { calls.get.push(id); return { id, status: 'running' } }
      }
      const hooks = { error: [] }
      const useTaskPolling = loadUseTaskPolling()(vueStub, { taskApi })
      const c = useTaskPolling({
        interval: 8000,
        onPollingError: (id, e) => hooks.error.push({ id, e })
      })

      c.startPolling('t1')
      await flush()

      // 500 只算普通失败：快速重试已排，但绝没有静默回退全量 detail
      assert.deepEqual(calls.getStatus, ['t1'])
      assert.deepEqual(calls.get, [])
      assert.equal(clock.state.timeouts.length, 1)
      assert.equal(clock.state.timeouts[0].ms, 1000)
      assert.equal(hooks.error.length, 0) // 未达降频阈值
    } finally {
      clock.restore()
    }
  })
})
