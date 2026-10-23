<template>
  <div class="datasets-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>数据集管理</span>
          <div>
            <el-button type="warning" :loading="cleanupLoading" @click="handleCleanupDuplicates">清理重复数据</el-button>
            <el-button type="primary" :disabled="cleanupLoading || readonlyRole" :title="readonlyRole ? '当前角色（viewer）只读' : ''" @click="openDialog()">新建数据集</el-button>
          </div>
        </div>
      </template>

      <el-table :data="datasets" style="width: 100%">
        <el-table-column prop="name" label="数据集名称" min-width="180" />
        <el-table-column prop="description" label="描述" min-width="160" show-overflow-tooltip />
        <el-table-column prop="question_count" label="题目数量" min-width="100" />
        <el-table-column prop="has_media" label="含媒体" min-width="80">
          <template #default="{ row }">
            <el-tag :type="row.has_media ? 'warning' : 'info'">
              {{ row.has_media ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="修改时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.updated_at) || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="300" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="rowOpLoading" @click="viewData(row)">预览</el-button>
            <el-button link type="primary" :disabled="rowOpLoading" @click="openDialog(row)">编辑</el-button>
            <el-button link type="primary" :disabled="rowOpLoading" @click="openVersions(row)">版本</el-button>
            <el-button
              link
              type="primary"
              :loading="exportingId === row.id"
              :disabled="rowOpLoading || (exportingId !== null && exportingId !== row.id)"
              @click="exportDataset(row)"
            >导出</el-button>
            <el-button link type="danger" :loading="deletingId === row.id" :disabled="rowOpLoading" @click="deleteDataset(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑数据集' : '新建数据集'"
      width="900px"
      @close="handleDialogClose"
      :close-on-click-modal="!uploadLoading && !importLoading && !sessionPreviewLoading && !sessionImportLoading && !hubQueryLoading && !hubImportLoading"
    >
      <div
        v-loading="uploadLoading || importLoading || sessionPreviewLoading || sessionImportLoading || hubQueryLoading || hubImportLoading"
        :element-loading-text="uploadLoading ? '文件上传中...' : (sessionPreviewLoading ? '会话解析中...' : (hubQueryLoading ? '远端文件查询中...' : (hubImportLoading ? '正在创建 anc-hub 导入任务...' : ((importLoading || sessionImportLoading) ? '导入中...' : ''))))"
      >
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="数据集名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入数据集名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="请输入描述" />
        </el-form-item>

        <!-- 导入方式选择（新建/编辑均显示；编辑态选择后可将新数据追加到当前数据集） -->
        <el-form-item label="导入方式">
          <div>
            <el-radio-group v-model="importMethod" @change="handleImportMethodChange">
              <el-radio value="excel">Excel 数据导入</el-radio>
              <el-radio value="session">Agent 会话文件导入</el-radio>
              <el-radio value="hub">从 anc-hub 导入</el-radio>
            </el-radio-group>
            <div v-if="isEdit" style="color: #909399; font-size: 12px; margin-top: 4px">编辑模式下选择导入方式后，导入的数据将追加到当前数据集</div>
          </div>
        </el-form-item>

        <!-- Excel 模式：模板下载和上传区域 -->
        <template v-if="importMethod === 'excel'">
          <el-form-item label="模板下载">
            <el-button type="primary" plain @click="downloadTemplate">
              <el-icon><Download /></el-icon>
              下载模板
            </el-button>
            <div style="color: #909399; font-size: 12px; margin-top: 4px">模板包含字段：user_input（用户输入）、user_input_is_media（用户输入是否关联文件）、answer（答案）、answer_is_media（答案是否关联文件）。支持添加 user_input_2、answer_2 等多字段</div>
          </el-form-item>
          <el-form-item label="上传数据集">
            <el-upload
              ref="uploadRef"
              :auto-upload="false"
              :limit="1"
              accept=".xlsx,.xls"
              :on-change="handleFileChange"
              :on-remove="handleFileRemove"
            >
              <el-button type="success">
                <el-icon><Upload /></el-icon>
                选择Excel文件
              </el-button>
            </el-upload>
          </el-form-item>
        </template>

        <!-- 会话模式：Agent 会话文件上传区域 -->
        <el-form-item v-if="importMethod === 'session'" label="会话文件">
          <el-upload
            ref="sessionUploadRef"
            :auto-upload="false"
            accept=".jsonl,.json"
            multiple
            :on-change="handleSessionFileChange"
            :on-remove="handleSessionFileRemove"
          >
            <el-button type="success">
              <el-icon><Upload /></el-icon>
              选择会话文件
            </el-button>
            <template #tip>
              <div class="el-upload__tip">支持 .jsonl / .json 格式的 Claude Code、Langfuse 会话文件，可多选，选择后自动解析预览</div>
            </template>
          </el-upload>
        </el-form-item>

        <!-- anc-hub 模式：远端文件浏览（file_query 透传）+ 勾选拉取导入。
             导入内容不再区分会话文件/执行包：统一从 anc-hub 下载解包，
             自动定位其中 jsonl 解析为会话数据行（同步完成），并登记执行包与引用 -->
        <template v-if="importMethod === 'hub'">

          <!-- 远端浏览区 -->
          <el-form-item label="远端筛选">
            <div class="hub-filter-bar">
              <el-input v-model="hubQuery.name_like" placeholder="文件名关键字" clearable style="width: 170px" @keyup.enter="handleHubSearch" />
              <el-date-picker
                v-model="hubQuery.dateRange"
                type="daterange"
                value-format="YYYY-MM-DD"
                range-separator="至"
                start-placeholder="上传开始日期"
                end-placeholder="上传结束日期"
                style="width: 250px"
              />
              <el-input v-model="hubQuery.uploader" placeholder="上传者" clearable style="width: 130px" @keyup.enter="handleHubSearch" />
              <el-button type="primary" :loading="hubQueryLoading" @click="handleHubSearch">查询</el-button>
              <el-button @click="resetHubQuery">重置</el-button>
            </div>
          </el-form-item>
          <el-form-item label="远端文件">
            <div class="hub-file-panel">
              <!-- hub 不可达 / 接口报错：表格区展示明确中文错误 + 重试按钮，不白屏 -->
              <div v-if="hubQueryError" class="hub-error-box">
                <el-alert type="error" :closable="false" :title="`anc-hub 文件查询失败：${hubQueryError}`" />
                <el-button size="small" type="primary" plain style="margin-top: 8px" @click="loadHubFiles">重试</el-button>
              </div>
              <el-table
                ref="hubTableRef"
                :data="hubFiles"
                max-height="280"
                size="small"
                row-key="file_id"
                :empty-text="hubQueryError ? '加载失败，请点击重试' : '暂无数据，请调整筛选条件后查询'"
                @selection-change="handleHubSelectionChange"
              >
                <el-table-column type="selection" width="45" :reserve-selection="true" />
                <el-table-column prop="name" label="文件名" min-width="170" show-overflow-tooltip />
                <el-table-column label="大小" width="90">
                  <template #default="{ row }">{{ row.size !== '' ? formatFileSize(row.size) : '-' }}</template>
                </el-table-column>
                <el-table-column label="上传时间" width="160">
                  <template #default="{ row }">{{ row.uploaded_at ? (formatTime(row.uploaded_at) || '-') : '-' }}</template>
                </el-table-column>
                <el-table-column label="上传者" width="110">
                  <template #default="{ row }">{{ row.uploader || '-' }}</template>
                </el-table-column>
              </el-table>
              <el-pagination
                v-if="hubTotal > 0"
                layout="total, prev, pager, next, sizes"
                :total="hubTotal"
                :current-page="hubPage"
                :page-size="hubPageSize"
                :page-sizes="[10, 20, 50]"
                @current-change="handleHubPageChange"
                @size-change="handleHubPageSizeChange"
                style="margin-top: 8px; display: flex; justify-content: flex-end"
              />
              <el-alert v-if="hubImportError" type="error" :closable="false" style="margin-top: 8px" :title="`anc-hub 导入失败：${hubImportError}`" />
              <!-- 执行包导入 job 进度面板（W14：复活 F1 面板样式；WS 推送 + 轮询兜底） -->
              <div v-if="hubJob" class="hub-job-panel">
                <div class="hub-job-status-line">
                  <el-icon v-if="hubJobRunning" class="is-loading"><Loading /></el-icon>
                  <el-icon v-else-if="hubJob.status === 'completed'" class="hub-job-ok"><CircleCheck /></el-icon>
                  <el-icon v-else-if="hubJob.status === 'failed'" class="hub-job-bad"><CircleClose /></el-icon>
                  <span class="hub-job-count">
                    导入任务 {{ hubJobShortId }}：成功 <span class="hub-job-ok">{{ hubJob.success_count || 0 }}</span> / 共 {{ hubJob.total_count || hubSelection.length }}
                    <template v-if="hubJob.fail_count">，失败 <span class="hub-job-bad">{{ hubJob.fail_count }}</span></template>
                    <template v-if="hubJob.status === 'pending'">（排队中…）</template>
                  </span>
                  <el-button
                    v-if="hubJob.status === 'failed'"
                    size="small"
                    type="warning"
                    :loading="hubJobRetrying"
                    @click="retryHubJobFailed"
                  >重试失败项</el-button>
                </div>
                <el-progress
                  v-if="hubJob.total_count"
                  :percentage="hubJobPercent"
                  :status="hubJobProgressStatus"
                  :stroke-width="8"
                  style="margin-top: 6px"
                />
                <div v-if="hubJobFailEntries.length" class="hub-job-fails">
                  <div v-for="[fid, msg] in hubJobFailEntries" :key="fid" class="hub-job-fail-item">
                    <span class="hub-job-bad">{{ fid }}</span>：{{ msg }}
                  </div>
                </div>
              </div>
              <el-form-item label="清洗脚本" style="margin-top: 12px; margin-bottom: 0">
                <el-select v-model="hubCleanScriptId" clearable filterable placeholder="不清洗（执行包原样入行）" style="width: 100%">
                  <el-option v-for="s in hubCleanScripts" :key="s.id" :label="s.name" :value="s.id" />
                </el-select>
                <div style="color: #909399; font-size: 12px; margin-top: 4px">
                  选择清洗脚本后，导入任务将逐包执行脚本并以脚本产出的行内容作为数据集数据（推荐使用脚本库中的 hub_exec_clean 清洗脚本套件）
                </div>
              </el-form-item>
              <div style="margin-top: 8px; color: #909399; font-size: 12px">
                已勾选 {{ hubSelection.length }} 个文件；「拉取并导入」创建异步导入任务：从 anc-hub 下载解包、逐包写入数据行（每包一行、自动登记原始 file_id 供抽屉预览与评测引用），进度在下方面板实时展示，失败项可一键重试
              </div>
            </div>
          </el-form-item>
        </template>

        <!-- 编辑模式下显示的字段（数据追加通过上方导入方式完成） -->
        <template v-if="isEdit">
          <el-form-item label="含媒体">
            <el-tag :type="form.has_media ? 'warning' : 'info'">
              {{ form.has_media ? '是' : '否' }}
            </el-tag>
          </el-form-item>
        </template>
      </el-form>

      <!-- 预览数据列表（Excel 模式，上传文件后显示） -->
      <div v-if="importMethod === 'excel' && previewData.length > 0" style="margin-top: 16px; border-top: 1px solid #eee; padding-top: 16px">
        <div style="margin-bottom: 8px; font-weight: 600">数据预览（共{{ previewData.length }}条{{ previewInvalidCount > 0 ? `，其中${previewInvalidCount}条有错误` : '' }}）</div>
        <el-alert v-if="previewInvalidCount > 0" type="error" :closable="false" style="margin-bottom: 8px">
          存在{{ previewInvalidCount }}条数据校验失败，请修正后重新上传
        </el-alert>
        <el-alert v-if="hasMissingMediaFiles" type="warning" :closable="false" style="margin-bottom: 8px">
          部分数据需要上传关联文件，请上传后再导入
        </el-alert>
        <el-table :data="previewData" max-height="300" size="small" :row-class-name="tableRowClassName">
          <el-table-column prop="row_num" label="行号" width="60" />
          <!-- 动态生成所有字段列（按原始顺序，支持任意字段名） -->
          <el-table-column
            v-for="field in previewAllFields"
            :key="field.name"
            :prop="field.name"
            :label="field.label || field.name"
            show-overflow-tooltip min-width="150" />
          <el-table-column label="状态" width="150">
            <template #default="{ row }">
              <!-- 有错误时显示具体错误信息 -->
              <el-tooltip v-if="row.errors && row.errors.length > 0" :content="row.errors.join('；')" placement="top">
                <div style="display: flex; align-items: center; color: #F56C6C; cursor: pointer">
                  <el-icon><CircleClose /></el-icon>
                  <span style="margin-left: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100px">{{ row.errors[0] }}</span>
                </div>
              </el-tooltip>
              <!-- 关联文件为是但字段值不符合 OSS URL 格式时显示警告 -->
              <el-tooltip v-else-if="previewAllFields.some(f => row[f.name + '_is_media'] && !isValidOssUrl(row[f.name]))" content="需要上传关联文件到 OSS">
                <el-icon color="#E6A23C"><Warning /></el-icon>
              </el-tooltip>
              <el-icon v-else color="#67C23A"><CircleCheck /></el-icon>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 会话文件导入预览（会话模式，选择文件后显示） -->
      <div v-if="importMethod === 'session' && sessionPreviewItems.length > 0" style="margin-top: 16px; border-top: 1px solid #eee; padding-top: 16px">
        <div style="margin-bottom: 8px; font-weight: 600">会话预览</div>
        <el-alert
          :type="sessionPreview.invalid_sessions > 0 ? 'error' : 'success'"
          :closable="false"
          style="margin-bottom: 8px"
        >
          共 {{ sessionPreview.total_sessions }} 个会话，可导入 {{ sessionPreview.valid_sessions }} 个，异常 {{ sessionPreview.invalid_sessions }} 个
        </el-alert>
        <el-table :data="sessionPreviewItems" max-height="300" size="small" :row-class-name="sessionTableRowClassName">
          <el-table-column prop="file_name" label="文件名" min-width="140" show-overflow-tooltip />
          <el-table-column label="来源" width="110">
            <template #default="{ row }">
              <el-tag :type="row.source === 'claude_code' ? 'primary' : 'success'">
                {{ row.source === 'claude_code' ? 'Claude Code' : 'Langfuse' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="summary" label="会话摘要" min-width="200" show-overflow-tooltip />
          <el-table-column prop="turns" label="轮次" width="70" />
          <el-table-column prop="message_count" label="消息数" width="80" />
          <el-table-column prop="tool_call_count" label="工具调用" width="90" />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag v-if="row.valid" type="success" size="small">可导入</el-tag>
              <el-tooltip v-else :content="row.error || '解析失败'" placement="top">
                <el-tag type="danger" size="small" style="cursor: pointer">异常</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
      </div>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false" :disabled="uploadLoading || importLoading || sessionPreviewLoading || sessionImportLoading || hubQueryLoading || hubImportLoading || submitLoading">取消</el-button>
        <!-- Excel 模式按钮 -->
        <el-button v-if="importMethod === 'excel' && previewData.length > 0" :disabled="footerBusy" @click="handleReUpload">重新选择文件</el-button>
        <el-button v-if="importMethod === 'excel' && previewData.length > 0 && previewInvalidCount === 0" :disabled="hasMissingMediaFiles || importLoading || footerBusy" :loading="importLoading" type="primary" @click="confirmImport">确认导入</el-button>
        <!-- 会话模式按钮 -->
        <el-button v-if="importMethod === 'session' && sessionPreview" :disabled="sessionPreviewLoading || sessionImportLoading || footerBusy" @click="handleSessionReUpload">重新选择文件</el-button>
        <el-button v-if="importMethod === 'session' && sessionCanConfirm" :disabled="sessionImportLoading || footerBusy" :loading="sessionImportLoading" type="primary" @click="confirmSessionImportAction">确认导入</el-button>
        <!-- anc-hub 模式按钮：同步拉取导入（统一解析 jsonl 会话）；编辑态同样可用，追加到当前数据集 -->
        <el-button v-if="importMethod === 'hub'" :disabled="hubImportLoading || hubSelection.length === 0 || footerBusy" :loading="hubImportLoading" type="primary" @click="confirmHubImportAction">拉取并导入</el-button>
        <el-button v-if="isEdit" type="primary" :loading="submitLoading" :disabled="footerBusy" @click="submitForm">确定</el-button>
      </template>
    </el-dialog>

    <!-- 数据查看/编辑对话框 -->
    <el-dialog v-model="dataDialogVisible" title="预览数据" width="1200px">
      <div style="margin-bottom: 16px">
        <el-button type="primary" :disabled="dataRowOpLoading" @click="openAddDataDialog">添加数据</el-button>
      </div>
      <!-- scrollbar-always-on：字段列多时（实测 29 列 = 4310px）横向滚动条常显可拖，
           否则 EP 默认只在 hover 数据区时冒出 6px 细条，用户看不到也拖不到 -->
      <el-table :data="dataList" max-height="500" scrollbar-always-on>
        <el-table-column prop="index" label="序号" width="60" />
        <!-- 动态生成所有字段列（排除 _is_media 控制字段） -->
        <el-table-column v-for="field in editableDataFields"
          :key="field.name"
          :prop="field.name"
          :label="field.label || field.name"
          show-overflow-tooltip min-width="150">
          <!-- 字段描述最长 195 字符，表头换行会把表头顶到 362px、数据区只剩 138px；
               这里单行省略，hover 用 title 看全文 -->
          <template #header>
            <span class="table-header-ellipsis" :title="field.label || field.name">{{ field.label || field.name }}</span>
          </template>
          <template #default="{ row }">
            <!-- hub_file 字段：file_id 截断 + 文件图标 + 「文件」按钮（纯静态渲染，不发文件树请求） -->
            <div v-if="field.type === 'hub_file' && row[field.name]" class="hub-file-cell">
              <el-icon class="hub-file-icon"><Box /></el-icon>
              <span class="hub-file-id" :title="row[field.name]">{{ truncateHubId(row[field.name]) }}</span>
              <el-button link type="primary" size="small" @click="openHubFileDrawer(row, field.name)">文件</el-button>
            </div>
            <!-- hub_summary：行内执行包摘要徽标（用例统计 + execution_id 截断） -->
            <el-tooltip v-else-if="hubSummaryBadge(row, field)" :content="formatCellValue(row[field.name])" placement="top">
              <span class="hub-summary-badge">
                📦 {{ hubSummaryBadge(row, field).caseTotal }}用例 · 通过{{ hubSummaryBadge(row, field).passed }} · 失败{{ hubSummaryBadge(row, field).failed }}
                <span v-if="hubSummaryBadge(row, field).executionId" class="hub-summary-exec">· {{ hubSummaryBadge(row, field).executionId }}</span>
              </span>
            </el-tooltip>
            <span v-else-if="!row[field.name + '_is_media']">{{ formatCellValue(row[field.name]) }}</span>
            <a v-else-if="row[field.name] && isValidOssUrl(row[field.name])" :href="row[field.name]" :download="getFileName(row[field.name])" target="_blank"
               style="display: flex; align-items: center; gap: 4px; color: #409eff; font-size: 12px; word-break: break-all">
              <el-icon><Document /></el-icon>{{ getFileName(row[field.name]) }}
            </a>
            <span v-else-if="row[field.name]" style="color: #909399">{{ row[field.name] }}</span>
            <span v-else style="color: #999">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="dataRowOpLoading" @click="editData(row)">编辑</el-button>
            <el-button link type="danger" :loading="deletingDataId === row.id" :disabled="dataRowOpLoading" @click="deleteData(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="dataDialogVisible = false" :disabled="dataRowOpLoading">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 添加/编辑数据对话框：左字段列表 + 右单字段编辑区。
         字段标签就是字段描述（实测平均 49 字符、最长 195 字符），并排表单标签会被
         EP 固定 32px 行高截断后溢出重叠，故改为「字段名在左、描述与控件全宽在右」 -->
    <el-dialog v-model="dataFormVisible" :title="isDataEdit ? '编辑数据' : '添加数据'" width="1200px" top="6vh" @close="handleDataFormClose">
      <div class="data-form-layout">
        <!-- 左栏：字段导航 -->
        <aside class="data-form-nav">
          <div v-if="editableDataFields.length === 0" class="data-form-nav-empty">暂无字段</div>
          <button v-for="field in editableDataFields" :key="field.name" type="button"
            class="data-form-nav-item"
            :class="{ 'is-active': field.name === activeFieldName }"
            :title="fieldNavTitle(field)"
            @click="activeFieldName = field.name">
            <span class="data-form-nav-name">{{ field.name }}</span>
            <span v-if="field.is_required" class="data-form-nav-flag">必填</span>
            <el-icon v-if="dataForm[field.name + '_is_media']" class="data-form-nav-media"><Upload /></el-icon>
          </button>
        </aside>

        <!-- 右栏：当前字段的完整描述 + 编辑控件 -->
        <section v-if="activeField" class="data-form-pane">
          <div class="data-form-pane-head">
            <span class="data-form-pane-name">{{ activeField.name }}</span>
            <el-tag v-if="activeField.is_required" type="danger" size="small" effect="plain" disable-transitions>必填</el-tag>
            <el-tag v-if="activeField.type" type="info" size="small" effect="plain" disable-transitions>{{ activeField.type }}</el-tag>
          </div>
          <p v-if="activeField.label && activeField.label !== activeField.name" class="data-form-pane-desc">{{ activeField.label }}</p>

          <el-form :model="dataForm" label-position="top">
            <!-- 统一动态字段渲染：值均绑定 dataForm[activeField.name] -->
            <el-form-item>
              <el-input v-if="activeField.type === 'string' || !activeField.type || activeField.type === 'text'" v-model="dataForm[activeField.name]" type="textarea" :rows="6" :placeholder="`请输入${activeField.label || activeField.name}`" />
              <!-- hub_file 字段：值为 anc-hub file_id，评测执行时平台自动下载落盘（设计文档五） -->
              <el-input v-else-if="activeField.type === 'hub_file'" v-model="dataForm[activeField.name]" placeholder="填 anc-hub 文件 file_id，评测时自动下载" clearable />
              <el-input-number v-else-if="activeField.type === 'number'" v-model="dataForm[activeField.name]" :min="0" style="width: 100%" />
              <el-switch v-else-if="activeField.type === 'boolean' && activeField.name.endsWith('_is_media')" v-model="dataForm[activeField.name]" />
              <!-- JSON 字段：只读美化展示（对象直接 v-model 会显示 [object Object]，且手改文本会把字符串写回破坏数据） -->
              <el-input v-else-if="activeField.type === 'json'" :model-value="formatCellValue(dataForm[activeField.name])" type="textarea" :rows="14" readonly disabled />
              <el-input v-else v-model="dataForm[activeField.name]" type="textarea" :rows="6" :placeholder="`请输入${activeField.label || activeField.name}`" />
            </el-form-item>
            <!-- is_media 开关 -->
            <el-form-item v-if="activeField.is_media || dataForm[activeField.name + '_is_media'] !== undefined" label="关联附件">
              <el-switch v-model="dataForm[activeField.name + '_is_media']" active-text="是" inactive-text="否" />
            </el-form-item>
            <!-- 文件上传区域（当is_media为是时显示）：file-list 绑 pendingFiles，
                 切字段再切回来时已选文件仍可见可删 -->
            <el-form-item v-if="dataForm[activeField.name + '_is_media']" label="上传">
              <el-upload
                :ref="el => { if(el) dataFormUploadRefs[activeField.name] = el }"
                :auto-upload="false"
                :limit="5"
                :file-list="pendingFiles[activeField.name] || []"
                :on-change="(file, fileList) => handleDataFormFileChange(file, fileList, activeField.name)"
                :on-remove="(file, fileList) => handleDataFormFileRemove(file, fileList, activeField.name)"
                accept=".jpg,.jpeg,.png,.gif,.bmp,.webp,.mp3,.wav,.aac,.flac,.ogg,.m4a,.pdf,.doc,.docx,.txt,.md,.xlsx,.xls,.csv"
                multiple
              >
                <el-button type="success" :disabled="fileRowOpLoading">
                  <el-icon><Upload /></el-icon>
                  选择文件
                </el-button>
              </el-upload>
            </el-form-item>
            <!-- 已上传文件列表（编辑时显示） -->
            <el-form-item v-if="isDataEdit && dataForm[activeField.name + '_is_media'] && dataForm[activeField.name + '_editedFiles']?.length > 0" label="已上传">
              <div v-for="file in dataForm[activeField.name + '_editedFiles']" :key="file.id" style="display: flex; align-items: center; margin-bottom: 4px">
                <el-icon><Document /></el-icon>
                <span style="margin-left: 4px; flex: 1; overflow: hidden; text-overflow: ellipsis;">{{ file.file_name }}</span>
                <el-button link type="danger" size="small" :loading="deletingFileId === file.id" :disabled="fileRowOpLoading" @click="deleteEditedDataFile(file, activeField.name)">删除</el-button>
              </div>
            </el-form-item>
          </el-form>
        </section>
        <section v-else class="data-form-pane data-form-pane-empty">请选择左侧字段</section>
      </div>
      <template #footer>
        <el-button @click="dataFormVisible = false" :disabled="dataFormLoading || fileRowOpLoading">取消</el-button>
        <el-button type="primary" :loading="dataFormLoading" :disabled="fileRowOpLoading" @click="submitDataForm">确定</el-button>
      </template>
    </el-dialog>

    <!-- 执行包「文件」抽屉（F3）：摘要卡 + 文件树 + 节点预览/下载 -->
    <HubFileDrawer v-model:visible="hubDrawerVisible" :file-id="hubDrawerFileId" :summary="hubDrawerSummary" />

    <!-- 版本抽屉（M5 QE5-1）：打版历史 + 行级 diff 双栏 + 恢复 + 从此版本建任务 -->
    <el-drawer v-model="versionDrawerVisible" :title="`版本管理 - ${versionDataset?.name || ''}`" size="72%" :close-on-click-modal="false">
      <div v-loading="versionsLoading" class="version-drawer-body">
        <!-- 打版区 -->
        <div class="version-actions">
          <el-input
            v-model="newVersionChangelog"
            placeholder="版本变更说明（可选，如：新增 20 条边界样本）"
            style="max-width: 380px"
            maxlength="500"
            :disabled="versionCreating || readonlyRole"
            @keyup.enter="createVersion"
          />
          <el-button type="primary" :loading="versionCreating" :disabled="readonlyRole" :title="readonlyRole ? '当前角色（viewer）只读' : ''" @click="createVersion">
            以当前数据打版
          </el-button>
          <span class="version-hint">打版后版本不可变，可 diff / 恢复 / 锁定版本运行评测</span>
        </div>

        <!-- 版本列表 -->
        <el-table :data="versions" size="small" border>
          <el-table-column prop="version_no" label="版本号" width="80" align="center">
            <template #default="{ row }">
              <el-tag size="small">v{{ row.version_no }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="row_count" label="行数" width="90" align="center" />
          <el-table-column prop="changelog" label="变更说明" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.changelog || '-' }}</template>
          </el-table-column>
          <el-table-column prop="created_by" label="创建人" width="120" show-overflow-tooltip>
            <template #default="{ row }">{{ row.created_by || '-' }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="260" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="diffWithLatest(row)">与最新版对比</el-button>
              <el-button link type="primary" size="small" :disabled="readonlyRole" @click="restoreVersion(row)">恢复到此版本</el-button>
              <el-button link type="primary" size="small" @click="createTaskFromVersion(row)">从此版本建任务</el-button>            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!versionsLoading && versions.length === 0" description="暂无版本，点「以当前数据打版」创建第一个版本" :image-size="60" />

        <!-- 行级 diff 双栏 -->
        <div v-if="diffResult" class="version-diff">
          <el-divider content-position="left">
            版本对比 v{{ diffResult.from.version_no }} → v{{ diffResult.to.version_no }}
          </el-divider>
          <div class="diff-summary">
            <el-tag type="success">新增 {{ diffResult.summary.added }}</el-tag>
            <el-tag type="danger">删除 {{ diffResult.summary.removed }}</el-tag>
            <el-tag type="warning">变更 {{ diffResult.summary.changed }}</el-tag>
            <el-tag type="info">未变 {{ diffResult.summary.unchanged }}</el-tag>
            <span class="version-hint">行数 {{ diffResult.summary.from_count }} → {{ diffResult.summary.to_count }}</span>
            <el-tag v-if="diffResult.truncated" type="warning">差异行超过 500 条，仅展示前 500 条</el-tag>
          </div>
          <!-- 双栏头（左=from 快照值 / 右=to 快照值，展示值超长已由后端截断） -->
          <div class="diff-table">
            <div class="diff-row diff-head">
              <div class="diff-cell diff-meta">行</div>
              <div class="diff-cell">v{{ diffResult.from.version_no }}（{{ formatTime(diffResult.from.created_at) || '-' }}）</div>
              <div class="diff-cell">v{{ diffResult.to.version_no }}（{{ formatTime(diffResult.to.created_at) || '-' }}）</div>
            </div>
            <div v-for="row in diffResult.rows" :key="row.key" class="diff-row" :class="`diff-${row.status}`">
              <div class="diff-cell diff-meta">
                <el-tag :type="diffStatusTag(row.status)" size="small">{{ diffStatusText(row.status) }}</el-tag>
                <div class="diff-index">{{ row.status === 'added' ? `#${row.to_index}` : `#${row.from_index}` }}</div>
              </div>
              <div class="diff-cell">{{ row.status === 'added' ? '（不存在）' : renderRowData(row.from_data) }}</div>
              <div class="diff-cell">{{ row.status === 'removed' ? '（不存在）' : renderRowData(row.to_data, row.changed_fields) }}</div>
            </div>
            <el-empty
              v-if="diffResult.rows.length === 0"
              description="两版本行级数据完全一致"
              :image-size="60"
            />
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { datasetApi, ossApi, hubApi, preJudgeScriptApi, hubImportApi } from '@/api'
import { useTaskWebSocket } from '@/composables/useTaskWebSocket'
import { Document, Download, Upload, CircleCheck, CircleClose, Warning, Box, Loading } from '@element-plus/icons-vue'
import { formatTime } from '@/utils/time'
import HubFileDrawer from '@/components/HubFileDrawer.vue'
import { canWrite } from '@/utils/permissions'

// M4 QE4-3：viewer 只读——写按钮禁用联动（canWrite 见 utils/permissions）
const currentUser = inject('currentUser', null)
const readonlyRole = computed(() => !canWrite(currentUser.value))

const datasets = ref([])
const dialogVisible = ref(false)
const dataDialogVisible = ref(false)
const dataFormVisible = ref(false)
const importDialogVisible = ref(false)
const isEdit = ref(false)
const isDataEdit = ref(false)
const formRef = ref(null)
const uploadRef = ref(null)
const dataList = ref([])
const currentDatasetId = ref(null)
const currentDataId = ref(null)
const importType = ref('excel')
const importFile = ref(null)
const importJson = ref('')

// 导入弹窗表单数据
const importFormRef = ref(null)
const importUploadRef = ref(null)
const importForm = ref({
  name: '',
  description: ''
})
const importPreviewData = ref([])
const importPreviewFields = ref([])  // 导入预览数据的字段元数据（支持多字段）
const importTempDatasetId = ref(null)  // 导入预览用的临时ID

// 上传加载状态
const uploadLoading = ref(false)
const importLoading = ref(false)
const importDialogLoading = ref(false)

// 写操作 loading 状态
const cleanupLoading = ref(false)
const submitLoading = ref(false)
const deletingId = ref(null)
const dataFormLoading = ref(false)
const deletingDataId = ref(null)
const deletingFileId = ref(null)

// ==================== 会话文件导入（模块 E） ====================
// 导入方式：excel=Excel 数据导入，session=Agent 会话文件导入
const importMethod = ref('excel')
const sessionUploadRef = ref(null)
const sessionFiles = ref([])          // 已选择的会话文件（File 对象列表）
const sessionPreviewLoading = ref(false)
const sessionImportLoading = ref(false)
const sessionPreview = ref(null)      // 会话预览结果 {is_valid, total_sessions, valid_sessions, invalid_sessions, items}
let sessionPreviewTimer = null        // 多文件一次选择时的预览防抖定时器

// ==================== 从 anc-hub 导入（hub_file 通道） ====================
const hubTableRef = ref(null)
const hubQueryLoading = ref(false)
const hubImportLoading = ref(false)
const hubQueryError = ref('')   // 远端浏览错误（后端 detail 为中文，直接展示）
const hubImportError = ref('')  // 拉取导入错误
const hubFiles = ref([])        // 规范化后的远端文件列表
const hubTotal = ref(0)
const hubQuery = ref({ name_like: '', uploader: '', dateRange: null })  // dateRange: ['YYYY-MM-DD', 'YYYY-MM-DD']
// 导入形态（W14 起固定为异步执行包 job 链路）：每包一行，可选挂载脚本库清洗脚本
// ——脚本产出的行内容即数据集数据；原「会话数据」同步 jsonl 解析链路已废弃
const hubCleanScripts = ref([])   // 可选清洗脚本（脚本库 exec_type=script 且启用）
const hubCleanScriptId = ref('')  // 选中的清洗脚本 ID（空=不清洗）
const hubPage = ref(1)
const hubPageSize = ref(10)
const hubSelection = ref([])    // 勾选行（跨页保留）

// ==================== 执行包导入 job 进度（W14：WS 推送 + 轮询兜底） ====================
const hubJob = ref(null)            // 当前关注的导入 job（进度面板数据源，GET/WS 共用形状）
const hubJobRetrying = ref(false)   // 失败项重试进行中
let hubJobWatchStartedAt = 0        // 本次关注起算时间（兜底轮询超时用）
let hubJobPollTimer = null          // WS 不可用时的轮询兜底定时器
const HUB_JOB_FALLBACK_POLL_MS = 3000          // 兜底轮询间隔
const HUB_JOB_WATCH_TIMEOUT_MS = 15 * 60 * 1000 // 关注时长上限（job 仍在后台执行，仅停止关注）

// ==================== 执行包「文件」抽屉（F2/F3） ====================
const hubDrawerVisible = ref(false)
const hubDrawerFileId = ref('')
const hubDrawerSummary = ref(null)

// ==================== 版本抽屉（M5 QE5-1：打版/diff/恢复/从此版本建任务） ====================
const router = useRouter()
const versionDrawerVisible = ref(false)
const versionDataset = ref(null)      // 当前抽屉对应数据集行
const versions = ref([])              // 版本列表（新→旧）
const versionsLoading = ref(false)
const versionCreating = ref(false)    // 打版进行中
const newVersionChangelog = ref('')   // 打版变更说明输入
const diffResult = ref(null)          // 行级 diff 结果（{from, to, summary, rows, truncated}）

const openVersions = async (row) => {
  versionDataset.value = row
  versionDrawerVisible.value = true
  diffResult.value = null
  newVersionChangelog.value = ''
  await loadVersions(row.id)
}

const loadVersions = async (datasetId) => {
  if (!datasetId) return
  versionsLoading.value = true
  try {
    const res = await datasetApi.listVersions(datasetId)
    versions.value = res || []
  } catch (e) {
    console.error('获取版本列表失败:', e)
    versions.value = []
  } finally {
    versionsLoading.value = false
  }
}

// 打版：以数据集当前数据创建不可变版本
const createVersion = async () => {
  if (!versionDataset.value || versionCreating.value) return
  versionCreating.value = true
  try {
    const version = await datasetApi.createVersion(versionDataset.value.id, {
      changelog: newVersionChangelog.value?.trim() || null
    })
    ElMessage.success(`打版成功：v${version.version_no}（${version.row_count} 行）`)
    newVersionChangelog.value = ''
    await loadVersions(versionDataset.value.id)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '打版失败')
  } finally {
    versionCreating.value = false
  }
}

// 与最新版对比（列表新→旧，最新版即 versions[0]）
const diffWithLatest = async (row) => {
  const latest = versions.value[0]
  if (!latest) return
  if (latest.version_no === row.version_no) {
    ElMessage.info('该版本已是最新版，无对比意义')
    return
  }
  await loadDiff(row.version_no, latest.version_no)
}

const loadDiff = async (fromVersion, toVersion) => {
  if (!versionDataset.value) return
  try {
    const res = await datasetApi.diffVersions(versionDataset.value.id, fromVersion, toVersion)
    diffResult.value = res
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '获取版本 diff 失败')
  }
}

// 恢复到指定版本：破坏性（当前活行软删后按版本快照重写），二次确认
const restoreVersion = async (row) => {
  try {
    await ElMessageBox.confirm(
      `恢复将用 v${row.version_no}（${row.row_count} 行）整体替换当前数据（含字段结构），当前未打版的数据变更将丢失。确定恢复？`,
      '恢复数据集版本',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' }
    )
  } catch { return }
  try {
    const res = await datasetApi.restoreVersion(versionDataset.value.id, row.version_no)
    ElMessage.success(`已恢复到 v${row.version_no}（重写 ${res.restored_count} 行）`)
    await loadDatasets()
    await loadVersions(versionDataset.value.id)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '恢复失败')
  }
}

// 从此版本建任务：带 dataset_id + dataset_version_no 跳任务页（Tasks 读取 query 预选+锁定）
const createTaskFromVersion = (row) => {
  versionDrawerVisible.value = false
  router.push({
    path: '/tasks',
    query: { dataset_id: versionDataset.value.id, dataset_version_no: row.version_no }
  })
}

// diff 行状态 → 标签
const diffStatusTag = (status) => ({ added: 'success', removed: 'danger', changed: 'warning' }[status] || 'info')
const diffStatusText = (status) => ({ added: '新增', removed: '删除', changed: '变更' }[status] || status)

// diff 单元格渲染：字段名: 值 逐行拼接；变更行高亮变更字段
const renderRowData = (data, changedFields) => {
  if (data == null) return ''
  if (typeof data !== 'object') return String(data)
  const changed = new Set(changedFields || [])
  return Object.entries(data)
    .map(([key, value]) => {
      const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
      return changed.has(key) ? `【${key}: ${text}】` : `${key}: ${text}`
    })
    .join('\n')
}


// 打开文件抽屉：file_id 取 hub_file 字段值，摘要取同行 hub_summary（存在时）
const openHubFileDrawer = (row, fieldName) => {
  const fileId = row?.[fieldName]
  if (!fileId) return
  hubDrawerFileId.value = String(fileId)
  const summary = row?.hub_summary
  hubDrawerSummary.value = summary && typeof summary === 'object' ? summary : null
  hubDrawerVisible.value = true
}

// file_id 截断展示（保留首尾）
const truncateHubId = (id) => {
  const s = String(id || '')
  if (s.length <= 16) return s
  return s.slice(0, 8) + '…' + s.slice(-6)
}

// hub_summary 徽标内容：非空对象且含任一统计/execution 字段时返回展示数据，否则 null（回落 JSON 文本展示）
const hubSummaryBadge = (row, field) => {
  if (field?.name !== 'hub_summary') return null
  const s = row?.[field.name]
  if (!s || typeof s !== 'object' || Array.isArray(s)) return null
  const hasStat = s.execution_id || s.case_total !== undefined || s.case_passed !== undefined || s.case_failed !== undefined || s.file_count !== undefined
  if (!hasStat) return null
  const execId = s.execution_id ? truncateHubId(String(s.execution_id)) : ''
  return {
    caseTotal: s.case_total ?? 0,
    passed: s.case_passed ?? 0,
    failed: s.case_failed ?? 0,
    executionId: execId
  }
}

// 预览数据相关
const previewData = ref([])
const previewFields = ref([])  // 预览数据的字段元数据（支持多字段）
const previewInvalidCount = ref(0)
const tempDatasetId = ref(null)  // 临时保存刚创建的数据集ID

// 从 URL 或路径中提取文件名
const getFileName = (url) => {
  if (!url) return ''
  try {
    console.log("URL:------------------------>", url)
    const decoded = decodeURIComponent(url)
    return decoded.split('/').pop().split('?')[0] || url
  } catch {
    return url.split('/').pop() || url
  }
}

// OSS URL 校验正则（基于 VITE_OSS_BASE_URL 动态构造；未配置时一律视为非 OSS 链接）
const OSS_URL_PATTERN = ossApi.ossUrlPattern
const isValidOssUrl = (url) => !!url && !!OSS_URL_PATTERN && OSS_URL_PATTERN.test(url.trim())

// 服务端列表瘦身占位标记：超大字段（如会话数据集的 session_messages）在列表
// 接口被替换为 {__oversize__: true, size_bytes: N}，前端据此展示大小提示
const OVERSIZE_FLAG = '__oversize__'

const isOversizePlaceholder = (value) =>
  !!value && typeof value === 'object' && value[OVERSIZE_FLAG] === true

const formatBytes = (bytes) => {
  if (!Number.isFinite(bytes)) return ''
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return bytes + ' B'
}

// 单元格/只读展示的值格式化：对象（如 session_stats/session_messages）转美化 JSON，
// 避免模板字符串插值显示 [object Object] 或原始单行超长 JSON
const formatCellValue = (value) => {
  if (value === null || value === undefined || value === '') return ''
  if (typeof value === 'object') {
    if (isOversizePlaceholder(value)) {
      return `[内容过大（${formatBytes(value.size_bytes)}），列表未加载，保存时自动保留原值]`
    }
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  // 防御：超长字符串截断，避免巨型文本节点拖垮表格渲染与滚动
  const text = String(value)
  if (text.length > 5000) {
    return text.slice(0, 5000) + `\n…[共 ${text.length} 字符，已截断展示]`
  }
  return text
}

// 检查是否有未上传的关联文件（动态检测所有字段）
const hasMissingMediaFiles = computed(() => {
  const fieldsToCheck = previewFields.value.filter(f => {
    if (f.name.endsWith('_is_media')) return false
    return previewData.value.some(row => row[f.name + '_is_media'] || f.is_media)
  })
  return previewData.value.some(row => {
    return fieldsToCheck.some(field => {
      const isMediaField = field.name + '_is_media'
      const isMedia = row[isMediaField] || field.is_media
      if (isMedia) {
        return !isValidOssUrl(row[field.name])
      }
      return false
    })
  })
})

// 导入预览相关计算属性
const importPreviewInvalidCount = computed(() => {
  return importPreviewData.value.filter(row => !row.is_valid).length
})

const importHasMissingMediaFiles = computed(() => {
  return importPreviewData.value.some(row => {
    return Object.keys(row).filter(k => k.endsWith('_is_media')).some(isMediaKey => {
      if (!row[isMediaKey]) return false
      return !isValidOssUrl(row[isMediaKey.slice(0, -9)])
    })
  })
})

// 预览数据字段过滤计算属性（安全过滤，避免模板中直接调用 filter）
// 预览表格字段：直接显示所有字段，按原始顺序（已废弃 user_input/answer 分类绑定）
const previewAllFields = computed(() => {
  if (!Array.isArray(previewFields.value)) return []
  return previewFields.value.filter(f => !f.name.endsWith('_is_media'))
})

// 获取用户输入字段列表（支持多字段）
const getUserInputFields = (fields) => {
  if (!fields || fields.length === 0) {
    return [{ name: 'user_input', label: '用户输入', is_media: false }]
  }
  return fields.filter(f => f.name === 'user_input' || f.name.startsWith('user_input_'))
}

// 获取答案字段列表（支持多字段）
const getAnswerFields = (fields) => {
  if (!fields || fields.length === 0) {
    return [{ name: 'answer', label: '答案', is_media: false }]
  }
  return fields.filter(f => f.name === 'answer' || f.name.startsWith('answer_'))
}

// 文件上传相关
const baseURL = ref(import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000')
const uploadHeaders = ref({
  // 可以添加认证token
})

const dataForm = ref({})

// 添加数据时用于存储待上传的文件（按字段名索引）
const pendingFiles = ref({})
// 编辑数据时用于存储已上传的文件列表（按字段名索引）
const editedFiles = ref({})
// 上传组件refs（按字段名索引）
const dataFormUploadRefs = ref({})

// 数据表单动态字段元数据
const dataFormFields = ref([])

// 数据表单可编辑字段（排除 _is_media 控制字段）：预览表格列与编辑弹窗左栏共用
const editableDataFields = computed(() => {
  if (!Array.isArray(dataFormFields.value)) return []
  return dataFormFields.value.filter(f => !f.name.endsWith('_is_media'))
})

// 编辑弹窗当前选中的字段（右栏显示哪个字段的编辑区）
const activeFieldName = ref('')
const activeField = computed(() =>
  editableDataFields.value.find(f => f.name === activeFieldName.value) || editableDataFields.value[0] || null
)

// 左栏导航项的 hover 提示：字段名会被省略号截断，label 是字段描述，两者都给出便于辨认
const fieldNavTitle = (field) => {
  const label = field.label || ''
  return label && label !== field.name ? `${field.name} —— ${label}` : field.name
}

const form = ref({
  name: '',
  description: '',
  has_media: false
})

const rules = {
  name: [{ required: true, message: '请输入数据集名称', trigger: 'blur' }]
}

/**
 * 尝试自动修复常见的JSON格式错误
 * @param {string} jsonStr - 输入的JSON字符串
 * @returns {{ success: boolean, data?: any, error?: string, fixed?: string }}
 */
const tryFixJson = (jsonStr) => {
  // 去除首尾空白
  let fixed = jsonStr.trim()

  // 检查是否为空
  if (!fixed) {
    return { success: false, error: 'JSON内容为空' }
  }

  // 1. 修复缺少引号包裹的键名 (键名没有引号)
  fixed = fixed.replace(/([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:)/g, '$1"$2"$3')

  // 2. 修复单引号为双引号
  fixed = fixed.replace(/'/g, '"')

  // 3. 移除JavaScript风格的注释
  fixed = fixed.replace(/\/\/.*$/gm, '')
  fixed = fixed.replace(/\/\*[\s\S]*?\*\//g, '')

  // 4. 移除尾部逗号
  fixed = fixed.replace(/,(\s*[}\]])/g, '$1')

  // 5. 尝试解析
  try {
    const data = JSON.parse(fixed)
    return { success: true, data, fixed }
  } catch (e) {
    // 尝试更激进的修复：处理常见的格式问题
    try {
      // 将JavaScript对象字面量转换为JSON
      // 例如：{a: 1} -> {"a": 1}
      let aggressiveFix = fixed

      // 替换没有引号的字符串值为带引号
      aggressiveFix = aggressiveFix.replace(/:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*([,}\]])/g, ': "$1"$2')

      const data = JSON.parse(aggressiveFix)
      return { success: true, data, fixed: aggressiveFix }
    } catch (e2) {
      // 提取错误位置信息
      const match = e.message.match(/position (\d+)/)
      let errorMsg = 'JSON格式错误'

      if (match) {
        const pos = parseInt(match[1])
        const lines = fixed.substring(0, pos).split('\n')
        const line = lines.length
        const column = lines[lines.length - 1].length + 1
        errorMsg = `JSON格式错误：位于第 ${line} 行，第 ${column} 列附近\n错误信息: ${e.message}`
      } else {
        errorMsg = `JSON格式错误：${e.message}`
      }

      return { success: false, error: errorMsg }
    }
  }
}

/**
 * 校验JSON格式，尝试自动修复
 * @returns {{ success: boolean, data?: any }}
 */
const validateAndFixJson = () => {
  if (!importJson.value.trim()) {
    ElMessage.warning('请输入JSON数据')
    return { success: false }
  }

  const result = tryFixJson(importJson.value)

  if (result.success) {
    // 如果进行了修复，提示用户
    if (result.fixed !== importJson.value.trim()) {
      ElMessage.warning('JSON格式已自动修复')
      importJson.value = result.fixed
    }
    return { success: true, data: result.data }
  } else {
    ElMessage.error(result.error)
    return { success: false }
  }
}

const loadDatasets = async () => {
  try {
    const res = await datasetApi.list({ limit: 100 })
    datasets.value = res || []
  } catch (e) {
    ElMessage.error('加载失败')
  }
}

const openDialog = (row = null) => {
  // 清空上传组件的文件列表
  uploadRef.value?.clearFiles()

  // 重置上一次导入 job 的进度面板与关注态（新导入从干净面板开始）
  resetHubJobWatch()

  // 重置预览数据
  previewData.value = []
  previewInvalidCount.value = 0
  tempDatasetId.value = null
  importFile.value = null

  // 重置导入方式与会话导入状态
  importMethod.value = 'excel'
  clearSessionImportState()
  clearHubImportState()

  if (row) {
    isEdit.value = true
    form.value = { ...row }
  } else {
    isEdit.value = false
    form.value = {
      name: '',
      description: '',
      has_media: false
    }
  }
  dialogVisible.value = true
}

// 关闭对话框时重置
const handleDialogClose = async () => {
  // 不需要删除数据集，因为没有创建真正的数据集
  // 预览缓存会在后端自动过期

  // 清空上传组件的文件列表
  uploadRef.value?.clearFiles()
  previewData.value = []
  previewInvalidCount.value = 0
  tempDatasetId.value = null
  importFile.value = null

  // 重置导入方式与会话导入状态
  importMethod.value = 'excel'
  clearSessionImportState()
  clearHubImportState()
}

// 下载 blob 文件
const downloadBlob = (blob, filename) => {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.setAttribute('download', filename)
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

// 从 Content-Disposition 解析文件名（后端使用 RFC 5987 的 filename*=UTF-8'' 形式）
const parseFilename = (disposition, fallback) => {
  if (!disposition) return fallback
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (utf8Match) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      // 解码失败则回退到默认名
    }
  }
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i)
  return plainMatch ? plainMatch[1] : fallback
}

// 下载模板
const downloadTemplate = async () => {
  try {
    const response = await datasetApi.downloadTemplate()
    downloadBlob(new Blob([response]), '数据集模板.xlsx')
    ElMessage.success('模板下载成功')
  } catch (e) {
    ElMessage.error('模板下载失败')
  }
}

// 导出数据集为 Excel
const exportingId = ref(null)
const exportDataset = async (row) => {
  if (exportingId.value !== null) return
  exportingId.value = row.id
  try {
    const response = await datasetApi.exportExcel(row.id)
    const blob = new Blob([response.data], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    })
    const filename = parseFilename(
      response.headers?.['content-disposition'],
      `${row.name || 'dataset'}_${Date.now()}.xlsx`
    )
    downloadBlob(blob, filename)
    ElMessage.success('导出成功')
  } catch (e) {
    console.error('导出失败:', e)
    ElMessage.error('导出失败: ' + (e.message || '未知错误'))
  } finally {
    exportingId.value = null
  }
}

// ==================== 写操作 loading 联动禁用（同区域其它按钮互斥禁用） ====================
// 数据集列表行级：删除/导出任一进行中，操作列其它按钮禁用
const rowOpLoading = computed(() => deletingId.value !== null || exportingId.value !== null)
// 预览数据弹窗行级：数据行删除进行中
const dataRowOpLoading = computed(() => deletingDataId.value !== null)
// 添加/编辑数据弹窗文件行级：已上传文件删除进行中
const fileRowOpLoading = computed(() => deletingFileId.value !== null)
// 编辑数据集弹窗 footer：保存/任一导入/上传任一进行中
const footerBusy = computed(() => submitLoading.value || importLoading.value || sessionImportLoading.value || hubImportLoading.value || uploadLoading.value)

// 处理文件选择变化
const handleFileChange = async (file) => {
  if (!form.value.name.trim()) {
    ElMessage.warning('请先输入数据集名称')
    uploadRef.value?.clearFiles()
    return
  }

  // 如果之前已经有文件，先清除旧文件再添加新文件
  if (uploadRef.value?.fileList?.length > 0) {
    uploadRef.value.clearFiles()
  }

  importFile.value = file.raw

  uploadLoading.value = true
  try {
    // 只有在没有临时ID时才生成新的（避免重复选择文件时丢失已上传的文件）
    if (!tempDatasetId.value) {
      tempDatasetId.value = 'preview_' + Date.now()
    }

    // 预览导入（校验）- 使用 v2 API 支持多字段
    const formData = new FormData()
    formData.append('file', importFile.value)
    const previewRes = await datasetApi.previewImportV2(tempDatasetId.value, formData)

    // 确保 fields 是数组
    const fieldsData = Array.isArray(previewRes.fields) ? previewRes.fields : []
    const itemsData = Array.isArray(previewRes.items) ? previewRes.items : []

    previewData.value = itemsData
    previewFields.value = fieldsData
    previewInvalidCount.value = previewRes.invalid_rows || 0

    // 调试日志
    console.log('[DEBUG] previewRes.fields:', previewRes.fields)
    console.log('[DEBUG] previewRes.fields type:', typeof previewRes.fields)
    console.log('[DEBUG] fieldsData:', fieldsData)
    console.log('[DEBUG] itemsData:', itemsData)

    if (previewInvalidCount.value > 0) {
      ElMessage.warning(`数据校验失败：存在 ${previewInvalidCount.value} 条错误`)
    }
  } catch (e) {
    ElMessage.error(e.message || '文件处理失败')
    previewData.value = []
    previewInvalidCount.value = 0
    tempDatasetId.value = null
  } finally {
    uploadLoading.value = false
  }
}

// 重新选择文件
const handleReUpload = async () => {
  // 不需要删除数据集，因为没有创建真正的数据集
  // 重置状态
  tempDatasetId.value = null
  previewData.value = []
  previewInvalidCount.value = 0
  importFile.value = null
  uploadRef.value?.clearFiles()
}

// 导入对话框关闭时重置
const handleImportDialogClose = async () => {
  importForm.value = { name: '', description: '' }
  importPreviewData.value = []
  importTempDatasetId.value = null
  importFile.value = null
  importUploadRef.value?.clearFiles()
}

// 重新选择导入文件
const handleReImportUpload = async () => {
  importTempDatasetId.value = null
  importPreviewData.value = []
  importFile.value = null
  importUploadRef.value?.clearFiles()
}

// 导入预览 - 处理文件选择变化（OSS 直传）
const handleImportPreviewFileChange = async (file, fileList, row, field = 'answer') => {
  const uploadOptions = {
    file: file.raw,
    onError: (e) => handleUploadError(e)
  }
  await handleImportManualUpload(uploadOptions, row, field)
}

// 导入预览 - 手动上传文件（OSS 直传）
const handleImportManualUpload = async (options, row, field = 'answer') => {
  const { file, onError } = options
  importDialogLoading.value = true
  try {
    const ossUrl = await ossApi.upload(file)
    if (!ossUrl) {
      throw new Error('上传成功但未获取到下载地址')
    }
    // 将 OSS URL 直接写入对应字段
    const index = importPreviewData.value.findIndex(item => item.index === row.index)
    if (index !== -1) {
      importPreviewData.value[index][field] = ossUrl

      // 上传成功后，重新校验该行是否有效
      const item = importPreviewData.value[index]
      const errors = []

      // 动态检查所有媒体字段
      for (const f of importPreviewFields.value || []) {
        if (f.name.endsWith('_is_media')) continue
        const isMediaField = f.name + '_is_media'
        if (item[isMediaField] && !isValidOssUrl(item[f.name])) {
          errors.push(`请上传${f.label || f.name}关联的媒体文件`)
        }
      }

      item.errors = errors
      item.is_valid = errors.length === 0
    }
    ElMessage.success('上传成功')
  } catch (e) {
    console.error('上传失败', e)
    if (onError) onError(e)
    ElMessage.error(e.message || '上传失败')
  } finally {
    importDialogLoading.value = false
  }
}

// 导入预览 - 文件上传成功
const handleImportPreviewFileUpload = (response, row) => {
  ElMessage.success('上传成功')
  // 直接更新本地数据
  const index = importPreviewData.value.findIndex(item => item.index === row.index)
  if (index !== -1) {
    if (!importPreviewData.value[index].temp_files) {
      importPreviewData.value[index].temp_files = []
    }
    // 避免重复添加
    const exists = importPreviewData.value[index].temp_files.some(f => f.id === response.id)
    if (!exists) {
      importPreviewData.value[index].temp_files.push(response)
    }
  }
}

// 导入预览 - 删除临时文件
const deleteImportPreviewTempFile = async (row, file) => {
  if (!file || !file.id) {
    ElMessage.error('文件信息无效')
    return
  }
  try {
    await datasetApi.deleteTempFile(importTempDatasetId.value, row.index, file.id)
    ElMessage.success('删除成功')
    // 直接更新本地数据
    const index = importPreviewData.value.findIndex(item => item.index === row.index)
    if (index !== -1 && importPreviewData.value[index].temp_files) {
      importPreviewData.value[index].temp_files = importPreviewData.value[index].temp_files.filter(f => f.id !== file.id)
    }
  } catch (e) {
    console.error('删除失败', e)
    ElMessage.error(e.message || '删除失败')
  }
}

// 文件被移除时清空预览数据
const handleFileRemove = () => {
  previewData.value = []
  previewInvalidCount.value = 0
  importFile.value = null
}

// 编辑态导入前先保存基本信息（名称/描述），失败则中止导入
const saveBasicInfoIfEdit = async () => {
  if (!isEdit.value) return true
  try {
    await datasetApi.update(form.value.id, form.value)
    return true
  } catch (e) {
    ElMessage.error(e.message || '保存基本信息失败')
    return false
  }
}

// Excel 导入核心：构建 FormData 并提交（成功后关闭弹窗并刷新列表）
const doExcelImport = async () => {
  // 校验关联文件是否都已上传到 OSS（动态检查所有字段）
  const missingFiles = []
  for (const row of previewData.value) {
    for (const f of previewFields.value || []) {
      if (f.name.endsWith('_is_media')) continue
      if (row[f.name + '_is_media'] && !isValidOssUrl(row[f.name])) {
        missingFiles.push(`${row.row_num}行${f.label || f.name}`)
      }
    }
  }
  if (missingFiles.length > 0) {
    ElMessage.warning(`第 ${missingFiles.join(', ')} 行数据需要上传关联文件，请先上传后再导入`)
    return
  }

  importLoading.value = true
  try {
    const formData = new FormData()
    formData.append('file', importFile.value)

    // 收集 OSS URL 覆盖数据：{ index: { field_name: url, ... } }
    const ossOverrides = {}
    for (const row of previewData.value) {
      const override = {}
      for (const field of (previewFields.value || [])) {
        if (field.name.endsWith('_is_media')) continue
        const isMediaField = field.name + '_is_media'
        if (row[isMediaField] && isValidOssUrl(row[field.name])) {
          override[field.name] = row[field.name]
        }
      }
      if (Object.keys(override).length > 0) {
        ossOverrides[row.index] = override
      }
    }
    if (Object.keys(ossOverrides).length > 0) {
      formData.append('oss_overrides', JSON.stringify(ossOverrides))
    }

    if (isEdit.value) {
      // 编辑模式下的导入：传递空name表示追加到现有数据集
      // 使用预览ID（临时文件存储在预览ID下），传递真实数据集ID
      formData.append('name', '')
      formData.append('description', form.value.description)
      formData.append('real_dataset_id', form.value.id)
      formData.append('is_update', 'true')
      await datasetApi.confirmImport(tempDatasetId.value, formData)
    } else {
      // 新建模式：创建新数据集
      formData.append('name', form.value.name)
      formData.append('description', form.value.description)
      await datasetApi.confirmImport(tempDatasetId.value, formData)
    }

    ElMessage.success('导入成功')
    dialogVisible.value = false
    loadDatasets()
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  } finally {
    importLoading.value = false
  }
}

// 确认导入（编辑态先保存基本信息，再提交导入）
const confirmImport = async () => {
  if (!tempDatasetId.value || !importFile.value) {
    ElMessage.warning('请先上传文件')
    return
  }
  if (!(await saveBasicInfoIfEdit())) return
  await doExcelImport()
}

// ==================== 会话文件导入相关方法（模块 E） ====================

// 清空会话导入状态（文件列表与预览）
const clearSessionImportState = () => {
  if (sessionPreviewTimer) {
    clearTimeout(sessionPreviewTimer)
    sessionPreviewTimer = null
  }
  sessionUploadRef.value?.clearFiles()
  sessionFiles.value = []
  sessionPreview.value = null
}

// 切换导入方式时，清空其他模式的文件列表与预览状态
const handleImportMethodChange = (val) => {
  if (val === 'session') {
    // 切到会话模式：清空 Excel 与 anc-hub 导入状态
    uploadRef.value?.clearFiles()
    previewData.value = []
    previewFields.value = []
    previewInvalidCount.value = 0
    tempDatasetId.value = null
    importFile.value = null
    clearHubImportState()
  } else if (val === 'hub') {
    // 切到 anc-hub 模式：清空 Excel 与会话导入状态，并自动查询远端文件
    uploadRef.value?.clearFiles()
    previewData.value = []
    previewFields.value = []
    previewInvalidCount.value = 0
    tempDatasetId.value = null
    importFile.value = null
    clearSessionImportState()
    clearHubImportState()
    loadHubFiles()
    loadHubCleanScripts()
  } else {
    // 切到 Excel 模式：清空会话与 anc-hub 导入状态
    clearSessionImportState()
    clearHubImportState()
  }
}

// 会话文件选择变化（多选，选择后自动预览）
const handleSessionFileChange = (file, fileList) => {
  sessionFiles.value = fileList.map(f => f.raw).filter(Boolean)
  // 一次多选多个文件时 on-change 会逐个触发，防抖后统一请求预览
  if (sessionPreviewTimer) clearTimeout(sessionPreviewTimer)
  sessionPreviewTimer = setTimeout(() => {
    sessionPreviewTimer = null
    previewSessionFiles()
  }, 200)
}

// 会话文件移除
const handleSessionFileRemove = (file, fileList) => {
  sessionFiles.value = fileList.map(f => f.raw).filter(Boolean)
  if (sessionFiles.value.length === 0) {
    sessionPreview.value = null
    return
  }
  previewSessionFiles()
}

// 组装 FormData 并调用会话导入预览接口（字段名 files，多文件逐个 append）
const previewSessionFiles = async () => {
  if (sessionFiles.value.length === 0) {
    sessionPreview.value = null
    return
  }
  if (!form.value.name.trim()) {
    ElMessage.warning('请先输入数据集名称')
    sessionUploadRef.value?.clearFiles()
    sessionFiles.value = []
    sessionPreview.value = null
    return
  }
  sessionPreviewLoading.value = true
  try {
    const formData = new FormData()
    sessionFiles.value.forEach(f => formData.append('files', f))
    const res = await datasetApi.previewSessionImport(formData)
    sessionPreview.value = res
    if (res && res.invalid_sessions > 0) {
      ElMessage.warning(`存在 ${res.invalid_sessions} 个异常会话，请修正后重新上传`)
    }
  } catch (e) {
    ElMessage.error(e.message || '会话文件解析失败')
    sessionPreview.value = null
  } finally {
    sessionPreviewLoading.value = false
  }
}

// 会话模式：重新选择文件
const handleSessionReUpload = () => {
  clearSessionImportState()
}

// 会话预览表格数据
const sessionPreviewItems = computed(() => sessionPreview.value?.items || [])

// 会话模式是否可确认导入（存在可导入会话且无异常）
const sessionCanConfirm = computed(() => {
  const p = sessionPreview.value
  return !!p && p.valid_sessions > 0 && p.invalid_sessions === 0
})

// 会话预览表格行样式（异常行标红）
const sessionTableRowClassName = ({ row }) => (row.valid ? '' : 'error-row')

// 会话导入核心：组装 FormData 并提交（成功后关闭弹窗并刷新列表）
const doSessionImport = async () => {
  sessionImportLoading.value = true
  try {
    const formData = new FormData()
    sessionFiles.value.forEach(f => formData.append('files', f))
    formData.append('name', form.value.name)
    formData.append('description', form.value.description || '')
    if (isEdit.value) {
      // 编辑态追加模式：传入真实数据集 ID
      formData.append('real_dataset_id', form.value.id)
    }
    const res = await datasetApi.confirmSessionImport(formData)
    ElMessage.success(res?.message || '导入成功')
    dialogVisible.value = false
    loadDatasets()
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  } finally {
    sessionImportLoading.value = false
  }
}

// 会话模式：确认导入（编辑态先保存基本信息，再提交导入）
const confirmSessionImportAction = async () => {
  if (!sessionFiles.value.length) {
    ElMessage.warning('请先选择会话文件')
    return
  }
  if (!sessionCanConfirm.value) {
    ElMessage.warning('存在异常会话，请修正后重新上传')
    return
  }
  if (!(await saveBasicInfoIfEdit())) return
  await doSessionImport()
}

// ==================== 从 anc-hub 导入相关方法 ====================

// anc-hub file_query 为服务端透传，字段名以 hub 实现为准；这里对常见别名做兜底
const pickHubField = (row, keys) => {
  for (const k of keys) {
    const v = row?.[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return ''
}

// 规范化远端文件行（缺少 file_id 的行无法勾选导入，由调用侧过滤）
const normalizeHubFile = (row) => ({
  file_id: String(pickHubField(row, ['file_id', 'id', 'fileId'])),
  name: pickHubField(row, ['name', 'filename', 'file_name']) || '(未命名)',
  size: pickHubField(row, ['size', 'file_size', 'bytes']),
  uploaded_at: pickHubField(row, ['uploaded_at', 'upload_time', 'uploaded_time', 'created_at']),
  uploader: pickHubField(row, ['uploader', 'uploaded_by', 'owner', 'creator'])
})

// 查询 anc-hub 远端文件列表（失败时表格区展示中文错误 + 重试按钮，不白屏）
const loadHubFiles = async () => {
  hubQueryLoading.value = true
  hubQueryError.value = ''
  try {
    const body = { page: hubPage.value, page_size: hubPageSize.value }
    if (hubQuery.value.name_like && hubQuery.value.name_like.trim()) body.name_like = hubQuery.value.name_like.trim()
    if (hubQuery.value.uploader && hubQuery.value.uploader.trim()) body.uploader = hubQuery.value.uploader.trim()
    if (Array.isArray(hubQuery.value.dateRange) && hubQuery.value.dateRange.length === 2) {
      body.uploaded_after = `${hubQuery.value.dateRange[0]}T00:00:00`
      body.uploaded_before = `${hubQuery.value.dateRange[1]}T23:59:59`
    }
    const res = await hubApi.queryFiles(body)
    const rawList = Array.isArray(res) ? res : (Array.isArray(res?.files) ? res.files : (Array.isArray(res?.items) ? res.items : []))
    hubFiles.value = rawList.map(normalizeHubFile).filter(f => f.file_id)
    hubTotal.value = Number(res?.total ?? res?.total_count ?? rawList.length) || 0
  } catch (e) {
    hubQueryError.value = e.message || '查询 anc-hub 文件列表失败'
    hubFiles.value = []
    hubTotal.value = 0
    hubSelection.value = []
  } finally {
    hubQueryLoading.value = false
  }
}

// 远端筛选：查询（新搜索语义，清空跨页勾选后回到第 1 页）
const handleHubSearch = () => {
  hubPage.value = 1
  hubTableRef.value?.clearSelection?.()
  hubSelection.value = []
  loadHubFiles()
}

// 远端筛选：重置
const resetHubQuery = () => {
  hubQuery.value = { name_like: '', uploader: '', dateRange: null }
  handleHubSearch()
}

const handleHubPageChange = (page) => {
  hubPage.value = page
  loadHubFiles()
}

const handleHubPageSizeChange = (size) => {
  hubPageSize.value = size
  hubPage.value = 1
  loadHubFiles()
}

const handleHubSelectionChange = (rows) => {
  hubSelection.value = rows || []
}

// 清空 anc-hub 导入状态（弹窗关闭 / 切换导入方式时调用）
// 注意：只清浏览/勾选/表单态——进行中的导入 job 关注不中断（面板数据源 hubJob
// 保留，关闭弹窗后完成/失败仍会提示并刷新列表），新弹窗打开时由 showDialog 重置
const clearHubImportState = () => {
  hubFiles.value = []
  hubTotal.value = 0
  hubPage.value = 1
  hubSelection.value = []
  hubQueryError.value = ''
  hubImportError.value = ''
  hubCleanScriptId.value = ''
  hubQuery.value = { name_like: '', uploader: '', dateRange: null }
  hubTableRef.value?.clearSelection?.()
}

// 重置导入 job 关注态（打开新弹窗 / 卸载时调用）
const resetHubJobWatch = () => {
  stopHubJobWatch()
  hubJob.value = null
  hubJobRetrying.value = false
}

// 加载可选清洗脚本（脚本库 exec_type=script 且启用），执行包导入形态的下拉数据源
const loadHubCleanScripts = async () => {
  try {
    const res = await preJudgeScriptApi.list({ page: 1, page_size: 200 })
    const items = res?.items || res?.scripts || res || []
    hubCleanScripts.value = (Array.isArray(items) ? items : [])
      .filter(s => (s.exec_type || 'script') === 'script' && s.is_active !== false)
  } catch (e) {
    // 脚本列表拉取失败不阻断导入：下拉置空，用户仍可原样导入
    hubCleanScripts.value = []
  }
}

// anc-hub 导入核心（W14）：创建异步导入 job 后交给 WS/兜底轮询推进，
// 弹窗不再被长任务阻塞；原「会话数据」同步链路（importSessionsFromHub，
// 10 分钟超时）已废弃，统一走本链路
const doHubImport = async () => {
  hubImportLoading.value = true
  hubImportError.value = ''
  try {
    const payload = {
      file_ids: hubSelection.value.map(f => f.file_id),
      name: form.value.name,
      description: form.value.description || ''
    }
    if (isEdit.value) {
      // 编辑态追加模式：传入真实数据集 ID
      payload.real_dataset_id = form.value.id
    }
    if (hubCleanScriptId.value) {
      payload.script_id = hubCleanScriptId.value
    }
    const job = await hubImportApi.createJob(payload)
    const jobId = job?.id
    if (!jobId) throw new Error('创建导入任务失败：未返回任务 ID')

    // 进入进度面板 + 订阅推送（WS 失联自动降级兜底轮询）
    stopHubJobWatch()
    hubJob.value = { fail_detail: {}, ...job }
    hubJobWatchStartedAt = Date.now()
    startHubJobWebSocket(jobId)
    ElMessage.success(`导入任务已创建（共 ${job.total_count ?? hubSelection.value.length} 个执行包），进度见下方面板`)
  } catch (e) {
    hubImportError.value = e.message || '执行包导入失败'
    ElMessage.error(hubImportError.value)
  } finally {
    hubImportLoading.value = false
  }
}

// 合并一次进度（WS 推送与兜底轮询共用）：status/计数/进度全覆盖
const applyHubJobProgress = (patch) => {
  if (!hubJob.value || !patch) return
  hubJob.value = { ...hubJob.value, ...patch }
}

// job 到达终端状态：停关注、提示、刷新列表；失败时保留面板供重试
const finalizeHubJobWatch = async (status) => {
  stopHubJobWatch()
  const job = hubJob.value
  if (!job) return
  if (status === 'completed') {
    ElMessage.success(`执行包导入完成（成功 ${job.success_count ?? 0}/${job.total_count ?? 0}）`)
  } else if (status === 'failed') {
    // WS 推送不带 fail_detail：拉一次完整 job 补齐失败明细
    try {
      const info = await hubImportApi.getJob(job.id)
      if (info) hubJob.value = { ...hubJob.value, ...info }
    } catch (e) {
      console.warn('拉取导入失败明细失败:', e)
    }
    ElMessage.error(`执行包导入存在失败项（成功 ${hubJob.value.success_count ?? 0}/${hubJob.value.total_count ?? 0}），可在面板中重试失败项`)
  }
  loadDatasets()
}

// WS 推送回调：仅处理 hub_import_progress 消息，推进面板；终端状态收敛到 finalize
const handleHubJobWsMessage = (channelId, data) => {
  if (!data || data.type !== 'hub_import_progress') return
  applyHubJobProgress({
    status: data.status,
    success_count: data.success_count,
    fail_count: data.fail_count,
    total_count: data.total
  })
  if (data.status === 'completed' || data.status === 'failed') {
    finalizeHubJobWatch(data.status)
  }
}

// WS 通道（/ws/task/{job_id}，频道 task_status:{job_id}）；重连 3 次失败 → 兜底轮询
const { startWebSocket, stopWebSocket } = useTaskWebSocket({
  onStatusUpdate: handleHubJobWsMessage,
  onFallback: (jobId) => {
    console.warn(`导入任务 WS 不可用，降级轮询: ${jobId}`)
    startHubJobFallbackPolling(jobId)
  }
})

const startHubJobWebSocket = (jobId) => {
  stopHubJobFallbackPolling()
  startWebSocket(jobId)
}

// 兜底轮询（WS 不可用/被 4404 拒时启用；轻量 GET，3s 一次，15 分钟上限）
const startHubJobFallbackPolling = (jobId) => {
  stopHubJobFallbackPolling()
  hubJobPollTimer = setInterval(async () => {
    if (Date.now() - hubJobWatchStartedAt > HUB_JOB_WATCH_TIMEOUT_MS) {
      stopHubJobWatch()
      ElMessage.warning(`导入任务 ${jobId} 长时间未完成，已停止关注；任务仍在后台执行，可稍后刷新数据集列表查看`)
      return
    }
    try {
      const info = await hubImportApi.getJob(jobId)
      applyHubJobProgress({
        status: info?.status,
        success_count: info?.success_count,
        fail_count: info?.fail_count,
        total_count: info?.total_count,
        fail_detail: info?.fail_detail
      })
      if (info?.status === 'completed' || info?.status === 'failed') {
        finalizeHubJobWatch(info.status)
      }
    } catch (e) {
      // 单次查询失败忽略，下个周期重试
      console.warn('导入任务轮询失败:', e)
    }
  }, HUB_JOB_FALLBACK_POLL_MS)
}

const stopHubJobFallbackPolling = () => {
  if (hubJobPollTimer) {
    clearInterval(hubJobPollTimer)
    hubJobPollTimer = null
  }
}

const stopHubJobWatch = () => {
  if (hubJob.value?.id) {
    stopWebSocket(hubJob.value.id)
  }
  stopHubJobFallbackPolling()
}

// 失败项重试：只重跑失败包（已成功包幂等跳过），重挂 WS 关注
const retryHubJobFailed = async () => {
  const jobId = hubJob.value?.id
  if (!jobId) return
  hubJobRetrying.value = true
  try {
    const job = await hubImportApi.retryJob(jobId)
    hubJob.value = { ...(hubJob.value || {}), ...(job || {}) }
    hubJobWatchStartedAt = Date.now()
    startHubJobWebSocket(jobId)
    ElMessage.success('已提交失败项重试')
  } catch (e) {
    ElMessage.error(e.message || '重试失败项失败')
  } finally {
    hubJobRetrying.value = false
  }
}

// 组件卸载：停 WS 与兜底轮询（useTaskWebSocket 自身也会清理）
onUnmounted(() => {
  stopHubJobWatch()
})

// 进度面板派生态
const hubJobRunning = computed(() => ['pending', 'running'].includes(hubJob.value?.status))
const hubJobShortId = computed(() => {
  const id = hubJob.value?.id || ''
  return id.length > 14 ? `${id.slice(0, 14)}…` : id
})
const hubJobPercent = computed(() => {
  const job = hubJob.value
  if (!job?.total_count) return 0
  const done = (job.success_count || 0) + (job.fail_count || 0)
  return Math.min(100, Math.round((done * 100) / job.total_count))
})
const hubJobProgressStatus = computed(() => {
  if (hubJob.value?.status === 'completed') return 'success'
  if (hubJob.value?.status === 'failed') return 'exception'
  return undefined
})
const hubJobFailEntries = computed(() => Object.entries(hubJob.value?.fail_detail || {}))

// anc-hub 模式：确认导入（编辑态先保存基本信息，再提交导入）
const confirmHubImportAction = async () => {
  if (!hubSelection.value.length) {
    ElMessage.warning('请先勾选要导入的 anc-hub 文件')
    return
  }
  if (!form.value.name.trim()) {
    ElMessage.warning('请先输入数据集名称')
    return
  }
  if (!(await saveBasicInfoIfEdit())) return
  await doHubImport()
}

// 预览阶段文件上传成功
const handlePreviewFileUpload = (response, row, field = 'answer') => {
  ElMessage.success('上传成功')
  // 直接更新本地数据，不重新请求预览（避免内存存储问题）
  const index = previewData.value.findIndex(item => item.index === row.index)
  if (index !== -1) {
    // 根据field维护对应字段的临时文件列表
    const tempFilesKey = field + '_temp_files'
    if (!previewData.value[index][tempFilesKey]) {
      previewData.value[index][tempFilesKey] = []
    }
    // 避免重复添加
    const exists = previewData.value[index][tempFilesKey].some(f => f.id === response.id)
    if (!exists) {
      previewData.value[index][tempFilesKey].push(response)
    }

    // 将 OSS URL 写入对应字段（这样 confirmImport 时 ossOverrides 才能正确收集）
    // response 中应该包含 oss_url 或直接的 url
    const ossUrl = response.oss_url || response.url || response.file_path
    if (ossUrl && isValidOssUrl(ossUrl)) {
      previewData.value[index][field] = ossUrl
    }

    // 上传成功后，重新校验该行是否有效
    const item = previewData.value[index]
    const errors = []

    // 检查所有媒体字段（包括自定义多字段）
    const allFields = previewFields.value || []
    for (const field of allFields) {
      if (field.name.endsWith('_is_media')) continue
      const isMediaField = field.name + '_is_media'
      if (item[isMediaField]) {
        // 检查是否有临时文件或有效的 OSS URL
        const tempFilesKey = field.name + '_temp_files'
        const hasTempFiles = item[tempFilesKey] && item[tempFilesKey].length > 0
        const hasValidUrl = item[field.name] && isValidOssUrl(item[field.name])
        if (!hasTempFiles && !hasValidUrl) {
          errors.push(`请上传${field.label || field.name}关联的媒体文件`)
        }
      }
    }

    // 更新校验状态
    item.errors = errors
    item.is_valid = errors.length === 0

    // 重新计算错误数量
    previewInvalidCount.value = previewData.value.filter(item => !item.is_valid).length
  }
}

// 处理文件选择变化（OSS 直传）
const handlePreviewFileChange = async (file, fileList, row, field = 'answer') => {
  const uploadOptions = {
    file: file.raw,
    onError: (e) => handleUploadError(e)
  }
  await handleManualUpload(uploadOptions, row, field)
}

// 手动上传文件（OSS 直传）
const handleManualUpload = async (options, row, field = 'answer') => {
  const { file, onError } = options
  uploadLoading.value = true
  try {
    const ossUrl = await ossApi.upload(file)
    if (!ossUrl) {
      throw new Error('上传成功但未获取到下载地址')
    }
    // 将 OSS URL 直接写入对应字段
    const index = previewData.value.findIndex(item => item.index === row.index)
    if (index !== -1) {
      previewData.value[index][field] = ossUrl

      // 上传成功后，重新校验该行是否有效
      const item = previewData.value[index]
      const errors = []

      // 动态检查所有媒体字段
      for (const f of previewFields.value || []) {
        if (f.name.endsWith('_is_media')) continue
        const isMediaField = f.name + '_is_media'
        if (item[isMediaField] && !isValidOssUrl(item[f.name])) {
          errors.push(`请上传${f.label || f.name}关联的媒体文件`)
        }
      }

      item.errors = errors
      item.is_valid = errors.length === 0
      previewInvalidCount.value = previewData.value.filter(item => !item.is_valid).length
    }
    ElMessage.success('上传成功')
  } catch (e) {
    console.error('上传失败', e)
    if (onError) onError(e)
    ElMessage.error(e.message || '上传失败')
  } finally {
    uploadLoading.value = false
  }
}

// 删除预览阶段上传的临时文件
const deletePreviewTempFile = async (row, file, field = 'answer') => {
  if (!file || !file.id) {
    ElMessage.error('文件信息无效')
    return
  }
  try {
    await datasetApi.deleteTempFile(tempDatasetId.value, row.index, file.id, field)
    ElMessage.success('删除成功')
    // 直接更新本地数据
    const index = previewData.value.findIndex(item => item.index === row.index)
    if (index !== -1) {
      const tempFilesKey = field + '_temp_files'
      if (previewData.value[index][tempFilesKey]) {
        previewData.value[index][tempFilesKey] = previewData.value[index][tempFilesKey].filter(f => f.id !== file.id)
      }

      // 删除文件后，重新校验该行是否有效
      const item = previewData.value[index]
      const errors = []

      // 动态检查所有媒体字段
      for (const f of previewFields.value || []) {
        if (f.name.endsWith('_is_media')) continue
        if (item[f.name + '_is_media']) {
          const tempFilesKey = f.name + '_temp_files'
          const hasTempFiles = item[tempFilesKey]?.length > 0
          const hasValidUrl = item[f.name] && isValidOssUrl(item[f.name])
          if (!hasTempFiles && !hasValidUrl) errors.push(`Please upload media file for ${f.label || f.name}`)
        }
      }

      // 更新校验状态
      item.errors = errors
      item.is_valid = errors.length === 0

      // 重新计算错误数量
      previewInvalidCount.value = previewData.value.filter(item => !item.is_valid).length
    }
  } catch (e) {
    console.error('删除失败', e)
    ElMessage.error(e.message || '删除失败')
  }
}

// 表格行样式（错误行标红）
const tableRowClassName = ({ row }) => {
  if (!row.is_valid) {
    return 'error-row'
  }
  return ''
}

// 编辑态是否存在有效待导入内容（用于「确定」时询问保存并导入）
const hasPendingImportContent = computed(() => {
  if (importMethod.value === 'excel') {
    return previewData.value.length > 0 && previewInvalidCount.value === 0 && !hasMissingMediaFiles.value
  }
  if (importMethod.value === 'session') {
    return sessionCanConfirm.value
  }
  if (importMethod.value === 'hub') {
    return hubSelection.value.length > 0
  }
  return false
})

const submitForm = async () => {
  if (submitLoading.value) return
  submitLoading.value = true
  try {
    await formRef.value.validate()
    if (isEdit.value) {
      await datasetApi.update(form.value.id, form.value)
      // 已选择导入内容时，询问是否在保存基本信息后一并追加导入到当前数据集
      if (hasPendingImportContent.value) {
        let importAfterSave = false
        try {
          await ElMessageBox.confirm('已选择导入内容，是否在保存基本信息后一并导入到当前数据集？', '提示', {
            confirmButtonText: '保存并导入',
            cancelButtonText: '仅保存基本信息',
            type: 'info'
          })
          importAfterSave = true
        } catch {
          importAfterSave = false
        }
        if (importAfterSave) {
          if (importMethod.value === 'excel') return await doExcelImport()
          if (importMethod.value === 'session') return await doSessionImport()
          if (importMethod.value === 'hub') return await doHubImport()
        }
      }
      ElMessage.success('更新成功')
    } else {
      await datasetApi.create(form.value)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadDatasets()
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
  } finally {
    submitLoading.value = false
  }
}

const viewData = async (row) => {
  try {
    // 使用 V2 接口获取数据（扁平格式）
    const res = await datasetApi.dataListV2(row.id, { limit: 100 })
    let items = res?.items || []
    currentDatasetId.value = row.id

    // 获取数据集字段元数据（用于动态显示多字段列）
    let fields = []
    try {
      const fieldsRes = await datasetApi.getFields(row.id)
      fields = fieldsRes || []
      dataFormFields.value = fields
    } catch (e) {
      console.warn('getFields failed, keeping existing fields:', e)
    }

    // 如果字段元数据为空，从 V2 数据的 data 字段推导字段
    if (fields.length === 0 && items.length > 0) {
      const firstItem = items[0]
      if (firstItem.data && typeof firstItem.data === 'object') {
        fields = Object.keys(firstItem.data).map(key => ({
          name: key,
          label: key,
          type: typeof firstItem.data[key] === 'boolean' ? 'boolean' : 'string',
          is_media: false,
          is_media_field: null,
          is_required: false,
          position: 0
        }))
      } else {
        fields = []
      }
      dataFormFields.value = fields
    }

    // V2 格式数据已经在 data 字段中，直接展开
    items = items.map((item, idx) => {
      const flattened = { ...item }
      if (flattened.data && typeof flattened.data === 'object') {
        Object.keys(flattened.data).forEach(key => {
          flattened[key] = flattened.data[key]
        })
      }
      // 动态计算序号（基于当前列表索引 + 1）
      flattened.index = idx + 1
      return flattened
    })

    dataList.value = items
    dataDialogVisible.value = true
  } catch (e) {
    ElMessage.error('加载数据失败')
  }
}

const importData = (row) => {
  importForm.value.name = row.name
  importForm.value.description = row.description
  currentDatasetId.value = row.id
  importType.value = 'excel'
  importFile.value = null
  importJson.value = ''
  importDialogVisible.value = true
}

const handleImportFileChange = async (file) => {
  if (!importForm.value.name.trim()) {
    ElMessage.warning('请先输入数据集名称')
    importUploadRef.value?.clearFiles()
    return
  }

  // 如果之前已经有文件，先清除旧文件再添加新文件
  if (importUploadRef.value?.fileList?.length > 0) {
    importUploadRef.value.clearFiles()
  }

  importFile.value = file.raw

  importDialogLoading.value = true
  try {
    // 生成预览ID（不创建数据集，只用于缓存预览数据）
    importTempDatasetId.value = 'import_preview_' + Date.now()

    // 预览导入（校验）- 使用 v2 API 支持多字段
    const formData = new FormData()
    formData.append('file', importFile.value)
    const previewRes = await datasetApi.previewImportV2(importTempDatasetId.value, formData)

    importPreviewData.value = previewRes.items || []
    importPreviewFields.value = previewRes.fields || []

    if (importPreviewInvalidCount.value > 0) {
      ElMessage.warning(`数据校验失败：存在 ${importPreviewInvalidCount.value} 条错误`)
    }
  } catch (e) {
    ElMessage.error(e.message || '文件处理失败')
    importPreviewData.value = []
    importTempDatasetId.value = null
  } finally {
    importDialogLoading.value = false
  }
}

const submitImport = async () => {
  if (!importForm.value.name.trim()) {
    return ElMessage.warning('请输入数据集名称')
  }
  if (!importFile.value) {
    return ElMessage.warning('请选择文件')
  }

  importDialogLoading.value = true
  try {
    const formData = new FormData()
    formData.append('file', importFile.value)
    formData.append('name', importForm.value.name)
    formData.append('description', importForm.value.description)
    await datasetApi.confirmImport(importTempDatasetId.value, formData)

    ElMessage.success('导入成功')
    importDialogVisible.value = false
    loadDatasets()
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  } finally {
    importDialogLoading.value = false
  }
}

// 清理重复数据
const handleCleanupDuplicates = async () => {
  try {
    await ElMessageBox.confirm('此操作将删除重复的数据集（保留最早创建的），是否继续？', '提示', { type: 'warning' })
    cleanupLoading.value = true
    const res = await datasetApi.cleanupDuplicates()
    ElMessage.success(res.message || '清理完成')
    loadDatasets()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.message || '清理失败')
  } finally {
    cleanupLoading.value = false
  }
}

const deleteDataset = async (row) => {
  try {
    await ElMessageBox.confirm('确定要删除该数据集吗？', '提示', { type: 'warning' })
    deletingId.value = row.id
    await datasetApi.delete(row.id)
    ElMessage.success('删除成功')
    loadDatasets()
  } catch (e) {
    if (e !== 'cancel') {
      // 显示后端返回的具体错误信息
      const errorMsg = e.response?.data?.detail || e.message || '删除失败'
      ElMessage.error(errorMsg)
    }
  } finally {
    deletingId.value = null
  }
}

// 打开数据表单弹窗时默认选中第一个字段（右栏显示其编辑区）
const resetActiveField = () => {
  activeFieldName.value = editableDataFields.value[0]?.name || ''
}

// 添加数据相关方法
const openAddDataDialog = async () => {
  isDataEdit.value = false
  currentDataId.value = null
  dataForm.value = {}
  pendingFiles.value = {}
  editedFiles.value = {}

  // 获取数据集字段元数据（仅在成功返回非空时更新，保持预览弹窗字段不变）
  try {
    const fieldsRes = await datasetApi.getFields(currentDatasetId.value)
    if (fieldsRes && fieldsRes.length > 0) {
      dataFormFields.value = fieldsRes
    }
    // 如果返回空数组，不更新 dataFormFields，保留预览弹窗已显示的字段
  } catch (e) {
    // 失败时不重置，保持当前预览弹窗字段状态
    console.warn('getFields failed in openAddDataDialog, keeping existing fields:', e)
  }

  resetActiveField()
  dataFormVisible.value = true
}

const editData = async (row) => {
  isDataEdit.value = true
  currentDataId.value = row.id

  // 重置 dataForm
  dataForm.value = {}

  // V2 格式：数据已经是展平的，直接从 row 中取值
  // row 中的字段是 viewData/refreshDataList 已经展开的
  if (row.data) {
    // 如果 row 有 data 字段，说明原始数据是 V2 格式
    Object.keys(row.data).forEach(key => {
      if (!key.endsWith('_is_media')) {
        dataForm.value[key] = row.data[key]
      }
    })
    // 处理 is_media 字段
    Object.keys(row.data).forEach(key => {
      if (key.endsWith('_is_media')) {
        dataForm.value[key] = row.data[key]
      }
    })
  } else {
    // 兼容 V1 格式或其他格式
    // 直接从 row 中复制所有字段（除了 id、index 等元字段）
    Object.keys(row).forEach(key => {
      if (['id', 'index'].includes(key)) return
      if (key.endsWith('_is_media')) {
        dataForm.value[key] = row[key]
      } else {
        dataForm.value[key] = row[key]
      }
    })
  }

  // 获取数据集字段元数据（仅在成功返回非空时更新，保持预览弹窗字段不变）
  try {
    const fieldsRes = await datasetApi.getFields(currentDatasetId.value)
    if (fieldsRes && fieldsRes.length > 0) {
      dataFormFields.value = fieldsRes
    }
  } catch (e) {
    console.warn('getFields failed in editData, keeping existing fields:', e)
  }

  resetActiveField()
  dataFormVisible.value = true
}

const submitDataForm = async () => {
  if (dataFormLoading.value) return
  dataFormLoading.value = true
  try {
    const firstRequiredField = dataFormFields.value?.find(f => f.is_required && !f.name.endsWith('_is_media'))
    if (firstRequiredField) {
      const val = dataForm.value[firstRequiredField.name]
      if (!val || !String(val).trim()) {
        return ElMessage.warning(`请输入${firstRequiredField.label || firstRequiredField.name}`)
      }
    }

    // 构建请求数据 - V2 扁平格式
    const dataObj = {}
    // 服务端瘦身占位字段（超大 JSON）不提交：更新接口为合并语义，
    // 缺省字段保留库中原值；误提交占位对象会覆盖破坏原数据
    const skipOversize = (key) => isOversizePlaceholder(dataForm.value[key])
    if (dataFormFields.value && dataFormFields.value.length > 0) {
      dataFormFields.value.forEach(field => {
        const value = dataForm.value[field.name]
        if (skipOversize(field.name)) return
        if (value !== undefined) dataObj[field.name] = value
      })
    } else {
      Object.keys(dataForm.value).forEach(key => {
        if (skipOversize(key)) return
        dataObj[key] = dataForm.value[key]
      })
    }
    const requestData = { data: dataObj }

    let dataId
    if (isDataEdit.value) {
      await datasetApi.updateData(currentDatasetId.value, currentDataId.value, requestData)
      dataId = currentDataId.value
      ElMessage.success('更新成功')
    } else {
      const res = await datasetApi.addData(currentDatasetId.value, requestData)
      dataId = res.id
      ElMessage.success('添加成功')
    }

    // 如果有关联附件且有待上传的文件，进行 OSS 直传
    for (const field of (dataFormFields.value || [])) {
      if (!dataForm.value[field.name + '_is_media']) continue
      const pending = pendingFiles.value[field.name]
      if (!pending || pending.length === 0) continue
      const fileItem = pending[pending.length - 1]
      try {
        const ossUrl = await ossApi.upload(fileItem.raw)
        if (ossUrl) {
          await datasetApi.updateData(currentDatasetId.value, dataId, { data: { [field.name]: ossUrl } })
        }
      } catch (e) {
        console.error(`Upload ${field.name} failed:`, e)
      }
    }

    // 先关闭弹窗
    dataFormVisible.value = false

    // 刷新数据列表
    await refreshDataList()
    loadDatasets()
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
    // 即使出错也要关闭弹窗
    dataFormVisible.value = false
  } finally {
    dataFormLoading.value = false
  }
}

// 刷新数据列表的独立方法
const refreshDataList = async () => {
  try {
    // 使用 V2 接口获取数据（扁平格式）
    const listRes = await datasetApi.dataListV2(currentDatasetId.value, { limit: 100 })
    let items = listRes?.items || []

    // V2 格式数据直接在 items 中包含 data 字段
    items = items.map((item, idx) => {
      const flattened = { ...item }
      // 将 data 字段的内容展开到顶层
      if (flattened.data && typeof flattened.data === 'object') {
        Object.keys(flattened.data).forEach(key => {
          flattened[key] = flattened.data[key]
        })
      }
      // 动态计算序号（基于当前列表索引 + 1）
      flattened.index = idx + 1
      return flattened
    })

    dataList.value = items
  } catch (e) {
    console.error('刷新数据列表失败:', e)
  }
}

const deleteData = async (row) => {
  try {
    await ElMessageBox.confirm('确定要删除该数据吗？', '提示', { type: 'warning' })
    deletingDataId.value = row.id
    await datasetApi.deleteData(row.id)
    ElMessage.success('删除成功')
    await refreshDataList()
    loadDatasets()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  } finally {
    deletingDataId.value = null
  }
}

// 文件上传相关方法
// 处理数据项文件上传成功
const handleDataFileUpload = async (response, row) => {
  ElMessage.success('上传成功')
  // 刷新该数据项的文件列表
  try {
    const res = await datasetApi.getDataFiles(currentDatasetId.value, row.id)
    row.fileList = res || []
    loadDatasets()
  } catch (e) {
    console.error('刷新文件列表失败', e)
  }
}

// 处理文件上传错误
const handleUploadError = (error) => {
  ElMessage.error(error.message || '上传失败')
}

// 关闭添加/编辑数据对话框时清理
const handleDataFormClose = () => {
  pendingFiles.value = {}
  editedFiles.value = {}
  // 清理所有上传组件
  Object.values(dataFormUploadRefs.value).forEach(ref => {
    ref?.clearFiles?.()
  })
  dataFormUploadRefs.value = {}
}

// 处理添加数据表单中的文件选择变化
const handleDataFormFileChange = (file, files, field = 'answer') => {
  pendingFiles.value[field] = files
}

// 处理添加数据表单中的文件移除
const handleDataFormFileRemove = (file, files, field = 'answer') => {
  pendingFiles.value[field] = files
}

// 删除编辑数据时已上传的文件
const deleteEditedDataFile = async (file, field = 'answer') => {
  try {
    await ElMessageBox.confirm('确定要删除该文件吗？', '提示', { type: 'warning' })
    deletingFileId.value = file.id
    await datasetApi.deleteDataFile(currentDatasetId.value, currentDataId.value, file.id, field)
    ElMessage.success('删除成功')
    // 刷新文件列表
    const res = await datasetApi.getDataFiles(currentDatasetId.value, currentDataId.value)
    // 根据field分别更新文件列表
    editedFiles.value[field] = res ? res.filter(f => f.field === field) : []
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  } finally {
    deletingFileId.value = null
  }
}

// 删除数据项关联的文件
const deleteDataFile = async (row, file, field = 'answer') => {
  try {
    await ElMessageBox.confirm('确定要删除该文件吗？', '提示', { type: 'warning' })
    await datasetApi.deleteDataFile(currentDatasetId.value, row.id, file.id, field)
    ElMessage.success('删除成功')
    // 刷新该数据项的文件列表
    for (const f of (dataFormFields.value || [])) {
      if (f.is_media_field) {
        const files = await datasetApi.getDataFiles(currentDatasetId.value, row.id, f.name)
        row[f.name + '_fileList'] = files || []
      }
    }
    loadDatasets()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  }
}

const formatFileSize = (bytes) => {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

onMounted(() => {
  loadDatasets()
})
</script>

<style scoped>
.datasets-page {
  max-width: 1400px;
  margin: 0 auto;
}

/* ==================== 版本抽屉（M5 QE5-1） ==================== */
.version-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.version-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.version-hint {
  color: #909399;
  font-size: 12px;
}

.diff-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

/* diff 双栏：左=from 快照 / 右=to 快照，行内字段值多行展示 */
.diff-table {
  border: 1px solid #ebeef5;
  border-radius: 4px;
  max-height: 480px;
  overflow: auto;
}

.diff-row {
  display: flex;
  border-bottom: 1px solid #ebeef5;
}

.diff-row:last-child {
  border-bottom: none;
}

.diff-head {
  background: #f5f7fa;
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 1;
}

.diff-cell {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  border-left: 1px solid #ebeef5;
}

.diff-meta {
  flex: 0 0 72px;
  text-align: center;
}

.diff-index {
  color: #909399;
  font-size: 11px;
}

.diff-added {
  background: #f0f9eb;
}

.diff-removed {
  background: #fef0f0;
}

.diff-changed {
  background: #fdf6ec;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header span {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
}

/* anc-hub 导入页签 */
.hub-filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.hub-file-panel {
  width: 100%;
}

.hub-error-box {
  margin-bottom: 8px;
}

/* 执行包导入 job 进度面板（F1，W14 复活：WS 推送 + 轮询兜底共用此面板） */
.hub-job-panel {
  width: 100%;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  background: #FAFAFA;
}

.hub-job-status-line {
  display: flex;
  align-items: center;
  gap: 10px;
}

.hub-job-count {
  color: #606266;
  font-size: 13px;
}

.hub-job-ok { color: #67c23a; }
.hub-job-bad { color: #f56c6c; }

/* 失败明细列表（W14）：失败项可逐条定位，配合「重试失败项」按钮 */
.hub-job-fails {
  margin-top: 6px;
  max-height: 96px;
  overflow-y: auto;
}

.hub-job-fail-item {
  color: #909399;
  font-size: 12px;
  line-height: 1.8;
  word-break: break-all;
}

/* hub_file 字段单元格：图标 + 截断 file_id + 「文件」按钮（F2） */
.hub-file-cell {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  max-width: 100%;
}

.hub-file-icon {
  color: #e6a23c;
  flex-shrink: 0;
}

.hub-file-id {
  color: #606266;
  font-size: 12px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* hub_summary 徽标：用例统计 + execution_id 截断（F2） */
.hub-summary-badge {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  background: #ecf5ff;
  border: 1px solid #d9ecff;
  color: #409eff;
  border-radius: 4px;
  padding: 1px 8px;
  font-size: 12px;
  white-space: nowrap;
  cursor: default;
}

.hub-summary-exec {
  color: #909399;
  font-size: 11px;
}

/* 表格样式 */
:deep(.el-table) {
  --el-table-border-color: #E5E7EB;
}

:deep(.el-table th) {
  background: #F9FAFB;
  color: #374151;
  font-weight: 500;
}

:deep(.el-table td) {
  padding: 14px 0;
}

:deep(.el-dialog) {
  border-radius: 12px;
}

:deep(.el-dialog__header) {
  border-bottom: 1px solid #E5E7EB;
  padding-bottom: 16px;
}

:deep(.el-dialog__title) {
  font-weight: 600;
  color: #1F2937;
}

/* 错误行样式 */
:deep(.el-table .error-row) {
  --el-table-tr-bg-color: #FEF2F2;
}

/* 预览弹窗表头：字段描述单行省略（换行会把表头撑到 362px、数据区只剩 138px） */
.table-header-ellipsis {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 数据编辑弹窗：左字段列表 + 右单字段编辑区 */
.data-form-layout {
  display: flex;
  gap: 16px;
  height: 66vh;
  min-height: 320px;
}

.data-form-nav {
  flex: 0 0 240px;
  width: 240px;
  overflow-y: auto;
  border-right: 1px solid #E5E7EB;
  padding-right: 8px;
}

.data-form-nav-empty {
  padding: 8px;
  color: #909399;
  font-size: 13px;
}

.data-form-nav-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  margin-bottom: 2px;
  padding: 8px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #4B5563;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.data-form-nav-item:hover {
  background: #F3F4F6;
}

.data-form-nav-item.is-active {
  background: var(--primary-light);
  color: var(--primary-color);
  font-weight: 500;
}

/* 字段名可很长：单行省略，hover 用 title 看全名 */
.data-form-nav-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

.data-form-nav-flag {
  flex-shrink: 0;
  color: #f56c6c;
  font-size: 11px;
}

.data-form-nav-media {
  flex-shrink: 0;
  color: #e6a23c;
}

.data-form-pane {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

.data-form-pane-empty {
  color: #909399;
  font-size: 13px;
}

.data-form-pane-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.data-form-pane-name {
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 14px;
  font-weight: 600;
  color: #1F2937;
  word-break: break-all;
}

/* 字段描述全文换行展示（最长 195 字符），不再截断溢出 */
.data-form-pane-desc {
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  background: #F9FAFB;
  color: #6B7280;
  font-size: 12px;
  line-height: 1.6;
  word-break: break-word;
}
</style>
