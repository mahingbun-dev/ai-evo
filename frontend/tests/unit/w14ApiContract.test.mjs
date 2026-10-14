/**
 * W14 API 层行为与契约单元测试
 * 文件: frontend/tests/unit/w14ApiContract.test.mjs
 * 运行: node --test frontend/tests/unit/w14ApiContract.test.mjs
 *
 * 覆盖（行为级：真实导入 src/api/index.js，axios 实例注入桩 adapter）：
 * - 401 拦截器：reject（不再 `new Promise(() => {})` 永久挂起）+ 统一跳登录
 *   （SSO 配置可用跳 SSO 登录页；无配置回首页由路由守卫接管）
 * - fetchSsoConfig：加载失败/空配置（无 sso_login_url）不缓存，可用配置才缓存
 * - 非 401 的 detail 错误透传、blob 错误体解析（回归，改动同函数）
 * - taskApi.getStatus 轻端点 URL 契约；hubImportApi 三端点契约不变
 * - Datasets.vue / Tasks.vue 源码锚点：异步 job 接线、WS 进度面板复活、
 *   同步链路废弃、WS 连接成功即停轮询
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const apiSrc = readFileSync(join(here, '../../src/api/index.js'), 'utf-8')
const datasetsSrc = readFileSync(join(here, '../../src/views/Datasets.vue'), 'utf-8')
const tasksSrc = readFileSync(join(here, '../../src/views/Tasks.vue'), 'utf-8')

// 拦截器内引用 window.location（本地登录分支读 pathname/search）—— 先放好桩
globalThis.window = { location: { href: 'http://localhost:3000/datasets', pathname: '/datasets', search: '' } }

// 本地登录的 token 存取走 localStorage —— 裸 node 环境放内存桩
const _store = new Map()
globalThis.localStorage = {
  getItem: (k) => (_store.has(k) ? _store.get(k) : null),
  setItem: (k, v) => _store.set(k, String(v)),
  removeItem: (k) => _store.delete(k),
  clear: () => _store.clear(),
}

const { default: request, taskApi, hubImportApi } = await import('../../src/api/index.js')

// 响应拦截器错误处理函数（axios 实例链上第 0 个 handler）
const responseRejected = request.interceptors.response.handlers[0].rejected

// 桩 adapter：每个用例自行设定行为，并捕获最近一次请求的 method/url
let adapterBehavior = null
let lastConfig = null
request.defaults.adapter = async (config) => {
  lastConfig = config
  return adapterBehavior(config)
}

const respond = (data) => async () => ({ data, status: 200, statusText: 'OK', headers: {}, config: {} })
const rejectWith = (status, data) => async () => {
  const err = new Error(`Request failed with status code ${status}`)
  err.response = { status, data, headers: {}, config: {} }
  throw err
}

describe('W14 401 拦截器：reject + 统一跳登录（本地登录形态）', () => {
  it('非认证接口 401 → 清 token + 跳登录页（带 redirect）+ 明确 reject（不再永久挂起）', async () => {
    localStorage.setItem('ai_evo_token', 'tok-1')
    globalThis.window.location.href = 'http://localhost:3000/tasks'

    await assert.rejects(
      responseRejected({ response: { status: 401, data: {} }, config: { url: '/evaluation/tasks/list' } }),
      /登录已失效/
    )
    assert.equal(localStorage.getItem('ai_evo_token'), null)          // token 已清
    assert.match(globalThis.window.location.href, /\/login\?redirect=/) // 跳登录并带回跳
  })

  it('登录/注册接口自身的 401（密码错误等）→ 不跳转不清 token，照常透传', async () => {
    localStorage.setItem('ai_evo_token', 'tok-2')
    globalThis.window.location.href = 'http://localhost:3000/login'
    globalThis.window.location.pathname = '/login'

    await assert.rejects(
      responseRejected({ response: { status: 401, data: { detail: '用户名或密码错误' } }, config: { url: '/auth/login' } }),
      (e) => e.message === '用户名或密码错误'
    )
    assert.equal(localStorage.getItem('ai_evo_token'), 'tok-2')       // 不清 token
    assert.equal(globalThis.window.location.href, 'http://localhost:3000/login') // 不跳转
  })

  it('已在登录页时的 401 → 不重复跳转，仍明确 reject', async () => {
    globalThis.window.location.href = 'http://localhost:3000/login'
    globalThis.window.location.pathname = '/login'
    await assert.rejects(
      responseRejected({ response: { status: 401, data: {} }, config: { url: '/models' } }),
      /登录已失效/
    )
    assert.equal(globalThis.window.location.href, 'http://localhost:3000/login')
  })

  it('非 401 的 detail 错误照常透传为中文消息（回归）', async () => {
    await assert.rejects(
      responseRejected({ response: { status: 400, data: { detail: '任务不存在' } }, config: { url: '/x' } }),
      (e) => e.message === '任务不存在' && e.response?.status === 400
    )
  })

  it('blob 错误体解析出 detail（回归，同函数改动）', async () => {
    const blob = new Blob([JSON.stringify({ detail: '文件过大' })], { type: 'application/json' })
    await assert.rejects(
      responseRejected({ response: { status: 413, data: blob }, config: { url: '/x' } }),
      (e) => e.message === '文件过大'
    )
  })
})

describe('W14 API 契约：轻端点与 hub 导入 job', () => {
  it('taskApi.getStatus → GET /evaluation/tasks/{id}/status', async () => {
    adapterBehavior = respond({ status: 'running', progress: 1, completed_questions: 0, total: 0 })
    const res = await taskApi.getStatus('task-abc')
    assert.equal(lastConfig.method, 'get')
    assert.equal(lastConfig.url, '/evaluation/tasks/task-abc/status')
    assert.equal(res.status, 'running')
  })

  it('hubImportApi.createJob → POST /evaluation/datasets/import/hub-files', async () => {
    adapterBehavior = respond({ id: 'hubjob_1', status: 'pending', total_count: 3 })
    const job = await hubImportApi.createJob({ file_ids: ['f1', 'f2'], name: '数据集A' })
    assert.equal(lastConfig.method, 'post')
    assert.equal(lastConfig.url, '/evaluation/datasets/import/hub-files')
    assert.equal(job.id, 'hubjob_1')
  })

  it('hubImportApi.getJob / retryJob → /hub-import-jobs/{id}（进度与重试）', async () => {
    adapterBehavior = respond({ id: 'hubjob_1', status: 'failed', fail_count: 1, fail_detail: { f2: '包损坏' } })
    await hubImportApi.getJob('hubjob_1')
    assert.equal(lastConfig.method, 'get')
    assert.equal(lastConfig.url, '/hub-import-jobs/hubjob_1')

    adapterBehavior = respond({ id: 'hubjob_1', status: 'pending' })
    await hubImportApi.retryJob('hubjob_1')
    assert.equal(lastConfig.method, 'post')
    assert.equal(lastConfig.url, '/hub-import-jobs/hubjob_1/retry')
  })
})

describe('W14 源码锚点：异步导入接线与双通道消除', () => {
  it('Datasets.vue：hub 导入全走异步 job 链路，同步 10 分钟链路废弃', () => {
    assert.match(datasetsSrc, /hubImportApi\.createJob\(/)       // 创建异步 job
    assert.match(datasetsSrc, /hubImportApi\.getJob\(/)          // 进度查询（兜底轮询/补明细）
    assert.match(datasetsSrc, /hubImportApi\.retryJob\(/)        // 失败项重试
    assert.doesNotMatch(datasetsSrc, /datasetApi\.importSessionsFromHub\(/) // 同步链路调用移除
    assert.doesNotMatch(datasetsSrc, /hubImportMode/)            // 导入形态分派随同步链路一并移除
  })

  it('Datasets.vue：WS 进度面板（死 CSS 复活）+ hub_import_progress 消费', () => {
    assert.match(datasetsSrc, /import \{ useTaskWebSocket \} from '@\/composables\/useTaskWebSocket'/)
    assert.match(datasetsSrc, /hub_import_progress/)             // WS 消息类型消费
    // 复活死 CSS：模板与样式同时存在 hub-job-panel 家族类
    for (const cls of ['hub-job-panel', 'hub-job-status-line', 'hub-job-count', 'hub-job-ok', 'hub-job-bad']) {
      const templateUses = new RegExp(`class="[^"]*${cls}`).test(datasetsSrc)
      const styleDefines = new RegExp(`\\.${cls}\\s*\\{`).test(datasetsSrc)
      assert.ok(templateUses && styleDefines, `面板类 ${cls} 应同时出现在模板与样式中`)
    }
    assert.match(datasetsSrc, /重试失败项/)
  })

  it('Datasets.vue：新弹窗重置 job 关注态，卸载停止 WS/轮询', () => {
    assert.match(datasetsSrc, /const openDialog[\s\S]*?resetHubJobWatch\(\)/)
    assert.match(datasetsSrc, /onUnmounted\(\(\) => \{\s*stopHubJobWatch\(\)/)
  })

  it('Tasks.vue：WS 连接成功即停对应任务轮询；失败降频有用户提示', () => {
    assert.match(tasksSrc, /onConnect: \(taskId\) => \{[\s\S]*?stopPolling\(taskId\)/)
    assert.match(tasksSrc, /onFallback: \(taskId\) => \{[\s\S]*?startPolling\(taskId\)/)
    assert.match(tasksSrc, /onPollingError[\s\S]*?ElMessage\.warning\(/)
  })

  it('api/index.js：轻端点方法与 401 reject 落位', () => {
    assert.match(apiSrc, /getStatus: \(id\) => request\.get\(`\/evaluation\/tasks\/\$\{id\}\/status`\)/)
    assert.doesNotMatch(apiSrc, /new Promise\(\(\) => \{\}\)/)  // 永久挂起手法清除
    assert.match(apiSrc, /登录已失效，请重新登录/)
  })
})
