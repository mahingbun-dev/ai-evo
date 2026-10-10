<template>
  <div class="ref-textarea">
    <!-- 多行模式：原生 textarea（便于精确读写光标位置），视觉对齐 Element Plus 输入框 -->
    <textarea
      v-if="!isSingleLine"
      ref="fieldRef"
      class="ref-textarea__field"
      :value="modelValue || ''"
      :placeholder="placeholder"
      :rows="rows"
      :disabled="disabled"
      @input="handleInput"
      @keydown="handleKeydown"
      @blur="handleBlur"
      @compositionstart="composing = true"
      @compositionend="handleCompositionEnd"
    ></textarea>
    <!-- 单行模式：等级描述（score_criteria[等级].desc）小输入框 -->
    <input
      v-else
      ref="fieldRef"
      type="text"
      class="ref-textarea__field ref-textarea__field--single"
      :value="modelValue || ''"
      :placeholder="placeholder"
      :disabled="disabled"
      @input="handleInput"
      @keydown="handleKeydown"
      @blur="handleBlur"
      @compositionstart="composing = true"
      @compositionend="handleCompositionEnd"
    />

    <!-- @ 触发的浮动引用下拉（分组：待评测数据 / 裁判变量，支持按查询段过滤）
         Teleport 到 body + fixed 定位：不受 el-collapse / el-dialog 祖先 overflow 裁剪 -->
    <Teleport to="body">
      <div v-if="dropdownVisible" ref="dropdownRef" class="ref-textarea__dropdown" :style="dropdownStyle">
        <template v-if="filteredTargets.length > 0">
          <div class="ref-textarea__group-title">待评测数据</div>
          <div
            v-for="opt in filteredTargets"
            :key="`target:${opt.key}`"
            class="ref-textarea__option"
            :class="{ 'is-active': activeOption === opt }"
            title="插入待评测数据引用"
            @mousedown.prevent
            @click="selectOption(opt)"
          >
            <span class="ref-textarea__option-name">{{ opt.name }}</span>
            <span class="ref-textarea__option-sub">{{ opt.key }}</span>
          </div>
        </template>
        <template v-if="filteredVars.length > 0">
          <div class="ref-textarea__group-title">裁判变量</div>
          <div
            v-for="opt in filteredVars"
            :key="`var:${opt.key}`"
            class="ref-textarea__option"
            :class="{ 'is-active': activeOption === opt }"
            title="插入裁判变量引用"
            @mousedown.prevent
            @click="selectOption(opt)"
          >
            <span class="ref-textarea__option-name">{{ opt.name }}</span>
            <span class="ref-textarea__option-sub">{{ opt.key }}</span>
          </div>
        </template>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { buildReferenceMark, detectCaretAtMark, insertMark } from '../utils/refMarks.js'

const props = defineProps({
  // v-model 绑定值（输入全文，含已插入的 {{ref:...}} 标记）
  modelValue: { type: String, default: '' },
  // 可引用的待评测数据行：[{id, display_name}]
  targets: { type: Array, default: () => [] },
  // 可引用的裁判变量：[{key, name}]
  vars: { type: Array, default: () => [] },
  placeholder: { type: String, default: '' },
  rows: { type: Number, default: 4 },
  disabled: { type: Boolean, default: false },
  // 单行模式：等级描述小输入框（渲染 input 而非 textarea）
  isSingleLine: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

// 输入控件（textarea / input 都挂 fieldRef，二选一渲染）
const fieldRef = ref(null)
// 下拉面板元素（键盘导航时滚动到当前项）
const dropdownRef = ref(null)
// 下拉 fixed 定位样式（Teleport 到 body，按输入框视口位置计算）
const dropdownStyle = ref({})
// IME 输入法组合态：组合期间不触发检测（避免拼音候选阶段误弹下拉）
const composing = ref(false)
// 当前命中的「孤立 @」检测结果；null 表示下拉关闭
const detection = ref(null)
// 键盘导航的当前项下标（针对 filteredOptions 平铺列表）
const activeIndex = ref(0)

// 可选项平铺：待评测数据在前、裁判变量在后（与下拉分组顺序一致）
const flatOptions = computed(() => {
  const targetOptions = (props.targets || [])
    .filter((t) => t && t.id !== undefined && t.id !== null)
    .map((t) => ({ kind: 'target', key: String(t.id), name: String(t.display_name ?? t.id) }))
  const varOptions = (props.vars || [])
    .filter((v) => v && v.key !== undefined && v.key !== null)
    .map((v) => ({ kind: 'var', key: String(v.key), name: String(v.name ?? v.key) }))
  return [...targetOptions, ...varOptions]
})

// 按查询段过滤（匹配显示名或 key/id，忽略大小写）
const filteredOptions = computed(() => {
  const q = (detection.value?.query || '').trim().toLowerCase()
  if (!q) return flatOptions.value
  return flatOptions.value.filter(
    (o) => o.name.toLowerCase().includes(q) || o.key.toLowerCase().includes(q)
  )
})
const filteredTargets = computed(() => filteredOptions.value.filter((o) => o.kind === 'target'))
const filteredVars = computed(() => filteredOptions.value.filter((o) => o.kind === 'var'))

// 下拉可见：触发命中 + 有可选项 + 未禁用
const dropdownVisible = computed(
  () => !props.disabled && detection.value !== null && filteredOptions.value.length > 0
)
const activeOption = computed(() => filteredOptions.value[activeIndex.value] || null)

// 过滤结果变短时收敛越界的导航下标
watch(filteredOptions, (opts) => {
  if (activeIndex.value >= opts.length) activeIndex.value = 0
})

// 导航项变化时滚动到可视区
watch(activeIndex, async () => {
  await nextTick()
  const el = dropdownRef.value?.querySelector('.ref-textarea__option.is-active')
  el?.scrollIntoView({ block: 'nearest' })
})

// ---------------- 下拉定位（Teleport 到 body，fixed 定位防祖先裁剪） ----------------

// 按输入框视口矩形计算下拉位置：默认下方左对齐，下方空间不足且上方更宽裕时向上弹
const updateDropdownPosition = async () => {
  const field = fieldRef.value
  if (!field) return
  await nextTick()
  const rect = field.getBoundingClientRect()
  const dropdownHeight = dropdownRef.value?.offsetHeight || 0
  const spaceBelow = window.innerHeight - rect.bottom
  const openUp = spaceBelow < Math.max(dropdownHeight, 120) && rect.top > spaceBelow
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - 260))
  dropdownStyle.value = {
    position: 'fixed',
    left: `${left}px`,
    width: `${Math.min(Math.max(rect.width, 240), 480)}px`,
    ...(openUp
      ? { bottom: `${window.innerHeight - rect.top + 4}px` }
      : { top: `${rect.bottom + 4}px` }),
    zIndex: 3000,
  }
}

// 下拉打开与输入查询段时重算位置
watch(detection, (val) => {
  if (val) updateDropdownPosition()
})

// 滚动（捕获：覆盖对话框内滚动容器）与窗口缩放时跟随输入框
const repositionOnScroll = () => {
  if (detection.value !== null) updateDropdownPosition()
}
onMounted(() => {
  window.addEventListener('scroll', repositionOnScroll, true)
  window.addEventListener('resize', repositionOnScroll)
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', repositionOnScroll, true)
  window.removeEventListener('resize', repositionOnScroll)
})

// 外部改写 modelValue（如表单重置）时关闭悬挂的下拉，避免与旧检测状态错位
watch(
  () => props.modelValue,
  (val) => {
    if (detection.value === null) return
    const field = fieldRef.value
    if (!field || field.value !== (val || '')) detection.value = null
  }
)

// 输入后按光标位置重算「孤立 @」检测结果
const refreshDetection = (field) => {
  const caret = field.selectionStart ?? field.value.length
  const result = detectCaretAtMark(field.value, caret)
  detection.value = result
  if (result) activeIndex.value = 0
}

const handleInput = (e) => {
  // IME 组合期间不同步（与 Vue 原生 v-model 的 composition 语义一致，组合结束再同步）
  if (composing.value) return
  emit('update:modelValue', e.target.value)
  refreshDetection(e.target)
}

const handleCompositionEnd = (e) => {
  composing.value = false
  emit('update:modelValue', e.target.value)
  refreshDetection(e.target)
}

const handleKeydown = (e) => {
  // 组合态或下拉未开时保持原生行为（Enter 换行/提交、方向键移动光标等）
  if (composing.value || e.isComposing || !dropdownVisible.value) return
  const opts = filteredOptions.value
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    activeIndex.value = (activeIndex.value + 1) % opts.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    activeIndex.value = (activeIndex.value - 1 + opts.length) % opts.length
  } else if (e.key === 'Enter' || e.key === 'NumpadEnter') {
    e.preventDefault()
    if (activeOption.value) selectOption(activeOption.value)
  } else if (e.key === 'Escape') {
    e.preventDefault()
    detection.value = null
  }
}

const handleBlur = () => {
  // 选项用 @mousedown.prevent 保住焦点，此处关闭不会吃掉点击
  detection.value = null
}

// 选中选项：在光标处用「标记 + 空格」替换「@ + 查询段」
const selectOption = (opt) => {
  const field = fieldRef.value
  const d = detection.value
  detection.value = null
  if (!field || !d) return
  // 以点击/回车时的实际光标复核一次，防止下拉期间光标被移走
  const caret = field.selectionStart ?? field.value.length
  const current = detectCaretAtMark(field.value, caret)
  if (!current) return
  const mark = buildReferenceMark(opt.kind, opt.key, opt.name)
  const { text, caretIndex } = insertMark(field.value, current, mark)
  field.value = text
  emit('update:modelValue', text)
  nextTick(() => {
    field.focus()
    field.setSelectionRange(caretIndex, caretIndex)
  })
}
</script>

<style scoped>
.ref-textarea {
  position: relative;
  width: 100%;
}

/* 原生输入控件，视觉对齐 Element Plus（用其 CSS 变量，缺省回落默认色板） */
.ref-textarea__field {
  box-sizing: border-box;
  width: 100%;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  color: var(--el-text-color-regular, #606266);
  background-color: var(--el-fill-color-blank, #ffffff);
  border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: var(--el-border-radius-base, 4px);
  padding: 5px 11px;
  outline: none;
  resize: vertical;
  transition: border-color 0.2s cubic-bezier(0.645, 0.045, 0.355, 1);
}

.ref-textarea__field:focus {
  border-color: var(--el-color-primary, #409eff);
}

.ref-textarea__field:disabled {
  color: var(--el-disabled-text-color, #a8abb2);
  background-color: var(--el-fill-color-light, #f5f7fa);
  border-color: var(--el-disabled-border-color, #e4e7ed);
  cursor: not-allowed;
  resize: none;
}

/* 单行模式：对齐 el-input 默认 32px 高度 */
.ref-textarea__field--single {
  height: 32px;
  padding: 0 11px;
  resize: none;
}

/* 浮动下拉：Teleport 到 body + fixed 定位（位置由组件内联样式按输入框矩形计算），
   不受 el-collapse / el-dialog 祖先 overflow 裁剪，高于 el-dialog / el-popper（2000+） */
.ref-textarea__dropdown {
  min-width: 240px;
  max-height: 260px;
  overflow-y: auto;
  padding: 4px 0;
  background-color: var(--el-bg-color-overlay, #ffffff);
  border: 1px solid var(--el-border-color-light, #e4e7ed);
  border-radius: var(--el-border-radius-base, 4px);
  box-shadow: var(--el-box-shadow-light, 0px 0px 12px rgba(0, 0, 0, 0.12));
}

.ref-textarea__group-title {
  padding: 4px 12px;
  font-size: 12px;
  line-height: 20px;
  color: var(--el-text-color-secondary, #909399);
}

.ref-textarea__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 32px;
  padding: 0 12px;
  cursor: pointer;
}

.ref-textarea__option:hover,
.ref-textarea__option.is-active {
  background-color: var(--el-fill-color-light, #f5f7fa);
}

.ref-textarea__option-name {
  font-size: 14px;
  color: var(--el-text-color-primary, #303133);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ref-textarea__option-sub {
  flex-shrink: 0;
  max-width: 45%;
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
