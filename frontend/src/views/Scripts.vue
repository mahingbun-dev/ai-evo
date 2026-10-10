<template>
  <div class="scripts-page">
    <!-- 脚本库（原 Models 页「评测前处理脚本管理」卡片迁移为独立页面） -->
    <el-card class="script-lib-card">
      <template #header>
        <div class="card-header">
          <span>脚本库</span>
          <div>
            <el-button size="small" :loading="preScriptsLoading" @click="loadPreJudgeScripts">刷新</el-button>
            <el-button size="small" type="info" plain @click="openScriptTemplatesDialog">脚手架模板</el-button>
            <el-button v-if="isSuperAdmin" size="small" type="success" plain @click="openScriptImportDialog">导入</el-button>
            <el-button v-if="isSuperAdmin" size="small" type="primary" @click="openScriptCreateDialog">新建脚本</el-button>
          </div>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
            title="评测任务可编排的前处理脚本列表（所有脚本统一子进程执行）。查看、试跑、导出对所有用户开放；新建、导入、编辑、启停、删除为管理员操作；删除为物理删除（被评测任务引用时不可删除，需先在任务中解除关联；仅停用可使用「停用」按钮，停用后可再启用）。停用或删除后的脚本不再出现在任务编排的可选项中。"
      />
      <el-table :data="preJudgeScripts" v-loading="preScriptsLoading" style="width: 100%">
        <el-table-column label="名称" min-width="170">
          <template #default="{ row }">
            <div class="script-name-cell">
              <span class="script-name-text">{{ row.name }}</span>
              <!-- 防御式读取：has_requirements 字段可能 undefined，仅 true 时展示 -->
              <el-tag v-if="row.has_requirements" type="warning" size="small">有依赖声明</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" min-width="120">
          <template #default="{ row }">
            <el-tag :type="execTypeTagType(row.exec_type)">{{ execTypeLabel(row.exec_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'danger'">{{ row.is_active ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文件状态" min-width="100">
          <template #default="{ row }">
            <!-- script 类型依赖服务端落盘文件目录，file_count 为文件数（防御式读取：可能 undefined）；
                 agent 类型无文件概念，不展示 -->
            <template v-if="row.exec_type === 'script'">
              <el-tag v-if="Number(row.file_count) > 0" type="success" size="small">{{ row.file_count }} 个文件</el-tag>
              <el-tag v-else type="info" size="small">缺文件</el-tag>
            </template>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="240" show-overflow-tooltip />
        <el-table-column label="超时(秒)" min-width="80">
          <template #default="{ row }">
            {{ row.timeout_seconds ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="300" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="rowOpLoading" @click="openScriptDebugDialog(row)">试跑</el-button>
            <el-button link type="primary" :disabled="!isSuperAdmin || rowOpLoading" @click="openScriptDialog(row)">编辑</el-button>
            <!-- zip 脚本包上传（阶段E/F5）：仅 script 类型 + 管理员，包根须含 main.py，走阻断校验链 -->
            <el-button link type="primary" :disabled="row.exec_type !== 'script' || !isSuperAdmin || rowOpLoading" @click="openPackageUploadDialog(row)">上传包</el-button>
            <el-button link :type="row.is_active ? 'warning' : 'success'" :disabled="!isSuperAdmin || rowOpLoading" :loading="togglingActiveId === row.id" @click="togglePreScriptActive(row)">
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
            <el-button link type="primary" :disabled="rowOpLoading" :loading="preScriptExportingId === row.id" @click="exportPreScript(row)">导出</el-button>
            <el-button link type="danger" :disabled="!isSuperAdmin || rowOpLoading" :loading="deletingPreScriptId === row.id" @click="deletePreScript(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建脚本：空白 / 单文件模板 / 文件夹模板（脚手架） -->
    <el-dialog v-model="scriptCreateVisible" title="新建评测前处理脚本" width="620px" class="pre-script-dialog" @closed="resetCreateState">
      <el-form :model="scriptCreateForm" :rules="scriptCreateRules" ref="scriptCreateFormRef" label-width="90px">
        <el-form-item label="创建方式">
          <div class="create-mode-cards">
            <div
              v-for="mode in createModeOptions"
              :key="mode.value"
              class="create-mode-card"
              :class="{ active: scriptCreateForm.template === mode.value }"
              @click="scriptCreateForm.template = mode.value"
            >
              <div class="create-mode-title">{{ mode.label }}</div>
              <div class="create-mode-desc">{{ mode.desc }}</div>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="脚本名称" prop="name">
          <el-input v-model="scriptCreateForm.name" placeholder="全局唯一；模板将按此名建记录并生成初始文件" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="scriptCreateForm.description" type="textarea" :rows="2" placeholder="脚本用途说明（可选，展示在任务编排选择列表中）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="scriptCreating" @click="scriptCreateVisible = false">取消</el-button>
        <el-button type="primary" :loading="scriptCreating" @click="submitCreateScript">创建并编辑</el-button>
      </template>
    </el-dialog>

    <!-- 编辑评测前处理脚本：全屏分栏（上部基本信息 + 左文件树 + 右在线编辑器） -->
    <el-dialog
      v-model="scriptDialogVisible"
      :title="isScriptEdit ? `编辑评测前处理脚本 - ${scriptForm.name || ''}` : '新建评测前处理脚本'"
      fullscreen
      class="script-editor-dialog"
      @closed="onEditorDialogClosed"
    >
      <div class="editor-layout">
        <!-- 基本信息与文件通过 tab 切换（默认基本信息；文件页获得完整弹窗高度） -->
        <el-tabs v-model="editorActiveTab" class="editor-tabs">
          <el-tab-pane label="基本信息" name="basic">
            <el-alert
              v-if="hasModelPy"
              type="info"
              :closable="false"
              show-icon
              class="model-py-tip"
              title="检测到 model.py：输入字段与输出说明以其中 PreScriptInput / PreScriptOutput 声明为准，保存 model.py 时自动同步（覆盖手工填写值）"
            />
            <el-form :model="scriptForm" :rules="scriptRules" ref="scriptFormRef" label-width="110px" inline-message>
              <el-row :gutter="16">
                <el-col :span="12">
                  <el-form-item label="脚本名称" prop="name">
                    <el-input v-model="scriptForm.name" placeholder="全局唯一" />
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="执行类型" prop="exec_type">
                    <el-radio-group v-model="scriptForm.exec_type" :disabled="isScriptEdit">
                      <el-radio-button v-for="opt in execTypeOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</el-radio-button>
                    </el-radio-group>
                    <div class="cc-field-tip">{{ execTypeTip(scriptForm.exec_type) }}</div>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-form-item label="描述">
                <el-input v-model="scriptForm.description" type="textarea" :rows="2" placeholder="脚本用途说明（展示在任务编排选择列表中）" />
              </el-form-item>
              <!-- claude_agent / deepseek_agent：指令文本（script 类型的入口由服务端管理） -->
              <el-form-item
                v-if="scriptForm.exec_type === 'claude_agent' || scriptForm.exec_type === 'deepseek_agent'"
                label="指令文本"
                prop="entrypoint"
              >
                <el-input v-model="scriptForm.entrypoint" type="textarea" :rows="4" placeholder="给 Agent 的自然语言指令，要求其最后一行输出标准输出 JSON（status/output/files/summary）" />
                <div class="cc-field-tip">脚本内容即指令文本；输出需遵循标准输出契约，产物文件以绝对路径注册进 files</div>
              </el-form-item>
              <el-form-item v-if="scriptForm.exec_type === 'claude_agent'" label="工具白名单">
                <el-select
                  v-model="scriptForm.allowed_tools"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  placeholder="选择或输入允许的工具（留空使用默认）"
                  style="width: 100%"
                >
                  <el-option v-for="tool in claudeToolOptions" :key="tool" :label="tool" :value="tool" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="scriptForm.exec_type === 'deepseek_agent'" label="Harness 配置">
                <div style="width: 100%">
                  <el-input
                    v-model="scriptHarnessExtraText"
                    type="textarea"
                    :rows="3"
                    placeholder='JSON 对象，如 {"provider": "deepseek-official", "model": "deepseek-v4-flash", "profile": "sdk-minimal", "max_tokens": 49152}'
                    style="font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace"
                  />
                  <div class="cc-field-tip">
                    DSH 专用扩展配置（JSON 对象，可空）；
                    <span v-if="scriptHarnessExtraError" style="color: #f56c6c">{{ scriptHarnessExtraError }}</span>
                  </div>
                </div>
              </el-form-item>
              <el-form-item label="输入字段">
                <div style="width: 100%">
                  <div v-for="(field, idx) in scriptInputFieldRows" :key="idx" class="script-input-field-row">
                    <el-input v-model="field.name" placeholder="字段名（如 cases_file）" style="width: 200px" />
                    <el-select v-model="field.value_type" style="width: 130px">
                      <el-option v-for="vt in valueTypeOptions" :key="vt.value" :label="vt.label" :value="vt.value" />
                    </el-select>
                    <el-input v-model="field.description" placeholder="字段说明" style="flex: 1" />
                    <el-button link type="danger" @click="scriptInputFieldRows.splice(idx, 1)">
                      <el-icon><Delete /></el-icon>
                    </el-button>
                  </div>
                  <el-button type="primary" plain size="small" @click="scriptInputFieldRows.push({ name: '', value_type: 'inline', description: '' })">
                    + 添加输入字段
                  </el-button>
                  <div class="cc-field-tip">声明脚本所需输入（名称/取值类型/说明），驱动任务编排时的数据集字段映射与试跑表单</div>
                </div>
              </el-form-item>
              <el-form-item label="输出说明">
                <div style="width: 100%">
                  <div
                    v-for="(row, idx) in flatOutputSchemaRows"
                    :key="idx"
                    class="script-input-field-row"
                    :style="row.depth ? { marginLeft: row.depth * 28 + 'px' } : null"
                  >
                    <span class="output-tree-caret">
                      <el-button
                        v-if="row.node.children && row.node.children.length"
                        link
                        @click="row.node.collapsed = !row.node.collapsed"
                      >
                        <el-icon><CaretBottom v-if="!row.node.collapsed" /><CaretRight v-else /></el-icon>
                      </el-button>
                    </span>
                    <el-input v-model="row.node.name" placeholder="输出键名（如 consistency_rate）" style="width: 200px" />
                    <el-select v-model="row.node.type" clearable placeholder="类型" style="width: 180px" @change="onOutputTypeChange(row.node)">
                      <el-option v-for="t in outputTypeOptions" :key="t.value" :label="t.label" :value="t.value" />
                    </el-select>
                    <el-input v-model="row.node.description" placeholder="键说明（如 一致率 0~1）" style="flex: 1" />
                    <el-button
                      v-if="row.node.type === 'object'"
                      link
                      type="primary"
                      title="添加子键"
                      @click="addChildOutputRow(row.node)"
                    >+子键</el-button>
                    <el-button link type="danger" title="删除（含子键）" @click="removeOutputRow(row.node)">
                      <el-icon><Delete /></el-icon>
                    </el-button>
                  </div>
                  <el-button type="primary" plain size="small" @click="scriptOutputSchemaRows.push({ name: '', type: '', description: '', collapsed: false, children: [] })">
                    + 添加输出键
                  </el-button>
                  <div class="cc-field-tip">描述标准输出 output 的键（供裁判变量配置选择）；类型选 object 可添加子键描述嵌套结构，主键类型切换后子键自动删除</div>
                </div>
              </el-form-item>
              <el-row :gutter="16">
                <el-col :span="6">
                  <el-form-item label="超时(秒)">
                    <el-input-number v-model="scriptForm.timeout_seconds" :min="1" :max="3600" controls-position="right" style="width: 140px" />
                  </el-form-item>
                </el-col>
                <el-col :span="6">
                  <el-form-item label="启用">
                    <el-switch v-model="scriptForm.is_active" />
                    <span class="cc-field-tip" style="margin-left: 8px">停用后不出现在任务编排可选项中</span>
                  </el-form-item>
                </el-col>
              </el-row>
            </el-form>
          </el-tab-pane>

          <!-- 文件管理 + 在线编辑（script 类型；tab 化后获得完整弹窗高度） -->
          <el-tab-pane v-if="scriptForm.exec_type === 'script'" label="文件" name="files" class="editor-files-pane">
            <div class="editor-body">
          <div class="file-tree-panel">
            <div class="file-tree-header">
              <span>文件</span>
              <el-button size="small" text type="primary" :disabled="fileTreeOpLoading" :loading="fileTreeOpLoading" @click="createNewScriptFile">新建文件</el-button>
            </div>
            <el-tree
              v-if="scriptFileTree.length"
              :data="scriptFileTree"
              node-key="path"
              default-expand-all
              :expand-on-click-node="false"
              highlight-current
              :current-node-key="activeFilePath"
              @node-click="onFileTreeNodeClick"
            >
              <template #default="{ data }">
                <span class="file-tree-node" :class="{ 'is-entry': data.path === 'main.py' }">
                  <span class="file-tree-label">
                    {{ data.label }}
                    <span v-if="data.path === activeFilePath && editorDirty" class="dirty-dot">●</span>
                  </span>
                  <span v-if="data.path" class="file-tree-ops">
                    <el-button
                      v-if="data.path !== 'main.py'"
                      class="file-tree-op-btn"
                      link
                      title="重命名/移动"
                      :disabled="fileTreeOpLoading"
                      :icon="EditPen"
                      @click.stop="renameScriptFileNode(data.path)"
                    />
                    <el-button
                      v-if="data.path !== 'main.py'"
                      class="file-tree-op-btn"
                      link
                      title="删除"
                      :disabled="fileTreeOpLoading"
                      :icon="Delete"
                      @click.stop="deleteScriptFileNode(data.path)"
                    />
                    <el-icon v-if="data.path === 'main.py'" title="入口文件（不可改名/删除）" class="entry-lock"><Lock /></el-icon>
                  </span>
                </span>
              </template>
            </el-tree>
            <div v-else class="file-tree-empty">
              暂无文件。点击「新建文件」创建 main.py（脚本包必须包含根级 main.py 入口），或从「导入」上传脚本包。
            </div>
          </div>
          <div class="editor-panel">
            <div class="editor-toolbar">
              <span class="editor-active-path">
                {{ activeFilePath || '未选择文件' }}
                <span v-if="editorDirty" class="dirty-dot" title="有未保存修改">●</span>
              </span>
              <span class="editor-toolbar-actions">
                <el-button size="small" type="primary" :disabled="!activeFilePath || editorOpLoading" :loading="fileSaving" @click="saveActiveFile">
                  保存文件 (⌘S)
                </el-button>
                <el-button size="small" :disabled="editorOpLoading" @click="tryDebugFromEditor">试跑</el-button>
                <el-button size="small" :disabled="editorOpLoading" :loading="packageExporting" @click="exportScriptPackageFromEditor">导出 zip</el-button>
              </span>
            </div>
            <div ref="codemirrorHost" class="codemirror-host"></div>
          </div>
        </div>
          </el-tab-pane>
        </el-tabs>
      </div>
      <template #footer>
        <el-button :disabled="scriptSaving" @click="scriptDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="scriptSaving" @click="saveScriptMeta">保存基本信息</el-button>
      </template>
    </el-dialog>

    <!-- 评测前处理脚本试跑（debug）对话框 -->
    <el-dialog v-model="scriptDebugVisible" :title="`脚本试跑 - ${debugScript?.name || ''}`" width="860px" class="pre-script-dialog" @closed="resetDebugState">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        title="按脚本声明的输入字段填入样例值（可填服务器本地路径——存在则直接使用，否则按 inline 内容写临时文件），单条试跑验证脚本配置。"
      />
      <div v-if="debugInputRows.length" class="debug-block">
        <div class="debug-block-title">输入样例</div>
        <el-form label-width="140px">
          <el-form-item v-for="row in debugInputRows" :key="row.name" :label="row.name">
            <div style="width: 100%">
              <el-input v-model="debugFields[row.name]" type="textarea" :rows="2" :placeholder="`输入 ${row.name} 的样例值（路径或内容）`" />
              <div class="cc-field-tip">
                <el-tag size="small" :type="row.value_type === 'hub_file' ? 'warning' : 'info'">{{ row.value_type }}</el-tag>
                <span style="margin-left: 6px">{{ row.description || '可填本地路径或 inline 内容' }}</span>
              </div>
            </div>
          </el-form-item>
        </el-form>
      </div>
      <div v-else class="debug-block">
        <div class="debug-block-title">输入样例</div>
        <div class="debug-empty">该脚本未声明输入字段（input_fields 为空）</div>
      </div>

      <div class="debug-block">
        <div class="debug-block-title">
          脚本参数（可选）
          <el-button type="primary" link size="small" @click="debugParamRows.push({ key: '', value: '' })">添加参数</el-button>
        </div>
        <div v-for="(param, idx) in debugParamRows" :key="idx" class="script-input-field-row">
          <el-input v-model="param.key" placeholder="参数名" style="width: 200px" />
          <el-input v-model="param.value" placeholder="参数值（数字 / true / false 自动识别类型）" style="flex: 1" />
          <el-button link type="danger" @click="debugParamRows.splice(idx, 1)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
        <div v-if="!debugParamRows.length" class="debug-empty">暂无参数</div>
      </div>

      <!-- 试跑结果 -->
      <div v-if="debugResult" class="debug-block">
        <div class="debug-block-title">试跑结果</div>
        <el-alert
          v-if="debugResult.status === 'fail'"
          type="error"
          :title="`脚本执行失败${debugResult.error ? '：' + debugResult.error : ''}`"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        />
        <el-descriptions :column="3" border size="small" style="margin-bottom: 12px">
          <el-descriptions-item label="状态">
            <el-tag :type="debugStatusTagType(debugResult.status)">{{ debugStatusLabel(debugResult.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="结束原因">{{ debugResult.finish_reason || '-' }}</el-descriptions-item>
          <el-descriptions-item label="耗时">{{ formatDebugDuration(debugResult.duration_ms) }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="debugResult.summary" class="debug-summary">{{ debugResult.summary }}</div>
        <template v-if="debugResult.output && Object.keys(debugResult.output).length">
          <div class="debug-block-title">标准输出（output）</div>
          <pre class="code-block">{{ JSON.stringify(debugResult.output, null, 2) }}</pre>
        </template>
        <template v-if="debugFileRows.length">
          <div class="debug-block-title" style="margin-top: 12px">产物文件（{{ debugFileRows.length }}）</div>
          <el-table :data="debugFileRows" size="small" style="width: 100%">
            <el-table-column prop="name" label="文件名" min-width="180" show-overflow-tooltip />
            <el-table-column prop="path" label="服务器路径" min-width="320" show-overflow-tooltip />
          </el-table>
          <div class="cc-field-tip" style="margin-top: 6px">产物保存在服务器临时目录（{{ debugResult.run_dir || 'debug 运行目录' }}），仅供试跑检查</div>
        </template>
      </div>
      <div v-else-if="!debugLoading" class="debug-block">
        <div class="debug-empty">尚未试跑，填写样例后点击「开始试跑」</div>
      </div>

      <template #footer>
        <el-button :disabled="debugLoading" @click="scriptDebugVisible = false">关闭</el-button>
        <el-button type="primary" :loading="debugLoading" @click="runScriptDebug">开始试跑</el-button>
      </template>
    </el-dialog>

    <!-- 导入脚本对话框：.zip 脚本包 / 文件夹直选 + 包内文件树预览（main.py/model.py 锁定必选） -->
    <el-dialog v-model="scriptImportVisible" title="导入评测前处理脚本" width="720px" class="pre-script-dialog" @closed="resetImportState">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        title="仅支持 .zip 脚本包或文件夹导入，包内必须包含 main.py 与 model.py（输入/输出契约声明）；__pycache__、.DS_Store 等噪音文件会被自动忽略。"
      />
      <div class="import-source-bar">
        <el-button type="primary" plain @click="triggerZipPick">
          <el-icon><Upload /></el-icon>
          选择 .zip 脚本包
        </el-button>
        <el-button type="success" plain @click="triggerFolderPick">
          <el-icon><FolderOpened /></el-icon>
          选择文件夹
        </el-button>
        <span class="cc-field-tip">两种来源互斥，重新选择将替换当前预览；一次导入一个脚本包</span>
        <input
          ref="zipInputRef"
          type="file"
          accept=".zip"
          style="display: none"
          @change="handleZipPick"
        />
        <input
          ref="folderInputRef"
          type="file"
          webkitdirectory
          multiple
          style="display: none"
          @change="handleFolderPick"
        />
      </div>
      <!-- 已选来源 + 包内文件树预览（默认全选；main.py/model.py 禁用锁定） -->
      <div v-if="importPreview" class="debug-block" style="margin-top: 12px">
        <div class="debug-block-title">
          {{ importPreview.sourceTitle }}：{{ importPreview.sourceLabel }}（{{ importPreview.fileCount }} 个文件{{ importPreview.filteredCount ? `，已忽略 ${importPreview.filteredCount} 个噪音文件` : '' }}，共 {{ formatImportSize(importPreview.totalSize) }}）
          <el-button link type="danger" size="small" @click="clearImportPreview">移除</el-button>
        </div>
        <el-tree
          ref="importTreeRef"
          :key="importPreview.version"
          :data="importPreview.treeData"
          node-key="key"
          default-expand-all
          show-checkbox
          :default-checked-keys="importPreview.checkedKeys"
          :props="{ children: 'children', label: 'label', disabled: 'disabled' }"
          class="import-file-tree"
        >
          <template #default="{ data }">
            <span class="import-tree-node">
              {{ data.label }}
              <el-icon v-if="data.locked" title="导入必需文件（不可取消勾选）" class="entry-lock"><Lock /></el-icon>
            </span>
          </template>
        </el-tree>
        <div class="cc-field-tip" style="margin-top: 6px">main.py / model.py 为导入必需文件，始终包含；其余文件可按需取消勾选</div>
      </div>
      <!-- 预检未通过（缺失 main.py/model.py、超限等）：报错且不出树 -->
      <el-alert v-if="importPrecheckErrors.length" type="error" :closable="false" show-icon style="margin-top: 12px">
        <template #title>导入预检未通过，请调整后重新选择</template>
        <div class="precheck-error-list">
          <div v-for="msg in importPrecheckErrors" :key="msg">{{ msg }}</div>
        </div>
      </el-alert>
      <el-checkbox v-model="importOverwrite" style="margin-top: 12px">
        同名脚本已存在时覆盖更新（不勾选则同名报错；同名停用脚本总是复活启用）
      </el-checkbox>
      <div v-if="importResultText" class="debug-block" style="margin-top: 12px">
        <div class="debug-block-title">导入结果</div>
        <pre class="code-block">{{ importResultText }}</pre>
      </div>
      <template #footer>
        <el-button :disabled="importLoading" @click="scriptImportVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="importLoading"
          :disabled="!importPreview || importPrecheckErrors.length > 0"
          @click="submitScriptImport"
        >开始导入</el-button>
      </template>
    </el-dialog>

    <!-- zip 脚本包上传对话框（阶段E/F5）：.zip 选择 + 可选风险声明 / 依赖声明 -->
    <el-dialog v-model="packageUploadVisible" :title="`上传 zip 脚本包 - ${packageUploadTarget?.name || ''}`" width="560px" class="pre-script-dialog" @closed="resetPackageUploadState">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        title="zip 包根目录必须含 main.py（zip 只含唯一一级子目录且其内有 main.py 时该子目录视为包根），大小 ≤20MB。上传走安全校验链（语法/风险扫描/依赖校验/契约冒烟），不通过将中文报错。重复上传为覆盖语义。"
      />
      <el-form label-width="90px">
        <el-form-item label="脚本包">
          <el-upload
            ref="packageUploadRef"
            :auto-upload="false"
            accept=".zip"
            :limit="1"
            :on-change="handlePackageFileChange"
            :on-remove="() => (packageFile = null)"
            :on-exceed="handlePackageFileExceed"
          >
            <el-button type="primary" plain>
              <el-icon><Upload /></el-icon>
              选择 .zip 脚本包
            </el-button>
            <template #tip>
              <div class="el-upload__tip">
                上传成功后 entrypoint 回写为目录内 main.py，包内模块可直接相互导入
              </div>
            </template>
          </el-upload>
        </el-form-item>
        <el-form-item label="风险声明">
          <el-input
            v-model="packageRiskAcknowledged"
            type="textarea" :rows="2"
            placeholder="可选；服务端风险扫描命中（如 os.system/subprocess 外部命令等）时必填，说明该风险的业务必要性"
          />
        </el-form-item>
        <el-form-item label="依赖声明">
          <el-input
            v-model="packageDeclaredRequirements"
            type="textarea" :rows="3"
            placeholder="可选；每行一个依赖（如 requests>=2.31），包内 import 的第三方库须在此声明（标准库与平台预装库无需声明）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="packageUploading" @click="packageUploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="packageUploading" :disabled="!packageFile" @click="submitPackageUpload">上传脚本包</el-button>
      </template>
    </el-dialog>

    <!-- 脚手架模板列表对话框（阶段E/F5）：按契约开发的模板 zip 下载入口 -->
    <el-dialog v-model="scriptTemplatesVisible" title="脚手架模板" width="640px" class="pre-script-dialog">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        title="模板 zip 按标准输入/输出契约开发（stdin 契约输入，最后一行输出标准输出 JSON），解压后按 README 修改即可作为自定义脚本上传。"
      />
      <div v-loading="scriptTemplatesLoading">
        <el-table :data="scriptTemplates" size="small">
          <el-table-column prop="title" label="模板" min-width="150" />
          <el-table-column prop="description" label="说明" min-width="220" show-overflow-tooltip />
          <el-table-column label="大小" width="90">
            <template #default="{ row }">{{ formatPackageSize(row.size) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :loading="templateDownloadingName === row.name" @click="downloadScriptTemplate(row)">下载</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!scriptTemplatesLoading && !scriptTemplates.length" class="debug-empty">暂无可用模板</div>
      </div>
      <template #footer>
        <el-button @click="scriptTemplatesVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, inject, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Upload, EditPen, Lock, FolderOpened, CaretRight, CaretBottom } from '@element-plus/icons-vue'
import JSZip from 'jszip'
import { preJudgeScriptApi } from '@/api'
import { basicSetup, EditorView } from 'codemirror'
import { EditorState } from '@codemirror/state'
import { python } from '@codemirror/lang-python'
import { markdown } from '@codemirror/lang-markdown'
import { json as jsonLang } from '@codemirror/lang-json'
import { yaml } from '@codemirror/lang-yaml'
import { oneDark } from '@codemirror/theme-one-dark'

// ==================== 评测前处理脚本管理 ====================

const preJudgeScripts = ref([])
const preScriptsLoading = ref(false)

// 管理员门控（设计决策 #4「管理员可写、普通用户可用」）：脚本写操作（新建/导入/
// 编辑/启停/删除）仅管理员可用；查看、试跑、导出对所有用户开放。
// 数据源与 App.vue 顶部菜单 /teams、/audit-logs 的门控一致（super_admin_flag）；
// 注入缺省按非管理员处理（deny by default）
const isSuperAdmin = inject('isSuperAdmin', computed(() => false))

// 执行类型中文标签与标签色（builtin 已下线：原「进程内」与「自定义脚本」合并为 script「脚本」，
// 统一子进程执行——stdin 标准输入 JSON / stdout 末行标准输出 JSON）
const PRE_SCRIPT_EXEC_TYPE_LABELS = {
  script: '脚本',
  claude_agent: 'Claude Agent',
  deepseek_agent: 'DeepSeek Agent'
}

const execTypeLabel = (execType) => PRE_SCRIPT_EXEC_TYPE_LABELS[execType] || execType || '-'

const execTypeTagType = (execType) => (
  { script: 'warning', claude_agent: 'primary', deepseek_agent: 'success' }[execType] || 'info'
)

// 加载评测前处理脚本列表（含已停用脚本，停用项可在此重新启用）
const loadPreJudgeScripts = async () => {
  preScriptsLoading.value = true
  try {
    const res = await preJudgeScriptApi.list({ include_inactive: true })
    preJudgeScripts.value = res?.items || []
  } catch (e) {
    ElMessage.error(e.message || '加载评测前处理脚本列表失败')
  } finally {
    preScriptsLoading.value = false
  }
}

// ---------- 新建脚本（脚手架：空白 / 单文件模板 / 文件夹模板） ----------

const createModeOptions = [
  { value: 'blank', label: '空白脚本', desc: '仅建记录，文件树为空（可在线新建 main.py）' },
  { value: 'single', label: '单文件模板', desc: '生成 main.py（run(stdin) 契约样例）+ README' },
  { value: 'folder', label: '文件夹模板', desc: 'main.py + utils.py + README + requirements.txt 工程骨架' }
]

const scriptCreateVisible = ref(false)
const scriptCreating = ref(false)
const scriptCreateFormRef = ref(null)
const scriptCreateForm = ref({ template: 'single', name: '', description: '' })

const scriptCreateRules = {
  name: [{ required: true, message: '请输入脚本名称', trigger: 'blur' }]
}

const openScriptCreateDialog = () => {
  scriptCreateForm.value = { template: 'single', name: '', description: '' }
  scriptCreateVisible.value = true
  nextTick(() => scriptCreateFormRef.value?.clearValidate())
}

const resetCreateState = () => {
  scriptCreateForm.value = { template: 'single', name: '', description: '' }
}

// 创建后直接进入编辑窗口（模板类已有文件可在线编辑）
const submitCreateScript = async () => {
  try {
    await scriptCreateFormRef.value.validate()
  } catch {
    return
  }
  const { template, name, description } = scriptCreateForm.value
  scriptCreating.value = true
  try {
    let created
    if (template === 'blank') {
      created = await preJudgeScriptApi.create({
        name: name.trim(), description: description || null,
        exec_type: 'script', entrypoint: null
      })
    } else {
      created = await preJudgeScriptApi.createFromTemplate({
        template, name: name.trim(), description: description || null
      })
    }
    ElMessage.success('脚本已创建，可在线编辑文件或直接试跑')
    scriptCreateVisible.value = false
    await loadPreJudgeScripts()
    const row = preJudgeScripts.value.find(s => s.id === created.id) || created
    openScriptDialog(row)
  } catch (e) {
    ElMessage.error(e.message || '创建脚本失败')
  } finally {
    scriptCreating.value = false
  }
}

// ---------- 编辑窗口（全屏分栏：基本信息 + 文件树 + 在线编辑器） ----------

const execTypeOptions = [
  { value: 'script', label: '脚本' },
  { value: 'claude_agent', label: 'Claude Agent' },
  { value: 'deepseek_agent', label: 'DeepSeek Agent' }
]

const execTypeTip = (execType) => ({
  script: '脚本包（main.py 入口，支持文件夹多文件），服务端子进程执行：stdin 传标准输入 JSON，stdout 末行输出标准输出 JSON；优先实现 run(stdin) 函数',
  claude_agent: '脚本内容为自然语言指令，由 Claude Code Agent 按标准输出契约执行',
  deepseek_agent: '脚本内容为自然语言指令，由 DeepSeek Harness Agent 按标准输出契约执行'
}[execType] || '')

// 输入字段取值类型（对齐设计文档第五节字段落地通道）
const valueTypeOptions = [
  { value: 'inline', label: 'inline（内容即值）' },
  { value: 'local', label: 'local（本地上传）' },
  { value: 'oss', label: 'oss（OSS 附件）' },
  { value: 'hub_file', label: 'hub_file（anc-hub 文件）' }
]

// Claude Agent 工具白名单常用项（可自由输入）
const claudeToolOptions = ['Read', 'Write', 'Edit', 'Bash', 'Grep', 'Glob', 'WebFetch', 'TodoWrite']

const scriptDialogVisible = ref(false)
const isScriptEdit = ref(false)
const editingScript = ref(null)
const scriptFormRef = ref(null)
const scriptSaving = ref(false)
// 编辑弹窗 Tab：basic=基本信息（默认）/ files=文件树+编辑器（获得完整弹窗高度）
const editorActiveTab = ref('basic')

// 文件树 / 编辑器状态（编辑器挂载在 CodeMirror 区段，此处仅声明桥接状态）
const scriptFiles = ref([])            // [{path, size, mtime, is_text}]
// model.py 声明联动：目录内存在 model.py 时，输入/输出声明以其中
// PreScriptInput/PreScriptOutput 为准（保存 model.py 时服务端解析回写）
const hasModelPy = computed(() => scriptFiles.value.some(f => f.path === 'model.py'))
const activeFilePath = ref('')
const activeFileContent = ref('')      // 当前文件的已加载内容（保存基线）
const currentEditorText = ref('')      // 编辑器当前文本（updateListener 同步）
const editorDirty = ref(false)
const fileSaving = ref(false)
const packageExporting = ref(false)
// 文件树写操作（新建/重命名/删除）互斥 loading：同一棵树三个入口共用，操作进行中禁用彼此
const fileTreeOpLoading = ref(false)
// 编辑器工具栏写操作（保存文件/试跑/导出 zip）互斥：任一进行中禁用其余入口
// （debugLoading 在下方「脚本试跑」区段声明，computed 惰性求值无时序问题）
const editorOpLoading = computed(() => fileSaving.value || packageExporting.value || debugLoading.value)
const baselineUpdatedAt = ref(null)    // 乐观锁基准（脚本记录 updated_at）

const getDefaultScriptForm = () => ({
  name: '',
  description: '',
  exec_type: 'script',
  entrypoint: '',
  allowed_tools: [],
  timeout_seconds: 120,
  is_active: true
})

const scriptForm = ref(getDefaultScriptForm())
const scriptInputFieldRows = ref([])  // [{name, value_type, description}]
const scriptOutputSchemaRows = ref([])  // 树节点 [{name, type, description, collapsed, children}]（output_schema 键 → 类型/说明，支持嵌套）
const scriptHarnessExtraText = ref('')
const scriptHarnessExtraError = ref('')

const scriptRules = {
  name: [{ required: true, message: '请输入脚本名称', trigger: 'blur' }],
  exec_type: [{ required: true, message: '请选择执行类型', trigger: 'change' }],
  entrypoint: [{
    validator: (rule, value, callback) => {
      if (['claude_agent', 'deepseek_agent'].includes(scriptForm.value.exec_type) && !String(value || '').trim()) {
        callback(new Error('请输入指令文本'))
      } else {
        callback()
      }
    },
    trigger: 'blur'
  }]
}

// 由输入字段编辑行构建 input_fields（过滤空行；无字段返回 null）
const buildInputFieldsPayload = () => {
  const result = {}
  for (const row of scriptInputFieldRows.value) {
    const name = (row.name || '').trim()
    if (!name) continue
    result[name] = { value_type: row.value_type || 'inline', description: row.description || '' }
  }
  return Object.keys(result).length ? result : null
}

// 由输出键编辑树构建 output_schema（叶子 "类型：说明" 串 / 嵌套 {type, description, children}，与 model.py 解析产物同构；过滤空名；无键返回 null）
const buildOutputSchemaValue = (node) => {
  const desc = node.description || ''
  const children = {}
  for (const child of (node.children || [])) {
    const name = (child.name || '').trim()
    if (name) children[name] = buildOutputSchemaValue(child)
  }
  if (Object.keys(children).length) {
    // 有子键时按嵌套对象落库；类型误选叶子类型时兜底 object
    return { type: ['object', 'array'].includes(node.type) ? node.type : 'object', description: desc, children }
  }
  return node.type ? `${node.type}：${desc}` : desc
}
const buildOutputSchemaPayload = () => {
  const result = {}
  for (const row of scriptOutputSchemaRows.value) {
    const name = (row.name || '').trim()
    if (!name) continue
    result[name] = buildOutputSchemaValue(row)
  }
  return Object.keys(result).length ? result : null
}

// 输出键字段类型（仅可选不可输入，防手写出错）；存储契约仍为 {键: "类型：说明"} 串，前后端消费方不变
const outputTypeOptions = [
  { value: 'string', label: 'string（字符串）' },
  { value: 'integer', label: 'integer（整数）' },
  { value: 'number', label: 'number（数值）' },
  { value: 'boolean', label: 'boolean（布尔）' },
  { value: 'array', label: 'array（数组）' },
  { value: 'object', label: 'object（对象）' }
]
// 存量串/模型注解里的类型别名 → 标准类型（含 Python 注解简写）
const OUTPUT_TYPE_ALIASES = {
  string: 'string', str: 'string', text: 'string',
  integer: 'integer', int: 'integer',
  number: 'number', float: 'number', double: 'number',
  boolean: 'boolean', bool: 'boolean',
  array: 'array', list: 'array',
  object: 'object', dict: 'object', json: 'object'
}

// 解析存量 output_schema 值为行数据：前缀是已知类型才拆分，否则整体进说明，保证无损回写
const parseOutputSchemaValue = (value) => {
  const raw = typeof value === 'string' ? value : JSON.stringify(value ?? '')
  const m = raw.match(/^(.*?)\s*[：:]\s*([\s\S]*)$/)
  const prefix = m ? m[1].trim().toLowerCase() : ''
  if (m && OUTPUT_TYPE_ALIASES[prefix]) {
    return { type: OUTPUT_TYPE_ALIASES[prefix], description: m[2] }
  }
  return { type: '', description: raw }
}

// 解析 output_schema 值为树节点：嵌套对象（含 children）递归成子树；
// 其余（叶子串 / 未知形态对象）走叶子解析，老数据兼容展示
const parseOutputSchemaNode = (value) => {
  if (value && typeof value === 'object' && value.children && typeof value.children === 'object') {
    return {
      type: ['object', 'array'].includes(value.type) ? value.type : 'object',
      description: value.description || '',
      collapsed: false,
      children: Object.entries(value.children).map(([name, child]) => ({
        name, ...parseOutputSchemaNode(child)
      }))
    }
  }
  const leaf = parseOutputSchemaValue(value)
  return { type: leaf.type, description: leaf.description, collapsed: false, children: [] }
}

// 树 → 展平行列表（收起的节点隐藏子层），供模板缩进渲染
const flatOutputSchemaRows = computed(() => {
  const rows = []
  const walk = (nodes, depth) => {
    for (const node of nodes) {
      rows.push({ node, depth })
      if (node.children && node.children.length && !node.collapsed) walk(node.children, depth + 1)
    }
  }
  walk(scriptOutputSchemaRows.value, 0)
  return rows
})

// 给 object 节点追加子键（自动展开）
const addChildOutputRow = (node) => {
  node.collapsed = false
  node.children = node.children || []
  node.children.push({ name: '', type: '', description: '', collapsed: false, children: [] })
}

// 主键类型切换即清空子键（子键只挂在 object 上；@change 仅用户操作触发，model.py 同步回填不受影响）
const onOutputTypeChange = (node) => {
  if (node.children && node.children.length) {
    node.children = []
    node.collapsed = false
  }
}

// 按节点身份从树中摘除（含子树）
const removeOutputRow = (node) => {
  const removeFrom = (nodes) => {
    const i = nodes.indexOf(node)
    if (i >= 0) {
      nodes.splice(i, 1)
      return true
    }
    return nodes.some(n => n.children && n.children.length && removeFrom(n.children))
  }
  removeFrom(scriptOutputSchemaRows.value)
}

// 文件树数据：files[].path 构树（目录节点合并，叶节点带 path）
const scriptFileTree = computed(() => {
  const root = { label: '/', path: '', children: {} }
  const sorted = [...scriptFiles.value].sort((a, b) => a.path.localeCompare(b.path))
  for (const file of sorted) {
    const parts = file.path.split('/').filter(Boolean)
    let node = root
    parts.forEach((part, idx) => {
      const isLeaf = idx === parts.length - 1
      const path = parts.slice(0, idx + 1).join('/')
      if (!node.children[part]) {
        node.children[part] = { label: part, path: isLeaf ? path : '', children: {}, isDir: !isLeaf }
      }
      node = node.children[part]
    })
  }
  const toTreeData = (node) => Object.values(node.children)
    .sort((a, b) => (b.isDir - a.isDir) || a.label.localeCompare(b.label))
    .map(child => ({
      label: child.label,
      path: child.isDir ? '' : child.path,
      isDir: child.isDir,
      children: child.isDir ? toTreeData(child) : undefined
    }))
  return toTreeData(root)
})

// 切回「文件」tab 时矫正 CodeMirror 测量（display:none 期间高度为 0）
watch(editorActiveTab, (tab) => {
  if (tab === 'files') nextTick(() => editorView?.requestMeasure?.())
})

// 打开编辑窗口（row 为空即新建——现由脚手架流程覆盖，保留兼容入口）
const openScriptDialog = (row = null) => {
  editingScript.value = row
  isScriptEdit.value = !!row
  scriptHarnessExtraError.value = ''
  editorActiveTab.value = 'basic'
  if (row) {
    scriptForm.value = {
      name: row.name || '',
      description: row.description || '',
      exec_type: row.exec_type || 'script',
      entrypoint: row.entrypoint || '',
      allowed_tools: Array.isArray(row.allowed_tools) ? [...row.allowed_tools] : [],
      timeout_seconds: row.timeout_seconds || 120,
      is_active: !!row.is_active
    }
    scriptInputFieldRows.value = Object.entries(row.input_fields || {}).map(([name, meta]) => ({
      name,
      value_type: (meta && meta.value_type) || 'inline',
      description: (meta && meta.description) || ''
    }))
    scriptOutputSchemaRows.value = Object.entries(row.output_schema || {}).map(([name, value]) => ({
      name,
      ...parseOutputSchemaNode(value)
    }))
    scriptHarnessExtraText.value = row.harness_extra ? JSON.stringify(row.harness_extra, null, 2) : ''
    baselineUpdatedAt.value = row.updated_at || null
  } else {
    scriptForm.value = getDefaultScriptForm()
    scriptInputFieldRows.value = []
    scriptOutputSchemaRows.value = []
    scriptHarnessExtraText.value = ''
    baselineUpdatedAt.value = null
  }
  // 重置编辑器桥接状态（编辑器视图由 CodeMirror 区段按 visible 重建）
  scriptFiles.value = []
  activeFilePath.value = ''
  activeFileContent.value = ''
  currentEditorText.value = ''
  editorDirty.value = false
  scriptDialogVisible.value = true
  nextTick(() => {
    scriptFormRef.value?.clearValidate()
    if ((scriptForm.value.exec_type || 'script') === 'script' && row?.id) {
      loadScriptFiles(row.id)
    }
  })
}

// 加载脚本文件树；缺省选中 main.py
const loadScriptFiles = async (scriptId) => {
  const id = scriptId || editingScript.value?.id
  if (!id) return
  try {
    const res = await preJudgeScriptApi.listFiles(id)
    scriptFiles.value = res?.files || []
    if (!activeFilePath.value && scriptFiles.value.some(f => f.path === 'main.py')) {
      await selectScriptFile('main.py', { skipDirtyGuard: true })
    }
  } catch (e) {
    // agent 类型 / 记录缺失等场景：静默降级为空文件树
    scriptFiles.value = []
  }
}

// 切换编辑文件（有未保存内容时确认；读取内容并通知编辑器重建文档）
const selectScriptFile = async (path, { skipDirtyGuard = false } = {}) => {
  if (path === activeFilePath.value) return
  if (editorDirty.value && !skipDirtyGuard) {
    try {
      await ElMessageBox.confirm(
        `「${activeFilePath.value}」有未保存的修改，切换将丢弃。是否继续？`,
        '未保存的修改',
        { type: 'warning', confirmButtonText: '放弃修改并切换', cancelButtonText: '留在当前文件' }
      )
    } catch {
      return
    }
  }
  const id = editingScript.value?.id
  if (!id) return
  try {
    const res = await preJudgeScriptApi.readFile(id, path)
    activeFilePath.value = res?.path || path
    activeFileContent.value = res?.content ?? ''
    currentEditorText.value = activeFileContent.value
    editorDirty.value = false
  } catch (e) {
    ElMessage.error(e.message || `读取文件失败：${path}`)
  }
}

const onFileTreeNodeClick = (data) => {
  if (data.path) selectScriptFile(data.path)
}

// 保存当前文件（乐观锁：updated_at 不一致 → 409）
const saveActiveFile = async () => {
  const id = editingScript.value?.id
  const path = activeFilePath.value
  if (!id || !path || fileSaving.value) return
  fileSaving.value = true
  try {
    const res = await preJudgeScriptApi.saveFile(id, {
      path,
      content: currentEditorText.value,
      updated_at: baselineUpdatedAt.value
    })
    activeFileContent.value = currentEditorText.value
    editorDirty.value = false
    if (res?.updated_at) baselineUpdatedAt.value = res.updated_at
    // model.py 声明同步（model.py 恒为准）：服务端解析回写后刷新编辑窗声明展示
    const sync = res?.schema_sync
    if (path === 'model.py' && sync && (sync.input_fields || sync.output_schema)) {
      if (sync.input_fields) {
        scriptInputFieldRows.value = Object.entries(sync.input_fields).map(([name, meta]) => ({
          name,
          value_type: (meta && meta.value_type) || 'inline',
          description: (meta && meta.description) || ''
        }))
        if (editingScript.value) editingScript.value.input_fields = sync.input_fields
      }
      if (sync.output_schema) {
        scriptOutputSchemaRows.value = Object.entries(sync.output_schema).map(([name, value]) => ({
          name,
          ...parseOutputSchemaNode(value)
        }))
        if (editingScript.value) editingScript.value.output_schema = sync.output_schema
      }
      ElMessage.success(`model.py 声明已同步：输入 ${Object.keys(sync.input_fields || {}).length} 字段 / 输出 ${Object.keys(sync.output_schema || {}).length} 键`)
    }
    const warnings = res?.warnings || []
    if (warnings.length) {
      ElMessage.warning({ message: `文件已保存。静态检查提示：${warnings.join('；')}`, duration: 6000 })
    } else {
      ElMessage.success('文件已保存')
    }
    await loadPreJudgeScripts()
  } catch (e) {
    if (e?.response?.status === 409) {
      ElMessage.error('脚本已被其他窗口修改（版本冲突），请关闭编辑窗口后重新打开')
    } else {
      ElMessage.error(e.message || '保存文件失败')
    }
  } finally {
    fileSaving.value = false
  }
}

// 新建文件（弹输入框取相对路径）
const createNewScriptFile = async () => {
  const id = editingScript.value?.id
  if (!id) return
  let path
  try {
    const { value } = await ElMessageBox.prompt(
      '文件相对路径（须为文本文件，如 utils.py 或 configs/map.yaml）',
      '新建文件',
      { inputPlaceholder: 'utils.py', inputValidator: v => !!String(v || '').trim() || '请输入路径' }
    )
    path = String(value || '').trim()
  } catch {
    return
  }
  fileTreeOpLoading.value = true
  try {
    const res = await preJudgeScriptApi.createFile(id, { path })
    ElMessage.success(`已创建 ${path}`)
    await loadScriptFiles(id)
    await selectScriptFile(res?.path || path, { skipDirtyGuard: true })
  } catch (e) {
    ElMessage.error(e.message || `新建文件失败：${path}`)
  } finally {
    fileTreeOpLoading.value = false
  }
}

// 重命名/移动文件（main.py 服务端保护，前端直接提示）
const renameScriptFileNode = async (path) => {
  if (path === 'main.py') {
    ElMessage.warning('入口文件 main.py 不可改名')
    return
  }
  const id = editingScript.value?.id
  if (!id) return
  let newPath
  try {
    const { value } = await ElMessageBox.prompt(
      `「${path}」的新相对路径`, '重命名/移动',
      { inputValue: path, inputValidator: v => !!String(v || '').trim() || '请输入路径' }
    )
    newPath = String(value || '').trim()
  } catch {
    return
  }
  if (newPath === path) return
  fileTreeOpLoading.value = true
  try {
    await preJudgeScriptApi.renameFile(id, { path, new_path: newPath })
    ElMessage.success('已重命名')
    if (activeFilePath.value === path) {
      activeFilePath.value = newPath
    }
    await loadScriptFiles(id)
    await loadPreJudgeScripts()
  } catch (e) {
    ElMessage.error(e.message || '重命名失败')
  } finally {
    fileTreeOpLoading.value = false
  }
}

// 删除文件（main.py 不可删）
const deleteScriptFileNode = async (path) => {
  if (path === 'main.py') {
    ElMessage.warning('入口文件 main.py 不可删除')
    return
  }
  const id = editingScript.value?.id
  if (!id) return
  try {
    await ElMessageBox.confirm(`确定删除文件「${path}」吗？`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  fileTreeOpLoading.value = true
  try {
    await preJudgeScriptApi.deleteFile(id, path)
    ElMessage.success('文件已删除')
    if (activeFilePath.value === path) {
      activeFilePath.value = ''
      activeFileContent.value = ''
      currentEditorText.value = ''
      editorDirty.value = false
    }
    await loadScriptFiles(id)
    await loadPreJudgeScripts()
  } catch (e) {
    ElMessage.error(e.message || '删除文件失败')
  } finally {
    fileTreeOpLoading.value = false
  }
}

// 校验并解析 JSON 文本域（harness_extra），出错时写入错误提示
const parseJsonText = (text, errorRef) => {
  const raw = String(text || '').trim()
  if (!raw) {
    errorRef.value = ''
    return null
  }
  try {
    const parsed = JSON.parse(raw)
    errorRef.value = ''
    return parsed
  } catch (e) {
    errorRef.value = `JSON 格式错误：${e.message}`
    return undefined  // 与 null（空）区分
  }
}

// 保存基本信息（元数据；script 类型的 entrypoint 由服务端管理，不随表单提交）
const saveScriptMeta = async () => {
  const harnessExtra = parseJsonText(scriptHarnessExtraText.value, scriptHarnessExtraError)
  if (harnessExtra === undefined) {
    ElMessage.error('Harness 配置 JSON 格式错误，请修正后保存')
    return
  }
  try {
    await scriptFormRef.value.validate()
  } catch {
    return
  }
  const execType = scriptForm.value.exec_type
  const payload = {
    name: scriptForm.value.name.trim(),
    description: scriptForm.value.description || null,
    exec_type: execType,
    entrypoint: execType === 'script'
      ? (editingScript.value?.entrypoint || null)
      : (String(scriptForm.value.entrypoint || '').trim() || null),
    allowed_tools: execType === 'claude_agent' && scriptForm.value.allowed_tools?.length ? scriptForm.value.allowed_tools : null,
    harness_extra: execType === 'deepseek_agent' ? harnessExtra : null,
    input_fields: buildInputFieldsPayload(),
    output_schema: buildOutputSchemaPayload(),
    timeout_seconds: scriptForm.value.timeout_seconds || 120,
    is_active: !!scriptForm.value.is_active
  }
  scriptSaving.value = true
  try {
    const saved = isScriptEdit.value
      ? await preJudgeScriptApi.update(editingScript.value.id, payload)
      : await preJudgeScriptApi.create(payload)
    if (!isScriptEdit.value) {
      editingScript.value = saved
      isScriptEdit.value = true
    } else {
      editingScript.value = { ...editingScript.value, ...saved }
    }
    baselineUpdatedAt.value = saved?.updated_at || baselineUpdatedAt.value
    ElMessage.success(isScriptEdit.value ? '基本信息已保存' : '脚本已创建')
    await loadPreJudgeScripts()
    if (payload.exec_type === 'script') await loadScriptFiles(editingScript.value?.id)
  } catch (e) {
    ElMessage.error(e.message || '保存脚本失败')
  } finally {
    scriptSaving.value = false
  }
}

// 编辑窗口关闭时若有未保存内容给出提示（仍允许关闭）
const onEditorDialogClosed = () => {
  if (editorDirty.value) {
    ElMessage.warning('存在未保存的文件修改，已放弃')
  }
}

// 编辑窗口内试跑：先保存未保存内容，再以当前声明打开试跑弹窗
const tryDebugFromEditor = async () => {
  if (editorDirty.value) {
    try {
      await ElMessageBox.confirm('当前文件有未保存修改，试跑前将先保存。是否继续？', '未保存的修改', { type: 'warning' })
    } catch {
      return
    }
    await saveActiveFile()
    if (editorDirty.value) return  // 保存失败（含 409）则不继续试跑
  }
  const merged = {
    ...(editingScript.value || {}),
    name: scriptForm.value.name,
    input_fields: buildInputFieldsPayload() ?? editingScript.value?.input_fields
  }
  openScriptDebugDialog(merged)
}

// 编辑窗口内导出 zip
const exportScriptPackageFromEditor = async () => {
  const id = editingScript.value?.id
  if (!id) return
  packageExporting.value = true
  try {
    const blob = await preJudgeScriptApi.exportPackage(id)
    const safeName = String(scriptForm.value.name || 'script').replace(/[^0-9A-Za-z_.\-\u4e00-\u9fa5]/g, '_')
    downloadBlob(blob instanceof Blob ? blob : new Blob([blob]), `${safeName}.zip`)
    ElMessage.success('脚本包已导出（代码 + manifest.json）')
  } catch (e) {
    ElMessage.error(e.message || '导出脚本包失败')
  } finally {
    packageExporting.value = false
  }
}

// ---------- 启停 / 删除 / 导出 ----------

// PUT 为全量 payload（后端 model_dump 语义），启停需携带完整字段避免其余字段被默认值覆盖
const buildFullPayloadFromRow = (row, overrides = {}) => ({
  name: row.name,
  description: row.description ?? null,
  exec_type: row.exec_type || 'script',
  entrypoint: row.entrypoint ?? null,
  allowed_tools: row.allowed_tools ?? null,
  harness_extra: row.harness_extra ?? null,
  input_fields: row.input_fields ?? null,
  output_schema: row.output_schema ?? null,
  timeout_seconds: row.timeout_seconds || 120,
  is_active: !!row.is_active,
  ...overrides
})

const togglingActiveId = ref(null)

const togglePreScriptActive = async (row) => {
  const target = !row.is_active
  const action = target ? '启用' : '停用'
  try {
    await ElMessageBox.confirm(
      `确定要${action}脚本「${row.name}」吗？${target ? '' : '停用后不再出现在任务编排可选项中。'}`,
      '提示',
      { type: 'warning' }
    )
  } catch {
    return
  }
  togglingActiveId.value = row.id
  try {
    await preJudgeScriptApi.update(row.id, buildFullPayloadFromRow(row, { is_active: target }))
    ElMessage.success(`脚本已${action}`)
    await loadPreJudgeScripts()
  } catch (e) {
    ElMessage.error(e.message || `${action}脚本失败`)
  } finally {
    togglingActiveId.value = null
  }
}

const deletingPreScriptId = ref(null)

const deletePreScript = async (row) => {
  try {
    // 后端删除为物理删除（DB 记录 + 落盘目录）；被评测任务引用时后端 400，
    // 错误消息（axios 拦截器提取 detail）会列出引用任务名，引导先解除关联
    await ElMessageBox.confirm(
      `确定要删除脚本「${row.name}」吗？删除为物理删除，脚本记录与文件将被清除且不可恢复；若该脚本被评测任务引用，需先在任务的「待评测数据」中解除关联后才能删除（仅停用请使用「停用」按钮）。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  deletingPreScriptId.value = row.id
  try {
    await preJudgeScriptApi.delete(row.id)
    ElMessage.success('脚本已删除')
    await loadPreJudgeScripts()
  } catch (e) {
    ElMessage.error(e.message || '删除脚本失败')
  } finally {
    deletingPreScriptId.value = null
  }
}

// 下载 blob 文件（与 Results.vue 同模式）
const downloadBlob = (blob, filename) => {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}

const preScriptExportingId = ref(null)

// 行级写操作（启停/删除/导出）互斥：任一进行中，操作列其余按钮一并禁用（进行中的那个由自身 :loading 自禁）
const rowOpLoading = computed(() => togglingActiveId.value !== null || deletingPreScriptId.value !== null || preScriptExportingId.value !== null)

// 导出脚本包 zip（代码 + manifest.json 契约；列表行主入口）
const exportPreScript = async (row) => {
  preScriptExportingId.value = row.id
  try {
    if (row.exec_type === 'script') {
      const blob = await preJudgeScriptApi.exportPackage(row.id)
      const safeName = String(row.name || 'script').replace(/[^0-9A-Za-z_.\-\u4e00-\u9fa5]/g, '_')
      downloadBlob(blob instanceof Blob ? blob : new Blob([blob]), `${safeName}.zip`)
      ElMessage.success('脚本包已导出（代码 + manifest.json）')
    } else {
      // agent 类型无文件目录：导出契约 JSON
      const res = await preJudgeScriptApi.export(row.id)
      const blob = res instanceof Blob ? res : new Blob([JSON.stringify(res, null, 2)], { type: 'application/json' })
      const safeName = String(row.name || 'script').replace(/[^0-9A-Za-z_.\-\u4e00-\u9fa5]/g, '_')
      downloadBlob(blob, `${safeName}.json`)
      ElMessage.success('脚本配置已导出')
    }
  } catch (e) {
    ElMessage.error(e.message || '导出脚本失败')
  } finally {
    preScriptExportingId.value = null
  }
}

// ---------- 导入脚本（.zip 脚本包 / 文件夹直选 + 包内文件树预览） ----------

// 预检限制常量：与后端导入通道对齐（app/services/pre_judge_script_service.py L44-59）
const IMPORT_MAX_TOTAL_SIZE = 10 * 1024 * 1024  // MAX_PACKAGE_TOTAL_SIZE：文件总大小 ≤10MB
const IMPORT_MAX_FILE_COUNT = 200               // MAX_PACKAGE_FILE_COUNT：文件数 ≤200
const IMPORT_MAX_FILE_SIZE = 2 * 1024 * 1024    // MAX_SCRIPT_FILE_SIZE：单文件 ≤2MB
// 锁定必选文件：剥离后包根必须同时存在（main.py 入口 + model.py 输入/输出契约声明）
const IMPORT_LOCKED_FILES = ['main.py', 'model.py']

const scriptImportVisible = ref(false)
const importOverwrite = ref(false)  // 同名活跃脚本已存在时是否覆盖更新（对齐后端 overwrite 参数）
const importLoading = ref(false)
const importResultText = ref('')
const zipInputRef = ref(null)
const folderInputRef = ref(null)
const importTreeRef = ref(null)
// 导入预览（zip/文件夹二选一，重新选择即替换）：
// { version, sourceTitle, sourceLabel, rootLabel, treeData, checkedKeys,
//   fileCount, totalSize, filteredCount, fileEntries: Map<展示路径, { body, size, originalPath }> }
// body 为 File（文件夹直选）或 Uint8Array（zip 条目）；originalPath 为原始相对路径（提交用）
const importPreview = ref(null)
const importPrecheckErrors = ref([])  // 预检错误非空时不展示树、禁用导入
let importPreviewSeq = 0  // 预览版本号：驱动 el-tree 以 :key 重挂载，保证 default-checked-keys 全量生效

const openScriptImportDialog = () => {
  importOverwrite.value = false
  importResultText.value = ''
  clearImportPreview()
  scriptImportVisible.value = true
}

// 噪音文件过滤：__pycache__ 目录、.pyc/.pyo 编译产物、任一段以 . 开头的隐藏路径（.DS_Store/.git 等）
const isImportNoisePath = (rel) => {
  const segs = String(rel || '').replace(/\\/g, '/').split('/')
  return segs.some(seg => seg === '__pycache__' || seg.startsWith('.')) || /\.(pyc|pyo)$/i.test(segs[segs.length - 1] || '')
}

// 唯一顶层目录剥离（口径复刻服务端 normalize_package：所有路径共享唯一顶层目录
// 且所有文件都带 '/' 时剥离；根级散文件存在时不剥离）。返回 { root, paths }
const stripCommonTopDir = (paths) => {
  const tops = new Set(paths.map(p => p.split('/', 1)[0]))
  if (!paths.length || tops.size !== 1 || !paths.every(p => p.includes('/'))) {
    return { root: '', paths }
  }
  const root = tops.values().next().value
  return { root, paths: paths.map(p => p.slice(root.length + 1)) }
}

// 文件大小可读化（预览摘要展示用）
const formatImportSize = (bytes) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes || 0} B`
}

// 构建单根文件树（展示路径已剥离）：目录节点 key 以 '/' 结尾防与同名文件冲突；
// main.py/model.py 置 disabled（禁用节点无法被用户取消，配合默认勾选实现锁定必选）
const buildImportTree = (rootLabel, displayPaths) => {
  const rootNode = { key: '__root__', label: rootLabel, children: [] }
  const dirNodes = new Map()
  const ensureDirNode = (dirPath) => {
    if (!dirPath) return rootNode
    const key = `${dirPath}/`
    if (!dirNodes.has(key)) {
      const parent = ensureDirNode(dirPath.split('/').slice(0, -1).join('/'))
      const node = { key, label: dirPath.split('/').pop(), children: [] }
      parent.children.push(node)
      dirNodes.set(key, node)
    }
    return dirNodes.get(key)
  }
  for (const p of [...displayPaths].sort()) {
    const parts = p.split('/')
    const name = parts.pop()
    ensureDirNode(parts.join('/')).children.push({
      key: p,
      label: name,
      locked: IMPORT_LOCKED_FILES.includes(p),
      disabled: IMPORT_LOCKED_FILES.includes(p)
    })
  }
  return [rootNode]
}

// 由 (相对路径, 文件体) 列表构建导入预览：过滤噪音 → 唯一顶层目录剥离 → 预检 → 构树。
// 任一预检失败只报错不出树；服务端会对提交的原始相对路径再做同样剥离，展示与提交幂等
const buildImportPreview = (sourceTitle, sourceLabel, entries) => {
  importResultText.value = ''  // 换源后清空上次导入结果
  const kept = entries.filter(e => !isImportNoisePath(e.relPath))
  const filteredCount = entries.length - kept.length
  if (!kept.length) {
    clearImportPreview()
    importPrecheckErrors.value = [`${sourceTitle}内没有可导入的文件（仅含噪音文件时会被自动忽略）`]
    return
  }
  const totalSize = kept.reduce((sum, e) => sum + (e.size || 0), 0)
  const { root, paths } = stripCommonTopDir(kept.map(e => e.relPath))
  // 展示路径 → 文件体索引；提交时取 originalPath（原始相对路径）重建 multipart
  const fileEntries = new Map()
  kept.forEach((e, i) => fileEntries.set(paths[i], { body: e.body, size: e.size, originalPath: e.relPath }))
  // 前端预检只做存在性与限制校验；可解析性等更强校验由后端负责
  const errors = []
  for (const locked of IMPORT_LOCKED_FILES) {
    if (!fileEntries.has(locked)) errors.push(`缺少根级 ${locked}（包内必须包含 main.py 与 model.py）`)
  }
  if (kept.length > IMPORT_MAX_FILE_COUNT) errors.push(`文件数超限（${kept.length} > ${IMPORT_MAX_FILE_COUNT}）`)
  if (totalSize > IMPORT_MAX_TOTAL_SIZE) errors.push(`文件总大小超限（${formatImportSize(totalSize)} > ${formatImportSize(IMPORT_MAX_TOTAL_SIZE)}）`)
  const oversized = kept.filter(e => (e.size || 0) > IMPORT_MAX_FILE_SIZE)
  if (oversized.length) {
    const names = oversized.slice(0, 5).map(e => e.relPath).join('、')
    errors.push(`单文件超限（> ${formatImportSize(IMPORT_MAX_FILE_SIZE)}）：${names}${oversized.length > 5 ? ' 等' : ''}`)
  }
  if (errors.length) {
    clearImportPreview()
    importPrecheckErrors.value = errors
    return
  }
  importPrecheckErrors.value = []
  importPreview.value = {
    version: ++importPreviewSeq,
    sourceTitle,
    sourceLabel,
    rootLabel: root || sourceLabel,  // 剥离时树根显示顶层目录名，否则显示 zip 名/文件夹名
    treeData: buildImportTree(root || sourceLabel, paths),
    checkedKeys: paths,  // 默认全选（main.py/model.py 禁用锁定，用户无法取消）
    fileCount: kept.length,
    totalSize,
    filteredCount,
    fileEntries
  }
}

const triggerZipPick = () => {
  zipInputRef.value?.click()
}

const triggerFolderPick = () => {
  folderInputRef.value?.click()
}

// zip 脚本包：浏览器本地 JSZip 解压预览（非目录条目 → 过滤噪音/隐藏 → 预检 → 构树）
const handleZipPick = async (event) => {
  const file = (event.target.files || [])[0]
  if (event.target) event.target.value = ''  // 允许重复选择同一文件
  if (!file) return
  let zip
  try {
    zip = await JSZip.loadAsync(file)
  } catch (e) {
    clearImportPreview()
    importPrecheckErrors.value = [`zip 解析失败：${e?.message || '文件不是合法的 zip 包'}`]
    return
  }
  const entries = []
  for (const [relPath, entry] of Object.entries(zip.files)) {
    if (entry.dir) continue  // 目录条目不参与导入
    const body = await entry.async('uint8array')
    entries.push({ relPath, size: body.byteLength, body })
  }
  buildImportPreview('已选脚本包', file.name, entries)
}

// 文件夹直选：webkitRelativePath 作相对路径，File 对象直接作提交体
const handleFolderPick = (event) => {
  const files = Array.from(event.target.files || [])
  if (event.target) event.target.value = ''  // 允许重复选择同一文件夹
  if (!files.length) return
  const entries = files.map(f => ({
    relPath: f.webkitRelativePath || f.name,
    size: f.size,
    body: f
  }))
  const sourceLabel = (files[0].webkitRelativePath || files[0].name).split('/')[0] || files[0].name
  buildImportPreview('已选文件夹', sourceLabel, entries)
}

const clearImportPreview = () => {
  importPreview.value = null
  importPrecheckErrors.value = []
}

const resetImportState = () => {
  importOverwrite.value = false
  importResultText.value = ''
  clearImportPreview()
}

// 导入结果语义文案（与后端 import_script 返回的 imported_as 对应）
const IMPORTED_AS_OUTCOMES = {
  created: '新建成功',
  revived: '同名停用脚本复活启用',
  updated: '覆盖更新成功'
}

// 脚本包导入结果行（import-package 响应 results[0] 形态，imported_as 同上权威）
const formatPackageResultLine = (item) => {
  if (item.status === 'ok') {
    const warnings = (item.warnings || []).join('；')
    const outcome = IMPORTED_AS_OUTCOMES[item.imported_as] || '导入成功'
    return `${item.source || item.name}：${outcome}（脚本=${item.name}${warnings ? `，提示：${warnings}` : ''}）`
  }
  return `${item.source || '包'}：导入失败（${item.error || '未知错误'}）`
}

// 以 multipart 调 import-package（files 与 relative_paths 一一对应）
const callImportPackage = async (files, relativePaths, sourceName = '') => {
  const formData = new FormData()
  for (const file of files) formData.append('files', file)
  for (const rel of relativePaths) formData.append('relative_paths', rel)
  if (sourceName) formData.append('source_name', sourceName)
  formData.append('overwrite', importOverwrite.value ? 'true' : 'false')
  const res = await preJudgeScriptApi.importPackage(formData)
  return res?.results || []
}

// 显式包名：zip 取文件名去扩展名，文件夹取文件夹名。平铺 zip / 无嵌套文件夹
// 展开后的路径无共同顶层目录，服务端无法从路径推断包名，必须随表单显式携带
const importSourceName = (preview) => {
  const label = preview.sourceLabel || ''
  return preview.sourceTitle === '已选脚本包' ? label.replace(/\.zip$/i, '') : label
}

// 提交导入：从树勾选中取叶子文件（main.py/model.py 锁定必选，恒在勾选集），
// 按原始相对路径重建 multipart（files 与 relative_paths 一一对应；服务端对
// 原始路径再做同样唯一顶层目录剥离，故传原始路径幂等）
const submitScriptImport = async () => {
  const preview = importPreview.value
  if (!preview || importPrecheckErrors.value.length) return
  const checkedNodes = importTreeRef.value?.getCheckedNodes(true) || []  // 仅叶子=文件节点
  const checkedKeys = new Set(checkedNodes.map(n => n.key))
  for (const locked of IMPORT_LOCKED_FILES) checkedKeys.add(locked)  // 禁用锁定节点兜底必含
  const files = []
  const relativePaths = []
  for (const key of checkedKeys) {
    const entry = preview.fileEntries.get(key)
    if (!entry) continue
    // 文件夹直选为 File 对象；zip 条目为 Uint8Array，重建为 File 以携带文件名
    files.push(entry.body instanceof File ? entry.body : new File([entry.body], entry.originalPath.split('/').pop()))
    relativePaths.push(entry.originalPath)
  }
  if (!files.length) return
  importLoading.value = true
  try {
    const results = await callImportPackage(files, relativePaths, importSourceName(preview))
    const source = `${preview.sourceTitle.replace('已选', '')}「${preview.rootLabel}」`
    importResultText.value = results.length
      ? results.map(formatPackageResultLine).join('\n')
      : `${source}：导入失败（服务端未返回结果）`
    if (results.some(r => r.status === 'ok')) {
      ElMessage.success('导入完成，详见导入结果')
    } else {
      ElMessage.error('导入失败，详见导入结果')
    }
    await loadPreJudgeScripts()
  } catch (e) {
    importResultText.value = `导入失败（${e.message || '未知错误'}）`
  } finally {
    importLoading.value = false
  }
}

// ---------- zip 脚本包上传（阶段E/F5：阻断校验链通道） ----------

const packageUploadVisible = ref(false)
const packageUploadTarget = ref(null)  // 目标脚本行
const packageUploadRef = ref(null)
const packageFile = ref(null)          // 已选 .zip（File 对象）
const packageRiskAcknowledged = ref('')  // 风险声明（风险扫描命中时必填）
const packageDeclaredRequirements = ref('')  // 依赖声明（每行一个）
const packageUploading = ref(false)

const openPackageUploadDialog = (row) => {
  packageUploadTarget.value = row
  packageRiskAcknowledged.value = row.risk_acknowledged || ''
  packageDeclaredRequirements.value = Array.isArray(row.declared_requirements) ? row.declared_requirements.join('\n') : ''
  packageFile.value = null
  packageUploadVisible.value = true
  nextTick(() => packageUploadRef.value?.clearFiles())
}

const resetPackageUploadState = () => {
  packageUploadTarget.value = null
  packageFile.value = null
  packageRiskAcknowledged.value = ''
  packageDeclaredRequirements.value = ''
  packageUploadRef.value?.clearFiles()
}

// el-upload limit=1：再次选择时替换原文件
const handlePackageFileExceed = (files) => {
  packageUploadRef.value?.clearFiles()
  const file = files[0]
  if (file) packageUploadRef.value?.handleStart(file)
}

const handlePackageFileChange = (file) => {
  const raw = file?.raw
  if (!raw) return
  if (!raw.name.toLowerCase().endsWith('.zip')) {
    ElMessage.warning('仅支持 .zip 脚本包')
    packageUploadRef.value?.clearFiles()
    packageFile.value = null
    return
  }
  packageFile.value = raw
}

// 依赖声明文本（每行一个）转 JSON 数组字符串（后端 Form 字段期望 ["pkg>=x", ...]）
const buildDeclaredRequirementsPayload = () => {
  const lines = String(packageDeclaredRequirements.value || '')
    .split('\n')
    .map(l => l.trim())
    .filter(Boolean)
  return lines.length ? JSON.stringify(lines) : ''
}

const submitPackageUpload = async () => {
  const target = packageUploadTarget.value
  if (!target || !packageFile.value) return
  packageUploading.value = true
  try {
    const formData = new FormData()
    formData.append('file', packageFile.value)
    const risk = packageRiskAcknowledged.value.trim()
    if (risk) formData.append('risk_acknowledged', risk)
    const reqs = buildDeclaredRequirementsPayload()
    if (reqs) formData.append('declared_requirements', reqs)
    await preJudgeScriptApi.uploadPackage(target.id, formData)
    ElMessage.success(`脚本包已上传并覆盖脚本「${target.name}」的执行文件`)
    packageUploadVisible.value = false
    await loadPreJudgeScripts()
  } catch (e) {
    ElMessage.error(e.message || '脚本包上传失败')
  } finally {
    packageUploading.value = false
  }
}

// ---------- 脚手架模板 zip（阶段E/F5：app/static/script_templates 下载） ----------

const scriptTemplatesVisible = ref(false)
const scriptTemplatesLoading = ref(false)
const scriptTemplates = ref([])
const templateDownloadingName = ref('')

const formatPackageSize = (bytes) => {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return '-'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(n) / Math.log(k))
  return parseFloat((n / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

const openScriptTemplatesDialog = async () => {
  scriptTemplatesVisible.value = true
  if (scriptTemplates.value.length) return  // 已加载过，直接展示
  scriptTemplatesLoading.value = true
  try {
    const res = await preJudgeScriptApi.listTemplates()
    scriptTemplates.value = res?.items || []
  } catch (e) {
    ElMessage.error(e.message || '加载脚手架模板列表失败')
  } finally {
    scriptTemplatesLoading.value = false
  }
}

// 下载模板 zip（blob 保存；download_path 缺 /api/v1 前缀的问题已在 api 层兜底处理）
const downloadScriptTemplate = async (row) => {
  templateDownloadingName.value = row.name
  try {
    const blob = await preJudgeScriptApi.downloadTemplate(row)
    downloadBlob(blob, row.filename || `${row.name}.zip`)
  } catch (e) {
    ElMessage.error(e.message || '模板下载失败')
  } finally {
    templateDownloadingName.value = ''
  }
}

// ---------- 脚本试跑（debug） ----------

const scriptDebugVisible = ref(false)
const debugScript = ref(null)
const debugFields = ref({})       // {字段名: 样例值}
const debugParamRows = ref([])    // [{key, value}]
const debugLoading = ref(false)
const debugResult = ref(null)

// 按脚本 input_fields 声明渲染的输入行（声明顺序）
const debugInputRows = computed(() => {
  const def = debugScript.value
  if (!def) return []
  return Object.entries(def.input_fields || {}).map(([name, meta]) => ({
    name,
    value_type: (meta && meta.value_type) || 'inline',
    description: (meta && meta.description) || ''
  }))
})

// 产物文件列表（files: {文件名: 路径}）
const debugFileRows = computed(() => {
  const files = debugResult.value?.files
  if (!files || typeof files !== 'object') return []
  return Object.entries(files).map(([name, path]) => ({ name, path }))
})

const debugStatusTagType = (status) => ({ ok: 'success', skip: 'info', fail: 'danger' }[status] || 'danger')

const debugStatusLabel = (status) => ({ ok: '成功', skip: '跳过', fail: '失败' }[status] || status || '-')

const formatDebugDuration = (ms) => {
  if (ms === null || ms === undefined) return '-'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`
}

const openScriptDebugDialog = (row) => {
  debugScript.value = row
  const fields = {}
  for (const name of Object.keys(row.input_fields || {})) {
    fields[name] = ''
  }
  debugFields.value = fields
  debugParamRows.value = []
  debugResult.value = null
  scriptDebugVisible.value = true
}

const resetDebugState = () => {
  debugScript.value = null
  debugFields.value = {}
  debugParamRows.value = []
  debugResult.value = null
}

// 参数值类型识别：数字 / true / false 自动转换，其余保持字符串（与 Tasks.vue 编排参数一致）
const coerceDebugParamValue = (value) => {
  if (value === null || value === undefined) return ''
  const raw = String(value).trim()
  if (raw === '') return ''
  if (/^-?\d+(\.\d+)?$/.test(raw)) return Number(raw)
  if (raw === 'true') return true
  if (raw === 'false') return false
  return value
}

const runScriptDebug = async () => {
  if (!debugScript.value || debugLoading.value) return
  // 校验声明字段均已填写（空值会按 inline 空内容落盘，易造成误判）
  const emptyFields = debugInputRows.value.filter(row => !String(debugFields.value[row.name] ?? '').trim()).map(row => row.name)
  if (emptyFields.length) {
    ElMessage.warning(`请填写输入样例：${emptyFields.join('、')}`)
    return
  }
  const fields = { ...debugFields.value }
  const params = {}
  for (const param of debugParamRows.value) {
    const key = (param.key || '').trim()
    if (key) params[key] = coerceDebugParamValue(param.value)
  }
  debugLoading.value = true
  try {
    const res = await preJudgeScriptApi.debug(debugScript.value.id, { fields, params })
    debugResult.value = { ...(res?.result || {}), run_dir: res?.run_dir }
    if (debugResult.value.status === 'ok') {
      ElMessage.success('试跑完成')
    } else {
      ElMessage.warning('试跑已结束，请检查结果详情')
    }
  } catch (e) {
    ElMessage.error(e.message || '脚本试跑失败')
  } finally {
    debugLoading.value = false
  }
}

// ==================== 在线编辑器（CodeMirror 6） ====================

const codemirrorHost = ref(null)
let editorView = null

// 按扩展名映射语言包
const editorLanguage = (path) => {
  const lower = String(path || '').toLowerCase()
  if (lower.endsWith('.py')) return python()
  if (lower.endsWith('.md')) return markdown()
  if (lower.endsWith('.json')) return jsonLang()
  if (lower.endsWith('.yaml') || lower.endsWith('.yml')) return yaml()
  return []
}

const buildEditorExtensions = (path) => [
  basicSetup,
  oneDark,
  editorLanguage(path),
  EditorView.lineWrapping,
  EditorView.updateListener.of((update) => {
    if (update.docChanged) {
      currentEditorText.value = update.state.doc.toString()
      editorDirty.value = currentEditorText.value !== activeFileContent.value
    }
  })
]

// 挂载/重建编辑器视图（对话框打开、文件切换、关闭销毁）
const mountEditor = () => {
  if (!codemirrorHost.value) return
  if (!editorView) {
    editorView = new EditorView({
      state: EditorState.create({ doc: activeFileContent.value, extensions: buildEditorExtensions(activeFilePath.value) }),
      parent: codemirrorHost.value
    })
  } else {
    editorView.setState(
      EditorState.create({ doc: activeFileContent.value, extensions: buildEditorExtensions(activeFilePath.value) })
    )
  }
}

const destroyEditor = () => {
  editorView?.destroy()
  editorView = null
}

// 对话框开关与文件内容变化的编排（nextTick 等待 DOM 挂载）
watch(scriptDialogVisible, async (visible) => {
  if (visible) {
    if (scriptForm.value.exec_type === 'script') {
      await nextTick()
      mountEditor()
    }
  } else {
    destroyEditor()
  }
})

watch(() => scriptForm.value.exec_type, async (type) => {
  if (scriptDialogVisible.value && type === 'script') {
    await nextTick()
    mountEditor()
  }
})

// 文件切换后重建文档（activeFileContent 由 selectScriptFile 设置）
watch([activeFilePath, activeFileContent], async () => {
  if (scriptDialogVisible.value && scriptForm.value.exec_type === 'script') {
    await nextTick()
    mountEditor()
  }
})

// Ctrl/Cmd + S 快捷保存（对话框打开期间监听）
const handleEditorKeydown = (e) => {
  if (!scriptDialogVisible.value) return
  if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {
    e.preventDefault()
    if (activeFilePath.value) saveActiveFile()
  }
}

onMounted(() => {
  // 加载评测前处理脚本列表
  loadPreJudgeScripts()
  window.addEventListener('keydown', handleEditorKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleEditorKeydown)
  destroyEditor()
})
</script>

<style scoped>
.scripts-page {
  max-width: 1400px;
  margin: 0 auto;
}

.script-lib-card {
  margin-bottom: 20px;
}

/* 名称列：名称 + 依赖声明标记 */
.script-name-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.script-name-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.code-block {
  background: #f5f5f5;
  padding: 10px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 12px;
  max-height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
}

/* 脚本对话框：输入字段/参数编辑行 */
.script-input-field-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

/* 输出说明树形行：占位对齐列（有子键的行放展开/收起按钮） */
.output-tree-caret {
  display: inline-flex;
  width: 22px;
  flex: none;
  justify-content: center;
}

/* 试跑对话框区块 */
.debug-block {
  margin-bottom: 16px;
}

.debug-block-title {
  font-size: 14px;
  font-weight: 600;
  color: #1F2937;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.debug-empty {
  color: #909399;
  font-size: 13px;
  padding: 8px 0;
}

.debug-summary {
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  color: #606266;
  border-radius: 4px;
  padding: 8px 12px;
  font-size: 13px;
  margin-bottom: 12px;
}

.cc-field-tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.4;
  margin-top: 4px;
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

/* 新建方式三卡片 */
.create-mode-cards {
  display: flex;
  gap: 10px;
  width: 100%;
}

.create-mode-card {
  flex: 1;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.create-mode-card:hover {
  border-color: #a0cfff;
}

.create-mode-card.active {
  border-color: #409eff;
  background: #ecf5ff;
}

.create-mode-title {
  font-weight: 600;
  font-size: 13px;
  color: #1F2937;
  margin-bottom: 4px;
}

.create-mode-desc {
  font-size: 12px;
  color: #909399;
  line-height: 1.4;
}

/* 全屏编辑窗口布局 */
/* 全屏编辑窗口布局（body 限高见文件末全局块——el-dialog teleport 下 scoped :deep 祖先匹配不生效） */
.editor-layout {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

/* Tab 高度链：el-tabs 撑满剩余高度，content 与 pane 逐层传递（文件页获得完整弹窗高度） */
.editor-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.editor-tabs :deep(.el-tabs__header) {
  margin-bottom: 8px;
  flex-shrink: 0;
}

.editor-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.editor-tabs :deep(.el-tab-pane) {
  height: 100%;
}

/* 文件 pane 需 flex 纵向布局（el-tab-pane 默认 block，内部 editor-body 的
   flex:1 撑满会失效被内容顶开）；基本信息 pane 维持自然流 + content 滚动 */
.editor-files-pane {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.model-py-tip {
  margin-bottom: 12px;
}

.editor-body {
  display: flex;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.file-tree-panel {
  width: 260px;
  flex-shrink: 0;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.file-tree-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid #e4e7ed;
  background: #f9fafb;
  font-weight: 600;
  font-size: 13px;
  color: #374151;
  flex-shrink: 0;
}

.file-tree-panel :deep(.el-tree) {
  flex: 1;
  overflow: auto;
  padding: 4px;
}

.file-tree-empty {
  padding: 12px;
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
}

.file-tree-node {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex: 1;
  min-width: 0;
  font-size: 13px;
}

.file-tree-node.is-entry .file-tree-label {
  font-weight: 600;
  color: #b45309;
}

.file-tree-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-tree-ops {
  display: none;
  gap: 6px;
  color: #909399;
  flex-shrink: 0;
}

.file-tree-node:hover .file-tree-ops {
  display: inline-flex;
}

/* 树节点操作按钮：disabled 须真实生效，故用 el-button 而非 el-icon（<i> 不支持 disabled） */
.file-tree-ops .el-button {
  color: #909399;
}

.file-tree-ops .el-button:hover {
  color: #409eff;
  background-color: transparent;
}

.entry-lock {
  color: #c0c4cc;
}

.dirty-dot {
  color: #e6a23c;
  margin-left: 4px;
}

.editor-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  overflow: hidden;
}

.editor-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid #e4e7ed;
  background: #f9fafb;
  flex-shrink: 0;
}

.editor-active-path {
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 13px;
  color: #374151;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-toolbar-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.codemirror-host {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.codemirror-host :deep(.cm-editor) {
  height: 100%;
}

.codemirror-host :deep(.cm-scroller) {
  overflow: auto;
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

/* 导入弹窗：来源按钮条 + 包内文件树预览 */
.import-source-bar {
  display: flex;
  align-items: center;
}

.import-source-bar .el-button + .el-button {
  margin-left: 12px;
}

.import-source-bar .cc-field-tip {
  margin-left: 12px;
}

.import-file-tree {
  margin-top: 8px;
  max-height: 260px;
  overflow: auto;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  padding: 6px;
}

.import-tree-node {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.precheck-error-list {
  line-height: 1.9;
}
</style>

<style>
/* 弹窗穿透规则（必须全局块：el-dialog 经 teleport 渲染到 body 下，.el-dialog
   元素不携带本组件 data-v 属性，scoped 的 :deep 祖先匹配恒不生效——曾导致
   编辑窗口 body 限高 calc(100vh - 130px) 失效、文件树/编辑器撑破视口）。
   选择器保留本页弹窗 class（pre-script-dialog / script-editor-dialog）前缀，
   不影响其他页面弹窗 */
.script-editor-dialog .el-dialog__body {
  padding: 12px 20px;
  display: flex;
  flex-direction: column;
  height: calc(100vh - 130px);
  overflow: hidden;
}

.pre-script-dialog,
.script-editor-dialog {
  border-radius: 12px;
}

.pre-script-dialog .el-dialog__header,
.script-editor-dialog .el-dialog__header {
  border-bottom: 1px solid #E5E7EB;
  padding-bottom: 16px;
}

.pre-script-dialog .el-dialog__title,
.script-editor-dialog .el-dialog__title {
  font-weight: 600;
  color: #1F2937;
}
</style>
