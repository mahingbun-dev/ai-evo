<template>
  <div class="statistics-container">
    <el-tabs v-model="activeTab" @tab-change="handleTabChange">
      <!-- 概览 Tab (existing content) -->
      <el-tab-pane label="概览" name="overview">
        <!-- 概览卡片区 -->
        <el-row :gutter="20" class="overview-cards">
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">任务总数</div>
                <div class="card-value">{{ summary.total_count || 0 }}</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">成功数</div>
                <div class="card-value success">{{ summary.success_count || 0 }}</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">成功率</div>
                <div class="card-value">{{ ((summary.success_rate || 0) * 100).toFixed(1) }}%</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">平均耗时</div>
                <div class="card-value">{{ formatDuration(summary.avg_duration_ms) }}</div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <!-- 视图切换 + 筛选器 -->
        <el-card class="filter-card">
          <div class="filter-header">
            <el-radio-group v-model="currentView" @change="handleViewChange">
              <el-radio-button label="trend">趋势图</el-radio-button>
              <el-radio-button label="distribution">分布图</el-radio-button>
              <el-radio-button label="detail">明细列表</el-radio-button>
            </el-radio-group>

            <div class="filters">
              <el-date-picker
                v-model="dateRange"
                type="daterange"
                range-separator="至"
                start-placeholder="开始日期"
                end-placeholder="结束日期"
                value-format="YYYY-MM-DD"
                style="width: 250%"
                @change="loadData"
              />
              <el-select v-model="filterForm.evaluation_type" placeholder="任务类型" clearable @change="loadData">
                <el-option label="全部" value="" />
                <el-option label="一致性" value="consistency" />
                <el-option label="成功率" value="success_rate" />
                <el-option label="响应耗时" value="latency" />
                <el-option label="裁判" value="judge" />
                <el-option label="二分分类" value="binary_classification" />
                <el-option label="RAG检索" value="rag" />
              </el-select>
              <el-select v-model="filterForm.status" placeholder="状态" clearable @change="loadData">
                <el-option label="全部" value="" />
                <el-option label="成功" value="success" />
                <el-option label="失败" value="failed" />
                <el-option label="运行中" value="running" />
                <el-option label="已停止" value="stopped" />
              </el-select>
              <el-button @click="resetFilters">重置</el-button>
            </div>
          </div>
        </el-card>

        <!-- 图表区 -->
        <el-card v-show="currentView !== 'detail'" class="chart-card">
          <div v-show="currentView === 'trend'" id="trendChart" style="width: 100%; height: 400px;"></div>
          <div v-show="currentView === 'distribution'" id="distributionChart" style="width: 100%; height: 400px;"></div>
        </el-card>

        <!-- 明细表格 -->
        <el-card v-show="currentView === 'detail'" class="table-card">
          <el-table :data="records" stripe>
            <el-table-column prop="task_name" label="任务名称" min-width="220" show-overflow-tooltip />
            <el-table-column prop="task_id" label="任务ID" min-width="220" />
            <el-table-column prop="evaluation_type" label="任务类型" min-width="120">
              <template #default="{ row }">
                {{ getTypeName(row.evaluation_type) }}
              </template>
            </el-table-column>
            <el-table-column prop="status" label="状态" min-width="100">
              <template #default="{ row }">
                <el-tag :type="getStatusType(row.status)">{{ getStatusName(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="duration_ms" label="耗时" min-width="120">
              <template #default="{ row }">
                {{ formatDuration(row.duration_ms) }}
              </template>
            </el-table-column>
            <el-table-column prop="started_at" label="开始时间" min-width="180">
              <template #default="{ row }">
                {{ formatDateTime(row.started_at) }}
              </template>
            </el-table-column>
            <el-table-column prop="finished_at" label="结束时间" min-width="180">
              <template #default="{ row }">
                {{ formatDateTime(row.finished_at) }}
              </template>
            </el-table-column>
          </el-table>

          <el-pagination
            v-model:current-page="pagination.page"
            v-model:page-size="pagination.pageSize"
            :total="pagination.total"
            :page-sizes="[10, 20, 50, 100]"
            layout="total, sizes, prev, pager, next"
            @size-change="loadRecords"
            @current-change="loadRecords"
          />
        </el-card>
      </el-tab-pane>

      <!-- 周覆盖度 Tab -->
      <el-tab-pane label="周覆盖度" name="coverage">
        <!-- 周配置 -->
        <el-card class="filter-card">
          <div style="display:flex; align-items:center; gap:16px; flex-wrap:wrap;">
            <span style="font-size:14px; color:#606266;">周配置：</span>
            <span style="font-size:12px; color:#909399;">起始日</span>
            <el-select v-model="coverageConfig.week_start_day" style="width:120px;" placeholder="周起始日">
              <el-option label="星期一" value="monday" />
              <el-option label="星期二" value="tuesday" />
              <el-option label="星期三" value="wednesday" />
              <el-option label="星期四" value="thursday" />
              <el-option label="星期五" value="friday" />
              <el-option label="星期六" value="saturday" />
              <el-option label="星期日" value="sunday" />
            </el-select>
            <span style="color:#c0c4cc;">~</span>
            <span style="font-size:12px; color:#909399;">结束日</span>
            <el-select v-model="coverageConfig.week_end_day" style="width:120px;" placeholder="周结束日">
              <el-option label="星期一" value="monday" />
              <el-option label="星期二" value="tuesday" />
              <el-option label="星期三" value="wednesday" />
              <el-option label="星期四" value="thursday" />
              <el-option label="星期五" value="friday" />
              <el-option label="星期六" value="saturday" />
              <el-option label="星期日" value="sunday" />
            </el-select>
            <el-button type="primary" size="small" :loading="configSaving" :disabled="planSubmitting" @click="saveCoverageConfig">保存</el-button>
          </div>
        </el-card>

        <!-- 计划录入 -->
        <el-card class="filter-card">
          <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span style="font-size:14px; color:#606266;">本周计划任务数：</span>
            <el-input-number v-model="newPlanCount" :min="1" style="width:160px;" />
            <el-button type="primary" size="small" :loading="planSubmitting" :disabled="planSubmitting || configSaving" @click="submitWeeklyPlan">录入</el-button>
            <span style="font-size:12px; color:#909399;">超出本周时间范围后将锁定不可修改</span>
          </div>
        </el-card>

        <!-- 覆盖率图表 -->
        <el-card class="chart-card">
          <div id="coverageChart" style="width:100%; height:350px;"></div>
        </el-card>

        <!-- 覆盖率明细表 -->
        <el-card class="table-card">
          <el-table :data="coverageList" stripe>
            <el-table-column label="周次" min-width="80">
              <template #default="{ row }">
                {{ getWeekLabel(row.week_start_date, row.week_end_date) }}
              </template>
            </el-table-column>
            <el-table-column prop="week_start_date" label="起始日期" min-width="110" />
            <el-table-column prop="week_end_date" label="结束日期" min-width="110" />
            <el-table-column prop="planned_count" label="计划任务数" min-width="100" align="right" />
            <el-table-column prop="actual_count" label="实际执行数" min-width="100" align="right" />
            <el-table-column label="覆盖率" min-width="100" align="right">
              <template #default="{ row }">
                <span :style="{ color: row.coverage_rate >= 80 ? '#67c23a' : row.coverage_rate >= 50 ? '#e6a23c' : '#f56c6c' }">
                  {{ row.coverage_rate }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="操作" min-width="100" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="editPlan(row)">编辑</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- 提效统计 Tab -->
      <el-tab-pane label="提效统计" name="efficiency">
        <!-- 概览卡片 -->
        <el-row :gutter="20" class="overview-cards">
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">总提效</div>
                <div class="card-value success">{{ totalEfficiency.total_efficiency_hours || 0 }}h</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">评测任务数</div>
                <div class="card-value">{{ totalEfficiency.task_count || 0 }}</div>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="card-content">
                <div class="card-label">平均单任务提效</div>
                <div class="card-value" style="color:#e6a23c;">{{ totalEfficiency.avg_efficiency_hours || 0 }}h</div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <!-- 时间范围筛选 -->
        <el-card class="filter-card">
          <div style="display:flex; align-items:center; gap:12px;">
            <span style="font-size:14px; color:#606266;">时间范围：</span>
            <el-date-picker
              v-model="efficiencyDateRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              value-format="YYYY-MM-DD"
              style="width: 300px;"
            />
            <el-button type="primary" @click="loadEfficiencySummary">查询</el-button>
          </div>
        </el-card>

        <!-- 提效明细 -->
        <el-card class="table-card">
          <el-table :data="efficiencyItems" stripe>
            <el-table-column prop="task_name" label="任务名称" min-width="200" />
            <el-table-column label="人工预计(h)" min-width="120" align="right">
              <template #default="{ row }">
                <span v-if="row.estimated_manual_hours !== null && row.estimated_manual_hours !== undefined">
                  {{ row.estimated_manual_hours }}
                </span>
                <span v-else style="color: #c0c4cc;">——</span>
              </template>
            </el-table-column>
            <el-table-column prop="system_hours" label="实际耗时(h)" min-width="120" align="right" />
            <el-table-column label="提效(h)" min-width="120" align="right">
              <template #default="{ row }">
                <span :style="{ color: row.efficiency_hours >= 0 ? '#67c23a' : '#f56c6c' }">
                  {{ row.efficiency_hours >= 0 ? '+' : '' }}{{ row.efficiency_hours }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { statsApi, coverageApi, efficiencyApi } from '@/api'
import * as echarts from 'echarts'

const activeTab = ref('overview')
const currentView = ref('trend')
const dateRange = ref([])
const summary = ref({})
const records = ref([])
const trends = ref([])
const trendChart = ref(null)
const distributionChart = ref(null)
const coverageChartInstance = ref(null)

const filterForm = reactive({
  evaluation_type: '',
  status: '',
})

const pagination = reactive({
  page: 1,
  pageSize: 20,
  total: 0,
})

// Coverage state
const coverageConfig = reactive({
  week_start_day: 'monday',
  week_end_day: 'sunday',
})
const newPlanCount = ref(50)
const coverageList = ref([])
const configSaving = ref(false)
const planSubmitting = ref(false)

// Efficiency state
const efficiencyDateRange = ref([])
const totalEfficiency = ref({})
const efficiencyItems = ref([])

const getParams = () => ({
  start_date: dateRange.value?.[0] || null,
  end_date: dateRange.value?.[1] || null,
  evaluation_type: filterForm.evaluation_type || null,
  status: filterForm.status || null,
})

const loadData = async () => {
  await Promise.all([loadSummary(), loadTrends(), loadRecords()])
  renderCharts()
}

const loadSummary = async () => {
  try {
    const res = await statsApi.getSummary({ ...getParams(), group_by: 'evaluation_type' })
    summary.value = res
  } catch (e) {
    ElMessage.error('加载统计概览失败')
  }
}

const loadTrends = async () => {
  try {
    const res = await statsApi.getTrends({ ...getParams(), granularity: 'day' })
    trends.value = res.data || []
  } catch (e) {
    ElMessage.error('加载趋势数据失败')
  }
}

const loadRecords = async () => {
  try {
    const res = await statsApi.getRecords({
      ...getParams(),
      page: pagination.page,
      page_size: pagination.pageSize,
    })
    records.value = res.items || []
    pagination.total = res.total || 0
  } catch (e) {
    ElMessage.error('加载任务记录失败')
  }
}

const resetFilters = () => {
  dateRange.value = []
  filterForm.evaluation_type = ''
  filterForm.status = ''
  loadData()
}

const handleViewChange = () => {
  nextTick(() => renderCharts())
}

const renderCharts = () => {
  if (currentView.value === 'trend') renderTrendChart()
  else if (currentView.value === 'distribution') renderDistributionChart()
}

const renderTrendChart = () => {
  const chartDom = document.getElementById('trendChart')
  if (!chartDom || !trends.value.length) return
  if (trendChart.value) trendChart.value.dispose()
  trendChart.value = echarts.init(chartDom)
  trendChart.value.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['任务数', '成功率', '平均耗时'] },
    grid: { left: 55, right: 200, bottom: 30 },
    xAxis: { type: 'category', data: trends.value.map(t => t.date) },
    yAxis: [
      {
        type: 'value',
        name: '任务数',
        position: 'left',
      },
      {
        type: 'value',
        name: '成功率',
        max: 1,
        position: 'right',
        axisLabel: { formatter: (v) => (v * 100).toFixed(0) + '%' },
        splitLine: { show: false },
      },
      {
        type: 'value',
        name: '耗时(ms)',
        position: 'right',
        offset: 65,
        splitLine: { show: false },
      },
    ],
    series: [
      { name: '任务数', type: 'bar', data: trends.value.map(t => t.count) },
      { name: '成功率', type: 'line', yAxisIndex: 1, data: trends.value.map(t => t.success_rate) },
      { name: '平均耗时', type: 'line', yAxisIndex: 2, data: trends.value.map(t => t.avg_duration_ms) },
    ],
  })
}

const renderDistributionChart = () => {
  const chartDom = document.getElementById('distributionChart')
  if (!chartDom || !summary.value.by_type) return
  if (distributionChart.value) distributionChart.value.dispose()
  distributionChart.value = echarts.init(chartDom)
  const data = Object.entries(summary.value.by_type).map(([type, val]) => ({
    name: getTypeName(type),
    value: val.count,
  }))
  distributionChart.value.setOption({
    tooltip: { trigger: 'item' },
    legend: { top: 'bottom' },
    series: [{ type: 'pie', radius: '60%', data }],
  })
}

// Coverage methods
const loadCoverageConfig = async () => {
  try {
    const res = await coverageApi.getConfig()
    coverageConfig.week_start_day = res.week_start_day
    coverageConfig.week_end_day = res.week_end_day
  } catch (e) {
    ElMessage.error('加载周配置失败')
  }
}

const saveCoverageConfig = async () => {
  if (configSaving.value) return
  configSaving.value = true
  try {
    await coverageApi.updateConfig({
      week_start_day: coverageConfig.week_start_day,
      week_end_day: coverageConfig.week_end_day,
    })
    ElMessage.success('周配置已保存')
    loadCoverageData()
  } catch (e) {
    ElMessage.error('保存周配置失败')
  } finally {
    configSaving.value = false
  }
}

const submitWeeklyPlan = async () => {
  if (planSubmitting.value) return
  planSubmitting.value = true
  try {
    const now = new Date()
    const weekStart = new Date(now)
    weekStart.setDate(now.getDate() - now.getDay() + 1)
    const weekEnd = new Date(weekStart)
    weekEnd.setDate(weekStart.getDate() + 6)

    await coverageApi.upsertPlan({
      week_start_date: weekStart.toISOString().slice(0, 10),
      week_end_date: weekEnd.toISOString().slice(0, 10),
      planned_count: newPlanCount.value,
    })
    ElMessage.success('本周计划已录入')
    loadCoverageData()
  } catch (e) {
    ElMessage.error('录入失败')
  } finally {
    planSubmitting.value = false
  }
}

const loadCoverageData = async () => {
  try {
    const res = await coverageApi.getCoverage({})
    coverageList.value = Array.isArray(res) ? res : []
    renderCoverageChart()
  } catch (e) {
    ElMessage.error('加载覆盖度数据失败')
  }
}

const editPlan = (row) => {
  newPlanCount.value = row.planned_count
  ElMessage.info('请在"本周计划任务数"输入框中修改数值后重新录入')
}

const getWeekLabel = (start, end) => {
  if (!start || !end) return ''
  const fmt = (d) => {
    const dt = new Date(d)
    return `${String(dt.getMonth() + 1).padStart(2, '0')}/${String(dt.getDate()).padStart(2, '0')}`
  }
  const s = new Date(start)
  const weekNum = Math.ceil((s - new Date(s.getFullYear(), 0, 1)) / 604800000)
  return `第${weekNum}周 (${fmt(start)}-${fmt(end)})`
}

// Efficiency methods
const loadEfficiencySummary = async () => {
  try {
    const params = {
      start_date: efficiencyDateRange.value?.[0] || null,
      end_date: efficiencyDateRange.value?.[1] || null,
    }
    const res = await efficiencyApi.getSummary(params)
    totalEfficiency.value = res
    efficiencyItems.value = res.items || []
  } catch (e) {
    ElMessage.error('加载提效统计失败')
  }
}

const renderCoverageChart = () => {
  const chartDom = document.getElementById('coverageChart')
  if (!chartDom || !coverageList.value.length) return
  if (coverageChartInstance.value) coverageChartInstance.value.dispose()
  coverageChartInstance.value = echarts.init(chartDom)
  coverageChartInstance.value.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['覆盖率', '计划数', '实际数'] },
    grid: { left: 55, right: 30, bottom: 30 },
    xAxis: {
      type: 'category',
      data: coverageList.value.map(c => getWeekLabel(c.week_start_date, c.week_end_date)),
    },
    yAxis: [
      { type: 'value', name: '覆盖率(%)', max: 100 },
      { type: 'value', name: '任务数' },
    ],
    series: [
      { name: '覆盖率', type: 'bar', data: coverageList.value.map(c => c.coverage_rate), yAxisIndex: 0 },
      { name: '计划数', type: 'line', data: coverageList.value.map(c => c.planned_count), yAxisIndex: 1 },
      { name: '实际数', type: 'line', data: coverageList.value.map(c => c.actual_count), yAxisIndex: 1 },
    ],
  })
}

const handleTabChange = (tabName) => {
  if (tabName === 'coverage') {
    nextTick(() => {
      loadCoverageConfig()
      loadCoverageData()
    })
  } else if (tabName === 'efficiency') {
    nextTick(() => loadEfficiencySummary())
  }
}

const formatDuration = (ms) => {
  if (!ms) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

const formatDateTime = (dt) => dt ? new Date(dt).toLocaleString() : '-'

const getTypeName = (type) => ({ consistency: '一致性', success_rate: '成功率', latency: '响应耗时', judge: '裁判', binary_classification: '二分分类', rag: 'RAG检索' }[type] || type)

const getStatusName = (s) => ({ success: '成功', failed: '失败', running: '运行中', stopped: '已停止', pending: '等待' }[s] || s)

const getStatusType = (s) => ({ success: 'success', failed: 'danger', running: 'warning', stopped: 'info', pending: '' }[s] || '')

onMounted(() => loadData())

onUnmounted(() => {
  if (trendChart.value) trendChart.value.dispose()
  if (distributionChart.value) distributionChart.value.dispose()
  if (coverageChartInstance.value) coverageChartInstance.value.dispose()
})
</script>

<style scoped>
.statistics-container { padding: 20px; }
.overview-cards { margin-bottom: 20px; }
.card-content { text-align: center; }
.card-label { font-size: 14px; color: #666; margin-bottom: 8px; }
.card-value { font-size: 28px; font-weight: bold; color: #333; }
.card-value.success { color: #67c23a; }
.filter-card { margin-bottom: 20px; }
.filter-header { display: flex; justify-content: space-between; align-items: center; }
.filters { display: flex; gap: 10px; align-items: center; }
.filters .el-select { width: 120px; flex-shrink: 0; }
.chart-card, .table-card { margin-bottom: 20px; }
</style>
