<template>
  <div class="share-report-page">
    <!-- 顶部：只读标识 + 报告头（无任何登录/操作入口） -->
    <el-card shadow="never" class="header-card">
      <div class="header-row">
        <div class="header-title">
          <span class="task-name">{{ report?.task?.name || '评测报告' }}</span>
          <el-tag v-if="report?.task?.evaluation_type" size="small">{{ report.task.evaluation_type }}</el-tag>
          <el-tag v-if="report?.task?.status" size="small" :type="report.task.status === 'completed' ? 'success' : 'info'">
            {{ report.task.status }}
          </el-tag>
        </div>
        <div class="header-meta">
          <span v-if="report?.execution_number != null" class="meta-item">第 {{ report.execution_number }} 次执行</span>
          <span v-if="report?.completed_at" class="meta-item">{{ formatTime(report.completed_at) }}</span>
          <el-tag type="warning" size="small" effect="plain">只读分享</el-tag>
        </div>
      </div>
      <div v-if="report?.expires_at" class="expires-tip">链接有效期至 {{ formatTime(report.expires_at) }}</div>
    </el-card>

    <!-- 加载/失效态 -->
    <el-card v-if="loading" shadow="never" class="state-card">
      <div v-loading="loading" style="height: 120px;"></div>
    </el-card>
    <el-card v-else-if="notFound" shadow="never" class="state-card">
      <el-empty description="报告不存在或已失效（链接可能已过期或被吊销）" />
    </el-card>

    <template v-else-if="report">
      <!-- 门禁判定横幅（复用结果页口径；仅汇总展示） -->
      <el-card v-if="report.gate_result?.verdict" shadow="never" class="gate-banner" :class="`gate-${report.gate_result.verdict}`">
        <div class="gate-line">
          <span class="gate-label">
            门禁判定：{{ gateVerdictLabel(report.gate_result.verdict) }}
          </span>
          <span v-if="report.gate_result.evaluated_at" class="gate-time">{{ formatTime(report.gate_result.evaluated_at) }}</span>
        </div>
        <el-table v-if="(report.gate_result.checks || []).length" :data="report.gate_result.checks" size="small" class="gate-checks">
          <el-table-column prop="metric" label="指标" min-width="140" />
          <el-table-column prop="op" label="比较" width="70" />
          <el-table-column prop="value" label="阈值" width="90" align="right" />
          <el-table-column prop="actual" label="实际值" min-width="110" />
          <el-table-column label="结果" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="row.passed ? 'success' : 'danger'">{{ row.passed ? '通过' : '未过' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 汇总统计卡（复用结果页汇总口径：总数/成功/失败/均分） -->
      <el-card shadow="never" class="summary-card">
        <el-row :gutter="20">
          <el-col :span="6">
            <div class="stat-card-small">
              <div class="stat-label">总题目数</div>
              <div class="stat-value">{{ agg.total || agg.total_questions || '-' }}</div>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="stat-card-small">
              <div class="stat-label">成功评测</div>
              <div class="stat-value success">{{ agg.success_count ?? '-' }}</div>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="stat-card-small">
              <div class="stat-label">失败数量</div>
              <div class="stat-value danger">{{ agg.failed_count ?? '-' }}</div>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="stat-card-small">
              <div class="stat-label">平均分</div>
              <div class="stat-value highlight">{{ agg.average_score ?? '-' }}</div>
            </div>
          </el-col>
        </el-row>
      </el-card>

      <!-- 维度评分雷达图（echarts，只读渲染） -->
      <el-card v-if="dimensionEntries.length" shadow="never" class="chart-card">
        <template #header>各维度评分</template>
        <div ref="radarRef" style="width: 100%; height: 320px;"></div>
      </el-card>

      <!-- 仅汇总提示 / 明细行表（row_scope 决定） -->
      <el-card v-if="report.row_scope === 'summary'" shadow="never" class="state-card">
        <el-empty description="该分享链接仅开放汇总指标（不含行级明细）" :image-size="80" />
      </el-card>
      <el-card v-else-if="rows.length" shadow="never" class="state-card">
        <template #header>
          明细结果（{{ rows.length }} 行）
        </template>
        <el-table :data="rows" size="small" stripe>
          <el-table-column type="index" label="#" width="50" />
          <el-table-column prop="user_input" label="输入" min-width="180" show-overflow-tooltip />
          <el-table-column prop="model_response" label="模型输出" min-width="200" show-overflow-tooltip />
          <el-table-column label="得分" width="80" align="right">
            <template #default="{ row }">{{ row.score ?? '-' }}</template>
          </el-table-column>
          <el-table-column prop="judge_feedback" label="评分理由" min-width="200" show-overflow-tooltip />
        </el-table>
      </el-card>
    </template>

    <!-- M6 QE6-2：品牌页脚与水印（开源版显示；企业 license 授权
         report_white_label / report_watermark 后由服务端 branding 关闭） -->
    <div v-if="report?.branding?.show_brand" class="brand-footer">
      Powered by <strong>AgentEvo</strong> · 私有化 Agent 质量工程平台
    </div>
    <div v-if="report?.branding?.show_watermark" class="report-watermark">AgentEvo · 开源版</div>
  </div>
</template>

<script setup>
// M5 QE5-4：免登录只读报告页（/share/:token）
// 复用结果展示页的汇总卡/门禁横幅/维度雷达图口径做只读渲染——无任何写操作
// 入口；token 即凭证，过期/吊销由公开端点 404，本页转「已失效」态。
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import * as echarts from 'echarts'
import { shareApi } from '@/api'
import { formatTime } from '@/utils/time'

const route = useRoute()
const loading = ref(true)
const notFound = ref(false)
const report = ref(null)
const radarRef = ref(null)
let radarChart = null

const agg = computed(() => report.value?.result || {})
const rows = computed(() => agg.value.items || [])
const dimensionEntries = computed(() => {
  const ds = agg.value.dimension_scores || {}
  return Object.entries(ds).filter(([, v]) => v && v.average != null)
})

const gateVerdictLabel = (v) => ({ passed: '通过（PASSED）', failed: '未通过（FAILED）', error: '错误（ERROR）' }[v] || v)

const renderRadar = () => {
  if (!radarRef.value || !dimensionEntries.value.length) return
  radarChart = echarts.init(radarRef.value)
  const entries = dimensionEntries.value
  radarChart.setOption({
    tooltip: {},
    radar: {
      indicator: entries.map(([name, data]) => ({ name, max: 10 })),
    },
    series: [{
      type: 'radar',
      data: [{
        value: entries.map(([, data]) => data.average),
        name: '维度均分',
        areaStyle: { opacity: 0.2 },
      }],
    }],
  })
}

onMounted(async () => {
  try {
    const res = await shareApi.getReport(route.params.token)
    report.value = res
    await nextTick()
    renderRadar()
  } catch (e) {
    // 404（不存在/吊销/过期）与 429（限流）都转「已失效」态，不区分原因
    notFound.value = true
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.share-report-page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
}
.header-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #909399;
  font-size: 13px;
}
.expires-tip {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
}
.stat-card-small {
  text-align: center;
  padding: 8px 0;
}
.stat-label { font-size: 13px; color: #666; margin-bottom: 6px; }
.stat-value { font-size: 26px; font-weight: bold; color: #303133; }
.stat-value.success { color: #67c23a; }
.stat-value.danger { color: #f56c6c; }
.stat-value.highlight { color: #409eff; }
.gate-banner { border-left: 4px solid #909399; }
.gate-banner.gate-passed { border-left-color: #67c23a; }
.gate-banner.gate-failed { border-left-color: #f56c6c; }
.gate-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.gate-label { font-weight: 600; }
.gate-time { color: #909399; font-size: 12px; }

/* M6 QE6-2：品牌页脚（开源版传播位；report_white_label 授权后服务端关闭） */
.brand-footer {
  margin-top: 16px;
  text-align: center;
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}

/* 汇总页水印：右下角固定半透明标识（report_watermark 授权后关闭） */
.report-watermark {
  position: fixed;
  right: 18px;
  bottom: 14px;
  color: rgba(144, 147, 153, 0.32);
  font-size: 13px;
  letter-spacing: 1px;
  pointer-events: none;
  user-select: none;
  z-index: 10;
}
</style>
