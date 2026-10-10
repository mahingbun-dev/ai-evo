<template>
  <el-dialog
    :model-value="visible"
    title="选择执行包文件（生成匹配模式）"
    width="760px"
    append-to-body
    :close-on-click-modal="false"
    @update:model-value="val => emit('update:visible', val)"
    @open="handleOpen"
  >
    <div v-loading="loading" class="hub-picker-body">
      <!-- 样本说明 -->
      <el-alert
        v-if="sampleInfo"
        type="info"
        :closable="false"
        show-icon
        class="hub-picker-sample"
        :title="`样本来源：数据集第 ${sampleInfo.index} 条数据的执行包（file_id: ${sampleInfo.fileId.slice(0, 10)}…）`"
      />

      <!-- 样本文件树：点选文件/目录生成 glob -->
      <div class="hub-picker-section-title">样本文件树（点选文件或目录自动填充匹配模式）</div>
      <el-alert v-if="error" type="error" :closable="false" :title="error" show-icon style="margin-bottom: 8px" />
      <el-tree
        v-else
        ref="treeRef"
        :data="treeData"
        :props="treeProps"
        node-key="key"
        highlight-current
        :expand-on-click-node="true"
        default-expand-all
        class="hub-picker-tree"
        @node-click="onNodeClick"
      >
        <template #default="{ data }">
          <div class="hub-picker-node">
            <el-icon v-if="data.isDir" color="#E6A23C"><Folder /></el-icon>
            <el-icon v-else-if="data.type === 'image'" color="#409EFF"><Picture /></el-icon>
            <el-icon v-else-if="data.type === 'archive'" color="#F56C6C"><Files /></el-icon>
            <el-icon v-else color="#909399"><Document /></el-icon>
            <span class="hub-picker-label" :title="data.path">{{ data.label }}</span>
            <el-tag v-if="data.isDir" size="small" type="info" effect="plain">{{ data.fileCount }} 文件</el-tag>
            <el-tag v-else-if="data.isNested" size="small" type="warning" effect="plain">嵌套zip</el-tag>
          </div>
        </template>
      </el-tree>

      <!-- 匹配模式（glob）：输入防抖即时匹配预览 -->
      <el-form label-width="90px" style="margin-top: 12px">
        <el-form-item label="匹配模式">
          <div style="width: 100%">
            <el-input
              v-model="localPattern"
              placeholder="如 reports/allure_results/*.json 或 logs/**/*.log（pathlib glob 语义）"
              clearable
            />
            <div class="hub-picker-tip">支持 *、? 与 **（递归）通配；点选目录生成「目录/**/*」可再手改</div>
          </div>
        </el-form-item>
        <el-form-item label="匹配预览">
          <div style="width: 100%">
            <template v-if="matchLoading"><el-icon class="is-loading"><Loading /></el-icon> 匹配中...</template>
            <template v-else-if="matchResult">
              <div class="hub-picker-match-count">
                样本中匹配 <b>{{ matchResult.matched_count }}</b> 个文件
                <span v-if="matchResult.sample_data_index !== null && matchResult.sample_data_index !== undefined">
                  （样本数据序号 {{ matchResult.sample_data_index }}）
                </span>
              </div>
              <div v-if="matchResult.message" class="hub-picker-tip">{{ matchResult.message }}</div>
              <div v-else-if="matchedPaths.length" class="hub-picker-paths">
                <div v-for="p in matchedPaths" :key="p" class="hub-picker-path-item">{{ p }}</div>
                <div v-if="matchResult.matched_count > matchedPaths.length" class="hub-picker-tip">
                  其余 {{ matchResult.matched_count - matchedPaths.length }} 个略
                </div>
              </div>
            </template>
            <div v-else class="hub-picker-tip">输入匹配模式后即时预览样本匹配结果</div>
          </div>
        </el-form-item>
        <el-form-item label="必须匹配">
          <el-switch v-model="localRequired" />
          <div class="hub-picker-tip" style="margin-left: 10px">
            开启时执行包未匹配到文件则该脚本按失败处理（on_fail 生效）；关闭时匹配 0 个仍继续（files 为空）
          </div>
        </el-form-item>
      </el-form>
    </div>
    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :disabled="!localPattern.trim()" @click="handleConfirm">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Folder, Picture, Files, Document, Loading } from '@element-plus/icons-vue'
import { datasetApi, hubApi } from '@/api'
import { buildHubTreeData } from '@/utils/hubFileTree'

const props = defineProps({
  visible: { type: Boolean, default: false },
  // 当前任务配置的数据集 ID（用于取首条样本与匹配预览）
  datasetId: { type: String, default: '' },
  // 映射的 hub_file 数据集字段名（如 hub_file_id）
  fieldName: { type: String, default: '' },
  // 已有 pattern / required（回显）
  pattern: { type: String, default: '' },
  required: { type: Boolean, default: true }
})
const emit = defineEmits(['update:visible', 'confirm'])

const loading = ref(false)
const error = ref('')
const treeData = ref([])
const treeRef = ref(null)
const treeProps = { children: 'children', label: 'label', isLeaf: 'isLeaf' }
const sampleInfo = ref(null)   // {fileId, index}

const localPattern = ref('')
const localRequired = ref(true)
const matchLoading = ref(false)
const matchResult = ref(null)
let matchTimer = null

// 打开：拉数据集首条含该字段数据 → 取 file_id → 拉样本文件树
const handleOpen = async () => {
  localPattern.value = props.pattern || ''
  localRequired.value = props.required !== false
  matchResult.value = null
  error.value = ''
  treeData.value = []
  sampleInfo.value = null

  if (!props.datasetId || !props.fieldName) {
    error.value = '缺少数据集或字段信息，无法加载样本'
    return
  }
  loading.value = true
  try {
    // 先查数据列表拿第一条含此字段数据的 file_id
    const res = await datasetApi.dataListV2(props.datasetId, { limit: 100 })
    const items = Array.isArray(res?.items) ? res.items : []
    const sample = items.find(it => it?.data && typeof it.data === 'object' &&
      typeof it.data[props.fieldName] === 'string' && it.data[props.fieldName].trim())
    if (!sample) {
      error.value = `数据集中暂无字段 ${props.fieldName} 的执行包样本数据，可手填匹配模式（无法即时预览）`
      return
    }
    const fileId = sample.data[props.fieldName].trim()
    const treeRes = await hubApi.getFileTree(fileId)
    const index = Array.isArray(treeRes?.file_index) ? treeRes.file_index : []
    treeData.value = buildHubTreeData(index)
    sampleInfo.value = { fileId, index: sample.index ?? '-' }
    scheduleMatch()
  } catch (e) {
    error.value = e.message || '加载样本文件树失败'
  } finally {
    loading.value = false
  }
}

// 点选文件/目录生成 glob：文件→自身路径；目录→目录/**/*（可手改）
const onNodeClick = (data) => {
  localPattern.value = data.isDir ? `${data.path}/**/*` : data.path
  scheduleMatch()
}

// 防抖匹配预览（无样本时跳过请求）
const scheduleMatch = () => {
  if (matchTimer) clearTimeout(matchTimer)
  const pattern = localPattern.value.trim()
  if (!pattern || !sampleInfo.value) {
    matchResult.value = null
    return
  }
  matchTimer = setTimeout(async () => {
    matchLoading.value = true
    try {
      const res = await hubApi.matchPreview({
        dataset_id: props.datasetId,
        field_name: props.fieldName,
        pattern
      })
      matchResult.value = res
    } catch (e) {
      matchResult.value = { matched_count: 0, sample_paths: [], message: e.message || '匹配预览失败' }
    } finally {
      matchLoading.value = false
    }
  }, 600)
}

watch(localPattern, () => scheduleMatch())

// 样本匹配路径预览（接口最多返回前 20 条）
const matchedPaths = computed(() => matchResult.value?.sample_paths || [])

const handleConfirm = () => {
  const pattern = localPattern.value.trim()
  if (!pattern) {
    ElMessage.warning('请先点选文件或输入匹配模式')
    return
  }
  emit('confirm', { pattern, required: localRequired.value })
  emit('update:visible', false)
}
</script>

<script>
export default { name: 'HubFilePatternPicker' }
</script>

<style scoped>
.hub-picker-body { min-height: 160px; }
.hub-picker-sample { margin-bottom: 10px; }

.hub-picker-section-title {
  font-weight: 600;
  color: #1f2937;
  margin-bottom: 6px;
  font-size: 13px;
}

.hub-picker-tree {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 6px;
  max-height: 300px;
  overflow: auto;
}

.hub-picker-node {
  display: flex;
  align-items: center;
  gap: 5px;
  flex: 1;
  min-width: 0;
}

.hub-picker-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hub-picker-tip {
  color: #909399;
  font-size: 12px;
  margin-top: 4px;
}

.hub-picker-match-count { color: #303133; font-size: 13px; }

.hub-picker-paths {
  margin-top: 6px;
  background: #f9fafb;
  border-radius: 6px;
  padding: 8px 10px;
  max-height: 180px;
  overflow: auto;
}

.hub-picker-path-item {
  font-size: 12px;
  color: #374151;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  padding: 1px 0;
  word-break: break-all;
}
</style>
