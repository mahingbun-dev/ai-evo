<template>
  <div class="ab-test-container">
    <!-- 顶部操作栏 -->
    <div class="ab-test-header">
      <div class="header-title">
        <el-icon :size="20"><Switch /></el-icon>
        <span>A/B 对比测试</span>
        <!-- M5 QE5-2 模式切换：模型对比（手工会话式，保留） / 模板版本对照（任务化实验） -->
        <el-radio-group v-model="pageMode" size="small" style="margin-left: 16px">
          <el-radio-button value="model">模型对比</el-radio-button>
          <el-radio-button value="template">模板版本对照</el-radio-button>
        </el-radio-group>
        <span class="subtitle" v-if="pageMode === 'model'">选择两个模型，输入变量，对比实时输出结果</span>
        <span class="subtitle" v-else>同一评测任务 × 两个模板版本，生成对照实验任务（执行历史留存可比）</span>
      </div>
      <el-button v-if="pageMode === 'model'" :icon="Clock" @click="openHistory">历史记录</el-button>
      <el-button v-else :icon="Clock" @click="openTemplateHistory">对照实验记录</el-button>
    </div>

    <!-- 模型选择区域（手工会话式模式，保留旧入口） -->
    <el-card v-if="pageMode === 'model'" class="config-card" shadow="never">
      <el-row :gutter="24">
        <!-- 模型 A -->
        <el-col :span="12">
          <div class="model-selector">
            <div class="model-label model-a-label">
              <el-tag type="primary" size="large" class="model-tag">A</el-tag>
              <span>模型 A</span>
            </div>
            <el-select
              v-model="modelAId"
              placeholder="选择模型 A"
              filterable
              style="width: 100%"
              @change="onModelAChange"
            >
              <el-option
                v-for="m in modelList"
                :key="m.id"
                :label="m.name"
                :value="m.id"
              >
                <span>{{ m.name }}</span>
                <span class="model-opt-provider">{{ m.provider }}</span>
              </el-option>
            </el-select>
          </div>
        </el-col>

        <!-- 模型 B -->
        <el-col :span="12">
          <div class="model-selector">
            <div class="model-label model-b-label">
              <el-tag type="warning" size="large" class="model-tag">B</el-tag>
              <span>模型 B</span>
            </div>
            <el-select
              v-model="modelBId"
              placeholder="选择模型 B"
              filterable
              style="width: 100%"
              @change="onModelBChange"
            >
              <el-option
                v-for="m in modelList"
                :key="m.id"
                :label="m.name"
                :value="m.id"
              >
                <span>{{ m.name }}</span>
                <span class="model-opt-provider">{{ m.provider }}</span>
              </el-option>
            </el-select>
          </div>
        </el-col>
      </el-row>

      <!-- 共享变量区域 -->
      <div v-if="allVariables.length > 0 || (modelAId && modelBId)" class="variables-section">
        <el-divider content-position="left">
          <el-icon><Setting /></el-icon>
          共享变量配置
          <span class="var-tip">（变量将同时传入两个模型）</span>
        </el-divider>
        <el-form label-width="140px" size="default">
          <VariableInjector
            :variables="allVariables"
            :dataset-fields="[]"
            :initial-data="injectorInitialData"
            @update="onVariableUpdate"
          />
        </el-form>
      </div>

      <!-- 开始按钮 -->
      <div class="action-row">
        <el-button
          type="primary"
          :icon="VideoPlay"
          :disabled="!modelAId || !modelBId || isRunning"
          :loading="isRunning"
          size="large"
          @click="startABTest"
        >
          {{ isRunning ? '测试中...' : '开始 A/B 测试' }}
        </el-button>
        <el-button v-if="isRunning" :icon="VideoPause" @click="stopTest" size="large">停止</el-button>
        <!-- 多轮配置（M2 QE2-3）：≥2 轮时同输入逐轮配对，跑完输出显著性统计 -->
        <div class="rounds-config" v-if="!isRunning">
          <span class="rounds-label">轮数</span>
          <el-input-number v-model="rounds" :min="1" :max="10" size="large" style="width: 110px;" />
          <span class="rounds-tip" v-if="rounds > 1">同输入跑 {{ rounds }} 轮，完成后给出显著性结论（Wilcoxon / McNemar）</span>
        </div>
        <span v-if="!modelAId || !modelBId" class="hint-text">请先选择模型 A 和模型 B</span>
      </div>
    </el-card>

    <!-- 模板版本对照面板（M5 QE5-2，任务化实验）：源任务 × 两模板版本 → 两个变体任务 -->
    <el-card v-if="pageMode === 'template'" class="config-card" shadow="never">
      <el-form label-width="110px">
        <el-form-item label="源评测任务">
          <el-select v-model="tplSourceTaskId" placeholder="选择要对照的评测任务（变体任务由此复制）" filterable style="width: 100%" :loading="tplTasksLoading">
            <el-option v-for="t in tplTasks" :key="t.id" :label="`${t.name}（${t.evaluation_type}）`" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="评测模板">
          <el-select v-model="tplTemplateId" placeholder="选择模板（需已打版）" filterable style="width: 100%" @change="onTplTemplateChange">
            <el-option v-for="t in tplTemplates" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="A 侧版本">
              <el-select v-model="tplVersionA" placeholder="版本 A" style="width: 100%" :disabled="tplVersions.length === 0">
                <el-option v-for="v in tplVersions" :key="v.version_no"
                           :label="`v${v.version_no}${v.changelog ? ' · ' + v.changelog : ''}`" :value="v.version_no" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="B 侧版本">
              <el-select v-model="tplVersionB" placeholder="版本 B" style="width: 100%" :disabled="tplVersions.length === 0">
                <el-option v-for="v in tplVersions" :key="v.version_no"
                           :label="`v${v.version_no}${v.changelog ? ' · ' + v.changelog : ''}`" :value="v.version_no" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="实验备注名">
          <el-input v-model="tplSessionName" placeholder="选填，便于后续查找" maxlength="60" style="max-width: 360px" />
        </el-form-item>
      </el-form>
      <div class="action-row">
        <el-button
          type="primary"
          size="large"
          :loading="tplCreating"
          :disabled="!tplSourceTaskId || !tplTemplateId || !tplVersionA || !tplVersionB || tplVersionA === tplVersionB"
          @click="createTemplateSession"
        >创建对照实验</el-button>
        <span v-if="tplVersionA && tplVersionA === tplVersionB" class="hint-text">对照两版本不能相同</span>
        <span v-else class="hint-text">创建后生成两个变体任务（仅模板派生配置不同），在记录里逐个运行并对比均分</span>
      </div>
    </el-card>

    <!-- 显著性面板（M2 QE2-3）：多轮跑完/历史详情有统计才显示 -->
    <el-card v-if="pageMode === 'model' && significance" class="significance-card" shadow="never">
      <template #header>
        <div class="significance-header">
          <span>显著性统计</span>
          <el-tag size="small" type="info">配对样本 n={{ significance.rounds }} 轮（同输入）</el-tag>
        </div>
      </template>
      <el-alert
        v-if="significance.duration_paired?.conclusion"
        :type="significance.duration_paired.significant ? 'success' : 'info'"
        :closable="false"
        class="significance-alert"
        :title="`耗时：${significance.duration_paired.conclusion}`"
      />
      <el-alert
        v-if="significance.success_mcnemar?.conclusion"
        :type="significance.success_mcnemar.significant ? 'success' : 'info'"
        :closable="false"
        class="significance-alert"
        :title="`成功率：${significance.success_mcnemar.conclusion}`"
      />
      <el-descriptions :column="3" size="small" border style="margin-top: 12px;">
        <el-descriptions-item label="Wilcoxon p（耗时）">
          {{ significance.duration_paired?.wilcoxon_p ?? '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="配对 t p（耗时）">
          {{ significance.duration_paired?.paired_t_p ?? '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="均值差 95% CI（B-A，秒）">
          <span v-if="significance.duration_paired?.diff_ci95">
            [{{ significance.duration_paired.diff_ci95[0] }}, {{ significance.duration_paired.diff_ci95[1] }}]
          </span>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="Cliff's δ（耗时）">
          {{ significance.duration_paired?.cliffs_delta ?? '-' }}
          <el-tag v-if="significance.duration_paired?.delta_magnitude" size="small" style="margin-left: 4px;">
            {{ significance.duration_paired.delta_magnitude }}效应
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="McNemar p（成功）">
          {{ significance.success_mcnemar?.p_value ?? '-' }}
        </el-descriptions-item>
        <el-descriptions-item label="不一致对（A过B败 / A败B过）">
          {{ significance.success_mcnemar?.b_a_pass_b_fail ?? '-' }} / {{ significance.success_mcnemar?.c_a_fail_b_pass ?? '-' }}
        </el-descriptions-item>
      </el-descriptions>
      <div class="significance-note">
        <template v-if="significance.duration_paired?.adequate === false || significance.success_mcnemar?.adequate === false">
          样本量提示：当前轮数偏少，结论仅供参考——连续配对建议 ≥30 轮、成败配对建议不一致对 ≥10 再下判定。
        </template>
        <template v-else>口径：耗时为主检验 Wilcoxon 符号秩（配对 t 需差值近似正态）；成败为 McNemar 精确检验；p&lt;0.05 视为显著。</template>
      </div>
    </el-card>

    <!-- 对比结果区域 -->
    <el-row :gutter="16" class="result-row" v-if="pageMode === 'model' && testStarted">
      <!-- 模型 A 结果 -->
      <el-col :span="12">
        <el-card shadow="never" class="result-card result-card-a">
          <template #header>
            <div class="result-card-header">
              <el-tag type="primary">A</el-tag>
              <span class="model-name">{{ modelAName || '模型 A' }}</span>
              <div class="result-meta" v-if="durationA !== null">
                <el-tag :type="successA ? 'success' : 'danger'" size="small">
                  {{ successA ? '成功' : '失败' }}
                </el-tag>
                <span class="duration-text">{{ durationA }}s</span>
              </div>
              <el-icon v-else-if="loadingA" class="is-loading spin-icon"><Loading /></el-icon>
            </div>
          </template>
          <ModelTestLog :logs="logsA" :is-loading="loadingA" :height="500" />
        </el-card>
      </el-col>

      <!-- 模型 B 结果 -->
      <el-col :span="12">
        <el-card shadow="never" class="result-card result-card-b">
          <template #header>
            <div class="result-card-header">
              <el-tag type="warning">B</el-tag>
              <span class="model-name">{{ modelBName || '模型 B' }}</span>
              <div class="result-meta" v-if="durationB !== null">
                <el-tag :type="successB ? 'success' : 'danger'" size="small">
                  {{ successB ? '成功' : '失败' }}
                </el-tag>
                <span class="duration-text">{{ durationB }}s</span>
              </div>
              <el-icon v-else-if="loadingB" class="is-loading spin-icon"><Loading /></el-icon>
            </div>
          </template>
          <ModelTestLog :logs="logsB" :is-loading="loadingB" :height="500" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 保存记录按钮 -->
    <div class="save-row" v-if="pageMode === 'model' && testDone">
      <el-button type="success" :icon="DocumentAdd" :disabled="isRunning" @click="startSave">
        保存本次对比记录
      </el-button>
      <el-button @click="resetTest" :icon="RefreshRight">重新测试</el-button>
    </div>

    <!-- 保存记录命名弹窗 -->
    <el-dialog v-model="saveNameVisible" title="保存对比记录" width="480px" :close-on-click-modal="false">
      <el-form label-width="80px">
        <el-form-item label="记录名称">
          <el-input
            v-model="saveName"
            placeholder="选填，便于后续查找（留空则不设名称）"
            clearable
            maxlength="60"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="saveNameVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="confirmSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 历史记录弹窗 -->
    <el-dialog v-model="historyVisible" title="历史 A/B 测试记录" width="960px" top="8vh">
      <div class="history-toolbar">
        <el-input
          v-model="historyKeyword"
          placeholder="搜索记录名称..."
          clearable
          style="width: 240px"
          @keyup.enter="searchHistory"
          @clear="searchHistory"
        >
          <template #suffix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button :icon="Refresh" size="small" @click="searchHistory">刷新</el-button>
      </div>
      <el-table :data="historyList" v-loading="historyLoading" size="small" style="width: 100%">
        <el-table-column label="备注名" prop="name" min-width="120">
          <template #default="{ row }">{{ row.name || '-' }}</template>
        </el-table-column>
        <el-table-column label="模型 A" prop="model_a_name" min-width="140" />
        <el-table-column label="模型 B" prop="model_b_name" min-width="140" />
        <el-table-column label="A 耗时" width="80">
          <template #default="{ row }">
            <span v-if="row.duration_a != null">{{ row.duration_a }}s</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="B 耗时" width="80">
          <template #default="{ row }">
            <span v-if="row.duration_b != null">{{ row.duration_b }}s</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === 'completed' ? 'success' : 'danger'" size="small">
              {{ row.status === 'completed' ? '完成' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" prop="created_at" min-width="160">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="viewHistory(row.id)">查看</el-button>
            <el-button type="danger" link size="small" :loading="deletingHistoryId === row.id" :disabled="deletingHistoryId !== null" @click="deleteHistory(row.id)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="history-pagination">
        <el-pagination
          v-model:current-page="historyPage"
          :page-size="20"
          :total="historyTotal"
          layout="total, prev, pager, next"
          @current-change="loadHistory"
          small
        />
      </div>
    </el-dialog>

    <!-- 历史详情弹窗 -->
    <el-dialog v-model="historyDetailVisible" title="A/B 测试详情" width="1100px" top="4vh">
      <div v-if="historyDetail">
        <!-- 历史显著性结论（M2 QE2-3）：多轮记录保存的统计快照 -->
        <el-alert
          v-if="historyDetail.significance?.duration_paired?.conclusion"
          :type="historyDetail.significance.duration_paired.significant ? 'success' : 'info'"
          :closable="false"
          style="margin-bottom: 12px;"
          :title="`耗时：${historyDetail.significance.duration_paired.conclusion}`"
        />
        <el-alert
          v-if="historyDetail.significance?.success_mcnemar?.conclusion"
          :type="historyDetail.significance.success_mcnemar.significant ? 'success' : 'info'"
          :closable="false"
          style="margin-bottom: 12px;"
          :title="`成功率：${historyDetail.significance.success_mcnemar.conclusion}`"
        />
        <el-row :gutter="16">
          <el-col :span="12">
            <el-card shadow="never" class="result-card result-card-a">
              <template #header>
                <div class="result-card-header">
                  <el-tag type="primary">A</el-tag>
                  <span class="model-name">{{ historyDetail.model_a_name }}</span>
                  <span class="duration-text" v-if="historyDetail.duration_a != null">
                    {{ historyDetail.duration_a }}s
                  </span>
                </div>
              </template>
              <ModelTestLog
                :logs="buildLogsFromResult(historyDetail.result_a)"
                :is-loading="false"
                :height="480"
              />
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="never" class="result-card result-card-b">
              <template #header>
                <div class="result-card-header">
                  <el-tag type="warning">B</el-tag>
                  <span class="model-name">{{ historyDetail.model_b_name }}</span>
                  <span class="duration-text" v-if="historyDetail.duration_b != null">
                    {{ historyDetail.duration_b }}s
                  </span>
                </div>
              </template>
              <ModelTestLog
                :logs="buildLogsFromResult(historyDetail.result_b)"
                :is-loading="false"
                :height="480"
              />
            </el-card>
          </el-col>
        </el-row>
      </div>
    </el-dialog>
    <!-- 模板版本对照实验记录（M5 QE5-2）：两侧变体任务状态/均分 + 运行/删除 -->
    <el-dialog v-model="tplHistoryVisible" title="模板版本对照实验记录" width="1060px" top="6vh">
      <div class="history-toolbar">
        <el-button :icon="Refresh" size="small" @click="loadTemplateHistory">刷新</el-button>
        <span class="tpl-history-tip">「运行」走任务执行全口径（配额/互斥），跑完刷新即见均分对照</span>
      </div>
      <el-table :data="tplHistoryList" v-loading="tplHistoryLoading" size="small" style="width: 100%">
        <el-table-column label="备注名" min-width="120">
          <template #default="{ row }">{{ row.name || '-' }}</template>
        </el-table-column>
        <el-table-column label="源任务" min-width="150">
          <template #default="{ row }">{{ tplTaskName(row.source_task_id) }}</template>
        </el-table-column>
        <el-table-column label="对照版本" width="130" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="primary">v{{ row.template_version_a }}</el-tag>
            vs
            <el-tag size="small" type="warning">v{{ row.template_version_b }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="A 均分 / 状态" width="150">
          <template #default="{ row }">
            <span v-if="comparisonOf(row)?.side_a?.latest_execution">
              {{ comparisonOf(row).side_a.latest_execution.average_score ?? '-' }}
              <el-tag size="small" :type="comparisonOf(row).side_a.latest_execution.status === 'completed' ? 'success' : 'info'">
                {{ comparisonOf(row).side_a.latest_execution.status }}
              </el-tag>
            </span>
            <span v-else>未运行</span>
          </template>
        </el-table-column>
        <el-table-column label="B 均分 / 状态" width="150">
          <template #default="{ row }">
            <span v-if="comparisonOf(row)?.side_b?.latest_execution">
              {{ comparisonOf(row).side_b.latest_execution.average_score ?? '-' }}
              <el-tag size="small" :type="comparisonOf(row).side_b.latest_execution.status === 'completed' ? 'success' : 'info'">
                {{ comparisonOf(row).side_b.latest_execution.status }}
              </el-tag>
            </span>
            <span v-else>未运行</span>
          </template>
        </el-table-column>
        <el-table-column label="实验状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="{ completed: 'success', failed: 'danger', pending: 'info' }[comparisonOf(row)?.state || 'pending']">
              {{ { completed: '已完成', failed: '失败', pending: '进行中/未跑' }[comparisonOf(row)?.state || 'pending'] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" :disabled="tplRunningKey === `${row.id}-a`" :loading="tplRunningKey === `${row.id}-a`" @click="runTplSide(row, 'a')">运行 A</el-button>
            <el-button type="primary" link size="small" :disabled="tplRunningKey === `${row.id}-b`" :loading="tplRunningKey === `${row.id}-b`" @click="runTplSide(row, 'b')">运行 B</el-button>
            <el-button type="danger" link size="small" :loading="tplDeletingId === row.id" :disabled="tplDeletingId !== null" @click="deleteTplSession(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="history-pagination">
        <el-pagination
          v-model:current-page="tplHistoryPage"
          :page-size="20"
          :total="tplHistoryTotal"
          layout="total, prev, pager, next"
          @current-change="loadTemplateHistory"
          small
        />
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Switch, Clock, Setting, VideoPlay, VideoPause, Loading,
  DocumentAdd, RefreshRight, Refresh, Search
} from '@element-plus/icons-vue'
import { modelApi, abTestApi, taskApi, instructionApi } from '@/api'
import appConfig from '@/config'
import VariableInjector from '@/components/VariableInjector.vue'
import { formatTime } from '@/utils/time'
import ModelTestLog from '@/components/ModelTestLog.vue'

// ==================== 页面模式（M5 QE5-2）：model=手工会话式双模型对比（保留），
// template=模板版本对照（任务化实验：源任务 × 两模板版本 → 两个变体任务） ====================
const pageMode = ref('model')

// ==================== 模板版本对照 ====================
const tplTasks = ref([])              // 可选源任务列表
const tplTasksLoading = ref(false)
const tplTemplates = ref([])          // 模板列表
const tplVersions = ref([])           // 所选模板的版本列表
const tplSourceTaskId = ref('')
const tplTemplateId = ref('')
const tplVersionA = ref(null)
const tplVersionB = ref(null)
const tplSessionName = ref('')
const tplCreating = ref(false)

const tplHistoryVisible = ref(false)
const tplHistoryList = ref([])
const tplHistoryLoading = ref(false)
const tplHistoryTotal = ref(0)
const tplHistoryPage = ref(1)
const tplDeletingId = ref(null)
const tplRunningKey = ref('')         // `${sessionId}-${side}` 运行中标识
const tplComparisons = ref({})        // sessionId → comparison 载荷缓存

const loadTplOptions = async () => {
  tplTasksLoading.value = true
  try {
    const [tasksRes, tplRes] = await Promise.all([
      taskApi.list({ limit: 200 }),
      instructionApi.list({ limit: 200 })
    ])
    tplTasks.value = tasksRes || []
    tplTemplates.value = tplRes || []
  } catch (e) {
    console.error('加载模板对照选项失败', e)
  } finally {
    tplTasksLoading.value = false
  }
}

const onTplTemplateChange = async () => {
  tplVersionA.value = null
  tplVersionB.value = null
  tplVersions.value = []
  if (!tplTemplateId.value) return
  try {
    const res = await instructionApi.listTemplateVersions(tplTemplateId.value)
    tplVersions.value = res || []
    if (tplVersions.value.length === 0) {
      ElMessage.warning('该模板还没有版本，请先到「评测模板」页打版')
    }
  } catch (e) {
    console.error('获取模板版本失败', e)
  }
}

const createTemplateSession = async () => {
  if (tplCreating.value) return
  if (!tplSourceTaskId.value || !tplTemplateId.value || !tplVersionA.value || !tplVersionB.value) return
  if (tplVersionA.value === tplVersionB.value) {
    ElMessage.error('对照两版本不能相同')
    return
  }
  tplCreating.value = true
  try {
    const res = await abTestApi.createTemplateSession({
      source_task_id: tplSourceTaskId.value,
      template_id: tplTemplateId.value,
      version_a: tplVersionA.value,
      version_b: tplVersionB.value,
      name: tplSessionName.value?.trim() || null
    })
    ElMessage.success(`对照实验已创建：${res.task_a_id ? 'A/B 两个变体任务就绪' : ''}，到「对照实验记录」运行`)
    tplSessionName.value = ''
    if (res.comparison) tplComparisons.value[res.id] = res.comparison
    openTemplateHistory()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '创建对照实验失败')
  } finally {
    tplCreating.value = false
  }
}

const openTemplateHistory = () => {
  tplHistoryVisible.value = true
  loadTemplateHistory()
}

const loadTemplateHistory = async () => {
  tplHistoryLoading.value = true
  try {
    const res = await abTestApi.listSessions({
      mode: 'template', page: tplHistoryPage.value, page_size: 20
    })
    tplHistoryList.value = res?.items || []
    tplHistoryTotal.value = res?.total || 0
    // 逐条拉对照载荷（任务状态/均分），缓存供表格渲染
    await Promise.all(tplHistoryList.value.map(async (row) => {
      if (!tplComparisons.value[row.id]) {
        try {
          const detail = await abTestApi.getSession(row.id)
          if (detail?.comparison) tplComparisons.value[row.id] = detail.comparison
        } catch (e) { /* 单条失败不阻断列表 */ }
      }
    }))
  } catch (e) {
    console.error('加载对照实验记录失败', e)
  } finally {
    tplHistoryLoading.value = false
  }
}

const comparisonOf = (row) => tplComparisons.value[row.id] || null
const tplTaskName = (taskId) => tplTasks.value.find(t => t.id === taskId)?.name || taskId || '-'

// 运行单侧变体任务：走既有任务 run 入口（RBAC/配额/互斥全口径）
const runTplSide = async (row, side) => {
  const taskId = side === 'a' ? row.task_a_id : row.task_b_id
  if (!taskId) return
  tplRunningKey.value = `${row.id}-${side}`
  try {
    await taskApi.run(taskId)
    ElMessage.success(`变体任务${side.toUpperCase()} 已开始执行，稍后点「刷新」查看均分`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '任务启动失败')
  } finally {
    tplRunningKey.value = ''
  }
}

const deleteTplSession = async (row) => {
  try {
    await ElMessageBox.confirm(
      '删除对照实验记录不会删除已生成的变体任务（任务可在「评测任务」页自行管理）。确定删除？',
      '删除对照记录',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch { return }
  tplDeletingId.value = row.id
  try {
    await abTestApi.deleteSession(row.id)
    ElMessage.success('已删除')
    await loadTemplateHistory()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  } finally {
    tplDeletingId.value = null
  }
}

// ==================== 模型列表 ====================
const modelList = ref([])

const loadModels = async () => {
  try {
    const res = await modelApi.list({ limit: 200 })
    modelList.value = res || []
  } catch (e) {
    console.error('加载模型列表失败', e)
  }
}

// ==================== 模型选择 & 变量合并 ====================
const modelAId = ref('')
const modelBId = ref('')
const modelAName = computed(() => modelList.value.find(m => m.id === modelAId.value)?.name || '')
const modelBName = computed(() => modelList.value.find(m => m.id === modelBId.value)?.name || '')

const varsA = ref([])  // 模型A的变量列表
const varsB = ref([])  // 模型B的变量列表

// 合并后的变量（取并集）
const allVariables = computed(() => {
  const set = new Set([...varsA.value, ...varsB.value])
  return Array.from(set)
})

const injectorInitialData = ref({})
const currentVariables = ref({})  // VariableInjector 输出的 runtime_variables

const onVariableUpdate = (data) => {
  currentVariables.value = data.runtime_variables || {}
}

const onModelAChange = async (modelId) => {
  if (!modelId) { varsA.value = []; return }
  try {
    const res = await modelApi.getVariables(modelId)
    varsA.value = res.variables || []
  } catch (e) {
    varsA.value = []
  }
}

const onModelBChange = async (modelId) => {
  if (!modelId) { varsB.value = []; return }
  try {
    const res = await modelApi.getVariables(modelId)
    varsB.value = res.variables || []
  } catch (e) {
    varsB.value = []
  }
}

// ==================== A/B 测试执行 ====================
const isRunning = ref(false)
const testStarted = ref(false)
const testDone = ref(false)

// 多轮配置（M2 QE2-3）：1=单轮（旧行为），2-10 轮同输入配对出显著性
const rounds = ref(1)
// 显著性统计（WS complete 消息返回 / 历史记录回放）：单轮为 null 不显示面板
const significance = ref(null)

const logsA = ref([])
const logsB = ref([])
const loadingA = ref(false)
const loadingB = ref(false)
const durationA = ref(null)
const durationB = ref(null)
const successA = ref(false)
const successB = ref(false)

// 保存执行结果（用于保存历史）
const resultA = ref(null)
const resultB = ref(null)

let ws = null

const startABTest = () => {
  if (!modelAId.value || !modelBId.value) return

  // 重置状态
  logsA.value = []
  logsB.value = []
  durationA.value = null
  durationB.value = null
  successA.value = false
  successB.value = false
  resultA.value = null
  resultB.value = null
  significance.value = null
  testStarted.value = true
  testDone.value = false
  isRunning.value = true
  loadingA.value = true
  loadingB.value = true

  const wsUrl = appConfig.getWebSocketUrl('/api/v1/ab-test/ws/run')
  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    ws.send(JSON.stringify({
      model_a_id: modelAId.value,
      model_b_id: modelBId.value,
      variables: currentVariables.value,
      rounds: rounds.value
    }))
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      const side = data.side

      if (side === 'A') {
        if (data.type !== 'stream_chunk') logsA.value.push(data)
        if (data.type === 'side_complete') {
          // 多轮：逐轮覆盖展示最近一轮耗时/成败（历史明细看逐轮记录）
          loadingA.value = false
          durationA.value = data.duration
          successA.value = data.success
        }
      } else if (side === 'B') {
        if (data.type !== 'stream_chunk') logsB.value.push(data)
        if (data.type === 'side_complete') {
          loadingB.value = false
          durationB.value = data.duration
          successB.value = data.success
        }
      } else if (data.type === 'round_start') {
        logsA.value.push({ type: 'info', content: `—— ${data.content} ——` })
        logsB.value.push({ type: 'info', content: `—— ${data.content} ——` })
      }

      if (data.type === 'complete') {
        isRunning.value = false
        testDone.value = true
        durationA.value = data.duration_a
        durationB.value = data.duration_b
        // M2 QE2-3：多轮显著性统计（单轮 complete 消息不携带该键）
        if (data.significance) {
          significance.value = data.significance
        }
        // 整理保存数据
        resultA.value = buildResultFromLogs(logsA.value, durationA.value, successA.value)
        resultB.value = buildResultFromLogs(logsB.value, durationB.value, successB.value)
        // 多轮：逐轮耗时/成败明细并入两侧保存结果（历史回放显著性复算用）
        if (data.rounds) {
          resultA.value.rounds = data.rounds.map(r => ({ round: r.round, duration: r.duration_a, success: r.success_a }))
          resultB.value.rounds = data.rounds.map(r => ({ round: r.round, duration: r.duration_b, success: r.success_b }))
        }
      }

      if (data.type === 'error' && !side) {
        // 全局错误
        ElMessage.error(data.content || '测试失败')
        isRunning.value = false
        loadingA.value = false
        loadingB.value = false
      }
    } catch (e) {
      console.error('解析 WebSocket 消息失败', e)
    }
  }

  ws.onerror = () => {
    ElMessage.error('WebSocket 连接错误')
    isRunning.value = false
    loadingA.value = false
    loadingB.value = false
  }

  ws.onclose = () => {
    if (isRunning.value) {
      isRunning.value = false
      loadingA.value = false
      loadingB.value = false
    }
  }
}

const stopTest = () => {
  if (ws) {
    ws.close()
    ws = null
  }
  isRunning.value = false
  loadingA.value = false
  loadingB.value = false
  testDone.value = true
}

const resetTest = () => {
  testStarted.value = false
  testDone.value = false
  logsA.value = []
  logsB.value = []
  durationA.value = null
  durationB.value = null
}

// ==================== 保存历史 ====================
const saving = ref(false)
const saveNameVisible = ref(false)
const saveName = ref('')

// 点击"保存本次对比记录"按钮 → 弹出命名对话框
const startSave = () => {
  saveName.value = `${modelAName.value} vs ${modelBName.value}`
  saveNameVisible.value = true
}

// 确认保存
const confirmSave = async () => {
  saving.value = true
  try {
    await abTestApi.saveSession({
      model_a_id: modelAId.value,
      model_b_id: modelBId.value,
      model_a_name: modelAName.value,
      model_b_name: modelBName.value,
      variables: currentVariables.value,
      result_a: resultA.value,
      result_b: resultB.value,
      status: (successA.value || successB.value) ? 'completed' : 'failed',
      duration_a: durationA.value,
      duration_b: durationB.value,
      name: saveName.value || null,
      // M2 QE2-3：多轮显著性统计一并留档（单轮为 null）
      significance: significance.value,
    })
    saveNameVisible.value = false
    ElMessage.success('记录已保存')
  } catch (e) {
    ElMessage.error('保存失败: ' + (e.message || '未知错误'))
  } finally {
    saving.value = false
  }
}

// ==================== 历史记录 ====================
const historyVisible = ref(false)
const historyLoading = ref(false)
const historyList = ref([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyKeyword = ref('')
const historyDetailVisible = ref(false)
const historyDetail = ref(null)
const deletingHistoryId = ref(null)

const loadHistory = async () => {
  historyLoading.value = true
  try {
    const res = await abTestApi.listSessions({
      page: historyPage.value,
      page_size: 20,
      keyword: historyKeyword.value || undefined,
    })
    historyList.value = res.items || []
    historyTotal.value = res.total || 0
  } catch (e) {
    ElMessage.error('加载历史记录失败')
  } finally {
    historyLoading.value = false
  }
}

// 搜索（重置到第一页再加载）
const searchHistory = () => {
  historyPage.value = 1
  loadHistory()
}

const viewHistory = async (id) => {
  try {
    const res = await abTestApi.getSession(id)
    historyDetail.value = res
    historyDetailVisible.value = true
  } catch (e) {
    ElMessage.error('加载详情失败')
  }
}

const deleteHistory = async (id) => {
  try {
    await ElMessageBox.confirm('确认删除这条记录？', '提示', { type: 'warning' })
    if (deletingHistoryId.value !== null) return
    deletingHistoryId.value = id
    try {
      await abTestApi.deleteSession(id)
      ElMessage.success('删除成功')
      await loadHistory()
    } finally {
      deletingHistoryId.value = null
    }
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('删除失败')
  }
}

// 打开历史弹窗时自动加载
const openHistory = () => {
  historyVisible.value = true
  historyKeyword.value = ''
  historyPage.value = 1
  loadHistory()
}

// ==================== 工具函数 ====================

const buildResultFromLogs = (logs, duration, success) => {
  const content = logs.find(l => l.type === 'response')?.content || ''
  const requestDetails = logs.filter(l => l.type === 'request_detail')
  return { success, content, request_details: requestDetails, duration }
}

const buildLogsFromResult = (result) => {
  if (!result) return []
  const logs = []
  if (result.request_details?.length) {
    result.request_details.forEach(d => logs.push({ ...d, type: 'request_detail' }))
  }
  if (result.success) {
    logs.push({ type: 'success', content: '连接成功' })
    if (result.content) logs.push({ type: 'response', content: result.content })
  } else {
    logs.push({ type: 'error', content: result.error || '执行失败' })
  }
  return logs
}

// ==================== 初始化 ====================
onMounted(() => {
  loadModels()
  // M5 QE5-2：模板对照模式的选项（任务/模板列表）随页预载，切模式零等待
  loadTplOptions()
})
</script>

<style scoped>
.ab-test-container {
  min-height: 100%;
}

.ab-test-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.header-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.subtitle {
  font-size: 13px;
  font-weight: 400;
  color: var(--text-secondary);
}

.config-card {
  margin-bottom: 16px;
}

.model-selector {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.model-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
  font-size: 14px;
  color: var(--text-primary);
}

.model-tag {
  font-size: 14px;
  font-weight: 700;
  min-width: 28px;
  text-align: center;
}

.model-opt-provider {
  margin-left: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.variables-section {
  margin-top: 20px;
}

.var-tip {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-secondary);
  margin-left: 6px;
}

.action-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--border-color);
}

/* 多轮配置（M2 QE2-3）：轮数次与提示 */
.rounds-config {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rounds-label {
  font-size: 14px;
  color: var(--text-secondary);
}

.rounds-tip {
  font-size: 12px;
  color: #909399;
}

/* 显著性面板（M2 QE2-3） */
.significance-card {
  margin-bottom: 16px;
}

.significance-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
}

.significance-alert {
  margin-bottom: 8px;
}

.significance-note {
  margin-top: 10px;
  font-size: 12px;
  color: #909399;
}

.hint-text {
  font-size: 12px;
  color: #c0c4cc;
}

.result-row {
  margin-bottom: 16px;
}

.result-card {
  height: 100%;
}

.result-card-a :deep(.el-card__header) {
  border-left: 4px solid #409eff;
}

.result-card-b :deep(.el-card__header) {
  border-left: 4px solid #e6a23c;
}

.result-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}

.model-name {
  font-weight: 500;
  font-size: 14px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}

.duration-text {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: monospace;
}

.spin-icon {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.save-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 0;
  border-top: 1px solid var(--border-color);
}

.history-toolbar {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: flex-end;
}

/* 模板对照记录工具栏提示（M5 QE5-2） */
.tpl-history-tip {
  color: #909399;
  font-size: 12px;
  margin-right: auto;
}

.history-pagination {
  margin-top: 16px;
  display: flex;
  justify-content: center;
}
</style>
