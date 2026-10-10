<template>
  <el-tag
    :type="tagType"
    :effect="effect"
    :size="size"
    class="evaluation-type-icon"
  >
    <el-icon v-if="showIcon" class="type-icon">
      <component :is="iconComponent" />
    </el-icon>
    <span>{{ label }}</span>
  </el-tag>
</template>

<script setup>
import { computed } from 'vue'
import {
  Avatar,
  Lock,
  ChatDotRound,
  Document,
  Timer,
  CircleCheck,
  Cpu,
  Warning,
  DataAnalysis
} from '@element-plus/icons-vue'

const props = defineProps({
  type: {
    type: String,
    required: true,
    validator: (value) => [
      'judge', 'consistency', 'success_rate', 'rag', 'latency', 'binary',
      'agent', 'safety', 'dialogue'
    ].includes(value)
  },
  size: {
    type: String,
    default: 'default',
    validator: (value) => ['large', 'default', 'small'].includes(value)
  },
  effect: {
    type: String,
    default: 'light',
    validator: (value) => ['dark', 'light', 'plain'].includes(value)
  },
  showIcon: {
    type: Boolean,
    default: true
  }
})

// 评测类型配置
const typeConfig = {
  judge: {
    label: 'AI 裁判员',
    icon: Avatar,
    type: 'primary'
  },
  consistency: {
    label: '一致性评测',
    icon: DataAnalysis,
    type: 'success'
  },
  success_rate: {
    label: '成功率评测',
    icon: CircleCheck,
    type: 'success'
  },
  rag: {
    label: 'RAG 评测',
    icon: Document,
    type: 'warning'
  },
  latency: {
    label: '耗时评测',
    icon: Timer,
    type: 'info'
  },
  binary: {
    label: '二分分类',
    icon: CircleCheck,
    type: 'info'
  },
  agent: {
    label: 'Agent 评测',
    icon: Cpu,
    type: 'danger'
  },
  safety: {
    label: '安全评测',
    icon: Lock,
    type: 'danger'
  },
  dialogue: {
    label: '对话评测',
    icon: ChatDotRound,
    type: 'primary'
  }
}

// 计算属性
const config = computed(() => {
  return typeConfig[props.type] || {
    label: props.type,
    icon: Warning,
    type: 'info'
  }
})

const label = computed(() => config.value.label)
const iconComponent = computed(() => config.value.icon)
const tagType = computed(() => config.value.type)
</script>

<style scoped>
.evaluation-type-icon {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.type-icon {
  font-size: 14px;
}
</style>
