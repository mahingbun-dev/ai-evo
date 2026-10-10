<template>
  <div class="chain-variable-panel">
    <div class="panel-header">
      <span class="panel-title">变量面板</span>
      <el-button link @click="$emit('close')" title="关闭变量面板">
        <el-icon><Close /></el-icon>
      </el-button>
    </div>

    <div class="panel-content">
      <!-- 请求链变量 -->
      <div class="var-section" v-if="chainVariables.length > 0">
        <div class="section-title">请求链变量</div>
        <div class="var-tree">
          <div v-for="group in chainVariables" :key="group.name" class="var-group">
            <div class="group-header">
              <el-icon><Folder /></el-icon>
              <span>{{ group.name }}</span>
            </div>
            <div class="group-children">
              <div
                v-for="item in group.children"
                :key="item.name"
                class="var-item"
                draggable="true"
                @dragstart="handleDragStart($event, group.name, item.name)"
                @click="handleVarClick(group.name, item.name)"
                :title="`${item.name}: ${item.value}`"
              >
                <el-icon><Document /></el-icon>
                <span class="var-name">{{ item.name }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 运行时变量 -->
      <div class="var-section" v-if="runtimeVariables.length > 0">
        <div class="section-title">运行时变量</div>
        <div class="var-list">
          <div
            v-for="item in runtimeVariables"
            :key="item.name"
            class="var-item"
            draggable="true"
            @dragstart="handleRuntimeDragStart($event, item.name)"
            @click="handleRuntimeClick(item.name)"
            :title="item.value"
          >
            <el-icon><Clock /></el-icon>
            <span class="var-name">{{ item.name }}</span>
          </div>
        </div>
      </div>

      <!-- 空状态 -->
      <div v-if="chainVariables.length === 0 && runtimeVariables.length === 0" class="empty-state">
        <el-icon><Warning /></el-icon>
        <span>暂无变量</span>
      </div>
    </div>

    <div class="panel-footer">
      <span class="hint">拖拽变量到输入框插入引用</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { Folder, Document, Clock, Warning, Close } from '@element-plus/icons-vue'

const props = defineProps({
  // 请求链变量树形结构
  chainVariables: {
    type: Array,
    default: () => []
  },
  // 运行时变量列表
  runtimeVariables: {
    type: Array,
    default: () => []
  },
  // 是否显示
  visible: {
    type: Boolean,
    default: true
  }
})

const emit = defineEmits(['drag-start', 'var-click', 'runtime-drag-start', 'runtime-click', 'close'])

// 拖拽请求链变量
const handleDragStart = (event, groupName, varName) => {
  const varRef = `{{${groupName}.${varName}}}`
  event.dataTransfer.setData('text/plain', varRef)
  event.dataTransfer.setData('application/x-chain-var', JSON.stringify({
    type: 'chain',
    group: groupName,
    name: varName,
    ref: varRef
  }))
  emit('drag-start', { groupName, varName, ref: varRef })
}

// 点击请求链变量
const handleVarClick = (groupName, varName) => {
  const varRef = `{{${groupName}.${varName}}}`
  emit('var-click', { groupName, varName, ref: varRef })
}

// 拖拽运行时变量
const handleRuntimeDragStart = (event, varName) => {
  const varRef = `{{${varName}}}`
  event.dataTransfer.setData('text/plain', varRef)
  event.dataTransfer.setData('application/x-chain-var', JSON.stringify({
    type: 'runtime',
    name: varName,
    ref: varRef
  }))
  emit('runtime-drag-start', { name: varName, ref: varRef })
}

// 点击运行时变量
const handleRuntimeClick = (varName) => {
  const varRef = `{{${varName}}}`
  emit('runtime-click', { name: varName, ref: varRef })
}
</script>

<style scoped>
.chain-variable-panel {
  width: 220px;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  background: #FAFAFA;
  display: flex;
  flex-direction: column;
}

.panel-header {
  padding: 10px 12px;
  border-bottom: 1px solid #E5E7EB;
  background: #F5F5F5;
  border-radius: 6px 6px 0 0;
}

.panel-title {
  font-weight: 600;
  font-size: 13px;
  color: #374151;
}

.panel-content {
  flex: 1;
  padding: 10px 8px;
  overflow-y: auto;
  max-height: 400px;
}

.var-section {
  margin-bottom: 12px;
}

.var-section:last-child {
  margin-bottom: 0;
}

.section-title {
  font-size: 11px;
  font-weight: 600;
  color: #6B7280;
  text-transform: uppercase;
  margin-bottom: 6px;
  padding-left: 4px;
}

.var-tree {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.var-group {
  display: flex;
  flex-direction: column;
}

.group-header {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  font-size: 12px;
  font-weight: 500;
  color: #4B5563;
  background: #F3F4F6;
  border-radius: 4px;
  cursor: default;
}

.group-header .el-icon {
  font-size: 12px;
  color: #6B7280;
}

.group-children {
  display: flex;
  flex-direction: column;
  margin-left: 16px;
  padding: 2px 0;
  border-left: 1px dashed #D1D5DB;
}

.var-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  font-size: 12px;
  color: #374151;
  cursor: grab;
  border-radius: 4px;
  transition: background 0.15s;
}

.var-item:hover {
  background: #E5E7EB;
}

.var-item:active {
  cursor: grabbing;
}

.var-item .el-icon {
  font-size: 12px;
  color: #9CA3AF;
}

.var-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.var-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 20px;
  color: #9CA3AF;
  font-size: 12px;
  gap: 8px;
}

.empty-state .el-icon {
  font-size: 24px;
}

.panel-footer {
  padding: 8px 12px;
  border-top: 1px solid #E5E7EB;
  background: #F5F5F5;
  border-radius: 0 0 6px 6px;
}

.hint {
  font-size: 11px;
  color: #9CA3AF;
}
</style>
