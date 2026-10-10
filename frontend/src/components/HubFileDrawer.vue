<template>
  <el-drawer
    :model-value="visible"
    title="执行包文件"
    direction="rtl"
    size="620px"
    :close-on-click-modal="false"
    @update:model-value="val => emit('update:visible', val)"
    @open="handleOpen"
    @close="handleClose"
  >
    <div v-loading="loading" class="hub-drawer-body">
      <!-- 摘要卡片：execution_id / 用例统计 / 文件数 -->
      <div class="hub-summary-card">
        <div class="hub-summary-title">
          <el-icon><Box /></el-icon>
          <span>执行包摘要</span>
          <el-tag size="small" type="info" style="margin-left: auto">file_id: {{ shortFileId }}</el-tag>
        </div>
        <template v-if="summaryDisplay">
          <div class="hub-summary-grid">
            <div class="hub-summary-item">
              <label>execution_id</label>
              <span :title="summaryDisplay.execution_id">{{ summaryDisplay.execution_id || '-' }}</span>
            </div>
            <div class="hub-summary-item">
              <label>用例统计</label>
              <span>
                共 {{ summaryDisplay.case_total ?? 0 }} 例
                <span class="hub-pass">通过 {{ summaryDisplay.case_passed ?? 0 }}</span>
                <span class="hub-fail">失败 {{ summaryDisplay.case_failed ?? 0 }}</span>
              </span>
            </div>
            <div class="hub-summary-item">
              <label>文件数</label>
              <span>{{ fileCount }}（segments: {{ summaryDisplay.segment_count ?? '-' }}）</span>
            </div>
          </div>
        </template>
        <div v-else class="hub-summary-empty">
          无 execution-manifest 摘要（共 {{ fileCount }} 个文件）
        </div>
      </div>

      <!-- 文件树 -->
      <div class="hub-tree-header">
        <span>文件树</span>
        <div class="hub-tree-header-right">
          <el-input
            v-model="treeFilter"
            placeholder="按路径过滤"
            size="small"
            clearable
            style="width: 180px"
          />
          <el-button type="primary" size="small" :icon="Upload" @click="openUploadDialog">补传文件</el-button>
        </div>
      </div>
      <el-alert v-if="error" type="error" :closable="false" :title="error" show-icon style="margin-bottom: 8px" />
      <el-tree
        v-else
        ref="treeRef"
        :data="treeData"
        :props="treeProps"
        node-key="key"
        :filter-node-method="filterNode"
        :expand-on-click-node="true"
        default-expand-all
        class="hub-file-tree"
      >
        <template #default="{ data }">
          <div class="hub-tree-node" :class="{ 'is-file': !data.isDir }" @click="onNodeClick(data)">
            <el-icon v-if="data.isDir" color="#E6A23C"><Folder /></el-icon>
            <el-icon v-else-if="data.type === 'image'" color="#409EFF"><Picture /></el-icon>
            <el-icon v-else-if="data.type === 'archive'" color="#F56C6C"><Files /></el-icon>
            <el-icon v-else-if="data.type === 'binary'" color="#909399"><Cpu /></el-icon>
            <el-icon v-else color="#909399"><Document /></el-icon>
            <span class="hub-tree-label" :title="data.path">{{ data.label }}</span>
            <el-tag v-if="data.isNested" size="small" type="warning" effect="plain" class="hub-tree-tag">嵌套zip</el-tag>
            <el-tooltip
              v-if="data.isManual"
              :content="`人工补传${data.uploadedBy ? ' · ' + data.uploadedBy : ''}${data.uploadedAt ? ' · ' + data.uploadedAt : ''}`"
              placement="top"
            >
              <el-tag size="small" type="success" effect="plain" class="hub-tree-tag">人工</el-tag>
            </el-tooltip>
            <span v-if="!data.isDir" class="hub-tree-size">{{ formatHubFileSize(data.size) }}</span>
            <el-button
              v-if="data.isManual"
              link
              type="danger"
              size="small"
              class="hub-tree-download"
              :loading="deletingSuppPath === data.path"
              @click.stop="deleteSupplementNode(data)"
            >删除</el-button>
            <el-button
              v-if="!data.isDir"
              link
              type="primary"
              size="small"
              class="hub-tree-download"
              :loading="downloadingPath === data.path"
              @click.stop="downloadNode(data)"
            >下载</el-button>
          </div>
        </template>
      </el-tree>
    </div>

    <!-- 节点预览弹窗（抽屉内二级弹窗，挂 body） -->
    <el-dialog
      v-model="previewVisible"
      :title="previewTitle"
      width="760px"
      append-to-body
      class="hub-preview-dialog"
    >
      <div v-loading="previewLoading" class="hub-preview-body">
        <!-- 文本：json 可格式化 -->
        <template v-if="previewKind === 'text'">
          <el-alert v-if="previewTruncated" type="warning" :closable="false" style="margin-bottom: 8px"
            title="文件过大，预览内容已截断（约 2MB），完整内容请下载" />
          <pre class="hub-preview-text">{{ previewText }}</pre>
        </template>
        <!-- 图片：blob 转 objectURL 内嵌展示 -->
        <div v-else-if="previewKind === 'image'" class="hub-preview-image-wrap">
          <img v-if="previewImageUrl" :src="previewImageUrl" alt="预览图片" class="hub-preview-image" />
        </div>
        <!-- 嵌套 zip：内层清单（entries 仅含 path/size/compress_size，无 type 分类） -->
        <template v-else-if="previewKind === 'nested'">
          <div class="hub-preview-tip">内层清单（只读 zip 目录，不解压；如需内容请下载 zip 后查看）</div>
          <el-table :data="nestedEntries" size="small" max-height="420">
            <el-table-column prop="path" label="内层路径" min-width="300" show-overflow-tooltip />
            <el-table-column label="大小" width="110">
              <template #default="{ row }">{{ formatHubFileSize(row.size) }}</template>
            </el-table-column>
          </el-table>
        </template>
        <!-- 二进制 / 普通 archive：提示走下载 -->
        <template v-else-if="previewKind === 'binary'">
          <el-alert type="info" :closable="false" title="该文件类型不支持在线预览，请使用「下载」获取原文件" show-icon />
        </template>
      </div>
      <template #footer>
        <el-button v-if="previewNode && previewKind !== 'nested'" :loading="downloadingPath === previewNode.path" type="primary" plain
          @click="downloadNode(previewNode)">下载原文件</el-button>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 人工补传上传弹窗（抽屉内二级弹窗，挂 body） -->
    <el-dialog
      v-model="uploadVisible"
      title="补传文件"
      width="540px"
      append-to-body
      class="hub-supplement-dialog"
      :close-on-click-modal="false"
    >
      <div class="hub-supplement-tip">
        向执行包补传文件（包级生效：引用该包的所有数据行/数据集共享，评测时自动进入脚本文件目录）。
        同名文件以补传版为准；<b>.zip 自动解压</b>保留内部目录结构（目标子目录不生效）。
      </div>
      <el-upload
        ref="supplementUploadRef"
        :auto-upload="false"
        multiple
        drag
        :on-change="handleSupplementFileChange"
        :on-remove="handleSupplementFileRemove"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖拽文件到此处，或 <em>点击选择</em></div>
        <template #tip>
          <div class="el-upload__tip">支持多选；.zip 自动解压；单个文件 ≤ 500MB、总量 ≤ 2GB</div>
        </template>
      </el-upload>
      <el-form label-width="90px" style="margin-top: 8px" @submit.prevent>
        <el-form-item label="目标子目录">
          <el-input
            v-model="uploadTargetDir"
            placeholder="仅散文件生效，如 inputs/（缺省传到包根）"
            clearable
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="uploading" @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" :disabled="!supplementFiles.length" @click="submitSupplements">
          上传{{ supplementFiles.length ? `（${supplementFiles.length}）` : '' }}
        </el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Box, Folder, Picture, Files, Document, Cpu, Upload, UploadFilled } from '@element-plus/icons-vue'
import JSZip from 'jszip'
import { hubApi } from '@/api'
import { buildHubTreeData, formatHubFileSize } from '@/utils/hubFileTree'
import { saveBlobFile } from '@/utils/fileDownload'

const props = defineProps({
  visible: { type: Boolean, default: false },
  // anc-hub 执行包 file_id（数据行 hub_file 字段值）
  fileId: { type: String, default: '' },
  // 数据行 hub_summary（可空；无 manifest 时为空对象）
  summary: { type: Object, default: null }
})
const emit = defineEmits(['update:visible'])

const loading = ref(false)
const error = ref('')
const treeData = ref([])
const treeRef = ref(null)
const treeFilter = ref('')
const fileCount = ref(0)
const treeProps = { children: 'children', label: 'label', isLeaf: 'isLeaf' }

const shortFileId = computed(() => {
  const id = props.fileId || ''
  return id.length > 18 ? id.slice(0, 10) + '…' + id.slice(-6) : id
})

// 摘要展示：hub_summary 存在且非空对象时展示卡片明细
const summaryDisplay = computed(() => {
  const s = props.summary
  if (!s || typeof s !== 'object' || Object.keys(s).length === 0) return null
  return s
})

// 打开时拉取文件树（一次构建）；补传/删除成功后复用刷新
const allFilePaths = ref(new Set())
const loadTree = async () => {
  if (!props.fileId) return
  loading.value = true
  error.value = ''
  treeData.value = []
  treeFilter.value = ''
  try {
    const res = await hubApi.getFileTree(props.fileId)
    const index = Array.isArray(res?.file_index) ? res.file_index : []
    fileCount.value = index.length
    allFilePaths.value = new Set(index.map(it => String(it.path || '')))
    treeData.value = buildHubTreeData(index)
  } catch (e) {
    error.value = e.message || '获取执行包文件树失败'
    fileCount.value = 0
    allFilePaths.value = new Set()
  } finally {
    loading.value = false
  }
}

const handleOpen = async () => {
  await loadTree()
}

const handleClose = () => {
  // 关闭时清理预览态与 objectURL，避免残留
  closePreview()
  treeFilter.value = ''
}

// 路径过滤
watch(treeFilter, (val) => {
  treeRef.value?.filter?.(val)
})
const filterNode = (value, data) => {
  if (!value) return true
  return String(data.path || '').toLowerCase().includes(String(value).toLowerCase())
}

// ==================== 节点预览 ====================
const previewVisible = ref(false)
const previewLoading = ref(false)
const previewKind = ref('')      // text | image | nested | binary
const previewText = ref('')
const previewTruncated = ref(false)
const previewImageUrl = ref('')
const nestedEntries = ref([])
const previewNode = ref(null)
const previewTitle = computed(() => previewNode.value?.path || '文件预览')

const onNodeClick = (data) => {
  if (data.isDir) return
  // 嵌套 zip：调 nested-tree 展示内层清单
  if (data.isNested) {
    openNestedPreview(data)
    return
  }
  if (data.type === 'text') {
    openTextPreview(data)
  } else if (data.type === 'image') {
    openImagePreview(data)
  } else {
    // archive（非嵌套可预览形态）/ binary：提示走下载
    previewNode.value = data
    previewKind.value = 'binary'
    previewText.value = ''
    previewVisible.value = true
  }
}

const openTextPreview = async (data) => {
  previewNode.value = data
  previewKind.value = 'text'
  previewText.value = ''
  previewTruncated.value = false
  previewVisible.value = true
  previewLoading.value = true
  try {
    const res = await hubApi.getFileContent(props.fileId, data.path)
    let content = res?.content ?? ''
    // JSON 尝试格式化（parse 失败则原样展示）
    if (/\.json$/i.test(data.path) && typeof content === 'string') {
      try {
        content = JSON.stringify(JSON.parse(content), null, 2)
      } catch { /* 非合法 JSON，原样展示 */ }
    }
    previewText.value = content
    previewTruncated.value = !!res?.truncated
  } catch (e) {
    ElMessage.error(e.message || '读取文件内容失败')
    previewVisible.value = false
  } finally {
    previewLoading.value = false
  }
}

const openImagePreview = async (data) => {
  previewNode.value = data
  previewKind.value = 'image'
  if (previewImageUrl.value) {
    window.URL.revokeObjectURL(previewImageUrl.value)
    previewImageUrl.value = ''
  }
  previewVisible.value = true
  previewLoading.value = true
  try {
    // content 接口对图片返回 FileResponse：走 axios blob（鉴权随 cookie）转 objectURL 内嵌展示
    const blob = await hubApi.getFileContentBlob(props.fileId, data.path)
    previewImageUrl.value = window.URL.createObjectURL(blob)
  } catch (e) {
    ElMessage.error(e.message || '读取图片失败')
    previewVisible.value = false
  } finally {
    previewLoading.value = false
  }
}

const openNestedPreview = async (data) => {
  previewNode.value = data
  previewKind.value = 'nested'
  nestedEntries.value = []
  previewVisible.value = true
  previewLoading.value = true
  try {
    const res = await hubApi.getNestedTree(props.fileId, data.path)
    nestedEntries.value = Array.isArray(res?.entries) ? res.entries : []
  } catch (e) {
    ElMessage.error(e.message || '读取嵌套 zip 内层清单失败')
    previewVisible.value = false
  } finally {
    previewLoading.value = false
  }
}

const closePreview = () => {
  previewVisible.value = false
  previewLoading.value = false
  previewKind.value = ''
  previewText.value = ''
  nestedEntries.value = []
  previewNode.value = null
  if (previewImageUrl.value) {
    window.URL.revokeObjectURL(previewImageUrl.value)
    previewImageUrl.value = ''
  }
}

// 弹窗关闭时释放 objectURL
watch(previewVisible, (val) => {
  if (!val && previewImageUrl.value) {
    window.URL.revokeObjectURL(previewImageUrl.value)
    previewImageUrl.value = ''
  }
})

// ==================== 下载 ====================
const downloadingPath = ref('')
const downloadNode = async (data) => {
  if (!data || data.isDir) return
  downloadingPath.value = data.path
  try {
    const blob = await hubApi.downloadFile(props.fileId, data.path)
    // 文件名取包内路径最后一段（后端 Content-Disposition 已 URL 编码，前端按 basename 保存）
    saveBlobFile(blob, String(data.path).split('/').pop() || 'download')
  } catch (e) {
    ElMessage.error(e.message || '下载失败')
  } finally {
    downloadingPath.value = ''
  }
}

// ==================== 人工补传 ====================
const uploadVisible = ref(false)
const uploading = ref(false)
const uploadTargetDir = ref('')
const supplementFiles = ref([])
const supplementUploadRef = ref(null)
const deletingSuppPath = ref('')

const openUploadDialog = () => {
  uploadTargetDir.value = ''
  supplementFiles.value = []
  supplementUploadRef.value?.clearFiles?.()
  uploadVisible.value = true
}

const handleSupplementFileChange = (file, fileList) => {
  supplementFiles.value = fileList.map(f => f.raw).filter(Boolean)
}

const handleSupplementFileRemove = (file, fileList) => {
  supplementFiles.value = fileList.map(f => f.raw).filter(Boolean)
}

// 展开目标相对路径：zip 枚举内部条目；散文件拼 target_dir（正斜杠、去首尾 /）
const normalizeDir = (dir) => String(dir || '').replace(/\\/g, '/').split('/').filter(p => p && p !== '.' && p !== '..').join('/')

const listSupplementTargets = async (file, targetDir) => {
  if (/\.zip$/i.test(file.name)) {
    const zip = await JSZip.loadAsync(file)
    const entries = []
    zip.forEach((relPath, entry) => { if (!entry.dir) entries.push(relPath) })
    return entries
  }
  const dir = normalizeDir(targetDir)
  return [dir ? `${dir}/${file.name}` : file.name]
}

const submitSupplements = async () => {
  const files = supplementFiles.value
  if (!files.length) {
    ElMessage.warning('请先选择要补传的文件')
    return
  }
  if (!props.fileId) return

  // 覆盖预检：zip 枚举条目 + 散文件目标路径，与树内已有路径比对（后端仍权威校验）
  let conflicts = []
  uploading.value = true
  try {
    for (const f of files) {
      const targets = await listSupplementTargets(f, uploadTargetDir.value)
      conflicts.push(...targets.filter(p => allFilePaths.value.has(p)))
    }
  } catch (e) {
    uploading.value = false
    ElMessage.error(e.message || '读取补传文件失败（zip 可能已损坏）')
    return
  }
  if (conflicts.length) {
    const shown = conflicts.slice(0, 20).join('\n')
    const suffix = conflicts.length > 20 ? `\n…等共 ${conflicts.length} 个` : ''
    try {
      await ElMessageBox.confirm(
        `以下同名文件将被覆盖（补传版为准）：\n${shown}${suffix}`,
        '确认覆盖',
        { type: 'warning', confirmButtonText: '覆盖上传', cancelButtonText: '取消', customStyle: { whiteSpace: 'pre-line' } }
      )
    } catch {
      uploading.value = false
      return
    }
  }

  try {
    const formData = new FormData()
    const relPaths = []
    for (const f of files) {
      formData.append('files', f)
      if (/\.zip$/i.test(f.name)) {
        // zip 条目的相对路径后端不使用（按内部结构解压），传原文件名占位
        relPaths.push(f.name)
      } else {
        // 散文件只传文件名：target_dir 由后端统一拼接（前端拼一次后端再拼
        // 会得到 inputs/inputs/… 双重路径）
        relPaths.push(f.name)
      }
    }
    relPaths.forEach(p => formData.append('relative_paths', p))
    formData.append('target_dir', normalizeDir(uploadTargetDir.value))

    const res = await hubApi.uploadSupplements(props.fileId, formData)
    const addedCount = res?.added_count ?? 0
    const overwritten = Array.isArray(res?.overwritten) ? res.overwritten : []
    let message = `已补传 ${addedCount} 个文件`
    if (overwritten.length) message += `，覆盖 ${overwritten.length} 个同名文件`
    if (overwritten.length) {
      const shown = overwritten.slice(0, 20).join('\n')
      const suffix = overwritten.length > 20 ? `\n…等共 ${overwritten.length} 个` : ''
      ElMessageBox.alert(`被覆盖文件：\n${shown}${suffix}`, '补传完成（含覆盖）', {
        type: 'warning', confirmButtonText: '知道了', customStyle: { whiteSpace: 'pre-line' }
      }).catch(() => {})
    } else {
      ElMessage.success(message)
    }
    uploadVisible.value = false
    await loadTree()
  } catch (e) {
    ElMessage.error(e.message || '补传失败')
  } finally {
    uploading.value = false
  }
}

const deleteSupplementNode = async (data) => {
  if (!data?.isManual) return
  try {
    await ElMessageBox.confirm(
      `确定删除人工补传文件「${data.path}」吗？原始执行包内容不受影响。`,
      '删除补传文件',
      { type: 'warning' }
    )
  } catch {
    return
  }
  deletingSuppPath.value = data.path
  try {
    await hubApi.deleteSupplement(props.fileId, data.path)
    ElMessage.success('已删除补传文件')
    await loadTree()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  } finally {
    deletingSuppPath.value = ''
  }
}

defineExpose({ downloadNode })
</script>

<style scoped>
.hub-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 100%;
}

/* 摘要卡片 */
.hub-summary-card {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 12px;
}

.hub-summary-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: #1f2937;
  margin-bottom: 8px;
}

.hub-summary-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
}

.hub-summary-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}

.hub-summary-item label {
  color: #909399;
  font-size: 12px;
  flex-shrink: 0;
  width: 88px;
}

.hub-summary-item span {
  color: #303133;
  word-break: break-all;
}

.hub-pass { color: #67c23a; margin-left: 6px; }
.hub-fail { color: #f56c6c; margin-left: 6px; }
.hub-summary-empty { color: #909399; font-size: 12px; }

/* 文件树 */
.hub-tree-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  color: #1f2937;
}

.hub-tree-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 400;
}

.hub-file-tree {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 6px;
  max-height: calc(100vh - 320px);
  overflow: auto;
}

.hub-tree-node {
  display: flex;
  align-items: center;
  gap: 5px;
  flex: 1;
  min-width: 0;
  padding-right: 4px;
}

.hub-tree-node.is-file {
  cursor: pointer;
}

.hub-tree-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hub-tree-tag { flex-shrink: 0; }
.hub-tree-size {
  color: #c0c4cc;
  font-size: 12px;
  flex-shrink: 0;
  margin-left: auto;
}

.hub-tree-download { flex-shrink: 0; }

/* 预览弹窗 */
.hub-preview-body { min-height: 120px; }

.hub-preview-text {
  background: #f9fafb;
  border-radius: 6px;
  padding: 12px;
  max-height: 56vh;
  overflow: auto;
  font-size: 12px;
  line-height: 1.6;
  color: #374151;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}

.hub-preview-image-wrap {
  display: flex;
  justify-content: center;
  background: #f9fafb;
  border-radius: 6px;
  padding: 12px;
}

.hub-preview-image {
  max-width: 100%;
  max-height: 56vh;
  object-fit: contain;
}

.hub-preview-tip {
  color: #909399;
  font-size: 12px;
  margin-bottom: 8px;
}

/* 人工补传 */
.hub-supplement-tip {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 8px 10px;
  color: #606266;
  font-size: 12px;
  line-height: 1.6;
  margin-bottom: 10px;
}
</style>
