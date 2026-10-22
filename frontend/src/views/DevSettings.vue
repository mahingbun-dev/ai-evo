<template>
  <div class="dev-settings-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>开发者设置</span>
          <span class="header-hint">API Key 供 CLI / CI 调用平台接口；Webhook 在评测收尾与门禁失败时推送通知</span>
        </div>
      </template>

      <el-tabs v-model="activeTab">
        <!-- ==================== API Keys ==================== -->
        <el-tab-pane label="API Keys" name="apiKeys">
          <div class="tab-toolbar">
            <el-button type="primary" :icon="Plus" @click="openKeyDialog">创建 API Key</el-button>
            <span class="tab-tip">明文 Key 仅创建时展示一次；scope 决定 Key 可调用的接口范围</span>
          </div>
          <el-table :data="apiKeys" v-loading="keysLoading" style="width: 100%">
            <el-table-column prop="name" label="名称" min-width="140" />
            <el-table-column label="Key 前缀" width="150">
              <template #default="{ row }">
                <code class="key-prefix">{{ row.key_prefix }}…</code>
              </template>
            </el-table-column>
            <el-table-column label="Scopes" min-width="260">
              <template #default="{ row }">
                <el-tag v-for="s in (row.scopes || [])" :key="s" size="small" class="scope-tag">{{ scopeLabel(s) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近使用" width="165">
              <template #default="{ row }">{{ formatTime(row.last_used_at) || '从未使用' }}</template>
            </el-table-column>
            <el-table-column label="过期时间" width="165">
              <template #default="{ row }">{{ row.expires_at ? formatTime(row.expires_at) : '永不过期' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="keyStatus(row).type">{{ keyStatus(row).label }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="!row.revoked_at"
                  link
                  type="danger"
                  @click="revokeKey(row)"
                >吊销</el-button>
                <span v-else class="placeholder-text">-</span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ==================== Webhooks ==================== -->
        <el-tab-pane label="Webhooks" name="webhooks">
          <div class="tab-toolbar">
            <el-button type="primary" :icon="Plus" @click="openWebhookDialog">创建 Webhook</el-button>
            <span class="tab-tip">HMAC-SHA256 签名头 X-Aievo-Signature 校验；secret 仅创建时展示一次</span>
          </div>
          <el-table :data="webhooks" v-loading="webhooksLoading" style="width: 100%">
            <el-table-column prop="name" label="名称" min-width="130" />
            <el-table-column prop="url" label="接收端 URL" min-width="240" show-overflow-tooltip />
            <el-table-column label="订阅事件" min-width="240">
              <template #default="{ row }">
                <el-tag v-for="e in (row.events || [])" :key="e" size="small" class="scope-tag">{{ eventLabel(e) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.active ? 'success' : 'info'">{{ row.active ? '启用' : '停用' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openDeliveries(row)">送达历史</el-button>
                <el-button link type="danger" @click="removeWebhook(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 创建 API Key 弹窗 -->
    <el-dialog v-model="keyDialogVisible" title="创建 API Key" width="560px">
      <el-form :model="keyForm" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="keyForm.name" placeholder="如：CI 门禁 Key" maxlength="100" />
        </el-form-item>
        <el-form-item label="Scopes" required>
          <div class="scope-check-group">
            <el-checkbox-group v-model="keyForm.scopes">
              <el-checkbox v-for="opt in SCOPE_OPTIONS" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </el-checkbox>
            </el-checkbox-group>
            <div class="scope-hint">CLI 门禁链路至少需要「触发运行 + 读取任务」；数据集上传需「写数据集」</div>
          </div>
        </el-form-item>
        <el-form-item label="过期时间">
          <el-date-picker
            v-model="keyForm.expires_at"
            type="datetime"
            placeholder="留空 = 永不过期"
            style="width: 100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="keyCreating" @click="createKey">创建</el-button>
      </template>
    </el-dialog>

    <!-- 明文 Key 一次性展示弹窗 -->
    <el-dialog v-model="tokenDialogVisible" title="API Key 创建成功" width="620px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon title="完整 Key 仅此一次展示，关闭后无法再查看，请立即复制保存" />
      <div class="secret-block">
        <code>{{ createdToken }}</code>
        <el-button size="small" type="primary" @click="copySecret(createdToken)">复制</el-button>
      </div>
      <div class="usage-tip">CLI 使用：aievo config add &lt;平台地址&gt; &lt;上方完整 Key&gt;</div>
      <template #footer>
        <el-button type="primary" @click="tokenDialogVisible = false">我已保存</el-button>
      </template>
    </el-dialog>

    <!-- 创建 Webhook 弹窗 -->
    <el-dialog v-model="webhookDialogVisible" title="创建 Webhook" width="560px">
      <el-form :model="webhookForm" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="webhookForm.name" placeholder="如：发布流水线通知" maxlength="100" />
        </el-form-item>
        <el-form-item label="URL" required>
          <el-input v-model="webhookForm.url" placeholder="https://接收端地址（内网目标需服务端放行）" />
        </el-form-item>
        <el-form-item label="事件" required>
          <el-checkbox-group v-model="webhookForm.events">
            <div class="event-check-item" v-for="ev in EVENT_OPTIONS" :key="ev.value">
              <el-checkbox :value="ev.value">{{ ev.label }}</el-checkbox>
              <span class="event-desc">{{ ev.desc }}</span>
            </div>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="webhookDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="webhookCreating" @click="createWebhook">创建</el-button>
      </template>
    </el-dialog>

    <!-- Webhook secret 一次性展示弹窗 -->
    <el-dialog v-model="secretDialogVisible" title="Webhook 创建成功" width="620px" :close-on-click-modal="false">
      <el-alert type="warning" :closable="false" show-icon title="签名 secret 仅此一次展示，请配置到接收端用于校验 X-Aievo-Signature" />
      <div class="secret-block">
        <code>{{ createdSecret }}</code>
        <el-button size="small" type="primary" @click="copySecret(createdSecret)">复制</el-button>
      </div>
      <div class="usage-tip">校验方式：HMAC-SHA256(secret, "时间戳.请求体")，对照请求头 X-Aievo-Signature（±300 秒防重放）</div>
      <template #footer>
        <el-button type="primary" @click="secretDialogVisible = false">我已保存</el-button>
      </template>
    </el-dialog>

    <!-- 送达历史抽屉 -->
    <el-drawer v-model="deliveriesVisible" :title="`送达历史 - ${deliveriesEndpoint?.name || ''}`" size="60%">
      <el-table :data="deliveries" v-loading="deliveriesLoading" style="width: 100%">
        <el-table-column prop="event" label="事件" min-width="190">
          <template #default="{ row }">{{ eventLabel(row.event) || row.event }}</template>
        </el-table-column>
        <el-table-column label="送达状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="响应码" width="90">
          <template #default="{ row }">{{ row.response_status ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="attempts" label="尝试次数" width="90" />
        <el-table-column prop="last_error" label="最后错误" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_error || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="165">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!deliveriesLoading && deliveries.length === 0" description="暂无送达记录" />
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { apiKeyApi, webhookApi } from '@/api'

// scope 与事件常量（与后端 app/core/scopes.py、webhook_service.WEBHOOK_EVENTS 对齐）
const SCOPE_OPTIONS = [
  { value: 'tasks:run', label: '触发运行' },
  { value: 'tasks:read', label: '读取任务/门禁' },
  { value: 'results:read', label: '读取结果' },
  { value: 'datasets:read', label: '读数据集' },
  { value: 'datasets:write', label: '写数据集' },
  { value: 'webhooks:manage', label: '管理 Webhook' }
]
const EVENT_OPTIONS = [
  { value: 'task.execution.completed', label: '执行完成', desc: '任务评测完成时推送' },
  { value: 'task.execution.failed', label: '执行失败', desc: '任务评测失败收尾时推送' },
  { value: 'gate.failed', label: '门禁失败', desc: '门禁判定为 failed 时额外推送' }
]

const scopeLabel = (value) => SCOPE_OPTIONS.find(o => o.value === value)?.label || value
const eventLabel = (value) => EVENT_OPTIONS.find(o => o.value === value)?.label || value

const activeTab = ref('apiKeys')

// ---------- API Keys ----------
const apiKeys = ref([])
const keysLoading = ref(false)
const keyDialogVisible = ref(false)
const keyCreating = ref(false)
const keyForm = ref({ name: '', scopes: [], expires_at: null })
const tokenDialogVisible = ref(false)
const createdToken = ref('')

const loadKeys = async () => {
  keysLoading.value = true
  try {
    apiKeys.value = await apiKeyApi.list()
  } catch (e) {
    ElMessage.error(e.message || '加载 API Keys 失败')
  } finally {
    keysLoading.value = false
  }
}

const openKeyDialog = () => {
  keyForm.value = { name: '', scopes: ['tasks:run', 'tasks:read'], expires_at: null }
  keyDialogVisible.value = true
}

const createKey = async () => {
  if (!keyForm.value.name.trim()) {
    ElMessage.error('请填写 Key 名称')
    return
  }
  if (!keyForm.value.scopes.length) {
    ElMessage.error('请至少勾选一个 scope')
    return
  }
  keyCreating.value = true
  try {
    const created = await apiKeyApi.create({
      name: keyForm.value.name.trim(),
      scopes: keyForm.value.scopes,
      expires_at: keyForm.value.expires_at || null
    })
    keyDialogVisible.value = false
    createdToken.value = created.token
    tokenDialogVisible.value = true
    loadKeys()
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    keyCreating.value = false
  }
}

// Key 状态：已吊销 > 已过期 > 正常
const keyStatus = (row) => {
  if (row.revoked_at) return { label: '已吊销', type: 'info' }
  if (row.expires_at && new Date(row.expires_at) < new Date()) return { label: '已过期', type: 'warning' }
  return { label: '有效', type: 'success' }
}

const revokeKey = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定吊销「${row.name}」？使用该 Key 的 CLI / CI 调用将立即失效（不可恢复）`,
      '吊销 API Key',
      { type: 'warning', confirmButtonText: '吊销', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await apiKeyApi.revoke(row.id)
    ElMessage.success('已吊销')
    loadKeys()
  } catch (e) {
    ElMessage.error(e.message || '吊销失败')
  }
}

// ---------- Webhooks ----------
const webhooks = ref([])
const webhooksLoading = ref(false)
const webhookDialogVisible = ref(false)
const webhookCreating = ref(false)
const webhookForm = ref({ name: '', url: '', events: [] })
const secretDialogVisible = ref(false)
const createdSecret = ref('')
const deliveriesVisible = ref(false)
const deliveriesLoading = ref(false)
const deliveries = ref([])
const deliveriesEndpoint = ref(null)

const loadWebhooks = async () => {
  webhooksLoading.value = true
  try {
    webhooks.value = await webhookApi.list()
  } catch (e) {
    ElMessage.error(e.message || '加载 Webhooks 失败')
  } finally {
    webhooksLoading.value = false
  }
}

const openWebhookDialog = () => {
  webhookForm.value = { name: '', url: '', events: ['task.execution.completed'] }
  webhookDialogVisible.value = true
}

const createWebhook = async () => {
  if (!webhookForm.value.name.trim()) {
    ElMessage.error('请填写名称')
    return
  }
  if (!/^https?:\/\//.test(webhookForm.value.url.trim())) {
    ElMessage.error('URL 需以 http:// 或 https:// 开头')
    return
  }
  if (!webhookForm.value.events.length) {
    ElMessage.error('请至少订阅一个事件')
    return
  }
  webhookCreating.value = true
  try {
    const created = await webhookApi.create({
      name: webhookForm.value.name.trim(),
      url: webhookForm.value.url.trim(),
      events: webhookForm.value.events
    })
    webhookDialogVisible.value = false
    createdSecret.value = created.secret
    secretDialogVisible.value = true
    loadWebhooks()
  } catch (e) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    webhookCreating.value = false
  }
}

const removeWebhook = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定删除 webhook「${row.name}」？删除后不再推送，历史送达记录一并清除`,
      '删除 Webhook',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await webhookApi.remove(row.id)
    ElMessage.success('已删除')
    loadWebhooks()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

const openDeliveries = async (row) => {
  deliveriesEndpoint.value = row
  deliveriesVisible.value = true
  deliveriesLoading.value = true
  try {
    deliveries.value = await webhookApi.deliveries(row.id, { limit: 100 })
  } catch (e) {
    ElMessage.error(e.message || '加载送达历史失败')
  } finally {
    deliveriesLoading.value = false
  }
}

// ---------- 公共 ----------
const formatTime = (value) => {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const copySecret = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    // 剪贴板 API 不可用（如非 https 环境）：退化提示手动选择
    ElMessage.warning('复制失败，请手动选择文本复制')
  }
}

onMounted(() => {
  loadKeys()
  loadWebhooks()
})
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-hint {
  font-size: 12px;
  color: #909399;
  font-weight: normal;
}

.tab-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.tab-tip {
  font-size: 12px;
  color: #909399;
}

.key-prefix {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 12px;
  color: #606266;
  background: #F4F4F5;
  padding: 2px 6px;
  border-radius: 4px;
}

.scope-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}

.scope-check-group .el-checkbox {
  margin-right: 16px;
}

.scope-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
  line-height: 1.5;
}

.event-check-item {
  display: flex;
  align-items: center;
  height: 32px;
}

.event-desc {
  font-size: 12px;
  color: #909399;
}

.secret-block {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
  padding: 12px;
  background: #F4F4F5;
  border-radius: 8px;
}

.secret-block code {
  flex: 1;
  word-break: break-all;
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 13px;
  color: #1F2937;
}

.usage-tip {
  margin-top: 10px;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}

.placeholder-text {
  color: #C0C4CC;
}
</style>
