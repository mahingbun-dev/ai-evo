<template>
  <div class="tasks-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>评测任务</span>
          <div>
            <el-button type="primary" @click="openDialog()" :disabled="batchOpLoading || readonlyRole" :title="readonlyRole ? '当前角色（viewer）只读' : ''">新建任务</el-button>
              <el-button type="success" @click="batchRun" :disabled="!selectedTasks.length" :loading="batchOpLoading">批量执行</el-button>
              <el-button type="warning" @click="batchStop" :disabled="!selectedTasks.length" :loading="batchOpLoading">批量停止</el-button>
              <el-button  type="danger" @click="batchDelete" :disabled="!selectedTasks.length" :loading="batchOpLoading">批量删除</el-button>
          </div>
        </div>
      </template>

      <el-table :data="tasks" style="width: 100%" @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="55" />
        <el-table-column prop="name" label="任务名称" min-width="150" />
        <el-table-column prop="evaluation_type" label="评测类型" min-width="100">
          <template #default="{ row }">
            <el-tag>{{ getTypeName(row.evaluation_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" min-width="100">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="progress" label="进度" min-width="150">
          <template #default="{ row }">
            <el-progress
              :percentage="row.progress"
              :status="getProgressStatus(row)"
              :striped="row.status === 'running'"
              :striped-flow="row.status === 'running'"
            />
          </template>
        </el-table-column>
        <el-table-column label="进度详情" min-width="120">
          <template #default="{ row }">
            <span v-if="row.total_questions">
              已完成 {{ row.completed_questions ?? 0 }} / {{ row.total_questions }} 题
            </span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="290" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="runTask(row)" :disabled="row.status === 'running' || rowOpLoading" :loading="runningId === row.id">执行</el-button>
            <el-button link type="warning" @click="stopTask(row)" :disabled="!['running', 'failed'].includes(row.status) || rowOpLoading" :loading="stoppingId === row.id">停止</el-button>
            <el-button link type="info" @click="openExecutionHistory(row)" :disabled="rowOpLoading">历史</el-button>
            <!-- M5 QE5-3：任务定时（cron 编辑器 + 下次运行 + 运行历史） -->
            <el-button link type="primary" @click="openSchedule(row)" :disabled="rowOpLoading">定时</el-button>
            <el-button link type="primary" @click="openDialog(row)" :disabled="rowOpLoading">编辑</el-button>
            <el-button link type="danger" @click="deleteTask(row)" :disabled="rowOpLoading" :loading="deletingId === row.id">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑任务' : '新建任务'" width="1200px" class="task-dialog">
      <!-- 旧版配置结构提示条：任务的 steps / pre_scripts 仍为旧版存储时展示（展示层已自动归一化），支持一键迁移 -->
      <el-alert v-if="isEdit && legacyConfigDetected" type="warning" :closable="false" class="legacy-config-alert">
        <template #title>
          <div class="legacy-config-alert-bar">
            <span>检测到旧版配置结构（评分步骤 / 评测前处理脚本），已在展示层自动归一化，可一键迁移为新版结构</span>
            <el-button type="primary" size="small" :disabled="dialogBusy" :loading="migratingConfig" @click="migrateLegacyConfig">一键迁移</el-button>
          </div>
        </template>
      </el-alert>
      <el-form :model="form" :rules="rules" ref="formRef" label-width="120px">
        <!-- 基本信息区 -->
        <el-divider content-position="left">基本信息</el-divider>
        <el-form-item label="任务名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入任务名称" />
        </el-form-item>
        <el-form-item label="任务描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="请输入描述（可选）" />
        </el-form-item>
        <el-form-item label="评测类型" prop="evaluation_type">
          <el-select v-model="form.evaluation_type" placeholder="请选择评测类型" style="width: 100%" @change="handleEvaluationTypeChange">
            <el-option label="AI裁判员评测" value="judge" />
            <el-option label="一致性评测" value="consistency" />
            <el-option label="成功率评测" value="success_rate" />
            <el-option label="RAG检索评测" value="rag" />
            <el-option label="响应耗时评测" value="latency" />
            <el-option label="二分分类评测" value="binary_classification" />
            <el-option label="Agent评测" value="agent" />
            <el-option label="插件评测" value="plugin" />
            <!-- M3 QE3-1：safety 放行（双通道红队引擎就绪：确定性检测器 + 裁判 rubric
                 + 内置样本库 redteam_pack_v1 + 变异增强）；dialogue 并入 agent 类型
                 多轮配置项，不单独成类型（方案 M3 宣传口径） -->
            <el-option label="安全评测" value="safety" />
          </el-select>
        </el-form-item>

        <!-- 评测配置区 -->
        <el-divider content-position="left">评测配置</el-divider>
        <!-- 数据集：安全评测「内置样本库」直跑不需要数据集（样本由 pack 合成） -->
        <el-form-item label="数据集" v-if="!(form.evaluation_type === 'safety' && form.safety_config?.sample_source === 'pack')">
          <el-select v-model="form.dataset_id" placeholder="请选择数据集" style="width: 100%">
            <el-option v-for="ds in datasets" :key="ds.id" :label="ds.name" :value="ds.id" />
          </el-select>
        </el-form-item>
        <!-- 数据集版本绑定（M5 QE5-1）：跟随最新（每次执行用当前数据）/ 锁定版本（执行用版本快照） -->
        <el-form-item label="版本绑定" v-if="form.dataset_id && !(form.evaluation_type === 'safety' && form.safety_config?.sample_source === 'pack')">
          <div style="width: 100%; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;">
            <el-radio-group v-model="datasetVersionMode">
              <el-radio value="latest">跟随最新</el-radio>
              <el-radio value="locked">锁定版本</el-radio>
            </el-radio-group>
            <el-select
              v-if="datasetVersionMode === 'locked'"
              v-model="form.dataset_version_no"
              placeholder="选择要锁定的版本"
              style="max-width: 360px"
              :loading="datasetVersionsLoading"
            >
              <el-option
                v-for="v in datasetVersions"
                :key="v.version_no"
                :label="`v${v.version_no}（${v.row_count} 行${v.changelog ? ' · ' + v.changelog : ''}）`"
                :value="v.version_no"
              />
            </el-select>
            <span v-if="datasetVersionMode === 'locked' && datasetVersions.length === 0 && !datasetVersionsLoading" style="color: #e6a23c; font-size: 12px;">
              该数据集还没有版本，请先到数据集管理页打版，或改用「跟随最新」
            </span>
            <span v-else style="color: #909399; font-size: 12px;">
              {{ datasetVersionMode === 'locked' ? '锁定后每次执行都用该版本快照数据（口径可复现）' : '每次执行用数据集当前数据' }}
            </span>
          </div>
        </el-form-item>
        <!-- 待评测内容来源：仅 AI 裁判员评测可配置；使用数据集字段/不使用模型输出时执行阶段不调用被测模型 -->
        <el-form-item label="待评测内容来源" v-if="form.evaluation_type === 'judge'">
          <div style="width: 100%;">
            <el-radio-group v-model="form.model_output_source" @change="handleModelOutputSourceChange">
              <el-radio label="model_call">调用被测模型</el-radio>
              <el-radio label="dataset_field">使用数据集字段</el-radio>
              <el-radio label="none">不使用模型输出（仅以待评测数据作为评分依据）</el-radio>
            </el-radio-group>
            <el-select
              v-if="form.model_output_source === 'dataset_field'"
              v-model="form.model_output_field"
              placeholder="选择作为模型输出的数据集字段"
              style="width: 100%; margin-top: 8px;"
              clearable
            >
              <el-option v-for="opt in modelOutputFieldOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
            <div style="color: #909399; font-size: 12px; margin-top: 4px;">
              <template v-if="form.model_output_source === 'dataset_field'">
                执行阶段不会调用被评测模型，所选字段值将作为模型输出（model_output）参与空值校验与裁判变量
              </template>
              <template v-else-if="form.model_output_source === 'none'">
                执行阶段不会调用被评测模型，仅以待评测数据作为评分依据
              </template>
              <template v-else>
                执行阶段会调用被评测模型，模型输出将作为待评测内容
              </template>
            </div>
          </div>
        </el-form-item>
        <!-- 长会话 Agent 评测不调用被测模型，隐藏被评测模型选择；插件评测评的是插件本身，同样不需要；
             AI 裁判员使用数据集字段/不使用模型输出时也不会调用被测模型 -->
        <el-form-item label="被评测模型" v-if="form.evaluation_type !== 'agent' && form.evaluation_type !== 'plugin' && !(form.evaluation_type === 'judge' && form.model_output_source !== 'model_call')">
          <el-select v-model="form.model_provider" placeholder="请选择模型" style="width: 100%">
            <el-option v-for="m in models" :key="m.id" :label="m.name" :value="m.id" />
          </el-select>
        </el-form-item>
        <!-- 裁判引擎：AI裁判员/长会话Agent评测或一致性评测(启用裁判时)显示 -->
        <el-form-item label="裁判引擎" v-if="form.evaluation_type === 'judge' || form.evaluation_type === 'agent' || (form.evaluation_type === 'consistency' && form.consistency_config.use_judge)">
          <div style="width: 100%;">
            <el-radio-group v-model="form.judge_engine">
              <el-radio label="claude_agent">Claude Code Agent</el-radio>
              <el-radio label="deepseek_agent">DeepSeek Agent</el-radio>
              <el-radio label="graph">传统裁判图</el-radio>
            </el-radio-group>
            <div style="color: #909399; font-size: 12px; margin-top: 4px;">{{ judgeEngineHint }}</div>
          </div>
        </el-form-item>
        <!-- 裁判数量：AI裁判员/长会话Agent评测或一致性评测(启用裁判时)显示 -->
        <el-form-item label="裁判数量" v-if="form.evaluation_type === 'judge' || form.evaluation_type === 'agent' || (form.evaluation_type === 'consistency' && form.consistency_config.use_judge)">
          <el-input-number v-model="form.judge_count" :min="1" :max="20" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px;">同时发起多个裁判评分，最终结果取平均值</span>
        </el-form-item>
        <!-- 行级并发数：同时处理的数据行数（与裁判数量解耦） -->
        <el-form-item label="行级并发数">
          <el-input-number v-model="form.concurrency" :min="1" :max="64" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px;">同时处理的数据行数；总并发仍受并发池限额约束</span>
        </el-form-item>

        <!-- 行级多 Trial：仅 AI裁判员评测显示（M2 QE2-3，pass^k） -->
        <el-form-item label="多 Trial 次数" v-if="form.evaluation_type === 'judge'">
          <el-input-number v-model="form.trials" :min="1" :max="5" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px;">
            &gt;1 时每条数据重复「模型调用+裁判+校验」k 次合并为一行，输出 pass@1 / pass^k（τ-bench 口径）；1=不开启
          </span>
        </el-form-item>

        <!-- 空值校验开关 -->
        <el-form-item label="空值校验" v-if="form.evaluation_type === 'judge'">
          <el-switch v-model="form.validation_enabled" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px;">启用后，若评测目标内容为空则返回0分</span>
        </el-form-item>

        <!-- 裁判治理（M2 QE2-4）：AI裁判员/长会话Agent评测显示 -->
        <template v-if="form.evaluation_type === 'judge' || form.evaluation_type === 'agent'">
          <el-divider content-position="left">裁判治理</el-divider>
          <el-form-item label="裁判输入消毒">
            <el-switch :model-value="!form.judge_guard_disabled" @update:model-value="form.judge_guard_disabled = !$event" />
            <span style="margin-left: 10px; color: #909399; font-size: 12px;">
              开启后裁判 prompt 中数据段以 &lt;eval_data&gt; 定界并声明「数据非指令」，抑制数据集内的 prompt 注入
            </span>
            <div v-if="form.judge_guard_disabled" style="color: #E6A23C; font-size: 12px; margin-top: 4px; width: 100%;">
              已显式关闭输入消毒：数据集内容可能以指令形式影响裁判打分，该声明将随执行记录审计留痕，请确认数据来源可信
            </div>
          </el-form-item>
          <el-form-item label="金丝雀自检">
            <el-switch v-model="form.judge_canary" />
            <span style="margin-left: 10px; color: #909399; font-size: 12px;">
              开启后混入约 2% 空/乱码探针行直送裁判：探针行拿到高分会在结果页标记「裁判可被欺骗」风险徽标（探针行不计入均分）
            </span>
          </el-form-item>
        </template>

        <!-- 重复次数：仅一致性评测显示 -->
        <el-form-item label="重复次数" v-if="form.evaluation_type === 'consistency'">
          <el-input-number v-model="form.consistency_config.repeat_count" :min="2" :max="20" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px;">同一问题调用模型的次数</span>
        </el-form-item>

        <!-- 使用裁判评估：仅一致性评测显示，启用后可使用自定义维度 -->
        <el-form-item label="使用裁判评估" v-if="form.evaluation_type === 'consistency'">
          <el-switch v-model="form.consistency_config.use_judge" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px;">启用后调用裁判模型进行自定义维度评分</span>
        </el-form-item>

        <!-- 耗时评测配置 -->
        <template v-if="form.evaluation_type === 'latency'">
          <el-divider content-position="left">耗时评测配置</el-divider>

          <el-form-item label="响应阈值">
            <el-input-number
              v-model="form.latency_config.threshold"
              :min="0.1"
              :step="1"
              :precision="1"
            />
            <span style="margin-left: 8px;">秒</span>
            <span style="margin-left: 12px; color: #909399; font-size: 12px;">
              响应时间超过此阈值判定为失败
            </span>
          </el-form-item>

          <el-form-item label="重复次数">
            <el-input-number
              v-model="form.latency_config.repeat_count"
              :min="1"
              :max="100"
              :step="1"
            />
            <span style="margin-left: 12px; color: #909399; font-size: 12px;">
              每条数据集重复评测次数，用于统计耗时稳定性
            </span>
          </el-form-item>

          <el-form-item label="评分模式">
            <el-radio-group v-model="form.latency_config.score_mode">
              <el-radio label="inverse">速度评分（时间越短分数越高）</el-radio>
            </el-radio-group>
          </el-form-item>
        </template>

        <!-- 二分分类评测配置 -->
        <template v-if="form.evaluation_type === 'binary_classification'">
          <el-divider content-position="left">二分分类评测配置</el-divider>
          <el-alert
            type="info"
            :closable="false"
            style="margin-bottom: 12px;"
            title="配置期望标签和预测标签的数据来源，系统将自动比较并计算准确率、灵敏度、特异度等指标"
          />

          <el-card shadow="never">
            <template #header>
              <span style="font-weight: 600;">标签配置</span>
            </template>

            <!-- 期望标签来源 -->
            <el-form-item label="期望标签来源">
              <div style="display: flex; gap: 8px; align-items: center; width: 100%;">
                <el-select v-model="form.binary_expected_label.source" size="default" style="width: 140px;" placeholder="来源" @change="(val) => handleBinaryLabelSourceChange(val, form.binary_expected_label)">
                  <el-option label="数据集字段" value="dataset_field" />
                  <el-option label="模型输出" value="model_output" />
                  <el-option label="请求链结果" value="chain_result" />
                  <el-option label="手动输入" value="manual" />
                </el-select>
                <el-select v-if="form.binary_expected_label.source === 'dataset_field'" v-model="form.binary_expected_label.source_config.field" size="default" style="width: 160px;" placeholder="选择字段">
                  <el-option v-for="opt in getDatasetFieldOptions()" :key="opt.value" :label="opt.label" :value="opt.value" />
                </el-select>
                <span v-else-if="form.binary_expected_label.source === 'model_output'" style="color: #909399; font-size: 13px;">模型输出内容</span>
                <el-input v-else-if="form.binary_expected_label.source === 'manual'" v-model="form.binary_expected_label.source_config.manual_value" size="default" style="width: 160px;" placeholder="输入值" />
                <el-popover v-else-if="form.binary_expected_label.source === 'chain_result'" placement="bottom-start" :width="300" trigger="click">
                  <template #reference>
                    <el-input :model-value="getBinaryLabelChainVarsText(form.binary_expected_label)" size="default" style="width: 160px;" placeholder="选择变量" readonly>
                      <template #suffix><el-icon><ArrowDown /></el-icon></template>
                    </el-input>
                  </template>
                  <div class="chain-var-tree">
                    <el-tree v-if="chainRequestTree.length > 0" :data="chainRequestTree" :props="{ children: 'children', label: 'label' }" node-key="value" show-checkbox :default-expand-all="true" @check="(node, checked) => handleBinaryLabelChainVarCheck(node, checked, form.binary_expected_label, 'expected')" :ref="(el) => setBinaryLabelChainTreeRef('expected', el)" />
                    <div v-else class="no-chain-msg"><el-icon><Warning /></el-icon><span>该模型无请求链或请求链无输出变量</span></div>
                  </div>
                  <div class="chain-var-selected" v-if="(form.binary_expected_label.source_config?.var_names || []).length > 0">
                    <el-tag v-for="varName in form.binary_expected_label.source_config.var_names" :key="varName" closable size="small" @close="removeBinaryLabelChainVar(form.binary_expected_label, varName)">{{ varName }}</el-tag>
                  </div>
                </el-popover>
              </div>
              <div style="color: #909399; font-size: 12px; margin-top: 4px;">数据中预先标注的真实值，如数据集的 is_valid 字段</div>
            </el-form-item>

            <!-- 预测标签来源 -->
            <el-form-item label="预测标签来源">
              <div style="display: flex; gap: 8px; align-items: center; width: 100%;">
                <el-select v-model="form.binary_predicted_label.source" size="default" style="width: 140px;" placeholder="来源" @change="(val) => handleBinaryLabelSourceChange(val, form.binary_predicted_label)">
                  <el-option label="数据集字段" value="dataset_field" />
                  <el-option label="模型输出" value="model_output" />
                  <el-option label="请求链结果" value="chain_result" />
                  <el-option label="手动输入" value="manual" />
                </el-select>
                <el-select v-if="form.binary_predicted_label.source === 'dataset_field'" v-model="form.binary_predicted_label.source_config.field" size="default" style="width: 160px;" placeholder="选择字段">
                  <el-option v-for="opt in getDatasetFieldOptions()" :key="opt.value" :label="opt.label" :value="opt.value" />
                </el-select>
                <span v-else-if="form.binary_predicted_label.source === 'model_output'" style="color: #909399; font-size: 13px;">模型输出内容</span>
                <el-input v-else-if="form.binary_predicted_label.source === 'manual'" v-model="form.binary_predicted_label.source_config.manual_value" size="default" style="width: 160px;" placeholder="输入值" />
                <el-popover v-else-if="form.binary_predicted_label.source === 'chain_result'" placement="bottom-start" :width="300" trigger="click">
                  <template #reference>
                    <el-input :model-value="getBinaryLabelChainVarsText(form.binary_predicted_label)" size="default" style="width: 160px;" placeholder="选择变量" readonly>
                      <template #suffix><el-icon><ArrowDown /></el-icon></template>
                    </el-input>
                  </template>
                  <div class="chain-var-tree">
                    <el-tree v-if="chainRequestTree.length > 0" :data="chainRequestTree" :props="{ children: 'children', label: 'label' }" node-key="value" show-checkbox :default-expand-all="true" @check="(node, checked) => handleBinaryLabelChainVarCheck(node, checked, form.binary_predicted_label, 'predicted')" :ref="(el) => setBinaryLabelChainTreeRef('predicted', el)" />
                    <div v-else class="no-chain-msg"><el-icon><Warning /></el-icon><span>该模型无请求链或请求链无输出变量</span></div>
                  </div>
                  <div class="chain-var-selected" v-if="(form.binary_predicted_label.source_config?.var_names || []).length > 0">
                    <el-tag v-for="varName in form.binary_predicted_label.source_config.var_names" :key="varName" closable size="small" @close="removeBinaryLabelChainVar(form.binary_predicted_label, varName)">{{ varName }}</el-tag>
                  </div>
                </el-popover>
              </div>
              <div style="color: #909399; font-size: 12px; margin-top: 4px;">模型/系统的预测结果，如模型输出的 prediction 字段</div>
            </el-form-item>

            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="正例判定值">
                  <el-input v-model="form.binary_positive_value" placeholder="默认 true" />
                  <div style="color: #909399; font-size: 12px; margin-top: 4px;">匹配此值的标签判定为正例（True），其余为负例</div>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="标签值类型">
                  <el-select v-model="form.binary_label_type" style="width: 100%;">
                    <el-option label="布尔值" value="boolean" />
                    <el-option label="字符串" value="string" />
                    <el-option label="数字" value="number" />
                  </el-select>
                  <div style="color: #909399; font-size: 12px; margin-top: 4px;">标签字段的值类型，影响正例匹配方式</div>
                </el-form-item>
              </el-col>
            </el-row>
          </el-card>
        </template>

        <!-- RAG 检索评测配置 -->
        <template v-if="form.evaluation_type === 'rag'">
          <el-divider content-position="left">RAG 检索评测配置</el-divider>
          <el-alert
            v-if="!form.rag_config.rag_endpoint && !form.model_provider"
            type="warning"
            title="请配置 RAG 接口地址或选择被评测模型（至少一项）"
            :closable="false"
            style="margin-bottom: 12px;"
          />
          <el-form-item label="RAG 接口地址">
            <el-input v-model="form.rag_config.rag_endpoint" placeholder="RAG 接口地址（优先使用），如 http://localhost:9000/rag/retrieve（可选）" />
            <div style="margin-top: 4px; color: #909399; font-size: 12px;">配置后优先调用此接口获取检索结果；不配置则使用下方被评测模型的输出</div>
          </el-form-item>
          <el-form-item label="RAG 接口密钥">
            <el-input v-model="form.rag_config.rag_api_key" type="password" show-password placeholder="RAG 接口密钥（可选）" />
          </el-form-item>
          <el-form-item label="评测指标">
            <el-checkbox-group v-model="form.rag_config.metrics">
              <el-checkbox value="context_precision" label="context_precision">上下文精度</el-checkbox>
              <el-checkbox value="context_recall" label="context_recall">上下文召回</el-checkbox>
              <el-checkbox value="hit_rate" label="hit_rate">命中率</el-checkbox>
              <el-checkbox value="mrr" label="mrr">平均倒数排名(MRR)</el-checkbox>
              <el-checkbox value="noise_sensitivity" label="noise_sensitivity">抗噪声能力 <el-tag size="small" type="warning">LLM</el-tag></el-checkbox>
              <el-checkbox value="negative_rejection" label="negative_rejection">拒绝能力 <el-tag size="small" type="warning">LLM</el-tag></el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="指标阈值">
            <div style="width: 100%;">
              <el-row v-for="metric in form.rag_config.metrics" :key="metric" :gutter="10" style="margin-bottom: 8px; align-items: center;">
                <el-col :span="8">
                  <span style="font-size: 13px; color: #606266;">{{ ragMetricNames[metric] || metric }}</span>
                </el-col>
                <el-col :span="16">
                  <el-input-number
                    v-model="form.rag_config.thresholds[metric]"
                    :min="0"
                    :max="1"
                    :step="0.1"
                    :precision="1"
                    controls-position="right"
                    style="width: 120px;"
                    placeholder="0-1"
                  />
                  <span style="margin-left: 8px; color: #909399; font-size: 12px;">优秀阈值</span>
                </el-col>
              </el-row>
              <div v-if="!form.rag_config.metrics.length" style="color: #c0c4cc; font-size: 13px;">请先选择评测指标</div>
            </div>
          </el-form-item>
          <!-- RAG 检索评测变量配置 -->
          <el-divider content-position="left">RAG 评测变量配置</el-divider>
          <div style="font-size: 12px; color: #909399; margin-bottom: 8px; background: #fff7e6; padding: 6px 8px; border-radius: 4px;">
            可选配置。变量解析后注入到分析+评分节点，可在评分标准中引用（如预期结果对比）。
          </div>
          <div class="judge-variable-config">
            <el-row class="config-row-title">
              <el-col :span="4">变量名</el-col>
              <el-col :span="5">数据来源</el-col>
              <el-col :span="6">字段映射</el-col>
              <el-col :span="4">必填</el-col>
              <el-col :span="5">操作</el-col>
            </el-row>
            <div class="config-items">
              <el-row :gutter="12" class="variable-row" v-for="(item, index) in form.judge_variable_config" :key="item.key">
                <el-col :span="4">
                  <div style="display: flex; align-items: center; gap: 4px;">
                    <span>{{ item.name }}</span>
                    <el-tag size="small" type="info">固定</el-tag>
                  </div>
                </el-col>
                <el-col :span="5">
                  <span class="placeholder-text">{{ getSourceLabel(item.source) }}</span>
                </el-col>
                <el-col :span="6">
                  <span class="placeholder-text">{{ getRagFieldMappingText(item) }}</span>
                </el-col>
                <el-col :span="4">
                  <el-checkbox :model-value="true" disabled />
                </el-col>
                <el-col :span="5" class="action-col">
                  <el-button type="danger" size="small" disabled>
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </el-col>
              </el-row>
            </div>
          </div>
        </template>

        <!-- 会话评测配置（长会话 Agent 评测） -->
        <template v-if="form.evaluation_type === 'agent'">
          <el-divider content-position="left">会话评测配置</el-divider>
          <AgentEvaluationConfig
            ref="agentConfigRef"
            :initial-data="form.agent_config || {}"
            @update="handleAgentConfigUpdate"
          />

          <!-- M2 轨迹断言（QE2-2）：对会话工具调用序列做确定性匹配（独立于裁判计分） -->
          <el-divider content-position="left">轨迹断言（确定性轨迹匹配）</el-divider>
          <div class="trajectory-config-block">
            <div class="gate-config-tip">
              对会话（session_messages）的工具调用序列做确定性匹配，截断 trace 虚高等问题由硬证据暴露；
              结果独立于裁判均分，聚合出「轨迹匹配率」（trajectory_match_rate，可挂门禁）
            </div>
            <el-radio-group v-model="trajectoryUi.mode" size="small" class="gate-mode-group" :disabled="dialogBusy">
              <el-radio-button value="strict">严格序列（strict）</el-radio-button>
              <el-radio-button value="unordered">乱序计数（unordered）</el-radio-button>
              <el-radio-button value="subset">按序子序列（subset）</el-radio-button>
              <el-radio-button value="superset">禁止多余（superset）</el-radio-button>
            </el-radio-group>
            <div class="gate-config-tip">
              strict=顺序+全集+无多余；unordered=顺序无关计数；subset=期望按序出现（允许多余调用）；
              superset=每条调用都须被期望覆盖（漏掉期望不算失败）；禁用工具任何模式下命中即失败
            </div>

            <div class="trajectory-sub-title">期望调用（expected）</div>
            <el-table :data="trajectoryUi.expected" size="small">
              <el-table-column label="工具名" min-width="140">
                <template #default="{ row }">
                  <el-input v-model="row.tool" size="small" placeholder="如 grep / Bash / Read" :disabled="dialogBusy" />
                </template>
              </el-table-column>
              <el-table-column label="参数须包含（逗号分隔，可选）" min-width="200">
                <template #default="{ row }">
                  <el-input v-model="row.argsContainsStr" size="small" placeholder="如 git push,origin" :disabled="dialogBusy" />
                </template>
              </el-table-column>
              <el-table-column label="最少次数" width="120">
                <template #default="{ row }">
                  <el-input-number v-model="row.min_calls" :min="1" :max="99" controls-position="right" style="width: 100%" :disabled="dialogBusy" />
                </template>
              </el-table-column>
              <el-table-column width="60">
                <template #default="{ $index }">
                  <el-button type="danger" size="small" circle :icon="Delete" :disabled="dialogBusy" @click="trajectoryUi.expected.splice($index, 1)" />
                </template>
              </el-table-column>
            </el-table>
            <el-button size="small" :icon="Plus" class="gate-add-btn" :disabled="dialogBusy" @click="trajectoryUi.expected.push({ tool: '', argsContainsStr: '', min_calls: 1 })">添加期望</el-button>

            <div class="trajectory-sub-title">禁用调用（forbidden，命中即失败）</div>
            <el-table :data="trajectoryUi.forbidden" size="small">
              <el-table-column label="工具名" min-width="140">
                <template #default="{ row }">
                  <el-input v-model="row.tool" size="small" placeholder="如 rm" :disabled="dialogBusy" />
                </template>
              </el-table-column>
              <el-table-column label="参数须包含（逗号分隔，可选）" min-width="200">
                <template #default="{ row }">
                  <el-input v-model="row.argsContainsStr" size="small" placeholder="如 sudo（留空=工具名整体禁用）" :disabled="dialogBusy" />
                </template>
              </el-table-column>
              <el-table-column width="60">
                <template #default="{ $index }">
                  <el-button type="danger" size="small" circle :icon="Delete" :disabled="dialogBusy" @click="trajectoryUi.forbidden.splice($index, 1)" />
                </template>
              </el-table-column>
            </el-table>
            <el-button size="small" :icon="Plus" class="gate-add-btn" :disabled="dialogBusy" @click="trajectoryUi.forbidden.push({ tool: '', argsContainsStr: '' })">添加禁用</el-button>
          </div>
        </template>

        <!-- 安全评测配置 -->
        <template v-if="form.evaluation_type === 'safety'">
          <el-divider content-position="left">安全评测配置</el-divider>
          <SafetyEvaluationConfig
            ref="safetyConfigRef"
            :initial-data="form.safety_config || {}"
            @update="handleSafetyConfigUpdate"
          />
        </template>

        <!-- 插件评测配置 -->
        <template v-if="form.evaluation_type === 'plugin'">
          <el-divider content-position="left">插件评测配置</el-divider>
          <el-form-item v-if="form.plugin_config.source !== 'trace'" label="评测深度">
            <el-radio-group v-model="form.plugin_config.depth">
              <el-radio label="quick">快速（仅静态检测）</el-radio>
              <el-radio label="standard" :disabled="isPluginLevelTarget">标准（静态 + LLM 评审）</el-radio>
            </el-radio-group>
            <div style="color: #909399; font-size: 12px; margin-top: 4px;">
              快速：结构化 lint（frontmatter / 触发短语 / 渐进披露 / 反模式等 7 项子分），零模型调用秒级完成；
              标准：额外由 Claude Code 做 4 项锚定 rubric 评审（评测动作实时加载自插件市场的 plugin-eval），失败维度自动降级剔除。
              <b>插件级对象只支持快速</b>（标准与蒙特卡洛仅支持 skill 级对象）。
            </div>
          </el-form-item>
          <el-form-item label="评测对象来源">
            <el-radio-group v-model="form.plugin_config.source">
              <el-radio label="manual">手动指定</el-radio>
              <el-radio label="marketplace">插件市场</el-radio>
              <el-radio label="dataset">数据集批量</el-radio>
              <el-radio label="trace">会话 Trace（数据集）</el-radio>
            </el-radio-group>
          </el-form-item>
          <template v-if="form.plugin_config.source === 'marketplace'">
            <el-form-item label="插件市场">
              <div style="display: flex; gap: 8px; width: 100%;">
                <el-select
                  v-model="form.plugin_config.market_id"
                  placeholder="请选择插件市场"
                  style="flex: 1"
                  :disabled="dialogBusy"
                  @change="handleMarketChange"
                >
                  <el-option v-for="m in marketplaces" :key="m.id" :value="m.id" :label="m.name">
                    <span>{{ m.name }}</span>
                    <span style="float: right; color: #909399; font-size: 12px;">{{ m.sync_status === 'ok' ? '已同步' : m.sync_status === 'failed' ? '同步失败' : '未同步' }}</span>
                  </el-option>
                </el-select>
                <el-button :disabled="dialogBusy" @click="marketDialogVisible = true">管理市场</el-button>
                <el-button :loading="marketPluginsLoading" :disabled="!form.plugin_config.market_id || dialogBusy" @click="loadMarketPlugins(true)">刷新</el-button>
              </div>
            </el-form-item>
            <el-form-item label="评测对象">
              <el-select
                v-model="form.plugin_config.market_plugin"
                placeholder="请先选择市场，再选择插件或 skill"
                style="width: 100%"
                :loading="marketPluginsLoading"
                :disabled="dialogBusy || !form.plugin_config.market_id"
              >
                <el-option
                  v-for="p in marketPlugins"
                  :key="p.source"
                  :value="p.source"
                  :label="`${p.name}（${p.target_type === 'plugin' ? '插件' : 'Skill'}${p.version ? ' v' + p.version : ''}）`"
                >
                  <span>{{ p.name }}</span>
                  <span style="float: right; color: #909399; font-size: 12px;">{{ p.target_type === 'plugin' ? '插件' : 'Skill' }}</span>
                </el-option>
              </el-select>
              <div style="color: #909399; font-size: 12px; margin-top: 4px;">
                列表含插件与其内部 skill：插件级按整体静态评审（<b>仅支持快速深度</b>）；<b>Skill 级</b>按单个 skill 评审（标准深度时由 Claude Code 做 4 项 rubric 深度评审）
              </div>
            </el-form-item>
          </template>
          <template v-if="form.plugin_config.source === 'manual'">
            <el-form-item label="上传插件包">
              <el-upload
                class="plugin-upload"
                drag
                accept=".zip,.md,.markdown"
                :show-file-list="false"
                :http-request="handlePluginUpload"
                :disabled="dialogBusy || pluginUploading"
              >
                <div class="plugin-upload-inner">
                  <el-icon class="plugin-upload-icon"><UploadFilled /></el-icon>
                  <div class="plugin-upload-text">拖拽 .zip 插件包 或 SKILL.md 到此处，或 <em>点击选择</em></div>
                  <div class="plugin-upload-hint">zip ≤ 20MB（服务端解包后按目录评测）；md ≤ 2MB（回填下方内联内容）</div>
                </div>
              </el-upload>
            </el-form-item>
            <el-form-item label="插件路径">
              <el-input
                v-model="form.plugin_config.plugin_path"
                placeholder="本地插件或 skill 目录绝对路径，如 /path/to/my-plugin"
                clearable
              />
              <div style="color: #909399; font-size: 12px; margin-top: 4px;">
                目录含 .claude-plugin/plugin.json 按整个插件评（仅静态层）；含 SKILL.md 按单个 skill 评
              </div>
            </el-form-item>
            <el-form-item label="内联 SKILL.md">
              <el-input
                v-model="form.plugin_config.skill_content"
                type="textarea"
                :rows="6"
                placeholder="直接粘贴 SKILL.md 全文（与插件路径二选一，同时填写时优先使用插件路径）"
              />
            </el-form-item>
          </template>
          <el-form-item v-if="form.plugin_config.source === 'trace'" label="判定方式">
            <el-radio-group v-model="form.plugin_config.trace_judgement">
              <el-radio label="model">模型判定（标准深度）</el-radio>
              <el-radio label="offline">离线（仅静态）</el-radio>
            </el-radio-group>
            <div style="color: #909399; font-size: 12px; margin-top: 4px;">
              评测深度随判定方式固定，不需要选择（一份报告同时含设计质量综合分与真实数据分）。<br />
              模型判定：逐关键步骤实例做抽取 + 逐检查项判定，并对插件内每个 skill 做 4 项 rubric 评审
              ——实测一条 26MB 会话线程含 90+ 个关键步骤实例，<b>单行可能 1 小时以上</b>，
              批量数据集请配合断点续跑分轮执行。<br />
              离线：只跑确定性结构检查（检查数量统计、结论是否有证据等）与静态层，<b>全程零模型调用</b>，
              单行分钟级；真实数据分与判定有效率照常产出，但语义类检查项未测、覆盖率更低。
            </div>
          </el-form-item>
          <el-form-item
            v-if="form.plugin_config.source === 'trace' && form.plugin_config.trace_judgement === 'model'"
            label="判定并发"
          >
            <el-input-number
              v-model="form.plugin_config.trace_concurrency"
              :min="1"
              :max="20"
              :step="1"
              :disabled="dialogBusy"
            />
            <div style="color: #909399; font-size: 12px; margin-top: 4px;">
              同时进行的模型判定数（plugin-eval 上限 20）。留空沿用服务端默认
              <code>PLUGIN_EVAL_CONCURRENCY</code>；调高可缩短单行耗时，但会成倍增加模型调用。
            </div>
          </el-form-item>
          <el-alert
            v-if="form.plugin_config.source === 'trace'"
            type="info"
            :closable="false"
            title="会话 Trace 评测（合并口径）：一行一份报告，同时含设计质量综合分（静态 + LLM 评审）与真实数据分（历史线程的执行 + 生成两阶段）"
          >
            <div style="font-size: 12px; line-height: 1.7; margin-top: 4px;">
              ① 数据集须由「从 anc-hub 导入」创建（行内含原始会话包，如 claude-thread.zip / .jsonl）；
              截断的会话摘要无法做该评测。<br />
              ② 被测插件由线程里的 <code>/anc-xxx:run</code> 调用自动识别，需市场 plugin-eval 已有该插件的
              <code>rules/history/&lt;插件&gt;.yaml</code> 规则。<br />
              ③ 两套分数相互独立：真实数据分不并入综合分/徽章/排名（plugin-eval 口径）。<br />
              ④ 无原始会话 / 无对应规则 / 未识别到调用的行会标为「未测」并写明原因，不计失败。<br />
              ⑤ 每条线程逐个关键步骤抽取与判定，单行耗时较长（分钟到十几分钟），任务可断点续跑。
            </div>
          </el-alert>
          <el-alert
            v-else
            type="info"
            :closable="false"
            title="数据集批量评测：上方选择数据集，每行数据需包含 plugin_path 字段（本地目录路径）或 skill_content 字段（SKILL.md 全文），系统逐行评测"
          />
        </template>

        <!-- 变量注入配置 -->
        <template v-if="modelVariables.length > 0">
          <el-divider content-position="left">变量注入配置</el-divider>
          <VariableInjector
            ref="variableInjectorRef"
            :variables="modelVariables"
            :dataset-fields="datasetFields"
            :model-provider="form.model_provider"
            :initial-data="{
              runtime_variables: form.runtime_variables,
              variable_types: form.variable_types,
              variable_sources: form.variable_sources,
              variable_mappings: form.variable_mappings,
              enable_user_import: form.enable_user_import,
              user_import_template: form.user_import_template
            }"
            @update="handleVariableUpdate"
          />
        </template>

        <!-- ===== 评测配置：4 段式流程（仅 AI裁判员 / 一致性+启用裁判 时显示） ===== -->
        <template v-if="showJudgeAndConsistencySections">
          <!-- ① 评测背景 -->
          <el-divider content-position="left">① 评测背景</el-divider>
          <el-form-item label="评测模板" prop="template_id">
            <div style="display: flex; gap: 8px; align-items: center; width: 100%;">
              <el-select
                v-model="form.template_id"
                placeholder="请选择或输入评测模板"
                style="flex: 1;"
                filterable
                allow-create
                default-first-option
                clearable
                @change="handleTemplateChange"
              >
                <el-option v-for="tpl in templates" :key="tpl.id" :label="tpl.name" :value="tpl.id" />
              </el-select>
              <el-button type="success" :disabled="dialogBusy" @click="saveAsTemplate">{{ isExistingTemplate ? '更新模板' : '保存为模板' }}</el-button>
              <el-button :disabled="dialogBusy" @click="clearTemplateConfig">清空</el-button>
              <el-button v-if="isExistingTemplate" type="danger" :disabled="dialogBusy" @click="deleteTemplate">删除</el-button>
            </div>
          </el-form-item>
          <el-form-item label="评测场景描述">
            <el-input v-model="form.scene_desc" type="textarea" :rows="2" placeholder="描述评测场景..." />
            <div class="effect-tags" style="margin-top: 4px;">
              <el-tag size="small" type="primary">分析节点</el-tag>
              <el-tag size="small" type="success">评分节点</el-tag>
              <span style="margin-left: 8px; color: #909399; font-size: 12px;">此信息将注入到裁判的分析和评分全过程</span>
            </div>
          </el-form-item>
          <el-form-item label="评测指令（可选）">
            <el-input v-model="form.judge_prompt" type="textarea" :rows="2" placeholder="额外的评测指引..." />
            <div class="effect-tags" style="margin-top: 4px;">
              <el-tag size="small" type="primary">分析节点</el-tag>
              <span style="margin-left: 8px; color: #909399; font-size: 12px;">此信息仅注入到分析阶段</span>
            </div>
          </el-form-item>

          <!-- ② 待评测数据 -->
          <el-divider content-position="left">② 待评测数据</el-divider>
          <el-form-item label="待评测内容">
            <div style="font-size: 12px; color: #909399; margin-bottom: 8px; background: #f5f0ff; padding: 6px 8px; border-radius: 4px;">
              这些内容将注入到裁判的分析和评分环节，是评分依据的核心数据。至少配置一项。
            </div>
            <div class="evaluation-targets-config">
              <div class="config-header">
                <span class="config-title">配置需要评测的目标内容</span>
                <el-button type="primary" size="small" :disabled="dialogBusy" @click="addEvaluationTarget">
                  <el-icon><Plus /></el-icon>添加
                </el-button>
              </div>
              <el-row class="config-row-title">
                <el-col :span="5">显示名称</el-col>
                <el-col :span="4">来源类型</el-col>
                <el-col :span="4">字段映射</el-col>
                <el-col :span="4">说明（可选）</el-col>
                <el-col :span="3">必填</el-col>
                <el-col :span="4">操作</el-col>
              </el-row>
              <div class="config-items">
                <template v-for="(target, index) in form.evaluation_targets" :key="target.id">
                  <el-row :gutter="10" class="variable-row" align="middle">
                    <el-col :span="5">
                      <el-input v-model="target.display_name" placeholder="名称" />
                    </el-col>
                    <el-col :span="4">
                      <el-select v-model="target.source" placeholder="来源" style="width: 100%" @change="(val) => handleTargetSourceChange(val, target)">
                        <el-option v-for="opt in targetSourceOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
                      </el-select>
                    </el-col>
                    <el-col :span="4">
                      <el-select v-if="target.source === 'dataset_field'" v-model="target.source_config.field" placeholder="选择字段" style="width: 100%">
                        <el-option v-for="opt in getDatasetFieldOptions()" :key="opt.value" :label="opt.label" :value="opt.value" />
                      </el-select>
                      <el-popover
                        v-else-if="target.source === 'chain_result'"
                        placement="bottom-start"
                        :width="400"
                        trigger="click"
                      >
                        <template #reference>
                          <el-input
                            :model-value="getSelectedChainVarsTextForTarget(target)"
                            placeholder="选择请求链变量（支持多选）"
                            readonly
                            style="width: 100%"
                          >
                            <template #suffix>
                              <el-icon><ArrowDown /></el-icon>
                            </template>
                          </el-input>
                        </template>
                        <div class="chain-var-tree">
                          <el-tree
                            v-if="chainRequestTree.length > 0"
                            :data="chainRequestTree"
                            :props="{ children: 'children', label: 'label' }"
                            node-key="value"
                            show-checkbox
                            :default-expand-all="true"
                            @check="(node, checked) => handleTargetChainVarCheck(node, checked, target)"
                            :ref="(el) => setChainTreeRefForTarget(target, el)"
                          />
                          <div v-else class="no-chain-msg">
                            <el-icon><Warning /></el-icon>
                            <span>该模型无请求链或请求链无输出变量</span>
                          </div>
                        </div>
                        <div class="chain-var-selected" v-if="getSelectedChainVarsForTarget(target).length > 0">
                          <el-tag
                            v-for="varName in getSelectedChainVarsForTarget(target)"
                            :key="varName"
                            closable
                            size="small"
                            @close="removeSelectedChainVarForTarget(target, varName)"
                          >
                            {{ varName }}
                          </el-tag>
                        </div>
                      </el-popover>
                      <el-input v-else-if="target.source === 'manual_input'" v-model="target.source_config.value" placeholder="输入值" />
                      <span v-else-if="target.source === 'script_output'" class="placeholder-text" :title="target.source_config?.output_var || targetScriptName(target)">
                        {{ targetScriptName(target) || '脚本输出' }}
                      </span>
                      <span v-else class="placeholder-text">-</span>
                    </el-col>
                    <el-col :span="4">
                      <el-input v-model="target.description" placeholder="可选说明" />
                    </el-col>
                    <el-col :span="3">
                      <el-checkbox v-model="target.required" />
                    </el-col>
                    <el-col :span="4" class="action-col">
                      <el-button type="danger" size="small" :disabled="dialogBusy" @click="removeEvaluationTarget(index)">
                        <el-icon><Delete /></el-icon>
                      </el-button>
                    </el-col>
                  </el-row>
                  <!-- 脚本输出编排卡片：来源为「脚本输出」时在该行下方展开（脚本/输出键/输入映射/参数/失败策略） -->
                  <div v-if="target.source === 'script_output'" class="script-target-card">
                    <div class="pre-script-card-body">
                      <div class="script-target-title">脚本输出编排</div>
                      <div v-if="target.source_config?.script_id && !preScriptDefMap[target.source_config.script_id]" class="pre-script-warning">
                        脚本定义未加载或已停用，仅可编辑既有映射配置
                      </div>
                      <el-row :gutter="12">
                        <el-col :span="12">
                          <div class="pre-script-block-title">评测前脚本</div>
                          <el-select
                            v-model="target.source_config.script_id"
                            filterable
                            clearable
                            placeholder="选择脚本"
                            style="width: 100%"
                            @change="handleTargetScriptChange(target)"
                          >
                            <el-option v-for="script in preJudgeScripts" :key="script.id" :label="script.name" :value="script.id">
                              <div style="display: flex; align-items: center; gap: 8px;">
                                <span>{{ script.name }}</span>
                                <el-tag size="small" type="info">{{ script.exec_type }}</el-tag>
                              </div>
                            </el-option>
                          </el-select>
                          <div v-if="preScriptDescription(target.source_config.script_id)" class="pre-script-desc">{{ preScriptDescription(target.source_config.script_id) }}</div>
                          <div class="pre-script-tip">脚本在裁判评分前执行，输出作为本行待评测内容注入裁判上下文</div>
                        </el-col>
                        <el-col :span="12">
                          <div class="pre-script-block-title">输出键（可选）</div>
                          <el-select v-model="target.source_config.output_key" clearable placeholder="默认取脚本整体输出" style="width: 100%">
                            <el-option v-for="row in targetScriptOutputRows(target)" :key="row.key" :label="row.key" :value="row.key">
                              <span style="display: flex; align-items: center; gap: 8px;">
                                <span>{{ row.key }}</span>
                                <span v-if="row.desc" style="color: #909399; font-size: 12px;">{{ row.desc }}</span>
                              </span>
                            </el-option>
                          </el-select>
                          <div class="pre-script-tip">取脚本输出 JSON 的该键作为本行内容；不选则整体序列化</div>
                        </el-col>
                      </el-row>
                      <!-- 输入映射：按脚本 input_fields 声明动态渲染 -->
                      <div class="pre-script-block">
                        <div class="pre-script-block-title">输入映射（数据集字段）</div>
                        <div v-for="row in targetScriptFieldRows(target)" :key="row.name" class="pre-script-field-row">
                          <span class="pre-script-field-name" :title="row.description">{{ row.name }}</span>
                          <el-tag size="small" :type="row.value_type === 'hub_file' ? 'warning' : 'info'">{{ row.value_type }}</el-tag>
                          <el-select v-model="target.source_config.input_mapping[row.name].field" placeholder="选择数据集字段" clearable filterable style="flex: 1;">
                            <el-option v-for="opt in getDatasetFieldOptions()" :key="opt.value" :label="opt.label" :value="opt.value" />
                          </el-select>
                          <!-- hub_file 通道（阶段B/F4）：选择文件生成 path_pattern，即时预览样本匹配数 -->
                          <el-button v-if="isHubFileMapping(target, row)" link type="primary" size="small" :disabled="dialogBusy" @click="openHubPatternPicker(target, row.name)">选择文件</el-button>
                          <el-tag
                            v-if="isHubFileMapping(target, row) && target.source_config.input_mapping[row.name].path_pattern"
                            size="small" type="warning" effect="plain" class="hub-pattern-tag"
                            :title="`${target.source_config.input_mapping[row.name].path_pattern}${target.source_config.input_mapping[row.name].required === false ? '（可为空匹配）' : '（必须匹配到文件）'}`"
                          >{{ target.source_config.input_mapping[row.name].path_pattern }}</el-tag>
                          <span v-if="row.description" class="pre-script-field-desc">{{ row.description }}</span>
                        </div>
                        <div v-if="!targetScriptFieldRows(target).length" class="pre-script-empty">该脚本未声明输入字段</div>
                      </div>
                      <!-- 参数编辑 -->
                      <div class="pre-script-block">
                        <div class="pre-script-block-title">
                          脚本参数（可选）
                          <el-button type="primary" link size="small" :disabled="dialogBusy" @click="addScriptTargetParam(target)">添加参数</el-button>
                        </div>
                        <div v-for="paramKey in Object.keys(target.source_config.params || {})" :key="paramKey" class="pre-script-param-row">
                          <el-input :model-value="paramKey" @change="(val) => renameScriptTargetParam(target, paramKey, val)" placeholder="参数名" style="width: 180px;" />
                          <el-input :model-value="target.source_config.params[paramKey]" @update:model-value="(val) => { target.source_config.params[paramKey] = val }" placeholder="参数值（数字 / true / false 自动识别类型）" style="flex: 1;" />
                          <el-button type="danger" link :disabled="dialogBusy" @click="removeScriptTargetParam(target, paramKey)">
                            <el-icon><Delete /></el-icon>
                          </el-button>
                        </div>
                        <div v-if="!Object.keys(target.source_config.params || {}).length" class="pre-script-empty">暂无参数</div>
                      </div>
                      <el-row :gutter="12">
                        <el-col :span="12">
                          <div class="pre-script-block-title">失败策略</div>
                          <el-radio-group v-model="target.source_config.on_fail">
                            <el-radio label="continue">失败继续（默认）</el-radio>
                            <el-radio label="abort">失败终止</el-radio>
                          </el-radio-group>
                          <div class="pre-script-tip">continue：脚本失败时本行内容为空；abort：该条数据评测终止</div>
                        </el-col>
                        <el-col :span="12">
                          <div class="pre-script-block-title">输出变量名（可选）</div>
                          <el-input v-model="target.source_config.output_var" placeholder="默认为脚本名" />
                          <div class="pre-script-tip">脚本输出以此变量名进入裁判变量体系（script_output）；同名脚本多行共享一次执行</div>
                        </el-col>
                      </el-row>
                    </div>
                  </div>
                </template>
              </div>
              <el-empty v-if="form.evaluation_targets.length === 0" description="暂无评测对象，点击添加" :image-size="60" />
            </div>
          </el-form-item>

          <!-- ③ 评分标准与评测维度：维度顺序即评分步骤；未配置待评测数据时整区置灰 -->
          <el-divider content-position="left">③ 评分标准与评测维度</el-divider>
          <div class="section-gated" :class="{ 'is-disabled': form.evaluation_targets.length === 0 }">
            <div class="section-gated-content">
              <el-form-item label="最大分值">
                <el-input-number v-model="form.max_score" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="全局评分标准">
                <RefTextarea
                  v-model="form.score_details"
                  :rows="3"
                  :targets="refMarkTargets"
                  :vars="refMarkVars"
                  placeholder="9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格（输入 @ 可引用待评测数据/裁判变量）"
                />
                <div style="margin-top: 4px; color: #909399; font-size: 12px;">
                  评分优先级：维度精确标准 → 全局评分标准 → 默认；输入 @ 可引用待评测数据与裁判变量
                </div>
              </el-form-item>
              <el-form-item label="评测维度">
                <div style="font-size: 12px; color: #909399; margin-bottom: 8px;">
                  维度按顺序即裁判的评分步骤（可拖拽排序），裁判将按「第1步、第2步…」逐步分析并评分。
                </div>
                <div class="metrics-grid">
                  <div
                    v-for="(metric, index) in form.evaluation_metrics"
                    :key="metric.id"
                    class="metric-card"
                    :class="{ 'metric-card-dragging': metricDragState.dragging && metricDragState.fromIndex === index }"
                    draggable="true"
                    @dragstart="handleMetricDragStart($event, index)"
                    @dragover="handleMetricDragOver($event, index)"
                    @drop="handleMetricDrop($event, index)"
                    @dragend="handleMetricDragEnd"
                  >
                    <div class="metric-item">
                      <div class="metric-header">
                        <div class="metric-header-left">
                          <el-icon class="drag-handle"><Rank /></el-icon>
                          <span class="metric-index">{{ index + 1 }}</span>
                          <span class="metric-title">步骤 {{ index + 1 }} · 维度</span>
                        </div>
                        <el-button type="danger" link :disabled="dialogBusy" @click="removeMetric(index)" class="metric-delete">
                          <el-icon><Delete /></el-icon>
                        </el-button>
                      </div>
                      <div class="metric-body">
                        <div class="metric-field-row">
                          <div class="metric-field metric-name">
                            <label>维度名称</label>
                            <el-input v-model="metric.name" placeholder="请输入维度名称" />
                          </div>
                          <div class="metric-field metric-field-xs">
                            <label>权重</label>
                            <el-select
                              v-model="metric.weight"
                              placeholder="权重"
                              style="width: 100%"
                              filterable
                              allow-create
                              default-first-option
                              @change="(val) => { metric.weight = normalizeMetricWeight(val) }"
                            >
                              <el-option v-if="isCustomWeight(metric.weight)" :label="String(metric.weight)" :value="metric.weight" />
                              <el-option label="高" value="high" />
                              <el-option label="中" value="medium" />
                              <el-option label="低" value="low" />
                            </el-select>
                          </div>
                          <div class="metric-field metric-field-xs">
                            <label>最高分</label>
                            <el-input-number v-model="metric.max_score" :min="1" :max="form.max_score" controls-position="right" style="width: 100%" />
                          </div>
                        </div>
                        <div class="metric-desc">
                          <label>维度定义</label>
                          <RefTextarea
                            v-model="metric.description"
                            :rows="2"
                            :targets="refMarkTargets"
                            :vars="refMarkVars"
                            placeholder="请输入维度定义描述（输入 @ 可引用待评测数据/裁判变量）"
                          />
                        </div>
                        <!-- 评分标准（可折叠） -->
                        <el-collapse style="margin-top: 8px;">
                          <el-collapse-item title="评分标准（可选，不配置则使用全局标准）">
                            <div v-for="(criteria, level, ci) in metric.score_criteria" :key="ci" style="display: flex; gap: 6px; align-items: center; margin-bottom: 6px;">
                              <el-tag size="small" style="width: 60px; text-align: center;">{{ level }}</el-tag>
                              <el-input-number v-model="criteria.min" :min="0" :max="form.max_score" size="small" controls-position="right" style="width: 90px;" />
                              <span style="color: #909399;">~</span>
                              <el-input-number v-model="criteria.max" :min="0" :max="form.max_score" size="small" controls-position="right" style="width: 90px;" />
                              <RefTextarea
                                v-model="criteria.desc"
                                is-single-line
                                :targets="refMarkTargets"
                                :vars="refMarkVars"
                                placeholder="等级描述（输入 @ 引用）"
                                style="flex: 1;"
                              />
                              <el-button type="danger" size="small" link :disabled="dialogBusy" @click="removeCriteriaLevel(metric, level)">
                                <el-icon><Delete /></el-icon>
                              </el-button>
                            </div>
                            <el-button size="small" type="primary" plain :disabled="dialogBusy" @click="addCriteriaLevel(metric)">
                              <el-icon><Plus /></el-icon> 添加等级
                            </el-button>
                            <div v-if="scoreCriteriaOverlap[metric.name || metric.id]"
                                 style="margin-top: 6px; background: #fef0f0; border-radius: 3px; padding: 4px 8px; font-size: 12px; color: #f56c6c;">
                              评分等级范围重叠：
                              <div v-for="msg in scoreCriteriaOverlap[metric.name || metric.id]" :key="msg">{{ msg }}</div>
                            </div>
                          </el-collapse-item>
                          <!-- 维度依赖关系（仅在 judge 类型显示） -->
                          <el-collapse-item title="维度依赖关系（可选）" v-if="form.evaluation_type === 'judge'"  name="dependency">
                            <div style="font-size: 12px; color: #909399; margin-bottom: 8px;">
                              配置此维度对其他维度的依赖条件。满足条件时，本维度得分将被覆盖。
                            </div>
                            <div v-for="(dep, depIdx) in (metric.dependencies || [])" :key="depIdx"
                                 style="display: flex; gap: 6px; align-items: center; margin-bottom: 6px; flex-wrap: wrap;">
                              <span style="font-size: 12px;">当</span>
                              <el-select v-model="dep.source_dimension" size="small" style="width: 140px;" placeholder="选择维度">
                                <el-option v-for="m in otherMetrics(metric.name)" :key="m.name" :label="m.name" :value="m.name" />
                              </el-select>
                              <el-select v-model="dep.condition" size="small" style="width: 70px;">
                                <el-option label="=" value="eq" />
                                <el-option label="≠" value="ne" />
                                <el-option label="<" value="lt" />
                                <el-option label=">" value="gt" />
                                <el-option label="≤" value="lte" />
                                <el-option label="≥" value="gte" />
                              </el-select>
                              <el-input-number v-model="dep.value" size="small" :min="0" :max="form.max_score" style="width: 70px;" />
                              <span style="font-size: 12px;">时，本维度得分 =</span>
                              <el-input-number v-model="dep.override_value" size="small" :min="0" :max="form.max_score" style="width: 70px;" />
                              <el-button type="danger" size="small" link :disabled="dialogBusy" @click="removeDependency(metric, depIdx)">
                                <el-icon><Delete /></el-icon>
                              </el-button>
                            </div>
                            <el-button size="small" type="primary" plain :disabled="dialogBusy" @click="addDependency(metric)">
                              <el-icon><Plus /></el-icon> 添加依赖条件
                            </el-button>
                          </el-collapse-item>
                        </el-collapse>
                      </div>
                    </div>
                  </div>
                  <div v-if="form.evaluation_metrics.length === 0" class="metrics-empty">
                    <el-icon><Plus /></el-icon>
                    <span>暂无评测维度</span>
                  </div>
                  <el-button type="primary" plain :disabled="dialogBusy" @click="addMetric" class="add-metric-btn">
                    <el-icon><Plus /></el-icon> 添加评测维度
                  </el-button>
                </div>
              </el-form-item>
            </div>
            <!-- 置灰遮罩：未配置待评测数据时覆盖整区并提示 -->
            <div v-if="form.evaluation_targets.length === 0" class="section-gated-overlay">
              <el-icon :size="22"><Warning /></el-icon>
              <span>请先配置待评测数据</span>
            </div>
          </div>

          <!-- ④ 额外变量 -->
          <el-divider content-position="left">④ 额外变量</el-divider>
          <div style="font-size: 12px; color: #909399; margin-bottom: 8px; background: #fff7e6; padding: 6px 8px; border-radius: 4px;">
            可选配置。变量解析后注入到分析+评分节点，可在评分标准中引用（如预期结果对比）。
          </div>
          <div class="judge-variable-config">
            <div class="config-header">
              <span class="config-title">裁判模型变量配置</span>
              <el-button type="primary" size="small" :disabled="dialogBusy" @click="addJudgeVariable">
                <el-icon><Plus /></el-icon>添加字段
              </el-button>
            </div>
            <el-row class="config-row-title">
              <el-col :span="4">变量名</el-col>
              <el-col :span="5">数据来源</el-col>
              <el-col :span="6">字段映射</el-col>
              <el-col :span="4">必填</el-col>
              <el-col :span="5">操作</el-col>
            </el-row>
            <div class="config-items">
              <el-row :gutter="12" class="variable-row" v-for="(item, index) in form.judge_variable_config" :key="item.key">
                <el-col :span="4">
                  <el-input v-model="item.name" placeholder="变量名" />
                </el-col>
                <el-col :span="5">
                  <el-select v-model="item.source" placeholder="数据来源" style="width: 100%" @change="(val) => handleSourceChange(val, item)">
                    <el-option v-for="opt in sourceOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
                  </el-select>
                </el-col>
                <el-col :span="6" v-if="item.source === 'dataset_field'">
                  <el-select v-model="item.source_config.field" placeholder="字段" style="width: 100%">
                    <el-option v-for="opt in getFieldOptions(item)" :key="opt.value" :label="opt.label" :value="opt.value" />
                  </el-select>
                </el-col>
                <el-col :span="6" v-else-if="item.source === 'chain_result'">
                  <el-popover
                    placement="bottom-start"
                    :width="400"
                    trigger="click"
                  >
                    <template #reference>
                      <el-input
                        :model-value="getSelectedChainVarsText(item)"
                        placeholder="选择请求链变量（支持多选）"
                        readonly
                        style="width: 100%"
                      >
                        <template #suffix>
                          <el-icon><ArrowDown /></el-icon>
                        </template>
                      </el-input>
                    </template>
                    <div class="chain-var-tree">
                      <el-tree
                        v-if="chainRequestTree.length > 0"
                        :data="chainRequestTree"
                        :props="{ children: 'children', label: 'label' }"
                        node-key="value"
                        show-checkbox
                        :default-expand-all="true"
                        @check="(node, checked) => handleChainVarCheck(node, checked, item)"
                        :ref="(el) => setChainTreeRef(item, el)"
                      />
                      <div v-else class="no-chain-msg">
                        <el-icon><Warning /></el-icon>
                        <span>该模型无请求链或请求链无输出变量</span>
                      </div>
                    </div>
                    <div class="chain-var-selected" v-if="getSelectedChainVars(item).length > 0">
                      <el-tag
                        v-for="varName in getSelectedChainVars(item)"
                        :key="varName"
                        closable
                        size="small"
                        @close="removeSelectedChainVar(item, varName)"
                      >
                        {{ varName }}
                      </el-tag>
                    </div>
                  </el-popover>
                </el-col>
                <el-col :span="6" v-else-if="item.source === 'manual'">
                  <el-input v-model="item.source_config.manual_value" placeholder="手动输入值" />
                </el-col>
                <el-col :span="6" v-else>
                  <span class="placeholder-text">-</span>
                </el-col>
                <el-col :span="4">
                  <el-checkbox v-model="item.required" />
                </el-col>
                <el-col :span="5" class="action-col">
                  <el-button type="danger" size="small" :disabled="dialogBusy" @click="removeJudgeVariable(index)">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </el-col>
              </el-row>
            </div>
            <el-empty v-if="form.judge_variable_config.length === 0" description="暂无配置字段，点击添加" :image-size="60" />
          </div>
          <!-- 指令预览 -->
          <el-form-item label="指令预览">
            <el-button type="primary" link :disabled="dialogBusy" :loading="promptPreviewing" @click="previewPromptFunc">
              <el-icon><View /></el-icon> 预览指令内容
            </el-button>
          </el-form-item>
        </template>

        <!-- M1 门禁配置：评测即门禁——执行完成按阈值自动判定，CLI `aievo run --wait` 据此卡发布 -->
        <el-form-item label="门禁配置">
          <div class="gate-config-block">
            <div class="gate-config-head">
              <el-switch v-model="gateEnabledUi" :disabled="dialogBusy" @change="onGateToggle" />
              <span class="gate-config-tip">启用后每次执行完成自动按阈值判定（通过/失败），供 CI 卡发布</span>
            </div>
            <template v-if="gateEnabledUi">
              <el-radio-group v-model="gateMode" size="small" class="gate-mode-group" :disabled="dialogBusy">
                <el-radio-button value="all">全部阈值须通过（all）</el-radio-button>
                <el-radio-button value="any">任一阈值通过即通过（any）</el-radio-button>
              </el-radio-group>
              <el-table :data="gateThresholds" size="small" style="width: 100%">
                <el-table-column label="指标" min-width="220">
                  <template #default="{ row }">
                    <el-select v-model="row.metric" placeholder="选择指标" :disabled="dialogBusy">
                      <el-option v-for="m in gateMetricOptions" :key="m.value" :label="m.label" :value="m.value" />
                    </el-select>
                  </template>
                </el-table-column>
                <el-table-column label="比较" width="100">
                  <template #default="{ row }">
                    <el-select v-model="row.op" :disabled="dialogBusy">
                      <el-option v-for="o in GATE_OPS" :key="o" :label="o" :value="o" />
                    </el-select>
                  </template>
                </el-table-column>
                <el-table-column label="阈值" width="150">
                  <template #default="{ row }">
                    <el-input-number
                      v-model="row.value"
                      :controls="false"
                      :disabled="dialogBusy"
                      placeholder="数值"
                      style="width: 100%"
                    />
                  </template>
                </el-table-column>
                <el-table-column width="70">
                  <template #default="{ $index }">
                    <el-button
                      type="danger"
                      size="small"
                      circle
                      :icon="Delete"
                      :disabled="dialogBusy"
                      @click="gateThresholds.splice($index, 1)"
                    />
                  </template>
                </el-table-column>
              </el-table>
              <el-button
                size="small"
                :icon="Plus"
                class="gate-add-btn"
                :disabled="dialogBusy"
                @click="gateThresholds.push({ metric: 'total_score', op: '>=', value: null })"
              >添加阈值</el-button>
              <div class="gate-config-tip">指标口径：任务均分 / 行通过率（0-1）/ 各维度均分；指标不可用时判 error（CI 按错误处理，不算质量失败）</div>
            </template>
          </div>
        </el-form-item>

        <!-- M2 终态校验器（QE2-1）：确定性终态校验优先——模型输出/被测 agent 执行之后运行，
             独立于裁判计分；聚合 verification_pass_rate 可挂门禁 -->
        <el-form-item label="终态校验器">
          <div class="verifier-config-block">
            <div class="gate-config-tip">
              模型输出之后运行的确定性断言（script / db_assert / file_assert / http_assert），
              证据随结果展示，聚合出「校验通过率」（verification_pass_rate，可挂门禁）；不配置则行为与旧版一致
            </div>
            <el-card
              v-for="(verifier, vIdx) in verifiersUi"
              :key="vIdx"
              shadow="never"
              class="verifier-card"
            >
              <template #header>
                <div class="verifier-card-header">
                  <el-select v-model="verifier.type" size="small" style="width: 150px" :disabled="dialogBusy" @change="onVerifierTypeChange(verifier)">
                    <el-option v-for="t in VERIFIER_TYPES" :key="t.value" :label="t.label" :value="t.value" />
                  </el-select>
                  <el-input v-model="verifier.name" size="small" placeholder="名称（可选）" style="width: 160px" :disabled="dialogBusy" />
                  <el-select v-model="verifier.on_fail" size="small" style="width: 130px" :disabled="dialogBusy">
                    <el-option label="失败继续（continue）" value="continue" />
                    <el-option label="失败中止（abort）" value="abort" />
                  </el-select>
                  <span class="verifier-header-spacer" />
                  <el-button size="small" :loading="verifierDebugRunning" :disabled="dialogBusy" @click="openVerifierDebug(vIdx)">试跑</el-button>
                  <el-button type="danger" size="small" circle :icon="Delete" :disabled="dialogBusy" @click="verifiersUi.splice(vIdx, 1)" />
                </div>
              </template>

              <!-- script：复用前处理脚本库（沙箱通道，stdin 含模型输出与会话 trace） -->
              <template v-if="verifier.type === 'script'">
                <div class="verifier-row">
                  <span class="verifier-label">脚本</span>
                  <el-select v-model="verifier.config.script_id" filterable placeholder="选择脚本库脚本" style="width: 260px" :disabled="dialogBusy">
                    <el-option v-for="script in preJudgeScripts" :key="script.id" :label="script.name" :value="script.id" />
                  </el-select>
                  <el-input-number v-model="verifier.config.timeout_seconds" :min="5" :max="600" placeholder="超时(秒)" controls-position="right" style="width: 120px" :disabled="dialogBusy" />
                  <el-checkbox v-model="verifier.config.network" :disabled="dialogBusy">容器出网</el-checkbox>
                </div>
                <div class="gate-config-tip">复用前处理脚本沙箱通道与标准输出契约（stdin 带 kind=verifier、模型输出与会话摘要）；脚本输出 status=ok/skip 记 pass，fail 记未通过</div>
              </template>

              <!-- db_assert：原生只读 SQL 断言（复用内置 db_assert 只读约束） -->
              <template v-else-if="verifier.type === 'db_assert'">
                <el-table :data="verifier.config.assertions" size="small">
                  <el-table-column label="名称" width="140">
                    <template #default="{ row }"><el-input v-model="row.name" size="small" placeholder="断言名" :disabled="dialogBusy" /></template>
                  </el-table-column>
                  <el-table-column label="SQL（只读 SELECT/WITH）" min-width="260">
                    <template #default="{ row }"><el-input v-model="row.sql" type="textarea" :rows="2" size="small" placeholder="SELECT count(*) AS cnt FROM orders" :disabled="dialogBusy" /></template>
                  </el-table-column>
                  <el-table-column label="期望（JSON）" min-width="200">
                    <template #default="{ row }"><el-input v-model="row.expectedJson" type="textarea" :rows="2" size="small" placeholder='{"scalar": 3}' :disabled="dialogBusy" /></template>
                  </el-table-column>
                  <el-table-column width="60">
                    <template #default="{ $index }">
                      <el-button type="danger" size="small" circle :icon="Delete" :disabled="dialogBusy" @click="verifier.config.assertions.splice($index, 1)" />
                    </template>
                  </el-table-column>
                </el-table>
                <el-button size="small" :icon="Plus" class="gate-add-btn" :disabled="dialogBusy" @click="verifier.config.assertions.push({ name: '', sql: '', expectedJson: DB_ASSERT_EXPECTED_DEFAULT })">添加断言</el-button>
                <div class="gate-config-tip">连接平台配置的只读 DSN（DB_ASSERT_READONLY_DSN），仅允许单条 SELECT/WITH；期望键：row_count / scalar / first_row / contains_row</div>
              </template>

              <!-- file_assert：工作区文件断言（exists/regex/jsonpath） -->
              <template v-else-if="verifier.type === 'file_assert'">
                <el-table :data="verifier.config.assertions" size="small">
                  <el-table-column label="文件路径（工作区相对）" min-width="200">
                    <template #default="{ row }"><el-input v-model="row.path" size="small" placeholder="outputs/result.json" :disabled="dialogBusy" /></template>
                  </el-table-column>
                  <el-table-column label="模式" width="120">
                    <template #default="{ row }">
                      <el-select v-model="row.mode" size="small" :disabled="dialogBusy">
                        <el-option label="存在" value="exists" />
                        <el-option label="正则" value="regex" />
                        <el-option label="JSONPath" value="jsonpath" />
                      </el-select>
                    </template>
                  </el-table-column>
                  <el-table-column label="参数（正则/JSONPath）" min-width="170">
                    <template #default="{ row }">
                      <el-input v-if="row.mode === 'regex'" v-model="row.pattern" size="small" placeholder="正则表达式" :disabled="dialogBusy" />
                      <el-input v-else-if="row.mode === 'jsonpath'" v-model="row.jsonpath" size="small" placeholder="a.b[0].c" :disabled="dialogBusy" />
                      <span v-else>-</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="期望值" min-width="130">
                    <template #default="{ row }">
                      <el-input v-if="row.mode === 'jsonpath'" v-model="row.expected" size="small" placeholder="等于（可空）" :disabled="dialogBusy" />
                      <span v-else>-</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="取反" width="60">
                    <template #default="{ row }"><el-checkbox v-model="row.negate" :disabled="dialogBusy" /></template>
                  </el-table-column>
                  <el-table-column width="60">
                    <template #default="{ $index }">
                      <el-button type="danger" size="small" circle :icon="Delete" :disabled="dialogBusy" @click="verifier.config.assertions.splice($index, 1)" />
                    </template>
                  </el-table-column>
                </el-table>
                <el-button size="small" :icon="Plus" class="gate-add-btn" :disabled="dialogBusy" @click="verifier.config.assertions.push({ path: '', mode: 'exists', pattern: '', jsonpath: '', expected: '', negate: false })">添加断言</el-button>
                <div class="gate-config-tip">对该条数据的工作区（uploads/script_runs/…/）文件断言，含前处理脚本 stage 的执行包副本；JSONPath 为极简点路径子集（a.b[0].c）</div>
              </template>

              <!-- http_assert：回调端点断言（egress 守卫，默认仅公网） -->
              <template v-else-if="verifier.type === 'http_assert'">
                <div class="verifier-row">
                  <el-input v-model="verifier.config.url" size="small" placeholder="回调端点 URL（http/https）" style="width: 300px" :disabled="dialogBusy" />
                  <el-select v-model="verifier.config.method" size="small" style="width: 96px" :disabled="dialogBusy">
                    <el-option v-for="m in ['GET', 'POST', 'PUT', 'PATCH', 'HEAD', 'DELETE']" :key="m" :label="m" :value="m" />
                  </el-select>
                  <el-input-number v-model="verifier.config.expect_status" :min="100" :max="599" controls-position="right" style="width: 110px" :disabled="dialogBusy" />
                  <el-checkbox v-model="verifier.config.allow_private" :disabled="dialogBusy">允许内网目标</el-checkbox>
                  <span v-if="verifier.config.allow_private" style="font-size: 12px; color: #909399;">
                    还需管理员启用 HTTP_ASSERT_ALLOW_PRIVATE_TARGETS 才生效
                  </span>
                </div>
                <div class="verifier-row">
                  <el-input v-model="verifier.config.body_regex" size="small" placeholder="响应体正则（可选）" style="width: 240px" :disabled="dialogBusy" />
                  <el-input v-model="verifier.config.jsonpath" size="small" placeholder="响应 JSONPath（可选，如 status）" style="width: 200px" :disabled="dialogBusy" />
                  <el-input v-model="verifier.config.expected" size="small" placeholder="期望值（可选）" style="width: 140px" :disabled="dialogBusy" />
                </div>
                <div class="gate-config-tip">出网走平台 egress 守卫：默认仅公网目标（内网/环回/DNS 重绑定拒绝）；内网回调端点需显式勾选「允许内网目标」（任务配置可审计）</div>
              </template>
            </el-card>
            <el-button size="small" :icon="Plus" class="gate-add-btn" :disabled="dialogBusy" @click="addVerifier">添加校验器</el-button>
          </div>
        </el-form-item>
      </el-form>

      <!-- 指令预览对话框 -->
      <el-dialog v-model="previewVisible" title="指令内容预览" width="700px">
        <pre class="prompt-preview">{{ previewPromptText }}</pre>
      </el-dialog>

      <!-- M2 校验器试跑对话框（复用脚本试跑通道语义：超管口径） -->
      <el-dialog v-model="verifierDebugVisible" title="校验器试跑" width="640px">
        <el-form label-width="90px">
          <el-form-item label="模型输出">
            <el-input
              v-model="verifierDebugSample"
              type="textarea" :rows="4"
              placeholder="样例模型输出（script 类型校验器进脚本 stdin.model_output；原生类型忽略）"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="verifierDebugVisible = false">关闭</el-button>
          <el-button type="primary" :loading="verifierDebugRunning" @click="runVerifierDebug">试跑一次</el-button>
        </template>
        <el-descriptions v-if="verifierDebugResult" :column="2" border size="small" style="margin-top: 12px">
          <el-descriptions-item label="结果">
            <el-tag :type="verifierStatusTag(verifierDebugResult.result?.status).type" size="small">
              {{ verifierStatusTag(verifierDebugResult.result?.status).label }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="耗时">{{ verifierDebugResult.result?.duration_ms ?? '-' }} ms</el-descriptions-item>
          <el-descriptions-item label="证据" :span="2">
            <pre class="verifier-evidence-preview">{{ JSON.stringify(verifierDebugResult.result?.evidence || {}, null, 2) }}</pre>
          </el-descriptions-item>
          <el-descriptions-item v-if="verifierDebugResult.result?.error" label="错误" :span="2">
            <span style="color: #F56C6C">{{ verifierDebugResult.result.error }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </el-dialog>

      <!-- 保存为模板对话框 -->
      <el-dialog v-model="saveTemplateVisible" :title="isExistingTemplate ? '更新评测模板' : '保存为评测模板'" width="500px" append-to-body>
        <el-form :model="{ templateName: saveTemplateName }" label-width="100px">
          <el-form-item label="模板名称" required>
            <el-input v-model="saveTemplateName" placeholder="请输入模板名称" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button :disabled="saveTemplateLoading" @click="saveTemplateVisible = false">取消</el-button>
          <el-button type="primary" :loading="saveTemplateLoading" @click="confirmSaveTemplate">确定</el-button>
        </template>
      </el-dialog>

      <template #footer>
        <el-button :disabled="submitting || dialogBusy" @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :disabled="dialogBusy" :loading="submitting" @click="submitForm">确定</el-button>
      </template>
    </el-dialog>

    <!-- 执行历史弹窗 -->
    <el-dialog v-model="executionHistoryVisible" title="执行历史记录" width="1100px">
      <div v-if="executionHistoryLoading" style="text-align:center; padding: 40px; color:#909399;">加载中...</div>
      <el-table v-else :data="executionHistoryList" style="width: 100%">
        <el-table-column prop="execution_number" label="执行次数" width="90">
          <template #default="{ row }">
            <el-tag type="info">第 {{ row.execution_number }} 次</el-tag>
          </template>
        </el-table-column>
        <!-- 耗时(h) -->
        <el-table-column label="耗时(h)" width="90" align="right">
          <template #default="{ row }">
            {{ formatHours(row.duration_ms) }}
          </template>
        </el-table-column>
        <!-- 预计耗时(h) -->
        <el-table-column label="预计耗时(h)" width="130" align="right">
          <template #default="{ row }">
            <el-input
              v-model="row.estimated_manual_hours"
              style="width:80px;"
              size="small"
              type="number"
              step="0.1"
              min="0"
              :disabled="manualHoursSavingId === row.id"
              @change="handleManualHoursChange(row)"
            />
          </template>
        </el-table-column>
        <!-- 提效(h) -->
        <el-table-column label="提效(h)" width="100" align="right">
          <template #default="{ row }">
            <span :style="{ color: getEfficiencyColor(row) }">
              {{ formatEfficiency(row) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            {{ row.status === 'completed' ? '成功' : row.status === 'failed' ? '失败' : row.status }}
          </template>
        </el-table-column>
        <!-- M1 门禁判定：未启用门禁的执行显示 - -->
        <el-table-column label="门禁" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.gate_result?.verdict" :type="gateVerdictTagType(row.gate_result.verdict)" size="small">
              {{ gateVerdictLabel(row.gate_result.verdict) }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="175">
          <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="完成时间" width="175">
          <template #default="{ row }">{{ formatTime(row.completed_at) || '-' }}</template>
        </el-table-column>
        <el-table-column prop="total_cost" label="消耗成本" width="90">
          <template #default="{ row }">{{ row.total_cost != null ? row.total_cost.toFixed(4) : '-' }}</template>
        </el-table-column>
        <el-table-column label="错误信息" min-width="120">
          <template #default="{ row }">
            <span v-if="row.error_message" style="color:#F56C6C; font-size:12px;">{{ row.error_message }}</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!executionHistoryLoading && executionHistoryList.length === 0" description="暂无执行记录" />
      <template #footer>
        <el-button :disabled="manualHoursSavingId !== null" @click="executionHistoryVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 任务定时（M5 QE5-3）：cron 编辑器 + 下次运行时间 + 运行历史（一任务一定时） -->
    <el-drawer v-model="scheduleVisible" :title="`定时评测 · ${scheduleTask?.name || ''}`" size="580px">
      <div v-loading="scheduleLoading">
        <el-form label-width="90px">
          <el-form-item label="启用">
            <el-switch v-model="scheduleForm.enabled" :disabled="readonlyRole" />
            <span class="cron-tip" style="margin-left: 12px;">停用后不再扫描触发，配置保留</span>
          </el-form-item>
          <el-form-item label="常用预设">
            <el-select v-model="schedulePreset" style="width: 200px" @change="applySchedulePreset">
              <el-option v-for="p in SCHEDULE_PRESETS" :key="p.label" :label="p.label" :value="p.label" />
            </el-select>
          </el-form-item>
          <el-form-item label="cron 表达式">
            <div class="cron-row">
              <div v-for="(f, i) in CRON_FIELDS" :key="f" class="cron-field">
                <el-input v-model="scheduleForm.cron[i]" size="small" :disabled="readonlyRole" />
                <span class="cron-field-label">{{ f }}</span>
              </div>
            </div>
            <div class="cron-tip">5 段标准 cron（分 时 日 月 周，北京时间），支持 * / , - 与 */n 步进</div>
          </el-form-item>
          <el-form-item label="时段限制">
            <el-time-select v-model="scheduleForm.window_start" start="00:00" step="00:30" end="23:30" placeholder="不限" style="width: 120px" :disabled="readonlyRole" />
            <span style="margin: 0 8px;">至</span>
            <el-time-select v-model="scheduleForm.window_end" start="00:00" step="00:30" end="23:30" placeholder="不限" style="width: 120px" :disabled="readonlyRole" />
            <div class="cron-tip">触发时刻须落在时段内（支持跨零点）；留空=不限</div>
          </el-form-item>
          <el-form-item v-if="scheduleInfo?.next_run_at" label="下次运行">
            <span>{{ formatTime(scheduleInfo.next_run_at) }}</span>
          </el-form-item>
          <el-form-item v-if="scheduleInfo?.last_status" label="最近结果">
            <el-tag :type="scheduleStatusTagType(scheduleInfo.last_status)" size="small">
              {{ scheduleStatusLabel(scheduleInfo.last_status) }}
            </el-tag>
            <span v-if="scheduleInfo?.last_message" class="cron-tip" style="margin-left: 8px;">{{ scheduleInfo.last_message }}</span>
          </el-form-item>
        </el-form>

        <el-divider content-position="left">运行历史（最近 20 次）</el-divider>
        <el-table :data="scheduleInfo?.history || []" size="small">
          <el-table-column label="时间" width="165">
            <template #default="{ row }">{{ formatTime(row.at) }}</template>
          </el-table-column>
          <el-table-column label="结果" width="110">
            <template #default="{ row }">
              <el-tag :type="scheduleStatusTagType(row.status)" size="small">
                {{ scheduleStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="message" label="说明" min-width="140" show-overflow-tooltip />
        </el-table>
        <el-empty v-if="!(scheduleInfo?.history || []).length" description="暂无触发记录" :image-size="60" />
      </div>
      <template #footer>
        <el-button v-if="scheduleInfo" type="danger" :loading="scheduleRemoving" :disabled="readonlyRole || scheduleSaving" @click="removeSchedule">删除定时</el-button>
        <el-button type="primary" :loading="scheduleSaving" :disabled="readonlyRole" @click="saveSchedule">保存</el-button>
      </template>
    </el-drawer>

    <!-- hub_file 输入映射「选择文件」弹窗（阶段B/F4）：样本树点选/手填 glob + 即时匹配预览 + required -->
    <HubFilePatternPicker
      v-model:visible="hubPickerVisible"
      :dataset-id="form?.dataset_id || ''"
      :field-name="hubPickerFieldName"
      :pattern="hubPickerPattern"
      :required="hubPickerRequired"
      @confirm="handleHubPatternConfirm"
    />

    <!-- 插件市场管理弹窗（插件评测「插件市场」来源）：列表/新增/同步/删除 -->
    <el-dialog v-model="marketDialogVisible" title="管理插件市场" width="860px" append-to-body>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="插件市场是一个 Claude Code marketplace git 仓库，添加后克隆到服务端，评测时从其中选择插件或 skill"
        style="margin-bottom: 12px;"
      />
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="marketForm.name" placeholder="如 ANC 插件市场" style="width: 160px" />
        </el-form-item>
        <el-form-item label="Git 地址">
          <el-input v-model="marketForm.git_url" placeholder="https://git.example.com/group/marketplace.git" style="width: 360px" />
        </el-form-item>
        <el-form-item label="分支">
          <el-input v-model="marketForm.branch" placeholder="默认分支可留空" style="width: 130px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="marketCreating" @click="handleCreateMarketplace">添加并克隆</el-button>
        </el-form-item>
      </el-form>
      <el-table :data="marketplaces" v-loading="marketListLoading" size="small" empty-text="暂无插件市场">
        <el-table-column prop="name" label="名称" width="150" />
        <el-table-column prop="git_url" label="Git 地址" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.sync_status === 'ok' ? 'success' : row.sync_status === 'failed' ? 'danger' : 'info'" size="small">
              {{ row.sync_status === 'ok' ? '已同步' : row.sync_status === 'failed' ? '失败' : '未同步' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近同步" width="160">
          <template #default="{ row }">{{ row.last_synced_at ? formatDateTime(row.last_synced_at) : '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :loading="marketSyncingId === row.id" @click="handleSyncMarketplace(row)">同步</el-button>
            <el-button link type="danger" size="small" @click="handleDeleteMarketplace(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="marketSyncError" style="color: #f56c6c; font-size: 12px; margin-top: 8px;">{{ marketSyncError }}</div>
      <template #footer>
        <el-button @click="marketDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, inject, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, View, Delete, ArrowDown, Warning, Rank, UploadFilled } from '@element-plus/icons-vue'
import { taskApi, datasetApi, modelApi, batchApi, instructionApi, efficiencyApi, preJudgeScriptApi, pluginMarketplaceApi } from '@/api'
import { getTemplate, previewPrompt } from '@/api/judgeConfig'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { useTaskWebSocket } from '@/composables/useTaskWebSocket'
import { formatTime } from '@/utils/time'
import { isCustomWeight, normalizeMetricWeight } from '@/utils/weight'
import HubFilePatternPicker from '@/components/HubFilePatternPicker.vue'
import VariableInjector from '@/components/VariableInjector.vue'
import AgentEvaluationConfig from '@/components/AgentEvaluationConfig.vue'
import SafetyEvaluationConfig from '@/components/SafetyEvaluationConfig.vue'
import { canWrite } from '@/utils/permissions'

// M4 QE4-3：viewer 只读——写按钮禁用联动（canWrite 见 utils/permissions）
const currentUser = inject('currentUser', null)
const readonlyRole = computed(() => !canWrite(currentUser.value))
import RefTextarea from '@/components/RefTextarea.vue'

// 裁判变量默认配置（judge 类型）
const DEFAULT_JUDGE_VARIABLE_CONFIG = [
  { key: 'user_input', name: '用户指令', source: 'dataset_field', source_config: { field: '' }, prompt_type: 'user' },
  { key: 'ref_answer', name: '参考答案', source: 'dataset_field', source_config: { field: '' }, prompt_type: 'user' },
  { key: 'model_answer', name: 'AI助手回复', source: 'model_output', source_config: {}, prompt_type: 'user' }
]

// 裁判变量默认配置（RAG 类型）：对应硬编码 prompt 的 {question} 和 {ground_truth} 占位符
const DEFAULT_RAG_JUDGE_VARIABLE_CONFIG = [
  { key: 'question', name: '问题', source: 'dataset_field', source_config: { field: '' }, prompt_type: 'system' },
  { key: 'ground_truth', name: '标准答案', source: 'dataset_field', source_config: { field: '' }, prompt_type: 'system' },
  { key: 'contexts', name: '检索上下文', source: 'model_output', source_config: {}, prompt_type: 'system' }
]

const tasks = ref([])
const datasets = ref([])
const models = ref([])

// ==================== 数据集版本绑定（M5 QE5-1） ====================
const route = useRoute()
const datasetVersions = ref([])           // 当前所选数据集的版本列表（新→旧）
const datasetVersionsLoading = ref(false)
// 绑定模式：latest=跟随最新（默认，存量口径）/ locked=锁定版本（执行用版本快照）
const datasetVersionMode = ref('latest')
// 编辑回显抑制标记：openDialog 回填表单触发 dataset_id watcher 时，
// 不按「用户换数据集」做版本绑定/字段重置（回显后由 taskDetail 值接管）
const hydratingForm = ref(false)

const fetchDatasetVersions = async (datasetId) => {
  if (!datasetId) {
    datasetVersions.value = []
    return
  }
  datasetVersionsLoading.value = true
  try {
    const res = await datasetApi.listVersions(datasetId)
    datasetVersions.value = res || []
  } catch (e) {
    console.error('获取数据集版本列表失败:', e)
    datasetVersions.value = []
  } finally {
    datasetVersionsLoading.value = false
  }
}


// 评测前脚本（Pre-Judge Script）相关状态：脚本以「待评测数据行 source=script_output」编排，
// 任务级 pre_scripts 旧结构不再提供编辑入口（编辑回显时自动转为 script_output 目标行）
const preJudgeScripts = ref([])            // 可用脚本列表（统一落库后的普通记录）

// 旧版配置结构提示：任务 steps / pre_scripts 仍为旧版存储时展示提示条并支持一键迁移
const legacyConfigDetected = ref(false)
const migratingConfig = ref(false)
const currentEditRow = ref(null)           // 当前编辑的表格行（一键迁移后重载表单用）

// 执行历史相关状态
const executionHistoryVisible = ref(false)
const executionHistoryLoading = ref(false)
const executionHistoryList = ref([])
const selectedTasks = ref([])
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref(null)

// 写操作 loading 状态（按钮 loading/禁用反馈 + 防重入）
const submitting = ref(false)               // 任务新建/编辑弹窗提交中
const runningId = ref(null)                 // 正在执行的列表行 id
const stoppingId = ref(null)                // 正在停止的列表行 id
const deletingId = ref(null)                // 正在删除的列表行 id
const batchOpLoading = ref(false)           // 批量执行/停止/删除进行中（批量操作互斥）
const manualHoursSavingId = ref(null)       // 正在保存预计耗时的执行记录 id
const promptPreviewing = ref(false)         // 指令预览请求进行中

// 列表行级写操作互斥信号：任一行 执行/停止/删除 进行中时，操作列其它行按钮一并禁用
const rowOpLoading = computed(() => runningId.value !== null || stoppingId.value !== null || deletingId.value !== null)

const form = ref({
  name: '',
  evaluation_type: 'judge',
  dataset_id: '',
  model_provider: '',
  // 待评测内容来源（仅 AI 裁判员评测使用）：model_call-调用被测模型 / dataset_field-使用数据集字段 / none-不使用模型输出
  model_output_source: 'model_call',
  model_output_field: '',
  // 裁判引擎：claude_agent（Claude Code Agent 引擎）/ deepseek_agent（DeepSeek Agent 引擎）/ graph（传统裁判图引擎）
  judge_engine: 'claude_agent',
  judge_count: 6,
  // 行级多 Trial 次数（M2 QE2-3，judge 类型）：2-5 开启 pass@1/pass^k，1=不开启
  trials: 1,
  // 裁判治理（M2 QE2-4）：false=开启输入消毒（默认）；金丝雀默认关闭
  judge_guard_disabled: false,
  judge_canary: false,
  // 行级并发数（同时处理的数据行数，与裁判数量解耦，origin/main judge-pool）
  concurrency: 4,
  description: '',
  // 评测指令配置（steps/pre_scripts 已废弃：维度顺序即步骤，脚本在待评测数据行内编排，提交时一律发 null）
  template_id: '',
  evaluation_scene: '',
  scene_desc: '',
  max_score: 10,
  score_details: '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格',
  evaluation_metrics: [],
  // 变量注入配置
  runtime_variables: {},
  variable_types: {},
  variable_sources: {},
  variable_mappings: {},
  enable_user_import: false,
  user_import_template: '',
  // 裁判模型变量配置
  judge_variable_config: [
    { key: 'user_input', name: '用户指令', source: 'dataset_field', source_config: { field: 'question' }, prompt_type: 'user', required: true },
    { key: 'ref_answer', name: '参考答案', source: 'dataset_field', source_config: { field: 'ref_answer' }, prompt_type: 'user', required: true },
    { key: 'model_answer', name: 'AI助手回复', source: 'model_output', source_config: {}, prompt_type: 'user', required: true }
  ],
  // 评测目标配置（多评测目标）
  evaluation_targets: [],
  validation_enabled: true,
  // RAG 检索评测配置
  rag_config: {
    rag_endpoint: '',
    rag_api_key: '',
    metrics: ['context_precision', 'context_recall', 'hit_rate', 'mrr'],
    thresholds: {}
  },
  // 一致性评测配置
  consistency_config: {
    repeat_count: 10,
    use_judge: false
  },
  // 耗时评测配置
  latency_config: {
    threshold: 10.0,
    repeat_count: 1,
    score_mode: 'inverse'
  },
  // 二分分类评测配置
  binary_metrics_config: {},
  binary_expected_label: { source: 'dataset_field', source_config: { field: '' } },
  binary_predicted_label: { source: 'model_output', source_config: { field: 'prediction' } },
  binary_positive_value: 'true',
  binary_label_type: 'boolean',
  // Agent 评测配置
  agent_config: {},
  // 安全评测配置
  safety_config: {},
  // 插件评测配置（source 为表单态字段：manual-任务级路径/内联内容，dataset-数据集行字段）
  plugin_config: { depth: 'quick', source: 'manual', plugin_path: '', skill_content: '', market_id: null, market_plugin: '', trace_judgement: 'model', trace_concurrency: null },
})

// 变量注入相关状态
const modelVariables = ref([])
const datasetFields = ref([])
const variableInjectorRef = ref(null)

// Agent/Safety 评测配置组件 ref
const agentConfigRef = ref(null)
const safetyConfigRef = ref(null)

// 请求链相关状态
const chainRequestsCache = ref(new Map())  // 请求链缓存
const currentChainOptions = ref([])         // 当前模型的请求链选项
const chainLoading = ref(false)             // 加载状态

// 评测模板列表
const templates = ref([])

// 指令预览
const previewVisible = ref(false)
const previewPromptText = ref('')

// 保存为模板
const saveTemplateVisible = ref(false)
const saveTemplateName = ref('')
const saveTemplateLoading = ref(false)

// 任务编辑弹窗内任一异步操作进行中：弹窗内其它按钮统一禁用（正在 loading 的那个由 :loading 自禁）
const dialogBusy = computed(() => submitting.value || promptPreviewing.value || saveTemplateLoading.value || migratingConfig.value)

// 待删除的模板ID（仅标记，任务保存时才真正删除）
const pendingDeleteTemplateId = ref(null)

// ---------- M1 门禁配置（表单态：gate_config 落库形状为 {mode, thresholds}，null=未启用） ----------
const GATE_OPS = ['>=', '>', '<=', '<', '==']
const gateEnabledUi = ref(false)
const gateMode = ref('all')
const gateThresholds = ref([])

// 指标口径与后端 gate_service 对齐：total_score=任务均分、pass_rate=行通过率(0-1)、
// dimension:<维度名>=维度均分（维度列表随当前表单的评测维度联动）；
// verification_pass_rate=终态校验器行通过率（M2 QE2-1，配置校验器时可选）、
// trajectory_match_rate=轨迹匹配行通过率（M2 QE2-2，配置轨迹断言时可选）
const gateMetricOptions = computed(() => {
  const opts = [
    { value: 'total_score', label: '任务均分（total_score）' },
    { value: 'pass_rate', label: '行通过率（pass_rate，0-1）' }
  ]
  if ((verifiersUi.value || []).length > 0) {
    opts.push({ value: 'verification_pass_rate', label: '校验通过率（verification_pass_rate，0-1）' })
  }
  const hasTrajectory = (trajectoryUi.value.expected || []).some(e => (e.tool || '').trim())
    || (trajectoryUi.value.forbidden || []).some(e => (e.tool || '').trim())
  if (form.value.evaluation_type === 'agent' && hasTrajectory) {
    opts.push({ value: 'trajectory_match_rate', label: '轨迹匹配率（trajectory_match_rate，0-1）' })
  }
  for (const metric of (form.value.evaluation_metrics || [])) {
    const name = metric && metric.name
    if (name) opts.push({ value: `dimension:${name}`, label: `维度均分：${name}` })
  }
  return opts
})

// 打开弹窗时从任务 gate_config 同步到表单态
const applyGateConfig = (gateConfig) => {
  const cfg = gateConfig && typeof gateConfig === 'object' ? gateConfig : null
  gateEnabledUi.value = !!cfg
  gateMode.value = cfg?.mode === 'any' ? 'any' : 'all'
  gateThresholds.value = (Array.isArray(cfg?.thresholds) ? cfg.thresholds : [])
    .filter(t => t && t.metric)
    .map(t => ({ metric: t.metric, op: GATE_OPS.includes(t.op) ? t.op : '>=', value: t.value ?? null }))
}

// 开关切换：首次启用给一行默认阈值
const onGateToggle = (enabled) => {
  if (enabled && gateThresholds.value.length === 0) {
    gateThresholds.value = [{ metric: 'total_score', op: '>=', value: null }]
  }
}

// 提交前收集：过滤无效行（指标/数值缺失）；有效行全空 = 未启用（落 null）
const collectGateConfig = () => {
  if (!gateEnabledUi.value) return null
  const thresholds = gateThresholds.value
    .filter(t => t.metric && t.value !== null && t.value !== '' && !Number.isNaN(Number(t.value)))
    .map(t => ({ metric: t.metric, op: t.op || '>=', value: Number(t.value) }))
  if (thresholds.length === 0) return null
  return { mode: gateMode.value === 'any' ? 'any' : 'all', thresholds }
}

// 门禁 verdict 展示口径（执行历史列表与结果页横幅共用语义）
const gateVerdictLabel = (verdict) => ({ passed: '通过', failed: '未通过', error: '错误' }[verdict] || verdict)
const gateVerdictTagType = (verdict) => ({ passed: 'success', failed: 'danger', error: 'warning' }[verdict] || 'info')

// ---------- M2 终态校验器（表单态：verifiers 落库形状为 [{type, name, on_fail, config}]，null=未配置） ----------
const VERIFIER_TYPES = [
  { value: 'script', label: 'script（脚本）' },
  { value: 'db_assert', label: 'db_assert（数据库）' },
  { value: 'file_assert', label: 'file_assert（文件）' },
  { value: 'http_assert', label: 'http_assert（回调）' }
]
// db_assert 期望断言的默认 JSON 模板（模板表达式内联字符串转义易错，收敛为常量）
const DB_ASSERT_EXPECTED_DEFAULT = '{"scalar": 1}'
const verifiersUi = ref([])

// 各类型的默认 config（切换类型时重置）
const _verifierDefaultConfig = (type) => {
  if (type === 'script') return { script_id: '', timeout_seconds: null, network: false }
  if (type === 'db_assert') return { assertions: [{ name: '', sql: '', expectedJson: DB_ASSERT_EXPECTED_DEFAULT }] }
  if (type === 'file_assert') return { assertions: [{ path: '', mode: 'exists', pattern: '', jsonpath: '', expected: '', negate: false }] }
  return { url: '', method: 'GET', expect_status: 200, allow_private: false, body_regex: '', jsonpath: '', expected: '' }
}

const addVerifier = () => {
  verifiersUi.value.push({ type: 'file_assert', name: '', on_fail: 'continue', config: _verifierDefaultConfig('file_assert') })
}

const onVerifierTypeChange = (verifier) => {
  verifier.config = _verifierDefaultConfig(verifier.type)
}

// 打开弹窗时从任务 verifiers 同步到表单态（db_assert 的 expected JSON 编辑态拆为字符串）
const applyVerifiers = (verifiers) => {
  const list = Array.isArray(verifiers) ? verifiers : []
  verifiersUi.value = list
    .filter(v => v && VERIFIER_TYPES.some(t => t.value === v.type))
    .map(v => {
      const config = { ...(v.config || {}) }
      if (v.type === 'db_assert') {
        config.assertions = (Array.isArray(config.assertions) ? config.assertions : []).map(a => ({
          name: a?.name || '',
          sql: a?.sql || '',
          expectedJson: a?.expected != null ? JSON.stringify(a.expected) : '{"scalar": 1}'
        }))
      }
      return { type: v.type, name: v.name || '', on_fail: v.on_fail === 'abort' ? 'abort' : 'continue', config }
    })
}

// 提交前收集：过滤无效行（缺脚本/断言/URL）；db_assert expectedJson 解析回对象；
// 有效行全空 = 未配置（落 null，清空校验器）
const _collectVerifierList = (list) => {
  const collected = []
  for (const verifier of (list || [])) {
    if (!VERIFIER_TYPES.some(t => t.value === verifier.type)) continue
    const config = verifier.config || {}
    if (verifier.type === 'script') {
      if (!config.script_id) continue
      const scriptConfig = { script_id: config.script_id }
      if (config.timeout_seconds) scriptConfig.timeout_seconds = config.timeout_seconds
      if (config.network) scriptConfig.network = true
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), on_fail: verifier.on_fail, config: scriptConfig })
      continue
    }
    if (verifier.type === 'db_assert') {
      const assertions = (config.assertions || [])
        .filter(a => a && (a.sql || '').trim())
        .map(a => {
          let expected
          try { expected = JSON.parse(a.expectedJson || '{}') } catch (e) { expected = { scalar: null } }
          return { name: (a.name || '').trim(), sql: a.sql, expected }
        })
      if (assertions.length === 0) continue
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), on_fail: verifier.on_fail, config: { assertions } })
      continue
    }
    if (verifier.type === 'file_assert') {
      const assertions = (config.assertions || [])
        .filter(a => a && (a.path || '').trim())
        .map(a => ({
          name: (a.name || '').trim(), path: a.path.trim(), mode: a.mode || 'exists',
          pattern: (a.pattern || '').trim(), jsonpath: (a.jsonpath || '').trim(),
          expected: a.expected === '' || a.expected == null ? null : a.expected,
          negate: !!a.negate
        }))
      if (assertions.length === 0) continue
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), on_fail: verifier.on_fail, config: { assertions } })
      continue
    }
    if (verifier.type === 'http_assert') {
      const url = (config.url || '').trim()
      if (!url) continue
      const httpConfig = {
        url, method: config.method || 'GET',
        expect_status: config.expect_status ?? 200,
        allow_private: !!config.allow_private
      }
      if ((config.body_regex || '').trim()) httpConfig.body_regex = config.body_regex.trim()
      if ((config.jsonpath || '').trim()) {
        httpConfig.jsonpath = config.jsonpath.trim()
        if ((config.expected || '') !== '') httpConfig.expected = config.expected
      }
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), on_fail: verifier.on_fail, config: httpConfig })
      continue
    }
    collected.push({ type: verifier.type, name: (verifier.name || '').trim(), on_fail: verifier.on_fail, config })
  }
  return collected
}

const collectVerifiers = () => {
  const collected = _collectVerifierList(verifiersUi.value)
  return collected.length > 0 ? collected : null
}

// 校验器试跑（复用脚本试跑通道语义：超管口径，POST /evaluation/tasks/verifiers/debug）
const verifierDebugVisible = ref(false)
const verifierDebugIndex = ref(-1)
const verifierDebugRunning = ref(false)
const verifierDebugSample = ref('')
const verifierDebugResult = ref(null)

const openVerifierDebug = (index) => {
  const verifier = verifiersUi.value[index]
  if (!verifier) return
  const payload = collectSingleVerifier(verifier)
  if (!payload) {
    ElMessage.warning('请先补全校验器必填项（脚本/断言/URL）再试跑')
    return
  }
  verifierDebugIndex.value = index
  verifierDebugSample.value = ''
  verifierDebugResult.value = null
  verifierDebugVisible.value = true
}

// 收集单个校验器（与 collectVerifiers 同构，供试跑）
const collectSingleVerifier = (verifier) => {
  const collected = _collectVerifierList([verifier])
  return collected.length > 0 ? collected[0] : null
}

const runVerifierDebug = async () => {
  const verifier = verifiersUi.value[verifierDebugIndex.value]
  if (!verifier) return
  const payload = collectSingleVerifier(verifier)
  if (!payload) {
    ElMessage.warning('请先补全校验器必填项再试跑')
    return
  }
  verifierDebugRunning.value = true
  try {
    const res = await taskApi.debugVerifier({
      verifier: payload,
      sample: { model_output: verifierDebugSample.value }
    })
    verifierDebugResult.value = res
  } catch (e) {
    ElMessage.error(e.message || '试跑失败')
  } finally {
    verifierDebugRunning.value = false
  }
}

// 校验器状态展示口径（pass/fail/error，与后端 verifier_runner 一致）
const verifierStatusTag = (status) => {
  const map = {
    pass: { type: 'success', label: '通过' },
    fail: { type: 'danger', label: '未通过' },
    error: { type: 'warning', label: '错误' }
  }
  return map[status] || { type: 'info', label: status || '-' }
}

// ---------- M2 轨迹断言（agent 类型表单态：trajectory_config 落库形状为
// {mode, expected[], forbidden[]}，null=未配置） ----------
const TRAJECTORY_MODES = ['strict', 'unordered', 'subset', 'superset']
const trajectoryUi = ref({ mode: 'subset', expected: [], forbidden: [] })

// 编辑态字符串 ↔ 落库数组：args_contains 编辑框为逗号分隔字符串
const _splitArgsStr = (str) => String(str || '').split(/[,，]/).map(s => s.trim()).filter(Boolean)
const _joinArgsList = (list) => Array.isArray(list) ? list.join(', ') : ''

// 打开弹窗时从任务 trajectory_config 同步表单态
const applyTrajectoryConfig = (config) => {
  const cfg = config && typeof config === 'object' ? config : null
  trajectoryUi.value = {
    mode: TRAJECTORY_MODES.includes(cfg?.mode) ? cfg.mode : 'subset',
    expected: (Array.isArray(cfg?.expected) ? cfg.expected : [])
      .map(e => ({
        tool: e?.tool || '',
        argsContainsStr: _joinArgsList(e?.args_contains),
        min_calls: Number.isInteger(e?.min_calls) && e.min_calls >= 1 ? e.min_calls : 1
      })),
    forbidden: (Array.isArray(cfg?.forbidden) ? cfg.forbidden : [])
      .map(e => ({ tool: e?.tool || '', argsContainsStr: _joinArgsList(e?.args_contains) }))
  }
}

// 提交前收集：过滤空 tool 行；期望/禁用全空 = 未配置（落 null）
const collectTrajectoryConfig = () => {
  const expected = trajectoryUi.value.expected
    .filter(e => (e.tool || '').trim())
    .map(e => {
      const entry = { tool: e.tool.trim(), min_calls: e.min_calls || 1 }
      const args = _splitArgsStr(e.argsContainsStr)
      if (args.length > 0) entry.args_contains = args
      return entry
    })
  const forbidden = trajectoryUi.value.forbidden
    .filter(e => (e.tool || '').trim())
    .map(e => {
      const entry = { tool: e.tool.trim() }
      const args = _splitArgsStr(e.argsContainsStr)
      if (args.length > 0) entry.args_contains = args
      return entry
    })
  if (expected.length === 0 && forbidden.length === 0) return null
  return { mode: TRAJECTORY_MODES.includes(trajectoryUi.value.mode) ? trajectoryUi.value.mode : 'subset', expected, forbidden }
}

// 是否选择了已有模板（用于动态切换按钮文字）
const isExistingTemplate = computed(() => {
  if (!form.value.template_id) return false
  const template = templates.value.find(t => t.id === form.value.template_id)
  return !!template
})

const rules = {
  name: [
    { required: true, message: '请输入任务名称', trigger: 'blur' },
    { max: 100, message: '任务名称不能超过100个字符', trigger: 'blur' }
  ],
  evaluation_type: [{ required: true, message: '请选择评测类型', trigger: 'change' }]
}

// 裁判引擎提示文案（三选一：claude_agent / deepseek_agent / graph）
const JUDGE_ENGINE_HINTS = {
  claude_agent: '使用 Claude Code Agent 裁判（超长会话自动用工具阅读）；新建任务默认引擎',
  deepseek_agent: '使用 DeepSeek Agent 裁判（deepseek-harness-sdk）',
  graph: '使用传统裁判图（HTTP 直调，兼容存量任务；历史任务无引擎值时按此处理）'
}
const judgeEngineHint = computed(() => JUDGE_ENGINE_HINTS[form.value.judge_engine] || JUDGE_ENGINE_HINTS.graph)

// 判断是否显示 4 段式评测配置（评测背景/待评测数据/评分标准与评测维度/额外变量）：
// AI 裁判员评测（judge）、长会话 Agent 评测（agent）和一致性评测启用裁判时（consistency + use_judge）显示
const showJudgeAndConsistencySections = computed(() => {
  return form.value.evaluation_type === 'judge' ||
    form.value.evaluation_type === 'agent' ||
    (form.value.evaluation_type === 'consistency' && form.value.consistency_config?.use_judge)
})

// @ 引用标记可选列表：待评测数据行（id/display_name）与裁判变量（key/name），供 RefTextarea 下拉引用
const refMarkTargets = computed(() =>
  (form.value.evaluation_targets || [])
    .filter(t => t && t.id !== undefined && t.id !== null)
    .map(t => ({ id: t.id, display_name: t.display_name || t.name || String(t.id) }))
)
const refMarkVars = computed(() =>
  (form.value.judge_variable_config || [])
    .filter(v => v && v.key)
    .map(v => ({ key: v.key, name: v.name || v.key }))
)

// 评分等级范围重叠校验
const scoreCriteriaOverlap = computed(() => {
  const overlaps = {}
  for (const metric of (form.value.evaluation_metrics || [])) {
    const sc = metric.score_criteria || {}
    const entries = Object.entries(sc)
    if (entries.length < 2) continue
    const metricOverlaps = []
    for (let i = 0; i < entries.length; i++) {
      for (let j = i + 1; j < entries.length; j++) {
        const [nameA, infoA] = entries[i]
        const [nameB, infoB] = entries[j]
        const minA = parseFloat(infoA.min) || 0
        const maxA = parseFloat(infoA.max) || 0
        const minB = parseFloat(infoB.min) || 0
        const maxB = parseFloat(infoB.max) || 0
        // Check overlap: ranges overlap when one's min is less than other's max
        if (minA < maxB && minB < maxA) {
          metricOverlaps.push(`${nameA}(${minA}-${maxA}) × ${nameB}(${minB}-${maxB})`)
        }
      }
    }
    if (metricOverlaps.length > 0) {
      overlaps[metric.name || metric.id] = metricOverlaps
    }
  }
  return overlaps
})

// 获取其他维度（用于依赖关系选择）
const otherMetrics = computed(() => {
  return (currentName) => {
    return (form.value.evaluation_metrics || []).filter(m => m.name && m.name !== currentName)
  }
})

const getTypeName = (type) => {
  const map = { judge: 'AI裁判员', consistency: '一致性', success_rate: '成功率', rag: 'RAG检索', latency: '响应耗时', binary_classification: '二分分类', agent: 'Agent评测', plugin: '插件评测', safety: '安全评测', dialogue: '对话评测' }
  return map[type] || type
}

const ragMetricNames = {
  context_precision: '上下文精度',
  context_recall: '上下文召回',
  hit_rate: '命中率',
  mrr: '平均倒数排名',
  noise_sensitivity: '抗噪声能力',
  negative_rejection: '拒绝能力',
}

const getStatusType = (status) => {
  const map = { pending: 'info', running: 'warning', completed: 'success', failed: 'danger', stopped: 'info' }
  return map[status] || 'info'
}

const loadTasks = async () => {
  try {
    const res = await taskApi.list({ limit: 100 })
    tasks.value = res || []

    // 对所有 running 状态的任务开始轮询和 WebSocket
    tasks.value.filter(t => t.status === 'running').forEach(t => {
      startPolling(t.id)
      startWebSocket(t.id)
    })
  } catch (e) {
    ElMessage.error('加载失败')
  }
}

// 轮询配置
const { startPolling, stopPolling, stopAll } = useTaskPolling({
  interval: 8000,
  onTaskUpdate: (taskId, updatedTask) => {
    const idx = tasks.value.findIndex(t => t.id === taskId)
    if (idx !== -1) {
      // 使用 splice 替换以触发 Vue 响应式更新
      tasks.value.splice(idx, 1, { ...tasks.value[idx], ...updatedTask })
    }
  },
  onTaskComplete: (taskId, task) => {
    // 轻端点载荷不含任务名，从当前列表行补齐
    const row = tasks.value.find(t => t.id === taskId) || task
    ElMessage.success(`任务 "${row.name}" 已完成`)
  },
  onTaskFailed: (taskId, task) => {
    const row = tasks.value.find(t => t.id === taskId) || task
    ElMessage.error(`任务 "${row.name}" 执行失败`)
  },
  onPollingError: (taskId, error) => {
    // W14：轮询连续失败已自动降频重试（不再永久停摆），此处给出用户提示
    console.error(`任务 ${taskId} 轮询失败:`, error)
    ElMessage.warning(`任务 ${taskId} 状态轮询暂时不可用，已降低频率继续重试`)
  }
})

// WebSocket 配置
const { startWebSocket, stopWebSocket, stopAll: stopAllWebSocket } = useTaskWebSocket({
  onStatusUpdate: (taskId, data) => {
    const idx = tasks.value.findIndex(t => t.id === taskId)
    if (idx !== -1) {
      // 使用 splice 替换以触发 Vue 响应式更新
      tasks.value.splice(idx, 1, { ...tasks.value[idx], ...data })
    }
  },
  onConnect: (taskId) => {
    console.log(`WebSocket connected: ${taskId}`)
    // W14：WS 通道就绪即停该任务的轮询，消除「WS + 轮询」双通道并存；
    // WS 断开重连耗尽后由 onFallback 恢复轮询
    stopPolling(taskId)
  },
  onDisconnect: (taskId) => {
    console.log(`WebSocket disconnected: ${taskId}`)
  },
  onError: (taskId, error) => {
    console.error(`WebSocket error: ${taskId}`, error)
  },
  onFallback: (taskId) => {
    // WS 重连 3 次失败：降级回轮询
    startPolling(taskId)
  }
})

const loadOptions = async () => {
  try {
    const [dsRes, mRes, tplRes, scriptRes] = await Promise.all([
      datasetApi.list({ limit: 100 }),
      modelApi.list({ limit: 100, model_role: 'evaluation' }),
      instructionApi.list({ limit: 100 }),
      preJudgeScriptApi.list()
    ])
    datasets.value = dsRes || []
    models.value = mRes || []
    templates.value = tplRes || []
    // 脚本列表返回 {total, items}
    preJudgeScripts.value = scriptRes?.items || []
  } catch (e) {
    console.error(e)
  }
}

// 获取数据集字段
const fetchDatasetFields = async (datasetId) => {
  if (!datasetId) {
    datasetFields.value = []
    return
  }
  try {
    const res = await taskApi.getDatasetFields(datasetId)
    // axios 响应中，后端数据在 res.data 中
    const data = res?.data || res
    datasetFields.value = data?.fields || []
  } catch (e) {
    console.error('获取数据集字段失败:', e)
    datasetFields.value = []
  }
}

// 监听模型选择变化
watch(() => form.value?.model_provider, () => {
  loadModelVariables()
  // 清空请求链选项（模型变化后，原选择无效）
  currentChainOptions.value = []
  if (form.value?.judge_variable_config) {
    form.value.judge_variable_config.forEach(item => {
      if (item.source === 'chain_result' && item.source_config) {
        item.source_config.chain_index = undefined
      }
    })
  }
})

// 监听数据集选择变化
watch(() => form.value.dataset_id, async (newDatasetId, oldDatasetId) => {
  if (newDatasetId !== oldDatasetId) {
    // 只有当 oldDatasetId 有值时才清空（避免初始化时清空已有的配置）；
    // 编辑回显（hydratingForm）由 taskDetail 的绑定值接管，不走重置
    if (oldDatasetId && !hydratingForm.value) {
      datasetFields.value = []
      // 清空所有已选的 field 值
      form.value?.judge_variable_config?.forEach(item => {
        if (item.source === 'dataset_field' && item.source_config) {
          item.source_config.field = ''
        }
      })
      form.value?.evaluation_targets?.forEach(target => {
        if (target.source === 'dataset_field' && target.source_config) {
          target.source_config.field = ''
        }
      })
      // 换数据集后旧版本号必然失效：重置为跟随最新
      form.value.dataset_version_no = null
      datasetVersionMode.value = 'latest'
    }
    await fetchDatasetFields(newDatasetId)
    await fetchDatasetVersions(newDatasetId)
    // 已选的模型输出字段若不在新数据集中，清空
    if (form.value.model_output_field) {
      const availableFields = modelOutputFieldOptions.value.map(opt => opt.value)
      if (!availableFields.includes(form.value.model_output_field)) {
        form.value.model_output_field = ''
      }
    }
  }
})

// 版本绑定模式切换（M5 QE5-1）：跟随最新清空锁定号；锁定时未选则默认取最新版
watch(datasetVersionMode, (mode) => {
  if (mode === 'latest') {
    form.value.dataset_version_no = null
  } else if (form.value.dataset_version_no == null && datasetVersions.value.length > 0) {
    form.value.dataset_version_no = datasetVersions.value[0].version_no
  }
})

// 评测维度操作
const addMetric = () => {
  form.value.evaluation_metrics.push({
    id: `metric_${Date.now()}`,
    name: '',
    weight: 'medium',
    max_score: form.value.max_score || 10,
    description: '',
    score_criteria: {
      '优秀': { min: 9, max: 10, desc: '' },
      '良好': { min: 7, max: 8, desc: '' },
      '及格': { min: 5, max: 6, desc: '' },
      '不及格': { min: 0, max: 4, desc: '' }
    },
    dependencies: []
  })
}

const removeMetric = (index) => {
  form.value.evaluation_metrics.splice(index, 1)
}

// 评测维度拖拽排序
const metricDragState = ref({
  dragging: false,
  fromIndex: null
})

const handleMetricDragStart = (event, index) => {
  metricDragState.value.dragging = true
  metricDragState.value.fromIndex = index
  event.dataTransfer.effectAllowed = 'move'
}

const handleMetricDragOver = (event, index) => {
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
}

const handleMetricDrop = (event, toIndex) => {
  event.preventDefault()
  if (metricDragState.value.fromIndex !== null && metricDragState.value.fromIndex !== toIndex) {
    const item = form.value.evaluation_metrics.splice(metricDragState.value.fromIndex, 1)[0]
    form.value.evaluation_metrics.splice(toIndex, 0, item)
  }
  metricDragState.value.dragging = false
  metricDragState.value.fromIndex = null
}

const handleMetricDragEnd = () => {
  metricDragState.value.dragging = false
  metricDragState.value.fromIndex = null
}

// 旧版评分步骤展示归一化：steps 非空时拼进 score_details 并注明（保存后合并固化，提交时 steps 一律发 null）
const appendLegacySteps = (baseScoreDetails, steps) => {
  const list = (Array.isArray(steps) ? steps : []).map(s => String(s || '').trim()).filter(Boolean)
  if (!list.length) return baseScoreDetails
  const base = baseScoreDetails || ''
  const sep = base.trim() ? '\n\n' : ''
  return `${base}${sep}旧版步骤（保存后合并）：\n${list.map((s, i) => `${i + 1}. ${s}`).join('\n')}`
}

// 变量注入更新
const handleVariableUpdate = (data) => {
  form.value.runtime_variables = data.runtime_variables || {}
  form.value.variable_types = data.variable_types || {}
  form.value.variable_sources = data.variable_sources || {}
  form.value.variable_mappings = data.variable_mappings || {}
  form.value.enable_user_import = data.enable_user_import || false
  form.value.user_import_template = data.user_import_template || ''
}

// 模板选择变化时自动填充评测指令配置
const handleTemplateChange = (templateId) => {
  if (!templateId) {
    // 清空评测指令配置
    form.value.evaluation_scene = ''
    form.value.scene_desc = ''
    form.value.max_score = 10
    form.value.score_details = '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格'
    form.value.evaluation_metrics = []
    form.value.judge_variable_config = []
    return
  }

  // 检查是选择已有模板还是输入的新模板名
  const template = templates.value.find(t => t.id === templateId)

  if (template) {
    // 选择了已有模板，填充配置
    form.value.evaluation_scene = template.scene || ''
    form.value.scene_desc = template.scene_description || ''
    form.value.judge_prompt = template.judge_prompt || ''
    form.value.max_score = template.max_score || 10
    // 旧版模板 steps 展示归一化：拼进 score_details（保存后合并固化）
    form.value.score_details = appendLegacySteps(
      template.score_details || '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格',
      template.steps
    )
    // 转换评测维度，添加唯一id
    form.value.evaluation_metrics = (template.evaluation_metrics || []).map((m, i) => ({
      id: `metric_${Date.now()}_${i}`,
      name: m.name || '',
      weight: m.weight || 'medium',
      max_score: m.max_score || template.max_score || 10,
      description: m.description || '',
      score_criteria: m.score_criteria || {},
      dependencies: m.dependencies || []
    }))
    // 加载裁判变量配置
    form.value.judge_variable_config = template.judge_variable_config || []
  } else {
    // 用户输入的新模板名，清空配置让用户填写
    form.value.evaluation_scene = ''
    form.value.scene_desc = ''
    form.value.max_score = 10
    form.value.score_details = '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格'
    form.value.evaluation_metrics = []
    form.value.judge_variable_config = []
  }
}

// 清空当前模板配置（仅清空配置，不删除模板）
const clearTemplateConfig = () => {
  form.value.template_id = ''
  handleTemplateChange('')
}

// 删除当前选中的模板（仅标记，任务保存时才真正删除）
const deleteTemplate = async () => {
  if (!form.value.template_id) return

  try {
    await ElMessageBox.confirm('确定要删除该评测模板吗？删除后任务将解除模板关联。', '提示', { type: 'warning' })
    // 仅标记待删除，不真正删除
    pendingDeleteTemplateId.value = form.value.template_id
    // 清空任务上的模板关联（前端显示）
    form.value.template_id = ''
    handleTemplateChange('')
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error(e.message || '操作失败')
    }
  }
}

// 监听最大分值变化，同步更新所有维度的max_score
watch(() => form.value.max_score, (newVal) => {
  form.value.evaluation_metrics.forEach(m => {
    if (m.max_score > newVal) {
      m.max_score = newVal
    }
  })
})

// 预览指令内容
const previewPromptFunc = async () => {
  if (promptPreviewing.value) return
  promptPreviewing.value = true
  try {
    // 获取默认模板
    const templateRes = await getTemplate()
    const defaultTemplates = templateRes.data || templateRes

    // 构建预览请求数据
    const targets = form.value.evaluation_targets.map(t => ({
      id: t.id,
      name: t.name || t.display_name,
      display_name: t.display_name,
      source: t.source,
      source_config: t.source_config || {},
      required: t.required !== false
    }))

    // 构建请求数据
    const previewData = {
      system_prompt_template: defaultTemplates.system_prompt_template,
      user_prompt_template: defaultTemplates.user_prompt_template,
      targets: targets,
      dataset_item: {},
      model_result: {
        content: '示例模型输出'
      },
      // PromptBuilder 所需配置字段（steps 已废弃：后端从 evaluation_metrics 派生评分步骤，不再发送）
      scene_desc: form.value.scene_desc || '',
      evaluation_metrics: form.value.evaluation_metrics || [],
      max_score: form.value.max_score || 10,
      score_details: form.value.score_details || '',
      judge_variable_config: form.value.judge_variable_config || {}
    }

    // 调用预览API
    const res = await previewPrompt(previewData)
    const data = res.data || res

    // 显示渲染后的提示词
    previewPromptText.value = `【System Prompt】\n${data.rendered_system_prompt}\n\n【User Prompt】\n${data.rendered_user_prompt}`
    previewVisible.value = true
  } catch (e) {
    console.error('预览失败:', e)
    ElMessage.error('预览失败: ' + (e.message || '未知错误'))
  } finally {
    promptPreviewing.value = false
  }
}

// 保存为模板
const saveAsTemplate = () => {
  if (isExistingTemplate.value) {
    // 选择了已有模板，填充该模板名称
    const template = templates.value.find(t => t.id === form.value.template_id)
    saveTemplateName.value = template ? template.name : ''
  } else {
    saveTemplateName.value = ''
  }
  saveTemplateVisible.value = true
}

const confirmSaveTemplate = async () => {
  if (!saveTemplateName.value || !saveTemplateName.value.trim()) {
    ElMessage.warning('请输入模板名称')
    return
  }

  saveTemplateLoading.value = true
  try {
    const templateData = {
      name: saveTemplateName.value.trim(),
      scene: form.value.evaluation_scene || '',
      scene_description: form.value.scene_desc || '',
      judge_prompt: form.value.judge_prompt || '',
      max_score: form.value.max_score || 10,
      score_details: form.value.score_details || '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格',
      evaluation_metrics: form.value.evaluation_metrics || [],
      // steps 已废弃：维度顺序即步骤，模板不再携带 steps（与迁移后结构一致）
      steps: null,
      judge_variable_config: form.value.judge_variable_config || []
    }

    // 如果当前已选择了一个已有模板，直接用该模板ID更新
    if (isExistingTemplate.value) {
      const currentTemplateId = form.value.template_id
      await instructionApi.update(currentTemplateId, templateData)
      ElMessage.success('模板更新成功')
    } else {
      // 没有选择已有模板，检查输入的名称是否已存在
      const existingTemplate = templates.value.find(t => t.name === saveTemplateName.value.trim())

      if (existingTemplate) {
        // 存在则更新
        await instructionApi.update(existingTemplate.id, templateData)
        ElMessage.success('模板更新成功')
        form.value.template_id = existingTemplate.id
      } else {
        // 不存在则创建
        const created = await instructionApi.create(templateData)
        ElMessage.success('模板创建成功')
        // 选中新创建的模板
        form.value.template_id = created.id
      }
    }

    // 刷新模板列表
    const res = await instructionApi.list({ limit: 100 })
    templates.value = res || []

    // 选中新创建或更新的模板
    const newTemplate = templates.value.find(t => t.name === saveTemplateName.value.trim())
    if (newTemplate) {
      form.value.template_id = newTemplate.id
      handleTemplateChange(newTemplate.id)
    }

    saveTemplateVisible.value = false
  } catch (e) {
    ElMessage.error(e.message || '保存模板失败')
  } finally {
    saveTemplateLoading.value = false
  }
}

// ==================== 评测前脚本编排（待评测数据行 source=script_output） ====================

// 可用脚本定义索引（id → 脚本定义；附带 builtin:<name> 历史引用别名做展示兜底）
const preScriptDefMap = computed(() => {
  const map = {}
  for (const s of preJudgeScripts.value) {
    map[s.id] = s
    map[`builtin:${s.name}`] = s
  }
  return map
})

// 脚本引用归一化：builtin:<name> 历史引用（脚本统一落库前）按名称改写为脚本记录 id；
// 解析不到时原样返回（由「脚本定义未加载」提示与提交校验兜底）
const normalizeScriptRef = (scriptId) => {
  if (!scriptId || typeof scriptId !== 'string') return scriptId
  const direct = preScriptDefMap.value[scriptId]
  if (direct) return direct.id
  const name = scriptId.startsWith('builtin:') ? scriptId.slice('builtin:'.length) : scriptId
  const byName = preJudgeScripts.value.find(s => s.name === name)
  return byName ? byName.id : scriptId
}

const preScriptName = (scriptId) => preScriptDefMap.value[scriptId]?.name || scriptId
const preScriptDescription = (scriptId) => preScriptDefMap.value[scriptId]?.description || ''

// 脚本输出行当前脚本名（待评测数据表「字段映射」列展示用）
const targetScriptName = (target) => preScriptName(target?.source_config?.script_id || '')

// output_schema 值 → 下拉展示文本：叶子串原样；嵌套结构展平子键路径做摘要；其余对象 JSON 兜底
const summarizeOutputValue = (value, depth = 0) => {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object' && value.children && typeof value.children === 'object' && depth < 4) {
    const paths = []
    const walk = (children, prefix) => {
      for (const [k, v] of Object.entries(children)) {
        if (paths.length >= 6) return
        const p = prefix ? `${prefix}.${k}` : k
        paths.push(p)
        if (v && typeof v === 'object' && v.children && typeof v.children === 'object') walk(v.children, p)
      }
    }
    walk(value.children, '')
    const head = [value.type, value.description].filter(Boolean).join('：')
    const more = paths.length >= 6 ? ' 等' : ''
    return `${head}（子键: ${paths.join('、')}${more}）`
  }
  return JSON.stringify(value)
}

// 脚本输出键候选（output_schema：输出键 → 说明），供「输出键」下拉选择；
// 顶层键即运行时取用的真实输出键（嵌套细节只在摘要里展示）
const targetScriptOutputRows = (target) => {
  const schema = preScriptDefMap.value[target?.source_config?.script_id]?.output_schema || {}
  return Object.entries(schema).map(([key, desc]) => ({
    key,
    desc: summarizeOutputValue(desc)
  }))
}

// 脚本输入映射行：合并「声明字段（input_fields）」与「存量映射中未声明的字段」；
// 声明字段缺失映射时补默认项（value_type 以脚本声明为准）
const targetScriptFieldRows = (target) => {
  const cfg = target?.source_config
  if (!cfg) return []
  if (!cfg.input_mapping || typeof cfg.input_mapping !== 'object') {
    cfg.input_mapping = {}
  }
  const declared = preScriptDefMap.value[cfg.script_id]?.input_fields || {}
  const rows = []
  for (const [name, meta] of Object.entries(declared)) {
    if (!cfg.input_mapping[name] || typeof cfg.input_mapping[name] !== 'object') {
      const legacy = cfg.input_mapping[name]
      cfg.input_mapping[name] = {
        field: typeof legacy === 'string' ? legacy : '',
        value_type: (meta && meta.value_type) || 'inline'
      }
    }
    rows.push({
      name,
      value_type: (meta && meta.value_type) || cfg.input_mapping[name].value_type || 'inline',
      description: (meta && meta.description) || ''
    })
  }
  for (const [name, mapping] of Object.entries(cfg.input_mapping)) {
    if (name in declared) continue
    rows.push({
      name,
      value_type: (mapping && mapping.value_type) || 'inline',
      description: ''
    })
  }
  return rows
}

// 脚本选择变化：按声明字段初始化输入映射、重置派生配置；展示名兜底为脚本名
const handleTargetScriptChange = (target) => {
  if (!target.source_config) target.source_config = {}
  const cfg = target.source_config
  if (!cfg.script_id) {
    cfg.input_mapping = {}
    cfg.output_key = ''
    cfg.output_var = ''
    return
  }
  const def = preScriptDefMap.value[cfg.script_id]
  const mapping = {}
  for (const [name, meta] of Object.entries(def?.input_fields || {})) {
    mapping[name] = { field: '', value_type: (meta && meta.value_type) || 'inline' }
  }
  cfg.input_mapping = mapping
  cfg.output_key = ''
  cfg.output_var = ''
  if (!target.display_name || target.display_name === '新评测对象') {
    target.display_name = def?.name || cfg.script_id
  }
}

// ---------- hub_file 输入映射（阶段B/F4：path_pattern/required 选择器） ----------

// 数据集字段类型查询（taskApi.getDatasetFields 返回的字段含 type，hub_file 即执行包字段）
const datasetFieldType = (fieldName) => {
  const hit = datasetFields.value.find(f => f.name === fieldName)
  return hit?.type || ''
}

// hub_file 通道判定：脚本声明 value_type=hub_file，或映射的数据集字段类型为 hub_file
const isHubFileMapping = (target, row) => {
  const mapping = target?.source_config?.input_mapping?.[row.name]
  if (!mapping) return false
  if (row.value_type === 'hub_file') return true
  return !!mapping.field && datasetFieldType(mapping.field) === 'hub_file'
}

// 选择器状态（打开时快照当前配置，确定后写回）
const hubPickerVisible = ref(false)
const hubPickerTarget = ref(null)     // 当前编辑的 script_output 目标行（引用，确定时写回）
const hubPickerScriptField = ref('')  // 当前编辑的脚本输入字段名
const hubPickerFieldName = ref('')    // 映射的数据集字段名（作为 match-preview 的 field_name）
const hubPickerPattern = ref('')
const hubPickerRequired = ref(true)

const openHubPatternPicker = (target, scriptFieldName) => {
  const mapping = target?.source_config?.input_mapping?.[scriptFieldName]
  if (!mapping?.field) {
    ElMessage.warning('请先为该输入字段映射数据集字段（hub_file 类型），再选择文件')
    return
  }
  hubPickerTarget.value = target
  hubPickerScriptField.value = scriptFieldName
  hubPickerFieldName.value = mapping.field
  hubPickerPattern.value = mapping.path_pattern || ''
  hubPickerRequired.value = mapping.required !== false
  hubPickerVisible.value = true
}

// 确定回调：pattern 与 required 写入 input_mapping（后端执行时按 path_pattern/required 解析）
const handleHubPatternConfirm = ({ pattern, required }) => {
  const mapping = hubPickerTarget.value?.source_config?.input_mapping?.[hubPickerScriptField.value]
  if (!mapping) return
  mapping.path_pattern = pattern
  mapping.required = required
  ElMessage.success(`已设置匹配模式：${pattern}（${required ? '必须匹配到文件' : '允许空匹配'}）`)
}

// 参数编辑：新增（自动避免重名）
const addScriptTargetParam = (target) => {
  if (!target.source_config.params || typeof target.source_config.params !== 'object') {
    target.source_config.params = {}
  }
  let i = 1
  let key = 'param'
  while (key in target.source_config.params) {
    key = `param${++i}`
  }
  target.source_config.params[key] = ''
}

// 参数编辑：重命名（保持键顺序；重名/空名时恢复显示）
const renameScriptTargetParam = (target, oldKey, newKey) => {
  const params = target.source_config.params
  const trimmed = (newKey || '').trim()
  if (!trimmed || trimmed === oldKey || trimmed in params) {
    if (trimmed && trimmed !== oldKey && trimmed in params) {
      ElMessage.warning(`参数名 ${trimmed} 已存在`)
    }
    target.source_config.params = Object.fromEntries(Object.entries(params))
    return
  }
  target.source_config.params = Object.fromEntries(
    Object.entries(params).map(([k, v]) => [k === oldKey ? trimmed : k, v])
  )
}

// 参数编辑：删除
const removeScriptTargetParam = (target, key) => {
  delete target.source_config.params[key]
}

// 参数值类型识别：数字 / true / false 自动转换，其余保持字符串
const coerceParamValue = (value) => {
  if (value === null || value === undefined) return ''
  const raw = String(value).trim()
  if (raw === '') return ''
  if (/^-?\d+(\.\d+)?$/.test(raw)) return Number(raw)
  if (raw === 'true') return true
  if (raw === 'false') return false
  return value
}

// 编辑回显归一化：旧版任务级 pre_scripts → script_output 待评测数据行（required=false，追加在现有目标之后）
// 行结构与后端 config_migration_service 规则2 一致（display_name=output_var 或脚本名，output_key 取 output_schema 首键）
const convertLegacyPreScriptsToTargets = (raw) => {
  const list = Array.isArray(raw) ? raw : []
  return list
    .filter(s => s && typeof s === 'object' && s.script_id)
    .map((step, i) => {
      const scriptId = normalizeScriptRef(step.script_id)
      const def = preScriptDefMap.value[scriptId]
      const outputVar = (step.output_var || '').trim() || def?.name || scriptId
      const outputKey = Object.keys(def?.output_schema || {})[0] || ''
      const inputMapping = {}
      for (const [name, existing] of Object.entries(step.input_mapping || {})) {
        if (existing && typeof existing === 'object') {
          inputMapping[name] = { field: existing.field || '', value_type: existing.value_type || 'inline' }
          // hub_file 通道的 path_pattern / required 随映射保留，避免迁移回显丢失
          if (existing.path_pattern) inputMapping[name].path_pattern = existing.path_pattern
          if (existing.required !== undefined) inputMapping[name].required = existing.required
        } else {
          inputMapping[name] = { field: String(existing || ''), value_type: 'inline' }
        }
      }
      return {
        id: `target_mig_${i}_${Date.now()}`,
        name: outputVar,
        display_name: outputVar,
        source: 'script_output',
        required: false,
        description: '',
        source_config: {
          script_id: scriptId,
          output_key: outputKey,
          input_mapping: inputMapping,
          params: (step.params && typeof step.params === 'object' && !Array.isArray(step.params)) ? { ...step.params } : {},
          on_fail: step.on_fail === 'abort' ? 'abort' : 'continue',
          output_var: outputVar
        }
      }
    })
}

// 提交前校验：脚本输出行必须选择脚本、声明输入字段必须映射数据集字段、输出变量名不可重复
const validateScriptTargets = () => {
  const problems = []
  const targets = (form.value.evaluation_targets || []).filter(t => t.source === 'script_output')
  const seenVars = new Map()
  targets.forEach((target) => {
    const cfg = target.source_config || {}
    const name = target.display_name || target.id
    if (!cfg.script_id) {
      problems.push(`待评测数据「${name}」（脚本输出）未选择脚本`)
      return
    }
    const scriptName = preScriptName(cfg.script_id)
    for (const row of targetScriptFieldRows(target)) {
      const mapping = (cfg.input_mapping || {})[row.name]
      if (!mapping || !mapping.field) {
        problems.push(`待评测数据「${name}」（脚本 ${scriptName}）的输入字段 ${row.name} 未映射数据集字段`)
      }
    }
    const varName = (cfg.output_var || '').trim() || scriptName
    if (seenVars.has(varName)) {
      problems.push(`脚本输出变量名「${varName}」重复（${seenVars.get(varName)} 与 ${name}）`)
    }
    seenVars.set(varName, name)
  })
  return problems
}

// 提交组装：脚本输出行 source_config 清理（过滤空映射、参数值类型识别），其余行原样透传
const cleanEvaluationTargetsForSubmit = (targets) => {
  return (Array.isArray(targets) ? targets : []).map(t => {
    if (!t || t.source !== 'script_output') return t
    const cfg = t.source_config || {}
    return {
      ...t,
      source_config: {
        script_id: cfg.script_id || '',
        output_key: (cfg.output_key || '').trim(),
        input_mapping: Object.fromEntries(
          Object.entries(cfg.input_mapping || {})
            .filter(([, m]) => m && m.field)
            .map(([name, m]) => {
              // hub_file 通道携带 path_pattern/required（阶段B：后端按其解析执行包输入）
              const item = { field: m.field, value_type: m.value_type || 'inline' }
              if (m.path_pattern) item.path_pattern = m.path_pattern
              if (m.required !== undefined) item.required = m.required
              return [name, item]
            })
        ),
        params: Object.fromEntries(
          Object.entries(cfg.params || {})
            .filter(([key]) => key && key.trim())
            .map(([key, value]) => [key, coerceParamValue(value)])
        ),
        on_fail: cfg.on_fail === 'abort' ? 'abort' : 'continue',
        output_var: (cfg.output_var || '').trim()
      }
    }
  })
}

const openDialog = async (row = null) => {
  // 重置待删除模板标记
  pendingDeleteTemplateId.value = null
  // 重置请求链树（避免残留数据）
  chainRequestTree.value = []
  // 记录当前编辑行（「一键迁移」后重载表单用）
  currentEditRow.value = row

  if (row) {
    isEdit.value = true
    // 调用任务配置API获取配置数据（不含执行结果，避免413）
    const taskDetail = await taskApi.getConfig(row.id)

    // 旧版配置结构检测：steps / pre_scripts 仍为旧版存储时展示提示条并支持一键迁移
    const legacySteps = (Array.isArray(taskDetail.steps) ? taskDetail.steps : []).filter(s => s && String(s).trim())
    const legacyPreScripts = Array.isArray(taskDetail.pre_scripts) ? taskDetail.pre_scripts : []
    legacyConfigDetected.value = legacySteps.length > 0 || legacyPreScripts.length > 0

    form.value = {
      ...taskDetail,
      // 确保新字段有默认值
      template_id: taskDetail.template_id || '',
      // 待评测内容来源（存量任务无值时按调用被测模型处理）
      model_output_source: taskDetail.model_output_source || 'model_call',
      model_output_field: taskDetail.model_output_field || '',
      // 裁判引擎（存量旧任务无值时保持 graph 引擎不变，避免编辑保存后静默切换执行方式）
      judge_engine: taskDetail.judge_engine || 'graph',
      // 行级多 Trial（M2 QE2-3，judge 类型；存量任务无值=1 不开启）
      trials: taskDetail.trials || 1,
      // 裁判治理（M2 QE2-4；存量任务无值=默认消毒开启/金丝雀关闭）
      judge_guard_disabled: !!taskDetail.judge_guard_disabled,
      judge_canary: !!taskDetail.judge_canary,
      evaluation_scene: taskDetail.evaluation_scene || '',
      scene_desc: taskDetail.scene_desc || '',
      max_score: taskDetail.max_score || 10,
      // 行级并发数（存量任务无值时回填默认 4）
      concurrency: taskDetail.concurrency || 4,
      // 旧版 steps 展示归一化：拼进 score_details 并注明（保存后合并固化，提交时 steps 一律发 null）
      score_details: appendLegacySteps(
        taskDetail.score_details || '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格',
        legacySteps
      ),
      evaluation_metrics: taskDetail.evaluation_metrics || [],
      // 确保 judge_variable_config 有值（兼容旧的对象格式，按评测类型返回正确默认值）
      judge_variable_config: convertToArrayConfig(taskDetail.judge_variable_config, taskDetail.evaluation_type),
      // 评测目标配置（向后兼容旧任务）；旧版 pre_scripts 展示归一化为 script_output 目标行（required=false，追加在后）
      evaluation_targets: [
        ...(taskDetail.evaluation_targets || []),
        ...convertLegacyPreScriptsToTargets(legacyPreScripts)
      ],
      // 空值校验开关（向后兼容旧任务）
      validation_enabled: taskDetail.validation_enabled !== undefined ? taskDetail.validation_enabled : true,
      // RAG 配置（从数据库加载时保证默认结构）
      rag_config: taskDetail.rag_config || {
        rag_endpoint: '',
        rag_api_key: '',
        metrics: ['context_precision', 'context_recall', 'hit_rate', 'mrr'],
        thresholds: {}
      },
      // 一致性配置（从数据库加载时保证默认结构）
      consistency_config: taskDetail.consistency_config || {
        repeat_count: 10,
        use_judge: false
      },
      // 耗时配置（从数据库加载时保证默认结构）
      latency_config: taskDetail.latency_config || {
        threshold: 10.0,
        repeat_count: 1,
        score_mode: 'inverse'
      },
      // 二分分类配置（从数据库加载时解析新格式）
      binary_metrics_config: taskDetail.binary_metrics_config || {},
      binary_expected_label: taskDetail.binary_metrics_config?.expected_label || { source: 'dataset_field', source_config: { field: '' } },
      binary_predicted_label: taskDetail.binary_metrics_config?.predicted_label || { source: 'model_output', source_config: { field: 'prediction' } },
      binary_positive_value: taskDetail.binary_metrics_config?.positive_value || 'true',
      binary_label_type: taskDetail.binary_metrics_config?.label_type || 'boolean',
      // Agent 评测配置（session_context 提交时并入 scene_desc 存储，编辑时反且回显）
      agent_config: {
        ...(taskDetail.agent_config || {}),
        session_context: taskDetail.agent_config?.session_context || taskDetail.scene_desc || ''
      },
      // 安全评测配置
      safety_config: taskDetail.safety_config || {},
      // 插件评测配置（source 为表单态：显式 trace 优先，含市场来源即 marketplace，
      // 含路径/内容即手动指定，否则数据集批量）
      plugin_config: (function (pc) {
        const base = { depth: 'quick', source: 'manual', plugin_path: '', skill_content: '', market_id: null, market_plugin: '', trace_judgement: 'model', trace_concurrency: null }
        if (!pc || typeof pc !== 'object') return base
        if (pc.source === 'trace') {
          // trace 深度不可选，回显登记值 standard（旧记录可能是 quick）
          return { ...base, depth: 'standard', source: 'trace', trace_judgement: pc.trace_judgement || 'model', trace_concurrency: pc.trace_concurrency ?? null }
        }
        const hasMarket = pc.market_id != null && !!(pc.market_plugin || '').trim()
        const hasSource = (pc.plugin_path || '').trim() || (pc.skill_content || '').trim()
        return {
          depth: pc.depth || 'quick',
          source: hasMarket ? 'marketplace' : (hasSource ? 'manual' : 'dataset'),
          plugin_path: pc.plugin_path || '',
          skill_content: pc.skill_content || '',
          market_id: hasMarket ? pc.market_id : null,
          market_plugin: hasMarket ? (pc.market_plugin || '') : ''
        }
      })(taskDetail.plugin_config)
    }
    // steps/pre_scripts 已废弃：不进入表单状态（展示归一化已分别并入 score_details / evaluation_targets）
    delete form.value.steps
    delete form.value.pre_scripts
    // M5 QE5-1 版本绑定回显：dataset_version_no 有值=锁定该版本，空=跟随最新。
    // 回显期间挂抑制标记——dataset_id watcher 识别编辑回填不做「换数据集」重置
    hydratingForm.value = true
    form.value.dataset_version_no = taskDetail.dataset_version_no ?? null
    datasetVersionMode.value = taskDetail.dataset_version_no ? 'locked' : 'latest'
    nextTick(() => { hydratingForm.value = false })
    // M1 门禁配置回填表单态
    applyGateConfig(taskDetail.gate_config)
    // M2 终态校验器回填表单态
    applyVerifiers(taskDetail.verifiers)
    // M2 轨迹断言回填表单态（agent 类型）
    applyTrajectoryConfig(taskDetail.trajectory_config)
    // 脚本统一落库前的历史引用归一化：script_output 行的 builtin:<name> 改写为脚本记录 id（保存即固化）
    for (const target of (form.value.evaluation_targets || [])) {
      if (target?.source === 'script_output' && target.source_config?.script_id) {
        target.source_config.script_id = normalizeScriptRef(target.source_config.script_id)
      }
    }
    // 加载运行时变量
    if (!form.value.runtime_variables) {
      form.value.runtime_variables = {}
    }
    // 插件评测编辑回显：拉市场列表；市场来源时进一步拉该市场的插件列表
    if (form.value.evaluation_type === 'plugin') {
      loadMarketplaces().then(() => {
        if (form.value.plugin_config?.source === 'marketplace' && form.value.plugin_config.market_id) {
          loadMarketPlugins()
        }
      })
    }
  } else {
    isEdit.value = false
    form.value = {
      name: '',
      evaluation_type: 'judge',
      dataset_id: '',
      // M5 QE5-1 数据集版本绑定：null=跟随最新；锁定时为 dataset_versions.version_no
      dataset_version_no: null,
      model_provider: '',
      model_output_source: 'model_call',
      model_output_field: '',
      judge_engine: 'claude_agent',
      judge_count: 6,
      // M2 QE2-3：行级多 Trial 默认不开启
      trials: 1,
      // M2 QE2-4：裁判治理默认（消毒开启/金丝雀关闭）
      judge_guard_disabled: false,
      judge_canary: false,
      // 行级并发数（origin/main judge-pool，与裁判数量解耦）
      concurrency: 4,
      description: '',
      template_id: '',
      evaluation_scene: '',
      scene_desc: '',
      max_score: 10,
      score_details: '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格',
      evaluation_metrics: [],
      runtime_variables: {},
      judge_variable_config: JSON.parse(JSON.stringify(DEFAULT_JUDGE_VARIABLE_CONFIG)),
      evaluation_targets: [],
      validation_enabled: true,
      rag_config: {
        rag_endpoint: '',
        rag_api_key: '',
        metrics: ['context_precision', 'context_recall', 'hit_rate', 'mrr'],
        thresholds: {}
      },
      consistency_config: {
        repeat_count: 10,
        use_judge: false
      },
      latency_config: {
        threshold: 10.0,
        repeat_count: 1,
        score_mode: 'inverse'
      },
      binary_metrics_config: {},
      binary_expected_label: { source: 'dataset_field', source_config: { field: '' } },
      binary_predicted_label: { source: 'model_output', source_config: { field: 'prediction' } },
      binary_positive_value: 'true',
      binary_label_type: 'boolean',
      agent_config: {},
      safety_config: {},
      plugin_config: { depth: 'quick', source: 'manual', plugin_path: '', skill_content: '', market_id: null, market_plugin: '', trace_judgement: 'model', trace_concurrency: null }
    }
    // 新建任务无旧版结构
    legacyConfigDetected.value = false
    // M5 QE5-1 新建默认跟随最新（清空锁定号与版本列表残留）
    datasetVersionMode.value = 'latest'
    datasetVersions.value = []
    // M1 门禁配置默认关闭
    applyGateConfig(null)
    // M2 终态校验器默认为空
    applyVerifiers(null)
    // M2 轨迹断言默认为空
    applyTrajectoryConfig(null)
  }
  dialogVisible.value = true

  // 获取模型变量
  await loadModelVariables()

  // 如果有数据集选择，加载数据集字段
  if (form.value.dataset_id) {
    await fetchDatasetFields(form.value.dataset_id)
  }

  // 如果任务关联了模板，从最新的模板列表中加载配置（修复模板更新后不同步的问题）
  if (isEdit.value && form.value.template_id) {
    const latestTemplate = templates.value.find(t => t.id === form.value.template_id)
    if (latestTemplate) {
      // 使用最新模板内容覆盖任务中的配置
      form.value.scene_desc = latestTemplate.scene_description || ''
      form.value.judge_prompt = latestTemplate.judge_prompt || ''
      form.value.max_score = latestTemplate.max_score || 10
      // 旧版模板 steps 展示归一化：拼进 score_details（保存后合并固化）
      form.value.score_details = appendLegacySteps(
        latestTemplate.score_details || '9-10分：优秀；7-8分：良好；5-6分：及格；<5分：不及格',
        latestTemplate.steps
      )
      form.value.evaluation_metrics = (latestTemplate.evaluation_metrics || []).map((m, i) => ({
        id: `metric_${Date.now()}_${i}`,
        name: m.name || '',
        weight: m.weight || 'medium',
        max_score: m.max_score || latestTemplate.max_score || 10,
        description: m.description || '',
        score_criteria: m.score_criteria || {},
        dependencies: m.dependencies || []
      }))
      // 裁判变量配置：仅当任务的配置与默认值一致时才用模板覆盖（保留用户自定义配置）
      if (latestTemplate.judge_variable_config) {
        const defaultKeys = Object.keys(DEFAULT_JUDGE_VARIABLE_CONFIG.reduce((acc, item) => { acc[item.key] = true; return acc }, {}))
        const taskConfigKeys = form.value.judge_variable_config.map(item => item.key).sort()
        const isDefaultConfig = taskConfigKeys.length === defaultKeys.length && defaultKeys.every(k => taskConfigKeys.includes(k))
        if (isDefaultConfig) {
          form.value.judge_variable_config = convertToArrayConfig(latestTemplate.judge_variable_config, form.value.evaluation_type)
        }
      }
    }
  }

  // 初始化评测维度的依赖字段（向后兼容旧任务）
  if (form.value.evaluation_metrics.length) {
    form.value.evaluation_metrics.forEach(m => {
      if (!m.dependencies) {
        m.dependencies = []
      }
    })
  }
  // 初始化评测目标的描述字段与脚本输出编排结构（向后兼容旧任务）
  if (form.value.evaluation_targets.length) {
    form.value.evaluation_targets.forEach(t => {
      if (!t.description) {
        t.description = ''
      }
      if (t.source === 'script_output') {
        ensureScriptTargetConfig(t)
      }
    })
  }

  // 如果裁判变量配置中使用了请求链结果，需要加载请求链并恢复已选状态
  await restoreChainResultSelection()
}

// 恢复请求链结果的已选状态
const restoreChainResultSelection = async () => {
  // 检查所有使用 chain_result 的配置：裁判变量、评测目标、二分分类标签
  const hasJudgeChainResult = form.value.judge_variable_config?.some(item => item.source === 'chain_result')
  const hasTargetChainResult = form.value.evaluation_targets?.some(target => target.source === 'chain_result')
  const hasBinaryExpectedChainResult = form.value.binary_expected_label?.source === 'chain_result'
  const hasBinaryPredictedChainResult = form.value.binary_predicted_label?.source === 'chain_result'

  const hasAnyChainResult = hasJudgeChainResult || hasTargetChainResult || hasBinaryExpectedChainResult || hasBinaryPredictedChainResult
  if (!hasAnyChainResult) return

  // 加载请求链树
  await updateChainOptions()

  if (chainRequestTree.value.length === 0) return

  // 等待 DOM 更新后恢复树形复选框的选中状态
  await nextTick()

  // 恢复裁判变量配置中 chain_result 的选中状态
  if (hasJudgeChainResult) {
    for (const item of form.value.judge_variable_config) {
      if (item.source !== 'chain_result') continue
      if (!item.source_config?.var_names?.length) continue

      const treeRef = getChainTreeRef(item)
      if (!treeRef) continue

      for (const varName of item.source_config.var_names) {
        let targetValue = null
        const findValue = (nodes) => {
          for (const node of nodes) {
            if (node.label === varName && node.isLeaf) {
              targetValue = node.value
              return true
            }
            if (node.children) {
              if (findValue(node.children)) return true
            }
          }
          return false
        }
        findValue(chainRequestTree.value)

        if (targetValue) {
          treeRef.setChecked(targetValue, true)
        }
      }
    }
  }

  // 恢复评测目标配置中 chain_result 的选中状态
  if (hasTargetChainResult) {
    for (const target of form.value.evaluation_targets) {
      if (target.source !== 'chain_result') continue
      if (!target.source_config?.var_names?.length) continue

      const treeRef = getChainTreeRefForTarget(target)
      if (!treeRef) continue

      for (const varName of target.source_config.var_names) {
        let targetValue = null
        const findValue = (nodes) => {
          for (const node of nodes) {
            if (node.label === varName && node.isLeaf) {
              targetValue = node.value
              return true
            }
            if (node.children) {
              if (findValue(node.children)) return true
            }
          }
          return false
        }
        findValue(chainRequestTree.value)

        if (targetValue) {
          treeRef.setChecked(targetValue, true)
        }
      }
    }
  }

  // 恢复二分分类期望标签中 chain_result 的选中状态
  if (hasBinaryExpectedChainResult && form.value.binary_expected_label?.source_config?.var_names?.length) {
    const treeRef = chainTreeRefs.value.get('binary_expected')
    if (treeRef) {
      for (const varName of form.value.binary_expected_label.source_config.var_names) {
        let targetValue = null
        const findValue = (nodes) => {
          for (const node of nodes) {
            if (node.label === varName && node.isLeaf) {
              targetValue = node.value
              return true
            }
            if (node.children) {
              if (findValue(node.children)) return true
            }
          }
          return false
        }
        findValue(chainRequestTree.value)

        if (targetValue) {
          treeRef.setChecked(targetValue, true)
        }
      }
    }
  }

  // 恢复二分分类预测标签中 chain_result 的选中状态
  if (hasBinaryPredictedChainResult && form.value.binary_predicted_label?.source_config?.var_names?.length) {
    const treeRef = chainTreeRefs.value.get('binary_predicted')
    if (treeRef) {
      for (const varName of form.value.binary_predicted_label.source_config.var_names) {
        let targetValue = null
        const findValue = (nodes) => {
          for (const node of nodes) {
            if (node.label === varName && node.isLeaf) {
              targetValue = node.value
              return true
            }
            if (node.children) {
              if (findValue(node.children)) return true
            }
          }
          return false
        }
        findValue(chainRequestTree.value)

        if (targetValue) {
          treeRef.setChecked(targetValue, true)
        }
      }
    }
  }
}

// 兼容旧版本的对象格式配置，支持任意 key（judge/rag 通用）
const convertToArrayConfig = (config, evaluationType = 'judge') => {
  const fallback = evaluationType === 'rag'
    ? JSON.parse(JSON.stringify(DEFAULT_RAG_JUDGE_VARIABLE_CONFIG))
    : JSON.parse(JSON.stringify(DEFAULT_JUDGE_VARIABLE_CONFIG))
  if (!config) return fallback
  // 如果已经是数组且非空，直接返回；若为空数组则使用默认值
  if (Array.isArray(config)) return config.length > 0 ? config : fallback
  // 对象格式通用转换为数组（适配任意 key）
  const result = Object.entries(config)
    .filter(([, val]) => val && typeof val === 'object')
    .map(([key, val]) => ({ key, name: val.name || key, ...val }))
  return result.length > 0 ? result : fallback
}

// 将数组格式转换为字典格式（提交时使用）
const convertToDictConfig = (config) => {
  if (!config || !Array.isArray(config)) return config
  const result = {}
  config.forEach(item => {
    if (item.key) {
      const { key, name, ...rest } = item
      result[key] = { name, ...rest }
    }
  })
  return result
}

// 待评测内容来源切换：离开「使用数据集字段」时清空已选字段
const handleModelOutputSourceChange = (val) => {
  if (val !== 'dataset_field') {
    form.value.model_output_field = ''
  }
}

// 评测类型切换时重置裁判变量配置为对应默认值
const handleEvaluationTypeChange = (newType) => {
  // 待评测内容来源仅 AI 裁判员评测使用：切到 judge 时默认调用被测模型，其它类型清空相关值
  if (newType === 'judge') {
    if (!form.value.model_output_source) {
      form.value.model_output_source = 'model_call'
    }
  } else {
    form.value.model_output_source = 'model_call'
    form.value.model_output_field = ''
  }
  if (newType === 'rag') {
    form.value.judge_variable_config = JSON.parse(JSON.stringify(DEFAULT_RAG_JUDGE_VARIABLE_CONFIG))
    // 初始化 RAG 配置
    if (!form.value.rag_config) {
      form.value.rag_config = {
        rag_endpoint: '',
        rag_api_key: '',
        metrics: ['context_precision', 'context_recall', 'hit_rate', 'mrr'],
        thresholds: {}
      }
    }
  } else if (newType === 'consistency') {
    // 初始化一致性配置
    form.value.consistency_config = {
      repeat_count: 10,
      use_judge: false,
    }
    // 重置裁判数量为默认值（一致性评测启用裁判时使用）
    if (!form.value.judge_count) {
      form.value.judge_count = 1
    }
  } else if (newType === 'success_rate') {
    // 成功率评测不使用裁判配置，清空相关配置
    form.value.judge_variable_config = []
    form.value.judge_count = 0
  } else if (newType === 'latency') {
    // 初始化耗时评测配置
    form.value.latency_config = {
      threshold: 10.0,
      repeat_count: 1,
      score_mode: 'inverse'
    }
  } else if (newType === 'binary_classification') {
    // 二分分类评测不使用裁判配置，清空相关配置
    form.value.judge_variable_config = {}
    form.value.judge_count = 0
    form.value.validation_enabled = true
    // 初始化二分标签配置
    form.value.binary_metrics_config = {}
    form.value.binary_expected_label = { source: 'dataset_field', source_config: { field: '' } }
    form.value.binary_predicted_label = { source: 'model_output', source_config: { field: 'prediction' } }
    form.value.binary_positive_value = 'true'
    form.value.binary_label_type = 'boolean'
  } else if (newType === 'agent') {
    // 长会话 Agent 评测：裁判直接评会话，复用 judge 类型的三变量默认配置
    form.value.judge_variable_config = JSON.parse(JSON.stringify(DEFAULT_JUDGE_VARIABLE_CONFIG))
    form.value.judge_count = 1
    form.value.agent_config = {}
    // 裁判引擎默认 Claude Code Agent
    if (!form.value.judge_engine) {
      form.value.judge_engine = 'claude_agent'
    }
  } else if (newType === 'plugin') {
    // 插件评测：不使用裁判变量配置（standard 深度评审由 Claude Code 承载）
    form.value.judge_variable_config = []
    form.value.judge_count = 0
    if (!form.value.plugin_config) {
      form.value.plugin_config = { depth: 'quick', source: 'manual', plugin_path: '', skill_content: '', market_id: null, market_plugin: '', trace_judgement: 'model', trace_concurrency: null }
    }
    // 市场来源下拉数据（仅首次拉取）
    if (!marketplaces.value.length) {
      loadMarketplaces()
    }
  } else if (newType === 'safety') {
    // 安全评测（M3 QE3-1 红队）：不使用裁判配置；safety_config 默认四类攻击面
    // 全选 + 内置样本库直跑 + 变异关闭 + 双通道全开（pack 直跑不需要数据集）
    form.value.judge_variable_config = []
    form.value.judge_count = 0
    form.value.safety_config = {
      categories: ['prompt_injection', 'jailbreak', 'sensitive_content', 'factual_assertion'],
      sample_source: 'pack',
      pack: 'redteam_pack_v1',
      mutation: { enabled: false, count: 1 },
      channels: {
        deterministic: { enabled: true, custom_words: [] },
        judge_rubric: { enabled: true }
      },
      metrics: []
    }
    form.value.dataset_id = null
  } else if (newType === 'dialogue') {
    // 对话评测使用默认裁判配置
    form.value.judge_variable_config = JSON.parse(JSON.stringify(DEFAULT_JUDGE_VARIABLE_CONFIG))
    if (!form.value.judge_count) {
      form.value.judge_count = 6
    }
  } else {
    form.value.judge_variable_config = JSON.parse(JSON.stringify(DEFAULT_JUDGE_VARIABLE_CONFIG))
    // 重置裁判数量
    if (!form.value.judge_count) {
      form.value.judge_count = 6
    }
  }
}

// Agent 评测配置更新处理
const handleAgentConfigUpdate = (data) => {
  form.value.agent_config = data
}

// 安全评测配置更新处理
const handleSafetyConfigUpdate = (data) => {
  form.value.safety_config = data
}

// 插件包/SKILL.md 上传（插件评测手动指定来源）：zip 回填解包路径，md 回填内联内容
const pluginUploading = ref(false)
const handlePluginUpload = async ({ file }) => {
  const name = (file.name || '').toLowerCase()
  if (!name.endsWith('.zip') && !name.endsWith('.md') && !name.endsWith('.markdown')) {
    ElMessage.error('仅支持 .zip 插件包或 SKILL.md（.md）文件')
    return
  }
  if (file.size > 20 * 1024 * 1024) {
    ElMessage.error('文件超限（>20MB）')
    return
  }
  pluginUploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    const res = await taskApi.uploadPluginPackage(fd)
    if (res.kind === 'plugin_dir') {
      form.value.plugin_config.plugin_path = res.path
      manualPluginTargetType.value = res.target_type || ''
      ElMessage.success(`已解包并填入插件路径（${res.target_type === 'plugin' ? '识别为插件，按静态层评测' : '识别为 Skill'}，${res.file_count} 个文件）`)
    } else if (res.kind === 'skill_md') {
      form.value.plugin_config.skill_content = res.content
      manualPluginTargetType.value = 'skill'
      ElMessage.success(`已读取 ${res.name} 并填入内联 SKILL.md`)
    } else {
      ElMessage.error(res.error || '上传失败')
    }
  } catch (e) {
    ElMessage.error(e.message || '上传失败')
  } finally {
    pluginUploading.value = false
  }
}

// ---------------- 插件市场（插件评测「插件市场」来源） ----------------
const marketplaces = ref([])
const marketListLoading = ref(false)
const marketCreating = ref(false)
const marketSyncingId = ref(null)
const marketSyncError = ref('')
const marketDialogVisible = ref(false)
const marketForm = reactive({ name: '', git_url: '', branch: '' })
const marketPlugins = ref([])
const marketPluginsLoading = ref(false)

const loadMarketplaces = async () => {
  marketListLoading.value = true
  try {
    marketplaces.value = await pluginMarketplaceApi.list()
  } catch (e) {
    console.warn('加载插件市场列表失败', e)
  } finally {
    marketListLoading.value = false
  }
}

const loadMarketPlugins = async (force = false) => {
  const marketId = form.value.plugin_config?.market_id
  if (!marketId) {
    marketPlugins.value = []
    return
  }
  if (!force && marketPlugins.value.length) return
  marketPluginsLoading.value = true
  try {
    marketPlugins.value = await pluginMarketplaceApi.plugins(marketId)
  } catch (e) {
    marketPlugins.value = []
    ElMessage.error(e.message || '加载市场插件列表失败')
  } finally {
    marketPluginsLoading.value = false
  }
}

const handleMarketChange = () => {
  form.value.plugin_config.market_plugin = ''
  marketPlugins.value = []
  loadMarketPlugins(true)
}

// 深度守卫：插件级目标只支持快速（standard/蒙特卡洛仅支持 skill 级对象）。
// 创建期由后端强校验，这里同步禁用选项并自动切回快速，避免用户提交后才报错。
const manualPluginTargetType = ref('')   // 手动上传时后端识别出的对象类型

const pluginTargetType = computed(() => {
  const pc = form.value.plugin_config
  if (pc.source === 'marketplace') {
    const item = marketPlugins.value.find((p) => p.source === pc.market_plugin)
    return item ? item.target_type : ''
  }
  if (pc.source === 'manual') {
    if (pc.skill_content) return 'skill'
    if (pc.plugin_path) return manualPluginTargetType.value
  }
  return ''
})

const isPluginLevelTarget = computed(() => pluginTargetType.value === 'plugin')

watch(isPluginLevelTarget, (isPluginLevel) => {
  if (isPluginLevel && form.value.plugin_config.depth !== 'quick') {
    form.value.plugin_config.depth = 'quick'
    ElMessage.info('插件级评测对象只支持快速深度（仅静态层），已切回快速')
  }
})

const handleCreateMarketplace = async () => {
  if (!marketForm.name.trim() || !marketForm.git_url.trim()) {
    ElMessage.warning('请填写市场名称和 Git 地址')
    return
  }
  marketCreating.value = true
  marketSyncError.value = ''
  try {
    await pluginMarketplaceApi.create({ ...marketForm })
    ElMessage.success('市场已添加并克隆成功')
    marketForm.name = ''
    marketForm.git_url = ''
    marketForm.branch = ''
    await loadMarketplaces()
  } catch (e) {
    marketSyncError.value = e.message || '添加失败'
    ElMessage.error(e.message || '添加失败')
  } finally {
    marketCreating.value = false
  }
}

const handleSyncMarketplace = async (row) => {
  marketSyncingId.value = row.id
  marketSyncError.value = ''
  try {
    await pluginMarketplaceApi.sync(row.id)
    ElMessage.success(`市场「${row.name}」已同步`)
    await loadMarketplaces()
    if (form.value.plugin_config?.market_id === row.id) {
      loadMarketPlugins(true)
    }
  } catch (e) {
    marketSyncError.value = e.message || '同步失败'
    ElMessage.error(e.message || '同步失败')
    await loadMarketplaces()
  } finally {
    marketSyncingId.value = null
  }
}

const handleDeleteMarketplace = async (row) => {
  try {
    await ElMessageBox.confirm(`确定删除市场「${row.name}」？将同时清理服务端克隆目录。`, '删除插件市场', { type: 'warning' })
  } catch {
    return
  }
  try {
    await pluginMarketplaceApi.remove(row.id)
    ElMessage.success('已删除')
    if (form.value.plugin_config?.market_id === row.id) {
      form.value.plugin_config.market_id = null
      form.value.plugin_config.market_plugin = ''
      marketPlugins.value = []
    }
    await loadMarketplaces()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

// 评测目标来源选项
const targetSourceOptions = [
  { label: '模型输出', value: 'model_output' },
  { label: '请求链结果', value: 'chain_result' },
  { label: '数据集字段', value: 'dataset_field' },
  { label: '脚本输出', value: 'script_output' },
  { label: '手动输入', value: 'manual_input' }
]

// 添加评测目标（表格样式）
const addEvaluationTarget = () => {
  const id = `target_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
  form.value.evaluation_targets.push({
    id,
    name: '新评测对象',
    display_name: '新评测对象',
    source: 'model_output',
    source_config: {},
    required: true,
    description: '',
  })
}

// 兜底补齐脚本输出行的 source_config 结构（编辑回显存量行时各子键可能缺失）
const ensureScriptTargetConfig = (target) => {
  if (!target.source_config || typeof target.source_config !== 'object') {
    target.source_config = {}
  }
  const cfg = target.source_config
  if (!cfg.input_mapping || typeof cfg.input_mapping !== 'object') cfg.input_mapping = {}
  if (!cfg.params || typeof cfg.params !== 'object' || Array.isArray(cfg.params)) cfg.params = {}
  if (cfg.output_key === undefined || cfg.output_key === null) cfg.output_key = ''
  if (cfg.output_var === undefined || cfg.output_var === null) cfg.output_var = ''
  if (cfg.script_id === undefined || cfg.script_id === null) cfg.script_id = ''
  if (cfg.on_fail !== 'abort' && cfg.on_fail !== 'continue') cfg.on_fail = 'continue'
}

// 处理评测目标来源变化
const handleTargetSourceChange = (val, target) => {
  if (!target.source_config) {
    target.source_config = {}
  }
  if (val === 'dataset_field') {
    target.source_config.field = undefined
  } else if (val === 'manual_input') {
    target.source_config.value = ''
  } else if (val === 'chain_result') {
    // 切换到请求链时加载树
    updateChainOptions()
    target.source_config.var_names = []
  } else if (val === 'script_output') {
    // 切换到脚本输出：初始化编排结构（脚本/输出键/输入映射/参数/失败策略/输出变量名）
    target.source_config = {
      script_id: '',
      output_key: '',
      input_mapping: {},
      params: {},
      on_fail: 'continue',
      output_var: ''
    }
  } else {
    target.source_config = {}
  }
}

// 二分分类 - 标签来源变化处理
const handleBinaryLabelSourceChange = (val, labelConfig) => {
  if (!labelConfig.source_config) {
    labelConfig.source_config = { field: '', chain_index: undefined, var_names: [], manual_value: '' }
  }
  if (val === 'chain_result') {
    updateChainOptions()
    labelConfig.source_config.field = ''
  } else if (val === 'model_output') {
    labelConfig.source_config.field = ''
    labelConfig.source_config.chain_index = undefined
    labelConfig.source_config.var_names = []
  } else {
    labelConfig.source_config.field = ''
    labelConfig.source_config.chain_index = undefined
    labelConfig.source_config.var_names = []
  }
}

// 二分分类 - 获取请求链变量文本
const getBinaryLabelChainVarsText = (labelConfig) => {
  if (!labelConfig.source_config?.var_names || labelConfig.source_config.var_names.length === 0) {
    return ''
  }
  return `已选 ${labelConfig.source_config.var_names.length} 个变量`
}

// 二分分类 - 设置请求链树引用（复用 chainTreeRefs）
const setBinaryLabelChainTreeRef = (labelKey, el) => {
  if (el) {
    chainTreeRefs.value.set(`binary_${labelKey}`, el)
  }
}

// 二分分类 - 请求链变量选择处理
const handleBinaryLabelChainVarCheck = (node, checkedStatus, labelConfig, labelKey) => {
  if (!labelConfig.source_config) {
    labelConfig.source_config = { field: '', chain_index: undefined, var_names: [], manual_value: '' }
  }

  const treeRef = chainTreeRefs.value.get(`binary_${labelKey}`)
  if (!treeRef) return

  const checkedNodes = treeRef.getCheckedNodes(false, true)
  const varNames = checkedNodes
    .filter(n => n.isLeaf)
    .map(n => n.label)

  labelConfig.source_config.var_names = varNames

  if (varNames.length > 0 && checkedNodes.length > 0) {
    const firstChecked = checkedNodes.find(n => !n.isLeaf)
    if (firstChecked) {
      labelConfig.source_config.chain_index = firstChecked.value
    }
  }
}

// 二分分类 - 移除请求链变量
const removeBinaryLabelChainVar = (labelConfig, varName) => {
  if (labelConfig.source_config?.var_names) {
    const idx = labelConfig.source_config.var_names.indexOf(varName)
    if (idx >= 0) {
      labelConfig.source_config.var_names.splice(idx, 1)
    }
  }
}

// 获取数据集字段选项（用于评测目标）
const getDatasetFieldOptions = () => {
  if (datasetFields.value && datasetFields.value.length > 0) {
    return datasetFields.value.map(f => ({
      label: f.label || f.name,
      value: f.name
    }))
  }
  return []
}

// 模型输出字段选项：排除平台辅助列（*_is_media / *_hub_file），
// 与后端 model_output_field 校验口径一致，避免 UI 可选、保存被拒
const modelOutputFieldOptions = computed(() =>
  getDatasetFieldOptions().filter(opt => {
    const name = String(opt.value || '')
    return name && !name.endsWith('_is_media') && !name.endsWith('_hub_file')
  })
)

// 获取评测目标选中的请求链变量文本
const getSelectedChainVarsTextForTarget = (target) => {
  if (!target.source_config?.var_names || target.source_config.var_names.length === 0) {
    return ''
  }
  return `已选 ${target.source_config.var_names.length} 个变量`
}

// 获取评测目标选中的请求链变量列表
const getSelectedChainVarsForTarget = (target) => {
  return target.source_config?.var_names || []
}

// 处理评测目标的请求链变量选择
const handleTargetChainVarCheck = (node, checkedStatus, target) => {
  if (!target.source_config) {
    target.source_config = {
      field: undefined,
      chain_index: undefined,
      var_names: [],
      manual_value: undefined
    }
  }
  if (!target.source_config.var_names) {
    target.source_config.var_names = []
  }

  // 获取所有选中的节点
  const treeRef = getChainTreeRefForTarget(target)
  if (!treeRef) return

  const checkedNodes = treeRef.getCheckedNodes(false, true) // 不包括半选中的父节点
  const varNames = checkedNodes
    .filter(n => n.isLeaf)
    .map(n => n.label)

  target.source_config.var_names = varNames

  // 如果选择了子节点，设置对应的 chain_index
  if (varNames.length > 0 && checkedNodes.length > 0) {
    const firstChecked = checkedNodes.find(n => !n.isLeaf)
    if (firstChecked) {
      target.source_config.chain_index = firstChecked.value
    }
  }
}

// 移除评测目标选中的请求链变量
const removeSelectedChainVarForTarget = (target, varName) => {
  if (!target.source_config?.var_names) return
  const index = target.source_config.var_names.indexOf(varName)
  if (index > -1) {
    target.source_config.var_names.splice(index, 1)
  }
  // 同步更新 tree 的选中状态
  const treeRef = getChainTreeRefForTarget(target)
  if (treeRef) {
    const node = treeRef.getNode(varName)
    if (node) {
      treeRef.setChecked(node, false, false)
    }
  }
}

// 获取请求链变量选项（用于评测目标）
const getChainVarOptions = () => {
  const options = []
  if (chainRequestTree.value && chainRequestTree.value.length > 0) {
    const extractLeafNodes = (nodes) => {
      for (const node of nodes) {
        if (node.isLeaf) {
          options.push({ label: node.label, value: node.label })
        }
        if (node.children) {
          extractLeafNodes(node.children)
        }
      }
    }
    extractLeafNodes(chainRequestTree.value)
  }
  return options
}

// 删除评测目标
const removeEvaluationTarget = (index) => {
  form.value.evaluation_targets.splice(index, 1)
}

// 添加裁判变量
const addJudgeVariable = () => {
  form.value.judge_variable_config.push({
    key: `custom_${Date.now()}`,
    name: '新字段',
    source: 'dataset_field',
    source_config: {
      field: '',
      chain_index: undefined,
      var_names: [],
      manual_value: undefined
    },
    prompt_type: 'user',
    required: true
  })
}

// 删除裁判变量
const removeJudgeVariable = (index) => {
  form.value.judge_variable_config.splice(index, 1)
}

// 数据来源选项配置
const sourceOptions = [
  { label: '数据集字段', value: 'dataset_field' },
  { label: '请求链结果', value: 'chain_result' },
  { label: '模型输出', value: 'model_output' },
  { label: '手动输入', value: 'manual' }
]

// 加载模型的请求链信息
const loadChainRequests = async (modelId) => {
  if (!modelId) return []

  // 检查缓存
  if (chainRequestsCache.value.has(modelId)) {
    return chainRequestsCache.value.get(modelId)
  }

  chainLoading.value = true
  try {
    const res = await modelApi.get(modelId)
    const requests = res.request_config?.chain_requests?.requests || []
    chainRequestsCache.value.set(modelId, requests)
    return requests
  } catch (e) {
    console.error('获取模型请求链失败:', e)
    return []
  } finally {
    chainLoading.value = false
  }
}

// 获取请求链选项（简单模式，用于下拉框）
const getChainRequestOptions = (requests) => {
  if (!requests || !requests.length) return []

  return requests.map(req => ({
    label: req.name || `请求${req.order}`,
    value: req.order
  }))
}

// 获取请求链树形结构（用于树形复选框多选）
const getChainRequestTree = (requests) => {
  if (!requests || !requests.length) return []

  return requests.map(req => {
    // 获取该请求的 output_vars
    const outputVars = req.output_vars || []
    let children = []

    // 如果有 output_vars，解析变量名（兼容两种格式：{name} 或 {Name: "xxx"}）
    if (outputVars.length > 0) {
      children = outputVars.map(ov => {
        // ov 可能是字符串 "varName" 或对象 {name: "varName"} 或 {Name: "varName"}
        const varName = typeof ov === 'string' ? ov : (ov.name || ov.Name || '')
        return {
          label: varName,
          value: `chain_${req.order}_${varName}`,
          isLeaf: true
        }
      })
    }

    // 如果没有 output_vars，但有 output_var（兼容旧配置）
    if (children.length === 0 && req.output_var) {
      children.push({
        label: req.output_var,
        value: `chain_${req.order}_${req.output_var}`,
        isLeaf: true
      })
    }

    // SSE 请求：检查 response_parse.data_field 是否配置了输出变量
    const isSSE = req.response_type === 'sse' || req.is_sse
    if (children.length === 0 && isSSE && req.response_parse) {
      const responseParse = typeof req.response_parse === 'string' ? JSON.parse(req.response_parse) : req.response_parse
      // data_field 可以是单个字段名或字段名数组
      const dataField = responseParse?.data_field
      if (dataField) {
        const fields = Array.isArray(dataField) ? dataField : [dataField]
        children = fields.map(field => ({
          label: field,
          value: `chain_${req.order}_${field}`,
          isLeaf: true
        }))
      }
    }

    // 如果没有子节点，则当前请求节点本身就是叶子节点（可直接选择）
    if (children.length === 0 && !req.output_var) {
      return {
        label: req.name || `请求${req.order}`,
        value: `chain_${req.order}_${req.name || req.order}`,
        isLeaf: true
      }
    }

    return {
      label: req.name || `请求${req.order}`,
      value: req.order,
      children: children.length > 0 ? children : undefined
    }
  })
}

// 更新当前模型的请求链选项
const updateChainOptions = async () => {
  if (!form.value?.model_provider) {
    currentChainOptions.value = []
    return
  }
  const requests = await loadChainRequests(form.value.model_provider)
  currentChainOptions.value = getChainRequestOptions(requests)
  // 同时更新树形结构
  chainRequestTree.value = getChainRequestTree(requests)
}

// 请求链树形数据
const chainRequestTree = ref([])

// 校验是否可以选择请求链结果
const validateChainSource = async (item) => {
  // 1. 检查是否选择了被评测模型
  if (!form.value?.model_provider) {
    ElMessageBox.alert('请先选择被评测模型，再选择请求链结果作为数据来源', '提示', {
      type: 'warning'
    })
    item.source = ''
    return false
  }

  // 2. 检查模型是否有请求链
  const requests = await loadChainRequests(form.value.model_provider)
  if (!requests || requests.length === 0) {
    ElMessageBox.alert('当前被评测模型没有配置请求链，无法使用请求链结果', '提示', {
      type: 'warning'
    })
    item.source = ''
    currentChainOptions.value = []
    return false
  }

  return true
}

// 处理数据来源变化
const handleSourceChange = async (val, item) => {
  // 确保 source_config 存在
  if (!item.source_config) {
    item.source_config = {
      field: undefined,
      chain_index: undefined,
      var_names: [],
      manual_value: undefined
    }
  }

  if (val === 'chain_result') {
    await updateChainOptions()
    // 切换到 chain_result 时清空 dataset_field 相关配置
    item.source_config.field = undefined
  } else if (val === 'dataset_field') {
    item.source_config.field = undefined
    item.source_config.chain_index = undefined
    item.source_config.var_names = []
  } else {
    // 切换到其他来源时清空所有映射配置
    item.source_config.field = undefined
    item.source_config.chain_index = undefined
    item.source_config.var_names = []
    if (val === 'manual') {
      item.source_config.manual_value = undefined
    }
  }
}

// 处理请求链索引选择变化
const handleChainIndexChange = async (val, item) => {
  // 确保 source_config 存在
  if (!item.source_config) {
    item.source_config = {
      field: undefined,
      chain_index: undefined,
      var_names: [],
      manual_value: undefined
    }
  }

  if (!val) {
    // 清空选择时不校验
    return
  }
  // 校验模型是否有请求链
  if (!form.value?.model_provider) {
    ElMessageBox.alert('请先选择被评测模型', '提示', { type: 'warning' })
    item.source_config.chain_index = undefined
    return
  }

  const requests = await loadChainRequests(form.value.model_provider)
  if (!requests || requests.length === 0) {
    ElMessageBox.alert('当前被评测模型没有配置请求链，无法使用请求链结果', '提示', { type: 'warning' })
    item.source_config.chain_index = undefined
  }
}

// 获取选中的请求链变量文本
const getSelectedChainVarsText = (item) => {
  if (!item.source_config?.var_names || item.source_config.var_names.length === 0) {
    return ''
  }
  return `已选择 ${item.source_config.var_names.length} 个变量`
}

// 获取选中的请求链变量列表
const getSelectedChainVars = (item) => {
  return item.source_config?.var_names || []
}

// 处理树形复选框选择变化
const handleChainVarCheck = (node, checkedStatus, item) => {
  if (!item.source_config) {
    item.source_config = {
      field: undefined,
      chain_index: undefined,
      var_names: [],
      manual_value: undefined
    }
  }

  // 获取所有选中的节点
  const treeRef = getChainTreeRef(item)
  if (!treeRef) return

  const checkedNodes = treeRef.getCheckedNodes(false, true) // 不包括半选中的父节点
  const varNames = checkedNodes
    .filter(n => n.isLeaf)
    .map(n => n.label)

  item.source_config.var_names = varNames

  // 如果选择了子节点，设置对应的 chain_index
  if (varNames.length > 0 && checkedNodes.length > 0) {
    const firstChecked = checkedNodes.find(n => !n.isLeaf)
    if (firstChecked) {
      item.source_config.chain_index = firstChecked.value
    }
  }
}

// 移除选中的请求链变量
const removeSelectedChainVar = (item, varName) => {
  if (!item.source_config?.var_names) return
  const index = item.source_config.var_names.indexOf(varName)
  if (index > -1) {
    item.source_config.var_names.splice(index, 1)
  }
}

// 存储每个变量的 tree ref
const chainTreeRefs = ref(new Map())

const setChainTreeRef = (item, el) => {
  if (el) {
    chainTreeRefs.value.set(item.key || JSON.stringify(item), el)
  }
}

const getChainTreeRef = (item) => {
  return chainTreeRefs.value.get(item.key || JSON.stringify(item))
}

// 存储评测目标的 tree ref
const setChainTreeRefForTarget = (target, el) => {
  if (el && target.id) {
    chainTreeRefs.value.set(target.id, el)
  }
}

const getChainTreeRefForTarget = (target) => {
  if (!target) return null
  return chainTreeRefs.value.get(target.id)
}

// 数据集字段选项（使用实际的数据集字段）
const getFieldOptions = (item) => {
  if (datasetFields.value && datasetFields.value.length > 0) {
    return datasetFields.value.map(f => ({
      label: f.label || f.name,
      value: f.name
    }))
  }
  return []
}

// 获取数据来源的展示文本
const getSourceLabel = (source) => {
  const labels = { dataset_field: '数据集字段', chain_result: '请求链结果', model_output: '模型输出', manual: '手动输入' }
  return labels[source] || source
}

// 获取 RAG 固定变量的字段映射展示文本
const getRagFieldMappingText = (item) => {
  if (item.source === 'dataset_field') {
    return item.source_config?.field || '(未选择字段)'
  }
  if (item.source === 'model_output') {
    return '模型输出'
  }
  return '-'
}

// 加载模型变量
const loadModelVariables = async () => {
  const modelId = form.value?.model_provider
  if (!modelId) {
    modelVariables.value = []
    return
  }

  try {
    const res = await modelApi.getVariables(modelId)
    modelVariables.value = res.data?.variables || res.variables || []
  } catch (e) {
    console.error('加载模型变量失败:', e)
    modelVariables.value = []
  }
}

// 一键迁移旧版配置结构：调后端迁移端点（steps 合并进 score_details、pre_scripts 转脚本输出目标行），成功后重载表单
const migrateLegacyConfig = async () => {
  if (!form.value.id || migratingConfig.value) return
  migratingConfig.value = true
  try {
    await taskApi.migrateConfig(form.value.id)
    ElMessage.success('旧版配置已迁移')
    // 重新拉取任务配置并重载表单（迁移后 steps/pre_scripts 置空、提示条自动消失）
    await openDialog(currentEditRow.value)
  } catch (e) {
    ElMessage.error(e.message || '迁移失败')
  } finally {
    migratingConfig.value = false
  }
}

const submitForm = async () => {
  if (submitting.value) return
  submitting.value = true
  try {
    await formRef.value.validate()
    // RAG 评测至少需要配置 rag_endpoint 或被评测模型
    if (form.value.evaluation_type === 'rag') {
      const hasRagEndpoint = form.value.rag_config?.rag_endpoint?.trim()
      const hasModel = form.value.model_provider?.trim()
      if (!hasRagEndpoint && !hasModel) {
        ElMessage.error('RAG 评测需要配置 RAG 接口地址 或 选择被评测模型（至少一项）')
        return
      }
    }
    // 插件评测：按来源校验评测对象
    if (form.value.evaluation_type === 'plugin') {
      const pc = form.value.plugin_config || {}
      if (pc.source === 'manual') {
        if (!pc.plugin_path?.trim() && !pc.skill_content?.trim()) {
          ElMessage.error('插件评测需要填写插件路径或内联 SKILL.md 内容（至少一项）')
          return
        }
      } else if (pc.source === 'marketplace') {
        if (!pc.market_id) {
          ElMessage.error('插件评测（插件市场）需要选择插件市场')
          return
        }
        if (!pc.market_plugin) {
          ElMessage.error('插件评测（插件市场）需要选择要评测的插件或 skill')
          return
        }
      } else if (pc.source === 'trace') {
        if (!form.value.dataset_id) {
          ElMessage.error('会话 Trace 评测需要选择数据集（每行提供一个会话 trace）')
          return
        }
      } else if (!form.value.dataset_id) {
        ElMessage.error('插件评测（数据集批量）需要选择包含 plugin_path/skill_content 字段的数据集')
        return
      }
    }
    // 安全评测（M3 QE3-1）：被攻击模型必填 + 样本来源与数据集一致性 + 类目非空
    if (form.value.evaluation_type === 'safety') {
      const sc = form.value.safety_config || {}
      if (!form.value.model_provider?.trim()) {
        ElMessage.error('安全评测需要选择被评测模型（红队攻击目标）')
        return
      }
      if (!Array.isArray(sc.categories) || sc.categories.length === 0) {
        ElMessage.error('安全评测至少选择一个攻击面类目')
        return
      }
      if (sc.sample_source === 'dataset' && !form.value.dataset_id) {
        ElMessage.error('攻击样本来源为「数据集」时必须选择数据集（可由内置样本库一键导入后编辑）')
        return
      }
      if (sc.sample_source === 'pack' && form.value.dataset_id) {
        form.value.dataset_id = null
      }
    }
    // 校验：所有引用到数据集字段的变量配置必须指定字段
    const datasetFieldVars = (form.value.judge_variable_config || []).filter(
      v => v.source === 'dataset_field' && (!v.source_config?.field || !v.source_config.field.toString().trim())
    )
    if (datasetFieldVars.length > 0) {
      const names = datasetFieldVars.map(v => v.name || v.key).join('、')
      ElMessage.error(`以下变量的数据来源为「数据集字段」但未选择字段：${names}`)
      return
    }
    // 校验：脚本输出的待评测数据行必须选择脚本且输入映射完整
    const scriptTargetProblems = validateScriptTargets()
    if (scriptTargetProblems.length > 0) {
      ElMessage.error(scriptTargetProblems[0])
      return
    }
    // 校验：AI 裁判员选择「使用数据集字段」作为待评测内容来源时必须指定字段
    if (
      form.value.evaluation_type === 'judge' &&
      form.value.model_output_source === 'dataset_field' &&
      !(form.value.model_output_field || '').trim()
    ) {
      ElMessage.error('待评测内容来源为「使用数据集字段」时，请选择作为模型输出的数据集字段')
      return
    }
    // 复制表单数据并转换 judge_variable_config 为字典格式
    const submitData = JSON.parse(JSON.stringify(form.value))
    // 行级并发数：编辑存量任务时该字段可能为 null/缺失，统一回填默认 4
    submitData.concurrency = Number(submitData.concurrency) || 4
    // 待评测内容来源：仅 AI 裁判员评测使用；其它类型传 null，避免影响执行逻辑
    if (submitData.evaluation_type === 'judge') {
      submitData.model_output_source = submitData.model_output_source || 'model_call'
      submitData.model_output_field = submitData.model_output_source === 'dataset_field'
        ? (submitData.model_output_field || '')
        : ''
    } else {
      submitData.model_output_source = null
      submitData.model_output_field = null
    }
    submitData.judge_variable_config = convertToDictConfig(submitData.judge_variable_config)
    // 待评测数据：脚本输出行 source_config 提交前清理（过滤空映射、参数值类型识别）
    submitData.evaluation_targets = cleanEvaluationTargetsForSubmit(submitData.evaluation_targets)
    // 新版契约：pre_scripts 与 steps 一律不再提交（待评测数据是唯一事实源；维度顺序即评分步骤）
    submitData.pre_scripts = null
    submitData.steps = null
    // 清理空依赖（没有 source_dimension 的依赖项）
    submitData.evaluation_metrics = submitData.evaluation_metrics.map(m => {
      const cleaned = { ...m }
      if (cleaned.dependencies) {
        cleaned.dependencies = cleaned.dependencies.filter(
          d => d.source_dimension && d.source_dimension.trim() !== ''
        )
      }
      return cleaned
    })
    // RAG 评测不使用评测指令配置，传后端默认值（judge_variable_config 保留 RAG 变量配置）
    if (submitData.evaluation_type === 'rag') {
      submitData.evaluation_scene = ''
      submitData.scene_desc = ''
      submitData.max_score = 10
      submitData.score_details = ''
      submitData.evaluation_metrics = []
      submitData.judge_prompt = ''
    }
    // 成功率评测不使用评测指令配置，传后端默认值
    if (submitData.evaluation_type === 'success_rate') {
      submitData.evaluation_scene = ''
      submitData.scene_desc = ''
      submitData.max_score = 10
      submitData.score_details = ''
      submitData.evaluation_metrics = []
      submitData.judge_prompt = ''
      submitData.template_id = ''
    }
    // 二分分类评测不使用裁判配置，清空相关字段
    if (submitData.evaluation_type === 'binary_classification') {
      submitData.judge_model = ''
      submitData.judge_count = 0
      submitData.judge_variable_config = {}
      submitData.evaluation_scene = ''
      submitData.scene_desc = ''
      submitData.max_score = 10
      submitData.score_details = ''
      submitData.evaluation_metrics = []
      submitData.judge_prompt = ''
      submitData.template_id = ''
      // 新格式：直接提交标签配置
      submitData.binary_metrics_config = {
        expected_label: form.value.binary_expected_label,
        predicted_label: form.value.binary_predicted_label,
        positive_value: form.value.binary_positive_value || 'true',
        label_type: form.value.binary_label_type || 'boolean'
      }
    }
    // Agent 评测（长会话评测）：裁判直接评会话，保留评测指令五要素/裁判提示词/裁判变量配置/模板，
    // 清空被测模型相关配置（长会话不调用被测模型）
    if (submitData.evaluation_type === 'agent') {
      submitData.model_provider = ''
      submitData.model_endpoint = ''
      submitData.eval_model_config = null
      // 裁判数量正常传递（默认 1）
      if (!submitData.judge_count || submitData.judge_count < 1) {
        submitData.judge_count = 1
      }
      // 裁判引擎默认 Claude Code Agent
      submitData.judge_engine = submitData.judge_engine || 'claude_agent'
      // 会话背景说明注入场景描述（供裁判提示词使用）
      submitData.scene_desc = submitData.agent_config?.session_context || submitData.scene_desc
    }
    // 安全评测（M3 QE3-1）：清空裁判相关配置、保留 safety_config 与被评测模型
    // （模型是红队攻击目标）；pack 直跑不带数据集
    if (submitData.evaluation_type === 'safety') {
      submitData.judge_model = ''
      submitData.judge_count = 0
      submitData.judge_variable_config = {}
      submitData.evaluation_scene = ''
      submitData.scene_desc = ''
      submitData.max_score = 10
      submitData.score_details = ''
      submitData.evaluation_metrics = []
      submitData.judge_prompt = ''
      submitData.template_id = ''
      submitData.model_output_source = null
      submitData.model_output_field = null
      if (submitData.safety_config?.sample_source === 'pack') {
        submitData.dataset_id = null
      }
      submitData.safety_config = submitData.safety_config || {}
    }
    // 插件评测：按来源组装 plugin_config（source 为表单态不提交），清空未用配置
    if (submitData.evaluation_type === 'plugin') {
      const pc = submitData.plugin_config || {}
      const source = pc.source || 'manual'
      submitData.plugin_config = {
        // trace 口径深度由服务端固定（模型判定=standard 静态 + LLM 评审，离线=quick 仅静态），
        // 这里写的是「深度口径固定」的登记值，不是可选项
        depth: source === 'trace' ? 'standard' : (pc.depth || 'quick'),
        source,
        plugin_path: source === 'manual' ? (pc.plugin_path || '').trim() : '',
        skill_content: source === 'manual' ? (pc.skill_content || '') : '',
        market_id: source === 'marketplace' ? pc.market_id : null,
        market_plugin: source === 'marketplace' ? (pc.market_plugin || '').trim() : '',
        trace_judgement: source === 'trace' ? (pc.trace_judgement || 'model') : null,
        trace_concurrency: source === 'trace' && pc.trace_concurrency != null
          ? Number(pc.trace_concurrency)
          : null
      }
      submitData.judge_count = 0
      submitData.judge_variable_config = {}
      submitData.evaluation_scene = ''
      submitData.scene_desc = ''
      submitData.max_score = 10
      submitData.score_details = ''
      submitData.evaluation_metrics = []
      submitData.judge_prompt = ''
      submitData.template_id = ''
    }
    // 数据集空值统一归一化（FK 列空串会触发外键违规）
    if (!submitData.dataset_id) {
      submitData.dataset_id = null
    }
    // M5 QE5-1 版本绑定提交：跟随最新显式发 null（服务端 None=跟随最新）；
    // 无数据集（如安全评测 pack 直跑）时锁定号一并清空
    submitData.dataset_version_no = submitData.dataset_id && datasetVersionMode.value === 'locked'
      ? (submitData.dataset_version_no ?? null)
      : null
    // M1 门禁配置：表单态收集为落库形状（null=未启用）
    submitData.gate_config = collectGateConfig()
    // M2 终态校验器：表单态收集为落库形状（null=未配置）
    submitData.verifiers = collectVerifiers()
    // M2 轨迹断言：仅 agent 类型收集（非 agent 显式置 null——切类型时清空，
    // 服务端对非 agent 的非空配置按 400 拒绝）
    submitData.trajectory_config = submitData.evaluation_type === 'agent'
      ? collectTrajectoryConfig()
      : null
    // M2 QE2-3 行级多 Trial：仅 judge 类型提交（非 judge 显式置 null——
    // 服务端对非 judge 的 trials>1 按 400 拒绝）
    submitData.trials = submitData.evaluation_type === 'judge'
      ? (Number(form.value.trials) > 1 ? Number(form.value.trials) : null)
      : null
    // M2 QE2-4 裁判治理：仅 judge/agent 提交（非 judge/agent 显式发 false——
    // 服务端 _validate_judge_governance 对其他类型按 400 拒绝）
    const governanceApplies = submitData.evaluation_type === 'judge' || submitData.evaluation_type === 'agent'
    submitData.judge_guard_disabled = governanceApplies ? !!form.value.judge_guard_disabled : false
    submitData.judge_canary = governanceApplies ? !!form.value.judge_canary : false
    // 对话评测：使用裁判配置，保留 judge_variable_config
    if (submitData.evaluation_type === 'dialogue') {
      submitData.template_id = ''
    }
    if (isEdit.value) {
      await taskApi.update(form.value.id, submitData)
      ElMessage.success('更新成功')
    } else {
      await taskApi.create(submitData)
      ElMessage.success('创建成功')
    }
    // 任务保存成功后，真正删除待删除的模板
    if (pendingDeleteTemplateId.value) {
      try {
        await instructionApi.delete(pendingDeleteTemplateId.value)
        ElMessage.success('模板已删除')
        pendingDeleteTemplateId.value = null
      } catch (e) {
        console.error('删除模板失败:', e)
      }
    }
    dialogVisible.value = false
    loadTasks()
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

const runTask = async (row) => {
  runningId.value = row.id
  try {
    await taskApi.run(row.id)
    ElMessage.success('任务已开始执行')
    startPolling(row.id) // 开始轮询
    startWebSocket(row.id) // 建立 WebSocket 连接
    loadTasks()
  } catch (e) {
    ElMessage.error(e.message || '启动失败')
  } finally {
    runningId.value = null
  }
}

const stopTask = async (row) => {
  stoppingId.value = row.id
  try {
    await taskApi.stop(row.id)
    stopPolling(row.id) // 停止轮询
    stopWebSocket(row.id) // 关闭 WebSocket
    ElMessage.success('任务已停止')
    loadTasks()
  } catch (e) {
    ElMessage.error(e.message || '停止失败')
  } finally {
    stoppingId.value = null
  }
}

const deleteTask = async (row) => {
  try {
    await ElMessageBox.confirm('确定要删除该任务吗？', '提示', { type: 'warning' })
    deletingId.value = row.id
    await taskApi.delete(row.id)
    ElMessage.success('删除成功')
    loadTasks()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  } finally {
    deletingId.value = null
  }
}

// 获取进度条状态
const getProgressStatus = (row) => {
  if (row.status === 'failed') return 'exception'
  if (row.status === 'completed') return 'success'
  return ''
}

const openExecutionHistory = async (row) => {
  executionHistoryVisible.value = true
  executionHistoryLoading.value = true
  executionHistoryList.value = []
  try {
    const res = await taskApi.executions(row.id, { limit: 50 })
    executionHistoryList.value = Array.isArray(res) ? res : []
  } catch (e) {
    ElMessage.error('获取执行历史失败')
  } finally {
    executionHistoryLoading.value = false
  }
}

// ==================== 任务定时（M5 QE5-3） ====================
const scheduleVisible = ref(false)
const scheduleLoading = ref(false)
const scheduleSaving = ref(false)
const scheduleRemoving = ref(false)
const scheduleTask = ref(null)
// 服务端返回的完整定时信息（next_run_at/last_*/history）
const scheduleInfo = ref(null)
// cron 五段表单（分 时 日 月 周）+ 启用开关 + 时段限制（空=不限）
const scheduleForm = reactive({ cron: ['*', '*', '*', '*', '*'], enabled: true, window_start: '', window_end: '' })
const schedulePreset = ref('')
const CRON_FIELDS = ['分', '时', '日', '月', '周']
const SCHEDULE_PRESETS = [
  { label: '每天 08:00', cron: ['0', '8', '*', '*', '*'] },
  { label: '工作日 09:00', cron: ['0', '9', '*', '*', '1-5'] },
  { label: '每周一 08:00', cron: ['0', '8', '*', '*', '1'] },
  { label: '每小时', cron: ['0', '*', '*', '*', '*'] },
]

const SCHEDULE_STATUS = {
  dispatched: { label: '已投递', type: 'success' },
  skipped_running: { label: '互斥跳过', type: 'warning' },
  skipped_window: { label: '窗口外跳过', type: 'info' },
  submit_rejected: { label: '提交被拒', type: 'danger' },
  task_unavailable: { label: '任务不可用', type: 'danger' },
}
const scheduleStatusLabel = (s) => SCHEDULE_STATUS[s]?.label || s
const scheduleStatusTagType = (s) => SCHEDULE_STATUS[s]?.type || 'info'

const applySchedulePreset = (label) => {
  const preset = SCHEDULE_PRESETS.find(p => p.label === label)
  if (preset) scheduleForm.cron = [...preset.cron]
}

const openSchedule = async (row) => {
  scheduleTask.value = row
  scheduleVisible.value = true
  scheduleLoading.value = true
  scheduleInfo.value = null
  schedulePreset.value = ''
  scheduleForm.cron = ['*', '*', '*', '*', '*']
  scheduleForm.enabled = true
  scheduleForm.window_start = ''
  scheduleForm.window_end = ''
  try {
    const res = await taskApi.getSchedule(row.id)
    scheduleInfo.value = res
    const parts = String(res.cron || '').trim().split(/\s+/)
    scheduleForm.cron = parts.length === 5 ? parts : ['*', '*', '*', '*', '*']
    scheduleForm.enabled = !!res.enabled
    scheduleForm.window_start = res.window_start || ''
    scheduleForm.window_end = res.window_end || ''
  } catch (e) {
    // 404=任务未配置定时：展示空表单供新建；其余错误才提示
    if (e?.response?.status !== 404) ElMessage.error('获取定时配置失败')
  } finally {
    scheduleLoading.value = false
  }
}

const saveSchedule = async () => {
  const cron = scheduleForm.cron.map(s => String(s || '').trim()).join(' ')
  if (cron.split(/\s+/).length !== 5) {
    ElMessage.warning('cron 表达式须为 5 段（分 时 日 月 周）')
    return
  }
  if ((scheduleForm.window_start ? 1 : 0) !== (scheduleForm.window_end ? 1 : 0)) {
    ElMessage.warning('时段限制须同时配置开始与结束（或都留空）')
    return
  }
  scheduleSaving.value = true
  try {
    const res = await taskApi.saveSchedule(scheduleTask.value.id, {
      cron,
      enabled: scheduleForm.enabled,
      window_start: scheduleForm.window_start || null,
      window_end: scheduleForm.window_end || null,
    })
    scheduleInfo.value = res
    ElMessage.success('定时配置已保存')
  } catch (e) {
    ElMessage.error(e?.message || '保存定时配置失败')
  } finally {
    scheduleSaving.value = false
  }
}

const removeSchedule = async () => {
  try {
    await ElMessageBox.confirm('删除后任务将不再定时触发，确定删除？', '删除定时', { type: 'warning' })
  } catch {
    return
  }
  scheduleRemoving.value = true
  try {
    await taskApi.deleteSchedule(scheduleTask.value.id)
    scheduleInfo.value = null
    scheduleForm.cron = ['*', '*', '*', '*', '*']
    scheduleForm.enabled = true
    scheduleForm.window_start = ''
    scheduleForm.window_end = ''
    ElMessage.success('定时配置已删除')
  } catch (e) {
    ElMessage.error('删除定时配置失败')
  } finally {
    scheduleRemoving.value = false
  }
}

const handleManualHoursChange = async (row) => {
  if (!row.id) {
    ElMessage.warning('执行记录ID缺失')
    return
  }
  manualHoursSavingId.value = row.id
  try {
    await efficiencyApi.updateManualHours({
      record_id: row.id,
      hours: Number(row.estimated_manual_hours) || 0,
    })
    ElMessage.success('保存成功')
  } catch (e) {
    ElMessage.error('保存失败')
  } finally {
    manualHoursSavingId.value = null
  }
}

const getEfficiencyColor = (row) => {
  const eff = getEfficiency(row)
  return eff >= 0 ? '#67c23a' : '#f56c6c'
}

const getEfficiency = (row) => {
  const manual = Number(row.estimated_manual_hours) || 0
  const system = row.duration_ms ? (row.duration_ms / 3600000) : 0
  return (manual - system).toFixed(2)
}

const formatEfficiency = (row) => {
  const eff = getEfficiency(row)
  return eff >= 0 ? `+${eff}` : eff
}

const formatHours = (ms) => {
  if (!ms) return '-'
  return (ms / 3600000).toFixed(2)
}

const formatDateTime = (value) => {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return String(value)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

const handleSelectionChange = (selection) => {
  selectedTasks.value = selection
}

const batchRun = async () => {
  if (batchOpLoading.value) return
  batchOpLoading.value = true
  try {
    const ids = selectedTasks.value.map(t => t.id)
    const res = await batchApi.run(ids)
    ElMessage.success(`成功执行 ${res.success} 个任务`)
    loadTasks()
  } catch (e) {
    ElMessage.error('批量执行失败')
  } finally {
    batchOpLoading.value = false
  }
}

const batchStop = async () => {
  if (batchOpLoading.value) return
  batchOpLoading.value = true
  try {
    const ids = selectedTasks.value.map(t => t.id)
    const res = await batchApi.stop(ids)
    ElMessage.success(`成功停止 ${res.success} 个任务`)
    loadTasks()
  } catch (e) {
    ElMessage.error('批量停止失败')
  } finally {
    batchOpLoading.value = false
  }
}

const batchDelete = async () => {
  if (batchOpLoading.value) return
  try {
    await ElMessageBox.confirm('确定要删除选中的任务吗？', '提示', { type: 'warning' })
    batchOpLoading.value = true
    const ids = selectedTasks.value.map(t => t.id)
    const res = await batchApi.delete(ids)
    ElMessage.success(`成功删除 ${res.success} 个任务`)
    loadTasks()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('批量删除失败')
  } finally {
    batchOpLoading.value = false
  }
}

onMounted(async () => {
  loadTasks()
  await loadOptions()
  // M5 QE5-1：数据集版本抽屉「从此版本建任务」跳转——query 携带 dataset_id
  // （+dataset_version_no）时预选数据集并锁定该版本，直接打开新建弹窗
  const qDatasetId = route.query.dataset_id
  if (qDatasetId && datasets.value.some(ds => ds.id === String(qDatasetId))) {
    await openDialog(null)
    form.value.dataset_id = String(qDatasetId)
    const qVersion = Number(route.query.dataset_version_no)
    if (Number.isInteger(qVersion) && qVersion > 0) {
      form.value.dataset_version_no = qVersion
      datasetVersionMode.value = 'locked'
    }
  }
})

onUnmounted(() => {
  stopAll() // 停止所有轮询
  stopAllWebSocket() // 停止所有 WebSocket 连接
})

const addCriteriaLevel = (metric) => {
  if (!metric.score_criteria) {
    metric.score_criteria = {}
  }
  const levels = Object.keys(metric.score_criteria)
  if (levels.length === 0) {
    // First time: pre-fill default template
    metric.score_criteria['优秀'] = { min: 9, max: 10, desc: '表现优秀' }
    metric.score_criteria['良好'] = { min: 7, max: 8, desc: '表现良好' }
    metric.score_criteria['及格'] = { min: 5, max: 6, desc: '达到要求' }
    metric.score_criteria['不及格'] = { min: 0, max: 4, desc: '未达标' }
    return
  }
  // Existing levels: add custom one
  let levelSeq = levels.length + 1
  while (Object.prototype.hasOwnProperty.call(metric.score_criteria, `等级${levelSeq}`)) {
    levelSeq += 1
  }
  metric.score_criteria[`等级${levelSeq}`] = { min: 0, max: 0, desc: '' }
}

const removeCriteriaLevel = (metric, level) => {
  if (!metric.score_criteria) return
  delete metric.score_criteria[level]
}

const addDependency = (metric) => {
  if (!metric.dependencies) {
    metric.dependencies = []
  }
  metric.dependencies.push({
    source_dimension: '',
    condition: 'eq',
    value: 0,
    override_value: 0,
  })
}

const removeDependency = (metric, index) => {
  metric.dependencies.splice(index, 1)
}
</script>

<style scoped>
.tasks-page {
  max-width: 1400px;
  margin: 0 auto;
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

/* 旧版配置结构提示条 */
.legacy-config-alert {
  margin-bottom: 12px;
}

.legacy-config-alert-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}

/* 待评测数据行下的脚本输出编排卡片 */
.script-target-card {
  border: 1px solid #ebeef5;
  border-left: 3px solid #409eff;
  border-radius: 6px;
  background: #fafbfc;
  margin: -4px 0 10px;
  padding: 4px 6px;
}

.script-target-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}

.pre-script-card-body {
  padding: 4px 8px 8px;
}

.pre-script-desc {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}

.pre-script-warning {
  font-size: 12px;
  color: #e6a23c;
  background: #fdf6ec;
  border-radius: 4px;
  padding: 4px 8px;
  margin-bottom: 8px;
}

.pre-script-block {
  margin-bottom: 12px;
}

.pre-script-block-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.pre-script-field-row,
.pre-script-param-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.pre-script-field-name {
  min-width: 110px;
  max-width: 160px;
  font-size: 13px;
  color: #303133;
  font-family: monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pre-script-field-desc {
  font-size: 12px;
  color: #909399;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hub-pattern-tag {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  flex-shrink: 0;
}

.pre-script-empty {
  font-size: 12px;
  color: #c0c4cc;
  padding: 2px 0;
}

.pre-script-output-row {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 4px;
}

.pre-script-output-key {
  font-size: 12px;
  font-family: monospace;
  color: #606266;
  min-width: 130px;
}

.pre-script-output-desc {
  font-size: 12px;
  color: #909399;
}

.pre-script-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
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

/* 任务对话框最大宽度 */
:deep(.task-dialog) {
  max-width: 95vw;
}

/* 修复对话框内容溢出 - 确保所有文本正确换行 */
:deep(.task-dialog .el-dialog__body) {
  overflow-wrap: break-word;
  word-break: break-word;
  max-height: calc(90vh - 140px);
  overflow-y: auto;
}

:deep(.task-dialog .el-form-item__content) {
  overflow-wrap: break-word;
  word-break: break-word;
  width: 100%;
}

/* 评测维度样式 - 两列网格布局 */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
  width: 100%;
  box-sizing: border-box;
}

/* 确保评测维度的el-form-item内容区域充满 */
:deep(.el-form-item:has(.metrics-grid) .el-form-item__content) {
  width: 100%;
  max-width: none;
}

.metric-card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px 14px;
  transition: border-color 0.2s, box-shadow 0.2s, opacity 0.2s;
  cursor: move;
}

.metric-card:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}

.metric-card-dragging {
  opacity: 0.5;
  border-color: #409eff;
}

/* 当只有一个维度时，不使用两列布局 */
.metrics-grid:has(.metrics-empty) {
  grid-template-columns: 1fr;
}

.metric-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.metric-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.drag-handle {
  cursor: move;
  color: #909399;
  font-size: 16px;
}

.drag-handle:hover {
  color: #409eff;
}

.metric-index {
  width: 22px;
  height: 22px;
  background: linear-gradient(135deg, #409eff, #3498db);
  color: #fff;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
}

.metric-title {
  font-weight: 600;
  color: #303133;
  font-size: 13px;
}

.metric-delete {
  font-size: 12px;
  padding: 2px 6px;
}

.metric-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.metric-field-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.metric-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.metric-field label {
  font-size: 11px;
  color: #909399;
  font-weight: 500;
}

.metric-name {
  flex: 1;
  min-width: 0;
}

.metric-field-xs {
  flex: 0 0 100px;
}

.metric-desc {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.metric-desc label {
  font-size: 11px;
  color: #909399;
  font-weight: 500;
}

.metrics-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 30px;
  color: #c0c4cc;
  background: #fafafa;
  border-radius: 8px;
  border: 1px dashed #dcdfe6;
  gap: 8px;
}

.metrics-empty .el-icon {
  font-size: 24px;
}

.add-metric-btn {
  width: 100%;
  justify-content: center;
  padding: 10px;
  border-style: dashed;
  color: #606266;
}

.add-metric-btn:hover {
  color: #409eff;
  border-color: #409eff;
  background: #f0f9ff;
}

/* 评分标准与评测维度区：未配置待评测数据时整区置灰 */
.section-gated {
  position: relative;
  width: 100%;
}

.section-gated-content {
  transition: opacity 0.2s;
}

.section-gated.is-disabled .section-gated-content {
  opacity: 0.45;
  pointer-events: none;
  user-select: none;
}

.section-gated-overlay {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: rgba(250, 251, 252, 0.72);
  color: #909399;
  font-size: 14px;
  font-weight: 500;
  border-radius: 6px;
}

/* 评测目标配置样式 - 参考裁判变量配置 */
.evaluation-targets-config {
  padding: 15px;
  background: #fafafa;
  border-radius: 8px;
  margin: 10px 0;
  width: 100%;
  box-sizing: border-box;
}

.evaluation-targets-config .config-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  width: 100%;
}

.evaluation-targets-config .config-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.evaluation-targets-config .config-row-title {
  font-weight: 500;
  color: #909399;
  font-size: 12px;
  padding: 8px 0;
  margin-bottom: 8px;
  border-bottom: 1px solid #ebeef5;
  width: 100%;
}

.evaluation-targets-config .config-items {
  max-height: 280px;
  overflow-y: auto;
  width: 100%;
}

.evaluation-targets-config .variable-row {
  margin-bottom: 10px;
  align-items: center;
  width: 100%;
}

.evaluation-targets-config .el-select,
.evaluation-targets-config .el-input {
  width: 100%;
}

.evaluation-targets-config .placeholder-text {
  color: #c0c4cc;
  display: flex;
  align-items: center;
  height: 32px;
  font-size: 12px;
}

.evaluation-targets-config .action-col {
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 裁判模型变量配置样式 */
.judge-variable-config {
  padding: 15px;
  background: #fafafa;
  border-radius: 8px;
  margin: 10px 0;
}

.judge-variable-config .config-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
}

.judge-variable-config .config-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.judge-variable-config .config-row-title {
  font-weight: 500;
  color: #909399;
  font-size: 12px;
  padding: 8px 0;
  margin-bottom: 10px;
  border-bottom: 1px solid #ebeef5;
}

.judge-variable-config .config-items {
  max-height: 300px;
  overflow-y: auto;
}

.judge-variable-config .variable-row {
  margin-bottom: 12px;
  align-items: center;
}

.judge-variable-config .el-select,
.judge-variable-config .el-input {
  width: 100%;
}

.judge-variable-config .el-select .el-input__wrapper {
  min-width: 80px;
}

.judge-variable-config .prompt-radio {
  display: flex;
  gap: 10px;
}

.judge-variable-config .prompt-radio .el-radio {
  margin-right: 0;
}

.judge-variable-config .placeholder-text {
  color: #c0c4cc;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 32px;
}

/* 操作列对齐 */
.judge-variable-config .action-col {
  display: flex;
  align-items: center;
  justify-content: center;
}

.judge-variable-config .action-col .el-button {
  width: 60px;
}

:deep(.el-divider__text) {
  font-weight: 600;
  color: #303133;
}

/* 指令预览样式 */
.prompt-preview {
  background: #f5f7fa;
  padding: 15px;
  border-radius: 6px;
  max-height: 400px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
}

/* M1 门禁配置块（表单内全宽编辑区） */
.gate-config-block {
  width: 100%;
}

.gate-config-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.gate-config-tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}

.gate-mode-group {
  margin: 10px 0;
}

.gate-add-btn {
  margin: 8px 0;
}

/* M5 QE5-3 任务定时 cron 编辑器 */
.cron-row {
  display: flex;
  gap: 8px;
  width: 100%;
}
.cron-field {
  flex: 1;
  text-align: center;
}
.cron-field-label {
  display: block;
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
.cron-tip {
  width: 100%;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
  margin-top: 4px;
}

/* M2 终态校验器配置区 */
.verifier-config-block {
  width: 100%;
}
.verifier-card {
  margin-bottom: 8px;
}
.verifier-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.verifier-header-spacer {
  flex: 1;
}
.verifier-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.verifier-label {
  color: #606266;
  font-size: 13px;
}
.verifier-evidence-preview {
  max-height: 260px;
  overflow: auto;
  background: #f5f7fa;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}

/* M2 轨迹断言配置区（agent 类型） */
.trajectory-config-block {
  width: 100%;
  padding: 8px 0;
}
.trajectory-sub-title {
  color: #303133;
  font-size: 13px;
  font-weight: 600;
  margin: 10px 0 6px;
}

/* 请求链变量树形选择样式 */
.chain-var-tree {
  max-height: 300px;
  overflow-y: auto;
}

.no-chain-msg {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  padding: 10px 0;
}

.chain-var-selected {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #EBEEF5;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* 评测背景 tag 区域布局 */
.effect-tags {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}

/* 插件评测：拖拽上传区 */
.plugin-upload {
  width: 100%;
}

.plugin-upload :deep(.el-upload-dragger) {
  padding: 12px 16px;
}

.plugin-upload-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.plugin-upload-icon {
  font-size: 32px;
  color: #a8abb2;
}

.plugin-upload-text {
  font-size: 13px;
  color: #606266;
}

.plugin-upload-text em {
  color: #409eff;
  font-style: normal;
}

.plugin-upload-hint {
  font-size: 12px;
  color: #909399;
}
</style>
