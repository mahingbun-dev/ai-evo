<template>
  <div class="home">
    <!-- 步骤引导区 -->
    <div class="steps-section">
      <div class="step-card" @click="$router.push('/models')">
        <div class="step-number">1</div>
        <div class="step-content">
          <div class="step-title">配置模型</div>
          <div class="step-desc">选择并配置待评测的AI模型</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
      <div class="step-card" @click="$router.push('/datasets')">
        <div class="step-number">2</div>
        <div class="step-content">
          <div class="step-title">上传数据集</div>
          <div class="step-desc">上传测试数据或选择已有数据集</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
      <div class="step-card" @click="$router.push('/tasks')">
        <div class="step-number">3</div>
        <div class="step-content">
          <div class="step-title">创建任务</div>
          <div class="step-desc">选择模板并创建评测任务</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
    </div>

    <!-- 统计卡片 -->
    <el-row :gutter="20" class="stats-row">
      <el-col :span="6">
        <el-card class="stat-card">
          <div class="stat-content">
            <div class="stat-icon" style="background: linear-gradient(135deg, #4F46E5 0%, #22D3EE 100%);">
              <el-icon :size="24"><Cpu /></el-icon>
            </div>
            <div class="stat-info">
              <div class="stat-value">{{ stats.models }}</div>
              <div class="stat-label">模型配置</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card">
          <div class="stat-content">
            <div class="stat-icon" style="background: linear-gradient(135deg, #10B981 0%, #34D399 100%);">
              <el-icon :size="24"><FolderOpened /></el-icon>
            </div>
            <div class="stat-info">
              <div class="stat-value">{{ stats.datasets }}</div>
              <div class="stat-label">数据集</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card">
          <div class="stat-content">
            <div class="stat-icon" style="background: linear-gradient(135deg, #F59E0B 0%, #FBBF24 100%);">
              <el-icon :size="24"><List /></el-icon>
            </div>
            <div class="stat-info">
              <div class="stat-value">{{ stats.tasks }}</div>
              <div class="stat-label">评测任务</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card">
          <div class="stat-content">
            <div class="stat-icon" style="background: linear-gradient(135deg, #EC4899 0%, #F472B6 100%);">
              <el-icon :size="24"><CircleCheckFilled /></el-icon>
            </div>
            <div class="stat-info">
              <div class="stat-value">{{ stats.completedTasks }}</div>
              <div class="stat-label">已完成</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 任务列表 -->
    <el-card class="tasks-card">
      <template #header>
        <div class="card-header">
          <span class="card-title">最近任务</span>
          <el-button type="primary" text @click="$router.push('/tasks')">查看全部 <el-icon><ArrowRight /></el-icon></el-button>
        </div>
      </template>
      <el-table :data="recentTasks" style="width: 100%" :header-cell-style="{ background: '#F9FAFB', color: '#374151', fontWeight: 500 }">
        <el-table-column prop="name" label="任务名称" min-width="200">
          <template #default="{ row }">
            <span class="task-name">{{ row.name || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="evaluation_type" label="评测类型" min-width="120" />
        <el-table-column prop="status" label="状态" min-width="100">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" effect="plain" size="small" round>
              {{ getStatusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { modelApi, datasetApi, taskApi } from '@/api'
import { Cpu, FolderOpened, List, CircleCheckFilled, ArrowRight } from '@element-plus/icons-vue'
import { formatTime } from '@/utils/time'

const stats = ref({
  models: 0,
  datasets: 0,
  tasks: 0,
  completedTasks: 0
})

const recentTasks = ref([])

const getStatusType = (status) => {
  const map = {
    pending: 'info',
    running: 'warning',
    completed: 'success',
    failed: 'danger',
    stopped: 'info'
  }
  return map[status] || 'info'
}

const getStatusText = (status) => {
  const map = {
    pending: '待开始',
    running: '进行中',
    completed: '已完成',
    failed: '失败',
    stopped: '已停止'
  }
  return map[status] || status
}

onMounted(async () => {
  try {
    const [modelsRes, datasetsRes, tasksRes] = await Promise.all([
      modelApi.list({ limit: 1000 }),
      datasetApi.list({ limit: 1000 }),
      taskApi.list({ limit: 100 })
    ])
    stats.value.models = modelsRes?.length || 0
    stats.value.datasets = datasetsRes?.length || 0
    stats.value.tasks = tasksRes?.length || 0
    stats.value.completedTasks = tasksRes?.filter(t => t.status === 'completed').length || 0
    recentTasks.value = tasksRes?.slice(0, 5) || []
  } catch (e) {
    console.error(e)
  }
})
</script>

<style scoped>
.home {
  max-width: 1400px;
  margin: 0 auto;
}

/* 步骤引导区 */
.steps-section {
  display: flex;
  gap: 16px;
  margin-bottom: 24px;
}

.step-card {
  flex: 1;
  background: linear-gradient(135deg, #EEF2FF 0%, #E0E7FF 100%);
  border: 1px solid #C7D2FE;
  border-radius: 12px;
  padding: 20px;
  display: flex;
  align-items: center;
  cursor: pointer;
  transition: all 0.2s ease;
}

.step-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(54, 98, 236, 0.15);
}

.step-number {
  width: 36px;
  height: 36px;
  background: #4F46E5;
  color: #fff;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 600;
  margin-right: 16px;
}

.step-content {
  flex: 1;
}

.step-title {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
  margin-bottom: 4px;
}

.step-desc {
  font-size: 13px;
  color: #6B7280;
}

.step-arrow {
  color: #4F46E5;
  font-size: 18px;
}

/* 统计卡片 */
.stats-row {
  margin-bottom: 24px;
}

.stat-card {
  border-radius: 12px;
}

.stat-card :deep(.el-card__body) {
  padding: 20px;
}

.stat-content {
  display: flex;
  align-items: center;
  gap: 16px;
}

.stat-icon {
  width: 52px;
  height: 52px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}

.stat-info {
  flex: 1;
}

.stat-value {
  font-size: 28px;
  font-weight: 600;
  color: #1F2937;
  line-height: 1.2;
}

.stat-label {
  font-size: 14px;
  color: #6B7280;
  margin-top: 4px;
}

/* 任务列表卡片 */
.tasks-card {
  border-radius: 12px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-title {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
}

.task-name {
  color: #4F46E5;
  font-weight: 500;
}

/* 表格样式 */
:deep(.el-table) {
  --el-table-border-color: #E5E7EB;
}

:deep(.el-table th) {
  font-weight: 500;
}

:deep(.el-table td) {
  padding: 14px 0;
}

:deep(.el-tag--success) {
  --el-tag-bg-color: #ECFDF5;
  --el-tag-border-color: #A7F3D0;
  --el-tag-text-color: #059669;
}

:deep(.el-tag--warning) {
  --el-tag-bg-color: #FFFBEB;
  --el-tag-border-color: #FDE68A;
  --el-tag-text-color: #D97706;
}

:deep(.el-tag--danger) {
  --el-tag-bg-color: #FEF2F2;
  --el-tag-border-color: #FECACA;
  --el-tag-text-color: #DC2626;
}

:deep(.el-tag--info) {
  --el-tag-bg-color: #F3F4F6;
  --el-tag-border-color: #E5E7EB;
  --el-tag-text-color: #6B7280;
}
</style>
