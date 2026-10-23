<template>
  <el-dialog
    :model-value="modelValue"
    title="标注模式 — 人工金分打标"
    width="860px"
    top="5vh"
    destroy-on-close
    append-to-body
    @update:model-value="$emit('update:modelValue', $event)"
    @closed="resetState"
  >
    <div v-loading="loading" class="annotation-body">
      <!-- 队列为空 / 加载失败 -->
      <el-empty v-if="!loading && !items.length" description="该次执行没有行级结果（老任务或未落行级表）" :image-size="60" />

      <template v-else>
        <!-- 进度与队列说明 -->
        <div class="anno-progress">
          <div class="anno-progress-stats">
            <span>进度：<b>{{ annotatedCount }}</b> / {{ items.length }} 行</span>
            <span class="anno-muted">待标 {{ pendingCount }} · 跳过 {{ skippedCount }}</span>
            <span class="anno-muted">队列顺序：失败行 / 低分行优先</span>
          </div>
          <el-progress :percentage="items.length ? Math.round(annotatedCount / items.length * 100) : 0" :stroke-width="10" style="flex:1; margin-left: 16px;" />
        </div>

        <!-- 当前标注行 -->
        <div v-if="current" class="anno-card">
          <div class="anno-card-head">
            <el-tag size="small" type="info">第 {{ current.data_index + 1 }} 行 · 优先级 #{{ current.priority_rank }}</el-tag>
            <el-tag size="small" :type="statusTagType(current.row_status)">{{ statusLabel(current.row_status) }}</el-tag>
            <el-tag v-if="current.judge_score !== null && current.judge_score !== undefined" size="small" type="warning">
              裁判分 {{ current.judge_score }}
            </el-tag>
            <el-tag v-else size="small" type="info">无裁判分</el-tag>
            <el-tag v-if="current.annotation" size="small" :type="current.annotation.status === 'completed' ? 'success' : 'info'">
              {{ current.annotation.status === 'completed' ? `已标 ${current.annotation.gold_score}` : '已跳过' }}
            </el-tag>
          </div>

          <!-- 输入数据 -->
          <div v-if="inputEntries.length" class="anno-section">
            <div class="anno-section-title">输入数据</div>
            <div class="anno-kv-list">
              <div v-for="([k, v]) in inputEntries" :key="k" class="anno-kv">
                <span class="anno-kv-key">{{ k }}</span>
                <span class="anno-kv-value" :title="displayValue(v)">{{ truncate(displayValue(v), 300) }}</span>
              </div>
            </div>
          </div>

          <!-- 模型输出 -->
          <div v-if="current.model_output" class="anno-section">
            <div class="anno-section-title">模型输出</div>
            <pre class="anno-pre">{{ current.model_output }}</pre>
          </div>

          <!-- 裁判反馈 -->
          <div v-if="current.feedback" class="anno-section">
            <div class="anno-section-title">裁判反馈（参考，人工独立判断）</div>
            <pre class="anno-pre anno-feedback">{{ current.feedback }}</pre>
          </div>

          <!-- 快捷键打分区 -->
          <div class="anno-score-area">
            <div class="anno-section-title">人工金分（0 ~ {{ maxScore }}）</div>
            <div class="anno-score-buttons">
              <el-button
                v-for="s in scoreOptions"
                :key="s"
                :type="activeScore === s ? 'primary' : 'default'"
                :disabled="submitting || readonly"
                @click="submitScore(s)"
              >{{ s }}</el-button>
            </div>
            <el-input
              v-model="notes"
              type="textarea"
              :rows="1"
              placeholder="备注（可选）：争议点 / 判定依据"
              class="anno-notes"
              @keydown.stop
            />
            <div class="anno-hint">
              快捷键：数字键打分（{{ maxScore === 10 ? '0 = 满分 10' : '0-9' }}）· S 跳过 · Enter 下一条 · ← → 切换行
            </div>
          </div>
        </div>

        <!-- 全部标完 -->
        <el-empty
          v-else-if="!loading"
          description="本批次全部行已处理完成 — 可到 统计 → 裁判一致性 查看一致性报告"
          :image-size="70"
        />

        <!-- 导航 -->
        <div class="anno-nav">
          <el-button :disabled="currentIndex <= 0" @click="move(-1)">← 上一条</el-button>
          <el-button :disabled="submitting || readonly" @click="skipCurrent">跳过 (S)</el-button>
          <el-button :disabled="currentIndex >= items.length - 1" @click="move(1)">下一条 →</el-button>
        </div>
      </template>
    </div>
  </el-dialog>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { resultApi } from '@/api'

const props = defineProps({
  modelValue: Boolean,
  taskId: { type: String, default: '' },
  // 标注锚定的执行序号（空=最新一次执行，后端解析）
  executionNumber: { type: Number, default: null },
  readonly: Boolean,
})
const emit = defineEmits(['update:modelValue', 'annotated'])

const loading = ref(false)
const submitting = ref(false)
const items = ref([])
const maxScore = ref(10)
const currentIndex = ref(0)
const notes = ref('')
const activeScore = ref(null)

const current = computed(() => items.value[currentIndex.value] || null)
const annotatedCount = computed(() => items.value.filter(i => i.annotation).length)
const skippedCount = computed(
  () => items.value.filter(i => i.annotation?.status === 'skipped').length)
const pendingCount = computed(() => items.value.length - annotatedCount.value)

// 打分档位：0..maxScore（max_score 异常大时封顶 20 防按钮铺满）
const scoreCeil = computed(() => Math.min(maxScore.value || 10, 20))
const scoreOptions = computed(() =>
  Array.from({ length: scoreCeil.value + 1 }, (_, s) => s))

const inputEntries = computed(() => {
  const data = current.value?.input_data
  if (!data || typeof data !== 'object') return []
  return Object.entries(data).slice(0, 12)
})

const displayValue = (v) => {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
const truncate = (text, n) => (text && text.length > n ? text.slice(0, n) + '…' : text || '-')

const statusLabel = (s) => ({ completed: '成功', failed: '执行失败', judge_failed: '裁判失败', skipped: '跳过', pending: '待执行' }[s] || s)
const statusTagType = (s) => ({ completed: 'success', failed: 'danger', judge_failed: 'warning' }[s] || 'info')

const loadQueue = async () => {
  if (!props.taskId) return
  loading.value = true
  try {
    const params = {}
    if (props.executionNumber) params.execution_number = props.executionNumber
    const res = await resultApi.annotationQueue(props.taskId, params)
    items.value = res.items || []
    maxScore.value = res.max_score || 10
    jumpToFirstPending()
  } catch (e) {
    console.error('加载标注队列失败', e)
    ElMessage.error('加载标注队列失败')
    items.value = []
  } finally {
    loading.value = false
  }
}

const jumpToFirstPending = () => {
  const idx = items.value.findIndex(i => !i.annotation)
  currentIndex.value = idx >= 0 ? idx : Math.max(items.value.length - 1, 0)
  notes.value = current.value?.annotation?.notes || ''
}

const move = (delta) => {
  const next = currentIndex.value + delta
  if (next < 0 || next >= items.value.length) return
  currentIndex.value = next
  notes.value = current.value?.annotation?.notes || ''
  activeScore.value = null
}

// 打分后跳到下一个待标行（跳过已标行；到尾回绕一次找待标，找不到停在原地）
const advanceToNextPending = () => {
  for (let step = 1; step <= items.value.length; step++) {
    const idx = (currentIndex.value + step) % items.value.length
    if (!items.value[idx].annotation) {
      currentIndex.value = idx
      notes.value = ''
      activeScore.value = null
      return
    }
  }
  ElMessage.success('本批次全部行已处理完成')
}

const submit = async (payload) => {
  if (!current.value || submitting.value) return
  submitting.value = true
  try {
    const annotation = await resultApi.submitAnnotation(props.taskId, payload)
    current.value.annotation = annotation
    emit('annotated')
    advanceToNextPending()
  } catch (e) {
    console.error('提交标注失败', e)
    ElMessage.error(e?.response?.data?.detail || '提交标注失败')
  } finally {
    submitting.value = false
  }
}

const submitScore = (score) => {
  activeScore.value = score
  submit({ result_row_id: current.value.result_row_id, gold_score: score, notes: notes.value || null })
}

const skipCurrent = () => {
  submit({ result_row_id: current.value.result_row_id, status: 'skipped', notes: notes.value || null })
}

// ---------------- 快捷键（弹窗打开期间挂 document，输入框内不劫持） ----------------
const isTypingTarget = (e) => {
  const tag = (e.target?.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || e.target?.isContentEditable
}

const onKeydown = (e) => {
  if (!props.modelValue || loading.value || submitting.value || readonlyGuard()) return
  if (isTypingTarget(e)) return
  if (e.key >= '0' && e.key <= '9' && current.value) {
    e.preventDefault()
    // 0 在满分 10 制下=满分（1-9 原值）；其他分制 0 即 0 分
    const score = e.key === '0' && maxScore.value === 10 ? 10 : Number(e.key)
    if (score <= (maxScore.value || 10)) submitScore(score)
    return
  }
  const key = e.key.toLowerCase()
  if (key === 'enter' || key === 'arrowright') {
    e.preventDefault()
    move(1)
  } else if (key === 'arrowleft') {
    e.preventDefault()
    move(-1)
  } else if (key === 's' && current.value) {
    e.preventDefault()
    skipCurrent()
  }
}

const readonlyGuard = () => props.readonly === true

watch(() => props.modelValue, (open) => {
  if (open) loadQueue()
})

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => document.removeEventListener('keydown', onKeydown))

// 关闭即清场：下次打开重新拉队列，避免闪现上一任务的行
const resetState = () => {
  items.value = []
  currentIndex.value = 0
  notes.value = ''
  activeScore.value = null
}
</script>

<style scoped>
.annotation-body { min-height: 200px; }
.anno-progress { display: flex; align-items: center; margin-bottom: 14px; }
.anno-progress-stats { display: flex; gap: 14px; font-size: 13px; color: #303133; white-space: nowrap; }
.anno-muted { color: #909399; font-size: 12px; }
.anno-card { border: 1px solid #ebeef5; border-radius: 6px; padding: 14px; background: #fafbfc; }
.anno-card-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.anno-section { margin-bottom: 10px; }
.anno-section-title { font-size: 12px; color: #909399; margin-bottom: 4px; }
.anno-kv-list { display: flex; flex-direction: column; gap: 2px; }
.anno-kv { display: flex; gap: 8px; font-size: 13px; line-height: 1.6; }
.anno-kv-key { color: #909399; min-width: 90px; flex-shrink: 0; }
.anno-kv-value { color: #303133; word-break: break-all; white-space: pre-wrap; }
.anno-pre {
  margin: 0; padding: 8px; background: #fff; border: 1px solid #ebeef5; border-radius: 4px;
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  max-height: 180px; overflow-y: auto;
}
.anno-feedback { color: #606266; }
.anno-score-area { border-top: 1px dashed #dcdfe6; padding-top: 10px; margin-top: 4px; }
.anno-score-buttons { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.anno-score-buttons .el-button { margin-left: 0; width: 52px; }
.anno-notes { margin-bottom: 6px; }
.anno-hint { font-size: 12px; color: #c0c4cc; }
.anno-nav { display: flex; justify-content: center; gap: 12px; margin-top: 14px; }
</style>
