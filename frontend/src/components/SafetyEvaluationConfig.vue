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
        title="安全评测说明"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 20px"
      >
        <template #default>
          <div class="safety-info">
            <p>安全评测用于检测模型输出中的潜在风险内容，包括：</p>
            <ul>
              <li><strong>偏见检测</strong>：识别性别、种族、年龄等偏见内容</li>
              <li><strong>毒性检测</strong>：检测攻击性、侮辱性或有害内容</li>
              <li><strong>PII 泄露</strong>：识别个人身份信息泄露风险</li>
              <li><strong>不当建议</strong>：检测可能造成伤害的建议内容</li>
              <li><strong>滥用检测</strong>：识别模型被滥用的情况</li>
              <li><strong>角色违规</strong>：检测是否违反预设角色约束</li>
            </ul>
          </div>
        </template>
      </el-alert>

      <!-- 评测指标选择 -->
      <DeepEvalMetricSelector
        v-model="formData.metrics"
        category="safety"
        label="安全评测指标"
        :required="true"
        placeholder="请选择安全评测指标"
      />

      <!-- 评测阈值 -->
      <el-form-item label="评测阈值" prop="threshold">
        <div class="threshold-container">
          <el-slider
            v-model="formData.threshold"
            :min="0"
            :max="1"
            :step="0.05"
            :format-tooltip="formatThreshold"
            style="flex: 1"
          />
          <span class="threshold-value">{{ formData.threshold.toFixed(2) }}</span>
        </div>
        <div class="form-tip">
          <p>阈值说明：</p>
          <ul>
            <li><strong>0.3</strong>：宽松模式，仅检测明显的安全问题</li>
            <li><strong>0.5</strong>：标准模式，适用于大多数场景（推荐）</li>
            <li><strong>0.7</strong>：严格模式，适用于高安全要求场景</li>
            <li><strong>0.9</strong>：极高要求，适用于金融、医疗等敏感领域</li>
          </ul>
        </div>
      </el-form-item>

      <!-- 高级配置 -->
      <el-collapse v-model="activeCollapse">
        <el-collapse-item title="高级配置" name="advanced">
          <template #title>
            <div class="collapse-title">
              <el-icon><Setting /></el-icon>
              <span>高级配置</span>
            </div>
          </template>

          <el-form-item label="评测说明（可选）">
            <el-input
              v-model="formData.evaluationNote"
              type="textarea"
              :rows="3"
              placeholder="请输入安全评测的特殊要求或说明，例如：重点关注医疗健康相关内容的安全性"
            />
          </el-form-item>
        </el-collapse-item>
      </el-collapse>
    </el-form>
  </div>
</template>

<script setup>
import { ref, reactive, watch, onMounted } from 'vue'
import { Setting } from '@element-plus/icons-vue'
import DeepEvalMetricSelector from './DeepEvalMetricSelector.vue'

const props = defineProps({
  initialData: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['update'])

const formRef = ref(null)
const activeCollapse = ref([])

const formData = reactive({
  metrics: [],
  threshold: 0.5,
  evaluationNote: ''
})

const rules = {
  metrics: [
    { required: true, type: 'array', message: '请选择至少一个安全评测指标', trigger: 'change' }
  ],
  threshold: [
    { required: true, message: '请设置评测阈值', trigger: 'change' }
  ]
}

// 格式化阈值提示
const formatThreshold = (value) => {
  return `阈值: ${value.toFixed(2)}`
}

// 发送更新
const emitUpdate = () => {
  emit('update', {
    metrics: formData.metrics,
    threshold: formData.threshold,
    evaluation_note: formData.evaluationNote
  })
}

// 监听表单变化
watch(
  () => formData,
  () => {
    emitUpdate()
  },
  { deep: true }
)

// 初始化
onMounted(() => {
  if (props.initialData) {
    if (props.initialData.metrics) {
      formData.metrics = [...props.initialData.metrics]
    }
    if (props.initialData.threshold !== undefined) {
      formData.threshold = props.initialData.threshold
    }
    if (props.initialData.evaluation_note) {
      formData.evaluationNote = props.initialData.evaluation_note
    }
  }
})

// 暴露方法供外部调用
defineExpose({
  validate: async () => {
    if (formRef.value) {
      return await formRef.value.validate()
    }
    return false
  },
  getData: () => ({
    metrics: formData.metrics,
    threshold: formData.threshold,
    evaluation_note: formData.evaluationNote
  }),
  reset: () => {
    formData.metrics = []
    formData.threshold = 0.5
    formData.evaluationNote = ''
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

.threshold-container {
  display: flex;
  align-items: center;
  gap: 16px;
  width: 100%;
}

.threshold-value {
  font-size: 16px;
  font-weight: 600;
  color: #e6a23c;
  min-width: 50px;
  text-align: center;
}

.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
  line-height: 1.5;
}

.form-tip p {
  margin: 0 0 4px 0;
}

.form-tip ul {
  margin: 0;
  padding-left: 16px;
}

.form-tip li {
  margin-bottom: 2px;
}

.collapse-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
