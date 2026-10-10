<template>
  <div class="ab-test-container">
    <!-- 顶部操作栏 -->
    <div class="ab-test-header">
      <div class="header-title">
        <el-icon :size="20"><Switch /></el-icon>
        <span>A/B 模型对比测试</span>
        <span class="subtitle">选择两个模型，输入变量，对比实时输出结果</span>
      </div>
      <el-button :icon="Clock" @click="openHistory">历史记录</el-button>
    </div>

    <!-- 模型选择区域 -->
    <el-card class="config-card" shadow="never">
      <el-row :gutter="24">
        <!-- 模型 A -->
        <el-col :span="12">
          <div class="model-selector">
            <div class="model-label model-a-label">
              <el-tag type="primary" size="large" class="model-tag">A</el-tag>
              <span>模型 A</span>
            </div>
            <el-select
              v-model="modelAId"
              placeholder="选择模型 A"
              filterable
              style="width: 100%"
              @change="onModelAChange"
            >
              <el-option
                v-for="m in modelList"
                :key="m.id"
                :label="m.name"
                :value="m.id"
              >
                <span>{{ m.name }}</span>
                <span class="model-opt-provider">{{ m.provider }}</span>
              </el-option>
            </el-select>
          </div>
        </el-col>

        <!-- 模型 B -->
        <el-col :span="12">
          <div class="model-selector">
            <div class="model-label model-b-label">
              <el-tag type="warning" size="large" class="model-tag">B</el-tag>
              <span>模型 B</span>
            </div>
            <el-select
              v-model="modelBId"
              placeholder="选择模型 B"
              filterable
              style="width: 100%"
              @change="onModelBChange"
            >
              <el-option
                v-for="m in modelList"
                :key="m.id"
                :label="m.name"
                :value="m.id"
              >
                <span>{{ m.name }}</span>
                <span class="model-opt-provider">{{ m.provider }}</span>
              </el-option>
            </el-select>
          </div>
        </el-col>
      </el-row>

      <!-- 共享变量区域 -->
      <div v-if="allVariables.length > 0 || (modelAId && modelBId)" class="variables-section">
        <el-divider content-position="left">
          <el-icon><Setting /></el-icon>
          共享变量配置
          <span class="var-tip">（变量将同时传入两个模型）</span>
        </el-divider>
        <el-form label-width="140px" size="default">
          <VariableInjector
            :variables="allVariables"
            :dataset-fields="[]"
            :initial-data="injectorInitialData"
            @update="onVariableUpdate"
          />
        </el-form>
      </div>

      <!-- 开始按钮 -->
      <div class="action-row">
        <el-button
          type="primary"
          :icon="VideoPlay"
          :disabled="!modelAId || !modelBId || isRunning"
          :loading="isRunning"
          size="large"
          @click="startABTest"
        >
          {{ isRunning ? '测试中...' : '开始 A/B 测试' }}
        </el-button>
        <el-button v-if="isRunning" :icon="VideoPause" @click="stopTest" size="large">停止</el-button>
        <span v-if="!modelAId || !modelBId" class="hint-text">请先选择模型 A 和模型 B</span>
      </div>
    </el-card>

    <!-- 对比结果区域 -->
    <el-row :gutter="16" class="result-row" v-if="testStarted">
      <!-- 模型 A 结果 -->
      <el-col :span="12">
        <el-card shadow="never" class="result-card result-card-a">
          <template #header>
            <div class="result-card-header">
              <el-tag type="primary">A</el-tag>
              <span class="model-name">{{ modelAName || '模型 A' }}</span>
              <div class="result-meta" v-if="durationA !== null">
                <el-tag :type="successA ? 'success' : 'danger'" size="small">
                  {{ successA ? '成功' : '失败' }}
                </el-tag>
                <span class="duration-text">{{ durationA }}s</span>
              </div>
              <el-icon v-else-if="loadingA" class="is-loading spin-icon"><Loading /></el-icon>
            </div>
          </template>
          <ModelTestLog :logs="logsA" :is-loading="loadingA" :height="500" />
        </el-card>
      </el-col>

      <!-- 模型 B 结果 -->
      <el-col :span="12">
        <el-card shadow="never" class="result-card result-card-b">
          <template #header>
            <div class="result-card-header">
              <el-tag type="warning">B</el-tag>
              <span class="model-name">{{ modelBName || '模型 B' }}</span>
              <div class="result-meta" v-if="durationB !== null">
                <el-tag :type="successB ? 'success' : 'danger'" size="small">
                  {{ successB ? '成功' : '失败' }}
                </el-tag>
                <span class="duration-text">{{ durationB }}s</span>
              </div>
              <el-icon v-else-if="loadingB" class="is-loading spin-icon"><Loading /></el-icon>
            </div>
          </template>
          <ModelTestLog :logs="logsB" :is-loading="loadingB" :height="500" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 保存记录按钮 -->
    <div class="save-row" v-if="testDone">
      <el-button type="success" :icon="DocumentAdd" :disabled="isRunning" @click="startSave">
        保存本次对比记录
      </el-button>
      <el-button @click="resetTest" :icon="RefreshRight">重新测试</el-button>
    </div>

    <!-- 保存记录命名弹窗 -->
    <el-dialog v-model="saveNameVisible" title="保存对比记录" width="480px" :close-on-click-modal="false">
      <el-form label-width="80px">
        <el-form-item label="记录名称">
          <el-input
            v-model="saveName"
            placeholder="选填，便于后续查找（留空则不设名称）"
            clearable
            maxlength="60"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="saveNameVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="confirmSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 历史记录弹窗 -->
    <el-dialog v-model="historyVisible" title="历史 A/B 测试记录" width="960px" top="8vh">
      <div class="history-toolbar">
        <el-input
          v-model="historyKeyword"
          placeholder="搜索记录名称..."
          clearable
          style="width: 240px"
          @keyup.enter="searchHistory"
          @clear="searchHistory"
        >
          <template #suffix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button :icon="Refresh" size="small" @click="searchHistory">刷新</el-button>
      </div>
      <el-table :data="historyList" v-loading="historyLoading" size="small" style="width: 100%">
        <el-table-column label="备注名" prop="name" min-width="120">
          <template #default="{ row }">{{ row.name || '-' }}</template>
        </el-table-column>
        <el-table-column label="模型 A" prop="model_a_name" min-width="140" />
        <el-table-column label="模型 B" prop="model_b_name" min-width="140" />
        <el-table-column label="A 耗时" width="80">
          <template #default="{ row }">
            <span v-if="row.duration_a != null">{{ row.duration_a }}s</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="B 耗时" width="80">
          <template #default="{ row }">
            <span v-if="row.duration_b != null">{{ row.duration_b }}s</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === 'completed' ? 'success' : 'danger'" size="small">
              {{ row.status === 'completed' ? '完成' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" prop="created_at" min-width="160">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="viewHistory(row.id)">查看</el-button>
            <el-button type="danger" link size="small" :loading="deletingHistoryId === row.id" :disabled="deletingHistoryId !== null" @click="deleteHistory(row.id)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="history-pagination">
        <el-pagination
          v-model:current-page="historyPage"
          :page-size="20"
          :total="historyTotal"
          layout="total, prev, pager, next"
          @current-change="loadHistory"
          small
        />
      </div>
    </el-dialog>

    <!-- 历史详情弹窗 -->
    <el-dialog v-model="historyDetailVisible" title="A/B 测试详情" width="1100px" top="4vh">
      <div v-if="historyDetail">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-card shadow="never" class="result-card result-card-a">
              <template #header>
                <div class="result-card-header">
                  <el-tag type="primary">A</el-tag>
                  <span class="model-name">{{ historyDetail.model_a_name }}</span>
                  <span class="duration-text" v-if="historyDetail.duration_a != null">
                    {{ historyDetail.duration_a }}s
                  </span>
                </div>
              </template>
              <ModelTestLog
                :logs="buildLogsFromResult(historyDetail.result_a)"
                :is-loading="false"
                :height="480"
              />
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="never" class="result-card result-card-b">
              <template #header>
                <div class="result-card-header">
                  <el-tag type="warning">B</el-tag>
                  <span class="model-name">{{ historyDetail.model_b_name }}</span>
                  <span class="duration-text" v-if="historyDetail.duration_b != null">
                    {{ historyDetail.duration_b }}s
                  </span>
                </div>
              </template>
              <ModelTestLog
                :logs="buildLogsFromResult(historyDetail.result_b)"
                :is-loading="false"
                :height="480"
              />
            </el-card>
          </el-col>
        </el-row>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Switch, Clock, Setting, VideoPlay, VideoPause, Loading,
  DocumentAdd, RefreshRight, Refresh, Search
} from '@element-plus/icons-vue'
import { modelApi, abTestApi } from '@/api'
import appConfig from '@/config'
import VariableInjector from '@/components/VariableInjector.vue'
import { formatTime } from '@/utils/time'
import ModelTestLog from '@/components/ModelTestLog.vue'

// ==================== 模型列表 ====================
const modelList = ref([])

const loadModels = async () => {
  try {
    const res = await modelApi.list({ limit: 200 })
    modelList.value = res || []
  } catch (e) {
    console.error('加载模型列表失败', e)
  }
}

// ==================== 模型选择 & 变量合并 ====================
const modelAId = ref('')
const modelBId = ref('')
const modelAName = computed(() => modelList.value.find(m => m.id === modelAId.value)?.name || '')
const modelBName = computed(() => modelList.value.find(m => m.id === modelBId.value)?.name || '')

const varsA = ref([])  // 模型A的变量列表
const varsB = ref([])  // 模型B的变量列表

// 合并后的变量（取并集）
const allVariables = computed(() => {
  const set = new Set([...varsA.value, ...varsB.value])
  return Array.from(set)
})

const injectorInitialData = ref({})
const currentVariables = ref({})  // VariableInjector 输出的 runtime_variables

const onVariableUpdate = (data) => {
  currentVariables.value = data.runtime_variables || {}
}

const onModelAChange = async (modelId) => {
  if (!modelId) { varsA.value = []; return }
  try {
    const res = await modelApi.getVariables(modelId)
    varsA.value = res.variables || []
  } catch (e) {
    varsA.value = []
  }
}

const onModelBChange = async (modelId) => {
  if (!modelId) { varsB.value = []; return }
  try {
    const res = await modelApi.getVariables(modelId)
    varsB.value = res.variables || []
  } catch (e) {
    varsB.value = []
  }
}

// ==================== A/B 测试执行 ====================
const isRunning = ref(false)
const testStarted = ref(false)
const testDone = ref(false)

const logsA = ref([])
const logsB = ref([])
const loadingA = ref(false)
const loadingB = ref(false)
const durationA = ref(null)
const durationB = ref(null)
const successA = ref(false)
const successB = ref(false)

// 保存执行结果（用于保存历史）
const resultA = ref(null)
const resultB = ref(null)

let ws = null

const startABTest = () => {
  if (!modelAId.value || !modelBId.value) return

  // 重置状态
  logsA.value = []
  logsB.value = []
  durationA.value = null
  durationB.value = null
  successA.value = false
  successB.value = false
  resultA.value = null
  resultB.value = null
  testStarted.value = true
  testDone.value = false
  isRunning.value = true
  loadingA.value = true
  loadingB.value = true

  const wsUrl = appConfig.getWebSocketUrl('/api/v1/ab-test/ws/run')
  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    ws.send(JSON.stringify({
      model_a_id: modelAId.value,
      model_b_id: modelBId.value,
      variables: currentVariables.value
    }))
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      const side = data.side

      if (side === 'A') {
        if (data.type !== 'stream_chunk') logsA.value.push(data)
        if (data.type === 'side_complete') {
          loadingA.value = false
          durationA.value = data.duration
          successA.value = data.success
        }
      } else if (side === 'B') {
        if (data.type !== 'stream_chunk') logsB.value.push(data)
        if (data.type === 'side_complete') {
          loadingB.value = false
          durationB.value = data.duration
          successB.value = data.success
        }
      }

      if (data.type === 'complete') {
        isRunning.value = false
        testDone.value = true
        // 整理保存数据
        resultA.value = buildResultFromLogs(logsA.value, durationA.value, successA.value)
        resultB.value = buildResultFromLogs(logsB.value, durationB.value, successB.value)
      }

      if (data.type === 'error' && !side) {
        // 全局错误
        ElMessage.error(data.content || '测试失败')
        isRunning.value = false
        loadingA.value = false
        loadingB.value = false
      }
    } catch (e) {
      console.error('解析 WebSocket 消息失败', e)
    }
  }

  ws.onerror = () => {
    ElMessage.error('WebSocket 连接错误')
    isRunning.value = false
    loadingA.value = false
    loadingB.value = false
  }

  ws.onclose = () => {
    if (isRunning.value) {
      isRunning.value = false
      loadingA.value = false
      loadingB.value = false
    }
  }
}

const stopTest = () => {
  if (ws) {
    ws.close()
    ws = null
  }
  isRunning.value = false
  loadingA.value = false
  loadingB.value = false
  testDone.value = true
}

const resetTest = () => {
  testStarted.value = false
  testDone.value = false
  logsA.value = []
  logsB.value = []
  durationA.value = null
  durationB.value = null
}

// ==================== 保存历史 ====================
const saving = ref(false)
const saveNameVisible = ref(false)
const saveName = ref('')

// 点击"保存本次对比记录"按钮 → 弹出命名对话框
const startSave = () => {
  saveName.value = `${modelAName.value} vs ${modelBName.value}`
  saveNameVisible.value = true
}

// 确认保存
const confirmSave = async () => {
  saving.value = true
  try {
    await abTestApi.saveSession({
      model_a_id: modelAId.value,
      model_b_id: modelBId.value,
      model_a_name: modelAName.value,
      model_b_name: modelBName.value,
      variables: currentVariables.value,
      result_a: resultA.value,
      result_b: resultB.value,
      status: (successA.value || successB.value) ? 'completed' : 'failed',
      duration_a: durationA.value,
      duration_b: durationB.value,
      name: saveName.value || null,
    })
    saveNameVisible.value = false
    ElMessage.success('记录已保存')
  } catch (e) {
    ElMessage.error('保存失败: ' + (e.message || '未知错误'))
  } finally {
    saving.value = false
  }
}

// ==================== 历史记录 ====================
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyList = ref([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyKeyword = ref('')
const historyDetailVisible = ref(false)
const historyDetail = ref(null)
const deletingHistoryId = ref(null)

const loadHistory = async () => {
  historyLoading.value = true
  try {
    const res = await abTestApi.listSessions({
      page: historyPage.value,
      page_size: 20,
      keyword: historyKeyword.value || undefined,
    })
    historyList.value = res.items || []
    historyTotal.value = res.total || 0
  } catch (e) {
    ElMessage.error('加载历史记录失败')
  } finally {
    historyLoading.value = false
  }
}

// 搜索（重置到第一页再加载）
const searchHistory = () => {
  historyPage.value = 1
  loadHistory()
}

const viewHistory = async (id) => {
  try {
    const res = await abTestApi.getSession(id)
    historyDetail.value = res
    historyDetailVisible.value = true
  } catch (e) {
    ElMessage.error('加载详情失败')
  }
}

const deleteHistory = async (id) => {
  try {
    await ElMessageBox.confirm('确认删除这条记录？', '提示', { type: 'warning' })
    if (deletingHistoryId.value !== null) return
    deletingHistoryId.value = id
    try {
      await abTestApi.deleteSession(id)
      ElMessage.success('删除成功')
      await loadHistory()
    } finally {
      deletingHistoryId.value = null
    }
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  }
}

// 打开历史弹窗时自动加载
const openHistory = () => {
  historyVisible.value = true
  historyKeyword.value = ''
  historyPage.value = 1
  loadHistory()
}

// ==================== 工具函数 ====================

const buildResultFromLogs = (logs, duration, success) => {
  const content = logs.find(l => l.type === 'response')?.content || ''
  const requestDetails = logs.filter(l => l.type === 'request_detail')
  return { success, content, request_details: requestDetails, duration }
}

const buildLogsFromResult = (result) => {
  if (!result) return []
  const logs = []
  if (result.request_details?.length) {
    result.request_details.forEach(d => logs.push({ ...d, type: 'request_detail' }))
  }
  if (result.success) {
    logs.push({ type: 'success', content: '连接成功' })
    if (result.content) logs.push({ type: 'response', content: result.content })
  } else {
    logs.push({ type: 'error', content: result.error || '执行失败' })
  }
  return logs
}

// ==================== 初始化 ====================
onMounted(() => {
  loadModels()
})
</script>

<style scoped>
.ab-test-container {
  min-height: 100%;
}

.ab-test-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.subtitle {
  font-size: 13px;
  font-weight: 400;
  color: var(--text-secondary);
}

.config-card {
  margin-bottom: 16px;
}

.model-selector {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.model-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
  font-size: 14px;
  color: var(--text-primary);
}

.model-tag {
  font-size: 14px;
  font-weight: 700;
  min-width: 28px;
  text-align: center;
}

.model-opt-provider {
  margin-left: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.variables-section {
  margin-top: 20px;
}

.var-tip {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary);
  margin-left: 6px;
}

.action-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--border-color);
}

.hint-text {
  font-size: 12px;
  color: #c0c4cc;
}

.result-row {
  margin-bottom: 16px;
}

.result-card {
  height: 100%;
}

.result-card-a :deep(.el-card__header) {
  border-left: 4px solid #409eff;
}

.result-card-b :deep(.el-card__header) {
  border-left: 4px solid #e6a23c;
}

.result-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.model-name {
  font-weight: 500;
  font-size: 14px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}

.duration-text {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: monospace;
}

.spin-icon {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.save-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 0;
  border-top: 1px solid var(--border-color);
}

.history-toolbar {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: flex-end;
}

.history-pagination {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}
</style>
