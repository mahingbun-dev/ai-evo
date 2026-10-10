<template>
  <div class="variable-injector">
    <el-alert
      v-if="variables.length > 0"
      title="该模型的请求链中使用了动态变量，请为每个变量配置值"
      type="info"
      :closable="false"
      style="margin-bottom: 16px"
    />

    <!-- 无变量时 -->
    <div v-if="variables.length === 0" class="empty-tip">
      <span style="color: #909399;">该模型无需配置变量</span>
    </div>

    <!-- 有变量时显示配置 - 每个变量单独配置来源 -->
    <template v-else>
      <el-form-item
        v-for="variable in variables"
        :key="variable"
        :label="variable"
      >
        <div class="variable-row">
          <!-- 数据来源选择 -->
          <el-select
            v-model="variableSources[variable]"
            placeholder="来源"
            style="width: 120px"
            @change="handleSourceChange(variable)"
          >
            <el-option label="手动输入" value="manual" />
            <el-option label="数据集字段" value="dataset" />
          </el-select>
          <el-select
            v-model="variableTypes[variable]"
            placeholder="类型"
            style="width: 100px"
            @change="handleTypeChange(variable)"
          >
            <el-option label="字符串" value="string" />
            <el-option label="数字" value="number" />
            <el-option label="布尔值" value="boolean" />
          </el-select>
          <!-- 手动输入模式 -->
          <template v-if="variableSources[variable] === 'manual'">
            <el-input
              v-model="runtimeVariables[variable]"
              :placeholder="`请输入 ${variable}`"
              style="flex: 1"
            />
          </template>

          <!-- 数据集字段模式 -->
          <template v-else-if="variableSources[variable] === 'dataset'">
            <el-select
              v-model="variableMappings[variable]"
              placeholder="选择字段"
              style="width: 200px"
              filterable
            >
              <el-option
                v-for="field in datasetFields"
                :key="field.name"
                :label="field.label"
                :value="field.name"
              />
            </el-select>

          </template>
        </div>
      </el-form-item>

      <!-- OpenAI/Anthropic user_import 配置 -->
      <template v-if="isOpenAIOrAnthropic">
        <el-divider content-position="left">user_import 配置</el-divider>
        <el-form-item label="启用 user_import">
          <el-switch v-model="localForm.enableUserImport" />
        </el-form-item>
        <el-form-item v-if="localForm.enableUserImport" label="模板格式">
          <el-input
            v-model="localForm.userImportTemplate"
            type="textarea"
            :rows="3"
            placeholder="可选，默认格式: {user_input}\n{answer}"
          />
          <div class="form-tip">可用变量: {'{user_input}'}, {'{answer}'}</div>
        </el-form-item>
      </template>
    </template>
  </div>
</template>

<script setup>
import { ref, watch, computed, toRaw, onMounted } from 'vue'

const props = defineProps({
  variables: {
    type: Array,
    default: () => []
  },
  datasetFields: {
    type: Array,
    default: () => []
  },
  modelProvider: {
    type: String,
    default: ''
  },
  initialData: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['update'])

// 本地状态
const runtimeVariables = ref({})
const variableTypes = ref({})
const variableMappings = ref({})
const variableSources = ref({})  // 每个变量的数据来源: manual 或 dataset
const localForm = ref({
  enableUserImport: false,
  userImportTemplate: ''
})

// 发送更新
const emitUpdate = () => {
  // 处理 runtime_variables，转换为带类型的格式
  const processedRuntimeVars = {}
  for (const [key, value] of Object.entries(runtimeVariables.value)) {
    if (value !== '' && value !== undefined && value !== null) {
      const varType = variableTypes.value[key] || 'string'
      processedRuntimeVars[key] = {
        value: String(value),
        type: varType
      }
    }
  }

  emit('update', {
    runtime_variables: processedRuntimeVars,
    variable_types: variableTypes.value,
    variable_mappings: variableMappings.value,
    variable_sources: variableSources.value,
    enable_user_import: localForm.value.enableUserImport,
    user_import_template: localForm.value.userImportTemplate
  })
}

// 初始化
const init = () => {
  if (props.initialData.runtime_variables) {
    // 处理两种格式：1) { key: "值" }  2) { key: { value: "值", type: "类型" } }
    const rawVars = props.initialData.runtime_variables
    for (const [key, value] of Object.entries(rawVars)) {
      if (typeof value === 'string') {
        // 格式1: { key: "值" }
        runtimeVariables.value[key] = value
      } else if (typeof value === 'object' && value !== null && value.value !== undefined) {
        // 格式2: { key: { value: "值", type: "类型" } }
        runtimeVariables.value[key] = value.value
      }
    }
  }
  if (props.initialData.variable_types) {
    variableTypes.value = { ...props.initialData.variable_types }
  }
  if (props.initialData.variable_mappings) {
    variableMappings.value = { ...props.initialData.variable_mappings }
  }
  if (props.initialData.variable_sources) {
    variableSources.value = { ...props.initialData.variable_sources }
  }
  if (props.initialData.enable_user_import !== undefined) {
    localForm.value.enableUserImport = props.initialData.enable_user_import
  }
  if (props.initialData.user_import_template) {
    localForm.value.userImportTemplate = props.initialData.user_import_template
  }

  // 初始化变量类型和来源
  props.variables.forEach(v => {
    if (!variableTypes.value[v]) {
      variableTypes.value[v] = 'string'
    }
    if (!variableSources.value[v]) {
      variableSources.value[v] = 'manual'  // 默认手动输入
    }
  })

  // 发送初始值
  emitUpdate()
}

onMounted(() => {
  init()
})

// 判断是否为 OpenAI/Anthropic 模型
const isOpenAIOrAnthropic = computed(() => {
  const provider = (props.modelProvider || '').toLowerCase()
  return provider === 'openai' || provider === 'anthropic'
})

// 监听变量变化，重新初始化
watch(() => props.variables, (newVars) => {
  newVars.forEach(v => {
    if (!variableTypes.value[v]) {
      variableTypes.value[v] = 'string'
    }
    if (!variableSources.value[v]) {
      variableSources.value[v] = 'manual'
    }
  })
  emitUpdate()
})

// 处理数据源切换
const handleSourceChange = (variable) => {
  emitUpdate()
}

// 处理类型变化
const handleTypeChange = (variable) => {
  emitUpdate()
}

// 监听本地状态变化
watch([
  () => runtimeVariables.value,
  () => variableMappings.value,
  () => variableSources.value,
  () => localForm.value
], () => {
  emitUpdate()
}, { deep: true })

// 暴露方法供外部调用
defineExpose({
  getData: () => ({
    runtime_variables: toRaw(runtimeVariables.value),
    variable_types: toRaw(variableTypes.value),
    variable_mappings: toRaw(variableMappings.value),
    variable_sources: toRaw(variableSources.value),
    enable_user_import: localForm.value.enableUserImport,
    user_import_template: localForm.value.userImportTemplate
  }),
  reset: () => {
    runtimeVariables.value = {}
    variableMappings.value = {}
    variableSources.value = {}
    localForm.value = {
      enableUserImport: false,
      userImportTemplate: ''
    }
    init()
  }
})
</script>

<style scoped>
.variable-injector {
  padding: 0;
}

.variable-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.variable-row .el-select {
  flex-shrink: 0;
}

.variable-row .el-input {
  flex: 1;
}

.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.empty-tip {
  padding: 16px;
  text-align: center;
}
</style>
