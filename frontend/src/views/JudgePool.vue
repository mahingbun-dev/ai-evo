<template>
  <div class="judge-pool">
    <!-- 降级 / 轮询失败共用一条横幅：失败只提示一次，页面保留最后一次成功数据 -->
    <el-alert
      v-if="bannerText"
      :type="bannerType"
      :closable="false"
      show-icon
      :title="bannerText"
      class="pool-banner"
    />

    <!-- 引擎状态卡片：engines 可能为 {}（Redis 不可用），空态兜底避免读 undefined -->
    <el-row v-if="engineCards.length" :gutter="16">
      <el-col v-for="card in engineCards" :key="card.key" :span="6" :xs="24" :sm="12" :md="6">
        <el-card shadow="hover">
          <div class="engine-title">{{ engineLabel(card.key) }}</div>
          <div class="engine-number">{{ card.active }} / {{ card.limit }}</div>
          <el-progress
            :percentage="percent(card)"
            :status="card.active >= card.limit ? 'exception' : undefined"
            :stroke-width="8"
          />
          <div class="engine-meta">
            排队 {{ card.waiting ?? 0 }} · 可用 {{ card.available ?? 0 }} · 近 1 分钟峰值 {{ card.peak_1m ?? 0 }}
          </div>
        </el-card>
      </el-col>
    </el-row>
    <el-card v-else shadow="never">
      <el-empty description="暂无引擎数据（Redis 不可用或尚未加载）" :image-size="80" />
    </el-card>

    <!-- 并发配置 -->
    <el-card class="pool-card">
      <template #header>
        <div class="card-header"><span>并发配置</span></div>
      </template>
      <el-form :inline="true" label-width="130px" v-loading="configLoading">
        <el-form-item v-for="field in limitFields" :key="field.key" :label="field.label">
          <el-input-number v-model="form[field.key]" :min="1" :max="128" />
        </el-form-item>
        <el-form-item label="租约有效期(秒)">
          <el-input-number v-model="form.lease_ttl_seconds" :min="30" :max="3600" />
        </el-form-item>
        <el-form-item label="心跳间隔(秒)">
          <el-input-number v-model="form.heartbeat_interval_seconds" :min="5" :max="600" />
        </el-form-item>
        <el-form-item label="等待超时(秒)">
          <el-input-number v-model="form.wait_timeout_seconds" :min="10" :max="7200" />
        </el-form-item>
        <el-form-item label="扫描间隔(秒)">
          <el-input-number v-model="form.scan_interval_seconds" :min="10" :max="600" />
        </el-form-item>
        <el-form-item label="审计保留(天)">
          <el-input-number v-model="form.audit_retention_days" :min="1" :max="365" />
        </el-form-item>
        <el-form-item label="Redis 故障放行">
          <el-switch v-model="form.fail_open" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" :disabled="!configLoaded" @click="save">保存</el-button>
          <el-button :disabled="saving" @click="loadConfig">重置</el-button>
        </el-form-item>
      </el-form>
      <div class="hint">保存后最多 5 秒全集群生效</div>
    </el-card>

    <!-- 实时占用 -->
    <el-card class="pool-card">
      <template #header>
        <div class="card-header">
          <span>实时占用（{{ leaseRows.length }} 条）</span>
          <div class="header-actions">
            <el-button size="small" @click="loadStatus">刷新</el-button>
            <el-button size="small" type="danger" :loading="releasingAll" @click="releaseAll">
              全部强制释放
            </el-button>
          </div>
        </div>
      </template>
      <el-table :data="leaseRows" size="small" empty-text="当前没有占用中的租约">
        <el-table-column label="引擎" width="140">
          <template #default="{ row }">{{ engineLabel(row.engine) }}</template>
        </el-table-column>
        <el-table-column label="任务" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.task_name || row.eval_task_id || '-' }}</template>
        </el-table-column>
        <el-table-column prop="row_idx" label="行号" width="80" />
        <el-table-column prop="host" label="主机" width="150" show-overflow-tooltip />
        <el-table-column prop="pid" label="进程" width="90" />
        <el-table-column label="剩余租约" width="110">
          <template #default="{ row }">{{ row.ttl_remaining_seconds ?? 0 }}s</template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button size="small" type="danger" @click="releaseOne(row)">强制释放</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 审计日志 -->
    <el-card class="pool-card">
      <template #header>
        <div class="card-header"><span>审计日志</span></div>
      </template>
      <el-form :inline="true">
        <el-form-item label="引擎">
          <el-select v-model="auditQuery.engine" clearable style="width: 160px" @change="handleAuditSearch">
            <el-option v-for="key in engineKeys" :key="key" :label="engineLabel(key)" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item label="事件">
          <el-select v-model="auditQuery.event" clearable style="width: 180px" @change="handleAuditSearch">
            <el-option v-for="e in auditEvents" :key="e" :label="e" :value="e" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button :loading="auditLoading" @click="handleAuditSearch">查询</el-button>
        </el-form-item>
      </el-form>
      <el-table :data="audit.items" size="small" v-loading="auditLoading" empty-text="暂无审计记录">
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) || row.created_at }}</template>
        </el-table-column>
        <el-table-column prop="event" label="事件" width="160" />
        <el-table-column label="引擎" width="140">
          <template #default="{ row }">{{ engineLabel(row.engine) }}</template>
        </el-table-column>
        <el-table-column prop="eval_task_id" label="任务ID" min-width="160" show-overflow-tooltip />
        <el-table-column prop="row_idx" label="行号" width="70" />
        <el-table-column prop="wait_ms" label="等待ms" width="90" />
        <el-table-column prop="hold_ms" label="占用ms" width="90" />
      </el-table>
      <div class="pagination-container">
        <el-pagination
          v-model:current-page="auditQuery.page"
          :page-size="auditQuery.page_size"
          :total="audit.total"
          layout="prev, pager, next, total"
          @current-change="loadAudit"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { judgePoolApi } from '@/api/judgePool'
import { validatePoolConfig } from '@/utils/judgePoolConfig'
import { formatTime } from '@/utils/time'

const POLL_INTERVAL_MS = 3000

const engineKeys = ['graph', 'claude_agent', 'harness', 'plugin']
const engineNames = {
  graph: '通用 LLM',
  claude_agent: 'Claude Code Agent',
  harness: 'DeepSeek Harness',
  plugin: '插件评测'
}
const engineLabel = (key) => engineNames[key] || key || '-'
const limitFields = [
  { key: 'graph_limit', label: '通用 LLM 上限' },
  { key: 'claude_agent_limit', label: 'CC Agent 上限' },
  { key: 'harness_limit', label: 'Harness 上限' },
  { key: 'plugin_limit', label: '插件评测上限' }
]
// 与后端 app/api/judge_pool_api.py::_AUDIT_EVENTS 保持一致（非法值后端返回 400）
const auditEvents = [
  'acquire',
  'release',
  'wait_timeout',
  'lease_expired',
  'lease_lost',
  'owner_thread_dead',
  'manual_release',
  'redis_unavailable'
]

const status = reactive({ redis_available: true, degraded: false, engines: {}, leases: [], error: '' })
const form = reactive({})
const saving = ref(false)
const configLoading = ref(false)
// 配置是否成功加载过：加载失败时 form 为空，不能拿空表单去 PUT（后端会合并现值返回 200，
// 页面却弹出「已保存」，实际什么都没改）
const configLoaded = ref(false)
const releasingAll = ref(false)
const audit = reactive({ total: 0, items: [] })
const auditQuery = reactive({ engine: '', event: '', page: 1, page_size: 20 })
const auditLoading = ref(false)

// 轮询失败提示（成功一次即清空）；与后端 degraded 横幅分开记录，避免互相覆盖
const pollError = ref('')
let timer = null
let polling = false

const bannerText = computed(() => {
  if (pollError.value) {
    return `状态刷新失败：${pollError.value}。页面保留最后一次成功数据，将自动重试`
  }
  if (status.degraded) {
    const detail = status.error ? `（${status.error}）` : ''
    return `Redis 不可用，并发池已降级为放行模式${detail}`
  }
  return ''
})
const bannerType = computed(() => (pollError.value ? 'error' : 'warning'))

const engineCards = computed(() =>
  engineKeys
    .filter((key) => status.engines && status.engines[key])
    .map((key) => ({ key, ...status.engines[key] }))
)

// 还没拿到 status 时 leases 可能是 undefined，模板统一用这个数组
const leaseRows = computed(() => (Array.isArray(status.leases) ? status.leases : []))

const percent = (item) => {
  const limit = Number(item.limit) || 0
  if (!limit) return 0
  const active = Number(item.active) || 0
  return Math.min(100, Math.round((active / limit) * 100))
}

const errorText = (e, fallback) => e?.message || fallback

/**
 * 拉取状态。轮询调用：任何失败都内部消化（横幅提示一次），不向定时器抛错，
 * 否则每 3 秒产生一次 unhandled rejection。
 */
async function loadStatus() {
  if (polling || document.hidden) return
  polling = true
  try {
    const data = await judgePoolApi.getStatus()
    Object.assign(status, data, { leases: data?.leases || [] })
    pollError.value = ''
  } catch (e) {
    pollError.value = errorText(e, '无法连接后端')
  } finally {
    polling = false
  }
}

async function loadConfig() {
  configLoading.value = true
  try {
    const data = await judgePoolApi.getConfig()
    Object.keys(form).forEach((k) => delete form[k])
    Object.assign(form, data)
    configLoaded.value = true
  } catch (e) {
    // 之前成功加载过时 form 仍持有真实值，保持可保存；只有空表单才判定为「未加载」
    if (!Object.keys(form).length) configLoaded.value = false
    ElMessage.error(errorText(e, '加载并发配置失败'))
  } finally {
    configLoading.value = false
  }
}

async function save() {
  if (!configLoaded.value) {
    ElMessage.warning('配置未加载，请先刷新')
    return
  }
  const error = validatePoolConfig(form)
  if (error) {
    ElMessage.error(error)
    return
  }
  saving.value = true
  try {
    const data = await judgePoolApi.updateConfig({ ...form })
    const { redis_synced: redisSynced, ...cfg } = data || {}
    Object.keys(form).forEach((k) => delete form[k])
    Object.assign(form, cfg)
    if (redisSynced === false) {
      // 已落库但 Redis 未同步：不是成功提示，恢复后才会生效
      ElMessage.warning('已落库，Redis 同步失败，恢复后生效')
    } else {
      ElMessage.success('已保存，最多 5 秒全集群生效')
    }
    await loadStatus()
  } catch (e) {
    ElMessage.error(errorText(e, '保存失败'))
  } finally {
    saving.value = false
  }
}

async function releaseOne(row) {
  try {
    await ElMessageBox.confirm(`确认强制释放 ${engineLabel(row.engine)} 的该条占用？`, '提示', {
      type: 'warning'
    })
  } catch {
    return // 用户取消
  }
  try {
    const res = await judgePoolApi.releaseLease(row.lease_id, row.engine)
    if (res && res.released) {
      ElMessage.success('已释放')
    } else {
      ElMessage.warning('租约已不存在，无需释放')
    }
    await loadStatus()
  } catch (e) {
    // 503（Redis 不可用）等失败必须报错，不能显示成「已释放」
    ElMessage.error(errorText(e, '强制释放失败'))
  }
}

async function releaseAll() {
  try {
    await ElMessageBox.confirm('确认强制释放全部引擎占用？', '提示', { type: 'warning' })
  } catch {
    return // 用户取消
  }
  releasingAll.value = true
  try {
    const res = await judgePoolApi.releaseAll()
    const released = Number(res?.released) || 0
    if (released > 0) {
      ElMessage.success(`已释放 ${released} 条`)
    } else {
      ElMessage.warning('当前没有可释放的租约')
    }
    await loadStatus()
  } catch (e) {
    // Redis 不可用时后端 503：明确报错，而不是「已释放 0 条」
    ElMessage.error(errorText(e, '强制释放失败'))
  } finally {
    releasingAll.value = false
  }
}

async function loadAudit() {
  auditLoading.value = true
  try {
    const params = { page: auditQuery.page, page_size: auditQuery.page_size }
    if (auditQuery.engine) params.engine = auditQuery.engine
    if (auditQuery.event) params.event = auditQuery.event
    const data = await judgePoolApi.getAudit(params)
    audit.items = data?.items || []
    audit.total = data?.total || 0
  } catch (e) {
    ElMessage.error(errorText(e, '加载审计日志失败'))
  } finally {
    auditLoading.value = false
  }
}

function handleAuditSearch() {
  auditQuery.page = 1
  loadAudit()
}

// 后台标签页里定时器会被浏览器节流（切回来可能要等 1 分钟），监控页不能显示过期数据：
// 回到前台立即补一次拉取
function handleVisibilityChange() {
  if (document.visibilityState === 'visible') loadStatus()
}

onMounted(async () => {
  // 三个加载函数内部都消化了异常，Promise.all 不会因单个失败整体 reject
  await Promise.all([loadStatus(), loadConfig(), loadAudit()])
  timer = setInterval(loadStatus, POLL_INTERVAL_MS)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<style scoped>
.judge-pool {
  max-width: 1600px;
  margin: 0 auto;
}

.pool-banner {
  margin-bottom: 16px;
}

.pool-card {
  margin-top: 16px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header span {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.engine-title {
  color: #606266;
  font-size: 13px;
}

.engine-number {
  font-size: 28px;
  font-weight: 600;
  margin: 6px 0;
}

.engine-meta {
  color: #909399;
  font-size: 12px;
  margin-top: 6px;
}

.hint {
  color: #909399;
  font-size: 12px;
}

.pagination-container {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
