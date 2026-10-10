<template>
  <div class="user-docs">
    <!-- 操作栏 -->
    <div class="docs-toolbar">
      <template v-if="!isEditing">
        <el-button type="primary" plain :icon="EditPen" size="small" @click="startEdit">
          编辑文档
        </el-button>
      </template>
      <template v-else>
        <el-button type="primary" :loading="saving" size="small" @click="saveDoc">保存</el-button>
        <el-button size="small" @click="cancelEdit">取消</el-button>
      </template>
    </div>

    <!-- 加载骨架屏 -->
    <div v-if="loading" class="loading-wrap">
      <el-skeleton :rows="20" animated />
    </div>

    <!-- 编辑模式 -->
    <div v-else-if="isEditing" class="edit-wrap">
      <el-input
        v-model="editContent"
        type="textarea"
        :rows="40"
        class="md-editor"
        placeholder="请输入 Markdown 格式的文档内容..."
      />
    </div>

    <!-- 阅读模式：目录 + 内容 -->
    <div v-else class="docs-layout">
      <!-- 目录导航 -->
      <aside v-if="toc.length > 0" class="docs-toc">
        <div class="toc-title">目录</div>
        <nav class="toc-nav">
          <span
            v-for="item in toc"
            :key="item.id"
            :class="['toc-item', 'toc-level-' + item.level, { active: activeId === item.id }]"
            @click="scrollToHeading(item.id)"
          >
            {{ item.text }}
          </span>
        </nav>
      </aside>

      <!-- 文档内容 -->
      <el-card class="docs-card">
        <div ref="contentRef" class="markdown-body" v-html="renderedHtml" />
      </el-card>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { EditPen } from '@element-plus/icons-vue'
import { docsApi } from '@/api/index.js'
import { ElMessage } from 'element-plus'

const loading = ref(true)
const isEditing = ref(false)
const saving = ref(false)
const content = ref('')
const editContent = ref('')
const contentRef = ref(null)
const activeId = ref('')

// 生成标题 ID
const generateHeadingId = (text) => {
  return text
    .toLowerCase()
    .replace(/[^\w\s\u4e00-\u9fff-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
}

// 从 Markdown 内容解析标题列表
const parseHeadingsFromContent = (markdown) => {
  const headings = []
  if (!markdown) return headings
  const lines = markdown.split('\n')
  const headingRegex = /^(#{1,4})\s+(.+)$/

  for (const line of lines) {
    const match = line.match(headingRegex)
    if (match) {
      const level = match[1].length
      const text = match[2].trim()
      const id = generateHeadingId(text)
      headings.push({ level, text, id })
    }
  }
  return headings
}

// 解析目录：从 Markdown 内容中提取标题
const toc = computed(() => {
  return parseHeadingsFromContent(content.value)
})

// 自定义渲染器，为标题添加正确的 id
const renderer = new marked.Renderer()
renderer.heading = function({ text, depth }) {
  const id = generateHeadingId(text)
  return `<h${depth} id="${id}">${text}</h${depth}>\n`
}

// 配置 marked
marked.use({
  breaks: true,
  gfm: true,
  renderer
})

const renderedHtml = computed(() => {
  if (!content.value) return '<p style="color:#999">文档内容为空</p>'
  return DOMPurify.sanitize(marked.parse(content.value), {
    ADD_ATTR: ['id']  // 允许 id 属性
  })
})

// 滚动到指定标题
const scrollToHeading = (id) => {
  nextTick(() => {
    const element = document.getElementById(id)
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' })
      activeId.value = id
    }
  })
}

// 监听滚动，更新当前激活的目录项
const handleScroll = () => {
  const headings = toc.value
  if (headings.length === 0) return

  const scrollTop = window.scrollY
  let currentId = ''

  for (const heading of headings) {
    const element = document.getElementById(heading.id)
    if (element) {
      const offsetTop = element.offsetTop
      if (scrollTop >= offsetTop - 100) {
        currentId = heading.id
      }
    }
  }

  if (currentId) {
    activeId.value = currentId
  }
}

const loadDoc = async () => {
  loading.value = true
  try {
    const data = await docsApi.getUserGuide()
    content.value = data.content || ''
  } catch {
    ElMessage.error('加载文档失败，请刷新重试')
  } finally {
    loading.value = false
  }
}

const startEdit = () => {
  editContent.value = content.value
  isEditing.value = true
}

const cancelEdit = () => {
  isEditing.value = false
  editContent.value = ''
}

const saveDoc = async () => {
  saving.value = true
  try {
    await docsApi.updateUserGuide(editContent.value)
    content.value = editContent.value
    isEditing.value = false
    ElMessage.success('文档保存成功')
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadDoc()
  // 监听滚动
  window.addEventListener('scroll', handleScroll, { passive: true })
})

onUnmounted(() => {
  window.removeEventListener('scroll', handleScroll)
})
</script>

<style scoped>
.user-docs {
  max-width: 1400px;
  margin: 0 auto;
}

.docs-toolbar {
  margin-bottom: 16px;
  display: flex;
  justify-content: flex-end;
}

.loading-wrap {
  padding: 24px;
  background: #fff;
  border-radius: 12px;
}

.edit-wrap {
  background: #fff;
  border-radius: 12px;
  padding: 16px;
}

.md-editor {
  font-family: 'Courier New', monospace;
}

/* 目录布局 */
.docs-layout {
  display: flex;
  gap: 24px;
  align-items: flex-start;
}

/* 目录侧边栏 */
.docs-toc {
  width: 240px;
  flex-shrink: 0;
  position: sticky;
  top: 20px;
  max-height: calc(100vh - 120px);
  overflow-y: auto;
  background: #fff;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

.toc-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-color);
}

.toc-nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.toc-item {
  font-size: 13px;
  color: var(--text-secondary);
  text-decoration: none;
  padding: 6px 8px;
  border-radius: 4px;
  transition: all 0.2s;
  line-height: 1.4;
  cursor: pointer;
  display: block;
}

.toc-item:hover {
  color: var(--primary-color);
  background: var(--primary-light);
}

.toc-item.active {
  color: var(--primary-color);
  background: var(--primary-light);
  font-weight: 500;
}

/* 目录层级缩进 */
.toc-level-1 {
  font-weight: 500;
}

.toc-level-2 {
  padding-left: 16px;
}

.toc-level-3 {
  padding-left: 28px;
  font-size: 12px;
}

.toc-level-4 {
  padding-left: 40px;
  font-size: 12px;
  color: var(--text-secondary);
}

/* 文档内容区 */
.docs-card {
  flex: 1;
  min-width: 0;
  padding: 8px;
}

.markdown-body {
  font-size: 15px;
  line-height: 1.8;
  color: var(--text-primary);
}

/* 响应式：小屏幕隐藏目录 */
@media (max-width: 1024px) {
  .docs-toc {
    display: none;
  }
}

.markdown-body :deep(h1) {
  font-size: 26px;
  font-weight: 700;
  border-bottom: 2px solid var(--border-color);
  padding-bottom: 12px;
  margin: 0 0 24px;
}

.markdown-body :deep(h2) {
  font-size: 20px;
  font-weight: 600;
  margin: 32px 0 14px;
  padding-left: 12px;
  border-left: 4px solid var(--primary-color);
}

.markdown-body :deep(h3) {
  font-size: 16px;
  font-weight: 600;
  margin: 20px 0 10px;
}

.markdown-body :deep(h4) {
  font-size: 14px;
  font-weight: 600;
  margin: 16px 0 8px;
  color: var(--text-secondary);
}

.markdown-body :deep(p) {
  margin: 0 0 10px;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 24px;
  margin: 6px 0 10px;
}

.markdown-body :deep(li) {
  margin: 4px 0;
}

.markdown-body :deep(code) {
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'Courier New', monospace;
  font-size: 13px;
  color: #e83e8c;
}

.markdown-body :deep(pre) {
  background: #1e293b;
  color: #e2e8f0;
  padding: 16px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 12px 0;
}

.markdown-body :deep(pre code) {
  background: none;
  color: inherit;
  padding: 0;
  font-size: 13px;
}

.markdown-body :deep(blockquote) {
  border-left: 4px solid var(--primary-color);
  background: var(--primary-light);
  padding: 8px 16px;
  margin: 12px 0;
  border-radius: 0 8px 8px 0;
  color: var(--text-secondary);
}

/* 文档配图：随卡片宽度自适应，避免截图横向溢出 */
.markdown-body :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
  border: 1px solid var(--border-color);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  margin: 8px 0;
  display: block;
}

.markdown-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 14px 0;
  font-size: 14px;
}

.markdown-body :deep(th) {
  background: var(--primary-light);
  color: var(--primary-color);
  font-weight: 600;
  padding: 10px 14px;
  text-align: left;
  border: 1px solid var(--border-color);
}

.markdown-body :deep(td) {
  padding: 8px 14px;
  border: 1px solid var(--border-color);
}

.markdown-body :deep(tr:nth-child(even) td) {
  background: #f9fafb;
}

.markdown-body :deep(a) {
  color: var(--primary-color);
  text-decoration: none;
}

.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--border-color);
  margin: 24px 0;
}

.markdown-body :deep(strong) {
  font-weight: 600;
}
</style>

<style>
/* 文档配图：v-html 注入的内容不带 scoped 属性，img 规则必须放全局才能命中 */
.markdown-body img {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
  border: 1px solid var(--border-color);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  margin: 8px 0;
  display: block;
}
</style>
