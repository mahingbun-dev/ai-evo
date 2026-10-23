<template>
  <div class="audit-logs-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>审计日志</span>
        </div>
      </template>

      <!-- 筛选条件 -->
      <div class="filter-container">
        <el-form :inline="true" :model="filters">
          <el-form-item label="操作人">
            <el-input v-model="filters.user_code" placeholder="请输入用户代码" clearable style="width: 150px;" />
          </el-form-item>
          <el-form-item label="操作类型">
            <el-select v-model="filters.action" placeholder="请选择" clearable style="width: 120px;">
              <el-option label="创建" value="CREATE" />
              <el-option label="更新" value="UPDATE" />
              <el-option label="删除" value="DELETE" />
              <el-option label="导出" value="EXPORT" />
            </el-select>
          </el-form-item>
            <el-form-item label="资源类型">
            <el-select v-model="filters.resource_type" placeholder="请选择" clearable style="width: 120px;">
              <el-option label="评测任务" value="task" />
              <el-option label="数据集" value="dataset" />
              <el-option label="模型配置" value="model" />
              <el-option label="评测模板" value="template" />
              <el-option label="媒体资源" value="media" />
              <el-option label="API Key" value="api_key" />
              <el-option label="Webhook" value="webhook" />
              <el-option label="A/B 记录" value="ab_session" />
              <el-option label="脚本" value="pre_judge_script" />
            </el-select>
          </el-form-item>
          <el-form-item label="时间范围">
            <el-date-picker
              v-model="dateRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              value-format="YYYY-MM-DD"
              style="width: 240px;"
            />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="handleSearch">查询</el-button>
            <el-button @click="handleReset">重置</el-button>
            <!-- M6 QE6-2：审计导出=企业功能位 audit_export，未授权禁用+锁标 -->
            <el-tooltip :disabled="auditExportEnabled" :content="auditExportLockTip" placement="top">
              <el-dropdown style="margin-left: 12px;" @command="handleExport">
                <el-button :loading="exporting" :disabled="!auditExportEnabled">
                  导出<el-icon style="margin-left: 4px;"><arrow-down /></el-icon>
                  <el-icon v-if="!auditExportEnabled" style="margin-left: 2px;"><Lock /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="csv">CSV</el-dropdown-item>
                    <el-dropdown-item command="xlsx">Excel (xlsx)</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </el-tooltip>
          </el-form-item>
        </el-form>
      </div>

      <el-table :data="logs" style="width: 100%" v-loading="loading">
        <el-table-column label="操作时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column prop="user_name" label="操作人" min-width="100">
          <template #default="{ row }">
            {{ row.user_name || row.user_code }}
          </template>
        </el-table-column>
        <el-table-column prop="team_code" label="团队" min-width="100" />
        <el-table-column prop="action" label="操作类型" min-width="80">
          <template #default="{ row }">
            <el-tag :type="getActionType(row.action)">
              {{ getActionText(row.action) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="resource_type" label="资源类型" min-width="100">
          <template #default="{ row }">
            {{ getResourceTypeText(row.resource_type) }}
          </template>
        </el-table-column>
        <el-table-column prop="resource_name" label="资源名称" min-width="150" show-overflow-tooltip />
        <el-table-column prop="resource_id" label="资源ID" min-width="120" show-overflow-tooltip />
        <el-table-column prop="ip_address" label="IP地址" min-width="100" />
        <el-table-column label="操作" min-width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="viewDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-container">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </el-card>

    <!-- 详情对话框 -->
    <el-dialog v-model="detailDialogVisible" title="操作详情" width="600px">
      <el-descriptions :column="1" border v-if="currentLog">
        <el-descriptions-item label="操作时间">{{ formatTime(currentLog.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="操作人">{{ currentLog.user_name || currentLog.user_code }}</el-descriptions-item>
        <el-descriptions-item label="团队">{{ currentLog.team_code }}</el-descriptions-item>
        <el-descriptions-item label="操作类型">
          <el-tag :type="getActionType(currentLog.action)">
            {{ getActionText(currentLog.action) }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="资源类型">{{ getResourceTypeText(currentLog.resource_type) }}</el-descriptions-item>
        <el-descriptions-item label="资源名称">{{ currentLog.resource_name }}</el-descriptions-item>
        <el-descriptions-item label="资源ID">{{ currentLog.resource_id }}</el-descriptions-item>
        <el-descriptions-item label="IP地址">{{ currentLog.ip_address }}</el-descriptions-item>
        <el-descriptions-item label="变更详情">
          <pre class="detail-json">{{ JSON.stringify(currentLog.detail, null, 2) }}</pre>
        </el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="detailDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, Lock } from '@element-plus/icons-vue'
import { auditApi } from '@/api'
import { formatTime } from '@/utils/time'
import { hasEntitlement, entitlementLockTip } from '@/utils/permissions'

// M6 QE6-2：license 授权信息（App 根组件注入）——审计导出企业功能位锁标
const licenseInfo = inject('licenseInfo', null)
const auditExportEnabled = computed(() => hasEntitlement(licenseInfo?.value, 'audit_export'))
const auditExportLockTip = computed(() => entitlementLockTip(licenseInfo?.value, 'audit_export'))

const logs = ref([])
const loading = ref(false)
const exporting = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)

const filters = ref({
  user_code: '',
  action: '',
  resource_type: ''
})

const dateRange = ref([])

// 详情对话框
const detailDialogVisible = ref(false)
const currentLog = ref(null)

const getActionType = (action) => {
  const map = {
    'CREATE': 'success',
    'UPDATE': 'warning',
    'DELETE': 'danger',
    'EXPORT': 'info'
  }
  return map[action] || 'info'
}

const getActionText = (action) => {
  const map = {
    'CREATE': '创建',
    'UPDATE': '更新',
    'DELETE': '删除',
    'EXPORT': '导出'
  }
  return map[action] || action
}

const getResourceTypeText = (type) => {
  const map = {
    'task': '评测任务',
    'dataset': '数据集',
    'model': '模型配置',
    'template': '评测模板',
    'media': '媒体资源'
  }
  return map[type] || type
}

const loadLogs = async () => {
  loading.value = true
  try {
    const params = {
      skip: (currentPage.value - 1) * pageSize.value,
      limit: pageSize.value,
      ...filters.value
    }
    if (dateRange.value && dateRange.value.length === 2) {
      params.start_date = dateRange.value[0]
      params.end_date = dateRange.value[1]
    }
    const res = await auditApi.list(params)
    logs.value = res.items || []
    total.value = res.total || 0
  } catch (e) {
    ElMessage.error('加载审计日志失败')
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  currentPage.value = 1
  loadLogs()
}

const handleReset = () => {
  filters.value = {
    user_code: '',
    action: '',
    resource_type: ''
  }
  dateRange.value = []
  currentPage.value = 1
  loadLogs()
}

const handleSizeChange = (val) => {
  pageSize.value = val
  loadLogs()
}

const handleCurrentChange = (val) => {
  currentPage.value = val
  loadLogs()
}

const viewDetail = (row) => {
  currentLog.value = row
  detailDialogVisible.value = true
}

// QE6-2：审计导出（企业功能位 audit_export）：license 授权后可用，未授权
// 403+upgrade_required（按钮已锁，此处兜底网络态）；导出按当前筛选条件收窄
const handleExport = async (format) => {
  if (!auditExportEnabled.value) {
    ElMessage.warning(auditExportLockTip.value)
    return
  }
  exporting.value = true
  try {
    const params = { format, ...filters.value }
    if (dateRange.value && dateRange.value.length === 2) {
      params.start_date = dateRange.value[0]
      params.end_date = dateRange.value[1]
    }
    const blob = await auditApi.export(params)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `audit_logs_${Date.now()}.${format}`
    link.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    // 403=企业功能位未授权（detail 为 {code,message} 对象）；404=紧急停止闸；
    // 400=超行数上限；blob 响应需先解包
    let detail = e?.response?.data?.detail
    if (!detail && e?.response?.data instanceof Blob) {
      try {
        detail = JSON.parse(await e.response.data.text())?.detail
      } catch { /* 保底走默认提示 */ }
    }
    if (detail && typeof detail === 'object') detail = detail.message
    ElMessage.error(detail || '导出审计日志失败')
  } finally {
    exporting.value = false
  }
}

onMounted(() => {
  loadLogs()
})
</script>

<style scoped>
.audit-logs-page {
  max-width: 1600px;
  margin: 0 auto;
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

.filter-container {
  margin-bottom: 20px;
  padding: 16px;
  background: #F9FAFB;
  border-radius: 8px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

.detail-json {
  background: #f5f5f5;
  padding: 12px;
  border-radius: 4px;
  font-size: 12px;
  max-height: 200px;
  overflow: auto;
  margin: 0;
}

:deep(.el-table) {
  --el-table-border-color: #E5E7EB;
}

:deep(.el-table th) {
  background: #F9FAFB;
  color: #374151;
  font-weight: 500;
}

:deep(.el-dialog) {
  border-radius: 12px;
}
</style>
