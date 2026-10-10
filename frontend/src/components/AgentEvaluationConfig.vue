<template>
  <div class="agent-evaluation-config">
    <el-form
      ref="formRef"
      :model="formData"
      label-width="120px"
      label-position="top"
    >
      <!-- 会话背景说明 -->
      <el-form-item label="会话背景说明">
        <el-input
          v-model="formData.sessionContext"
          type="textarea"
          :rows="4"
          placeholder="描述该批会话的业务背景与评测侧重点，例如：客服售后场景，重点关注问题解决效率与服务态度"
        />
        <div class="form-tip">
          会话背景说明会注入裁判提示词，帮助裁判结合业务背景对长会话进行评分（非必填）
        </div>
      </el-form-item>
    </el-form>
  </div>
</template>

<script setup>
import { ref, reactive, watch, onMounted } from 'vue'

const props = defineProps({
  initialData: {
    type: Object,
    default: () => ({})
  }
})

const emit = defineEmits(['update'])

const formRef = ref(null)

const formData = reactive({
  sessionContext: ''
})

// 发送更新
const emitUpdate = () => {
  emit('update', {
    session_context: formData.sessionContext
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
    if (props.initialData.session_context) {
      formData.sessionContext = props.initialData.session_context
    }
  }
})

// 暴露方法供外部调用
defineExpose({
  validate: async () => {
    // 会话背景说明非必填，无校验规则，直接通过
    if (formRef.value) {
      return await formRef.value.validate()
    }
    return false
  },
  getData: () => ({
    session_context: formData.sessionContext
  }),
  reset: () => {
    formData.sessionContext = ''
  }
})
</script>

<style scoped>
.agent-evaluation-config {
  padding: 0;
}

.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
  line-height: 1.5;
}
</style>
