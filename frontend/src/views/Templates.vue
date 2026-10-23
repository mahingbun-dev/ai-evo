<template>
  <div class="templates-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>评测模板</span>
          <div class="card-header-actions">
            <!-- 批量迁移旧版模板：steps 合并进 score_details（幂等） -->
            <el-button :loading="migrating" @click="migrateLegacyTemplates">迁移旧模板</el-button>
            <el-button type="primary" :disabled="migrating" @click="openDialog()">新建模板</el-button>
          </div>
        </div>
      </template>

      <el-table :data="templates" style="width: 100%">
        <el-table-column prop="name" label="模板名称" min-width="150" />
        <el-table-column prop="scene" label="场景" min-width="100" />
        <el-table-column prop="scene_description" label="场景描述" min-width="200" show-overflow-tooltip />
        <el-table-column prop="max_score" label="最高分" min-width="80" />
        <el-table-column prop="is_preset" label="类型" min-width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_preset ? 'warning' : 'primary'">
              {{ row.is_preset ? '预置' : '自定义' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :loading="previewingId === row.id" :disabled="rowOpLoading" @click="previewTemplate(row)">预览</el-button>
            <el-button link type="primary" :disabled="rowOpLoading" @click="openVersions(row)">版本</el-button>
            <el-button link type="primary" :disabled="rowOpLoading" @click="openDialog(row)">编辑</el-button>
            <el-button link type="primary" :loading="copyingId === row.id" :disabled="rowOpLoading" @click="copyTemplate(row)">复制</el-button>
            <el-button link type="danger" :loading="deletingId === row.id" :disabled="rowOpLoading" @click="deleteTemplate(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑模板' : '新建模板'" width="1200px" class="template-dialog">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px">
        <el-divider content-position="left">基本信息</el-divider>
        <el-form-item label="模板名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入模板名称" />
        </el-form-item>
        <el-form-item label="场景" prop="scene">
          <el-select v-model="form.scene" placeholder="请选择场景" style="width: 100%">
            <el-option v-for="s in scenes" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="场景描述">
          <el-input v-model="form.scene_description" type="textarea" rows="2" placeholder="请输入场景描述" />
          <div class="effect-tags" style="margin-top: 4px;">
            <el-tag size="small" type="primary">分析节点</el-tag>
            <el-tag size="small" type="success">评分节点</el-tag>
            <span style="margin-left: 8px; color: #909399; font-size: 12px;">此信息将注入到裁判的分析和评分全过程</span>
          </div>
        </el-form-item>
        <el-form-item label="评测指令（可选）">
          <el-input v-model="form.judge_prompt" type="textarea" rows="2" placeholder="额外的评测指引..." />
          <div class="effect-tags" style="margin-top: 4px;">
            <el-tag size="small" type="primary">分析节点</el-tag>
            <span style="margin-left: 8px; color: #909399; font-size: 12px;">此信息仅注入到分析阶段</span>
          </div>
        </el-form-item>
        <el-form-item label="最高分">
          <el-input-number v-model="form.max_score" :min="1" :max="100" />
        </el-form-item>
        <el-form-item label="分值标准">
          <RefTextarea
            v-model="form.score_details"
            :targets="[]"
            :vars="refVarOptions"
            :rows="3"
            placeholder="请输入评分标准，如：9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格（输入 @ 可引用裁判变量）"
          />
          <div style="margin-top: 4px; color: #909399; font-size: 12px;">
            评分优先级：维度精确标准 → 全局评分标准 → 默认
          </div>
        </el-form-item>

        <!-- 评测维度 -->
        <el-divider content-position="left">评测维度</el-divider>
        <el-form-item label="评测维度">
          <div style="width: 100%; margin-bottom: 8px; color: #909399; font-size: 12px;">
            维度顺序即评分步骤：裁判将按下方维度排列顺序逐步分析并评分，可拖拽卡片调整步骤顺序，无需单独配置「评分步骤」。
          </div>
          <div class="metrics-grid">
            <div
              v-for="(metric, index) in form.evaluation_metrics"
              :key="metric.id"
              class="metric-card"
              :class="{ 'metric-card-dragging': metricDragState.dragging && metricDragState.fromIndex === index }"
              draggable="true"
              @dragstart="handleMetricDragStart($event, index)"
              @dragover="handleMetricDragOver($event, index)"
              @drop="handleMetricDrop($event, index)"
              @dragend="handleMetricDragEnd"
            >
              <div class="metric-item">
                <div class="metric-header">
                  <div class="metric-header-left">
                    <el-icon class="drag-handle"><Rank /></el-icon>
                    <span class="metric-index">{{ index + 1 }}</span>
                    <span class="metric-title">维度 {{ index + 1 }}</span>
                  </div>
                  <el-button type="danger" link @click="removeMetric(index)" class="metric-delete">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </div>
                <div class="metric-body">
                  <div class="metric-field-row">
                    <div class="metric-field metric-name">
                      <label>维度名称</label>
                      <el-input v-model="metric.name" placeholder="请输入维度名称" />
                    </div>
                    <div class="metric-field metric-field-xs">
                      <label>权重</label>
                      <el-select
                        v-model="metric.weight"
                        placeholder="权重"
                        style="width: 100%"
                        filterable
                        allow-create
                        default-first-option
                        @change="(val) => { metric.weight = normalizeMetricWeight(val) }"
                      >
                        <el-option v-if="isCustomWeight(metric.weight)" :label="String(metric.weight)" :value="metric.weight" />
                        <el-option label="高" value="high" />
                        <el-option label="中" value="medium" />
                        <el-option label="低" value="low" />
                      </el-select>
                    </div>
                    <div class="metric-field metric-field-xs">
                      <label>最高分</label>
                      <el-input-number v-model="metric.max_score" :min="1" :max="form.max_score" controls-position="right" style="width: 100%" />
                    </div>
                  </div>
                  <div class="metric-desc">
                    <label>维度定义</label>
                    <RefTextarea
                      v-model="metric.description"
                      :rows="2"
                      :targets="[]"
                      :vars="refVarOptions"
                      placeholder="请输入维度定义描述（输入 @ 可引用裁判变量）"
                    />
                  </div>
                  <!-- 评分标准（可折叠） -->
                  <el-collapse style="margin-top: 8px;">
                    <el-collapse-item title="评分标准（可选，不配置则使用全局标准）">
                      <div v-for="(criteria, level, ci) in metric.score_criteria" :key="ci" style="display: flex; gap: 6px; align-items: center; margin-bottom: 6px;">
                        <el-tag size="small" style="width: 60px; text-align: center;">{{ level }}</el-tag>
                        <el-input-number v-model="criteria.min" :min="0" :max="form.max_score" size="small" controls-position="right" style="width: 90px;" />
                        <span style="color: #909399;">~</span>
                        <el-input-number v-model="criteria.max" :min="0" :max="form.max_score" size="small" controls-position="right" style="width: 90px;" />
                        <el-input v-model="criteria.desc" placeholder="等级描述" size="small" style="flex: 1;" />
                        <el-button type="danger" size="small" link :disabled="saving" @click="removeCriteriaLevel(metric, level)">
                          <el-icon><Delete /></el-icon>
                        </el-button>
                      </div>
                      <el-button size="small" type="primary" plain @click="addCriteriaLevel(metric)">
                        <el-icon><Plus /></el-icon> 添加等级
                      </el-button>
                      <div v-if="scoreCriteriaOverlap[metric.name || metric.id]"
                           style="margin-top: 6px; background: #fef0f0; border-radius: 3px; padding: 4px 8px; font-size: 12px; color: #f56c6c;">
                        评分等级范围重叠：
                        <div v-for="msg in scoreCriteriaOverlap[metric.name || metric.id]" :key="msg">{{ msg }}</div>
                      </div>
                    </el-collapse-item>
                    <!-- 维度依赖关系 -->
                    <el-collapse-item title="维度依赖关系（可选）" name="dependency">
                      <div style="font-size: 12px; color: #909399; margin-bottom: 8px;">
                        配置此维度对其他维度的依赖条件。满足条件时，本维度得分将被覆盖。
                      </div>
                      <div v-for="(dep, depIdx) in (metric.dependencies || [])" :key="depIdx"
                           style="display: flex; gap: 6px; align-items: center; margin-bottom: 6px; flex-wrap: wrap;">
                        <span style="font-size: 12px;">当</span>
                        <el-select v-model="dep.source_dimension" size="small" style="width: 140px;" placeholder="选择维度">
                          <el-option v-for="m in otherMetrics(metric.name)" :key="m.name" :label="m.name" :value="m.name" />
                        </el-select>
                        <el-select v-model="dep.condition" size="small" style="width: 70px;">
                          <el-option label="=" value="eq" />
                          <el-option label="≠" value="ne" />
                          <el-option label="<" value="lt" />
                          <el-option label=">" value="gt" />
                          <el-option label="≤" value="lte" />
                          <el-option label="≥" value="gte" />
                        </el-select>
                        <el-input-number v-model="dep.value" size="small" :min="0" :max="form.max_score" style="width: 70px;" />
                        <span style="font-size: 12px;">时，本维度得分 =</span>
                        <el-input-number v-model="dep.override_value" size="small" :min="0" :max="form.max_score" style="width: 70px;" />
                        <el-button type="danger" size="small" link @click="removeDependency(metric, depIdx)">
                          <el-icon><Delete /></el-icon>
                        </el-button>
                      </div>
                      <el-button size="small" type="primary" plain @click="addDependency(metric)">
                        <el-icon><Plus /></el-icon> 添加依赖条件
                      </el-button>
                    </el-collapse-item>
                  </el-collapse>
                </div>
              </div>
            </div>
            <div v-if="form.evaluation_metrics.length === 0" class="metrics-empty">
              <el-icon><Plus /></el-icon>
              <span>暂无评测维度</span>
            </div>
            <el-button type="primary" plain @click="addMetric" class="add-metric-btn">
              <el-icon><Plus /></el-icon> 添加评测维度
            </el-button>
          </div>
        </el-form-item>

        <!-- 编辑留档（M5 QE5-2）：填写即先把改前内容打版进版本链再保存 -->
        <el-form-item v-if="isEdit" label="版本说明">
          <el-input
            v-model="versionChangelog"
            placeholder="选填：填写后保存时自动把当前内容打版（改前状态进版本链，可 diff/恢复）"
            maxlength="500"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" :disabled="saving">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitForm">确定</el-button>
      </template>
    </el-dialog>

    <!-- 预览对话框 -->
    <el-dialog v-model="previewVisible" title="模板预览" width="700px">
      <pre class="preview-content">{{ previewContent }}</pre>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 版本抽屉（M5 QE5-2）：打版历史 + 字段级 diff 双栏 + 恢复 -->
    <el-drawer v-model="versionDrawerVisible" :title="`版本管理 - ${versionTemplate?.name || ''}`" size="66%" :close-on-click-modal="false">
      <div v-loading="versionsLoading" class="version-drawer-body">
        <!-- 打版区 -->
        <div class="version-actions">
          <el-input
            v-model="newVersionChangelog"
            placeholder="版本变更说明（可选，如：收紧评分标准）"
            style="max-width: 380px"
            maxlength="500"
            @keyup.enter="createVersion"
          />
          <el-button type="primary" :loading="versionCreating" @click="createVersion">
            以当前内容打版
          </el-button>
          <span class="version-hint">版本不可变：可 diff / 恢复 / 在 A/B 测试做两版本对照实验</span>
        </div>

        <!-- 版本列表 -->
        <el-table :data="templateVersions" size="small" border>
          <el-table-column prop="version_no" label="版本号" width="80" align="center">
            <template #default="{ row }">
              <el-tag size="small">v{{ row.version_no }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="changelog" label="变更说明" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">{{ row.changelog || '-' }}</template>
          </el-table-column>
          <el-table-column prop="created_by" label="创建人" width="110" show-overflow-tooltip>
            <template #default="{ row }">{{ row.created_by || '-' }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="diffWithLatest(row)">与最新版对比</el-button>
              <el-button link type="primary" size="small" @click="restoreVersion(row)">恢复到此版本</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!versionsLoading && templateVersions.length === 0" description="暂无版本，点「以当前内容打版」创建第一个版本" :image-size="60" />

        <!-- 字段级 diff 双栏 -->
        <div v-if="versionDiff" class="version-diff">
          <el-divider content-position="left">
            版本对比 v{{ versionDiff.from.version_no }} → v{{ versionDiff.to.version_no }}
          </el-divider>
          <div class="diff-summary">
            <el-tag type="warning">变更字段 {{ versionDiff.summary.changed }}</el-tag>
            <el-tag type="info">未变字段 {{ versionDiff.summary.unchanged }}</el-tag>
          </div>
          <div class="diff-table">
            <div class="diff-row diff-head">
              <div class="diff-cell diff-meta">字段</div>
              <div class="diff-cell">v{{ versionDiff.from.version_no }}（{{ formatTime(versionDiff.from.created_at) || '-' }}）</div>
              <div class="diff-cell">v{{ versionDiff.to.version_no }}（{{ formatTime(versionDiff.to.created_at) || '-' }}）</div>
            </div>
            <div v-for="f in versionDiff.fields" :key="f.field" class="diff-row diff-changed">
              <div class="diff-cell diff-meta">{{ fieldLabel(f.field) }}</div>
              <div class="diff-cell">{{ renderDiffValue(f.from) }}</div>
              <div class="diff-cell">{{ renderDiffValue(f.to) }}</div>
            </div>
            <el-empty
              v-if="versionDiff.fields.length === 0"
              description="两版本内容完全一致"
              :image-size="60"
            />
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Rank } from '@element-plus/icons-vue'
import { instructionApi } from '@/api'
import { formatTime } from '@/utils/time'
import { isCustomWeight, normalizeMetricWeight } from '@/utils/weight'
import RefTextarea from '@/components/RefTextarea.vue'

const templates = ref([])
const scenes = ref([])
const dialogVisible = ref(false)
const previewVisible = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const previewContent = ref('')

const form = ref({
  name: '',
  scene: '',
  scene_description: '',
  judge_prompt: '',
  max_score: 10,
  score_details: '',
  evaluation_metrics: [],
  steps: [],
  judge_variable_config: []
})

// RefTextarea 可引用的裁判变量（模板自带 judge_variable_config，模板页不编辑仅用于 @ 引用提示）
const refVarOptions = computed(() =>
  (Array.isArray(form.value.judge_variable_config) ? form.value.judge_variable_config : [])
    .filter((item) => item && item.key !== undefined && item.key !== null)
    .map((item) => ({ key: String(item.key), name: item.name || String(item.key) }))
)

// 评分标准重叠检查
const scoreCriteriaOverlap = computed(() => {
  const overlaps = {}
  for (const metric of form.value.evaluation_metrics) {
    if (!metric.score_criteria || Object.keys(metric.score_criteria).length < 2) continue
    const levels = Object.entries(metric.score_criteria)
    const msgs = []
    for (let i = 0; i < levels.length; i++) {
      for (let j = i + 1; j < levels.length; j++) {
        const [aName, a] = levels[i]
        const [bName, b] = levels[j]
        if (a.min <= b.max && b.min <= a.max) {
          msgs.push(`${aName}(${a.min}-${a.max}) 与 ${bName}(${b.min}-${b.max}) 范围重叠`)
        }
      }
    }
    if (msgs.length > 0) overlaps[metric.name || metric.id] = msgs
  }
  return overlaps
})

const rules = {
  name: [{ required: true, message: '请输入模板名称', trigger: 'blur' }],
  scene: [{ required: true, message: '请选择场景', trigger: 'change' }]
}

// ==================== 模板版本（M5 QE5-2：打版/diff/恢复） ====================
const versionChangelog = ref('')       // 编辑留档说明（填写即改前自动打版）
const versionDrawerVisible = ref(false)
const versionTemplate = ref(null)      // 当前抽屉对应模板行
const templateVersions = ref([])       // 版本列表（新→旧）
const versionsLoading = ref(false)
const versionCreating = ref(false)     // 打版进行中
const newVersionChangelog = ref('')    // 打版变更说明输入
const versionDiff = ref(null)          // 字段级 diff 结果

const openVersions = async (row) => {
  versionTemplate.value = row
  versionDrawerVisible.value = true
  versionDiff.value = null
  newVersionChangelog.value = ''
  await loadVersions(row.id)
}

const loadVersions = async (templateId) => {
  if (!templateId) return
  versionsLoading.value = true
  try {
    const res = await instructionApi.listTemplateVersions(templateId)
    templateVersions.value = res || []
  } catch (e) {
    console.error('获取模板版本列表失败:', e)
    templateVersions.value = []
  } finally {
    versionsLoading.value = false
  }
}

const createVersion = async () => {
  if (!versionTemplate.value || versionCreating.value) return
  versionCreating.value = true
  try {
    const version = await instructionApi.createTemplateVersion(versionTemplate.value.id, {
      changelog: newVersionChangelog.value?.trim() || null
    })
    ElMessage.success(`打版成功：v${version.version_no}`)
    newVersionChangelog.value = ''
    await loadVersions(versionTemplate.value.id)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '打版失败')
  } finally {
    versionCreating.value = false
  }
}

const diffWithLatest = async (row) => {
  const latest = templateVersions.value[0]
  if (!latest) return
  if (latest.version_no === row.version_no) {
    ElMessage.info('该版本已是最新版，无对比意义')
    return
  }
  try {
    versionDiff.value = await instructionApi.diffTemplateVersions(
      versionTemplate.value.id, row.version_no, latest.version_no)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '获取版本 diff 失败')
  }
}

const restoreVersion = async (row) => {
  try {
    await ElMessageBox.confirm(
      `恢复将把模板内容整体替换为 v${row.version_no}（恢复前的当前内容会自动留存为新版本，不会丢失）。确定恢复？`,
      '恢复模板版本',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' }
    )
  } catch { return }
  try {
    await instructionApi.restoreTemplateVersion(versionTemplate.value.id, row.version_no)
    ElMessage.success(`已恢复到 v${row.version_no}`)
    await loadTemplates()
    // 抽屉对应行已变，重新挂最新行数据
    versionTemplate.value = templates.value.find(t => t.id === versionTemplate.value.id) || versionTemplate.value
    await loadVersions(versionTemplate.value.id)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '恢复失败')
  }
}

// diff 字段中文名
const FIELD_LABELS = {
  name: '模板名称', scene: '评测场景', scene_description: '场景描述',
  judge_prompt: '评测指令', evaluation_metrics: '评测维度', score_details: '分值标准',
  max_score: '最高分', steps: '评分步骤（旧）', judge_variable_config: '裁判变量配置',
  instruction_template: '完整指令模板'
}
const fieldLabel = (field) => FIELD_LABELS[field] || field

// diff 单元格渲染：结构字段 JSON 化、多行文本保留换行
const renderDiffValue = (value) => {
  if (value == null) return '（空）'
  if (typeof value === 'object') {
    try { return JSON.stringify(value, null, 0) } catch { return String(value) }
  }
  return String(value)
}

const loadTemplates = async () => {
  try {
    const res = await instructionApi.list({ limit: 100 })
    templates.value = res || []
  } catch (e) {
    ElMessage.error('加载失败')
  }
}

const loadScenes = async () => {
  try {
    const res = await instructionApi.scenes()
    scenes.value = res || []
  } catch (e) {
    console.error(e)
  }
}

// 批量迁移旧版模板：将旧版「评分步骤」合并进评分标准（后端幂等，可重复执行）
const migrating = ref(false)
const migrateLegacyTemplates = async () => {
  try {
    await ElMessageBox.confirm(
      '将把旧版模板的「评分步骤」合并进各自的评分标准（幂等操作，可重复执行），确定开始迁移吗？',
      '迁移旧模板',
      { type: 'warning', confirmButtonText: '开始迁移', cancelButtonText: '取消' }
    )
  } catch (e) {
    return // 用户取消确认
  }
  migrating.value = true
  try {
    const res = await instructionApi.migrateAllLegacy()
    const summary = `迁移完成：共扫描 ${res.total} 个模板，迁移 ${res.migrated} 个，跳过 ${res.skipped} 个`
    if (res.errors && res.errors.length > 0) {
      ElMessage.warning(`${summary}，失败 ${res.errors.length} 个（详情见后端日志）`)
    } else {
      ElMessage.success(summary)
    }
    loadTemplates()
  } catch (e) {
    ElMessage.error(e.message || '迁移失败')
  } finally {
    migrating.value = false
  }
}

const openDialog = (row = null) => {
  // 编辑留档（M5 QE5-2）：每次打开重置版本说明输入
  versionChangelog.value = ''
  if (row) {
    isEdit.value = true
    form.value = {
      ...row,
      judge_prompt: row.judge_prompt || '',
      score_details: row.score_details || '',
      evaluation_metrics: (row.evaluation_metrics || []).map((m, i) => ({
        id: `metric_${Date.now()}_${i}`,
        name: m.name || '',
        weight: m.weight || 'medium',
        max_score: m.max_score || row.max_score || 10,
        description: m.description || '',
        score_criteria: m.score_criteria || { 'S': { min: 9, max: 10, desc: '优秀' }, 'A': { min: 7, max: 8, desc: '良好' }, 'B': { min: 5, max: 6, desc: '及格' }, 'C': { min: 0, max: 4, desc: '不及格' } },
        dependencies: m.dependencies || []
      })),
      steps: Array.isArray(row.steps) ? [...row.steps] : (row.steps ? row.steps.split('\n').filter(s => s.trim()) : []),
      judge_variable_config: Array.isArray(row.judge_variable_config) ? row.judge_variable_config : []
    }
  } else {
    isEdit.value = false
    form.value = {
      name: '',
      scene: '',
      scene_description: '',
      judge_prompt: '',
      max_score: 10,
      score_details: '',
      evaluation_metrics: [],
      steps: [],
      judge_variable_config: []
    }
  }
  dialogVisible.value = true
}

const addMetric = () => {
  form.value.evaluation_metrics.push({
    id: `metric_${Date.now()}`,
    name: '',
    weight: 'medium',
    max_score: form.value.max_score || 10,
    description: '',
    score_criteria: { 'S': { min: 9, max: 10, desc: '优秀' }, 'A': { min: 7, max: 8, desc: '良好' }, 'B': { min: 5, max: 6, desc: '及格' }, 'C': { min: 0, max: 4, desc: '不及格' } },
    dependencies: []
  })
}

// 添加评分标准等级
const addCriteriaLevel = (metric) => {
  if (!metric.score_criteria) {
    metric.score_criteria = {}
  }
  const levels = ['S', 'A', 'B', 'C', 'D', 'E', 'F']
  const usedLevels = Object.keys(metric.score_criteria)
  const nextLevel = levels.find(l => !usedLevels.includes(l)) || `Level${usedLevels.length + 1}`
  metric.score_criteria[nextLevel] = { min: 0, max: 0, desc: '' }
}

// 删除评分标准等级
const removeCriteriaLevel = (metric, level) => {
  if (!metric.score_criteria) return
  delete metric.score_criteria[level]
}

// 添加维度依赖关系
const addDependency = (metric) => {
  if (!metric.dependencies) {
    metric.dependencies = []
  }
  metric.dependencies.push({
    source_dimension: '',
    condition: 'eq',
    value: 0,
    override_value: 0
  })
}

// 删除维度依赖关系
const removeDependency = (metric, idx) => {
  metric.dependencies.splice(idx, 1)
}

// 获取其他维度列表（排除当前维度）
const otherMetrics = (currentName) => {
  return form.value.evaluation_metrics.filter(m => m.name && m.name !== currentName)
}

const removeMetric = (idx) => {
  form.value.evaluation_metrics.splice(idx, 1)
}

// 评测维度拖拽排序
const metricDragState = ref({
  dragging: false,
  fromIndex: null
})

const handleMetricDragStart = (event, index) => {
  metricDragState.value.dragging = true
  metricDragState.value.fromIndex = index
  event.dataTransfer.effectAllowed = 'move'
}

const handleMetricDragOver = (event, index) => {
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
}

const handleMetricDrop = (event, toIndex) => {
  event.preventDefault()
  if (metricDragState.value.fromIndex !== null && metricDragState.value.fromIndex !== toIndex) {
    const item = form.value.evaluation_metrics.splice(metricDragState.value.fromIndex, 1)[0]
    form.value.evaluation_metrics.splice(toIndex, 0, item)
  }
  metricDragState.value.dragging = false
  metricDragState.value.fromIndex = null
}

const handleMetricDragEnd = () => {
  metricDragState.value.dragging = false
  metricDragState.value.fromIndex = null
}

// 模板保存中（防重复提交）
const saving = ref(false)
const submitForm = async () => {
  if (saving.value) return
  saving.value = true
  try {
    await formRef.value.validate()
    const data = { ...form.value }
    if (isEdit.value) {
      // M5 QE5-2 编辑留档：填写版本说明即先为改前内容打版（后端先打版再应用编辑）
      if (versionChangelog.value?.trim()) {
        data.version_changelog = versionChangelog.value.trim()
      }
      await instructionApi.update(form.value.id, data)
      ElMessage.success('更新成功')
    } else {
      await instructionApi.create(data)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadTemplates()
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
  } finally {
    saving.value = false
  }
}

// 模板预览请求中（按行标识，仅当前行按钮转圈）
const previewingId = ref(null)
const previewTemplate = async (row) => {
  previewingId.value = row.id
  try {
    const res = await instructionApi.preview({
      scene: row.scene,
      scene_description: row.scene_description,
      judge_prompt: row.judge_prompt || '',
      evaluation_metrics: row.evaluation_metrics || [],
      max_score: row.max_score,
      steps: Array.isArray(row.steps) ? row.steps : []
    })
    previewContent.value = res.full_prompt
    previewVisible.value = true
  } catch (e) {
    ElMessage.error('预览失败')
  } finally {
    previewingId.value = null
  }
}

// 模板删除请求中（按行标识；确认后再置位，避免确认框期间转圈）
const deletingId = ref(null)
const deleteTemplate = async (row) => {
  try {
    await ElMessageBox.confirm('确定要删除该模板吗？', '提示', { type: 'warning' })
    deletingId.value = row.id
    await instructionApi.delete(row.id)
    ElMessage.success('删除成功')
    loadTemplates()
  } catch (e) {
    if (e !== 'cancel') {
      // 显示后端返回的具体错误信息
      const errorMsg = e.response?.data?.detail || e.message || '删除失败'
      ElMessage.error(errorMsg)
    }
  } finally {
    deletingId.value = null
  }
}

// 模板复制请求中（按行标识，仅当前行按钮转圈）
const copyingId = ref(null)
// 任一行级操作（预览/复制/删除）进行中 → 操作列所有按钮禁用，防止并发写操作
const rowOpLoading = computed(() => previewingId.value !== null || deletingId.value !== null || copyingId.value !== null)
const copyTemplate = async (row) => {
  copyingId.value = row.id
  try {
    const data = {
      name: `${row.name} - 副本`,
      scene: row.scene,
      scene_description: row.scene_description || '',
      judge_prompt: row.judge_prompt || '',
      max_score: row.max_score || 10,
      score_details: row.score_details || '',
      evaluation_metrics: row.evaluation_metrics || [],
      steps: Array.isArray(row.steps) ? [...row.steps] : [],
      judge_variable_config: row.judge_variable_config || []
    }
    await instructionApi.create(data)
    ElMessage.success('复制成功')
    loadTemplates()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message || '复制失败')
  } finally {
    copyingId.value = null
  }
}

onMounted(() => {
  loadTemplates()
  loadScenes()
})
</script>

<style scoped>
.templates-page {
  max-width: 1400px;
  margin: 0 auto;
}

/* ==================== 版本抽屉（M5 QE5-2） ==================== */
.version-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.version-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.version-hint {
  color: #909399;
  font-size: 12px;
}

.diff-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

/* diff 双栏：左=from 版本 / 右=to 版本，字段值多行展示 */
.diff-table {
  border: 1px solid #ebeef5;
  border-radius: 4px;
  max-height: 460px;
  overflow: auto;
}

.diff-row {
  display: flex;
  border-bottom: 1px solid #ebeef5;
}

.diff-row:last-child {
  border-bottom: none;
}

.diff-head {
  background: #f5f7fa;
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 1;
}

.diff-cell {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  border-left: 1px solid #ebeef5;
}

.diff-meta {
  flex: 0 0 110px;
}

.diff-changed {
  background: #fdf6ec;
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

/* 表格样式 */
:deep(.el-table) {
  --el-table-border-color: #E5E7EB;
}

:deep(.el-table th) {
  background: #F9FAFB;
  color: #374151;
  font-weight: 500;
}

:deep(.el-table td) {
  padding: 14px 0;
}

:deep(.el-dialog) {
  border-radius: 12px;
}

:deep(.el-dialog__header) {
  border-bottom: 1px solid #E5E7EB;
  padding-bottom: 16px;
}

:deep(.el-dialog__title) {
  font-weight: 600;
  color: #1F2937;
}

/* 对话框最大宽度 */
:deep(.template-dialog) {
  max-width: 95vw;
}

/* 确保对话框内容区域充满 */
:deep(.template-dialog .el-dialog__body) {
  overflow-wrap: break-word;
  word-break: break-word;
  max-height: calc(90vh - 140px);
  overflow-y: auto;
}

:deep(.template-dialog .el-form-item__content) {
  overflow-wrap: break-word;
  word-break: break-word;
  width: 100%;
}

/* 评测维度样式 - 两列网格布局 */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  width: 100%;
  box-sizing: border-box;
}

/* 确保评测维度的el-form-item内容区域充满 */
:deep(.el-form-item:has(.metrics-grid) .el-form-item__content) {
  width: 100%;
  max-width: none;
}

.metric-card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px 14px;
  transition: border-color 0.2s, box-shadow 0.2s, opacity 0.2s;
  cursor: move;
}

.metric-card:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}

.metric-card-dragging {
  opacity: 0.5;
  border-color: #409eff;
}

/* 当只有一个维度时，不使用两列布局 */
.metrics-grid:has(.metrics-empty) {
  grid-template-columns: 1fr;
}

.metric-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.metric-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.drag-handle {
  cursor: move;
  color: #909399;
  font-size: 16px;
}

.drag-handle:hover {
  color: #409eff;
}

.metric-index {
  width: 22px;
  height: 22px;
  background: linear-gradient(135deg, #409eff, #3498db);
  color: #fff;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
}

.metric-title {
  font-weight: 600;
  color: #303133;
  font-size: 13px;
}

.metric-delete {
  font-size: 12px;
  padding: 2px 6px;
}

.metric-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.metric-field-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.metric-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.metric-field label {
  font-size: 11px;
  color: #909399;
  font-weight: 500;
}

.metric-name {
  flex: 1;
  min-width: 0;
}

.metric-field-xs {
  flex: 0 0 100px;
}

.metric-desc {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.metric-desc label {
  font-size: 11px;
  color: #909399;
  font-weight: 500;
}

.metrics-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 30px;
  color: #c0c4cc;
  background: #fafafa;
  border-radius: 8px;
  border: 1px dashed #dcdfe6;
  gap: 8px;
}

.metrics-empty .el-icon {
  font-size: 24px;
}

.add-metric-btn {
  width: 100%;
  justify-content: center;
  padding: 10px;
  border-style: dashed;
  color: #606266;
}

.add-metric-btn:hover {
  color: #409eff;
  border-color: #409eff;
  background: #f0f9ff;
}

:deep(.el-divider__text) {
  font-weight: 600;
  color: #303133;
}

.preview-content {
  background: #F9FAFB;
  padding: 15px;
  border-radius: 8px;
  max-height: 400px;
  overflow: auto;
  white-space: pre-wrap;
  font-size: 12px;
  color: #374151;
}
</style>
