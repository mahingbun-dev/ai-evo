<template>
  <div class="deepeval-metric-selector">
    <el-form-item :label="label" :required="required">
      <el-select
        v-model="selectedMetrics"
        multiple
        filterable
        :placeholder="placeholder"
        style="width: 100%"
        @change="handleChange"
      >
        <el-option-group
          v-for="group in groupedMetrics"
          :key="group.category"
          :label="group.label"
        >
          <el-option
            v-for="metric in group.metrics"
            :key="metric.key"
            :label="metric.name"
            :value="metric.key"
          >
            <div class="metric-option">
              <span class="metric-name">{{ metric.name }}</span>
              <span class="metric-desc">{{ metric.description }}</span>
            </div>
          </el-option>
        </el-option-group>
      </el-select>
      <div v-if="showDescription && selectedMetrics.length > 0" class="selected-descriptions">
        <div
          v-for="metricKey in selectedMetrics"
          :key="metricKey"
          class="description-item"
        >
          <el-tag size="small" type="info">{{ getMetricName(metricKey) }}</el-tag>
          <span class="desc-text">{{ getMetricDescription(metricKey) }}</span>
        </div>
      </div>
    </el-form-item>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'

const props = defineProps({
  modelValue: {
    type: Array,
    default: () => []
  },
  category: {
    type: String,
    default: 'all',
    validator: (value) => ['agent', 'safety', 'rag', 'conversation', 'all'].includes(value)
  },
  label: {
    type: String,
    default: '评测指标'
  },
  placeholder: {
    type: String,
    default: '请选择评测指标'
  },
  required: {
    type: Boolean,
    default: false
  },
  showDescription: {
    type: Boolean,
    default: true
  }
})

const emit = defineEmits(['update:modelValue'])

// DeepEval 指标配置（与后端 deepeval_metric_registry.py 对应）
const allMetrics = [
  // Agent 评测指标
  { key: 'task_completion', name: '任务完成度', category: 'agent', description: '评估 Agent 是否成功完成了指定任务' },
  { key: 'tool_correctness', name: '工具正确性', category: 'agent', description: '评估 Agent 是否选择了正确的工具' },
  { key: 'step_efficiency', name: '步骤效率', category: 'agent', description: '评估 Agent 完成任务的步骤是否高效' },
  { key: 'argument_correctness', name: '参数正确性', category: 'agent', description: '评估 Agent 调用工具时的参数是否正确' },
  { key: 'plan_adherence', name: '计划遵循度', category: 'agent', description: '评估 Agent 是否遵循了既定计划' },
  { key: 'plan_quality', name: '计划质量', category: 'agent', description: '评估 Agent 制定的计划质量' },
  // 安全评测指标
  { key: 'bias', name: '偏见检测', category: 'safety', description: '检测输出中的偏见内容' },
  { key: 'toxicity', name: '毒性检测', category: 'safety', description: '检测输出中的有害或攻击性内容' },
  { key: 'pii_leakage', name: 'PII 泄露检测', category: 'safety', description: '检测输出中是否泄露了个人身份信息' },
  { key: 'non_advice', name: '非建议检测', category: 'safety', description: '检测输出是否提供了不当建议' },
  { key: 'misuse', name: '滥用检测', category: 'safety', description: '检测模型是否被滥用' },
  { key: 'role_violation', name: '角色违规检测', category: 'safety', description: '检测是否违反了预设角色约束' },
  // RAG 评测指标
  { key: 'answer_relevancy', name: '答案相关性', category: 'rag', description: '评估生成的答案与问题的相关程度' },
  { key: 'faithfulness', name: '忠实度', category: 'rag', description: '评估生成的答案是否忠实于提供的上下文' },
  { key: 'hallucination', name: '幻觉检测', category: 'rag', description: '检测生成内容中的幻觉信息' },
  { key: 'contextual_precision', name: '上下文精确度', category: 'rag', description: '评估检索到的上下文的精确程度' },
  { key: 'contextual_recall', name: '上下文召回率', category: 'rag', description: '评估检索到的上下文的召回率' },
  { key: 'contextual_relevancy', name: '上下文相关性', category: 'rag', description: '评估检索到的上下文与问题的相关程度' },
  // 对话评测指标
  { key: 'knowledge_retention', name: '知识保持度', category: 'conversation', description: '评估对话中知识的保持能力' },
  { key: 'goal_accuracy', name: '目标准确度', category: 'conversation', description: '评估对话是否达到了预期目标' }
]

const categoryLabels = {
  agent: 'Agent 评测',
  safety: '安全评测',
  rag: 'RAG 评测',
  conversation: '对话评测'
}

const selectedMetrics = ref([])

// 根据 category 过滤指标
const filteredMetrics = computed(() => {
  if (props.category === 'all') {
    return allMetrics
  }
  return allMetrics.filter(m => m.category === props.category)
})

// 按类别分组
const groupedMetrics = computed(() => {
  const groups = {}
  filteredMetrics.value.forEach(metric => {
    if (!groups[metric.category]) {
      groups[metric.category] = {
        category: metric.category,
        label: categoryLabels[metric.category] || metric.category,
        metrics: []
      }
    }
    groups[metric.category].metrics.push(metric)
  })
  return Object.values(groups)
})

// 获取指标名称
const getMetricName = (key) => {
  const metric = allMetrics.find(m => m.key === key)
  return metric ? metric.name : key
}

// 获取指标描述
const getMetricDescription = (key) => {
  const metric = allMetrics.find(m => m.key === key)
  return metric ? metric.description : ''
}

// 处理选择变化
const handleChange = (value) => {
  emit('update:modelValue', value)
}

// 监听外部值变化
watch(() => props.modelValue, (newVal) => {
  selectedMetrics.value = [...newVal]
}, { immediate: true })

onMounted(() => {
  if (props.modelValue) {
    selectedMetrics.value = [...props.modelValue]
  }
})
</script>

<style scoped>
.deepeval-metric-selector {
  width: 100%;
}

.metric-option {
  display: flex;
  flex-direction: column;
  padding: 2px 0;
}

.metric-name {
  font-weight: 500;
  color: #303133;
}

.metric-desc {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.selected-descriptions {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.description-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.desc-text {
  font-size: 12px;
  color: #606266;
}
</style>
