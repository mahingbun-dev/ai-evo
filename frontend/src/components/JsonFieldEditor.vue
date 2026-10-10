<template>
  <div class="json-field-editor">
    <!-- 粘贴JSON按钮 -->
    <div v-if="!readonly" class="paste-json-bar">
      <el-button type="info" plain size="small" @click="showPasteDialog = true">
        <el-icon><DocumentCopy /></el-icon> 粘贴JSON
      </el-button>
      <span class="paste-hint">粘贴JSON字符串自动解析为配置</span>
    </div>

    <div v-if="fields.length === 0" class="empty-tip">
      <el-button type="primary" plain size="small" @click="addField">+ 添加字段</el-button>
    </div>

    <div v-for="(field, index) in fields" :key="index" class="field-item">
      <!-- 对象类型字段 -->
      <template v-if="field.type === 'object'">
        <div class="field-row object-field">
          <el-button link type="primary" @click="field.isExpanded = !field.isExpanded">
            <el-icon>
              <ArrowRight v-if="!field.isExpanded" />
              <ArrowDown v-else />
            </el-icon>
          </el-button>
          <el-input v-if="!readonly" v-model="field.key" placeholder="字段名" class="field-key" @change="emitChange" />
          <span v-else class="field-key-text">{{ field.key }}</span>
          <el-select v-if="!readonly" v-model="field.type" class="field-type" @change="handleTypeChange(field)">
            <el-option v-for="opt in typeOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <span class="field-type-text">{{ getTypeLabel(field.type) }}</span>
          <el-button link type="danger" @click="removeField(index)" :disabled="readonly">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
        <div v-if="field.isExpanded" class="field-children">
          <JsonFieldEditor
            :fields="field.children || []"
            :readonly="readonly"
            :is-nested="true"
            @update:fields="field.children = $event; emitChange()"
            @add="addChildField(field, $event)"
            @remove="removeChildField(field, $event)"
          />
          <el-button v-if="!readonly" type="primary" plain size="small" @click="addChildField(field)" style="margin-top: 8px;">
            + 添加子字段
          </el-button>
        </div>
      </template>

      <!-- 数组类型字段 -->
      <template v-else-if="field.type === 'array'">
        <div class="field-row array-field">
          <el-button link type="primary" @click="field.isExpanded = !field.isExpanded">
            <el-icon>
              <ArrowRight v-if="!field.isExpanded" />
              <ArrowDown v-else />
            </el-icon>
          </el-button>
          <el-input v-if="!readonly" v-model="field.key" placeholder="字段名" class="field-key" @change="emitChange" />
          <span v-else class="field-key-text">{{ field.key }}</span>
          <el-select v-if="!readonly" v-model="field.type" class="field-type" @change="handleTypeChange(field)">
            <el-option v-for="opt in typeOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <span class="field-type-text">{{ getTypeLabel(field.type) }}</span>
          <el-button link type="danger" @click="removeField(index)" :disabled="readonly">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
        <div v-if="field.isExpanded" class="field-children array-children">
          <div v-for="(item, arrIndex) in (field.value || [])" :key="arrIndex" class="array-item">
            <template v-if="typeof item === 'object' && item !== null && item.type === 'object'">
              <div class="array-item-header">
                <span class="array-index">[{{ arrIndex }}]</span>
                <el-button link type="primary" @click="item.isExpanded = !item.isExpanded">
                  <el-icon><ArrowRight v-if="!item.isExpanded" /><ArrowDown v-else /></el-icon>
                </el-button>
                <el-button link type="danger" size="small" @click="removeArrayItem(field, arrIndex)" :disabled="readonly">删除</el-button>
              </div>
              <div v-if="item.isExpanded" class="field-children">
                <JsonFieldEditor
                  :fields="item.children || []"
                  :readonly="readonly"
                  :is-nested="true"
                  @update:fields="item.children = $event; emitChange()"
                  @add="addArrayChildField(item, $event)"
                  @remove="removeArrayChildField(item, $event)"
                />
              </div>
            </template>
            <template v-else>
              <div class="array-item-row">
                <span class="array-index">[{{ arrIndex }}]</span>
                <el-select v-if="!readonly" v-model="item.type" class="field-type" size="small" @change="emitChange">
                  <el-option label="字符串" value="string" />
                  <el-option label="数字" value="number" />
                  <el-option label="布尔值" value="boolean" />
                  <el-option label="对象" value="object" />
                </el-select>
                <el-input
                  v-if="item.type !== 'object'"
                  v-model="item.value"
                  :placeholder="getPlaceholder(item.type)"
                  class="field-value"
                  size="small"
                  :type="item.type === 'number' ? 'number' : 'text'"
                  @input="emitChange"
                />
                <el-button v-if="item.type === 'object'" type="primary" plain size="small" @click="addArrayObjectChild(item)">
                  + 添加对象属性
                </el-button>
                <el-button link type="danger" @click="removeArrayItem(field, arrIndex)" :disabled="readonly">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </template>
          </div>
          <el-button v-if="!readonly" type="primary" plain size="small" @click="addArrayItem(field)" style="margin-top: 8px;">
            + 添加数组项
          </el-button>
        </div>
      </template>

      <!-- 基本类型字段 -->
      <template v-else>
        <div class="field-row">
          <el-input v-if="!readonly" v-model="field.key" placeholder="字段名" class="field-key" @input="emitChange" />
          <span v-else class="field-key-text">{{ field.key }}</span>
          <el-select v-if="!readonly" v-model="field.type" class="field-type" @change="handleTypeChange(field)">
            <el-option v-for="opt in typeOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <span v-else class="field-type-text">{{ getTypeLabel(field.type) }}</span>

          <!-- 根据类型显示不同的输入框 -->
          <template v-if="field.type === 'boolean'">
            <el-switch v-model="field.value" @change="emitChange" :disabled="readonly" />
          </template>
          <template v-else-if="field.type === 'null'">
            <el-tag size="small">null</el-tag>
          </template>
          <template v-else>
            <el-input
              v-model="field.value"
              :placeholder="getPlaceholder(field.type)"
              class="field-value"
              @input="emitChange"
              :type="field.type === 'number' ? 'number' : 'text'"
              :disabled="readonly"
            />
          </template>

          <el-button v-if="!readonly" link type="danger" @click="removeField(index)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </template>
    </div>

    <el-button v-if="fields.length > 0 && !readonly" type="primary" plain size="small" @click="addField" style="margin-top: 8px;">
      + 添加字段
    </el-button>

    <!-- 粘贴JSON弹窗 -->
    <el-dialog v-model="showPasteDialog" title="粘贴JSON字符串" width="650px" append-to-body @open="handleDialogOpen">
      <el-alert type="info" :closable="false" style="margin-bottom: 15px;">
        请粘贴 JSON 格式的字符串，系统将自动解析并填充到配置中。
      </el-alert>
      <div class="json-input-container">
        <div class="json-input-toolbar">
          <el-button size="small" @click="handleFormatJson" :disabled="!pasteJsonText.trim()">
            <el-icon><Document /></el-icon> 格式化
          </el-button>
          <el-button size="small" @click="handleClearJson">
            <el-icon><Delete /></el-icon> 清空
          </el-button>
          <span class="validation-status" :class="jsonValidationStatus.class">
            <el-icon v-if="jsonValidationStatus.valid === true"><CircleCheck /></el-icon>
            <el-icon v-else-if="jsonValidationStatus.valid === false"><CircleClose /></el-icon>
            {{ jsonValidationStatus.text }}
          </span>
        </div>
        <el-input
          v-model="pasteJsonText"
          type="textarea"
          :rows="10"
          placeholder='{"key1": "value1", "key2": 123}'
          style="font-family: monospace;"
          @input="validateJsonInput"
          @blur="handleFormatJson"
        />
      </div>
      <div class="import-mode-select" v-if="fields.length > 0">
        <el-radio-group v-model="importMode">
          <el-radio value="merge">合并（追加新字段，保留已有值）</el-radio>
          <el-radio value="replace">覆盖（清空后完全替换）</el-radio>
        </el-radio-group>
      </div>
      <template #footer>
        <el-button @click="showPasteDialog = false">取消</el-button>
        <el-button type="primary" @click="handleParseJson" :disabled="!jsonValidationStatus.valid">解析并导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { Delete, ArrowRight, ArrowDown, DocumentCopy, Document, CircleCheck, CircleClose } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  fields: {
    type: Array,
    default: () => []
  },
  readonly: {
    type: Boolean,
    default: false
  },
  isNested: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['update:fields', 'add', 'remove'])

// 粘贴JSON相关
const showPasteDialog = ref(false)
const pasteJsonText = ref('')
const jsonValidationStatus = ref({ valid: null, text: '', class: '' })
const importMode = ref('merge')  // 'merge' | 'replace'

// 弹窗打开时，如果有已有配置，转换为JSON字符串填充
const handleDialogOpen = () => {
  if (props.fields.length > 0) {
    // 将现有配置转换为JSON对象再转字符串
    const jsonObj = configToJsonForExport(props.fields)
    pasteJsonText.value = JSON.stringify(jsonObj, null, 2)
    jsonValidationStatus.value = { valid: true, text: 'JSON格式正确', class: 'valid' }
  } else {
    pasteJsonText.value = ''
    jsonValidationStatus.value = { valid: null, text: '', class: '' }
  }
  importMode.value = 'merge'
}

// 将配置数组转换为JSON对象（用于导出）
const configToJsonForExport = (fields) => {
  const result = {}

  const processField = (field) => {
    switch (field.type) {
      case 'string':
      case 'number':
      case 'boolean':
        return field.value
      case 'null':
        return null
      case 'array':
        return (field.value || []).map(item => {
          if (item.type === 'object' && item.children) {
            return configToJsonForExport(item.children)
          }
          return item.value
        })
      case 'object':
        return configToJsonForExport(field.children || [])
      default:
        return field.value
    }
  }

  for (const field of fields) {
    result[field.key] = processField(field)
  }

  return result
}

const validateJsonInput = () => {
  const text = pasteJsonText.value.trim()
  if (!text) {
    jsonValidationStatus.value = { valid: null, text: '', class: '' }
    return
  }
  try {
    JSON.parse(text)
    jsonValidationStatus.value = { valid: true, text: 'JSON格式正确', class: 'valid' }
  } catch (e) {
    jsonValidationStatus.value = { valid: false, text: '格式错误: ' + e.message, class: 'invalid' }
  }
}

const handleFormatJson = () => {
  const text = pasteJsonText.value.trim()
  if (!text) return
  try {
    const parsed = JSON.parse(text)
    pasteJsonText.value = JSON.stringify(parsed, null, 2)
    jsonValidationStatus.value = { valid: true, text: 'JSON格式正确', class: 'valid' }
  } catch (e) {
    // 格式化失败时不清空，保持原内容让用户修正
  }
}

const handleClearJson = () => {
  pasteJsonText.value = ''
  jsonValidationStatus.value = { valid: null, text: '', class: '' }
}

const handleParseJson = () => {
  if (!pasteJsonText.value.trim()) {
    ElMessage.warning('请输入JSON字符串')
    return
  }

  try {
    const parsed = JSON.parse(pasteJsonText.value)

    // 将解析后的JSON对象转换为配置数组
    const newFields = jsonToConfigFromPaste(parsed)

    if (importMode.value === 'replace') {
      // 覆盖模式：清空后完全替换
      props.fields.splice(0, props.fields.length, ...newFields)
      ElMessage.success(`已使用 ${newFields.length} 个字段覆盖原有配置`)
    } else {
      // 合并模式：如果已存在同名字段，保留现有值；否则添加新字段
      let addedCount = 0
      newFields.forEach(newField => {
        const existingIndex = props.fields.findIndex(f => f.key === newField.key)
        if (existingIndex === -1) {
          props.fields.push(newField)
          addedCount++
        }
      })
      if (addedCount === 0) {
        ElMessage.info('没有新增字段（所有字段已存在）')
      } else {
        ElMessage.success(`成功导入了 ${addedCount} 个新字段`)
      }
    }

    showPasteDialog.value = false
    pasteJsonText.value = ''
    jsonValidationStatus.value = { valid: null, text: '', class: '' }
    emitChange()
  } catch (e) {
    ElMessage.error('JSON格式错误：' + e.message)
  }
}

const jsonToConfigFromPaste = (jsonObj) => {
  // 将JSON对象转换为配置数组（用于粘贴导入）
  if (!jsonObj || typeof jsonObj !== 'object') {
    return []
  }

  const config = []

  const parseValue = (value) => {
    if (value === null) {
      return { type: 'null', value: 'null' }
    } else if (Array.isArray(value)) {
      return {
        type: 'array',
        value: value.map((item) => {
          if (typeof item === 'object' && item !== null) {
            return {
              type: 'object',
              children: jsonToConfigFromPaste(item),
              isExpanded: true
            }
          }
          return { type: getType(item), value: String(item) }
        }),
        isExpanded: true
      }
    } else if (typeof value === 'object') {
      return {
        type: 'object',
        children: jsonToConfigFromPaste(value),
        isExpanded: true
      }
    } else {
      return { type: getType(value), value: String(value) }
    }
  }

  const getType = (val) => {
    if (val === null) return 'null'
    if (typeof val === 'number') return 'number'
    if (typeof val === 'boolean') return 'boolean'
    if (Array.isArray(val)) return 'array'
    if (typeof val === 'object') return 'object'
    return 'string'
  }

  for (const key in jsonObj) {
    const value = jsonObj[key]
    config.push({
      key: key,
      ...parseValue(value)
    })
  }

  return config
}

const typeOptions = [
  { label: '字符串', value: 'string' },
  { label: '数字', value: 'number' },
  { label: '布尔值', value: 'boolean' },
  { label: '对象', value: 'object' },
  { label: '数组', value: 'array' },
  { label: 'null', value: 'null' }
]

const getTypeLabel = (type) => {
  const found = typeOptions.find(opt => opt.value === type)
  return found ? found.label : type
}

const getPlaceholder = (type) => {
  switch (type) {
    case 'string': return '请输入字符串'
    case 'number': return '请输入数字'
    default: return '请输入值'
  }
}

const addField = () => {
  // 直接添加字段并触发更新
  props.fields.push({
    key: `field_${props.fields.length + 1}`,
    type: 'string',
    value: ''
  })
  emit('update:fields', props.fields)
  emitChange()
}

const removeField = (index) => {
  // 直接删除字段并触发更新
  props.fields.splice(index, 1)
  emit('update:fields', props.fields)
  emitChange()
}

const addChildField = (parent, config = null) => {
  if (!parent.children) {
    parent.children = []
  }
  if (config) {
    parent.children.push(config)
  } else {
    parent.children.push({
      key: `field_${parent.children.length + 1}`,
      type: 'string',
      value: ''
    })
  }
  emit('update:fields', props.fields)
}

const removeChildField = (parent, index) => {
  parent.children.splice(index, 1)
  emit('update:fields', props.fields)
}

const addArrayItem = (field) => {
  if (!field.value) {
    field.value = []
  }
  field.value.push({
    type: 'string',
    value: ''
  })
  emit('update:fields', props.fields)
}

const removeArrayItem = (field, index) => {
  field.value.splice(index, 1)
  emit('update:fields', props.fields)
}

const addArrayObjectChild = (item) => {
  if (!item.children) {
    item.children = []
  }
  item.children.push({
    key: `field_${item.children.length + 1}`,
    type: 'string',
    value: ''
  })
  emit('update:fields', props.fields)
}

const addArrayChildField = (parent, config = null) => {
  if (!parent.children) {
    parent.children = []
  }
  if (config) {
    parent.children.push(config)
  } else {
    parent.children.push({
      key: `field_${parent.children.length + 1}`,
      type: 'string',
      value: ''
    })
  }
  emit('update:fields', props.fields)
}

const removeArrayChildField = (parent, index) => {
  parent.children.splice(index, 1)
  emit('update:fields', props.fields)
}

const handleTypeChange = (field) => {
  // 类型改变时重置值
  if (field.type === 'object') {
    field.children = []
    field.isExpanded = true
  } else if (field.type === 'array') {
    field.value = []
    field.isExpanded = true
  } else if (field.type === 'boolean') {
    field.value = false
  } else if (field.type === 'null') {
    field.value = 'null'
  } else {
    field.value = ''
  }
  emitChange()
}

const emitChange = () => {
  emit('update:fields', props.fields)
}
</script>

<style scoped>
.json-field-editor {
  width: 100%;
  max-height: 400px;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 12px;
  background: #fafafa;
  border-radius: 4px;
  border: 1px solid #ebeef5;
}

.empty-tip {
  padding: 10px;
  color: #909399;
  text-align: center;
}

.paste-json-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: #f0f9ff;
  border-radius: 4px;
  margin-bottom: 10px;
  border: 1px dashed #b3d8ff;
}

.paste-hint {
  font-size: 12px;
  color: #909399;
}

.json-input-container {
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
}

.json-input-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #f5f7fa;
  border-bottom: 1px solid #dcdfe6;
}

.validation-status {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  margin-left: auto;
}

.validation-status.valid {
  color: #67c23a;
}

.validation-status.invalid {
  color: #f56c6c;
}

.json-input-container .el-textarea__inner {
  border: none;
  border-radius: 0;
}

.json-input-container .el-textarea__inner:focus {
  box-shadow: none;
}

.import-mode-select {
  margin-top: 12px;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  border: 1px solid #e4e7ed;
}

.import-mode-select .el-radio-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.field-item {
  margin-bottom: 8px;
}

.field-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: #fff;
  border-radius: 4px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}

.field-key {
  width: 140px;
  flex-shrink: 0;
}

.field-key-text {
  width: 140px;
  font-weight: 500;
  color: #606266;
  flex-shrink: 0;
}

.field-type {
  width: 90px;
  flex-shrink: 0;
}

.field-type-text {
  width: 60px;
  color: #909399;
  font-size: 12px;
  flex-shrink: 0;
}

.field-value {
  flex: 1;
  min-width: 120px;
}

.field-value .el-input__wrapper {
  width: 100%;
}

.field-children {
  margin-left: 20px;
  padding: 10px;
  background: #f0f2f5;
  border-radius: 4px;
  margin-top: 4px;
  border: 1px dashed #dcdfe6;
}

.array-children {
  margin-left: 0;
}

.array-item {
  margin-bottom: 8px;
  padding: 10px;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}

.array-item-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.array-index {
  font-weight: 500;
  color: #409eff;
  flex-shrink: 0;
}

.array-item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.array-item-row .field-type {
  width: 80px;
  flex-shrink: 0;
}

.array-item-row .field-value {
  flex: 1;
  min-width: 100px;
}

.object-field, .array-field {
  background: #ecf5ff;
  padding: 8px;
  border-radius: 4px;
  border: 1px solid #b3d8ff;
}

/* 添加滚动条样式 */
.json-field-editor::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

.json-field-editor::-webkit-scrollbar-thumb {
  background: #c0c4cc;
  border-radius: 3px;
}

.json-field-editor::-webkit-scrollbar-track {
  background: #f5f7fa;
  border-radius: 3px;
}
</style>
