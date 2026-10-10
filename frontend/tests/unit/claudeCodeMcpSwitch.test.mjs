/**
 * 二期 UI 验收缺陷修复单元测试：CC 配置卡「脚本 MCP 工具」开关（enable_script_mcp）
 * 文件: frontend/tests/unit/claudeCodeMcpSwitch.test.mjs
 * 运行: node --test frontend/tests/unit/claudeCodeMcpSwitch.test.mjs
 *
 * 与验收一致的手法：提取 Models.vue「Claude Code 裁判配置（模块 B）」区段真实源码，
 * 在受控上下文中求值：
 * - D1 CC 卡不存在开关：模板须有 el-switch（默认关）、表单默认值 enable_script_mcp=false、
 *   loadCcConfig 回读后端布尔
 * - D2 UI 保存静默关闭开关：saveCcConfig 提交体必须显式携带 enable_script_mcp
 *   （后端 PUT 为全量语义，claude_code_config_service.save_config 对缺省字段按 False 落库）
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const vueSrc = readFileSync(join(here, '../../src/views/Models.vue'), 'utf-8')

const START_MARK = '// ==================== Claude Code 裁判配置（模块 B） ===================='
const END_MARK = '// ==================== DeepSeek Harness 裁判配置 ===================='
const startIdx = vueSrc.indexOf(START_MARK)
const endIdx = vueSrc.indexOf(END_MARK)
if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx) {
  throw new Error('未能在 Models.vue 中定位「Claude Code 裁判配置」区段（源码结构可能已变化，请修正测试锚点）')
}

function buildView() {
  const calls = { update: [], messages: [] }
  const ctx = {
    ref: (v) => ({ value: v }),
    computed: (fn) => ({ get value() { return fn() } }),
    ElMessage: {
      success: (m) => calls.messages.push(['success', m]),
      error: (m) => calls.messages.push(['error', m]),
    },
    claudeCodeApi: {
      getConfig: async () => ctx.__getConfigResponse,
      update: async (payload) => { calls.update.push(payload); return { configured: true } },
      test: async () => ({ success: true }),
    },
    // 区段内使用的 JSON 辅助（真实实现位于文件更后处的通用段，行为对齐即可）
    parseJsonField: (field) => {
      if (field == null) return {}
      if (typeof field === 'object') return field
      try { return JSON.parse(field) } catch { return {} }
    },
    jsonToConfig: (obj) => Object.entries(obj || {}).map(([key, value]) => ({ key, value })),
    configToJson: (rows) => Object.fromEntries((rows || []).map((r) => [r.key, r.value])),
  }
  ctx.__getConfigResponse = { configured: false, config: null }
  const body = vueSrc.slice(startIdx, endIdx)
  const factory = new Function(
    ...Object.keys(ctx),
    `${body}\nreturn {
      getDefaultCcForm, ccForm, loadCcConfig, saveCcConfig, ccConfigured,
      ccExtraEnvConfig, ccFormRef,
    };`
  )
  return { api: factory(...Object.values(ctx)), calls, ctx }
}

describe('D1 CC 配置卡「脚本 MCP 工具」开关存在且默认关', () => {
  it('表单默认值 enable_script_mcp=false（默认关），且为完整表单字段', () => {
    const { api } = buildView()
    const def = api.getDefaultCcForm()
    assert.equal(def.enable_script_mcp, false, '开关默认必须为关')
    for (const key of ['name', 'base_url', 'auth_token', 'model', 'timeout_seconds', 'max_turns', 'proxy_url']) {
      assert.ok(key in def, `缺少既有字段 ${key}`)
    }
    assert.equal(api.ccForm.value.enable_script_mcp, false)
  })

  it('模板存在 el-switch 绑定 enable_script_mcp（验收步骤「出现开关」）', () => {
    assert.ok(
      /<el-switch[^>]*v-model="ccForm\.enable_script_mcp"/.test(vueSrc),
      'CC 卡模板须有 <el-switch v-model="ccForm.enable_script_mcp">'
    )
    assert.ok(vueSrc.includes('label="脚本 MCP 工具"'), '表单项标签')
    assert.ok(vueSrc.includes('默认关闭'), '提示文案说明默认关闭')
  })

  it('loadCcConfig 回读后端布尔：true→开、false/缺省→关', async () => {
    const { api, ctx } = buildView()
    api.ccFormRef.value = { validate: async () => true, clearValidate() {} }

    ctx.__getConfigResponse = {
      configured: true,
      config: { name: 'CC', base_url: 'http://gw', auth_token: '****', model: 'm',
                timeout_seconds: 60, max_turns: 10, proxy_url: '', enable_script_mcp: true },
    }
    await api.loadCcConfig()
    assert.equal(api.ccForm.value.enable_script_mcp, true, '后端 true 应回读为开')
    assert.equal(api.ccConfigured.value, true)

    ctx.__getConfigResponse = {
      configured: true,
      config: { name: 'CC', base_url: 'http://gw', auth_token: '****', model: 'm',
                timeout_seconds: 60, max_turns: 10, proxy_url: '' }, // 旧数据无该键
    }
    await api.loadCcConfig()
    assert.equal(api.ccForm.value.enable_script_mcp, false, '缺省键按关处理')
  })
})

describe('D2 UI 保存不再静默关闭开关', () => {
  it('回归（验收实测链路）：API 置 true → UI 回读 → 点击保存，提交体仍携带 true', async () => {
    const { api, calls, ctx } = buildView()
    api.ccFormRef.value = { validate: async () => true, clearValidate() {} }
    ctx.__getConfigResponse = {
      configured: true,
      config: { name: 'CC', base_url: 'http://gw', auth_token: '****', model: 'm',
                timeout_seconds: 60, max_turns: 10, proxy_url: '', enable_script_mcp: true },
    }
    await api.loadCcConfig()
    await api.saveCcConfig()
    assert.equal(calls.update.length, 1)
    assert.equal(calls.update[0].enable_script_mcp, true, '保存提交体必须携带开启状态')
  })

  it('表单为关时提交体显式携带 false（键存在，而非缺省被后端落 False）', async () => {
    const { api, calls } = buildView()
    api.ccFormRef.value = { validate: async () => true, clearValidate() {} }
    api.ccForm.value = { ...api.getDefaultCcForm(), base_url: 'http://gw', model: 'm' }
    await api.saveCcConfig()
    assert.equal(calls.update.length, 1)
    assert.ok('enable_script_mcp' in calls.update[0], '提交体必须显式包含该键')
    assert.equal(calls.update[0].enable_script_mcp, false)
  })

  it('用户在开关上切换为开后保存，提交体为 true', async () => {
    const { api, calls } = buildView()
    api.ccFormRef.value = { validate: async () => true, clearValidate() {} }
    api.ccForm.value = { ...api.getDefaultCcForm(), base_url: 'http://gw', model: 'm' }
    api.ccForm.value.enable_script_mcp = true
    await api.saveCcConfig()
    assert.equal(calls.update[0].enable_script_mcp, true)
  })
})
