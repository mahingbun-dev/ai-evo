<template>
  <div class="home">
    <!-- 步骤引导区（P0-D：按已有统计动态显示完成态） -->
    <div class="steps-section">
      <div class="step-card" :class="{ done: stepDone(1), current: stepCurrent(1) }" @click="$router.push('/models')">
        <div class="step-number">
          <el-icon v-if="stepDone(1)"><CircleCheckFilled /></el-icon>
          <template v-else>1</template>
        </div>
        <div class="step-content">
          <div class="step-title">{{ stepDone(1) ? '模型已就绪' : '配置模型' }}</div>
          <div class="step-desc">{{ stepDone(1) ? '去查看 / 新增模型配置' : '选择并配置待评测的AI模型' }}</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
      <div class="step-card" :class="{ done: stepDone(2), current: stepCurrent(2) }" @click="$router.push('/datasets')">
        <div class="step-number">
          <el-icon v-if="stepDone(2)"><CircleCheckFilled /></el-icon>
          <template v-else>2</template>
        </div>
        <div class="step-content">
          <div class="step-title">{{ stepDone(2) ? '数据集已就绪' : '上传数据集' }}</div>
          <div class="step-desc">{{ stepDone(2) ? '去查看 / 上传数据集' : '上传测试数据或选择已有数据集' }}</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
      <div class="step-card" :class="{ done: stepDone(3), current: stepCurrent(3) }" @click="$router.push('/tasks')">
        <div class="step-number">
          <el-icon v-if="stepDone(3)"><CircleCheckFilled /></el-icon>
          <template v-else>3</template>
        </div>
        <div class="step-content">
          <div class="step-title">{{ stepDone(3) ? '任务已创建' : '创建任务' }}</div>
          <div class="step-desc">{{ stepDone(3) ? '去执行 / 查看任务' : '选择模板并创建评测任务' }}</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
      <div class="step-card" :class="{ done: stepDone(4), current: stepCurrent(4) }" @click="$router.push('/results')">
        <div class="step-number">
          <el-icon v-if="stepDone(4)"><CircleCheckFilled /></el-icon>
          <template v-else>4</template>
        </div>
        <div class="step-content">
          <div class="step-title">{{ stepDone(4) ? '已看到评测结果' : '查看结果' }}</div>
          <div class="step-desc">{{ stepDone(4) ? '去查看结果与分析' : '在结果页查看评分明细' }}</div>
        </div>
        <el-icon class="step-arrow"><ArrowRight /></el-icon>
      </div>
    </div>

    <!-- 一键体验（P0-D：空库或演示数据未加载时展示） -->
    <el-alert
      v-if="showDemoBanner"
      class="demo-banner"
      type="success"
      :closable="false"
      show-icon
    >
      <template #title>
        <span>第一次使用？加载示例数据并跑一次演示评测，10 秒看到第一个评测结果。</span>
        <el-button
          class="demo-btn"
          type="primary"
          size="small"
          :loading="demoLoading"
          @click="runDemo"
        >一键体验演示评测</el-button>
      </template>
    </el-alert>

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
        <template #empty>
          <div class="empty-tasks">
            <p>还没有评测任务——从上方步骤 ① 开始，或点「一键体验演示评测」直接看一次完整评测。</p>
          </div>
        </template>
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
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { modelApi, datasetApi, taskApi, demoApi } from '@/api'
import { Cpu, FolderOpened, List, CircleCheckFilled, ArrowRight } from '@element-plus/icons-vue'
import { formatTime } from '@/utils/time'

const router = useRouter()

const stats = ref({
  models: 0,
  datasets: 0,
  tasks: 0,
  completedTasks: 0
})

const recentTasks = ref([])

const demoLoading = ref(false)
// 空库（models=0）或从未跑完过任务时展示一键体验横幅（P0-D）
const showDemoBanner = computed(() => stats.value.models === 0 || stats.value.completedTasks === 0)

const stepDone = (n) => {
  if (n === 1) return stats.value.models > 0
  if (n === 2) return stats.value.datasets > 0
  if (n === 3) return stats.value.tasks > 0
  return stats.value.completedTasks > 0
}
const stepCurrent = (n) => !stepDone(n) && (n === 1 || stepDone(n - 1))

const runDemo = async () => {
  demoLoading.value = true
  try {
    const res = await demoApi.seedAndRun()
    ElMessage.success('演示评测已启动，正在跳转任务进度…')
    const taskId = res?.task_id || res?.data?.task_id
    if (taskId) {
      router.push('/tasks')
    } else {
      router.push('/tasks')
    }
  } catch (e) {
    const detail = e?.response?.data?.detail || e?.message || '演示数据加载失败'
    ElMessage.error(typeof detail === 'string' ? detail : '演示数据加载失败')
  } finally {
    demoLoading.value = false
  }
}

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

/* 一键体验横幅 */
.demo-banner {
  margin-bottom: 20px;
}
.demo-banner :deep(.el-alert__title) {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 14px;
}
.demo-btn {
  flex-shrink: 0;
}

.empty-tasks {
  padding: 12px 0;
  color: var(--aievo-text-3, #9CA3AF);
  font-size: 13px;
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

.step-card.done {
  background: linear-gradient(135deg, #F0FDF4 0%, #DCFCE7 100%);
  border-color: #86EFAC;
}
.step-card.done .step-number {
  background: #059669;
  color: #fff;
  border: none;
  font-size: 18px;
}
.step-card.done .step-title {
  color: #047857;
}
.step-card.current {
  border-color: var(--aievo-primary, #4F46E5);
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12);
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
