<template>
  <div class="safety-evaluation-config">
    <el-form
      ref="formRef"
      :model="formData"
      :rules="rules"
      label-width="120px"
      label-position="top"
    >
      <!-- 安全评测说明 -->
      <el-alert
        title="安全评测（红队）说明"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 20px"
      >
        <template #default>
          <div class="safety-info">
            <p>以攻击样本库对被评测模型发起红队攻击，双通道判定防守效果：</p>
            <ul>
              <li><strong>确定性通道</strong>：中文 PII（手机号/身份证/银行卡）+ 敏感词词表，命中即 fail、证据高亮；</li>
              <li><strong>裁判 rubric 通道</strong>：按攻击面动态安全维度，裁判模型判定回复是否违规；</li>
              <li><strong>变异增强</strong>（可选）：裁判模型改写样本变体二次攻击，检验对「换皮攻击」的泛化防守。</li>
            </ul>
          </div>
        </template>
      </el-alert>

      <!-- 攻击面类目多选（四类） -->
      <el-form-item label="攻击面类目" prop="categories">
        <el-checkbox-group v-model="formData.categories">
          <el-checkbox v-for="cat in categoryOptions" :key="cat.value" :label="cat.value">
            {{ cat.label }}
            <span v-if="packCategoryCount(cat.value) !== null" class="pack-count">
              （{{ packCategoryCount(cat.value) }} 条）
            </span>
          </el-checkbox>
        </el-checkbox-group>
        <div class="form-tip">内置样本库直跑时按所选类目载入对应攻击样本（每类 ≥30 条）。</div>
      </el-form-item>

      <!-- 样本来源 -->
      <el-form-item label="攻击样本来源" prop="sample_source">
        <el-radio-group v-model="formData.sample_source">
          <el-radio label="pack">内置样本库直跑（不可变、口径可复现）</el-radio>
          <el-radio label="dataset">数据集（可导入样本库后编辑）</el-radio>
        </el-radio-group>
        <div class="form-tip">
          内置样本库直跑无需选择数据集；数据集来源请选择 user_input 为攻击提示词、
          含 attack_category 字段的数据集（可由内置样本库一键导入后增删改）。
        </div>
      </el-form-item>

      <!-- 样本库选择（pack 来源） -->
      <template v-if="formData.sample_source === 'pack'">
        <el-form-item label="内置样本库" prop="pack">
          <div class="pack-row">
            <el-select v-model="formData.pack" style="flex: 1;" placeholder="选择内置样本库">
              <el-option
                v-for="p in packs"
                :key="p.name"
                :label="`${p.name}（v${p.version}，共 ${totalPackCount(p)} 条）`"
                :value="p.name"
              />
            </el-select>
            <el-button
              v-if="formData.sample_source === 'pack'"
              size="default"
              :loading="importing"
              @click="handleImportPack"
            >
              导入为数据集（可编辑）
            </el-button>
          </div>
          <div class="form-tip">
            导入后可在数据集管理页编辑样本，再把样本来源切到「数据集」引用编辑后的版本。
          </div>
        </el-form-item>
      </template>

      <!-- 变异增强开关 -->
      <el-form-item label="变异增强">
        <div class="mutation-row">
          <el-switch v-model="formData.mutation.enabled" />
          <span class="mutation-label">每样本变体数</span>
          <el-input-number
            v-model="formData.mutation.count"
            :min="1"
            :max="3"
            :disabled="!formData.mutation.enabled"
            size="small"
          />
        </div>
        <div class="form-tip">
          开启后由裁判模型对每条样本改写 {{ formData.mutation.count }} 个保持攻击意图的变体（二次攻击）；
          变异体单独统计防御率。变体生成失败会跳过该样本的变体，不阻断任务。
        </div>
      </el-form-item>

      <!-- 检测通道开关 -->
      <el-form-item label="检测通道">
        <div class="channel-row">
          <el-checkbox v-model="formData.channels.deterministic.enabled">
            确定性通道（PII + 敏感词，命中即 fail）
          </el-checkbox>
          <el-checkbox v-model="formData.channels.judge_rubric.enabled">
            裁判 rubric 通道（攻击面动态维度）
          </el-checkbox>
        </div>
        <div class="form-tip">双通道默认全开：确定性检出是铁证兜底，裁判通道覆盖语义级违规。</div>
      </el-form-item>

      <!-- deepeval 指标开关（M3 QE3-2：bias/toxicity 并入 safety metric 开关，可选增强） -->
      <el-form-item label="deepeval 指标（可选增强）">
        <DeepEvalMetricSelector
          v-model="formData.metrics"
          category="safety"
          label="安全增强指标"
          :required="false"
          placeholder="可选：对每条攻击回复追加 bias/toxicity 等逐指标判定（score 低于阈值计违规）"
        />
        <div class="form-tip">
          原「超管直调端点」的六项指标已并入此处（端点已标废弃）；未选择则不调用，
          双通道判定不受影响。
        </div>
      </el-form-item>

      <!-- 自定义敏感词（确定性通道高级配置） -->
      <el-collapse v-model="activeCollapse">
        <el-collapse-item title="确定性通道高级配置" name="advanced">
          <template #title>
            <div class="collapse-title">
              <el-icon><Setting /></el-icon>
              <span>确定性通道高级配置（自定义敏感词）</span>
            </div>
          </template>
          <el-form-item label="自定义敏感词">
            <el-select
              v-model="formData.channels.deterministic.custom_words"
              multiple
              filterable
              allow-create
              default-first-option
              style="width: 100%"
              placeholder="输入后回车添加（追加到内置词表）"
            />
            <div class="form-tip">内置词表覆盖暴恐/涉政/违禁品/淫秽色情类目，此处可追加业务专属敏感词。</div>
          </el-form-item>
        </el-collapse-item>
      </el-collapse>
    </el-form>
  </div>
</template>

<script setup>
import { ref, reactive, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Setting } from '@element-plus/icons-vue'
import DeepEvalMetricSelector from './DeepEvalMetricSelector.vue'
import { redteamApi } from '../api'

const props = defineProps({
  initialData: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['update'])

const formRef = ref(null)
const activeCollapse = ref([])

// 四类攻击面（M3 首批）
const categoryOptions = [
  { value: 'prompt_injection', label: '提示注入' },
  { value: 'jailbreak', label: '越狱' },
  { value: 'sensitive_content', label: '敏感内容' },
  { value: 'factual_assertion', label: '事实性断言' }
]

const formData = reactive({
  categories: categoryOptions.map(c => c.value),
  sample_source: 'pack',
  pack: 'redteam_pack_v1',
  mutation: { enabled: false, count: 1 },
  channels: {
    deterministic: { enabled: true, custom_words: [] },
    judge_rubric: { enabled: true }
  },
  // M3 QE3-2：deepeval 指标开关（bias/toxicity 等，可选增强通道，默认不启用）
  metrics: []
})

const rules = {
  categories: [
    { required: true, type: 'array', message: '请至少选择一个攻击面类目', trigger: 'change' }
  ]
}

// 内置样本库列表（载入延迟容忍：拉取失败仅提示，不阻断表单）
const packs = ref([])
const importing = ref(false)

const loadPacks = async () => {
  try {
    const data = await redteamApi.packs()
    packs.value = Array.isArray(data) ? data : []
  } catch (e) {
    // 样本库列表非关键路径：失败保留默认 redteam_pack_v1，提交由后端校验兜底
    packs.value = []
  }
}

const packCategoryCount = (categoryKey) => {
  const pack = packs.value.find(p => p.name === formData.pack)
  if (!pack) return null
  const cat = (pack.categories || []).find(c => c.key === categoryKey)
  return cat ? cat.sample_count : null
}

const totalPackCount = (pack) => (pack.categories || []).reduce((sum, c) => sum + (c.sample_count || 0), 0)

// 样本库一键导入数据集（可导入可编辑）：导入成功后提示切换来源
const handleImportPack = async () => {
  try {
    await ElMessageBox.confirm(
      `将内置样本库 ${formData.pack} 导入为数据集（${formData.categories.length} 个类目）？导入后可在数据集管理页编辑样本。`,
      '导入样本库',
      { confirmButtonText: '导入', cancelButtonText: '取消', type: 'info' }
    )
  } catch {
    return
  }
  importing.value = true
  try {
    const res = await redteamApi.importPack(formData.pack, {
      name: `红队样本库-${formData.pack}`,
      categories: formData.categories
    })
    ElMessage.success(`已导入数据集「${res.dataset_name}」（${res.imported_count} 条），可在数据集管理页编辑`)
  } catch (e) {
    const detail = e?.response?.data?.detail || e?.message || '未知错误'
    ElMessage.error(`样本库导入失败：${detail}`)
  } finally {
    importing.value = false
  }
}

// 发送更新（表单态 → 落库形状）
const emitUpdate = () => {
  emit('update', JSON.parse(JSON.stringify({
    categories: formData.categories,
    sample_source: formData.sample_source,
    pack: formData.pack,
    mutation: { ...formData.mutation },
    channels: {
      deterministic: { ...formData.channels.deterministic },
      judge_rubric: { ...formData.channels.judge_rubric }
    },
    metrics: [...formData.metrics]
  })))
}

watch(formData, emitUpdate, { deep: true })

// 初始化（编辑回显：兼容旧 deepeval 形状 {metrics, threshold}——视为未配置重置为红队默认）
onMounted(() => {
  const init = props.initialData || {}
  if (Array.isArray(init.categories) && init.categories.length) {
    formData.categories = [...init.categories]
  }
  if (init.sample_source === 'dataset' || init.sample_source === 'pack') {
    formData.sample_source = init.sample_source
  }
  if (init.pack) {
    formData.pack = init.pack
  }
  if (init.mutation && typeof init.mutation === 'object') {
    formData.mutation = {
      enabled: !!init.mutation.enabled,
      count: Math.min(Math.max(parseInt(init.mutation.count, 10) || 1, 1), 3)
    }
  }
  if (init.channels && typeof init.channels === 'object') {
    if (init.channels.deterministic) {
      formData.channels.deterministic = {
        enabled: init.channels.deterministic.enabled !== false,
        custom_words: Array.isArray(init.channels.deterministic.custom_words)
          ? [...init.channels.deterministic.custom_words]
          : []
      }
    }
    if (init.channels.judge_rubric) {
      formData.channels.judge_rubric = {
        enabled: init.channels.judge_rubric.enabled !== false
      }
    }
  }
  if (Array.isArray(init.metrics)) {
    formData.metrics = [...init.metrics]
  }
  loadPacks()
  emitUpdate()
})

defineExpose({
  validate: async () => {
    if (formRef.value) {
      return await formRef.value.validate()
    }
    return false
  },
  getData: () => JSON.parse(JSON.stringify({
    categories: formData.categories,
    sample_source: formData.sample_source,
    pack: formData.pack,
    mutation: { ...formData.mutation },
    channels: {
      deterministic: { ...formData.channels.deterministic },
      judge_rubric: { ...formData.channels.judge_rubric }
    },
    metrics: [...formData.metrics]
  })),
  reset: () => {
    formData.categories = categoryOptions.map(c => c.value)
    formData.sample_source = 'pack'
    formData.pack = 'redteam_pack_v1'
    formData.mutation = { enabled: false, count: 1 }
    formData.channels = {
      deterministic: { enabled: true, custom_words: [] },
      judge_rubric: { enabled: true }
    }
    formData.metrics = []
  }
})
</script>

<style scoped>
.safety-evaluation-config {
  padding: 0;
}

.safety-info p {
  margin: 0 0 8px 0;
  font-size: 13px;
  color: #606266;
}

.safety-info ul {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  color: #606266;
}

.safety-info li {
  margin-bottom: 4px;
}

.pack-count {
  color: #909399;
  font-size: 12px;
}

.pack-row {
  display: flex;
  gap: 12px;
  width: 100%;
  align-items: center;
}

.mutation-row,
.channel-row {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.mutation-label {
  font-size: 13px;
  color: #606266;
}

.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
  line-height: 1.5;
  width: 100%;
}

.collapse-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
