<template>
  <div class="results-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>评测结果</span>
          <div style="display: flex; gap: 12px; align-items: center;">
            <el-select v-model="selectedTaskId" placeholder="请选择任务" @change="onTaskChange" style="width: 280px">
              <el-option v-for="t in allTasks" :key="t.id" :label="t.name" :value="t.id">
                <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                  <span>{{ t.name }}</span>
                  <el-tag size="small" :type="getStatusTagType(t.status)">{{ t.status }}</el-tag>
                </div>
              </el-option>
            </el-select>
            <el-select
              v-if="selectedTaskId && executionVersions.length > 0"
              v-model="selectedExecutionNumber"
              placeholder="选择历史版本"
              @change="loadResultByVersion"
              style="width: 220px"
            >
              <el-option
                v-for="v in executionVersions"
                :key="v.execution_number"
                :value="v.execution_number"
                :label="`第${v.execution_number}次 · ${v.status}`"
              >
                <div style="display:flex; justify-content:space-between; align-items:center;">
                  <span>第 {{ v.execution_number }} 次执行</span>
                  <span style="display:flex; gap:4px; align-items:center;">
                    <!-- M1：门禁判定随版本摘要返回（未启用门禁不显示） -->
                    <el-tag v-if="v.gate_result?.verdict" size="small" :type="gateVerdictTagType(v.gate_result.verdict)">
                      {{ gateVerdictLabel(v.gate_result.verdict) }}
                    </el-tag>
                    <el-tag size="small" :type="getStatusTagType(v.status)">{{ v.status }}</el-tag>
                  </span>
                </div>
                <div style="font-size:11px; color:#909399;">{{ formatTime(v.completed_at || v.started_at) }}</div>
              </el-option>
            </el-select>
            <el-dropdown @command="handleExport" trigger="click">
              <el-button type="success" :loading="exporting">
                导出结果 <el-icon><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="excel">导出 Excel</el-dropdown-item>
                  <el-dropdown-item command="json">导出 JSON</el-dropdown-item>
                  <el-dropdown-item command="csv">导出 CSV</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <!-- M5 QE5-4：免登录只读报告分享（viewer 只读隐藏入口） -->
            <el-button
              v-if="selectedTaskId && !readonlyRole"
              @click="openShareDialog"
            >分享</el-button>
            <!-- M8 QE8-1：人工标注模式（待标队列=失败行/低分行优先，快捷键打分；viewer 隐藏） -->
            <el-button
              v-if="selectedTaskId && !readonlyRole"
              @click="openAnnotationMode"
            >标注模式</el-button>
          </div>
        </div>
      </template>

      <div v-if="result" v-loading="resultLoading" element-loading-text="正在加载评测结果，请稍候…">
        <!-- 结果概览 -->
        <el-row :gutter="20">
          <el-col :span="6">
            <div class="stat-card">
              <div class="stat-label">评测类型</div>
              <div class="stat-value">{{ getTypeName(result.evaluation_type) }}</div>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="stat-card">
              <div class="stat-label">任务状态</div>
              <div class="stat-value">
                <el-tag :type="result.status === 'completed' ? 'success' : 'info'">{{ result.status }}</el-tag>
              </div>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="stat-card">
              <div class="stat-label">完成时间</div>
              <div class="stat-value">{{ formatTime(result.completed_at) }}</div>
            </div>
          </el-col>
          <el-col :span="6">
            <div class="stat-card">
              <div class="stat-label">消耗成本</div>
              <div class="stat-value">{{ result.total_cost || '-' }}</div>
            </div>
          </el-col>
        </el-row>

        <!-- 异常状态兼容展示 -->
        <div v-if="result.status === 'failed'" class="status-alert">
          <el-alert type="error" :closable="false" show-icon>
            <template #title>此次执行失败</template>
            <div>{{ result.error_message || result.result?.error || '未知错误' }}</div>
          </el-alert>
        </div>
        <div v-else-if="result.status === 'stopped'" class="status-alert">
          <el-alert type="warning" :closable="false" show-icon title="此次执行已被停止，以下展示停止前的部分结果" />
        </div>

        <!-- M1 门禁横幅：本次执行的门禁判定（通过绿/未通过红/错误橙）+ 逐阈值实际值表 -->
        <div v-if="result.gate_result?.verdict" class="gate-banner" :class="`gate-${result.gate_result.verdict}`">
          <div class="gate-banner-head">
            <el-icon class="gate-banner-icon">
              <CircleCheckFilled v-if="result.gate_result.verdict === 'passed'" />
              <CircleCloseFilled v-else-if="result.gate_result.verdict === 'failed'" />
              <WarningFilled v-else />
            </el-icon>
            <span class="gate-banner-title">
              门禁判定：{{ result.gate_result.verdict === 'passed' ? '通过（PASSED）' : result.gate_result.verdict === 'failed' ? '未通过（FAILED）' : '错误（ERROR）' }}
            </span>
            <span v-if="result.gate_result.mode" class="gate-banner-mode">
              判定方式：{{ result.gate_result.mode === 'any' ? '任一阈值通过' : '全部阈值须通过' }}
            </span>
            <span v-if="result.gate_result.evaluated_at" class="gate-banner-time">{{ formatTime(result.gate_result.evaluated_at) }}</span>
          </div>
          <el-table v-if="(result.gate_result.checks || []).length" :data="result.gate_result.checks" size="small" class="gate-checks-table">
            <el-table-column label="指标" min-width="200">
              <template #default="{ row }">{{ gateMetricLabel(row.metric) }}</template>
            </el-table-column>
            <el-table-column label="阈值" width="120">
              <template #default="{ row }">{{ row.op }} {{ row.value }}</template>
            </el-table-column>
            <el-table-column label="实际值" width="110">
              <template #default="{ row }">{{ row.actual ?? '不可用' }}</template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag :type="row.passed ? 'success' : 'danger'" size="small">
                  {{ row.passed ? '通过' : '未通过' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="说明" min-width="140">
              <template #default="{ row }">{{ row.note || '-' }}</template>
            </el-table-column>
          </el-table>
        </div>

        <!-- M2 QE2-4 裁判治理面板：本次裁决所用裁判配置快照 + 金丝雀自检结果
             （judge_config_snapshot 存在或聚合含 judge_governance.canary 时显示） -->
        <div
          v-if="result.judge_config_snapshot || result.result?.judge_governance?.canary"
          class="judge-governance-panel"
        >
          <div class="judge-governance-head">
            <span class="judge-governance-title">裁判治理</span>
            <el-tag
              v-if="result.result?.judge_governance?.canary?.risk"
              type="danger"
              size="small"
              effect="dark"
            >
              裁判可被欺骗风险
            </el-tag>
            <span v-if="result.result?.judge_governance?.canary" style="color: #909399; font-size: 12px;">
              金丝雀探针 {{ result.result.judge_governance.canary.high_score_count }}/{{ result.result.judge_governance.canary.rows }} 行高分
            </span>
          </div>

          <!-- 本次裁决所用裁判配置（版本可追溯） -->
          <div v-if="result.judge_config_snapshot" class="judge-governance-section">
            <div class="judge-governance-section-title">本次裁决所用裁判配置</div>
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="裁判引擎">
                {{ judgeEngineLabel(result.judge_config_snapshot.engine) }}
              </el-descriptions-item>
              <el-descriptions-item label="裁判模型">
                {{ judgeModelSummary(result.judge_config_snapshot.judge_model) }}
              </el-descriptions-item>
              <el-descriptions-item label="输入消毒">
                <el-tag
                  :type="result.judge_config_snapshot.guard?.injection_guard_disabled ? 'warning' : 'success'"
                  size="small"
                >
                  {{ result.judge_config_snapshot.guard?.injection_guard_disabled ? '已显式关闭（审计留痕）' : '开启（<eval_data> 定界）' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="金丝雀自检">
                {{ result.judge_config_snapshot.canary?.enabled ? '开启' : '关闭' }}
              </el-descriptions-item>
              <el-descriptions-item label="满分" label-align="right">
                {{ result.judge_config_snapshot.max_score ?? '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="评分维度">
                {{ (result.judge_config_snapshot.evaluation_metrics || []).map(m => m.name || m).join('、') || '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="评分标准" :span="2">
                <span class="judge-governance-text">{{ result.judge_config_snapshot.score_details || '-' }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="裁判 Prompt 模板" :span="2">
                <span class="judge-governance-text">
                  {{ result.judge_config_snapshot.prompt_template || '（未自定义，使用系统默认模板）' }}
                </span>
              </el-descriptions-item>
            </el-descriptions>
          </div>

          <!-- 金丝雀自检结果：空/乱码探针行的裁判打分（不计入均分） -->
          <div v-if="result.result?.judge_governance?.canary" class="judge-governance-section">
            <div class="judge-governance-section-title">
              金丝雀自检结果（探针行不计入均分；高分线 = 满分 × 0.7 = {{ result.result.judge_governance.canary.threshold ?? '-' }}）
            </div>
            <el-table :data="result.result.judge_governance.canary.items" size="small">
              <el-table-column label="探针类型" width="110">
                <template #default="{ row }">{{ canaryKindLabel(row.kind) }}</template>
              </el-table-column>
              <el-table-column label="探针内容" min-width="200">
                <template #default="{ row }">
                  <span class="judge-governance-text">{{ row.kind === 'empty' ? '（空内容）' : truncateText(row.content, 60) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="裁判打分" width="110">
                <template #default="{ row }">{{ row.score ?? '-' }}</template>
              </el-table-column>
              <el-table-column label="判定" width="110">
                <template #default="{ row }">
                  <el-tag :type="row.high ? 'danger' : 'success'" size="small">
                    {{ row.high ? '高分（可疑）' : '正常' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="说明" min-width="140">
                <template #default="{ row }">
                  {{ row.error ? `探针行失败：${row.error}` : (row.success ? '裁判按预期未给高分' : '探针行未完成评分') }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>

        <!-- 详细结果 -->
        <div v-if="isResultRenderable(result)" class="result-detail">
          <el-divider />
          <h3>评测详情</h3>

          <!-- 统计卡片 - 一致性评测、耗时评测、二分分类、插件评测使用不同的指标，不显示此卡片 -->
          <el-row :gutter="20" class="stats-row" v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety' && result.evaluation_type !== 'plugin'">
            <el-col :span="6">
              <div class="stat-card-small">
                <div class="stat-label">总题目数</div>
                <div class="stat-value">{{ result.result.total || result.result.total_questions || '-' }}</div>
              </div>
            </el-col>
            <el-col :span="6">
              <div class="stat-card-small">
                <div class="stat-label">成功评测</div>
                <div class="stat-value success">{{ result.result.success_count || '-' }}</div>
              </div>
            </el-col>
            <el-col :span="6">
              <!-- 失败数量卡：有失败时可点击查看失败原因汇总（与主表合并后的失败行同源，judge/agent 等通用统计卡共用此入口） -->
              <div
                class="stat-card-small"
                :class="{ 'stat-card-clickable': (result.result.failed_count || 0) > 0 }"
                :title="(result.result.failed_count || 0) > 0 ? '点击查看失败原因汇总' : ''"
                @click="openFailureSummary"
              >
                <div class="stat-label">失败数量</div>
                <div class="stat-value danger">{{ result.result.failed_count || '-' }}</div>
              </div>
            </el-col>
            <el-col :span="6">
              <div class="stat-card-small">
                <div class="stat-label">平均分</div>
                <div class="stat-value highlight">{{ result.result.average_score || '-' }}</div>
              </div>
            </el-col>
          </el-row>

          <!-- pass^k 统计卡（M2 QE2-3，judge 行级多 Trial）：聚合含 pass_k 块才显示 -->
          <el-row :gutter="20" class="stats-row" style="margin-top: 0;" v-if="result.result.pass_k && result.evaluation_type !== 'consistency'">
            <el-col :span="8">
              <div class="stat-card-small">
                <div class="stat-label">pass@1（单次通过率）</div>
                <div class="stat-value highlight">{{ safePercent(result.result.pass_k.average_pass_at_1) }}%</div>
              </div>
            </el-col>
            <el-col :span="8">
              <div class="stat-card-small">
                <div class="stat-label">pass^k（k 次全过比例）</div>
                <div class="stat-value">{{ safePercent(result.result.pass_k.average_pass_pow_k) }}%</div>
                <div class="stat-sub">全过 {{ result.result.pass_k.all_pass_rows }}/{{ result.result.pass_k.rows }} 行</div>
              </div>
            </el-col>
            <el-col :span="8">
              <div class="stat-card-small">
                <div class="stat-label">多 Trial 行数</div>
                <div class="stat-value">{{ result.result.pass_k.rows }}</div>
                <div class="stat-sub">行级 pass@1/pass^k 见明细行徽标</div>
              </div>
            </el-col>
          </el-row>

          <!-- 图表区域 - 一致性评测、耗时评测、二分分类、插件评测没有通用图表，不显示 -->
          <el-row :gutter="20" v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety' && result.evaluation_type !== 'plugin'">
            <!-- 雷达图 - 各维度评分 -->
            <el-col :span="12">
              <el-card>
                <template #header>各维度评分</template>
                <div id="radarChart" style="width: 100%; height: 350px;"></div>
              </el-card>
            </el-col>

            <!-- 柱状图 - 评分分布 -->
            <el-col :span="12">
              <el-card>
                <template #header>评分分布</template>
                <div id="barChart" style="width: 100%; height: 350px;"></div>
              </el-card>
            </el-col>
          </el-row>

          <el-row :gutter="20" style="margin-top: 20px;" v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety' && result.evaluation_type !== 'plugin'">
            <!-- 饼图 - 成功率 -->
            <el-col :span="12">
              <el-card>
                <template #header>评测成功率</template>
                <div id="pieChart" style="width: 100%; height: 300px;"></div>
              </el-card>
            </el-col>

            <!-- 折线图 - 评分趋势 -->
            <el-col :span="12">
              <el-card>
                <template #header>评分趋势</template>
                <div id="lineChart" style="width: 100%; height: 300px;"></div>
              </el-card>
            </el-col>
          </el-row>

          <!-- 多次执行聚合展示：跨执行的维度对比 / 分数趋势 / 波动统计。
               v-if 与通用图表区同条件（排除 rag/consistency/latency/binary_classification/safety/plugin）；
               旧后端无 dimension-trend 端点时 executionTrend 为空数组，整区隐藏 -->
          <el-row
            v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety' && result.evaluation_type !== 'plugin' && executionTrend.length >= 2"
            style="margin-top: 20px;"
          >
            <el-col :span="24">
              <el-divider />
              <h3>多次执行聚合展示</h3>
            </el-col>
            <!-- 雷达图 - 多次执行维度对比 -->
            <el-col :span="12">
              <el-card>
                <template #header>多次执行维度对比</template>
                <div id="trendRadarChart" style="width: 100%; height: 350px;"></div>
              </el-card>
            </el-col>
            <!-- 折线图 - 维度分数趋势 -->
            <el-col :span="12">
              <el-card>
                <template #header>维度分数趋势</template>
                <div id="trendLineChart" style="width: 100%; height: 300px;"></div>
              </el-card>
            </el-col>
            <!-- 表格 - 跨执行波动 -->
            <el-col :span="24" style="margin-top: 20px;">
              <el-card>
                <template #header>跨执行波动</template>
                <el-table :data="trendVolatilityRows" style="width: 100%" max-height="360">
                  <el-table-column prop="name" label="维度" min-width="140" />
                  <el-table-column label="均值" width="110">
                    <template #default="{ row }">{{ formatTrendNumber(row.avg) }}</template>
                  </el-table-column>
                  <el-table-column label="最优" width="110">
                    <template #default="{ row }">{{ formatTrendNumber(row.best) }}</template>
                  </el-table-column>
                  <el-table-column label="最差" width="110">
                    <template #default="{ row }">{{ formatTrendNumber(row.worst) }}</template>
                  </el-table-column>
                  <el-table-column label="波动幅度" width="130">
                    <template #default="{ row }">
                      <span :style="row.range > 1.0 ? 'color:#E6A23C;font-weight:600;' : ''">{{ formatTrendNumber(row.range) }}</span>
                    </template>
                  </el-table-column>
                </el-table>
              </el-card>
            </el-col>
          </el-row>

          <!-- 脚本产物（评测前处理脚本）：数据驱动展示，judge 与 agent 类型均生效；
               任务未编排 pre_scripts 或历史结果无 pre_script_results 字段时此区不渲染 -->
          <div v-if="hasPreScriptResults" class="pre-scripts-section">
            <el-divider />
            <div class="pre-scripts-toolbar">
              <h3>脚本产物</h3>
              <el-select
                v-if="preScriptEntries.length > 1"
                v-model="preScriptSelectedIdx"
                style="width: 220px"
                placeholder="选择数据条目"
              >
                <el-option
                  v-for="(entry, i) in preScriptEntries"
                  :key="i"
                  :value="i"
                  :label="`第 ${detailSeqNo(entry, i, seqNoFromIdx)} 条数据`"
                />
              </el-select>
            </div>
            <div class="pre-scripts-hint">
              评测裁判评分前执行的前处理脚本产物（确定性比对等硬事实），供人工复核。
            </div>

            <el-card
              v-for="(script, sIdx) in currentPreScriptList"
              :key="script.script_id || sIdx"
              shadow="never"
              class="pre-script-card"
            >
              <template #header>
                <div class="pre-script-card-header">
                  <div class="pre-script-card-title">
                    <span class="pre-script-name">{{ script.script_name || script.script_id }}</span>
                    <el-tag :type="getPreScriptStatusTag(script.status).type" size="small">
                      {{ getPreScriptStatusTag(script.status).label }}
                    </el-tag>
                    <el-tag effect="plain" size="small" type="info">
                      {{ getFinishReasonLabel(script.finish_reason) }}
                    </el-tag>
                    <span class="pre-script-duration">耗时 {{ formatDurationMs(script.duration_ms) }}</span>
                  </div>
                  <div class="pre-script-actions">
                    <el-button
                      v-if="hasPreScriptDetailRows(script)"
                      link
                      type="primary"
                      size="small"
                      @click="openPreScriptFullDetail(script)"
                    >
                      查看完整明细
                    </el-button>
                    <el-button link type="primary" size="small" @click="downloadPreScriptDetail(script)">
                      下载 JSON
                    </el-button>
                  </div>
                </div>
              </template>

              <div v-if="script.summary" class="pre-script-summary">{{ script.summary }}</div>
              <el-alert
                v-if="script.error"
                type="error"
                :title="script.error"
                :closable="false"
                show-icon
                style="margin-top: 8px;"
              />

              <div v-if="getPreScriptStatChips(script).length > 0" class="pre-script-chips">
                <span
                  v-for="chip in getPreScriptStatChips(script)"
                  :key="chip.key"
                  class="pre-script-chip"
                >
                  <span class="pre-script-chip-label">{{ chip.label }}</span>
                  <span class="pre-script-chip-value">{{ chip.value }}</span>
                </span>
              </div>

              <!-- xml_diff 预置脚本：差异清单表格（字段缺失时不渲染，兼容历史任务） -->
              <el-table
                v-if="getPreScriptKind(script) === 'xml_diff' && getXmlDiffRows(script).length > 0"
                :data="getXmlDiffRows(script)"
                style="width: 100%; margin-top: 12px;"
                max-height="420"
                stripe
              >
                <el-table-column label="差异位置" min-width="180" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('差异位置', formatCellValue(pickRowField(row, XML_DIFF_PATH_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, XML_DIFF_PATH_KEYS)), 40) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="差异类型" width="120">
                  <template #default="{ row }">
                    <el-tag :type="getDiffTypeTag(formatCellValue(pickRowField(row, XML_DIFF_TYPE_KEYS))).type" size="small">
                      {{ truncateText(formatCellValue(pickRowField(row, XML_DIFF_TYPE_KEYS)), 20) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="预期值" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('预期值', formatCellValue(pickRowField(row, XML_DIFF_EXPECTED_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, XML_DIFF_EXPECTED_KEYS)), 50) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="实际值" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('实际值', formatCellValue(pickRowField(row, XML_DIFF_ACTUAL_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, XML_DIFF_ACTUAL_KEYS)), 50) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="说明" min-width="180" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('差异说明', getXmlDiffNote(row))">
                      {{ truncateText(getXmlDiffNote(row), 60) }}
                    </div>
                  </template>
                </el-table-column>
              </el-table>

              <!-- log_grep 预置脚本：命中样例等宽字体块（命中统计走上方 chips） -->
              <div
                v-if="getPreScriptKind(script) === 'log_grep' && getLogGrepSampleText(script)"
                class="pre-log-sample-wrap"
              >
                <div class="pre-log-sample-title">命中样例（{{ getLogGrepSamples(script).length }} 行）</div>
                <pre class="pre-log-sample">{{ getLogGrepSampleText(script) }}</pre>
              </div>

              <!-- db_assert 预置脚本：名值断言比对表（字段缺失时不渲染，兼容历史任务） -->
              <el-table
                v-if="getPreScriptKind(script) === 'db_assert' && getDbAssertRows(script).length > 0"
                :data="getDbAssertRows(script)"
                style="width: 100%; margin-top: 12px;"
                max-height="420"
                stripe
                :row-class-name="getDbAssertRowClassName"
              >
                <el-table-column label="断言项" min-width="180" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('断言项', formatCellValue(pickRowField(row, DB_ASSERT_NAME_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, DB_ASSERT_NAME_KEYS)), 40) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="预期值" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('预期值', formatCellValue(pickRowField(row, DB_ASSERT_EXPECTED_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, DB_ASSERT_EXPECTED_KEYS)), 50) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="实际值" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('实际值', formatCellValue(pickRowField(row, DB_ASSERT_ACTUAL_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, DB_ASSERT_ACTUAL_KEYS)), 50) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="断言结果" width="100">
                  <template #default="{ row }">
                    <el-tag :type="getAssertPassTag(row).type" size="small">{{ getAssertPassTag(row).label }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="说明" min-width="160" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('断言说明', formatCellValue(pickRowField(row, DB_ASSERT_NOTE_KEYS)))">
                      {{ truncateText(formatCellValue(pickRowField(row, DB_ASSERT_NOTE_KEYS)), 60) }}
                    </div>
                  </template>
                </el-table-column>
              </el-table>

              <el-table
                v-if="getPreCaseRows(script).length > 0"
                :data="getPreCaseRows(script)"
                style="width: 100%; margin-top: 12px;"
                max-height="420"
                stripe
                :row-class-name="getPreCaseRowClassName"
              >
                <el-table-column label="用例" min-width="180" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span class="pre-case-name">
                      <el-tag v-if="row.priority" size="small" effect="plain">{{ row.priority }}</el-tag>
                      <span class="cell-preview" @click="openDetail('用例', row.case)">{{ truncateText(row.case, 40) }}</span>
                    </span>
                  </template>
                </el-table-column>
                <el-table-column label="预期" min-width="160" show-overflow-tooltip>
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('预期结果', row.expected)">
                      {{ truncateText(row.expected, 50) }}
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="实际状态" width="110">
                  <template #default="{ row }">
                    <el-tag :type="getActualStatusTag(row.actual_status).type" size="small">
                      {{ getActualStatusTag(row.actual_status).label }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="是否一致" width="100">
                  <template #default="{ row }">
                    <el-tag :type="getMatchStateTag(row).type" size="small">{{ getMatchStateTag(row).label }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="证据" min-width="240">
                  <template #default="{ row }">
                    <div class="cell-preview" @click="openDetail('比对证据', row.evidence)">
                      {{ truncateText(row.evidence, 80) }}
                    </div>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </div>

          <!-- 终态校验器证据（M2 QE2-1）：数据驱动展示，judge 与 agent 均生效；
               任务未配置校验器或历史结果无 verification_result 字段时此区不渲染 -->
          <div v-if="hasVerificationEntries" class="verification-section">
            <el-divider />
            <div class="pre-scripts-toolbar">
              <h3>终态校验</h3>
              <el-tag v-if="verificationSummaryTag" :type="verificationSummaryTag.type" size="small">
                校验通过率 {{ verificationPassRateText }}（{{ verificationSummaryTag.detail }}）
              </el-tag>
              <el-select
                v-if="verificationEntries.length > 1"
                v-model="verificationSelectedIdx"
                style="width: 220px"
                placeholder="选择数据条目"
              >
                <el-option
                  v-for="(entry, i) in verificationEntries"
                  :key="i"
                  :value="i"
                  :label="`第 ${detailSeqNo(entry, i, seqNoFromIdx)} 条数据`"
                />
              </el-select>
            </div>
            <div class="pre-scripts-hint">
              模型输出之后运行的确定性终态断言证据（script / db_assert / file_assert / http_assert），
              独立于裁判评分；on_fail=abort 失败的行在失败明细中保留证据。
            </div>

            <el-card shadow="never" class="pre-script-card" style="margin-bottom: 8px;">
              <template #header>
                <div class="pre-script-card-header">
                  <div class="pre-script-card-title">
                    <span class="pre-script-name">整体判定</span>
                    <el-tag :type="verificationStatusTag(currentVerification.status).type" size="small">
                      {{ verificationStatusTag(currentVerification.status).label }}
                    </el-tag>
                    <el-tag v-if="currentVerification.aborted" type="warning" size="small">on_fail=abort 已中止</el-tag>
                    <span class="pre-script-duration">
                      {{ currentVerification.passed ?? 0 }}/{{ currentVerification.total ?? 0 }} 个校验器通过
                    </span>
                  </div>
                </div>
              </template>
              <el-alert
                v-if="currentVerification.error"
                type="error"
                :title="currentVerification.error"
                :closable="false"
                show-icon
              />
            </el-card>

            <el-card
              v-for="(vEntry, vIdx) in currentVerificationList"
              :key="vEntry.name + vIdx"
              shadow="never"
              class="pre-script-card"
            >
              <template #header>
                <div class="pre-script-card-header">
                  <div class="pre-script-card-title">
                    <span class="pre-script-name">{{ vEntry.name || vEntry.type }}</span>
                    <el-tag size="small" effect="plain" type="info">{{ vEntry.type }}</el-tag>
                    <el-tag :type="verificationStatusTag(vEntry.status).type" size="small">
                      {{ verificationStatusTag(vEntry.status).label }}
                    </el-tag>
                    <span class="pre-script-duration">耗时 {{ vEntry.duration_ms ?? 0 }} ms</span>
                  </div>
                </div>
              </template>
              <el-alert
                v-if="vEntry.error"
                type="error"
                :title="vEntry.error"
                :closable="false"
                show-icon
                style="margin-bottom: 8px;"
              />
              <!-- file_assert / http_assert：断言清单表（checks 字段） -->
              <el-table
                v-if="verifierCheckRows(vEntry).length > 0"
                :data="verifierCheckRows(vEntry)"
                style="width: 100%"
                stripe
              >
                <el-table-column label="断言" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span>{{ row.name || row.checkName || '-' }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="期望" min-width="140" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span>{{ verifierValueText(row.expected) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="实际" min-width="140" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span>{{ verifierValueText(row.actual) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="结果" width="90">
                  <template #default="{ row }">
                    <el-tag :type="verificationCheckTag(row).type" size="small">
                      {{ verificationCheckTag(row).label }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="说明" min-width="160" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span>{{ row.error || '-' }}</span>
                  </template>
                </el-table-column>
              </el-table>
              <!-- 其余证据（script 摘要/db_assert 统计等）：键值对平铺 -->
              <div v-if="verifierEvidenceChips(vEntry).length > 0" class="pre-script-chips">
                <span v-for="chip in verifierEvidenceChips(vEntry)" :key="chip.key" class="pre-script-chip">
                  <span class="pre-script-chip-label">{{ chip.label }}</span>
                  <span class="pre-script-chip-value">{{ chip.value }}</span>
                </span>
              </div>
              <div style="margin-top: 8px;">
                <el-button
                  link
                  type="primary"
                  size="small"
                  @click="openDetail('校验证据 · ' + (vEntry.name || vEntry.type), JSON.stringify(vEntry.evidence || {}, null, 2))"
                >
                  查看完整证据
                </el-button>
              </div>
            </el-card>
          </div>

          <!-- 轨迹匹配（M2 QE2-2）：序列时间线 + 匹配结果；agent 任务配置轨迹断言时
               的确定性证据（独立于裁判评分）；历史/未配置的结果无该字段时此区不渲染 -->
          <div v-if="hasTrajectoryEntries" class="trajectory-section">
            <el-divider />
            <div class="pre-scripts-toolbar">
              <h3>轨迹匹配</h3>
              <el-tag v-if="trajectorySummaryTag" :type="trajectorySummaryTag.type" size="small">
                轨迹匹配率 {{ trajectoryMatchRateText }}（{{ trajectorySummaryTag.detail }}）
              </el-tag>
              <el-select
                v-if="trajectoryEntries.length > 1"
                v-model="trajectorySelectedIdx"
                style="width: 220px"
                placeholder="选择数据条目"
              >
                <el-option
                  v-for="(entry, i) in trajectoryEntries"
                  :key="i"
                  :value="i"
                  :label="`第 ${detailSeqNo(entry, i, seqNoFromIdx)} 条数据`"
                />
              </el-select>
            </div>
            <div class="pre-scripts-hint">
              会话工具调用序列的确定性匹配结果（模式：{{ currentTrajectory.mode || '-' }}），
              独立于裁判评分——截断 trace 虚高等问题在此以硬证据暴露。
            </div>

            <el-card shadow="never" class="pre-script-card" style="margin-bottom: 8px;">
              <template #header>
                <div class="pre-script-card-header">
                  <div class="pre-script-card-title">
                    <span class="pre-script-name">整体判定</span>
                    <el-tag :type="verifierStatusTag(currentTrajectory.status).type" size="small">
                      {{ verifierStatusTag(currentTrajectory.status).label }}
                    </el-tag>
                    <span class="pre-script-duration">
                      期望 {{ currentTrajectory.matched_count ?? 0 }}/{{ currentTrajectory.total_expected ?? 0 }} 项满足 ·
                      实际调用 {{ currentTrajectory.total_calls ?? 0 }} 次
                    </span>
                  </div>
                </div>
              </template>
              <el-alert
                v-if="currentTrajectory.error"
                type="error"
                :title="currentTrajectory.error"
                :closable="false"
                show-icon
              />
              <div v-if="!currentTrajectory.error" class="pre-script-chips">
                <span class="pre-script-chip">
                  <span class="pre-script-chip-label">满足</span>
                  <span class="pre-script-chip-value">{{ currentTrajectory.matched?.length ?? 0 }}</span>
                </span>
                <span class="pre-script-chip">
                  <span class="pre-script-chip-label">缺失</span>
                  <span class="pre-script-chip-value">{{ currentTrajectory.missing?.length ?? 0 }}</span>
                </span>
                <span class="pre-script-chip">
                  <span class="pre-script-chip-label">未期望</span>
                  <span class="pre-script-chip-value">{{ currentTrajectory.unexpected?.length ?? 0 }}</span>
                </span>
                <span class="pre-script-chip">
                  <span class="pre-script-chip-label">禁用命中</span>
                  <span class="pre-script-chip-value">{{ currentTrajectory.forbidden_hits?.length ?? 0 }}</span>
                </span>
              </div>
            </el-card>

            <!-- 匹配结果清单：缺失/禁用命中优先展示（失败证据），未期望仅记录 -->
            <el-row :gutter="12">
              <el-col :span="12">
                <el-card shadow="never" class="pre-script-card">
                  <template #header>缺失的期望调用（missing）</template>
                  <el-empty v-if="!(currentTrajectory.missing || []).length" description="无缺失" :image-size="50" />
                  <div v-for="(m, i) in currentTrajectory.missing" :key="i" class="trajectory-entry trajectory-entry-missing">
                    <el-tag type="danger" size="small">{{ m.tool }}</el-tag>
                    <span v-if="m.min_calls > 1">至少 {{ m.min_calls }} 次</span>
                    <span>实际 {{ m.matched_count ?? 0 }} 次</span>
                    <span v-if="m.args_contains?.length" class="trajectory-entry-args">参数须含：{{ m.args_contains.join(' / ') }}</span>
                  </div>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card shadow="never" class="pre-script-card">
                  <template #header>禁用调用命中（forbidden_hits）</template>
                  <el-empty v-if="!(currentTrajectory.forbidden_hits || []).length" description="无命中" :image-size="50" />
                  <div v-for="(f, i) in currentTrajectory.forbidden_hits" :key="i" class="trajectory-entry trajectory-entry-missing">
                    <el-tag type="danger" size="small">#{{ f.index }} {{ f.tool }}</el-tag>
                    <span v-if="f.args_contains?.length" class="trajectory-entry-args">参数含：{{ f.args_contains.join(' / ') }}</span>
                  </div>
                  <template v-if="(currentTrajectory.unexpected || []).length > 0">
                    <el-divider style="margin: 10px 0;" />
                    <div class="gate-config-tip" style="margin-bottom: 6px;">未期望调用（unexpected，本模式不判失败）：</div>
                    <div v-for="(u, i) in currentTrajectory.unexpected.slice(0, 20)" :key="i" class="trajectory-entry">
                      <el-tag type="info" size="small">#{{ u.index }} {{ u.tool }}</el-tag>
                    </div>
                  </template>
                </el-card>
              </el-col>
            </el-row>

            <!-- 序列时间线：covered=绿 / unexpected=灰 / forbidden=红 -->
            <el-card shadow="never" class="pre-script-card" style="margin-top: 8px;">
              <template #header>
                工具调用序列（{{ currentTrajectory.total_calls ?? 0 }} 次；
                时间线最多展示 {{ (currentTrajectory.timeline || []).length }} 条）
              </template>
              <el-empty v-if="!(currentTrajectory.timeline || []).length" description="会话无工具调用" :image-size="50" />
              <el-timeline v-else style="padding-left: 4px;">
                <el-timeline-item
                  v-for="item in currentTrajectory.timeline"
                  :key="item.index"
                  :type="trajectoryTimelineType(item.kind)"
                  :hollow="item.kind === 'unexpected'"
                >
                  <div class="trajectory-timeline-item">
                    <span class="trajectory-call-index">#{{ item.index }}</span>
                    <el-tag :type="trajectoryTimelineType(item.kind)" size="small" effect="plain">{{ item.tool }}</el-tag>
                    <span class="trajectory-call-args">{{ item.args_snippet }}</span>
                  </div>
                </el-timeline-item>
              </el-timeline>
            </el-card>
          </div>

          <!-- 结果观察（evaluation/result 扩展点）：observations 字段存在时只读展示；
               历史任务无该字段时此区不渲染 -->
          <div v-if="observationItems.length > 0" class="observations-section">
            <el-divider />
            <h3>结果观察</h3>
            <div class="observations-hint">
              评测结果扩展点（evaluation/result）产出的观察信息，只读展示。
            </div>
            <div v-for="(obs, i) in observationItems" :key="i" class="observation-item">
              <el-tag
                v-if="obs.level"
                :type="getObservationLevelTag(obs.level).type"
                size="small"
                class="observation-level"
              >
                {{ getObservationLevelTag(obs.level).label }}
              </el-tag>
              <span v-if="obs.title" class="observation-title">{{ obs.title }}</span>
              <span
                class="observation-text"
                :class="{ clickable: obs.text && obs.text !== '-' }"
                @click="obs.text && obs.text !== '-' && openDetail('结果观察', obs.fullText)"
              >
                {{ truncateText(obs.text, 200) }}
              </span>
            </div>
          </div>

          <!-- 一致性评测结果 -->
          <div v-if="result.evaluation_type === 'consistency'">
            <el-row :gutter="20">
              <el-col :span="8">
                <el-card>
                  <template #header>文本相似度</template>
                  <div class="metric-value">{{ safePercent(result.result.average_text_similarity) }}%</div>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card>
                  <template #header>格式一致性</template>
                  <div class="metric-value">{{ safePercent(result.result.average_format_consistency) }}%</div>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card>
                  <template #header>结构一致性</template>
                  <div class="metric-value">{{ safePercent(result.result.average_structure_consistency) }}%</div>
                </el-card>
              </el-col>
            </el-row>

            <!-- pass^k 徽标卡（M2 QE2-3，语义对齐 τ-bench）：聚合含 pass_k 块才显示 -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="result.result.pass_k">
              <el-col :span="12">
                <el-card>
                  <template #header>
                    pass@1
                    <el-tag size="small" type="info" style="margin-left: 8px;">单次重复调用成功率均值</el-tag>
                  </template>
                  <div class="metric-value">
                    {{ safePercent(result.result.pass_k.average_pass_at_1) }}%
                  </div>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card>
                  <template #header>
                    pass^k
                    <el-tag size="small" type="info" style="margin-left: 8px;">k 次重复全部成功的行比例</el-tag>
                  </template>
                  <div class="metric-value">
                    {{ safePercent(result.result.pass_k.average_pass_pow_k) }}%
                    <el-tag :type="result.result.pass_k.average_pass_pow_k >= 0.9 ? 'success' : (result.result.pass_k.average_pass_pow_k >= 0.5 ? 'warning' : 'danger')" size="small" style="margin-left: 8px;">
                      全过 {{ result.result.pass_k.all_pass_rows }}/{{ result.result.pass_k.rows }} 行
                    </el-tag>
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 自定义维度评分卡片 -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="hasCustomDimensions">
              <el-col
                v-for="(dimData, dimName) in result.result.custom_dimension_summary"
                :key="dimName"
                :span="8"
              >
                <el-card>
                  <template #header>{{ dimName }}</template>
                  <div class="metric-value">{{ dimData.average ?? '-' }}</div>
                  <div style="text-align: center; color: #909399; font-size: 12px;">
                    评分数量: {{ dimData.count ?? 0 }}
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 一致性评测明细表格 -->
            <el-divider />
            <h3>一致性评测明细</h3>
            <el-table
              :data="result.result.items || []"
              style="width: 100%"
              max-height="450"
              stripe
            >
              <el-table-column label="序号" width="60">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column
                v-for="col in getInputColumns(result.result.items)"
                :key="col.key"
                :prop="col.key"
                :label="col.label"
                min-width="150"
                show-overflow-tooltip
              >
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail(col.label, row[col.key])">
                    {{ truncateText(row[col.key], 60) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="文本相似度" width="120">
                <template #default="{ row }">
                  <el-tag :type="getConsistencyType(row.metrics?.text_similarity)" size="small">
                    {{ row.metrics?.text_similarity != null ? (row.metrics.text_similarity * 100).toFixed(1) + '%' : '-' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="格式一致性" width="120">
                <template #default="{ row }">
                  <el-tag :type="getConsistencyType(row.metrics?.format_consistency)" size="small">
                    {{ row.metrics?.format_consistency != null ? (row.metrics.format_consistency * 100).toFixed(1) + '%' : '-' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="结构一致性" width="120">
                <template #default="{ row }">
                  <el-tag :type="getConsistencyType(row.metrics?.structure_consistency)" size="small">
                    {{ row.metrics?.structure_consistency != null ? (row.metrics.structure_consistency * 100).toFixed(1) + '%' : '-' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="调用统计" width="120">
                <template #default="{ row }">
                  <span>{{ row.metrics?.successful_calls ?? '-' }}/{{ row.metrics?.total_calls ?? '-' }}</span>
                </template>
              </el-table-column>
              <!-- pass^k 行级徽标（M2 QE2-3）：旧结果无该键显示 '-' -->
              <el-table-column label="pass@1 / pass^k" width="150">
                <template #default="{ row }">
                  <template v-if="row.metrics?.pass_at_1 != null">
                    <el-tag :type="getConsistencyType(row.metrics?.pass_at_1)" size="small">
                      {{ safePercent(row.metrics?.pass_at_1) }}%
                    </el-tag>
                    <el-tag
                      :type="row.metrics?.pass_pow_k >= 1 ? 'success' : 'danger'"
                      size="small"
                      style="margin-left: 4px;"
                    >
                      {{ row.metrics?.pass_pow_k >= 1 ? '全过' : '未全过' }}
                    </el-tag>
                  </template>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="重复结果" width="100">
                <template #default="{ row }">
                  <el-button
                    v-if="row.repeat_results && row.repeat_results.length > 0"
                    link
                    type="primary"
                    size="small"
                    @click="openRepeatResults(row)"
                  >
                    查看 {{ row.repeat_results.length }} 条
                  </el-button>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="请求详情" width="120">
                <template #default="{ row }">
                  <el-dropdown v-if="row.repeat_results && row.repeat_results.length > 0" @command="(cmd) => openRequestDetail(row, cmd)" trigger="click">
                    <el-button link type="primary" size="small">
                      查看详情 <el-icon><ArrowDown /></el-icon>
                    </el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item
                          v-for="(r, idx) in row.repeat_results"
                          :key="idx"
                          :command="idx"
                        >
                          第 {{ r.repeat_id || idx + 1 }} 次
                          <el-tag :type="r.success ? 'success' : 'danger'" size="small" style="margin-left: 4px;">
                            {{ r.success ? '成功' : '失败' }}
                          </el-tag>
                        </el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column v-if="hasJudgeAssessment" label="裁判评估" width="180">
                <template #default="{ row }">
                  <!-- 多裁判独立折叠展示 -->
                  <el-collapse v-if="row.judge_assessments && row.judge_assessments.length > 0" style="width: 100%;">
                    <el-collapse-item
                      v-for="(ja, idx) in row.judge_assessments"
                      :key="idx"
                      :name="idx"
                    >
                      <template #title>
                        <span style="font-size: 12px;">
                          裁判{{ ja.judge_index !== undefined ? ja.judge_index + 1 : idx + 1 }}
                          <el-tag v-if="ja.evaluation_target_name" size="small" style="margin-left: 4px;">{{ ja.evaluation_target_name }}</el-tag>
                          <el-tag v-if="ja.validation_message" type="danger" size="small" style="margin-left: 4px;">校验失败</el-tag>
                        </span>
                      </template>
                      <div style="font-size: 12px; padding: 4px 0;">
                        <div v-if="ja.scores">评分: {{ ja.scores.综合评分 ?? ja.overall_score ?? '-' }}</div>
                        <div v-if="ja.reasoning" style="color: #909399; margin-top: 4px;">{{ ja.reasoning }}</div>
                        <div v-if="ja.validation_message" style="color: #f56c6c; margin-top: 4px;">{{ ja.validation_message }}</div>
                      </div>
                    </el-collapse-item>
                  </el-collapse>
                  <!-- 单裁判或旧数据结构 -->
                  <el-button
                    v-else-if="row.judge_assessment && Object.keys(row.judge_assessment).length > 0"
                    link
                    type="primary"
                    size="small"
                    @click="openJudgeAssessment(row)"
                  >
                    查看
                  </el-button>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                    {{ row.success ? '成功' : '失败' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 成功率评测结果 -->
          <div v-if="result.evaluation_type === 'success_rate'">
            <el-row :gutter="20">
              <el-col :span="8">
                <el-card>
                  <template #header>整体成功率</template>
                  <div class="metric-value">{{ safePercent(result.result.overall_success_rate) }}%</div>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card>
                  <template #header>平均成功率</template>
                  <div class="metric-value">{{ safePercent(result.result.average_success_rate) }}%</div>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card>
                  <template #header>总调用次数</template>
                  <div class="metric-value">{{ result.result.total_calls ?? '-' }}</div>
                </el-card>
              </el-col>
            </el-row>
          </div>

          <!-- 耗时评测结果 -->
          <div v-if="result.evaluation_type === 'latency'" class="latency-result">
            <el-row :gutter="20">
              <el-col :span="6">
                <el-statistic title="百分制评分" :value="result.result.score" suffix="分">
                  <template #prefix>
                    <el-icon><Timer /></el-icon>
                  </template>
                </el-statistic>
              </el-col>
              <el-col :span="6">
                <el-statistic title="通过率" :value="((result.result.pass_rate || 0) * 100).toFixed(1)" suffix="%">
                  <template #prefix>
                    <el-icon><CircleCheck /></el-icon>
                  </template>
                </el-statistic>
              </el-col>
              <el-col :span="6">
                <el-statistic title="平均耗时" :value="(result.result.avg_duration || 0).toFixed(3)" suffix="秒" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="响应阈值" :value="result.result.threshold" suffix="秒" />
              </el-col>
            </el-row>

            <el-divider />

            <!-- 耗时分布直方图 -->
            <el-row :gutter="20" style="margin-top: 20px;">
              <el-col :span="16">
                <el-card>
                  <template #header>耗时分布直方图</template>
                  <div id="latencyHistogramChart" style="width: 100%; height: 300px;"></div>
                </el-card>
              </el-col>
              <el-col :span="8">
                <el-card>
                  <template #header>阈值参考</template>
                  <div class="threshold-info">
                    <div class="threshold-item">
                      <span class="threshold-label">响应阈值：</span>
                      <span class="threshold-value">{{ result.result.threshold }} 秒</span>
                    </div>
                    <div class="threshold-item success">
                      <span class="threshold-label">通过数：</span>
                      <span class="threshold-value">{{ result.result.passed_count || 0 }}</span>
                    </div>
                    <div class="threshold-item danger">
                      <span class="threshold-label">失败数：</span>
                      <span class="threshold-value">{{ result.result.failed_count || 0 }}</span>
                    </div>
                    <div class="threshold-item">
                      <span class="threshold-label">通过率：</span>
                      <span class="threshold-value">{{ ((result.result.pass_rate || 0) * 100).toFixed(1) }}%</span>
                    </div>
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 耗时分布表格 -->
            <h4>耗时分布</h4>
            <el-table :data="distributionData" stripe>
              <el-table-column prop="metric" label="指标" width="150" />
              <el-table-column prop="value" label="数值">
                <template #default="{ row }">
                  <span v-if="row.unit === '秒'">{{ row.value.toFixed(3) }} 秒</span>
                  <span v-else>{{ row.value }}</span>
                </template>
              </el-table-column>
            </el-table>

            <!-- 耗时明细 -->
            <h4>单次请求耗时明细</h4>
            <el-table :data="result.result.records || []" stripe max-height="400" :row-class-name="getLatencyRowClassName">
              <el-table-column prop="index" label="序号" width="80" />
              <el-table-column prop="duration" label="响应时间" width="120">
                <template #default="{ row }">
                  <span :class="{ 'text-danger': row.duration > (result.result.threshold || 10) }">
                    {{ (row.duration || 0).toFixed(3) }} 秒
                    <el-icon v-if="row.duration > (result.result.threshold || 10)" color="#F56C6C"><WarningFilled /></el-icon>
                  </span>
                </template>
              </el-table-column>
              <el-table-column prop="passed" label="状态" width="100">
                <template #default="{ row }">
                  <el-tag :type="row.passed ? 'success' : 'danger'">
                    {{ row.passed ? '通过' : '失败' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="input_tokens" label="输入Token" width="120" />
              <el-table-column prop="output_tokens" label="输出Token" width="120" />
              <el-table-column prop="timestamp" label="时间戳" />
            </el-table>
          </div>

          <!-- RAG 检索质量评测结果 -->
          <div v-if="result.evaluation_type === 'rag'">
            <!-- RAG 雷达图 -->
            <el-row :gutter="20" style="margin-bottom: 20px;">
              <el-col :span="12">
                <el-card>
                  <template #header>检索质量雷达图</template>
                  <div id="ragRadarChart" style="width: 100%; height: 350px;"></div>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card>
                  <template #header>统计概览</template>
                  <el-row :gutter="10" style="padding: 10px 0;">
                    <el-col :span="12">
                      <div class="stat-card-small">
                        <div class="stat-label">总题目数</div>
                        <div class="stat-value">{{ result.result.total_questions ?? '-' }}</div>
                      </div>
                    </el-col>
                    <el-col :span="12">
                      <div class="stat-card-small">
                        <div class="stat-label">成功评测</div>
                        <div class="stat-value success">{{ result.result.successful_questions ?? '-' }}</div>
                      </div>
                    </el-col>
                  </el-row>
                </el-card>
              </el-col>
            </el-row>

            <!-- 指标卡片 -->
            <el-row :gutter="12" style="margin-bottom: 20px;">
              <el-col
                v-for="(metricData, metricName) in (result.result.metrics_summary || {})"
                :key="metricName"
                :span="4"
              >
                <el-card shadow="hover" class="rag-metric-card clickable" @click="openMetricCardDetail(metricName)">
                  <div class="rag-metric-name">{{ ragMetricNames[metricName] || metricName }}</div>
                  <div class="rag-metric-score">{{ metricData.average != null ? metricData.average.toFixed(3) : '-' }}</div>
                  <el-tag :type="getRagMetricGrade(metricData.average).type" size="small" style="margin-top: 6px;">
                    {{ getRagMetricGrade(metricData.average).label }}
                  </el-tag>
                  <div class="rag-metric-range">
                    <span>最低: {{ metricData.min != null ? metricData.min.toFixed(3) : '-' }}</span>
                    <span>最高: {{ metricData.max != null ? metricData.max.toFixed(3) : '-' }}</span>
                  </div>
                  <div class="metric-click-hint">
                    <el-icon :size="12"><InfoFilled /></el-icon>
                    <span>点击查看说明</span>
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- RAG 明细表格 -->
            <el-divider />
            <h3>检索质量明细</h3>
            <el-table
              :data="result.result.detail_results || []"
              style="width: 100%"
              max-height="450"
              stripe
            >
              <el-table-column label="序号" width="60">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column
                v-for="col in getInputColumns(result.result.detail_results)"
                :key="col.key"
                :prop="col.key"
                :label="col.label"
                min-width="150"
                show-overflow-tooltip
              >
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail(col.label, row[col.key])">
                    {{ truncateText(row[col.key], 60) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column
                v-for="metricName in Object.keys(result.result.metrics_summary || {})"
                :key="metricName"
                :label="ragMetricNames[metricName] || metricName"
                width="120"
              >
                <template #default="{ row }">
                  <div class="metric-cell">
                    <el-tag
                      v-if="row.scores && row.scores[metricName] != null"
                      :type="getRagMetricGrade(row.scores[metricName]).type"
                      size="small"
                      class="metric-score"
                      @click="openMetricDetail(metricName, row)"
                      style="cursor: pointer;"
                    >
                      {{ row.scores[metricName].toFixed(3) }}
                    </el-tag>
                    <span v-else>-</span>
                    <!-- 评分理由提示图标 -->
                    <el-tooltip
                      v-if="row.reasons && row.reasons[metricName]"
                      :content="row.reasons[metricName]"
                      placement="top"
                      effect="light"
                    >
                      <el-icon class="reason-hint" :size="14"><InfoFilled /></el-icon>
                    </el-tooltip>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="评分理由" width="90">
                <template #default="{ row }">
                  <el-button
                    v-if="row.reasons && Object.keys(row.reasons).length > 0"
                    link
                    type="primary"
                    size="small"
                    @click="openReasonDialog(row)"
                  >
                    查看
                  </el-button>
                  <span v-else class="no-reason">-</span>
                </template>
              </el-table-column>
              <el-table-column prop="success" label="状态" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                    {{ row.success ? '成功' : '失败' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 二分分类评测结果 -->
          <div v-if="result.evaluation_type === 'binary_classification'">
            <el-row :gutter="20">
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">准确率 (Accuracy)</div>
                  <div class="binary-metric-rate">{{ safePercent(result.result.accuracy) }}%</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">精确率 (Precision)</div>
                  <div class="binary-metric-rate">{{ safePercent(result.result.precision) }}%</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">灵敏度 (Recall)</div>
                  <div class="binary-metric-rate">{{ safePercent(result.result.sensitivity) }}%</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">特异度 (Specificity)</div>
                  <div class="binary-metric-rate">{{ safePercent(result.result.specificity) }}%</div>
                </div>
              </el-col>
            </el-row>

            <el-row :gutter="20" style="margin-top: 16px;">
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">F1 Score</div>
                  <div class="binary-metric-rate">{{ safePercent(result.result.f1_score) }}%</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">MCC</div>
                  <div class="binary-metric-rate">{{ result.result.mcc ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">错误率 (Error Rate)</div>
                  <div class="binary-metric-rate">{{ safePercent(result.result.error_rate) }}%</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="binary-metric-card">
                  <div class="binary-metric-name">总样本数</div>
                  <div class="binary-metric-rate">{{ result.result.total_count ?? '-' }}</div>
                </div>
              </el-col>
            </el-row>

            <!-- 更多指标（可展开） -->
            <el-collapse style="margin-top: 16px;">
              <el-collapse-item title="更多指标">
                <el-row :gutter="20">
                  <el-col :span="6">
                    <div class="binary-metric-card">
                      <div class="binary-metric-name">F2 Score (偏重召回)</div>
                      <div class="binary-metric-rate">{{ safePercent(result.result.f2_score) }}%</div>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="binary-metric-card">
                      <div class="binary-metric-name">F0.5 Score (偏重精确)</div>
                      <div class="binary-metric-rate">{{ safePercent(result.result.f05_score) }}%</div>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="binary-metric-card">
                      <div class="binary-metric-name">阴性预测值 (NPV)</div>
                      <div class="binary-metric-rate">{{ safePercent(result.result.npv) }}%</div>
                    </div>
                  </el-col>
                  <el-col :span="6">
                    <div class="binary-metric-card">
                      <div class="binary-metric-name">阳性率 (Prevalence)</div>
                      <div class="binary-metric-rate">{{ safePercent(result.result.prevalence) }}%</div>
                    </div>
                  </el-col>
                </el-row>
                <el-row :gutter="20" style="margin-top: 16px;">
                  <el-col :span="6">
                    <div class="binary-metric-card">
                      <div class="binary-metric-name">Youden 指数</div>
                      <div class="binary-metric-rate">{{ result.result.youden_index ?? '-' }}</div>
                    </div>
                  </el-col>
                </el-row>
              </el-collapse-item>
            </el-collapse>

            <!-- 混淆矩阵 -->
            <el-divider />
            <h3>混淆矩阵</h3>
            <div class="confusion-matrix">
              <table class="cm-table">
                <thead>
                  <tr>
                    <th></th>
                    <th>预测正例</th>
                    <th>预测负例</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <th>实际正例</th>
                    <td class="cm-cell tp">TP: {{ result.result.confusion_matrix?.tp ?? 0 }}</td>
                    <td class="cm-cell fn">FN: {{ result.result.confusion_matrix?.fn ?? 0 }}</td>
                  </tr>
                  <tr>
                    <th>实际负例</th>
                    <td class="cm-cell fp">FP: {{ result.result.confusion_matrix?.fp ?? 0 }}</td>
                    <td class="cm-cell tn">TN: {{ result.result.confusion_matrix?.tn ?? 0 }}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- 逐条结果明细表格 -->
            <el-divider />
            <h3>逐条结果明细</h3>
            <el-table
              :data="result.result.items || []"
              style="width: 100%"
              max-height="450"
              stripe
            >
              <el-table-column label="序号" width="60">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column label="期望标签" width="100">
                <template #default="{ row }">
                  <el-tag :type="row.expected_label ? 'success' : 'info'" size="small">{{ row.expected_label }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="预测标签" width="100">
                <template #default="{ row }">
                  <el-tag :type="row.predicted_label ? 'success' : 'info'" size="small">{{ row.predicted_label }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="判定结果" width="100">
                <template #default="{ row }">
                  <el-tag :type="row.is_correct ? 'success' : 'danger'" size="small">
                    {{ row.is_correct ? '正确' : '错误' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="耗时" width="100">
                <template #default="{ row }">
                  {{ row.duration != null ? row.duration.toFixed(3) + 's' : '-' }}
                </template>
              </el-table-column>
              <el-table-column label="模型响应" min-width="200">
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('模型响应', row.model_output)">
                    {{ truncateText(row.model_output, 80) }}
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 安全评测结果（M3 QE3-1 双通道红队口径） -->
          <div v-if="result.evaluation_type === 'safety'">
            <!-- 违规告警横幅 -->
            <el-alert
              v-if="hasSafetyAlerts"
              :title="safetyAlertTitle"
              :type="safetyAlertType"
              :closable="false"
              show-icon
              style="margin-bottom: 20px;"
            >
              <template #default>
                <div>检出 {{ safetyAlertCount }} 条违规（攻击成功），请展开明细查看证据与复测建议</div>
              </template>
            </el-alert>
            <el-alert
              v-else-if="result.result?.executed"
              title="防守成功：本轮攻击样本全部通过双通道检测"
              type="success"
              :closable="false"
              show-icon
              style="margin-bottom: 20px;"
            />

            <!-- 红队总览：样本数 / 违规数 / 防御率 / 变异体防御率 -->
            <el-row :gutter="20">
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">攻击样本数</div>
                  <div class="stat-value">{{ result.result?.executed ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">违规（攻击成功）</div>
                  <div class="stat-value danger">{{ result.result?.violations ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">防御率</div>
                  <div class="stat-value highlight">{{ safePercent(result.result?.defense_rate) }}%</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">变异体防御率</div>
                  <div class="stat-value">{{ result.result?.mutation?.defense_rate != null ? safePercent(result.result.mutation.defense_rate) + '%' : '未开启' }}</div>
                  <div class="stat-sub">变异体 {{ result.result?.mutation?.variants || 0 }} 条</div>
                </div>
              </el-col>
            </el-row>

            <!-- 双通道命中分布（+ deepeval 指标开关，M3 QE3-2） -->
            <el-row :gutter="20" style="margin-top: 20px;">
              <el-col :span="12">
                <div class="stat-card-small">
                  <div class="stat-label">确定性通道（PII/敏感词，命中即 fail）</div>
                  <div class="stat-value">命中 {{ result.result?.by_channel?.deterministic?.triggered ?? 0 }} 条</div>
                </div>
              </el-col>
              <el-col :span="12">
                <div class="stat-card-small">
                  <div class="stat-label">裁判 rubric 通道</div>
                  <div class="stat-value">
                    违规 {{ result.result?.by_channel?.judge_rubric?.violations ?? 0 }} 条
                    <span v-if="result.result?.by_channel?.judge_rubric?.unavailable" class="stat-sub">
                      （{{ result.result.by_channel.judge_rubric.unavailable }} 条通道不可用，不计违规）
                    </span>
                  </div>
                </div>
              </el-col>
            </el-row>
            <el-row v-if="hasSafetyDeepeval" :gutter="20" style="margin-top: 20px;">
              <el-col :span="24">
                <div class="stat-card-small">
                  <div class="stat-label">deepeval 指标通道（可选增强）</div>
                  <div class="safety-metric-list">
                    <el-tag
                      v-for="(stats, metric) in result.result.by_channel.deepeval.metrics"
                      :key="metric"
                      :type="stats.violations > 0 ? 'danger' : 'success'"
                      size="small"
                      style="margin-right: 8px;"
                    >
                      {{ metric }}：违规 {{ stats.violations }}<template v-if="stats.unavailable">（不可用 {{ stats.unavailable }}）</template>
                    </el-tag>
                  </div>
                </div>
              </el-col>
            </el-row>

            <!-- 攻击面类目统计 -->
            <el-divider />
            <h3>攻击面类目统计</h3>
            <el-table :data="safetyCategoryRows" style="width: 100%" max-height="300" stripe>
              <el-table-column label="攻击面" prop="name" min-width="140" />
              <el-table-column label="样本数" prop="total" width="100" />
              <el-table-column label="违规数" width="100">
                <template #default="{ row }">
                  <span :style="row.violations > 0 ? 'color: #f56c6c; font-weight: 600;' : ''">{{ row.violations }}</span>
                </template>
              </el-table-column>
              <el-table-column label="防御率" width="180">
                <template #default="{ row }">
                  <el-progress
                    :percentage="Math.round((row.defense_rate || 0) * 100)"
                    :status="(row.defense_rate || 0) >= 0.9 ? 'success' : ((row.defense_rate || 0) >= 0.7 ? 'warning' : 'exception')"
                    :stroke-width="14"
                    :text-inside="true"
                  />
                </template>
              </el-table-column>
            </el-table>

            <!-- 类目热力（M3 QE3-3：攻击面 × 检出通道违规集中度，颜色越深越集中） -->
            <div class="safety-heatmap-card">
              <div class="safety-heatmap-title">
                类目热力：攻击面 × 检出通道
                <span class="safety-heatmap-sub">单元格为该类目在该通道的违规/命中条数；悬停查看明细</span>
              </div>
              <div id="safetyCategoryHeatmap" class="safety-heatmap-chart"></div>
              <div v-if="!safetyHeatmapRows.length" class="safety-heatmap-empty">暂无类目数据</div>
            </div>

            <!-- 逐条明细（违规行筛选 + 证据引用） -->
            <el-divider />
            <div class="safety-detail-header">
              <h3 style="margin: 0;">攻击明细</h3>
              <el-checkbox v-model="safetyOnlyViolations">仅看违规行</el-checkbox>
            </div>
            <el-table :data="safetyItems" style="width: 100%" max-height="450" stripe>
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="safety-evidence-panel">
                    <!-- 证据引用串：评审/合规材料可直接引用定位到行 -->
                    <div class="evidence-cite">
                      <span class="cite-label">证据引用</span>
                      <code>{{ safetyEvidenceCite(row) }}</code>
                    </div>
                    <el-descriptions :column="2" size="small" border style="margin-bottom: 12px;">
                      <el-descriptions-item label="样本 ID">{{ row.attack_sample_id || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="期望行为">{{ safetyExpectedBehaviorLabel(row.expected_behavior) }}</el-descriptions-item>
                      <el-descriptions-item label="风险点">{{ row.risk_point || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="样本溯源">
                        {{ row.is_mutation ? `变体（改写自 ${row.mutated_from || '-'}）` : '原始样本' }}
                      </el-descriptions-item>
                    </el-descriptions>
                    <div v-if="(row.safety?.channels?.deterministic?.findings || []).length" class="evidence-block">
                      <div class="evidence-block-title">确定性通道证据（掩码展示，不泄露原文）</div>
                      <ul class="evidence-list">
                        <li v-for="(f, i) in row.safety.channels.deterministic.findings" :key="i">
                          <el-tag size="small" type="danger">{{ f.label }}</el-tag>
                          <code class="evidence-text">{{ f.masked || f.evidence }}</code>
                          <span v-if="f.note" class="evidence-note">{{ f.note }}</span>
                        </li>
                      </ul>
                    </div>
                    <div
                      v-if="row.safety?.channels?.judge_rubric?.reason || row.safety?.channels?.judge_rubric?.evidence"
                      class="evidence-block"
                    >
                      <div class="evidence-block-title">裁判 rubric 证据</div>
                      <div class="evidence-text-block">{{ row.safety.channels.judge_rubric.reason }}</div>
                      <div v-if="row.safety.channels.judge_rubric.evidence" class="evidence-quote">
                        回复摘录：「{{ row.safety.channels.judge_rubric.evidence }}」
                      </div>
                    </div>
                    <div
                      v-if="row.safety?.channels?.deepeval && Object.keys(row.safety.channels.deepeval).length"
                      class="evidence-block"
                    >
                      <div class="evidence-block-title">deepeval 指标证据（M3 QE3-2）</div>
                      <div class="evidence-metric-list">
                        <span v-for="(m, key) in row.safety.channels.deepeval" :key="key" class="evidence-metric-item">
                          <el-tag
                            size="small"
                            :type="m.metric_unavailable ? 'info' : (m.success === false ? 'danger' : 'success')"
                          >
                            {{ key }}：{{ m.metric_unavailable ? '不可用（不计违规）' : (m.success === false ? `违规（score=${m.score}）` : `通过（score=${m.score}）`) }}
                          </el-tag>
                          <span v-if="m.reason" class="evidence-note">{{ m.reason }}</span>
                        </span>
                      </div>
                    </div>
                    <div v-if="!hasSafetyEvidence(row)" class="evidence-empty">
                      本行无通道证据（双通道均未命中，防守成功行）
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="序号" width="60">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column label="攻击面" width="110">
                <template #default="{ row }">{{ row.attack_category_name || '未分类' }}</template>
              </el-table-column>
              <el-table-column label="变异" width="60">
                <template #default="{ row }">
                  <el-tag v-if="row.is_mutation" size="small" type="warning">变体</el-tag>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="攻击内容" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('攻击内容', row.user_input)">
                    {{ truncateText(row.user_input, 50) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="模型回复" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('模型回复', row.model_output)">
                    {{ truncateText(row.model_output, 50) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="确定性通道" width="130">
                <template #default="{ row }">
                  <el-tooltip
                    v-if="row.safety?.channels?.deterministic?.triggered"
                    :content="safetyFindingsTooltip(row)"
                    placement="top"
                  >
                    <el-tag type="danger" size="small">命中（证据）</el-tag>
                  </el-tooltip>
                  <el-tag v-else type="success" size="small">未命中</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="裁判 rubric" width="130">
                <template #default="{ row }">
                  <el-tooltip
                    v-if="row.safety?.channels?.judge_rubric?.reason"
                    :content="row.safety.channels.judge_rubric.reason"
                    placement="top"
                  >
                    <el-tag :type="safetyJudgeTagType(row)" size="small">{{ safetyJudgeLabel(row) }}</el-tag>
                  </el-tooltip>
                  <el-tag v-else :type="safetyJudgeTagType(row)" size="small">{{ safetyJudgeLabel(row) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="综合判定" width="100">
                <template #default="{ row }">
                  <el-tag :type="getSafetyItemTagType(row.safety?.violation)" size="small">
                    {{ row.safety?.violation ? '违规' : '防守成功' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="严重度" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.safety?.severity" :type="safetySeverityTagType(row.safety.severity)" size="small">
                    {{ { high: '高', medium: '中', low: '低' }[row.safety.severity] || row.safety.severity }}
                  </el-tag>
                  <span v-else>-</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 插件评测结果 -->
          <div v-if="result.evaluation_type === 'plugin'" class="plugin-result">
            <!-- 统计卡（trace 合并口径：一份报告两套分数——设计质量综合分 / 真实数据分）
                 得分必须与样本量同屏：真实数据分的分母只有「通过 + 失败」，
                 证据不足的检查不进分母，故得分高只代表已判定的少量样本里没有违规 -->
            <!-- 两条线分开显示：设计质量线（按插件只评一次） / 历史数据线（逐行线程） -->
            <template v-if="isTraceMode">
            <div class="trace-line-title">设计质量线（每个插件只评测一次）</div>
            <el-row :gutter="20">
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">平均综合分（设计质量）</div>
                  <div class="stat-value highlight">{{ traceCompositeSummary.average_score ?? '-' }}</div>
                  <div class="trace-note">
                    <template v-if="traceCompositeSummary.average_score != null">
                      静态层 + LLM 评审；{{ traceCompositeSummary.measured_count }} 个插件出分
                    </template>
                    <template v-else>本次执行无综合分口径（改动前的记录只出真实数据分）</template>
                  </div>
                </div>
              </el-col>
              <el-col :span="18">
                <div class="stat-card-small">
                  <div class="stat-label">设计质量线口径</div>
                  <div class="trace-note" style="line-height: 1.9;">
                    只与插件目录有关，与线程无关：同一插件（同版本、同市场路径、同判定方式）
                    在任务内只跑一次静态层 + judge，其余行复用该结果，只补历史线。
                    两条线相互独立，不并入计算。
                  </div>
                </div>
              </el-col>
            </el-row>

            <div class="trace-line-title" style="margin-top: 20px;">历史数据线（逐行线程）</div>
            <el-row :gutter="20">
              <el-col :span="8">
                <div class="stat-card-small">
                  <div class="stat-label">{{ traceSummary.score_label || '真实数据得分' }}</div>
                  <div class="stat-value highlight">{{ traceSummary.average_score ?? '-' }}</div>
                  <div class="trace-note" v-if="traceSummary.average_score != null">
                    得分只含已判定的 <b>{{ traceSummary.decidable_count ?? 0 }} / {{ traceSummary.applicable_count ?? 0 }}</b> 项检查
                    <template v-if="traceSummary.conservative_score != null">
                      （{{ traceSummary.applicable_count - traceSummary.decidable_count }} 项证据不足未计分；若按违规计，下界 {{ traceSummary.conservative_score }}）
                    </template>
                  </div>
                  <el-tag
                    v-if="traceSummary.score_label === '部分样本得分'"
                    size="small"
                    type="warning"
                    effect="plain"
                    style="margin-top: 6px;"
                  >评审不完整，不代表整条线程</el-tag>
                </div>
              </el-col>
              <el-col :span="8">
                <div class="stat-card-small">
                  <div class="stat-label">平均判定有效率</div>
                  <div class="stat-value">{{ traceSummary.average_coverage != null ? traceSummary.average_coverage + '%' : '-' }}</div>
                  <div class="trace-note">可判定检查 ÷ 适用检查</div>
                </div>
              </el-col>
              <el-col :span="8">
                <div class="stat-card-small">
                  <div class="stat-label">{{ traceCoverageLabel }}</div>
                  <div class="stat-value">{{ traceSummary.average_behavior_coverage != null ? traceSummary.average_behavior_coverage + '%' : '-' }}</div>
                  <div class="trace-note">只展示、不计分</div>
                </div>
              </el-col>
            </el-row>

            <!-- 设计质量线明细：按插件一条，覆盖行数/复用行数直接说明「只评一次」 -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="traceDesignPlugins.length">
              <el-col :span="24">
                <el-card>
                  <template #header>设计质量线明细（按插件）</template>
                  <el-table :data="traceDesignPlugins" size="small" stripe border>
                    <el-table-column type="expand">
                      <template #default="{ row }">
                        <div style="padding: 4px 12px;">
                          <template v-if="(row.dimensions || []).length">
                            <h5>维度评分</h5>
                            <el-table :data="row.dimensions" size="small" border>
                              <el-table-column prop="label" label="维度" min-width="140" />
                              <el-table-column label="权重" width="90">
                                <template #default="{ row: dim }">{{ dim.weight }}</template>
                              </el-table-column>
                              <el-table-column label="得分" width="90">
                                <template #default="{ row: dim }">
                                  <span v-if="dim.score !== null && dim.score !== undefined">{{ Number(dim.score).toFixed(3) }}</span>
                                  <span v-else>未测</span>
                                </template>
                              </el-table-column>
                              <el-table-column prop="grade" label="等级" width="70" />
                            </el-table>
                          </template>
                          <template v-if="(row.skills || []).length">
                            <h5>Skill 明细</h5>
                            <el-table :data="row.skills" size="small" border>
                              <el-table-column prop="name" label="Skill" min-width="180" />
                              <el-table-column label="综合分" width="100">
                                <template #default="{ row: skill }">{{ skill.score ?? '未测' }}</template>
                              </el-table-column>
                              <el-table-column label="徽章" width="120">
                                <template #default="{ row: skill }">{{ getBadgeLabel(skill.badge) }}</template>
                              </el-table-column>
                              <el-table-column label="已测维度" width="100">
                                <template #default="{ row: skill }">{{ skill.measured_dimensions }} / {{ skill.dimension_count }}</template>
                              </el-table-column>
                              <el-table-column label="各层得分" min-width="220">
                                <template #default="{ row: skill }">{{ traceSkillLayers(skill) }}</template>
                              </el-table-column>
                            </el-table>
                          </template>
                          <div class="trace-note">
                            评测深度：{{ traceDepthLabel(row.depth) }}；
                            模型用量：{{ traceModelUsageText(row.model_usage) }}；
                            本插件设计质量评测耗时：{{ row.duration_ms != null ? row.duration_ms + ' ms' : '-' }}
                          </div>
                        </div>
                      </template>
                    </el-table-column>
                    <el-table-column prop="plugin" label="插件" min-width="180" />
                    <el-table-column label="设计质量分" width="120">
                      <template #default="{ row }">
                        <el-tag v-if="row.score !== null" :type="getScoreType(row.score)">{{ row.score }}</el-tag>
                        <span v-else class="trace-note">未出</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="徽章" width="120">
                      <template #default="{ row }">{{ getBadgeLabel(row.badge) }}</template>
                    </el-table-column>
                    <el-table-column prop="confidence_label" label="置信标签" width="120" />
                    <el-table-column label="覆盖行数" width="100">
                      <template #default="{ row }">{{ row.covered_rows }}</template>
                    </el-table-column>
                    <el-table-column label="复用行数" width="100">
                      <template #default="{ row }">{{ row.reused_rows }}</template>
                    </el-table-column>
                    <el-table-column label="反模式条数" width="100">
                      <template #default="{ row }">{{ row.anti_pattern_count ?? 0 }}</template>
                    </el-table-column>
                  </el-table>
                  <div class="trace-note">
                    同一插件在任务内只跑一次静态层与 judge；其余行只跑历史线并复用本结果。
                    覆盖行数 = 该插件参与评测的数据行数，复用行数 = 其中未重复跑设计质量的行数。
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 历史数据线阶段汇总：报告「测评总览」的生成/执行两行；未测阶段显示未测 -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="tracePhaseSummary.length">
              <el-col :span="24">
                <el-card>
                  <template #header>历史数据线 · 阶段汇总</template>
                  <el-table :data="tracePhaseSummary" size="small" stripe border>
                    <el-table-column prop="label" label="阶段" width="160" />
                    <el-table-column label="状态" width="110">
                      <template #default="{ row }">
                        <el-tag size="small" :type="traceStatusTagType(row.status)">{{ traceStatusLabel(row.status) }}</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column label="平均可信度" width="130">
                      <template #default="{ row }">
                        <span v-if="row.status === 'unmeasured'">—</span>
                        <span v-else>{{ traceNumber(row.average_score, '/100') }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="平均判定有效率" width="150">
                      <template #default="{ row }">
                        <span v-if="row.status === 'unmeasured'">—</span>
                        <span v-else>{{ traceNumber(row.average_coverage, '%') }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="可判定 / 适用（跨行求和）" min-width="200">
                      <template #default="{ row }">
                        <span v-if="row.status === 'unmeasured'">—</span>
                        <span v-else>{{ row.decidable }} / {{ row.applicable }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="出分行数 / 覆盖行数" width="170">
                      <template #default="{ row }">{{ row.measured_rows }} / {{ row.row_count }}</template>
                    </el-table-column>
                  </el-table>
                  <div class="trace-note">
                    未测阶段表示本批线程里没有该阶段任务（例如只调用了 /anc-tms:run、没有调用
                    testcase-generation）；该阶段不产生检查项，因此不显示 0 计数。
                  </div>
                </el-card>
              </el-col>
            </el-row>
            </template>

            <el-row v-else :gutter="20">
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">平均复合分</div>
                  <div class="stat-value highlight">{{ result.result.average_score ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">评测对象数</div>
                  <div class="stat-value">{{ result.result.total ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">成功</div>
                  <div class="stat-value success">{{ result.result.success_count ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">失败</div>
                  <div class="stat-value danger">{{ result.result.failed_count ?? '-' }}</div>
                </div>
              </el-col>
            </el-row>

            <!-- 判定分布（trace 口径：真实数据分不产徽章/维度分） -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="isTraceMode">
              <el-col :span="12">
                <el-card>
                  <template #header>判定分布（逐检查项求和）</template>
                  <div class="trace-check-summary">
                    <el-tag type="success" size="large">通过 {{ traceCheckSummary.passed || 0 }}</el-tag>
                    <el-tag type="danger" size="large">失败 {{ traceCheckSummary.failed || 0 }}</el-tag>
                    <el-tag type="info" size="large">证据不足 {{ traceCheckSummary.insufficient || 0 }}</el-tag>
                    <el-tag type="warning" size="large">不适用 {{ traceCheckSummary.not_applicable || 0 }}</el-tag>
                  </div>
                  <div class="trace-note" style="margin-top: 8px;">
                    得分分母 = 通过 {{ traceCheckSummary.passed || 0 }} + 失败 {{ traceCheckSummary.failed || 0 }} = <b>{{ traceDecidable }} 项</b>；
                    证据不足的 {{ traceCheckSummary.insufficient || 0 }} 项<b>不计分、也不等于通过</b>（缺证据，需补产物或人工复核）；
                    不适用 {{ traceCheckSummary.not_applicable || 0 }} 项不进任何分母。
                  </div>
                  <div class="trace-note">
                    真实数据分 = 通过 ÷ (通过 + 失败) × 100；判定有效率 = 有效判定 ÷ 已抽取检查项。
                    必须与判定有效率一起看：有效率低时，得分只代表已判定的少量样本，不代表整条线程、也不代表当前插件版本。
                    该分数独立于综合分与徽章，不并入排名（plugin-eval 口径）。
                  </div>
                </el-card>
              </el-col>
              <el-col :span="12">
                <el-card>
                  <template #header>评分状态与限制</template>
                  <div v-for="(count, status) in (result.result.real_data_summary?.status_summary || {})" :key="status" style="font-size: 13px; line-height: 1.9;">
                    {{ traceStatusLabel(status) }}：<b>{{ count }}</b> 行
                  </div>
                  <div style="font-size: 13px; line-height: 1.9;">
                    评测行数：<b>{{ result.result.total ?? '-' }}</b>，失败 <b class="danger">{{ result.result.failed_count ?? 0 }}</b>
                  </div>
                  <div style="font-size: 13px; line-height: 1.9;">
                    真实数据已测 / 未测行：<b>{{ traceSummary.measured_count ?? 0 }} / {{ traceSummary.unmeasured_count ?? 0 }}</b>
                    <span style="color: #909399;">（未测行不计失败，原因见明细表）</span>
                  </div>
                  <div style="font-size: 13px; line-height: 1.9;">
                    设计质量线已出 / 未出插件：
                    <b>{{ traceCompositeSummary.measured_count ?? 0 }} / {{ traceCompositeSummary.unmeasured_count ?? 0 }}</b>
                    <span style="color: #909399;">（按插件统计；未出=该插件没有跑设计质量评测）</span>
                  </div>
                  <div class="trace-note" style="margin-top: 6px;">
                    两套分数相互独立：设计质量综合分（静态层 + LLM 评审，产徽章与维度分）与
                    真实数据分（历史线程，不并入综合分/徽章/排名）。
                  </div>
                  <div v-if="traceLimitations.length" style="margin-top: 8px;">
                    <div v-for="(text, idx) in traceLimitations" :key="idx" style="color: #909399; font-size: 12px; line-height: 1.7;">
                      · {{ text }}
                    </div>
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 方向分项（报告「分项」表的逐行汇总口径） -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="isTraceMode && traceDirectionRows.length">
              <el-col :span="24">
                <el-card>
                  <template #header>方向分项（逐行求和，按阶段）</template>
                  <el-table :data="traceDirectionRows" size="small" stripe>
                    <el-table-column prop="phase" label="阶段" width="140" />
                    <el-table-column prop="label" label="方向" width="190" />
                    <el-table-column label="平均可信度" width="140">
                      <template #default="{ row }">
                        <span>{{ row.average_score != null ? row.average_score + '/100' : '未测' }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="平均判定有效率" width="160">
                      <template #default="{ row }">
                        <span>{{ row.average_coverage != null ? row.average_coverage + '%' : '未测' }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="可判定 / 适用（跨行求和）" min-width="200">
                      <template #default="{ row }">{{ row.measured }} / {{ row.applicable }}</template>
                    </el-table-column>
                  </el-table>
                  <div style="color: #909399; font-size: 12px; margin-top: 8px;">
                    方向即 plugin-eval 评测口径（执行：报告准确性 / 流程遵循 / 异常处理可信度；
                    生成：来源一致性 / 需求覆盖 / 用例质量 / 生成流程与报告），按阶段分行。
                    未测阶段没有检查项，不出现在本表；可信度 = 通过 ÷ (通过 + 失败) × 100。
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 徽章分布（trace 合并口径下同样有综合分与徽章；改动前的旧记录无徽章，此时不显示） -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="badgeSummaryEntries.length">
              <el-col :span="24">
                <el-card>
                  <template #header>徽章分布</template>
                  <div class="badge-summary">
                    <div v-for="entry in badgeSummaryEntries" :key="entry.badge" class="badge-summary-item">
                      <el-tag :type="getBadgeTagType(entry.badge)" size="large" effect="dark">
                        {{ getBadgeLabel(entry.badge) }}
                      </el-tag>
                      <span class="badge-count">× {{ entry.count }}</span>
                    </div>
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <el-row
              :gutter="20"
              style="margin-top: 20px;"
              v-if="!isTraceMode || pluginDimensionRows.length || antiPatternRows.length"
            >
              <!-- 维度平均分 -->
              <el-col :span="12">
                <el-card>
                  <template #header>维度平均分（0-1）</template>
                  <el-table :data="pluginDimensionRows" stripe size="small">
                    <el-table-column prop="label" label="维度" width="140" />
                    <el-table-column label="均分" width="100">
                      <template #default="{ row }">
                        <span>{{ row.average.toFixed(3) }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column label="分值条" align="center">
                      <template #default="{ row }">
                        <el-progress :percentage="Math.round(row.average * 100)" :stroke-width="10" />
                      </template>
                    </el-table-column>
                  </el-table>
                </el-card>
              </el-col>
              <!-- 反模式汇总 -->
              <el-col :span="12">
                <el-card>
                  <template #header>反模式汇总</template>
                  <el-empty v-if="!antiPatternRows.length" description="未检测到反模式" :image-size="60" />
                  <el-table v-else :data="antiPatternRows" stripe size="small">
                    <el-table-column prop="flag" label="标记" width="200" />
                    <el-table-column prop="count" label="次数" width="70" />
                    <el-table-column label="最重严重度" width="110">
                      <template #default="{ row }">{{ row.max_severity != null ? Math.round(row.max_severity * 100) + '%' : '-' }}</template>
                    </el-table-column>
                    <el-table-column prop="description" label="说明" show-overflow-tooltip />
                  </el-table>
                </el-card>
              </el-col>
            </el-row>

            <!-- 逐插件明细 -->
            <el-divider />
            <h3>插件明细</h3>
            <el-table :data="result.result.items || []" style="width: 100%" stripe>
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="plugin-expand-detail">
                    <!-- trace 口径：线程锚点 + 规则 + 逐检查项明细 -->
                    <template v-if="isTraceMode">
                      <h5>线程与规则锚点</h5>
                      <el-descriptions :column="3" size="small" border>
                        <el-descriptions-item label="被测插件">{{ row.trace?.detected?.plugin || row.target_name || '-' }}</el-descriptions-item>
                        <el-descriptions-item label="市场收录路径">{{ row.trace?.plugin_source || '-' }}</el-descriptions-item>
                        <el-descriptions-item label="插件版本">{{ row.trace?.plugin_version || '-' }}</el-descriptions-item>
                        <el-descriptions-item label="会话 ID">{{ row.trace?.thread_id || row.trace?.detected?.session_id || '-' }}</el-descriptions-item>
                        <el-descriptions-item label="线程文件">{{ row.trace?.thread_file || '-' }}</el-descriptions-item>
                        <el-descriptions-item label="线程来源">{{ row.trace?.thread_origin || '-' }}</el-descriptions-item>
                        <el-descriptions-item label="线程 SHA256" :span="3">
                          <span style="word-break: break-all;">{{ row.trace?.thread_sha256 || '-' }}</span>
                        </el-descriptions-item>
                        <el-descriptions-item label="历史规则">
                          {{ row.trace?.rule?.file || '-' }} v{{ row.trace?.rule?.version || '-' }}
                        </el-descriptions-item>
                        <el-descriptions-item label="规则 SHA256" :span="2">
                          <span style="word-break: break-all;">{{ row.trace?.rule?.sha256 || '-' }}</span>
                        </el-descriptions-item>
                        <el-descriptions-item label="调用识别" :span="3">
                          {{ traceCountsText(row.trace?.detected?.counts) || '-' }}
                        </el-descriptions-item>
                      </el-descriptions>
                      <template v-if="row.trace?.unavailable_reason">
                        <el-alert :title="row.trace.unavailable_reason" type="info" :closable="false" style="margin-top: 8px;" />
                      </template>

                      <!-- 设计质量线（报告「测评总览 / Dimension Scores / Skill 明细 / Anti-Patterns」）
                           同一插件任务内只评一次，本行展示的是同一份结果 -->
                      <template v-if="traceCompositeScore(row) !== null">
                        <h5>设计质量线（静态层 + LLM 评审）</h5>
                        <el-descriptions :column="4" size="small" border>
                          <el-descriptions-item label="综合分">{{ traceCompositeScore(row) }}/100</el-descriptions-item>
                          <el-descriptions-item label="徽章">{{ getBadgeLabel(row.badge) }}</el-descriptions-item>
                          <el-descriptions-item label="置信标签">{{ row.confidence_label || '-' }}</el-descriptions-item>
                          <el-descriptions-item label="反模式惩罚">
                            {{ row.anti_pattern_penalty !== null && row.anti_pattern_penalty !== undefined ? row.anti_pattern_penalty : '-' }}
                          </el-descriptions-item>
                          <el-descriptions-item label="评测深度">{{ traceDepthLabel(row.depth) }}</el-descriptions-item>
                          <el-descriptions-item label="反模式条数">{{ (row.anti_patterns || []).length }}</el-descriptions-item>
                          <el-descriptions-item label="Skill 数">{{ (row.skills || []).length }}</el-descriptions-item>
                          <el-descriptions-item label="本行设计质量来源">
                            {{ row.design_reused ? '复用任务内首次评测' : '本任务首次评测' }}
                          </el-descriptions-item>
                        </el-descriptions>
                        <div class="trace-note">
                          设计质量线只与插件有关，同一插件在任务内只评一次；本行若为「复用」，
                          展示的是首次评测得到的同一份结果。历史数据线来自本行线程，两条线相互独立。
                          维度与各层得分见下方「维度评分 / 各层得分」。
                        </div>

                        <template v-if="(row.skills || []).length">
                          <h5>Skill 明细</h5>
                          <el-table :data="row.skills" size="small" border max-height="360">
                            <el-table-column prop="name" label="Skill" min-width="180" show-overflow-tooltip />
                            <el-table-column label="综合分" width="100">
                              <template #default="{ row: skill }">{{ skill.score !== null && skill.score !== undefined ? skill.score + '/100' : '未测' }}</template>
                            </el-table-column>
                            <el-table-column label="徽章" width="120">
                              <template #default="{ row: skill }">
                                <el-tag size="small" :type="getBadgeTagType(skill.badge)" effect="dark">
                                  {{ getBadgeLabel(skill.badge) }}
                                </el-tag>
                              </template>
                            </el-table-column>
                            <el-table-column label="已测维度" width="100">
                              <template #default="{ row: skill }">{{ skill.measured_dimensions }} / {{ skill.dimension_count }}</template>
                            </el-table-column>
                            <el-table-column label="各层得分" min-width="220">
                              <template #default="{ row: skill }">
                                {{ traceSkillLayers(skill) }}
                              </template>
                            </el-table-column>
                            <el-table-column label="反模式" width="90">
                              <template #default="{ row: skill }">{{ skill.anti_pattern_count || 0 }}</template>
                            </el-table-column>
                          </el-table>
                        </template>
                      </template>

                      <!-- 真实数据测评指标（报告「真实数据测评指标」一节） -->
                      <template v-if="tracePhaseBlocks(row).length">
                        <h5>真实数据测评指标</h5>
                        <div class="trace-metric-line">
                          <b>{{ traceScoreLabel(row.real_data) }}</b>；判定有效率：{{ traceNumber(row.real_data?.coverage, '%') }}（可判定 / 适用 {{ traceMeasuredText(row.real_data?.counts) }}）；关键步骤覆盖率：{{ traceCoverageText(row.real_data?.behavior_coverage) }}
                        </div>
                        <div class="trace-note">
                          得分只由已判定的检查得出（通过 ÷ (通过 + 失败)）；本例可判定 {{ traceMeasuredText(row.real_data?.counts) }} 项，
                          若把证据不足的检查一并按违规计，下界为 {{ traceConservativeScore(row.real_data?.counts) }}。
                          判定有效率偏低时，得分只代表少量样本，不代表整条线程，也不代表当前插件版本。
                        </div>
                        <div class="trace-note">
                          测评范围：仅关键步骤（规则定义的准备、查证、冻结、执行、业务写、登录、委派、出报、上传与结论节点；只读查询按步骤确认是否调用，连续重试合并为一次）。
                        </div>
                        <div class="trace-note">
                          判定有效率 = 可判定检查 ÷ 适用检查；关键步骤覆盖率 = 至少有一个检查项的关键步骤实例 ÷ 关键步骤实例总数，只展示、不计分。
                        </div>
                        <div class="trace-note">
                          与原有插件综合分独立，不混合计算。检查项数量不等同于业务用例数量。抽取清单完整性：{{ row.real_data?.extraction_complete ? '已完成规则覆盖范围抽取。' : '未穷尽；覆盖率不能代表整条线程。' }}
                        </div>
                      </template>

                      <!-- 逐阶段（历史执行/生成可信度）：与报告一节一一对应 -->
                      <template v-for="phase in tracePhaseBlocks(row)" :key="phase.metric">
                        <h5>{{ phase.label }}</h5>
                        <el-alert
                          v-if="tracePhaseUnmeasured(phase)"
                          :title="phase.unmeasured_reason || '本批线程未发现该阶段的受支持任务，该阶段未测'"
                          type="info"
                          :closable="false"
                          style="margin-bottom: 8px;"
                        />
                        <el-descriptions :column="3" size="small" border>
                          <el-descriptions-item label="插件">{{ phase.plugin || row.target_name || '-' }}</el-descriptions-item>
                          <el-descriptions-item label="状态">
                            <el-tag size="small" :type="traceStatusTagType(phase.status)">{{ traceStatusLabel(phase.status) }}</el-tag>
                          </el-descriptions-item>
                          <el-descriptions-item label="可信度得分">
                            <span v-if="tracePhaseUnmeasured(phase)">—</span>
                            <span v-else>{{ traceNumber(phase.score, '/100') }}</span>
                          </el-descriptions-item>
                          <el-descriptions-item label="判定有效率">
                            <span v-if="tracePhaseUnmeasured(phase)">—</span>
                            <span v-else>{{ traceNumber(phase.coverage, '%') }}</span>
                          </el-descriptions-item>
                          <el-descriptions-item label="关键步骤覆盖率">
                            <span v-if="tracePhaseUnmeasured(phase)">—</span>
                            <span v-else>{{ traceCoverageText(phase.behavior_coverage) }}</span>
                          </el-descriptions-item>
                          <el-descriptions-item label="可判定 / 适用检查">
                            <span v-if="tracePhaseUnmeasured(phase)">—</span>
                            <span v-else>{{ traceMeasuredText(phase.counts) }}</span>
                          </el-descriptions-item>
                          <el-descriptions-item label="通过 / 失败 / 证据不足 / 不适用">
                            <span v-if="tracePhaseUnmeasured(phase)">—</span>
                            <span v-else>{{ traceCountsSlash(phase.counts) }}</span>
                          </el-descriptions-item>
                          <el-descriptions-item label="独立任务数">{{ phase.source?.independent_user_task_count ?? '-' }}</el-descriptions-item>
                          <el-descriptions-item label="会话 ID">{{ phase.thread_id || '-' }}</el-descriptions-item>
                          <el-descriptions-item label="规则文件">{{ phase.rule?.file || '-' }}</el-descriptions-item>
                          <el-descriptions-item label="规则版本">{{ phase.rule?.version || '-' }}</el-descriptions-item>
                          <el-descriptions-item label="规则 SHA256">
                            <span style="word-break: break-all;">{{ phase.rule?.sha256 || '-' }}</span>
                          </el-descriptions-item>
                        </el-descriptions>

                        <template v-if="!tracePhaseUnmeasured(phase) && (phase.dimensions || []).length">
                          <h5>分项</h5>
                          <el-table :data="phase.dimensions" size="small" border>
                            <el-table-column prop="label" label="方向" width="180" />
                            <el-table-column label="可信度" width="130">
                              <template #default="{ row: dim }">{{ traceNumber(dim.score, '/100') }}</template>
                            </el-table-column>
                            <el-table-column label="判定有效率" width="140">
                              <template #default="{ row: dim }">{{ traceNumber(dim.coverage, '%') }}</template>
                            </el-table-column>
                            <el-table-column label="可判定 / 适用" min-width="140">
                              <template #default="{ row: dim }">{{ dim.measured }} / {{ dim.applicable }}</template>
                            </el-table-column>
                          </el-table>
                        </template>

                        <template v-if="(phase.key_steps || []).length">
                          <h5>关键步骤</h5>
                          <el-table :data="traceKeyStepRows(phase)" size="small" border>
                            <el-table-column prop="name" label="步骤" min-width="150" />
                            <el-table-column prop="instances" label="实例数" width="80" />
                            <el-table-column prop="attempts" label="工具尝试次数" width="120" />
                            <el-table-column prop="achieved" label="最终达成" width="90" />
                            <el-table-column prop="calls" label="本步调用确认" min-width="190" />
                            <el-table-column prop="checks" label="检查项数" width="100" />
                          </el-table>
                        </template>

                        <template v-if="traceUncoveredRows(phase).length">
                          <h5>未被检查的关键步骤</h5>
                          <el-table :data="traceUncoveredRows(phase)" size="small" border>
                            <el-table-column prop="name" label="类型" min-width="150" />
                            <el-table-column prop="count" label="数量" width="80" />
                          </el-table>
                        </template>

                        <template v-if="(phase.failed_checks || []).length">
                          <h5>失败项</h5>
                          <el-table :data="phase.failed_checks" size="small" border>
                            <el-table-column label="检查 / 对象" width="220">
                              <template #default="{ row: check }">{{ check.id }} / {{ check.subject }}</template>
                            </el-table-column>
                            <el-table-column label="方向" width="150">
                              <template #default="{ row: check }">{{ check.dimension_label || traceDimensionLabel(check.dimension) }}</template>
                            </el-table-column>
                            <el-table-column prop="expectation" label="预期" min-width="200" show-overflow-tooltip />
                            <el-table-column prop="actual" label="实际" min-width="200" show-overflow-tooltip />
                            <el-table-column prop="reason" label="判定依据" min-width="220" show-overflow-tooltip />
                          </el-table>
                        </template>

                        <template v-if="!tracePhaseUnmeasured(phase) && (phase.evidence || []).length">
                          <h5>引用证据</h5>
                          <el-table :data="phase.evidence.slice(0, 50)" size="small" border max-height="300">
                            <el-table-column prop="id" label="证据 ID" width="130" />
                            <el-table-column label="来源" width="220" show-overflow-tooltip>
                              <template #default="{ row: ev }">
                                {{ ev.source?.line != null ? 'L' + ev.source.line : '' }}
                                {{ ev.source?.tool_name || ev.source?.role || ev.kind || '' }}
                              </template>
                            </el-table-column>
                            <el-table-column prop="text" label="摘录（最多 500 字）" min-width="320" show-overflow-tooltip />
                          </el-table>
                          <div class="trace-note" v-if="phase.evidence.length > 50">
                            共 {{ phase.evidence.length }} 条，仅展示前 50 条；完整记录见行结果 plugin_eval.raw。
                          </div>
                        </template>

                        <template v-if="(phase.limitations || []).length">
                          <h5>限制与未验证项</h5>
                          <div v-for="(text, li) in phase.limitations" :key="li" class="trace-note">· {{ text }}</div>
                        </template>

                        <template v-if="traceModelUsage(phase).length">
                          <h5>模型用量</h5>
                          <div v-for="(usage, mi) in traceModelUsage(phase)" :key="mi" class="trace-note">
                            {{ usage.model }}：{{ usage.tokens }} tokens
                          </div>
                        </template>
                      </template>

                      <template v-if="(row.checks || []).length">
                        <h5>逐检查项明细（{{ row.checks_total ?? row.checks.length }} 项{{ row.checks_total > row.checks.length ? '，仅展示前 ' + row.checks.length + ' 项' : '' }}）</h5>
                        <el-table :data="row.checks" size="small" border max-height="420">
                          <el-table-column label="阶段" width="130">
                            <template #default="{ row: check }">{{ check.phase_label || tracePhaseLabel(check.phase) }}</template>
                          </el-table-column>
                          <el-table-column label="检查 / 对象" width="210">
                            <template #default="{ row: check }">{{ check.id }} / {{ check.subject }}</template>
                          </el-table-column>
                          <el-table-column label="方向" width="150">
                            <template #default="{ row: check }">{{ check.dimension_label || traceDimensionLabel(check.dimension) }}</template>
                          </el-table-column>
                          <el-table-column prop="rule_id" label="检查项" width="150" />
                          <el-table-column label="判定" width="90">
                            <template #default="{ row: check }">
                              <el-tag size="small" :type="traceCheckTagType(check.status)">{{ traceCheckLabel(check.status) }}</el-tag>
                            </template>
                          </el-table-column>
                          <el-table-column prop="expectation" label="预期" min-width="180" show-overflow-tooltip />
                          <el-table-column prop="actual" label="实际" min-width="180" show-overflow-tooltip />
                          <el-table-column prop="reason" label="判定依据" min-width="220" show-overflow-tooltip />
                          <el-table-column label="证据" width="150" show-overflow-tooltip>
                            <template #default="{ row: check }">{{ (check.evidence_ids || []).slice(0, 3).join(', ') }}</template>
                          </el-table-column>
                        </el-table>
                      </template>
                    </template>
                    <!-- 维度与各层得分：非 trace 行恒显示（与原行为一致）；trace 行在有综合分时显示 -->
                    <template v-if="!isTraceMode || (row.dimensions || []).length || (row.layers || []).length">
                    <h5>维度评分</h5>
                    <el-table :data="row.dimensions || []" size="small" border>
                      <el-table-column prop="label" label="维度" min-width="140" />
                      <el-table-column label="权重" width="90">
                        <template #default="{ row: dim }">{{ dim.weight }}</template>
                      </el-table-column>
                      <el-table-column label="得分" width="90">
                        <template #default="{ row: dim }">
                          <span v-if="dim.score !== null && dim.score !== undefined">{{ dim.score.toFixed(3) }}</span>
                          <span v-else>未测</span>
                        </template>
                      </el-table-column>
                      <el-table-column prop="grade" label="等级" width="70" />
                    </el-table>
                    <h5>各层得分</h5>
                    <el-table :data="row.layers || []" size="small" border>
                      <el-table-column prop="layer" label="层" width="120" />
                      <el-table-column label="层分" width="100">
                        <template #default="{ row: lr }">{{ lr.score !== undefined ? lr.score.toFixed(3) : '-' }}</template>
                      </el-table-column>
                      <el-table-column label="子分" min-width="280">
                        <template #default="{ row: lr }">
                          <span class="plugin-sub-scores">
                            {{ formatSubScores(lr.sub_scores) }}
                          </span>
                        </template>
                      </el-table-column>
                    </el-table>
                    </template>
                    <template v-if="row.warnings && row.warnings.length">
                      <h5>警告</h5>
                      <el-alert
                        v-for="(w, wi) in row.warnings"
                        :key="wi"
                        :title="w"
                        type="warning"
                        :closable="false"
                        style="margin-bottom: 6px;"
                      />
                    </template>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="评测对象" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">{{ row.target_name || '-' }}</template>
              </el-table-column>
              <el-table-column label="类型" width="90">
                <template #default="{ row }">
                  <el-tag size="small" effect="plain" type="info">{{ row.target_type === 'plugin' ? '插件' : 'Skill' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="isTraceMode" label="设计质量分（任务级）" width="140">
                <template #default="{ row }">
                  <el-tag v-if="traceCompositeScore(row) !== null" :type="getScoreType(traceCompositeScore(row))">
                    {{ traceCompositeScore(row) }}
                  </el-tag>
                  <span v-else class="trace-note">未出</span>
                  <div v-if="traceCompositeScore(row) !== null" class="trace-note">
                    {{ row.design_reused ? '复用' : '首次' }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column :label="isTraceMode ? '真实数据分（可判定/适用）' : '复合分'" width="190">
                <template #default="{ row }">
                  <template v-if="isTraceMode">
                    <el-tag v-if="traceRealScore(row) !== null" :type="getScoreType(traceRealScore(row))">
                      {{ traceRealScore(row) }}
                    </el-tag>
                    <span v-else class="trace-note">未测</span>
                    <span v-if="traceRealScore(row) !== null" class="trace-note">
                      （{{ traceMeasuredText(row.real_data?.counts) }}）
                    </span>
                    <div v-if="traceIsLegacyRow(row)" class="trace-note">
                      旧口径记录：此列即该行的得分，无综合分
                    </div>
                  </template>
                  <template v-else>
                    <el-tag v-if="row.score !== null && row.score !== undefined" :type="getScoreType(row.score)">
                      {{ row.score }}
                    </el-tag>
                    <span v-else>-</span>
                  </template>
                </template>
              </el-table-column>
              <template v-if="isTraceMode">
                <el-table-column label="徽章" width="100">
                  <template #default="{ row }">
                    <el-tag v-if="traceCompositeScore(row) !== null" :type="getBadgeTagType(row.badge)" effect="dark">
                      {{ getBadgeLabel(row.badge) }}
                    </el-tag>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
                <el-table-column label="深度" width="80">
                  <template #default="{ row }">{{ traceDepthLabel(row.depth) }}</template>
                </el-table-column>
                <el-table-column label="判定有效率" width="110">
                  <template #default="{ row }">
                    <span>{{ row.real_data?.coverage !== undefined && row.real_data?.coverage !== null ? row.real_data.coverage + '%' : '-' }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="关键步骤覆盖率" width="130">
                  <template #default="{ row }">
                    <span>{{ row.real_data?.behavior_coverage?.rate !== undefined ? row.real_data.behavior_coverage.rate.toFixed(1) + '%' : '-' }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="判定（通过/失败/不足）" width="180">
                  <template #default="{ row }">
                    <span v-if="row.real_data?.counts">
                      {{ row.real_data.counts.passed || 0 }} / {{ row.real_data.counts.failed || 0 }} / {{ row.real_data.counts.insufficient || 0 }}
                    </span>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="110">
                  <template #default="{ row }">
                    <el-tag size="small" :type="traceStatusTagType(row.real_data?.status)">
                      {{ traceStatusLabel(row.real_data?.status) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="未测原因" min-width="220" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.unmeasured_reason || '-' }}</template>
                </el-table-column>
              </template>
              <template v-else>
                <el-table-column label="徽章" width="110">
                  <template #default="{ row }">
                    <el-tag :type="getBadgeTagType(row.badge)" effect="dark">
                      {{ getBadgeLabel(row.badge) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="置信标签" width="110">
                  <template #default="{ row }">
                    <el-tag size="small" effect="plain">{{ row.confidence_label || '-' }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="深度" width="90">
                  <template #default="{ row }">{{ row.depth === 'standard' ? '标准' : '快速' }}</template>
                </el-table-column>
                <el-table-column label="反模式" width="90">
                  <template #default="{ row }">
                    <el-tag v-if="(row.anti_patterns || []).length" type="danger" size="small">
                      {{ row.anti_patterns.length }}
                    </el-tag>
                    <span v-else>0</span>
                  </template>
                </el-table-column>
              </template>
            </el-table>
          </div>

          <!-- 详细结果表格 - 仅对 judge / agent / success_rate / dialogue 类型显示 -->
          <template v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety' && result.evaluation_type !== 'plugin'">
            <el-divider />
            <h3>详细结果</h3>
            <el-table :data="resultItems" style="width: 100%" max-height="400">
              <el-table-column label="序号" width="60">
                <!-- 合并 failed_items 后 $index 与数据序号错位：序号以数据自带 idx 为准；
                     idx 全行存在且唯一才按 idx+1（历史脏数据全行 idx=0 时回退位置序号，避免全列显示 1） -->
                <template #default="{ row, $index }">{{ detailSeqNo(row, $index, seqNoFromIdx) }}</template>
              </el-table-column>
              <!-- 状态：区分成功/失败数据行；执行失败与裁判失败可点击查看失败详情，空输入与成功不可点击 -->
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.success === false" type="danger" class="failure-tag-clickable" @click="openFailureDetail(row)">
                    执行失败
                  </el-tag>
                  <el-tag v-else-if="row.row_status === 'judge_failed'" type="warning" class="failure-tag-clickable" @click="openFailureDetail(row)">
                    裁判失败
                  </el-tag>
                  <el-tag v-else-if="row.row_status === 'empty_input'" type="info">
                    空输入
                  </el-tag>
                  <el-tag v-else type="success">成功</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="average_score" label="评分" width="100">
                <template #default="{ row }">
                  <el-tag v-if="row.average_score !== undefined" :type="getScoreType(row.average_score)">
                    {{ row.average_score.toFixed(2) }}
                  </el-tag>
                </template>
              </el-table-column>
              <!-- 多 Trial 行徽标（M2 QE2-3）：trials_count 存在才显示，评分列为 k 次均值 -->
              <el-table-column label="多 Trial" width="140" v-if="resultItems.some(r => r.trials_count > 1)">
                <template #default="{ row }">
                  <template v-if="row.trials_count > 1">
                    <el-tag size="small" :type="row.pass_pow_k >= 1 ? 'success' : 'danger'">
                      pass^{{ row.trials_count }} {{ row.pass_pow_k >= 1 ? '全过' : '未全过' }}
                    </el-tag>
                    <div class="trials-sub">{{ safePercent(row.pass_at_1) }}% · ±{{ row.score_sem ?? 0 }}</div>
                  </template>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column prop="judge_count" label="裁判数" width="80" />
              <el-table-column prop="feedback" label="反馈" min-width="200">
                <template #default="{ row }">
                  <!-- judge_scores 折叠面板 - 多裁判横向汇总，点击后垂直展开详情 -->
                  <div v-if="row.judge_scores && row.judge_scores.length > 0" class="judge-scores-summary">
                    <el-tag size="small" style="margin-right: 4px;">{{ row.judge_count }}位裁判</el-tag>
                    <span
                      v-for="(js, idx) in row.judge_scores"
                      :key="idx"
                      class="judge-score-pill"
                      :class="{ 'judge-score-pill-clickable': js.feedback || js.judge_failed || js.fail_reason || (js.dimension_scores && Object.keys(js.dimension_scores).length > 0) || js.langfuse_trace_url }"
                      @click.stop="(js.feedback || js.judge_failed || js.fail_reason || (js.dimension_scores && Object.keys(js.dimension_scores).length > 0) || js.langfuse_trace_url) && openJudgeDetail(row, idx)"
                    >
                      裁判{{ js.judge_index !== undefined ? js.judge_index + 1 : idx + 1 }}：{{ js.score !== undefined ? js.score : '-' }}
                    </span>
                  </div>
                  <!-- 无 judge_scores 时回退为文本预览 -->
                  <div v-else class="cell-preview" @click="openDetail('评测反馈', row.feedback)">
                    {{ truncateText(row.feedback, 80) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column
                v-for="col in getInputColumns(resultItems)"
                :key="col.key"
                :prop="col.key"
                :label="col.label"
                min-width="150"
                show-overflow-tooltip
              >
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail(col.label, row[col.key])">
                    {{ truncateText(row[col.key], 60) }}
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </div>
      </div>

      <!-- 未选中任务：加载中显示遮罩 spinner（min-height 保证可见），空闲时显示引导 -->
      <div
        v-else
        class="result-empty-placeholder"
        v-loading="resultLoading"
        element-loading-text="正在加载评测结果，请稍候…"
      >
        <el-empty v-if="!resultLoading" description="请选择一个已完成的任务查看结果" />
      </div>
    </el-card>

    <!-- 内容详情弹窗 -->
    <el-dialog
      v-model="detailDialog.visible"
      :title="detailDialog.title"
      width="720px"
      top="8vh"
      destroy-on-close
      class="detail-dialog"
    >
      <div class="detail-content" v-html="renderContent(detailDialog.content)"></div>
      <template #footer>
        <el-button @click="copyContent(detailDialog.content)">复制内容</el-button>
        <el-button type="primary" @click="detailDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 裁判详情弹窗（含 LangFuse Trace 链接） -->
    <el-dialog
      v-model="judgeDetailDialog.visible"
      title="裁判详情"
      width="600px"
      top="8vh"
      destroy-on-close
    >
      <div style="padding: 8px 0;">
        <p style="margin-bottom: 8px;">
          <span style="color: #909399; font-size: 13px;">裁判序号：</span>
          <span style="font-weight: 600;">{{ judgeDetailDialog.judgeIndex }}</span>
        </p>
        <p style="margin-bottom: 8px;">
          <span style="color: #909399; font-size: 13px;">评分：</span>
          <span style="font-weight: 600; color: #409eff;">{{ judgeDetailDialog.score }}</span>
        </p>

        <!-- 失败原因：该裁判执行/解析失败时显式展示（部分裁判失败行的唯一查看入口） -->
        <div v-if="judgeDetailDialog.failReason" style="margin-bottom: 8px;">
          <span style="color: #909399; font-size: 13px;">失败原因：</span>
          <span style="font-weight: 600; color: #f56c6c;">{{ judgeDetailDialog.failReason }}</span>
        </div>

        <!-- 各维度评分 -->
        <div v-if="judgeDetailDialog.dimensionScores && Object.keys(judgeDetailDialog.dimensionScores).length > 0" style="margin-top: 16px;">
          <p style="font-weight: 600; color: #303133; margin-bottom: 8px;">各维度评分</p>
          <div
            v-for="(score, name) in judgeDetailDialog.dimensionScores"
            :key="name"
            style="padding: 4px 0; font-size: 13px;"
          >
            <!-- 维度理由 tooltip：有分维度理由显示理由；旧版数据回退整体反馈 -->
            <el-tooltip placement="top" :content="getDimensionTooltip(name)" popper-class="dimension-feedback-tooltip">
              <div class="dimension-score-row">
                <span style="color: #606266;">{{ name }}：</span>
                <el-tag size="small">{{ score }}{{ dimensionMaxLabel(name, score) }}</el-tag>
              </div>
            </el-tooltip>
          </div>
        </div>

        <!-- 反馈 -->
        <div v-if="judgeDetailDialog.feedback" style="margin-top: 16px;">
          <p style="font-weight: 600; color: #303133; margin-bottom: 8px;">反馈</p>
          <div style="background: #f5f7fa; padding: 12px; border-radius: 6px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;">
            {{ judgeDetailDialog.feedback }}
          </div>
        </div>

        <!-- LangFuse Trace 链接 -->
        <div v-if="judgeDetailDialog.traceUrl" style="margin-top: 20px; padding-top: 16px; border-top: 1px solid #ebeef5; text-align: center;">
          <el-button type="primary" @click="openJudgeTraceUrl(judgeDetailDialog.traceUrl)">
            <el-icon><Link /></el-icon> 查看 LangFuse Trace 详情
          </el-button>
        </div>
      </div>

      <template #footer>
        <el-button @click="judgeDetailDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 失败详情弹窗：执行失败 / 裁判失败行的失败阶段、错误信息与现场数据 -->
    <el-dialog
      v-model="failureDialog.visible"
      title="失败详情"
      width="720px"
      top="8vh"
      destroy-on-close
      class="failure-dialog"
    >
      <div v-if="failureDialog.row" class="failure-body">
        <!-- 失败阶段 -->
        <div class="failure-section">
          <p class="failure-section-title">失败阶段</p>
          <el-tag :type="getFailureStageTagType(failureDialog.row)">{{ getFailureStage(failureDialog.row) }}</el-tag>
        </div>

        <!-- 错误信息：裁判失败优先展示各裁判 fail_reason，无则回退整行 error -->
        <div class="failure-section">
          <p class="failure-section-title">错误信息</p>
          <template v-if="failureJudgeFailReasons.length > 0">
            <div v-for="(fr, i) in failureJudgeFailReasons" :key="i" class="failure-error-box">
              <span class="failure-judge-name">{{ fr.judge }}：</span>
              <div class="failure-error-text" v-html="renderContent(fr.reason || '暂无错误信息')"></div>
            </div>
          </template>
          <div v-else class="failure-error-box">
            <div class="failure-error-text" v-html="renderContent(failureDialog.row.error || '暂无错误信息')"></div>
          </div>
        </div>

        <!-- 数据输入：非结构化字段逐项展示 -->
        <div v-if="failureInputEntries.length > 0" class="failure-section">
          <p class="failure-section-title">数据输入</p>
          <div v-for="entry in failureInputEntries" :key="entry.key" class="failure-input-row">
            <span class="failure-input-label">{{ VARIABLE_LABEL_MAP[entry.key] || entry.key }}：</span>
            <div class="failure-error-text" v-html="renderContent(entry.value)"></div>
          </div>
        </div>

        <!-- 模型输出：长文本点击看全文 -->
        <div v-if="failureDialog.row.model_output" class="failure-section">
          <p class="failure-section-title">模型输出</p>
          <div
            class="failure-error-box failure-error-text failure-model-output"
            @click="openDetail('模型输出', failureDialog.row.model_output)"
          >
            {{ truncateText(failureDialog.row.model_output, 300) }}
          </div>
        </div>

        <!-- 脚本产物：由页面脚本产物专区数据驱动渲染，弹窗内不重复展示 -->
        <el-alert
          v-if="failureDialog.row.pre_script_results"
          type="info"
          :closable="false"
          show-icon
          title="该条数据的脚本产物详见上方脚本产物区"
          class="failure-section"
        />
      </div>
      <template #footer>
        <el-button v-if="failureDialog.row && failureDialog.row.error" @click="copyContent(failureDialog.row.error)">复制错误信息</el-button>
        <el-button type="primary" @click="failureDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 失败原因汇总弹窗：统计卡「失败数量」点击打开，按原因分组（与主表合并后的失败行同源） -->
    <el-dialog
      v-model="failureSummaryDialog.visible"
      title="失败原因汇总"
      width="720px"
      top="8vh"
      destroy-on-close
      class="failure-dialog"
    >
      <div class="failure-body">
        <p class="failure-summary-hint">
          共 {{ failureSummaryDialog.total }} 条失败，按原因归为 {{ failureReasonGroups.length }} 类；
          点击行号可查看该条失败的详细信息（行号为「详细结果」表格中的行序）
        </p>
        <div v-for="(group, gi) in failureReasonGroups" :key="gi" class="failure-section">
          <div class="failure-summary-head">
            <el-tag type="danger" size="small" class="failure-summary-count">{{ group.count }} 条</el-tag>
            <div class="failure-error-text failure-summary-reason" v-html="renderContent(group.reason)"></div>
          </div>
          <div class="failure-summary-rows">
            <el-tag
              v-for="r in group.rows"
              :key="r.pos"
              size="small"
              effect="plain"
              class="failure-tag-clickable"
              @click="openFailureDetail(r.row)"
            >
              第{{ r.pos }}行
            </el-tag>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="failureSummaryDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 指标说明弹窗 -->
    <el-dialog
      v-model="metricDialog.visible"
      :title="metricDialog.metricInfo?.name || '指标说明'"
      width="600px"
      top="8vh"
      destroy-on-close
      class="metric-dialog"
    >
      <div v-if="metricDialog.metricInfo" class="metric-detail">
        <div class="metric-section">
          <h4>定义</h4>
          <p>{{ metricDialog.metricInfo.description }}</p>
        </div>
        <div class="metric-section">
          <h4>计算方式</h4>
          <p>{{ metricDialog.metricInfo.calculation }}</p>
        </div>
        <div class="metric-section">
          <h4>评分范围</h4>
          <p>{{ metricDialog.metricInfo.range }}</p>
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="metricDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 评分理由弹窗 -->
    <el-dialog
      v-model="reasonDialog.visible"
      title="评分理由"
      width="700px"
      top="8vh"
      destroy-on-close
      class="reason-dialog"
    >
      <div v-if="reasonDialog.question" class="reason-content">
        <div class="reason-question">
          <div class="reason-question-header">
            <h4>问题</h4>
            <el-button
              v-if="isQuestionLong"
              link
              size="small"
              type="primary"
              @click="reasonDialog.questionExpanded = !reasonDialog.questionExpanded"
            >
              {{ reasonDialog.questionExpanded ? '收起' : '展开全文' }}
            </el-button>
          </div>
          <p class="reason-question-text" :class="{ collapsed: isQuestionLong && !reasonDialog.questionExpanded }">
            {{ cleanText(reasonDialog.question) }}
          </p>
        </div>
        <div class="reason-list">
          <h4>各指标评分理由</h4>
          <div
            v-for="(reason, metric) in reasonDialog.reasons"
            :key="metric"
            class="reason-item"
          >
            <div class="reason-metric">
              <span class="metric-name">{{ ragMetricNames[metric] || metric }}</span>
              <el-tag :type="getRagMetricGrade(getScoreByMetric(metric)).type" size="small">
                {{ getScoreByMetric(metric).toFixed(3) }}
              </el-tag>
            </div>
            <div class="reason-text">{{ reason || '无' }}</div>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="reasonDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 人工标注模式弹窗（M8 QE8-1）：待标队列（失败/低分优先）+ 快捷键打分 -->
    <AnnotationModeDialog
      v-model="annotationMode.visible"
      :task-id="selectedTaskId"
      :execution-number="selectedExecutionNumber"
    />

    <!-- 报告分享弹窗（M5 QE5-4）：创建链接（明文 token 仅一次）+ 吊销管理 -->
    <el-dialog v-model="shareDialog.visible" title="分享评测报告" width="640px">
      <!-- 新建区 -->
      <el-form label-width="90px">
        <el-form-item label="报告版本">
          <el-radio-group v-model="shareDialog.form.executionMode">
            <el-radio value="latest">跟随最新执行</el-radio>
            <el-radio value="locked" :disabled="!selectedExecutionNumber">锁定当前版本（第 {{ selectedExecutionNumber || '-' }} 次）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="有效期">
          <el-select v-model="shareDialog.form.expiresDays" style="width: 200px">
            <el-option label="1 天" :value="1" />
            <el-option label="7 天" :value="7" />
            <el-option label="30 天" :value="30" />
            <el-option label="永久" :value="null" />
          </el-select>
        </el-form-item>
        <el-form-item label="可见范围">
          <el-radio-group v-model="shareDialog.form.rowScope">
            <el-radio value="all">全部（含明细行）</el-radio>
            <el-radio value="summary">仅汇总</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="shareDialog.creating" @click="createShareLink">创建链接</el-button>
          <span class="share-tip">免登录只读打开；明文链接仅创建时展示一次，请及时复制保存</span>
        </el-form-item>
      </el-form>

      <!-- 一次性明文链接展示 -->
      <el-alert v-if="shareDialog.createdUrl" type="success" :closable="false" class="share-url-alert">
        <div class="share-url-row">
          <span class="share-url-text">{{ shareDialog.createdUrl }}</span>
          <el-button size="small" @click="copyShareUrl">复制链接</el-button>
        </div>
      </el-alert>

      <!-- 链接管理列表 -->
      <el-divider content-position="left">已创建链接（{{ shareDialog.links.length }}）</el-divider>
      <el-table :data="shareDialog.links" size="small">
        <el-table-column label="版本" width="110">
          <template #default="{ row }">{{ row.execution_number == null ? '跟随最新' : `第 ${row.execution_number} 次` }}</template>
        </el-table-column>
        <el-table-column label="范围" width="90">
          <template #default="{ row }">{{ row.row_scope === 'summary' ? '仅汇总' : '全部' }}</template>
        </el-table-column>
        <el-table-column label="有效期" min-width="150">
          <template #default="{ row }">{{ row.expires_at ? formatTime(row.expires_at) : '永久' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.active ? 'success' : 'info'">{{ row.active ? '有效' : '已失效' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.active"
              link type="danger" size="small" :loading="shareDialog.revokingId === row.id"
              @click="revokeShareLink(row)"
            >吊销</el-button>
            <span v-else class="share-tip">—</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!shareDialog.links.length" description="暂无分享链接" :image-size="60" />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, inject, onMounted, onUnmounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { InfoFilled, ArrowDown, Timer, CircleCheck, CircleCheckFilled, CircleClose, CircleCloseFilled, Warning, WarningFilled, Link, User } from '@element-plus/icons-vue'
import { taskApi, resultApi, shareApi } from '@/api'
import AnnotationModeDialog from '@/components/AnnotationModeDialog.vue'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { formatTime } from '@/utils/time'
import { canUseIdxAsSeqNo, detailSeqNo } from '@/utils/seqno'
import { canWrite } from '@/utils/permissions'
import * as echarts from 'echarts'

const tasks = ref([])
const allTasks = ref([])
const selectedTaskId = ref(null)
const result = ref(null)
const resultItems = ref([])
// 序号口径：合并后的明细行 idx 全行存在且唯一才按 idx+1 展示（对齐数据集行位
// 与分数趋势图 x 轴）；历史脏数据（执行链路 payload 缺 idx 回归导致全行 idx=0）
// 回退位置序号，避免序号列/坐标轴全为 1
const seqNoFromIdx = computed(() => canUseIdxAsSeqNo(resultItems.value))
// 任务/版本切换加载态：选任务到结果渲染之间给出可见反馈（此前无任何提示）
const resultLoading = ref(false)

// 判断是否有裁判评估数据（一致性评测专用）
const hasJudgeAssessment = computed(() => {
  return resultItems.value.some(item =>
    item.judge_assessment && Object.keys(item.judge_assessment).length > 0
  )
})

// 判断是否有自定义维度（一致性评测专用）
const hasCustomDimensions = computed(() => {
  if (!result.value?.result?.custom_dimension_summary) return false
  const summary = result.value.result.custom_dimension_summary
  return Object.keys(summary).length > 0
})

// 版本管理
const executionVersions = ref([])
const selectedExecutionNumber = ref(null)

// 多次执行维度分数趋势（多次执行聚合展示数据源）：后端按 execution_number 倒序
// 返回（最近一次在前），前端使用前显式按 execution_number 排序，不依赖传输顺序；
// 旧后端无 dimension-trend 端点时保持空数组
const executionTrend = ref([])

const detailDialog = reactive({
  visible: false,
  title: '',
  content: ''
})

// 裁判详情弹窗（含 LangFuse Trace 链接）
const judgeDetailDialog = reactive({
  visible: false,
  judgeIndex: '',
  score: null,
  feedback: '',
  dimensionScores: null,
  // 分维度理由：新版 js.dimension_feedbacks 直取；旧版从 judge_details.dimension_details 派生；都没有为 null
  dimensionFeedbacks: null,
  // 该裁判失败原因（judge_failed/fail_reason 存在时展示，部分裁判失败行的查看入口）
  failReason: null,
  traceUrl: null,
})

// 失败详情弹窗（主表「执行失败 / 裁判失败」状态 tag 点击打开）
const failureDialog = reactive({
  visible: false,
  row: null
})

// 失败原因汇总弹窗（统计卡「失败数量」点击打开）
const failureSummaryDialog = reactive({
  visible: false,
  total: 0
})

// 指标说明弹窗
const metricDialog = reactive({
  visible: false,
  metricName: '',
  metricInfo: null
})

// 评分理由弹窗
const reasonDialog = reactive({
  visible: false,
  question: '',
  reasons: {},
  scores: {},
  questionExpanded: false
})

// 结构化字段（不作为输入变量列展示）
const STRUCTURED_KEYS = new Set([
  'idx', 'success', 'error', 'model_output', 'average_score', 'judge_count',
  'judge_scores', 'judge_details', 'feedback', 'dimension_scores',
  'input_data', 'model_raw_response', 'metrics', 'repeat_results',
  'judge_assessment', 'judge_assessments', 'evaluation_content',
  'expected_label', 'predicted_label', 'is_correct', 'duration',
  'question_index', 'scores', 'reasons', 'contexts', 'ground_truth',
  'model_answer', 'question', 'ref_answer', 'user_input',
  'custom_dimensions', 'contexts_count',
  'session_summary', 'session_source',
  // 行级口径字段：内部状态，不作为输入变量列/失败弹窗输入项展示
  'row_status', 'failed_judge_count',
  // 前处理脚本产物：数组对象，由「脚本产物」专区数据驱动渲染，不作为输入变量列
  'pre_script_results'
])

// 变量名中文映射（仅用于展示，不用于逻辑判断）
const VARIABLE_LABEL_MAP = {
  user_input: '用户输入',
  ref_answer: '参考答案',
  question: '问题',
  answer: '回答',
  ground_truth: '标准答案',
}

// 从结果 items 中提取输入变量列（排除结构化字段和全空字段）
const getInputColumns = (items) => {
  if (!items || items.length === 0) return []
  const keys = new Set()
  items.forEach(item => {
    if (item && typeof item === 'object') {
      Object.keys(item).forEach(k => {
        if (!STRUCTURED_KEYS.has(k) && !k.startsWith('_') && item[k] !== null && item[k] !== undefined) {
          keys.add(k)
        }
      })
    }
  })
  return Array.from(keys).map(k => ({
    key: k,
    label: VARIABLE_LABEL_MAP[k] || k
  }))
}

// 去除 HTML 标签和多余 Markdown 语法，提取纯文本
const cleanText = (text) => {
  if (!text) return ''
  return text
    .replace(/<[^>]+>/g, ' ')           // 去除 HTML 标签
    .replace(/!\[.*?\]\(.*?\)/g, '[图片]') // 替换图片链接
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // 保留链接文字
    .replace(/#{1,6}\s/g, '')           // 去除标题符号
    .replace(/\*{1,3}([^*]+)\*{1,3}/g, '$1') // 去除加粗/斜体
    .replace(/`{1,3}[^`]*`{1,3}/g, '')  // 去除代码块
    .replace(/\s{2,}/g, ' ')            // 合并多余空白
    .replace(/\n{3,}/g, '\n\n')         // 合并多余换行
    .trim()
}

const QUESTION_COLLAPSE_LIMIT = 200
const isQuestionLong = computed(() => cleanText(reasonDialog.question).length > QUESTION_COLLAPSE_LIMIT)

// 耗时评测分布数据
const distributionData = computed(() => {
  if (!result.value?.result || result.value.evaluation_type !== 'latency') return []
  const r = result.value.result
  return [
    { metric: '最短耗时', value: r.min_duration || 0, unit: '秒' },
    { metric: '最长耗时', value: r.max_duration || 0, unit: '秒' },
    { metric: '平均耗时', value: r.avg_duration || 0, unit: '秒' },
    { metric: 'P50', value: r.p50_duration || 0, unit: '秒' },
    { metric: 'P90', value: r.p90_duration || 0, unit: '秒' },
    { metric: 'P99', value: r.p99_duration || 0, unit: '秒' },
    { metric: '通过数', value: r.passed_count || 0, unit: '' },
    { metric: '失败数', value: r.failed_count || 0, unit: '' },
    { metric: '总请求数', value: r.total_count || 0, unit: '' },
  ]
})

// 打开指标说明弹窗（从表格）
const openMetricDetail = (metricName, row) => {
  const info = ragMetricDescriptions[metricName]
  if (info) {
    metricDialog.metricName = metricName
    metricDialog.metricInfo = info
    metricDialog.visible = true
  }
}

// 打开指标说明弹窗（从卡片）
const openMetricCardDetail = (metricName) => {
  const info = ragMetricDescriptions[metricName]
  if (info) {
    metricDialog.metricName = metricName
    metricDialog.metricInfo = info
    metricDialog.visible = true
  }
}

// 打开评分理由弹窗
const openReasonDialog = (row) => {
  reasonDialog.question = row.user_input || row.question || Object.values(row).find(v => typeof v === 'string' && v.length > 0) || ''
  reasonDialog.reasons = row.reasons || {}
  reasonDialog.scores = row.scores || {}
  reasonDialog.questionExpanded = false
  reasonDialog.visible = true
}

// 获取指定指标的评分
const getScoreByMetric = (metric) => {
  return reasonDialog.scores[metric] || 0
}

// 对象/数组转可读 JSON 文本，避免 String() 产生 "[object Object]"
const toDisplayText = (value) => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

const truncateText = (text, maxLen = 80) => {
  const str = toDisplayText(text)
  if (!str) return '-'
  return str.length > maxLen ? str.slice(0, maxLen) + '...' : str
}

const openDetail = (title, content) => {
  const str = toDisplayText(content)
  if (!str) return
  detailDialog.title = title
  detailDialog.content = str
  detailDialog.visible = true
}

const renderContent = (text) => {
  if (!text) return ''
  // 转义 HTML 特殊字符防止 XSS
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
  // 将换行转为 <br>，连续空格保留
  return escaped.replace(/\n/g, '<br>')
}

// 导出结果处理
const exporting = ref(false)
const handleExport = async (format) => {
  if (!selectedTaskId.value) {
    ElMessage.warning('请先选择一个任务')
    return
  }

  if (!result.value || result.value.status !== 'completed') {
    ElMessage.warning('只能导出已完成的任务结果')
    return
  }

  if (exporting.value) return
  exporting.value = true
  try {
    ElMessage.info('正在导出，请稍候...')

    if (format === 'json') {
      // JSON 导出直接使用 API
      const response = await resultApi.get(selectedTaskId.value)
      const dataStr = JSON.stringify(response.result, null, 2)
      const blob = new Blob([dataStr], { type: 'application/json' })
      downloadBlob(blob, `evaluation_result_${selectedTaskId.value}_${Date.now()}.json`)
      ElMessage.success('JSON 导出成功')
    } else if (format === 'excel' || format === 'csv') {
      // Excel/CSV 使用批量导出接口；必须走带鉴权拦截器的 request 实例
      // （裸 axios 不带 Authorization 头，后端 401），blob 响应由拦截器透传
      const response = await resultApi.exportBatch({
        task_ids: [selectedTaskId.value],
        format: format
      })

      const blob = new Blob([response.data], {
        type: response.headers['content-type'] || 'application/octet-stream'
      })
      // format 为 'excel' 时实际导出的是 .xlsx 文件
      const ext = format === 'excel' ? 'xlsx' : format
      const filename = `evaluation_result_${selectedTaskId.value}_${Date.now()}.${ext}`
      downloadBlob(blob, filename)
      ElMessage.success(`${format.toUpperCase()} 导出成功`)
    }
  } catch (e) {
    console.error('导出失败:', e)
    ElMessage.error('导出失败: ' + (e.message || '未知错误'))
  } finally {
    exporting.value = false
  }
}

// ==================== 报告分享（M5 QE5-4，免登录只读） ====================
const currentUser = inject('currentUser', null)
const readonlyRole = computed(() => !canWrite(currentUser.value))

// ==================== 人工标注模式（M8 QE8-1，标注闭环） ====================
// 队列排序（失败行/低分行优先）与快捷键打分在 AnnotationModeDialog 内实现；
// 这里只管入口开关——锚定当前选中的执行版本（未选=最新一次执行）
const annotationMode = reactive({ visible: false })

const openAnnotationMode = () => {
  if (!selectedTaskId.value) return
  annotationMode.visible = true
}

const shareDialog = reactive({
  visible: false,
  creating: false,
  revokingId: null,
  createdUrl: '',   // 明文链接仅创建时展示一次
  links: [],
  form: {
    executionMode: 'latest',   // latest=跟随最新执行 / locked=锁定当前选中版本
    expiresDays: 7,
    rowScope: 'all',
  },
})

const openShareDialog = async () => {
  shareDialog.visible = true
  shareDialog.createdUrl = ''
  shareDialog.form = { executionMode: 'latest', expiresDays: 7, rowScope: 'all' }
  await loadShareLinks()
}

const loadShareLinks = async () => {
  try {
    const res = await shareApi.listLinks(selectedTaskId.value)
    shareDialog.links = Array.isArray(res) ? res : []
  } catch (e) {
    ElMessage.error('获取分享链接列表失败')
  }
}

const createShareLink = async () => {
  if (shareDialog.creating) return
  shareDialog.creating = true
  try {
    const res = await shareApi.createLink(selectedTaskId.value, {
      execution_number: shareDialog.form.executionMode === 'locked' ? selectedExecutionNumber.value : null,
      expires_days: shareDialog.form.expiresDays,
      row_scope: shareDialog.form.rowScope,
    })
    shareDialog.createdUrl = `${window.location.origin}/share/${res.token}`
    ElMessage.success('分享链接已创建（明文仅展示一次，请复制保存）')
    await loadShareLinks()
  } catch (e) {
    ElMessage.error(e?.message || '创建分享链接失败')
  } finally {
    shareDialog.creating = false
  }
}

const copyShareUrl = async () => {
  try {
    await navigator.clipboard.writeText(shareDialog.createdUrl)
    ElMessage.success('链接已复制')
  } catch {
    // 剪贴板 API 不可用（非 https 等）：退回老 API
    const input = document.createElement('textarea')
    input.value = shareDialog.createdUrl
    document.body.appendChild(input)
    input.select()
    document.execCommand('copy')
    document.body.removeChild(input)
    ElMessage.success('链接已复制')
  }
}

const revokeShareLink = async (row) => {
  try {
    await ElMessageBox.confirm('吊销后该链接立即失效（免登录页将显示不存在），确定吊销？', '吊销分享链接', { type: 'warning' })
  } catch {
    return
  }
  shareDialog.revokingId = row.id
  try {
    await shareApi.revokeLink(selectedTaskId.value, row.id)
    ElMessage.success('已吊销')
    await loadShareLinks()
  } catch (e) {
    ElMessage.error('吊销失败')
  } finally {
    shareDialog.revokingId = null
  }
}

// 下载 blob 文件
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

const copyContent = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败')
  }
}

const completedTasks = ref([])

const getTypeName = (type) => {
  const map = { judge: 'AI裁判员', consistency: '一致性', success_rate: '成功率', rag: 'RAG检索', latency: '响应耗时', binary_classification: '二分分类', agent: 'Agent评测', safety: '安全评测', dialogue: '对话评测' }
  return map[type] || type
}

// RAG 指标中文名映射
const ragMetricNames = {
  context_precision: '上下文精度',
  context_recall: '上下文召回',
  hit_rate: '命中率',
  mrr: '平均倒数排名',
  noise_sensitivity: '抗噪声能力',
  negative_rejection: '拒绝能力',
}

// RAG 指标详细说明
const ragMetricDescriptions = {
  context_precision: {
    name: '上下文精度',
    description: '衡量检索到的上下文与问题的相关程度。高精度表示检索结果中包含的大部分内容都是回答问题所需的。',
    calculation: '基于 RAGAS 库计算，相关上下文数量 / 总上下文数量',
    range: '0-1，越高越好',
  },
  context_recall: {
    name: '上下文召回',
    description: '衡量检索结果是否覆盖了回答问题所需的全部关键信息。高召回表示检索到了回答问题所需的核心内容。',
    calculation: '基于 RAGAS 库计算，参考答案中的关键信息被上下文覆盖的比例',
    range: '0-1，越高越好',
  },
  hit_rate: {
    name: '命中率',
    description: '衡量 Top-K 检索结果中是否包含与标准答案相关的上下文。',
    calculation: '检查前 K 个检索结果中是否有与 ground_truth 相关的上下文',
    range: '0-1，越高越好',
  },
  mrr: {
    name: '平均倒数排名',
    description: '衡量相关上下文出现在检索结果中的位置。第一个相关结果越靠前，MRR 越高。',
    calculation: '1 / 第一个相关上下文的位置排名',
    range: '0-1，越高越好',
  },
  noise_sensitivity: {
    name: '抗噪声能力',
    description: '衡量检索结果抵抗无关信息的能力。高抗噪声能力表示检索结果纯净，相关内容占比高。',
    calculation: 'LLM 评测：分析每个上下文与问题的相关性，计算信噪比',
    range: '0-1，越高越好',
  },
  negative_rejection: {
    name: '拒绝能力',
    description: '衡量当检索结果不足以回答问题时，系统识别并拒绝回答的能力。',
    calculation: 'LLM 评测：检查检索结果是否覆盖了回答问题所需的全部关键信息',
    range: '0-1，越高越好',
  },
}

// RAG 指标评级
const getRagMetricGrade = (value) => {
  if (value == null) return { label: '-', type: 'info' }
  if (value >= 0.8) return { label: '优秀', type: 'success' }
  if (value >= 0.6) return { label: '良好', type: '' }
  if (value >= 0.4) return { label: '一般', type: 'warning' }
  return { label: '较差', type: 'danger' }
}

const getScoreType = (score) => {
  if (score >= 8) return 'success'
  if (score >= 5) return 'warning'
  return 'danger'
}

// 一致性评分颜色类型
const getConsistencyType = (score) => {
  if (score == null) return 'info'
  if (score >= 0.8) return 'success'
  if (score >= 0.6) return 'warning'
  return 'danger'
}

// 格式化变量值用于展示
const formatVariableValue = (value, maxLength = 100) => {
  if (value === null || value === undefined) return '(空)'
  if (typeof value === 'object') {
    try {
      const jsonStr = JSON.stringify(value, null, 2)
      return jsonStr.length > maxLength ? jsonStr.substring(0, maxLength) + '...' : jsonStr
    } catch {
      return String(value)
    }
  }
  const str = String(value)
  return str.length > maxLength ? str.substring(0, maxLength) + '...' : str
}

// 打开重复结果详情
const openRepeatResults = (row) => {
  if (!row.repeat_results || row.repeat_results.length === 0) return
  const content = row.repeat_results.map((r, i) => {
    let text = `=== 第 ${r.repeat_id || (i + 1)} 次 ===\n`
    text += `结果: ${r.result || '(空)'}\n`
    text += `状态: ${r.success ? '成功' : '失败'}\n`

    // 展示请求链详情
    if (r.chain_results && r.chain_results.length > 0) {
      text += `\n--- 请求链详情 ---\n`
      r.chain_results.forEach((cr, idx) => {
        text += `\n[请求 ${cr.order || idx + 1}] ${cr.name || ''}\n`
        if (cr.content) {
          text += `响应内容: ${cr.content}\n`
        }
        if (cr.variables && Object.keys(cr.variables).length > 0) {
          text += `变量:\n`
          for (const [key, value] of Object.entries(cr.variables)) {
            text += `  ${key}: ${formatVariableValue(value, 100)}\n`
          }
        }
      })
    }

    return text
  }).join('\n\n')
  openDetail('重复结果对比', content)
}

// 打开单次调用的请求详情
const openRequestDetail = (row, repeatIdx) => {
  if (!row.repeat_results || !row.repeat_results[repeatIdx]) return
  const repeatResult = row.repeat_results[repeatIdx]

  let content = `=== 第 ${repeatResult.repeat_id || repeatIdx + 1} 次调用详情 ===\n\n`
  content += `状态: ${repeatResult.success ? '成功' : '失败'}\n`
  content += `结果: ${repeatResult.result || '(空)'}\n\n`

  // 展示请求链详情
  if (repeatResult.chain_results && repeatResult.chain_results.length > 0) {
    content += `--- 请求链详情 ---\n\n`
    repeatResult.chain_results.forEach((cr, idx) => {
      content += `[请求 ${cr.order || idx + 1}] ${cr.name || ''}\n`
      content += `${'='.repeat(40)}\n`

      if (cr.content) {
        content += `响应内容:\n${cr.content}\n\n`
      }

      if (cr.variables && Object.keys(cr.variables).length > 0) {
        content += `变量信息:\n`
        for (const [key, value] of Object.entries(cr.variables)) {
          content += `  ${key}: ${formatVariableValue(value, 200)}\n`
        }
        content += '\n'
      }
    })
  } else {
    content += '(无请求链信息)\n'
  }

  openDetail(`第 ${repeatResult.repeat_id || repeatIdx + 1} 次调用详情`, content)
}

// 打开 LangFuse Trace 链接
const openJudgeTraceUrl = (url) => {
  if (url) {
    window.open(url, '_blank')
  }
}

// 分维度理由取值链：
// 1) 新版 js.dimension_feedbacks（{维度名: 理由}）为非空对象时直取；
// 2) 旧版 js.judge_details?.dimension_details 存在时派生 {维度名: 条目.reasoning}（仅取 reasoning 为非空字符串的）；
// 3) 都没有返回 null（旧版数据无分维度理由，tooltip 回退裁判整体反馈）
const getDimensionFeedbacks = (js) => {
  if (!js) return null
  if (js.dimension_feedbacks && typeof js.dimension_feedbacks === 'object' && Object.keys(js.dimension_feedbacks).length > 0) {
    return js.dimension_feedbacks
  }
  const dimensionDetails = js.judge_details?.dimension_details
  if (dimensionDetails && typeof dimensionDetails === 'object') {
    const derived = {}
    for (const [name, detail] of Object.entries(dimensionDetails)) {
      if (detail && typeof detail.reasoning === 'string' && detail.reasoning.trim()) {
        derived[name] = detail.reasoning
      }
    }
    if (Object.keys(derived).length > 0) return derived
  }
  return null
}

// 打开裁判详情弹窗（含 LangFuse Trace 链接）
const openJudgeDetail = (row, judgeIdx) => {
  const js = row.judge_scores[judgeIdx]
  if (!js) return
  judgeDetailDialog.judgeIndex = js.judge_index !== undefined ? js.judge_index + 1 : judgeIdx + 1
  judgeDetailDialog.score = js.score !== undefined ? js.score : null
  judgeDetailDialog.feedback = js.feedback || ''
  judgeDetailDialog.dimensionScores = js.dimension_scores || null
  judgeDetailDialog.dimensionFeedbacks = getDimensionFeedbacks(js)
  judgeDetailDialog.failReason = js.judge_failed || js.fail_reason ? (toDisplayText(js.fail_reason) || '该裁判执行失败，未返回原因') : null
  judgeDetailDialog.traceUrl = js.langfuse_trace_url || null
  judgeDetailDialog.visible = true
}

// 各维度评分行 tooltip 文案：该维度有理由时显示理由；
// 没有时回退裁判整体反馈（旧版数据），整体反馈也为空则显示暂无理由数据
const getDimensionTooltip = (name) => {
  const fb = judgeDetailDialog.dimensionFeedbacks?.[name]
  if (typeof fb === 'string' && fb.trim()) return fb
  const overall = judgeDetailDialog.feedback
  if (overall && overall.trim()) return '旧版数据无分维度理由。该裁判整体反馈：' + overall
  return '旧版数据无分维度理由。该裁判整体反馈：暂无理由数据'
}

// 各维度评分展示「得分/最高分」：最高分取当前任务维度配置的 max_score
// （缺省/非法回退任务 max_score）；任务未选中或维度不在配置中时不显示分母。
// 非数值得分（CC 协议的 "F" 哨兵=本行不涉及）不拼分母
const dimensionMaxLabel = (name, score) => {
  if (typeof score !== 'number' || !Number.isFinite(score)) return ''
  const task = allTasks.value.find(t => t.id === selectedTaskId.value)
  const metrics = task?.evaluation_metrics
  if (!Array.isArray(metrics)) return ''
  const metric = metrics.find(m => m && m.name === name)
  if (!metric) return ''
  const taskMax = task.max_score || 10
  const raw = Number(metric.max_score)
  const dimMax = Number.isFinite(raw) && raw > 0 ? raw : taskMax
  return ` / ${dimMax}`
}

// ---------------- 失败详情弹窗（执行失败 / 裁判失败行） ----------------

// 打开失败详情弹窗
const openFailureDetail = (row) => {
  if (!row) return
  failureDialog.row = row
  failureDialog.visible = true
}

// 打开失败原因汇总弹窗：无失败行时不响应（卡片也无点击态）
const openFailureSummary = () => {
  const failedRows = (resultItems.value || []).filter(r => r && r.success === false)
  if (failedRows.length === 0) return
  failureSummaryDialog.total = failedRows.length
  failureSummaryDialog.visible = true
}

// 失败原因分组：与主表同源的失败行（success === false）按 error 文案归组，
// 空文案归入「暂无错误信息」；pos 为合并后表格的行序（1 起），点击行号打开该条失败详情
const failureReasonGroups = computed(() => {
  const failedRows = (resultItems.value || []).filter(r => r && r.success === false)
  const groups = new Map()
  failedRows.forEach((row, i) => {
    const reason = (typeof row.error === 'string' && row.error.trim()) ? row.error.trim() : '暂无错误信息'
    if (!groups.has(reason)) {
      groups.set(reason, { reason, count: 0, rows: [] })
    }
    const group = groups.get(reason)
    group.count += 1
    group.rows.push({ pos: i + 1, row })
  })
  return Array.from(groups.values()).sort((a, b) => b.count - a.count)
})

// 失败阶段文案：按 row_status 与 error 内容定位失败发生在哪个阶段
const getFailureStage = (row) => {
  if (!row) return '执行异常'
  if (row.row_status === 'pre_script_failed') return '前处理脚本失败'
  // on_fail=abort 与脚本自定义报错的行不写 row_status（聚合派生为 failed），
  // 靠 error 文本中的「前处理脚本」字样归因到脚本阶段
  if (typeof row.error === 'string' && row.error.includes('前处理脚本')) return '前处理脚本失败'
  if (typeof row.error === 'string' && row.error.startsWith('模型调用失败')) return '模型调用失败'
  if (typeof row.error === 'string' && row.error.includes('任务已停止')) return '手动停止'
  if (row.row_status === 'judge_failed') return '裁判评测失败'
  return '执行异常'
}

// 失败阶段 tag 颜色：停止为中性，裁判失败为警告，其余失败为危险
const getFailureStageTagType = (row) => {
  const stage = getFailureStage(row)
  if (stage === '手动停止') return 'info'
  if (stage === '裁判评测失败') return 'warning'
  return 'danger'
}

// 裁判失败行：各裁判的 fail_reason 列表（仅取 judge_failed 为真的裁判）
const failureJudgeFailReasons = computed(() => {
  const row = failureDialog.row
  if (!row || !Array.isArray(row.judge_scores)) return []
  const list = []
  row.judge_scores.forEach((js, i) => {
    if (js && js.judge_failed) {
      list.push({
        judge: js.judge_index !== undefined ? `裁判${js.judge_index + 1}` : `裁判${i + 1}`,
        reason: toDisplayText(js.fail_reason)
      })
    }
  })
  return list
})

// 失败行数据输入：排除结构化字段与下划线开头的内部字段后的键值对；
// 用户输入/参考答案等标准字段虽在 STRUCTURED_KEYS（主表输入列不用它们），
// 但带中文标签且是失败现场的关键上下文，弹窗需要展示
const failureInputEntries = computed(() => {
  const row = failureDialog.row
  if (!row || typeof row !== 'object') return []
  return Object.entries(row)
    .filter(([k, v]) => v !== null && v !== undefined && !k.startsWith('_')
      && (!STRUCTURED_KEYS.has(k) || VARIABLE_LABEL_MAP[k]))
    .map(([k, v]) => ({ key: k, value: toDisplayText(v) }))
})

// 打开裁判评估详情
const openJudgeAssessment = (row) => {
  if (!row.judge_assessment || Object.keys(row.judge_assessment).length === 0) return
  const assessment = row.judge_assessment
  let content = ''

  // 新格式：综合评分 / 综合评分原因 / 各维度评分
  if (assessment['综合评分'] !== undefined) {
    content += `综合评分: ${assessment['综合评分']}\n\n`
  }
  if (assessment['综合评分原因']) {
    content += `评分原因: ${assessment['综合评分原因']}\n\n`
  }
  // 各维度评分
  const dimensionScores = assessment['各维度评分']
  if (dimensionScores && typeof dimensionScores === 'object' && Object.keys(dimensionScores).length > 0) {
    content += '各维度评分:\n'
    for (const [dimName, dimData] of Object.entries(dimensionScores)) {
      if (typeof dimData === 'object' && dimData !== null) {
        content += `  ${dimName}: ${dimData.score ?? '-'}`
        if (dimData.reason) {
          content += ` (${dimData.reason})`
        }
        content += '\n'
      } else {
        content += `  ${dimName}: ${dimData}\n`
      }
    }
  }

  // 旧格式兼容：consistency_score / content_consistency / format_consistency / structure_consistency
  if (!content && assessment.consistency_score !== undefined) {
    content += `总体一致性评分: ${assessment.consistency_score}\n\n`
  }
  if (!content && assessment.content_consistency) {
    content += `内容一致性: ${assessment.content_consistency.score || '-'}`
    if (assessment.content_consistency.analysis) {
      content += `\n分析: ${assessment.content_consistency.analysis}`
    }
    content += '\n\n'
  }
  if (!content && assessment.format_consistency) {
    content += `格式一致性: ${assessment.format_consistency.score || '-'}`
    if (assessment.format_consistency.analysis) {
      content += `\n分析: ${assessment.format_consistency.analysis}`
    }
    content += '\n\n'
  }
  if (!content && assessment.structure_consistency) {
    content += `结构一致性: ${assessment.structure_consistency.score || '-'}`
    if (assessment.structure_consistency.analysis) {
      content += `\n分析: ${assessment.structure_consistency.analysis}`
    }
  }

  // 兜底：如果以上都不匹配，直接 JSON 展示
  if (!content) {
    content = JSON.stringify(assessment, null, 2)
  }

  openDetail('裁判评估详情', content.trim())
}

const getStatusTagType = (status) => {
  const map = { pending: 'info', running: 'warning', completed: 'success', failed: 'danger', stopped: 'info' }
  return map[status] || 'info'
}

// M1 门禁 verdict 展示口径（横幅/版本下拉共用；语义与 gate_service 对齐）
const gateVerdictLabel = (verdict) => ({ passed: '通过', failed: '未通过', error: '错误' }[verdict] || verdict)
const gateVerdictTagType = (verdict) => ({ passed: 'success', failed: 'danger', error: 'warning' }[verdict] || 'info')
// 指标可读名：total_score/pass_rate 为固定口径，dimension:<名> 拆出维度名
const gateMetricLabel = (metric) => {
  if (metric === 'total_score') return '任务均分'
  if (metric === 'pass_rate') return '行通过率（0-1）'
  if (typeof metric === 'string' && metric.startsWith('dimension:')) return `维度均分：${metric.slice('dimension:'.length)}`
  return metric
}

// 安全地计算百分比，防止 null * 100 崩溃
const safePercent = (val) => {
  if (val == null || isNaN(Number(val))) return '-'
  return (Number(val) * 100).toFixed(1)
}

// M2 QE2-4 裁判治理面板展示口径
const judgeEngineLabel = (engine) => ({
  claude_agent: 'Claude Code Agent',
  deepseek_agent: 'DeepSeek Agent',
  graph: '传统裁判图',
}[engine] || engine || '-')
// 裁判模型摘要（掩码凭据快照：provider/model_type + 掩码 key 指纹，无明文）
const judgeModelSummary = (m) => {
  if (!m) return '-'
  if (m.config_missing) return `配置缺失（${m.source || '-'}）`
  const parts = [m.provider, m.model_type].filter(Boolean)
  if (m.api_key_masked) parts.push(m.api_key_masked)
  return parts.join(' / ') || m.source || '-'
}
const canaryKindLabel = (kind) => ({ empty: '空样本', garbage: '乱码样本' }[kind] || kind || '-')

// 安全获取 result.items，防止字段不存在时崩溃；
// failed_items（模型调用失败/前处理脚本失败/裁判失败等未进入评分的数据行）合并进明细，
// 合并后按 idx 升序稳定排序（同 idx / 缺失 idx 保持合并前相对顺序），保证主表序号与图表 x 轴连续
const safeGetItems = (resultData) => {
  if (!resultData) return []
  let items = []
  if (resultData.items && Array.isArray(resultData.items)) items = resultData.items
  else if (resultData.results && Array.isArray(resultData.results)) items = resultData.results
  else if (resultData.detail_results && Array.isArray(resultData.detail_results)) items = resultData.detail_results
  if (Array.isArray(resultData.failed_items) && resultData.failed_items.length > 0) {
    items = items
      .concat(resultData.failed_items)
      .map((item, order) => ({ item, order }))
      .sort((a, b) => {
        const ai = a.item?.idx
        const bi = b.item?.idx
        if (ai === bi) return a.order - b.order
        return (ai ?? Number.MAX_SAFE_INTEGER) - (bi ?? Number.MAX_SAFE_INTEGER)
      })
      .map(e => e.item)
  }
  return items
}

// 判断结果是否可渲染图表（异常兼容：failed 不渲染，stopped 有部分结果可渲染）
const isResultRenderable = (r) => {
  if (!r || !r.result) return false
  if (r.status === 'failed') return false
  return true
}

// 安全评测辅助函数（M3 QE3-1 双通道红队口径）
// 聚合结构来自 evaluators/safety.aggregate_safety_results：
// {executed, violations, defense_rate, by_category, by_channel, mutation, items}
const hasSafetyAlerts = computed(() => {
  if (!result.value?.result || result.value.evaluation_type !== 'safety') return false
  return (result.value.result.violations || 0) > 0
})

const safetyAlertTitle = computed(() => {
  if (!result.value?.result) return ''
  const violations = result.value.result.violations || 0
  return `安全评测告警：${violations} 条攻击成功（模型被攻破）`
})

const safetyAlertType = computed(() => {
  if (!result.value?.result) return 'warning'
  const violations = result.value.result.violations || 0
  if (violations > 10) return 'error'
  if (violations > 0) return 'warning'
  return 'success'
})

const safetyAlertCount = computed(() => {
  return result.value?.result?.violations || 0
})

// 攻击面类目统计行（by_category 对象 → 表格行）
const safetyCategoryRows = computed(() => {
  const byCategory = result.value?.result?.by_category || {}
  return Object.entries(byCategory).map(([key, row]) => ({
    key,
    ...row
  }))
})

// deepeval 指标通道（M3 QE3-2）：启用且有逐指标统计时显示
const hasSafetyDeepeval = computed(() => {
  const metrics = result.value?.result?.by_channel?.deepeval?.metrics
  return !!(metrics && Object.keys(metrics).length)
})

// 明细行：违规行筛选 + 攻击面中文名
const safetyOnlyViolations = ref(false)
const safetyItems = computed(() => {
  const items = result.value?.result?.items || []
  const nameMap = {
    prompt_injection: '提示注入',
    jailbreak: '越狱',
    sensitive_content: '敏感内容',
    factual_assertion: '事实性断言'
  }
  const rows = items.map(row => ({
    ...row,
    attack_category_name: nameMap[row.attack_category] || row.attack_category || '未分类'
  }))
  return safetyOnlyViolations.value
    ? rows.filter(row => row.safety?.violation)
    : rows
})

// 确定性通道命中证据 tooltip（PII/敏感词原文摘录 + 掩码展示）
const safetyFindingsTooltip = (row) => {
  const findings = row.safety?.channels?.deterministic?.findings || []
  if (!findings.length) return ''
  return findings.map(f => `${f.label}: ${f.masked || f.evidence}`).join('\n')
}

const safetyJudgeTagType = (row) => {
  const judge = row.safety?.channels?.judge_rubric
  if (!judge || judge.unavailable) return 'info'
  return judge.violation ? 'danger' : 'success'
}

const safetyJudgeLabel = (row) => {
  const judge = row.safety?.channels?.judge_rubric
  if (!judge) return '-'
  if (judge.unavailable) return '不可用'
  return judge.violation ? '违规' : '未违规'
}

const safetySeverityTagType = (severity) => {
  if (severity === 'high') return 'danger'
  if (severity === 'medium') return 'warning'
  return 'info'
}

const getSafetyItemTagType = (flagged) => {
  return flagged ? 'danger' : 'success'
}

// ---------------- 安全专视图扩展（M3 QE3-3：类目热力 + 证据引用） ----------------

// 类目热力：行 = 攻击面类目（by_category 顺序），列 = 检出通道
// （确定性命中 / 裁判违规 / 各 deepeval 指标），值 = 该类目在该通道的违规/命中条数。
// by_channel 只有全局计数，交叉计数从 items 逐行统计（与聚合口径一致：
// 确定性看 triggered、裁判看 violation、deepeval 逐指标看 success===false 且非不可用）
const safetyHeatmapRows = computed(() => {
  return safetyCategoryRows.value.map(row => row.name || row.key)
})

const safetyHeatmapCols = computed(() => {
  const cols = ['确定性命中', '裁判违规']
  const metrics = result.value?.result?.by_channel?.deepeval?.metrics || {}
  cols.push(...Object.keys(metrics))
  return cols
})

const safetyHeatmapData = computed(() => {
  const rowKeys = safetyCategoryRows.value.map(row => row.key)
  const metricCols = Object.keys(result.value?.result?.by_channel?.deepeval?.metrics || {})
  const counts = rowKeys.map(() => safetyHeatmapCols.value.map(() => 0))
  const items = result.value?.result?.items || []
  for (const row of items) {
    if (!row?.safety) continue
    const y = rowKeys.indexOf(row.attack_category || 'uncategorized')
    if (y < 0) continue
    const channels = row.safety.channels || {}
    if (channels.deterministic?.triggered) counts[y][0] += 1
    if (channels.judge_rubric?.violation) counts[y][1] += 1
    const deepeval = channels.deepeval || {}
    for (const [mi, metricKey] of metricCols.entries()) {
      const m = deepeval[metricKey]
      if (m && m.success === false && !m.metric_unavailable) counts[y][2 + mi] += 1
    }
  }
  return counts
})

// 类目热力渲染（与其它类型图表同款 initChart 销毁重建模式）
const renderSafetyHeatmap = () => {
  const chartDom = document.getElementById('safetyCategoryHeatmap')
  if (!chartDom) return

  const myChart = initChart(chartDom)
  const rows = safetyHeatmapRows.value
  const cols = safetyHeatmapCols.value
  if (!rows.length) return

  const data = []
  let maxVal = 0
  safetyHeatmapData.value.forEach((rowCounts, y) => {
    rowCounts.forEach((count, x) => {
      data.push([x, y, count])
      if (count > maxVal) maxVal = count
    })
  })

  myChart.setOption({
    tooltip: {
      confine: true,
      formatter: (p) => `${rows[p.value[1]]} × ${cols[p.value[0]]}：${p.value[2]} 条`
    },
    grid: { left: 110, right: 60, top: 10, bottom: 60 },
    xAxis: {
      type: 'category',
      data: cols,
      splitArea: { show: true },
      axisLabel: { fontSize: 12 }
    },
    yAxis: {
      type: 'category',
      data: rows,
      splitArea: { show: true },
      axisLabel: { fontSize: 12 }
    },
    visualMap: {
      min: 0,
      max: Math.max(maxVal, 1),
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      text: ['违规集中', '无违规'],
      inRange: {
        // 浅绿 → 黄 → 红：0 违规安全色，集中处显眼红
        color: ['#f0f9eb', '#f3d19e', '#f56c6c']
      },
      itemWidth: 12,
      itemHeight: 80
    },
    series: [{
      type: 'heatmap',
      data,
      label: { show: true, fontSize: 12, color: '#303133' },
      emphasis: {
        itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0, 0, 0, 0.3)' }
      }
    }]
  })
}

// 证据引用串（评审/合规材料可复制引用，定位到任务 + 执行版本 + 样本 + 攻击面）
const safetyEvidenceCite = (row) => {
  const taskId = result.value?.task_id || '-'
  const exec = selectedExecutionNumber.value ?? '-'
  const sample = row.attack_sample_id || `row-${(row.idx ?? 0) + 1}`
  const origin = row.is_mutation && row.mutated_from ? `（变体←${row.mutated_from}）` : ''
  return `${taskId} / 第${exec}次执行 / ${sample}${origin} / ${row.attack_category_name || row.attack_category || '-'}`
}

const safetyExpectedBehaviorLabel = (behavior) => {
  const map = { refuse: '应拒绝作答', safe_answer: '应安全作答', fact_check: '应核实后作答' }
  return map[behavior] || behavior || '-'
}

const hasSafetyEvidence = (row) => {
  const channels = row.safety?.channels || {}
  return !!(
    (channels.deterministic?.findings || []).length ||
    channels.judge_rubric?.reason ||
    channels.judge_rubric?.evidence ||
    (channels.deepeval && Object.keys(channels.deepeval).length)
  )
}

// ---------------- 插件评测（plugin 类型结果渲染辅助） ----------------
// 数据来源：result_aggregator.aggregate_plugin_results 的聚合结构
// （average_score/badge_summary/dimension_scores/anti_pattern_summary/items）。

const PLUGIN_DIMENSION_LABELS = {
  triggering_accuracy: '触发准确性',
  orchestration_fitness: '编排适配度',
  output_quality: '输出质量',
  scope_calibration: '范围校准',
  progressive_disclosure: '渐进式披露',
  token_efficiency: 'Token 效率',
  robustness: '健壮性',
  structural_completeness: '结构完整性',
  code_template_quality: '代码模板质量',
  ecosystem_coherence: '生态一致性'
}

const PLUGIN_BADGE_LABELS = {
  platinum: '白金 Platinum',
  gold: '金牌 Gold',
  silver: '银牌 Silver',
  bronze: '铜牌 Bronze',
  no_badge: '无徽章'
}

const badgeSummaryEntries = computed(() => {
  const summary = result.value?.result?.badge_summary || {}
  return Object.entries(summary).map(([badge, count]) => ({ badge, count }))
})

const pluginDimensionRows = computed(() => {
  const dimScores = result.value?.result?.dimension_scores || {}
  return Object.entries(dimScores)
    .map(([name, stat]) => ({
      name,
      label: PLUGIN_DIMENSION_LABELS[name] || name,
      average: stat?.average ?? 0,
      count: stat?.count ?? 0
    }))
    .sort((a, b) => b.average - a.average)
})

const antiPatternRows = computed(() => {
  const summary = result.value?.result?.anti_pattern_summary || {}
  return Object.entries(summary)
    .map(([flag, info]) => ({
      flag,
      count: info?.count ?? 0,
      max_severity: info?.max_severity ?? null,
      description: info?.description || ''
    }))
    .sort((a, b) => b.count - a.count)
})

const getBadgeLabel = (badge) => PLUGIN_BADGE_LABELS[badge] || badge || '无徽章'

// ---- 插件评测 · 会话 Trace 口径（真实数据分） ----
// 数据来源：aggregate_plugin_results 的 mode/real_data_summary 与 items[].real_data/trace/checks
const isTraceMode = computed(() => result.value?.result?.mode === 'trace')

const traceCheckSummary = computed(() => result.value?.result?.real_data_summary?.check_summary || {})

const traceDirectionRows = computed(() => result.value?.result?.real_data_summary?.direction_summary || [])

// 得分卡的口径字段：后端（新聚合）优先，缺失时按 check_summary 现算——
// 老执行记录里的 aggregate 是改动前算的，不带 score_label/decidable_count/conservative_score
const traceSummary = computed(() => {
  const summary = result.value?.result?.real_data_summary || {}
  const counts = summary.check_summary || {}
  const decidable = summary.decidable_count ?? ((counts.passed || 0) + (counts.failed || 0))
  const applicable = summary.applicable_count ?? (decidable + (counts.insufficient || 0))
  const conservative = summary.conservative_score ?? (
    applicable ? Math.round((counts.passed || 0) / applicable * 10000) / 100 : null
  )
  const partial = Object.keys(summary.status_summary || {}).some((status) => status !== 'complete')
  return {
    ...summary,
    decidable_count: decidable,
    applicable_count: applicable,
    conservative_score: conservative,
    score_label: summary.score_label || (partial ? '部分样本得分' : '真实数据得分')
  }
})

// ---- 两套分数（trace 合并口径）：综合分来自静态 + LLM 评审，真实数据分来自历史线程 ----
// items[] 里 composite_score/real_data_score 是后端改动后新加的键：老执行记录没有这两个键，
// 那时「行分（score）」装的是真实数据分，故按「键是否存在」区分，不做数值猜测。
const traceCompositeScore = (row) => {
  if (!row || row.composite_score === undefined) return null
  return row.composite_score === null ? null : row.composite_score
}

const traceRealScore = (row) => {
  if (!row) return null
  if (row.real_data_score !== undefined) return row.real_data_score
  // 老记录（无 real_data_score 键）：行分即真实数据分
  return row.score ?? null
}

const traceCompositeSummary = computed(() => {
  const summary = result.value?.result?.composite_summary
  if (summary) return summary
  // 老执行记录没有 composite_summary：按行现算（老行没有 composite_score 键 → 全部算「未出」）
  const items = result.value?.result?.items || []
  const scores = items.map(traceCompositeScore).filter((value) => value !== null)
  return {
    average_score: scores.length
      ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length * 100) / 100
      : null,
    measured_count: scores.length,
    unmeasured_count: items.length - scores.length,
    unit: 'row'
  }
})

// 设计质量线：按插件一条（后端 design_quality）；老记录没有该字段，回退为空
const traceDesignPlugins = computed(() => {
  const summary = result.value?.result?.design_quality
  if (summary?.plugins?.length) return summary.plugins
  return []
})

// 历史数据线阶段汇总（生成/执行两行，未测阶段保留）
const tracePhaseSummary = computed(() => result.value?.result?.real_data_summary?.phase_summary || [])

// 覆盖率口径：key_steps=关键步骤覆盖率，其余按行为覆盖率
const traceCoverageLabel = computed(() => {
  const scope = result.value?.result?.real_data_summary?.behavior_scope
  if (scope === 'key_steps') return '平均关键步骤覆盖率'
  if (scope === 'all') return '平均行为覆盖率'
  return '平均关键步骤覆盖率'
})

const tracePhaseLabel = (metric) => HISTORY_PHASE_LABELS[metric] || metric || '-'

const tracePhaseUnmeasured = (phase) =>
  !!phase && (phase.status === 'unmeasured' || phase.detected === false)

const traceModelUsageText = (usage) => {
  const rows = Object.entries(usage || {})
  return rows.length ? rows.map(([m, t]) => `${m}：${Number(t).toLocaleString()} tokens`).join('；') : '无'
}

// 老执行记录：两套分数的键都不存在，但行分有值 —— 页面要显式说明此列的口径
const traceIsLegacyRow = (row) =>
  !!row && row.composite_score === undefined && row.real_data_score === undefined && row.score != null

const traceDepthLabel = (depth) => (depth === 'standard' ? '标准' : depth === 'quick' ? '快速' : '-')

const traceSkillLayers = (skill) =>
  (skill?.layers || [])
    .map((l) => `${l.layer} ${l.score !== null && l.score !== undefined ? Number(l.score).toFixed(3) : '未测'}`)
    .join('；') || '-'

// 得分分母只有「通过 + 失败」：证据不足与不适用都不进分母
const traceDecidable = computed(() => {
  const c = traceCheckSummary.value
  return (c.passed || 0) + (c.failed || 0)
})

const traceLimitations = computed(() => {
  const items = result.value?.result?.items || []
  const texts = []
  for (const item of items) {
    for (const text of (item.real_data?.limitations || [])) {
      if (text && !texts.includes(text)) texts.push(text)
    }
  }
  return texts.slice(0, 6)
})

const TRACE_STATUS_LABELS = {
  complete: '完整',
  partial: '部分样本',
  unmeasured: '未测'
}

const traceStatusLabel = (status) => TRACE_STATUS_LABELS[status] || status || '未测'

const traceStatusTagType = (status) => {
  switch (status) {
    case 'complete': return 'success'
    case 'partial': return 'warning'
    default: return 'info'
  }
}

const TRACE_CHECK_LABELS = {
  passed: '通过',
  failed: '失败',
  insufficient: '证据不足',
  not_applicable: '不适用'
}

const traceCheckLabel = (status) => TRACE_CHECK_LABELS[status] || status || '-'

const traceCheckTagType = (status) => {
  switch (status) {
    case 'passed': return 'success'
    case 'failed': return 'danger'
    case 'not_applicable': return 'warning'
    default: return 'info'
  }
}

const traceCountsText = (counts) => {
  if (!counts || typeof counts !== 'object') return ''
  return Object.entries(counts).map(([name, n]) => `${name} × ${n}`).join('，')
}

// 方向名与 plugin-eval 报告同源（reporter.py 的 _DIMENSION_NAMES）：
// 规则文件里的 dimension 是抽取期的内部键，页面统一展示报告里的方向名。
// 行结果已带 dimension_label/label，此表只作老行（改动前落库）的兜底。
const HISTORY_DIMENSION_LABELS = {
  reporting_accuracy: '报告准确性',
  procedure_adherence: '流程遵循',
  error_handling: '异常处理可信度',
  source_alignment: '来源一致性',
  coverage: '需求覆盖',
  case_quality: '用例质量',
  workflow_reporting: '生成流程与报告'
}

const HISTORY_PHASE_LABELS = {
  historical_execution_reliability: '历史执行可信度',
  historical_generation_reliability: '历史生成可信度'
}

// 本步调用确认的判定文案（与报告「关键步骤」表一致）
const TRACE_CALL_LABELS = {
  passed: '已调用',
  failed: '缺失（有当时要求）',
  insufficient: '未见，无强制依据'
}

const traceDimensionLabel = (key) => HISTORY_DIMENSION_LABELS[key] || key || '-'

const traceNumber = (value, suffix = '') =>
  (value === null || value === undefined || Number.isNaN(value)) ? '未测' : `${value}${suffix}`

const traceCoverageText = (coverage) => {
  if (!coverage || coverage.rate === null || coverage.rate === undefined) return '未测'
  return `${Number(coverage.rate).toFixed(1)}%（${coverage.covered} / ${coverage.total}）`
}

const traceMeasuredText = (counts) => {
  const c = counts || {}
  const measured = (c.passed || 0) + (c.failed || 0)
  return `${measured} / ${measured + (c.insufficient || 0)}`
}

const traceCountsSlash = (counts) => {
  const c = counts || {}
  return `${c.passed || 0} / ${c.failed || 0} / ${c.insufficient || 0} / ${c.not_applicable || 0}`
}

// 敏感性下界：证据不足的检查全按违规计的对照值（plugin-eval 口径是 通过 ÷ (通过 + 失败)）
const traceConservativeScore = (counts) => {
  const c = counts || {}
  const applicable = (c.passed || 0) + (c.failed || 0) + (c.insufficient || 0)
  if (!applicable) return '未测'
  return `${Math.round((c.passed || 0) / applicable * 10000) / 100}/100`
}

// 报告「真实数据测评指标」的得分标签：抽取不完整时是「部分样本得分」
const traceScoreLabel = (realData) => {
  const data = realData || {}
  const label = (data.status === 'partial' || data.extraction_complete === false)
    ? '部分样本得分'
    : '真实数据得分'
  const score = data.score
  return `${label}：${score === null || score === undefined ? '未测' : score + '/100'}`
}

const traceDimensionRows = (dimensions) => {
  if (Array.isArray(dimensions)) return dimensions
  return Object.entries(dimensions || {}).map(([key, value]) => {
    const counts = (value || {}).counts || {}
    const measured = (counts.passed || 0) + (counts.failed || 0)
    return {
      key,
      label: traceDimensionLabel(key),
      score: (value || {}).score,
      coverage: (value || {}).coverage,
      measured,
      applicable: measured + (counts.insufficient || 0),
      counts
    }
  })
}

// 阶段块：新行走 trace.phases；老行从 trace.history 兜底适配（结构一致，仅方向未带名）
const tracePhaseBlocks = (row) => {
  const trace = (row || {}).trace || {}
  if ((trace.phases || []).length) return trace.phases
  const history = trace.history
  if (!history) return []
  return [{
    ...history,
    label: HISTORY_PHASE_LABELS[history.metric] || '历史执行可信度',
    dimensions: traceDimensionRows(history.dimensions),
    failed_checks: (row.checks || []).filter((check) => check.status === 'failed')
  }]
}

// 关键步骤按步骤名合并（与报告一致：实例数/尝试次数/最终达成/检查项数求和）
const traceKeyStepRows = (phase) => {
  const rows = []
  const index = {}
  for (const step of phase.key_steps || []) {
    const key = step.step || step.name || ''
    let row = index[key]
    if (!row) {
      row = index[key] = {
        key,
        name: step.name || step.step || '-',
        instances: 0,
        attempts: 0,
        achievedCount: 0,
        checks: 0,
        calls: {}
      }
      rows.push(row)
    }
    row.instances += 1
    row.attempts += step.attempts || 0
    if (step.achieved === true) row.achievedCount += 1
    row.checks += step.check_count || 0
    if (step.call_check) row.calls[step.call_check] = (row.calls[step.call_check] || 0) + 1
  }
  return rows.map((row) => ({
    ...row,
    achieved: row.achievedCount,
    calls: Object.entries(row.calls)
      .map(([status, n]) => `${TRACE_CALL_LABELS[status] || status} ${n}`)
      .join('、') || '—'
  }))
}

// 未被检查的关键步骤：未覆盖分类里存的是步骤键，按关键步骤表还原步骤名
const traceUncoveredRows = (phase) => {
  const uncovered = (phase.behavior_coverage || {}).uncovered_by_category || {}
  const names = {}
  for (const step of phase.key_steps || []) names[step.step] = step.name
  return Object.entries(uncovered).map(([key, count]) => ({ name: names[key] || key, count }))
}

const traceModelUsage = (phase) => {
  const usage = phase.model_usage || {}
  return Object.entries(usage).map(([model, tokens]) => ({
    model,
    tokens: typeof tokens === 'number' ? tokens.toLocaleString() : tokens
  }))
}

const getBadgeTagType = (badge) => {
  switch (badge) {
    case 'platinum': return 'success'
    case 'gold': return 'warning'
    case 'silver': return 'primary'
    case 'bronze': return 'info'
    default: return 'info'
  }
}

const formatSubScores = (subScores) => {
  if (!subScores || typeof subScores !== 'object') return '-'
  const entries = Object.entries(subScores)
  if (!entries.length) return '-'
  return entries
    .map(([key, val]) => (typeof val === 'number' ? `${key}: ${val.toFixed(3)}` : `${key}: ${val}`))
    .join('；')
}


// ---------------- 脚本产物（评测前处理脚本 pre_script_results） ----------------
// 数据来源：result.items[i].pre_script_results（judge 与 agent 类型通用，见 result_aggregator._format_items）；
// on_fail=abort 失败的条目保留脚本结果于 result.failed_items（result_aggregator._format_failed_items）。
// 任务未编排 pre_scripts 的历史结果无该字段，本区不渲染。

const preScriptSelectedIdx = ref(0)

// 拥有脚本结果的数据条目列表（含 abort 失败条目）
const preScriptEntries = computed(() => {
  if (!result.value?.result) return []
  const entries = []
  const collect = (list) => {
    if (Array.isArray(list)) {
      list.forEach(item => {
        if (Array.isArray(item?.pre_script_results) && item.pre_script_results.length > 0) {
          entries.push(item)
        }
      })
    }
  }
  collect(result.value.result.items)
  collect(result.value.result.failed_items)
  return entries
})

const hasPreScriptResults = computed(() => preScriptEntries.value.length > 0)

// 当前选中数据条目的脚本结果列表（越界时钳制到最后一个，避免切换任务后错位）
const currentPreScriptList = computed(() => {
  const entries = preScriptEntries.value
  if (entries.length === 0) return []
  const idx = Math.min(Math.max(preScriptSelectedIdx.value, 0), entries.length - 1)
  return entries[idx].pre_script_results || []
})

// 脚本执行状态 tag（标准输出契约 status: ok | skip | fail）
const getPreScriptStatusTag = (status) => {
  const map = {
    ok: { type: 'success', label: '成功' },
    skip: { type: 'info', label: '跳过' },
    fail: { type: 'danger', label: '失败' },
  }
  return map[status] || { type: 'info', label: status || '-' }
}

// finish_reason 文案（借鉴 DSH RunResult 语义）
const getFinishReasonLabel = (reason) => {
  const map = { completed: '正常完成', max_turns: '达到轮次上限', timeout: '执行超时', error: '执行出错' }
  return map[reason] || reason || '-'
}

const formatDurationMs = (ms) => {
  if (ms == null || isNaN(Number(ms))) return '-'
  const num = Number(ms)
  if (num < 1000) return `${Math.round(num)} ms`
  return `${(num / 1000).toFixed(2)} s`
}

// ---------------- 终态校验器证据（M2 QE2-1 verification_result） ----------------
// 数据来源：result.items[i].verification_result（judge 与 agent 通用，见
// result_aggregator._format_items）；on_fail=abort 失败条目在 failed_items 内
// 保留（_format_failed_items）。任务未配置校验器/历史结果无该字段时本区不渲染。

const verificationSelectedIdx = ref(0)

// 拥有校验结果的数据条目列表（含 abort 失败条目）
const verificationEntries = computed(() => {
  if (!result.value?.result) return []
  const entries = []
  const collect = (list) => {
    if (Array.isArray(list)) {
      list.forEach(item => {
        if (item?.verification_result && typeof item.verification_result === 'object') {
          entries.push(item)
        }
      })
    }
  }
  collect(result.value.result.items)
  collect(result.value.result.failed_items)
  return entries
})

const hasVerificationEntries = computed(() => verificationEntries.value.length > 0)

// 当前选中条目的整体校验结果（越界钳制，切换任务后不错位）
const currentVerification = computed(() => {
  const entries = verificationEntries.value
  if (entries.length === 0) return {}
  const idx = Math.min(Math.max(verificationSelectedIdx.value, 0), entries.length - 1)
  return entries[idx].verification_result || {}
})

const currentVerificationList = computed(() => currentVerification.value.results || [])

// 任务级校验通过率（聚合写入 result.verification；仅配置校验器的任务存在）
const verificationSummaryTag = computed(() => {
  const stats = result.value?.result?.verification
  if (!stats || typeof stats !== 'object') return null
  const rate = Number(stats.pass_rate ?? 0)
  const detail = `${stats.passed ?? 0}/${stats.total ?? 0} 行通过`
  if (rate >= 1) return { type: 'success', detail }
  if (rate > 0) return { type: 'warning', detail }
  return { type: 'danger', detail }
})

const verificationPassRateText = computed(() => {
  const stats = result.value?.result?.verification
  if (!stats) return '-'
  return `${Math.round(Number(stats.pass_rate ?? 0) * 100)}%`
})

// 校验器状态 tag（与后端 verifier_runner 的 pass/fail/error 一致）
const verificationStatusTag = (status) => {
  const map = {
    pass: { type: 'success', label: '通过' },
    fail: { type: 'danger', label: '未通过' },
    error: { type: 'warning', label: '错误' }
  }
  return map[status] || { type: 'info', label: status || '-' }
}

// 断言行状态 tag（file_assert/http_assert 的 checks[].passed）
const verificationCheckTag = (check) => {
  return check?.passed
    ? { type: 'success', label: '通过' }
    : { type: 'danger', label: '未通过' }
}

// 断言清单行（file_assert checks / http_assert checks 归一：http 的 name 键补 checkName）
const verifierCheckRows = (vEntry) => {
  const checks = vEntry?.evidence?.checks
  if (!Array.isArray(checks)) return []
  return checks.map(check => ({
    ...check,
    checkName: check.name,
    name: check.name === 'status' || check.name === 'body_regex' || check.name === 'jsonpath'
      ? ({ status: '状态码', body_regex: '响应体正则', jsonpath: '响应 JSONPath' }[check.name] || check.name)
      : check.name
  }))
}

// 证据键值 chips（script 摘要 / db_assert 统计等浅层字段）
const verifierEvidenceChips = (vEntry) => {
  const evidence = vEntry?.evidence
  if (!evidence || typeof evidence !== 'object') return []
  const chips = []
  if (evidence.summary) chips.push({ key: 'summary', label: '摘要', value: evidence.summary })
  const output = evidence.output
  if (output && typeof output === 'object') {
    for (const key of ['total', 'passed', 'failed', 'consistency_rate']) {
      if (output[key] != null) chips.push({ key, label: key, value: String(output[key]) })
    }
  }
  if (evidence.status_code != null) chips.push({ key: 'status_code', label: '状态码', value: String(evidence.status_code) })
  if (evidence.finish_reason) chips.push({ key: 'finish_reason', label: '结束原因', value: evidence.finish_reason })
  return chips
}

const verifierValueText = (value) => {
  if (value == null) return '-'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

// ---------------- 轨迹匹配证据（M2 QE2-2 trajectory_match） ----------------
// 数据来源：result.items[i].trajectory_match（agent 类型，见 result_aggregator
// ._format_items）。任务未配置轨迹断言/历史结果无该字段时本区不渲染。

const trajectorySelectedIdx = ref(0)

// 拥有轨迹匹配结果的数据条目列表
const trajectoryEntries = computed(() => {
  if (!result.value?.result) return []
  const entries = []
  const collect = (list) => {
    if (Array.isArray(list)) {
      list.forEach(item => {
        if (item?.trajectory_match && typeof item.trajectory_match === 'object') {
          entries.push(item)
        }
      })
    }
  }
  collect(result.value.result.items)
  collect(result.value.result.failed_items)
  return entries
})

const hasTrajectoryEntries = computed(() => trajectoryEntries.value.length > 0)

// 当前选中条目的匹配结果（越界钳制，切换任务后不错位）
const currentTrajectory = computed(() => {
  const entries = trajectoryEntries.value
  if (entries.length === 0) return {}
  const idx = Math.min(Math.max(trajectorySelectedIdx.value, 0), entries.length - 1)
  return entries[idx].trajectory_match || {}
})

// 任务级轨迹匹配率（聚合写入 result.trajectory；仅配置轨迹断言的任务存在）
const trajectorySummaryTag = computed(() => {
  const stats = result.value?.result?.trajectory
  if (!stats || typeof stats !== 'object') return null
  const rate = Number(stats.pass_rate ?? 0)
  const detail = `${stats.passed ?? 0}/${stats.total ?? 0} 行通过`
  if (rate >= 1) return { type: 'success', detail }
  if (rate > 0) return { type: 'warning', detail }
  return { type: 'danger', detail }
})

const trajectoryMatchRateText = computed(() => {
  const stats = result.value?.result?.trajectory
  if (!stats) return '-'
  return `${Math.round(Number(stats.pass_rate ?? 0) * 100)}%`
})

// 时间线节点配色：covered=绿 / unexpected=灰（hollow）/ forbidden=红
const trajectoryTimelineType = (kind) => (
  { covered: 'success', unexpected: 'info', forbidden: 'danger' }[kind] || 'info'
)

// per_case 比对结论：一致绿 / 不一致红 / 缺失灰（兼容只有 match 布尔、无 match_state 的结构）
const getMatchStateOfRow = (row) => {
  return row?.match_state || (row?.match ? 'consistent' : 'missing')
}

const getMatchStateTag = (row) => {
  const state = getMatchStateOfRow(row)
  const map = {
    consistent: { type: 'success', label: '一致' },
    mismatch: { type: 'danger', label: '不一致' },
    missing: { type: 'info', label: '缺失' },
  }
  return map[state] || { type: 'warning', label: state || '-' }
}

const getActualStatusTag = (status) => {
  if (!status) return { type: 'info', label: '未匹配' }
  const map = {
    passed: { type: 'success', label: 'passed' },
    failed: { type: 'danger', label: 'failed' },
    broken: { type: 'danger', label: 'broken' },
    skipped: { type: 'warning', label: 'skipped' },
  }
  return map[status] || { type: 'info', label: status }
}

const getPreCaseRowClassName = ({ row }) => {
  const state = getMatchStateOfRow(row)
  if (state === 'mismatch') return 'pre-case-row-mismatch'
  if (state === 'missing') return 'pre-case-row-missing'
  if (state === 'consistent') return 'pre-case-row-consistent'
  return ''
}

const getPreCaseRows = (script) => {
  const out = script?.output
  return out && Array.isArray(out.per_case) ? out.per_case : []
}

// ---------------- 预置脚本类别识别与专属渲染（xml_diff / log_grep / db_assert） ----------------
// 二期预置脚本的标准输出键尚未在后端固化，此处按 script_id/script_name 识别类别、
// 按常见别名拾取字段并防御式渲染：字段缺失时回退通用展示，历史任务不受影响。

// 识别预置脚本类别（builtin:xml_diff / xml_diff / sql_assert 等命名变体，大小写不敏感）
const getPreScriptKind = (script) => {
  const raw = `${script?.script_id || ''} ${script?.script_name || ''}`.toLowerCase()
  if (raw.includes('xml') && raw.includes('diff')) return 'xml_diff'
  if (raw.includes('log') && (raw.includes('grep') || raw.includes('match'))) return 'log_grep'
  if ((raw.includes('db') || raw.includes('database') || raw.includes('sql')) && raw.includes('assert')) return 'db_assert'
  return ''
}

// 单元格取值格式化：空值返回 ''（配合 truncateText 展示 '-'、openDetail 不弹窗），
// 对象值序列化为缩进 JSON 供点击查看全量
const formatCellValue = (value) => {
  if (value === null || value === undefined || value === '') return ''
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

// 按候选键顺序拾取行内第一个非空字段（兼容后端字段命名变体）
const pickRowField = (row, keys) => {
  if (!row || typeof row !== 'object') return ''
  for (const k of keys) {
    const v = row[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return ''
}

// 各渲染位置的候选字段名（首个命中者生效）。
// 后端真实行键：xml_diff 为 type/path/tag/attribute/a/b（app/scripts_builtin/xml_diff.py
// _diff_*：a=基准侧、b=待比对侧，即「预期值/实际值」）；db_assert 为
// name/sql/passed/expected/actual/error（app/scripts_builtin/db_assert.py，
// error 承载失败断言原因，如「row_count 期望 3，实际 5」）。
const XML_DIFF_PATH_KEYS = ['path', 'xpath', 'node', 'node_path', 'location', 'element', 'target']
const XML_DIFF_TYPE_KEYS = ['diff_type', 'type', 'kind', 'change_type', 'severity']
const XML_DIFF_EXPECTED_KEYS = ['expected', 'expected_value', 'base', 'old_value', 'a']
const XML_DIFF_ACTUAL_KEYS = ['actual', 'actual_value', 'target_value', 'new_value', 'b']
const XML_DIFF_NOTE_KEYS = ['message', 'detail', 'evidence', 'note', 'description']
const DB_ASSERT_NAME_KEYS = ['name', 'assert_name', 'assertion', 'metric', 'field', 'column', 'key']
const DB_ASSERT_EXPECTED_KEYS = ['expected', 'expected_value', 'expect']
const DB_ASSERT_ACTUAL_KEYS = ['actual', 'actual_value', 'value']
const DB_ASSERT_NOTE_KEYS = ['message', 'detail', 'note', 'description', 'error']
const LOG_SAMPLE_TEXT_KEYS = ['line', 'content', 'text', 'message', 'sample']
const LOG_SAMPLE_NO_KEYS = ['line_no', 'line_number', 'lineno', 'no']

// xml_diff 差异清单行（原始元素为字符串时归一化为 {path}）
const getXmlDiffRows = (script) => {
  const out = script?.output
  if (!out || typeof out !== 'object') return []
  const list = out.diffs ?? out.diff_list ?? out.per_node ?? out.differences
  if (!Array.isArray(list)) return []
  return list.map((r) => (r !== null && typeof r === 'object' ? r : { path: r }))
}

// 差异类型 tag：移除/错误红、新增/警告橙、其余灰
const getDiffTypeTag = (typeText) => {
  const raw = String(typeText || '').trim()
  if (!raw || raw === '-') return { type: 'info', label: '-' }
  const s = raw.toLowerCase()
  if (s.includes('miss') || s.includes('remov') || s.includes('error') || s.includes('fail')) {
    return { type: 'danger', label: raw }
  }
  if (s.includes('add') || s.includes('warn') || s.includes('insert')) {
    return { type: 'warning', label: raw }
  }
  return { type: 'info', label: raw }
}

// xml_diff「说明」列：后端差异行无 message 类字段（键为 type/path/tag/attribute/a/b），
// 行内说明键缺失时按差异类型推导中文说明（属性类附属性名），供表格与完整明细弹窗共用
const XML_DIFF_TYPE_NOTES = {
  node_added: '新增节点',
  node_removed: '删除节点',
  attribute_added: '新增属性',
  attribute_removed: '删除属性',
  attribute_changed: '属性值变化',
  text_changed: '文本内容变化',
}
const getXmlDiffNote = (row) => {
  const note = formatCellValue(pickRowField(row, XML_DIFF_NOTE_KEYS))
  if (note) return note
  const type = String(pickRowField(row, XML_DIFF_TYPE_KEYS) || '').trim().toLowerCase()
  const attr = formatCellValue(pickRowField(row, ['attribute']))
  const desc = XML_DIFF_TYPE_NOTES[type] || ''
  return desc && attr && attr !== '-' ? `${desc} ${attr}` : desc
}

// db_assert 名值断言比对行（原始元素为字符串时归一化为 {name}）
const getDbAssertRows = (script) => {
  const out = script?.output
  if (!out || typeof out !== 'object') return []
  const list = out.asserts ?? out.assertions ?? out.per_assert ?? out.results
  if (!Array.isArray(list)) return []
  return list.map((r) => (r !== null && typeof r === 'object' ? r : { name: r }))
}

// 断言结论 tag：passed/match/ok 布尔优先，缺失时按 match_state/status 文本推断
const getAssertPassTag = (row) => {
  let v = row?.passed ?? row?.match ?? row?.ok ?? row?.assert_ok
  if (v === undefined || v === null) {
    const s = String(row?.match_state ?? row?.status ?? '').toLowerCase()
    if (['pass', 'passed', 'ok', 'true', 'consistent'].includes(s)) v = true
    else if (['fail', 'failed', 'false', 'mismatch'].includes(s)) v = false
  }
  if (v === true) return { type: 'success', label: '通过' }
  if (v === false) return { type: 'danger', label: '失败' }
  return { type: 'info', label: '-' }
}

const getDbAssertRowClassName = ({ row }) => {
  const tag = getAssertPassTag(row)
  if (tag.label === '通过') return 'pre-assert-row-pass'
  if (tag.label === '失败') return 'pre-assert-row-fail'
  return ''
}

// log_grep 模式统计列表（后端 output.patterns[]：pattern/type/match_count/hit_rate/sample_lines）
const getLogGrepPatterns = (script) => {
  const out = script?.output
  if (!out || typeof out !== 'object') return []
  return Array.isArray(out.patterns) ? out.patterns : []
}

// log_grep 命中样例行：后端样例嵌套在每个模式的 sample_lines 内
// （app/scripts_builtin/log_grep.py：patterns[i].sample_lines = [{line_no, line}]），
// 此处平铺聚合供「命中样例」块与完整明细共用；多模式时给样例附 pattern 字段
// 便于区分归属。顶层 sample_lines/samples/matched_lines/matches 仅作兼容兜底。
const getLogGrepSamples = (script) => {
  const out = script?.output
  if (!out || typeof out !== 'object') return []
  const patterns = getLogGrepPatterns(script)
  const withSamples = patterns.filter(
    (p) => Array.isArray(p?.sample_lines) && p.sample_lines.length > 0
  )
  if (withSamples.length > 0) {
    const flat = []
    for (const p of withSamples) {
      for (const s of p.sample_lines) {
        flat.push(
          withSamples.length > 1 && s !== null && typeof s === 'object'
            ? { ...s, pattern: p.pattern }
            : s
        )
      }
    }
    return flat
  }
  const list = out.sample_lines ?? out.samples ?? out.matched_lines ?? out.matches
  return Array.isArray(list) ? list : []
}

const getLogGrepSampleText = (script) => {
  const samples = getLogGrepSamples(script)
  if (samples.length === 0) return ''
  return samples
    .map((s) => {
      if (s === null || s === undefined) return ''
      if (typeof s !== 'object') return String(s)
      // 多模式平铺时的归属前缀（模式名截断防超长正则撑爆行宽）
      const patRaw = s.pattern != null ? String(s.pattern) : ''
      const pat = patRaw ? `[${patRaw.length > 50 ? patRaw.slice(0, 50) + '…' : patRaw}] ` : ''
      const no = pickRowField(s, LOG_SAMPLE_NO_KEYS)
      const text = formatCellValue(pickRowField(s, LOG_SAMPLE_TEXT_KEYS))
      return `${pat}${no !== '' ? `L${no}: ` : ''}${text}`
    })
    .join('\n')
}

// 「查看完整明细」按钮可见条件：per_case 或类别专属明细行任一非空
const hasPreScriptDetailRows = (script) => {
  if (getPreCaseRows(script).length > 0) return true
  const kind = getPreScriptKind(script)
  if (kind === 'xml_diff') return getXmlDiffRows(script).length > 0
  if (kind === 'db_assert') return getDbAssertRows(script).length > 0
  if (kind === 'log_grep') return getLogGrepSamples(script).length > 0
  return false
}

// ---------------- 结果观察（evaluation/result 扩展点） ----------------
// 后端观察产出附加在每条 result 上（pre_judge_runner.run_result_observers 写
// result["observations"]），聚合后经 result_aggregator._format_items 平铺为
// result.items[i].observations / failed_items[i].observations（聚合顶层无该键）；
// 聚合顶层 observations 仅作旧结构兜底。多条数据各自携带观察时合并展示并在标题
// 标注数据条目序号。
// 条目兼容字符串 / {title, level, content} 对象 / {key: value} 对象三种形态；
// 后端标准条目 {extension_point, observer, status, output, files, summary, error,
// duration_ms} 按 observer→标题、status→级别、summary/error/output→正文映射
// （fail 条目 summary 为空、error 有值，不得整条过滤）。
const observationItems = computed(() => {
  const host = result.value
  if (!host) return []

  const toEntries = (obs) => {
    if (Array.isArray(obs)) return obs
    if (obs && typeof obs === 'object') {
      return Object.entries(obs).map(([k, v]) => ({ title: k, content: v }))
    }
    return [obs]
  }
  const groups = []
  const collect = (list) => {
    if (!Array.isArray(list)) return
    list.forEach((item) => {
      const obs = item?.observations
      if (obs == null || obs === '') return
      groups.push({ itemIdx: item?.idx, entries: toEntries(obs) })
    })
  }
  collect(host.result?.items)
  collect(host.result?.failed_items)
  const topLevel = host.result?.observations ?? host.observations
  if (topLevel != null && topLevel !== '') {
    groups.push({ itemIdx: null, entries: toEntries(topLevel) })
  }

  const multiItems = groups.length > 1
  return groups
    .flatMap(({ itemIdx, entries }) => entries.map((entry) => ({ entry, itemIdx })))
    .map(({ entry, itemIdx }) => {
      if (entry === null || entry === undefined || entry === '') return null
      let title = ''
      let level = ''
      let text = ''
      let outputText = ''
      if (typeof entry !== 'object') {
        text = String(entry)
      } else {
        title = formatCellValue(pickRowField(entry, ['title', 'observer', 'name', 'key', 'summary']))
        level = formatCellValue(pickRowField(entry, ['status', 'level', 'severity']))
        text = formatCellValue(
          pickRowField(entry, ['summary', 'content', 'text', 'message', 'detail', 'description', 'error', 'output', 'value'])
        )
        // output 为结构化对象时不进正文行（避免挤压摘要），拼进点击后的完整明细
        if (entry.output != null && typeof entry.output === 'object' && Object.keys(entry.output).length > 0) {
          outputText = formatCellValue(entry.output)
        }
      }
      if (multiItems && itemIdx != null) {
        const prefix = `第 ${Number(itemIdx) + 1} 条数据`
        title = title ? `${prefix} · ${title}` : prefix
      }
      const fullTextParts = [title, text]
      if (outputText && outputText !== text) fullTextParts.push(outputText)
      return { title, level, text, fullText: fullTextParts.filter(Boolean).join('\n') }
    })
    .filter(Boolean)
    .filter((o) => o.title || (o.text && o.text !== ''))
})

// 观察条目级别 tag：后端 status（ok/skip/fail）与通用 level 值统一映射中文标签，
// 错误红 / 警告橙 / 成功绿 / 跳过灰 / 其余原样灰
const getObservationLevelTag = (level) => {
  const raw = String(level || '').trim()
  if (!raw) return { type: 'info', label: '' }
  const s = raw.toLowerCase()
  if (['ok', 'success', 'pass', 'passed'].includes(s)) return { type: 'success', label: '成功' }
  if (['skip', 'skipped'].includes(s)) return { type: 'info', label: '跳过' }
  if (['fail', 'failed', 'error', 'danger', 'critical'].includes(s)) return { type: 'danger', label: '失败' }
  if (['warn', 'warning'].includes(s)) return { type: 'warning', label: '警告' }
  return { type: 'info', label: raw }
}

// 摘要卡片统计芯片：比对脚本已知统计 + 其他脚本输出的标量键（上限 8 个防过载）
const PRE_SCRIPT_KNOWN_OUTPUT_KEYS = new Set(['consistency_rate', 'mismatched_count', 'summary_text', 'per_case', 'p0_skipped'])
// 已知输出键的中文标签（未命中别名的键原样展示，保持兼容）。
// 覆盖四个内置脚本真实输出键：allure_vs_cases / xml_diff（identical/diff_count/
// total_nodes/diff_truncated，app/scripts_builtin/xml_diff.py）/ log_grep
// （total_hits/matched_line_count/total_lines，app/scripts_builtin/log_grep.py）/
// db_assert（total/passed/failed，app/scripts_builtin/db_assert.py）
const PRE_SCRIPT_CHIP_LABELS = {
  matched_count: '命中行数', match_count: '命中行数', hits: '命中行数', hit_count: '命中行数',
  total_hits: '命中行次', matched_line_count: '命中行数（去重）',
  scanned_lines: '扫描行数', total_lines: '总行数',
  total_diffs: '差异数', diff_count: '差异数', changed_count: '变更数',
  total_nodes: '节点总数', identical: '完全一致', diff_truncated: '差异截断',
  passed_count: '通过断言', failed_count: '失败断言', total_asserts: '断言总数',
  passed: '通过断言', failed: '失败断言',
  pattern: '检索模式', regex: '检索模式', target_table: '目标表',
  total: '总数', consistent_count: '一致数',
}
const getPreScriptStatChips = (script) => {
  const out = script?.output
  if (!out || typeof out !== 'object') return []
  const chips = []
  // 多个输出键别名可能映射同一中文标签（如 matched_count/hits 均→「命中行数」），
  // 同标签只保留首个：既避免冗余展示，也保证 v-for 的 :key 不重复
  const pushChip = (key, label, value) => {
    if (chips.some((c) => c.label === label)) return
    chips.push({ key, label, value })
  }
  if (out.consistency_rate != null) pushChip('consistency_rate', '一致率', `${safePercent(out.consistency_rate)}%`)
  if (out.mismatched_count != null) pushChip('mismatched_count', '不一致', String(out.mismatched_count))
  if (Array.isArray(out.per_case)) pushChip('per_case', '用例数', String(out.per_case.length))
  if (Array.isArray(out.p0_skipped) && out.p0_skipped.length > 0) {
    pushChip('p0_skipped', 'P0跳过', String(out.p0_skipped.length))
  }
  for (const [key, value] of Object.entries(out)) {
    if (chips.length >= 8) break
    if (PRE_SCRIPT_KNOWN_OUTPUT_KEYS.has(key) || value == null || typeof value === 'object') continue
    pushChip(
      key,
      PRE_SCRIPT_CHIP_LABELS[key] || key,
      typeof value === 'boolean' ? (value ? '是' : '否') : truncateText(value, 30)
    )
  }
  return chips
}

// 完整明细弹窗（复用 detailDialog：全量不截断 + 复制按钮）
const openPreScriptFullDetail = (script) => {
  if (!script) return
  const perCase = getPreCaseRows(script)
  const lines = []
  lines.push(`脚本: ${script.script_name || script.script_id || '-'}`)
  lines.push(`状态: ${getPreScriptStatusTag(script.status).label}    结束原因: ${getFinishReasonLabel(script.finish_reason)}    耗时: ${formatDurationMs(script.duration_ms)}`)
  if (script.summary) lines.push(`摘要: ${script.summary}`)
  if (script.error) lines.push(`错误: ${script.error}`)
  if (perCase.length > 0) {
    lines.push('')
    lines.push(`=== 逐条比对明细（共 ${perCase.length} 条）===`)
    perCase.forEach((c, i) => {
      lines.push('')
      lines.push(`[${i + 1}] ${c?.case || '-'}${c?.priority ? `（${c.priority}）` : ''}`)
      lines.push(`预期: ${c?.expected || '(空)'}`)
      lines.push(`实际状态: ${c?.actual_status || '未匹配'}`)
      lines.push(`是否一致: ${getMatchStateTag(c).label}`)
      if (c?.evidence) lines.push(`证据: ${c.evidence}`)
    })
  } else if (getPreScriptKind(script) === 'xml_diff' && getXmlDiffRows(script).length > 0) {
    const rows = getXmlDiffRows(script)
    lines.push('')
    lines.push(`=== 差异清单（共 ${rows.length} 条）===`)
    rows.forEach((r, i) => {
      lines.push('')
      lines.push(`[${i + 1}] ${formatCellValue(pickRowField(r, XML_DIFF_PATH_KEYS)) || '-'}`)
      lines.push(`差异类型: ${formatCellValue(pickRowField(r, XML_DIFF_TYPE_KEYS)) || '-'}`)
      lines.push(`预期: ${formatCellValue(pickRowField(r, XML_DIFF_EXPECTED_KEYS)) || '-'}`)
      lines.push(`实际: ${formatCellValue(pickRowField(r, XML_DIFF_ACTUAL_KEYS)) || '-'}`)
      const note = getXmlDiffNote(r)
      if (note) lines.push(`说明: ${note}`)
    })
  } else if (getPreScriptKind(script) === 'db_assert' && getDbAssertRows(script).length > 0) {
    const rows = getDbAssertRows(script)
    lines.push('')
    lines.push(`=== 断言比对明细（共 ${rows.length} 条）===`)
    rows.forEach((r, i) => {
      lines.push('')
      lines.push(`[${i + 1}] ${formatCellValue(pickRowField(r, DB_ASSERT_NAME_KEYS)) || '-'}`)
      lines.push(`预期: ${formatCellValue(pickRowField(r, DB_ASSERT_EXPECTED_KEYS)) || '-'}`)
      lines.push(`实际: ${formatCellValue(pickRowField(r, DB_ASSERT_ACTUAL_KEYS)) || '-'}`)
      lines.push(`结果: ${getAssertPassTag(r).label}`)
      const note = formatCellValue(pickRowField(r, DB_ASSERT_NOTE_KEYS))
      if (note) lines.push(`说明: ${note}`)
    })
  } else if (getPreScriptKind(script) === 'log_grep' && getLogGrepSamples(script).length > 0) {
    lines.push('')
    lines.push(`=== 命中样例（${getLogGrepSamples(script).length} 行）===`)
    lines.push(getLogGrepSampleText(script))
  } else if (script.output && Object.keys(script.output).length > 0) {
    lines.push('')
    lines.push('=== 脚本输出（无逐条明细）===')
    lines.push(JSON.stringify(script.output, null, 2))
  } else {
    lines.push('')
    lines.push('（脚本无输出明细）')
  }
  openDetail(`脚本产物明细 - ${script.script_name || ''}`, lines.join('\n'))
}

// 客户端导出脚本明细 JSON（服务端 files 产物随任务清理，故由结果内嵌的 output 生成下载）
const downloadPreScriptDetail = (script) => {
  if (!script) return
  const safeName = String(script.script_name || script.script_id || 'script').replace(/[^0-9A-Za-z_.-]/g, '_')
  const data = {
    script_name: script.script_name,
    script_id: script.script_id,
    status: script.status,
    summary: script.summary,
    finish_reason: script.finish_reason,
    duration_ms: script.duration_ms,
    error: script.error,
    output: script.output || {},
  }
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  downloadBlob(blob, `pre_script_${safeName}_${Date.now()}.json`)
  ElMessage.success('脚本明细 JSON 已下载')
}

const loadTasks = async () => {
  try {
    const res = await taskApi.list({ limit: 100 })
    allTasks.value = res || []
    tasks.value = allTasks.value
    completedTasks.value = allTasks.value.filter(t => t.status === 'completed')
  } catch (e) {
    console.error(e)
  }
}

// 轮询配置
const { startPolling, stopPolling, stopAll } = useTaskPolling({
  interval: 8000,
  onTaskUpdate: (taskId, updatedTask) => {
    // 如果更新的任务是当前选中的任务，刷新任务列表和结果
    if (taskId === selectedTaskId.value) {
      // 更新 allTasks 中的任务状态
      const idx = allTasks.value.findIndex(t => t.id === taskId)
      if (idx !== -1) {
        allTasks.value[idx] = { ...allTasks.value[idx], ...updatedTask }
      }
      // 如果任务完成或失败，重新加载结果
      if (['completed', 'failed', 'stopped'].includes(updatedTask.status)) {
        stopPolling(taskId)
        // W14 轮询走轻端点，载荷不含任务名：从列表行补齐
        const rowName = allTasks.value.find(t => t.id === taskId)?.name
        ElMessage.success(`任务 "${rowName}" 已${updatedTask.status === 'completed' ? '完成' : '结束'}`)
        // 刷新历史版本（轻量模式）并自动加载最新完成版本，避免结果区停留在执行中旧态
        resultApi.history(taskId, { limit: 50, include_result: false }).then(async versions => {
          executionVersions.value = Array.isArray(versions) ? versions : []
          if (taskId === selectedTaskId.value) {
            const latestCompleted = executionVersions.value.find(v => v.status === 'completed')
            if (latestCompleted) {
              selectedExecutionNumber.value = latestCompleted.execution_number
              await loadResultByVersion(latestCompleted.execution_number)
            }
          }
        })
        // 任务跑完时同步刷新多次执行聚合趋势：running 任务完成后聚合区立即出现/补上
        // 最新一次，不必手动重选任务（旧后端无端点时静默置空，聚合区整体隐藏）
        resultApi.dimensionTrend(taskId, 50).then(async trend => {
          executionTrend.value = Array.isArray(trend) ? trend : []
          // 趋势晚于结果到达时聚合区刚从隐藏变可见，补一次渲染；
          // 结果尚未就绪时由 loadResultByVersion 完成后的 renderCharts 兜底
          await nextTick()
          if (isResultRenderable(result.value) && executionTrend.value.length >= 2) {
            renderTrendCharts()
          }
        }).catch(() => {
          executionTrend.value = []
        })
      }
    }
  },
  onTaskComplete: (taskId, task) => {
    if (taskId === selectedTaskId.value) {
      // W14 轮询走轻端点，载荷不含任务名：从列表行补齐
      const rowName = allTasks.value.find(t => t.id === taskId)?.name
      ElMessage.success(`任务 "${rowName}" 已完成`)
    }
  },
  onTaskFailed: (taskId, task) => {
    if (taskId === selectedTaskId.value) {
      const rowName = allTasks.value.find(t => t.id === taskId)?.name
      ElMessage.error(`任务 "${rowName}" 执行失败`)
    }
  }
})

// 选择任务后加载历史版本列表
const onTaskChange = async (taskId) => {
  result.value = null
  resultItems.value = []
  executionVersions.value = []
  // 切换任务时重置多次执行聚合数据，避免残留上个任务的趋势
  executionTrend.value = []
  selectedExecutionNumber.value = null

  if (!taskId) return

  // 查找选中的任务
  const selectedTask = allTasks.value.find(t => t.id === taskId)

  // 如果任务是 running 状态，开始轮询
  if (selectedTask?.status === 'running') {
    startPolling(taskId)
  }

  resultLoading.value = true
  try {
    // 轻量模式拉版本列表（include_result=false）：结果展示慢的根因是历史接口
    // 把所有版本的完整 result 一次性传回，而前端只渲染选中的一版
    const versions = await resultApi.history(taskId, { limit: 50, include_result: false })
    executionVersions.value = Array.isArray(versions) ? versions : []

    // 多次执行维度分数趋势：旧后端无 dimension-trend 端点时静默置空，聚合区整体隐藏
    executionTrend.value = await resultApi.dimensionTrend(taskId, 50).catch(() => [])

    // 自动选中最新 completed 版本；没有则选第一条
    const latestCompleted = executionVersions.value.find(v => v.status === 'completed')
    const autoSelect = latestCompleted || executionVersions.value[0]
    if (autoSelect) {
      selectedExecutionNumber.value = autoSelect.execution_number
      await loadResultByVersion(autoSelect.execution_number)
    }
  } catch (e) {
    console.error('获取历史版本失败', e)
    // 降级：直接加载当前结果
    await loadResult()
  } finally {
    resultLoading.value = false
  }
}

// 按版本号加载结果
const loadResultByVersion = async (executionNumber) => {
  const version = executionVersions.value.find(v => v.execution_number === executionNumber)
  if (!version) return

  const currentTask = allTasks.value.find(t => t.id === selectedTaskId.value)
  resultLoading.value = true
  try {
    // 轻量历史模式下 version.result 为空：从单版端点取全量；旧后端无该端点时
    // 回退用历史响应内嵌的 version.result，两种部署顺序下都可用
    let detail = version
    if (!version.result) {
      try {
        detail = await resultApi.historyVersion(selectedTaskId.value, executionNumber)
      } catch (e) {
        console.error('获取单版结果失败', e)
        ElMessage.error('加载该版本结果失败，请稍后重试')
        return
      }
    }

    result.value = {
      task_id: selectedTaskId.value,
      task_name: currentTask?.name || '',
      evaluation_type: currentTask?.evaluation_type || '',
      status: detail.status || version.status,
      result: detail.result,
      total_cost: detail.total_cost,
      completed_at: detail.completed_at,
      error_message: detail.error_message,
      // M1 门禁判定：单版端点随结果返回（null=该版未启用门禁，横幅不显示）
      gate_result: detail.gate_result || null,
      // M2 QE2-4 裁判治理快照：单版端点随结果返回（null=该版未走裁判链路）
      judge_config_snapshot: detail.judge_config_snapshot || null,
    }

    resultItems.value = safeGetItems(result.value.result)
    // 重置脚本产物/终态校验的数据条目选择，避免跨任务/版本残留旧选中项
    preScriptSelectedIdx.value = 0
    verificationSelectedIdx.value = 0
    trajectorySelectedIdx.value = 0
    await nextTick()
    if (isResultRenderable(result.value)) {
      renderCharts()
    }
  } finally {
    resultLoading.value = false
  }
}

const loadResult = async () => {
  if (!selectedTaskId.value) return

  try {
    const res = await resultApi.get(selectedTaskId.value)
    result.value = res
    resultItems.value = safeGetItems(res.result)
    // 重置脚本产物/终态校验的数据条目选择，避免跨任务残留旧选中项
    preScriptSelectedIdx.value = 0
    verificationSelectedIdx.value = 0
    trajectorySelectedIdx.value = 0
    await nextTick()
    if (isResultRenderable(result.value)) {
      renderCharts()
    }
  } catch (e) {
    console.error(e)
  }
}

const renderCharts = () => {
  if (!result.value?.result) return

  const res = result.value.result

  // 插件评测类型无 ECharts 图表区（维度条/徽章/明细均为表格化展示）
  if (result.value.evaluation_type === 'plugin') {
    return
  }

  // 耗时评测类型单独处理
  if (result.value.evaluation_type === 'latency') {
    nextTick(() => renderLatencyHistogram(res))
    return
  }

  // RAG 类型单独处理
  if (result.value.evaluation_type === 'rag') {
    nextTick(() => renderRagRadarChart(res.metrics_summary))
    return
  }

  // 安全评测类型单独处理（M3 QE3-3：类目热力图）
  if (result.value.evaluation_type === 'safety') {
    nextTick(() => renderSafetyHeatmap())
    return
  }

  // 渲染雷达图 - 各维度评分
  renderRadarChart(res.dimension_scores)

  // 渲染柱状图 - 评分分布
  renderBarChart(res)

  // 渲染饼图 - 成功率
  renderPieChart(res)

  // 渲染折线图 - 评分趋势
  renderLineChart(resultItems.value)

  // 多次执行聚合展示（通用类型专属；rag/consistency/latency 等专属分支已在上方 return）
  nextTick(() => renderTrendCharts())
}

// 渲染前先销毁同 DOM 上的旧实例：任务/版本切换反复 init 会残留实例（控制台告警 + 内存增长）
const initChart = (dom) => {
  const existing = echarts.getInstanceByDom(dom)
  if (existing) existing.dispose()
  return echarts.init(dom)
}

const renderRagRadarChart = (metricsSummary) => {
  const chartDom = document.getElementById('ragRadarChart')
  if (!chartDom) return

  const myChart = initChart(chartDom)

  if (!metricsSummary || Object.keys(metricsSummary).length === 0) {
    myChart.setOption({
      title: { text: '暂无指标数据', left: 'center', top: 'middle' }
    })
    return
  }

  const indicators = Object.entries(metricsSummary).map(([name]) => ({
    name: ragMetricNames[name] || name,
    max: 1.0
  }))
  const values = Object.values(metricsSummary).map(d => d.average || 0)
  const scoreByName = new Map(
    Object.entries(metricsSummary).map(([name, d]) => [ragMetricNames[name] || name, d.average || 0])
  )

  myChart.setOption({
    // 分值直显在指标名下方，不再用悬停 tooltip——雷达 tooltip 粒度是整个数据项
    // （悬停任意处都全量罗列所有指标），指标多时溢出截断
    tooltip: { show: false },
    radar: {
      indicator: indicators,
      shape: 'polygon',
      splitNumber: 5,
      axisName: {
        fontSize: 12,
        formatter: (name) => {
          const score = scoreByName.get(name)
          return score != null ? `{v|${name}}\n{s|${Number(score).toFixed(3)}}` : name
        },
        rich: {
          v: { color: '#666', fontSize: 12, lineHeight: 16, align: 'center' },
          s: { color: '#67C23A', fontSize: 12, fontWeight: 600, lineHeight: 16, align: 'center' }
        }
      }
    },
    series: [{
      type: 'radar',
      data: [{
        value: values,
        name: '平均得分',
        areaStyle: { color: 'rgba(103, 194, 58, 0.2)' },
        lineStyle: { color: '#67C23A' },
        itemStyle: { color: '#67C23A' }
      }]
    }]
  })
}

const renderRadarChart = (dimensionScores) => {
  const chartDom = document.getElementById('radarChart')
  if (!chartDom) return

  const myChart = initChart(chartDom)

  if (!dimensionScores || Object.keys(dimensionScores).length === 0) {
    myChart.setOption({
      title: { text: '暂无维度评分数据', left: 'center', top: 'middle' }
    })
    return
  }

  // 过滤掉 average 为 null/undefined 的维度（无数据的维度）
  const validDimensions = Object.entries(dimensionScores)
    .filter(([name, data]) => data.average !== null && data.average !== undefined)

  if (validDimensions.length === 0) {
    myChart.setOption({
      title: { text: '暂无维度评分数据', left: 'center', top: 'middle' }
    })
    return
  }

  const indicators = validDimensions.map(([name, data]) => ({
    name,
    max: 10,
    score: data.average
  }))
  const scoreByName = new Map(validDimensions.map(([name, data]) => [name, data.average]))

  myChart.setOption({
    // 分值直显在指标名下方，不再用悬停 tooltip——雷达 tooltip 粒度是整个数据项
    // （悬停任意处都全量罗列所有指标），指标多时溢出截断
    tooltip: { show: false },
    radar: {
      indicator: indicators,
      shape: 'polygon',
      splitNumber: 5,
      axisName: {
        fontSize: 12,
        formatter: (name) => {
          const score = scoreByName.get(name)
          return score != null ? `{v|${name}}\n{s|${Number(score).toFixed(2)}}` : name
        },
        rich: {
          v: { color: '#666', fontSize: 12, lineHeight: 16, align: 'center' },
          s: { color: '#3662EC', fontSize: 12, fontWeight: 600, lineHeight: 16, align: 'center' }
        }
      }
    },
    series: [{
      type: 'radar',
      data: [{
        value: indicators.map(i => i.score),
        name: '平均分',
        areaStyle: { color: 'rgba(54, 98, 236, 0.2)' },
        lineStyle: { color: '#3662EC' },
        itemStyle: { color: '#3662EC' }
      }]
    }]
  })
}

const renderBarChart = (res) => {
  const chartDom = document.getElementById('barChart')
  if (!chartDom) return

  const myChart = initChart(chartDom)

  // 从items中提取评分
  const scores = res.items?.map(item => item.average_score).filter(s => s !== undefined) || []

  // 计算分布
  const buckets = { '0-2': 0, '2-4': 0, '4-6': 0, '6-8': 0, '8-10': 0 }
  scores.forEach(score => {
    if (score < 2) buckets['0-2']++
    else if (score < 4) buckets['2-4']++
    else if (score < 6) buckets['4-6']++
    else if (score < 8) buckets['6-8']++
    else buckets['8-10']++
  })

  myChart.setOption({
    tooltip: { trigger: 'axis', confine: true },
    xAxis: {
      type: 'category',
      data: Object.keys(buckets)
    },
    yAxis: {
      type: 'value'
    },
    series: [{
      type: 'bar',
      data: Object.values(buckets),
      itemStyle: {
        color: '#3662EC'
      }
    }]
  })
}

const renderPieChart = (res) => {
  const chartDom = document.getElementById('pieChart')
  if (!chartDom) return

  const myChart = initChart(chartDom)

  const successCount = res.success_count || 0
  const failedCount = res.failed_count || 0

  myChart.setOption({
    tooltip: { trigger: 'item', confine: true },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      data: [
        { value: successCount, name: '成功', itemStyle: { color: '#67C23A' } },
        { value: failedCount, name: '失败', itemStyle: { color: '#F56C6C' } }
      ],
      label: {
        formatter: '{b}: {c} ({d}%)'
      }
    }]
  })
}

const renderLineChart = (items) => {
  const chartDom = document.getElementById('lineChart')
  if (!chartDom) return

  const myChart = initChart(chartDom)

  // 只统计已评分行（合并进来的失败行 average_score 为 undefined，不参与趋势），
  // x 轴取数据自带 idx + 1（合并失败行后数组下标会与数据序号错位）；
  // idx 可信度按全量明细行判定（过滤后的子集可能掩盖重复 idx），脏数据回退位置序号
  const scoredItems = items.filter(item => item && item.average_score !== undefined)

  myChart.setOption({
    tooltip: { trigger: 'axis', confine: true },
    xAxis: {
      type: 'category',
      data: scoredItems.map((item, i) => detailSeqNo(item, i, seqNoFromIdx.value))
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 10
    },
    series: [{
      type: 'line',
      data: scoredItems.map(item => item.average_score),
      smooth: true,
      areaStyle: { color: 'rgba(54, 98, 236, 0.1)' },
      lineStyle: { color: '#3662EC' },
      itemStyle: { color: '#3662EC' }
    }]
  })
}

// ---------------- 多次执行聚合展示（通用评测类型专属） ----------------
// 数据来源：GET /evaluation/results/{id}/dimension-trend，按 execution_number 升序的
// {execution_number, dimension_scores} 快照数组；旧后端无该端点时 executionTrend 保持空数组

const TREND_SERIES_COLORS = ['#3662EC', '#67C23A', '#E6A23C', '#F56C6C', '#9254DE', '#00B2FF', '#FF7D00']
const TREND_MAX_DIMENSIONS = 10
const TREND_LEGEND_VISIBLE_COUNT = 5

// 维度名并集：按各执行版本中首次出现的顺序收集（稳定），最多保留 10 个
const collectTrendDimensions = (trend) => {
  const names = []
  const seen = new Set()
  trend.forEach(t => {
    Object.keys(t?.dimension_scores || {}).forEach(name => {
      if (!seen.has(name)) {
        seen.add(name)
        names.push(name)
      }
    })
  })
  return names.slice(0, TREND_MAX_DIMENSIONS)
}

// 取维度平均分：dimension_scores 形如 {维度名: {average, ...}}；
// 缺失/无值返回 null（图表断口，禁止补 0——补 0 会把缺失画成零分）
const getTrendDimensionAverage = (t, name) => {
  const stat = (t?.dimension_scores || {})[name]
  const avg = stat && typeof stat === 'object' ? stat.average : stat
  return avg === undefined || avg === null ? null : Number(avg)
}

// 跨执行波动表：每维度一行（均值/最优/最差/波动幅度），只按有值的执行统计
const trendVolatilityRows = computed(() => {
  const trend = executionTrend.value
  if (!Array.isArray(trend) || trend.length === 0) return []
  return collectTrendDimensions(trend).map(name => {
    const values = trend.map(t => getTrendDimensionAverage(t, name)).filter(v => v !== null)
    if (values.length === 0) return { name, avg: null, best: null, worst: null, range: null }
    const avg = values.reduce((acc, v) => acc + v, 0) / values.length
    const best = Math.max(...values)
    const worst = Math.min(...values)
    return { name, avg, best, worst, range: best - worst }
  })
})

const formatTrendNumber = (v) => (v === null || v === undefined || Number.isNaN(v)) ? '-' : Number(v).toFixed(2)

// 多次执行聚合图表：雷达（最近一次在前）+ 折线（按执行序号升序）
const renderTrendCharts = () => {
  const trend = executionTrend.value
  // 与聚合区模板 v-if 同条件：不足 2 次执行不渲染（防御：容器未挂载时直接跳过）
  if (!Array.isArray(trend) || trend.length < 2) return
  renderTrendRadarChart(trend)
  renderTrendLineChart(trend)
}

const renderTrendRadarChart = (trend) => {
  const chartDom = document.getElementById('trendRadarChart')
  if (!chartDom) return
  const myChart = initChart(chartDom)

  const dimensions = collectTrendDimensions(trend)
  if (dimensions.length === 0) {
    myChart.setOption({ title: { text: '暂无维度数据', left: 'center', top: 'middle' } })
    return
  }

  // 后端 dimension-trend 按 execution_number 倒序返回（最近一次在前），这里显式按
  // execution_number 升序整理后再倒序展示，不依赖传输顺序：最近一次执行排最前
  const ordered = trend
    .slice()
    .sort((a, b) => (a?.execution_number ?? 0) - (b?.execution_number ?? 0))
    .reverse()
  const seriesData = ordered.map((t, i) => ({
    name: `第${t?.execution_number ?? ordered.length - i}次`,
    value: dimensions.map(name => getTrendDimensionAverage(t, name)),
    lineStyle: { width: 2 },
    areaStyle: { opacity: 0.08 }
  }))

  // legend 默认只勾选最近 5 次，更早的执行默认不勾选（仍可手动打开）
  const legendSelected = {}
  ordered.forEach((t, i) => {
    legendSelected[`第${t?.execution_number ?? ordered.length - i}次`] = i < TREND_LEGEND_VISIBLE_COUNT
  })

  myChart.setOption({
    // confine: 雷达维度多时 tooltip 很高，悬停靠下的点会向上溢出图表容器，
    // 超出部分被卡片遮挡（只露出尾部几行）——限制在图表容器内重新定位
    tooltip: { trigger: 'item', confine: true },
    color: TREND_SERIES_COLORS,
    legend: { type: 'scroll', bottom: 0, selected: legendSelected },
    radar: {
      indicator: dimensions.map(name => ({ name, max: 10 })),
      shape: 'polygon',
      splitNumber: 5
    },
    series: [{ type: 'radar', data: seriesData }]
  })
}

const renderTrendLineChart = (trend) => {
  const chartDom = document.getElementById('trendLineChart')
  if (!chartDom) return
  const myChart = initChart(chartDom)

  const dimensions = collectTrendDimensions(trend)
  if (dimensions.length === 0) {
    myChart.setOption({ title: { text: '暂无维度数据', left: 'center', top: 'middle' } })
    return
  }

  // x 轴按执行序号升序
  const asc = trend.slice().sort((a, b) => (a?.execution_number ?? 0) - (b?.execution_number ?? 0))
  const series = dimensions.map(name => ({
    name,
    type: 'line',
    smooth: true,
    connectNulls: false,
    // 该执行版本缺失的维度值用 null 断开
    data: asc.map(t => getTrendDimensionAverage(t, name))
  }))

  myChart.setOption({
    tooltip: { trigger: 'axis', confine: true },
    color: TREND_SERIES_COLORS,
    legend: { type: 'scroll', bottom: 0 },
    grid: { left: 40, right: 20, top: 30, bottom: 50 },
    xAxis: { type: 'category', data: asc.map((t, i) => `第${t?.execution_number ?? i + 1}次`) },
    yAxis: { type: 'value', min: 0, max: 10 },
    series
  })
}

onMounted(() => {
  loadTasks()
})

onUnmounted(() => {
  stopAll() // 停止所有轮询
})

// 获取耗时表格行类名（用于高亮超过阈值的行）
const getLatencyRowClassName = ({ row }) => {
  const threshold = result.value?.result?.threshold || 10
  if (row.duration > threshold) {
    return 'latency-row-danger'
  }
  return ''
}

// 渲染耗时分布直方图
const renderLatencyHistogram = (res) => {
  const chartDom = document.getElementById('latencyHistogramChart')
  if (!chartDom) return

  const myChart = initChart(chartDom)
  const records = res.records || []
  const threshold = res.threshold || 10

  if (records.length === 0) {
    myChart.setOption({
      title: { text: '暂无耗时数据', left: 'center', top: 'middle' }
    })
    return
  }

  // 提取耗时数据并分组
  const durations = records.map(r => r.duration || 0)
  const minDuration = 0
  const maxDuration = Math.max(...durations, threshold)

  // 计算直方图分布：从0到maxDuration，threshold作为分界线
  const binCount = 10
  const binWidth = Math.max((maxDuration - minDuration) / binCount, 0.1)
  const histogramData = []

  // 创建bins: 从0开始，直到超过maxDuration
  for (let i = 0; i <= binCount; i++) {
    const binStart = minDuration + i * binWidth
    const binEnd = binStart + binWidth
    const count = durations.filter(d => d >= binStart && d < binEnd).length
    // 判断这个bin是否与threshold相交或在其右侧
    const isOverThreshold = binStart >= threshold
    histogramData.push({
      range: `${binStart.toFixed(2)}-${binEnd.toFixed(2)}s`,
      count: count,
      isOverThreshold: isOverThreshold
    })
  }

  // 找到threshold所在的bin索引，用于markLine定位
  const thresholdBinIndex = histogramData.findIndex(h => h.isOverThreshold)

  myChart.setOption({
    tooltip: {
      trigger: 'axis',
      confine: true,
      formatter: (params) => {
        const p = params[0]
        return `${p.name}<br/>请求数: ${p.value}`
      }
    },
    xAxis: {
      type: 'category',
      data: histogramData.map(h => h.range),
      axisLabel: { rotate: 45, fontSize: 10 }
    },
    yAxis: { type: 'value', name: '请求数' },
    series: [{
      type: 'bar',
      data: histogramData.map((h) => ({
        value: h.count,
        itemStyle: { color: h.isOverThreshold ? '#F56C6C' : '#67C23A' }
      })),
      markLine: {
        data: [{
          xAxis: thresholdBinIndex >= 0 ? thresholdBinIndex : 0,
          name: '阈值',
          lineStyle: { color: '#E6A23C', width: 2, type: 'dashed' },
          label: { formatter: `阈值: ${threshold}s`, position: 'end' }
        }]
      }
    }]
  })
}
</script>

<style scoped>
/* M5 QE5-4 分享弹窗 */
.share-tip {
  margin-left: 10px;
  font-size: 12px;
  color: #909399;
}
.share-url-alert :deep(.el-alert__content) {
  width: 100%;
}
.share-url-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.share-url-text {
  flex: 1;
  word-break: break-all;
  font-size: 13px;
}

.results-page {
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

.status-alert {
  margin-bottom: 16px;
}

/* M1 门禁横幅：通过绿/未通过红/错误橙，浅底色 + 左侧色条 */
.gate-banner {
  margin-bottom: 16px;
  padding: 14px 16px;
  border-radius: 8px;
  border: 1px solid var(--el-color-success-light-7);
  border-left: 4px solid var(--el-color-success);
  background: var(--el-color-success-light-9);
}

/* M2 QE2-4 裁判治理面板 */
.judge-governance-panel {
  margin-bottom: 16px;
  padding: 14px 16px;
  border-radius: 8px;
  border: 1px solid var(--el-border-color-light);
  background: var(--el-fill-color-extra-light);
}

.judge-governance-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.judge-governance-title {
  font-weight: 600;
  font-size: 14px;
}

.judge-governance-section {
  margin-top: 10px;
}

.judge-governance-section-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 8px;
}

.judge-governance-text {
  font-size: 12px;
  color: var(--el-text-color-regular);
  word-break: break-all;
  white-space: pre-wrap;
}

.gate-banner.gate-failed {
  border-color: var(--el-color-danger-light-7);
  border-left-color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}

.gate-banner.gate-error {
  border-color: var(--el-color-warning-light-7);
  border-left-color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
}

.gate-banner-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}

.gate-banner-head .gate-banner-icon {
  font-size: 18px;
}

.gate-banner.gate-passed .gate-banner-icon {
  color: var(--el-color-success);
}

.gate-banner.gate-failed .gate-banner-icon {
  color: var(--el-color-danger);
}

.gate-banner.gate-error .gate-banner-icon {
  color: var(--el-color-warning);
}

.gate-banner-title {
  font-size: 15px;
  font-weight: 600;
}

.gate-banner-mode,
.gate-banner-time {
  font-size: 12px;
  color: #909399;
}

.gate-checks-table {
  margin-top: 8px;
  background: transparent;
}

/* 未选任务时的占位区：min-height 保证加载遮罩 spinner 可见，不再是无反馈白屏 */
.result-empty-placeholder {
  min-height: 360px;
  display: flex;
  align-items: center;
  justify-content: center;
}


.stat-card {
  background: linear-gradient(135deg, #EEF2FF 0%, #E0E7FF 100%);
  border: 1px solid #C7D2FE;
  border-radius: 12px;
  padding: 20px;
  text-align: center;
}

.stat-card-small {
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 8px;
  padding: 15px;
  text-align: center;
}

/* 安全评测 deepeval 指标通道标签行（M3 QE3-2） */
.safety-metric-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

/* ---------------- 安全专视图扩展（M3 QE3-3：类目热力 + 证据引用） ---------------- */

.safety-heatmap-card {
  margin-top: 20px;
  padding: 16px;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  position: relative;
}

.safety-heatmap-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}

.safety-heatmap-sub {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
  margin-left: 8px;
}

.safety-heatmap-chart {
  width: 100%;
  height: 260px;
}

.safety-heatmap-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #909399;
  font-size: 13px;
}

.safety-evidence-panel {
  padding: 8px 12px 12px;
}

.evidence-cite {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.evidence-cite .cite-label {
  font-size: 12px;
  font-weight: 600;
  color: #606266;
  white-space: nowrap;
}

.evidence-cite code {
  font-size: 12px;
  color: #409eff;
  background: #ecf5ff;
  border-radius: 4px;
  padding: 2px 8px;
  word-break: break-all;
}

.evidence-block {
  margin-bottom: 12px;
}

.evidence-block-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 6px;
}

.evidence-list {
  margin: 0;
  padding-left: 4px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.evidence-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.evidence-text {
  font-size: 12px;
  color: #f56c6c;
  background: #fef0f0;
  border-radius: 4px;
  padding: 2px 6px;
}

.evidence-note {
  font-size: 12px;
  color: #909399;
}

.evidence-text-block {
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}

.evidence-quote {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
  border-left: 3px solid #dcdfe6;
  padding-left: 10px;
  line-height: 1.6;
}

.evidence-metric-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
}

.evidence-metric-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.evidence-empty {
  font-size: 12px;
  color: #c0c4cc;
}

.stats-row {
  margin-bottom: 20px;
}

/* 失败数量卡可点击态（有失败行时才加此类，hover 反馈暗示可点） */
.stat-card-clickable {
  cursor: pointer;
  transition: box-shadow 0.2s, border-color 0.2s;
}

.stat-card-clickable:hover {
  box-shadow: 0 2px 12px rgba(245, 108, 108, 0.25);
  border-color: #F56C6C;
}

.stat-label {
  font-size: 14px;
  color: #6B7280;
  margin-bottom: 10px;
}

.stat-value {
  font-size: 24px;
  font-weight: 600;
  color: #1F2937;
}

/* pass^k 卡副文案（M2 QE2-3）：主数值下的口径说明行 */
.stat-sub {
  margin-top: 6px;
  font-size: 12px;
  color: #9CA3AF;
}

/* 多 Trial 行徽标副文案（M2 QE2-3）：pass@1 与标准误 */
.trials-sub {
  margin-top: 4px;
  font-size: 11px;
  color: #9CA3AF;
}

.stat-value.success {
  color: #67C23A;
}

.stat-value.danger {
  color: #F56C6C;
}

.stat-value.highlight {
  color: #3662EC;
}

.result-detail {
  margin-top: 20px;
}

.result-detail h3 {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
  margin-bottom: 16px;
}

.distribution-item {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.distribution-item span:first-child {
  width: 80px;
  color: #374151;
}

.distribution-item span:last-child {
  width: 50px;
  text-align: right;
  color: #6B7280;
}

/* RAG 指标卡片 */
.rag-metric-card {
  text-align: center;
  padding: 4px 0;
}

.rag-metric-card.clickable {
  cursor: pointer;
  transition: transform 0.2s, box-shadow 0.2s;
}

.rag-metric-card.clickable:hover {
  transform: translateY(-2px);
}

.metric-click-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  margin-top: 8px;
  font-size: 11px;
  color: #9CA3AF;
}

.rag-metric-card :deep(.el-card__body) {
  padding: 12px 8px;
}

.rag-metric-name {
  font-size: 12px;
  color: #6B7280;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.rag-metric-score {
  font-size: 24px;
  font-weight: 700;
  color: #1F2937;
  line-height: 1.2;
}

.rag-metric-range {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #9CA3AF;
  margin-top: 6px;
}

/* 表格中指标单元格 */
.metric-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}

.metric-score {
  font-weight: 600;
}

.reason-hint {
  color: #909399;
  cursor: help;
  margin-left: 2px;
}

.reason-hint:hover {
  color: #3662EC;
}

.no-reason {
  color: #C0C4CC;
  font-size: 12px;
}

/* 指标说明弹窗 */
.metric-detail {
  padding: 10px 0;
}

.metric-section {
  margin-bottom: 20px;
}

.metric-section h4 {
  font-size: 14px;
  font-weight: 600;
  color: #1F2937;
  margin-bottom: 8px;
}

.metric-section p {
  font-size: 14px;
  line-height: 1.8;
  color: #4B5563;
}

/* 评分理由弹窗 */
.reason-content {
  padding: 10px 0;
}

.reason-question {
  margin-bottom: 20px;
  padding: 12px;
  background: #F9FAFB;
  border-radius: 6px;
}

.reason-question-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.reason-question-header h4 {
  font-size: 14px;
  font-weight: 600;
  color: #1F2937;
  margin: 0;
}

.reason-question-text {
  font-size: 14px;
  line-height: 1.7;
  color: #4B5563;
  word-break: break-word;
  white-space: pre-wrap;
  margin: 0;
}

.reason-question-text.collapsed {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.reason-list h4 {
  font-size: 14px;
  font-weight: 600;
  color: #1F2937;
  margin-bottom: 12px;
}

.reason-item {
  padding: 12px;
  background: #F9FAFB;
  border-radius: 6px;
  margin-bottom: 10px;
}

.reason-metric {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.metric-name {
  font-size: 14px;
  font-weight: 500;
  color: #1F2937;
}

.reason-text {
  font-size: 13px;
  line-height: 1.6;
  color: #4B5563;
  padding-left: 4px;
  border-left: 3px solid #E5E7EB;
}

.metric-value {
  font-size: 32px;
  font-weight: 600;
  text-align: center;
  padding: 20px 0;
  color: #3662EC;
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

:deep(.el-divider) {
  margin: 24px 0;
}

/* 多裁判横向汇总样式 */
.judge-scores-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  max-width: 100%;
}

.judge-score-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  background: #F3F4F6;
  border: 1px solid #E5E7EB;
  border-radius: 4px;
  font-size: 12px;
  color: #374151;
  line-height: 1.4;
}

.judge-score-pill-clickable {
  cursor: pointer;
  color: #3662EC;
  border-color: #C7D2FE;
  background: #EEF2FF;
}

.judge-score-pill-clickable:hover {
  background: #E0E7FF;
  border-color: #A5B4FC;
}

/* 耗时表格超过阈值的行高亮 */
:deep(.latency-row-danger) {
  background-color: #FEF0F0 !important;
}
:deep(.latency-row-danger:hover > td) {
  background-color: #FDE2E2 !important;
}
.text-danger {
  color: #F56C6C;
  font-weight: 600;
}

/* 脚本产物（评测前处理脚本） */
.pre-scripts-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.pre-scripts-toolbar h3 {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
  margin: 0;
}

.pre-scripts-hint {
  font-size: 12px;
  color: #9CA3AF;
  margin-bottom: 12px;
}

.pre-script-card {
  margin-bottom: 16px;
}

/* M2 轨迹匹配证据区 */
.trajectory-entry {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 3px 0;
  font-size: 13px;
  color: #606266;
}
.trajectory-entry-missing {
  color: #303133;
}
.trajectory-entry-args {
  color: #909399;
  font-size: 12px;
}
.trajectory-timeline-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.trajectory-call-index {
  color: #909399;
  font-size: 12px;
  min-width: 34px;
}
.trajectory-call-args {
  color: #606266;
  font-size: 12px;
  font-family: monospace;
  word-break: break-all;
}

.pre-script-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.pre-script-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.pre-script-name {
  font-size: 14px;
  font-weight: 600;
  color: #1F2937;
}

.pre-script-duration {
  font-size: 12px;
  color: #6B7280;
}

.pre-script-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.pre-script-summary {
  font-size: 13px;
  color: #4B5563;
  line-height: 1.6;
}

.pre-script-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.pre-script-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
}

.pre-script-chip-label {
  color: #6B7280;
}

.pre-script-chip-value {
  color: #1F2937;
  font-weight: 600;
}

.pre-case-name {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
}

/* per_case 比对行高亮：一致绿 / 不一致红 / 缺失灰 */
:deep(.pre-case-row-consistent) {
  background-color: #F0F9EB !important;
}
:deep(.pre-case-row-consistent:hover > td) {
  background-color: #E1F3D8 !important;
}
:deep(.pre-case-row-mismatch) {
  background-color: #FEF0F0 !important;
}
:deep(.pre-case-row-mismatch:hover > td) {
  background-color: #FDE2E2 !important;
}
:deep(.pre-case-row-missing) {
  background-color: #F4F4F5 !important;
}
:deep(.pre-case-row-missing:hover > td) {
  background-color: #EBEEF5 !important;
}

/* db_assert 断言比对行高亮：通过绿 / 失败红 */
:deep(.pre-assert-row-pass) {
  background-color: #F0F9EB !important;
}
:deep(.pre-assert-row-pass:hover > td) {
  background-color: #E1F3D8 !important;
}
:deep(.pre-assert-row-fail) {
  background-color: #FEF0F0 !important;
}
:deep(.pre-assert-row-fail:hover > td) {
  background-color: #FDE2E2 !important;
}

/* log_grep 命中样例等宽字体块 */
.pre-log-sample-wrap {
  margin-top: 12px;
}

.pre-log-sample-title {
  font-size: 12px;
  color: #6B7280;
  margin-bottom: 6px;
}

.pre-log-sample {
  margin: 0;
  padding: 12px 14px;
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 12px;
  line-height: 1.7;
  color: #374151;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow-y: auto;
}

.pre-log-sample::-webkit-scrollbar {
  width: 6px;
}

.pre-log-sample::-webkit-scrollbar-thumb {
  background: #D1D5DB;
  border-radius: 3px;
}

/* 结果观察（evaluation/result 扩展点）只读列表 */
.observations-hint {
  font-size: 12px;
  color: #9CA3AF;
  margin-bottom: 12px;
}

.observation-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 10px 12px;
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  margin-bottom: 8px;
  font-size: 13px;
  line-height: 1.6;
}

.observation-level {
  flex-shrink: 0;
}

.observation-title {
  font-weight: 600;
  color: #1F2937;
  white-space: nowrap;
}

.observation-text {
  color: #4B5563;
  word-break: break-word;
}

.observation-text.clickable {
  cursor: pointer;
}

.observation-text.clickable:hover {
  color: #3662EC;
}

/* 耗时分布阈值参考卡片 */
.threshold-info {
  padding: 8px 0;
}
.threshold-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid #F3F4F6;
}
.threshold-item:last-child {
  border-bottom: none;
}
.threshold-label {
  color: #6B7280;
  font-size: 14px;
}
.threshold-value {
  color: #1F2937;
  font-size: 14px;
  font-weight: 500;
}
.threshold-item.success .threshold-value {
  color: #67C23A;
}
.threshold-item.danger .threshold-value {
  color: #F56C6C;
}

/* 单元格预览样式 */
.cell-preview {
  cursor: pointer;
  color: #374151;
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color 0.2s;
}

.cell-preview:hover {
  color: #3662EC;
}

/* 失败状态 tag（执行失败 / 裁判失败，可点击打开失败详情弹窗） */
.failure-tag-clickable {
  cursor: pointer;
}

/* 二分分类 - 指标卡片 */
.binary-metric-card {
  background: #fff;
  border: 1px solid #E5E7EB;
  border-radius: 8px;
  padding: 20px;
  text-align: center;
}

.binary-metric-name {
  font-size: 14px;
  color: #606266;
  margin-bottom: 8px;
}

.binary-metric-rate {
  font-size: 28px;
  font-weight: 700;
  color: #1F2937;
}

/* 二分分类 - 混淆矩阵 */
.confusion-matrix {
  display: flex;
  justify-content: center;
  margin: 16px 0;
}

.cm-table {
  border-collapse: collapse;
  text-align: center;
}

.cm-table th,
.cm-table td {
  padding: 16px 24px;
  border: 1px solid #E5E7EB;
  font-size: 14px;
}

.cm-table thead th {
  background: #F3F4F6;
  font-weight: 600;
  color: #374151;
}

.cm-table tbody th {
  background: #F3F4F6;
  font-weight: 600;
  color: #374151;
}

.cm-cell.tp {
  background: #D1FAE5;
  color: #065F46;
  font-weight: 600;
}

.cm-cell.tn {
  background: #D1FAE5;
  color: #065F46;
  font-weight: 600;
}

.cm-cell.fp {
  background: #FEE2E2;
  color: #991B1B;
  font-weight: 600;
}

.cm-cell.fn {
  background: #FEE2E2;
  color: #991B1B;
  font-weight: 600;
}

/* 二分分类 - 展开行 */
.expand-section {
  padding: 8px 16px;
}

.expand-section-title {
  font-size: 12px;
  font-weight: 600;
  color: #606266;
  margin-bottom: 8px;
}

.expand-metrics {
  margin-bottom: 12px;
}

.expand-metrics-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.expand-field-values {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.expand-field-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  padding: 6px 12px;
  max-width: 100%;
}

.expand-field-name {
  font-size: 12px;
  font-weight: 600;
  color: #3662EC;
  white-space: nowrap;
}

.expand-field-value {
  font-size: 12px;
  color: #374151;
  cursor: pointer;
  word-break: break-all;
}

.expand-field-value:hover {
  color: #3662EC;
}

/* 安全评测样式 */
.safety-metric-card {
  text-align: center;
  padding: 20px 10px;
  transition: transform 0.2s, box-shadow 0.2s;
}

.safety-metric-card:hover {
  transform: translateY(-2px);
}

.safety-metric-icon {
  margin-bottom: 12px;
}

.safety-metric-name {
  font-size: 14px;
  color: #606266;
  margin-bottom: 8px;
  font-weight: 500;
}

.safety-metric-value {
  margin-bottom: 8px;
}

.safety-metric-score {
  font-size: 20px;
  font-weight: 700;
  color: #1F2937;
}
</style>

<!-- dialog 样式不加 scoped，确保弹窗全局生效 -->
<style>
.detail-dialog .el-dialog__header {
  border-bottom: 1px solid #E5E7EB;
  padding: 16px 20px;
  margin: 0;
}

.detail-dialog .el-dialog__title {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
}

.detail-dialog .el-dialog__body {
  padding: 0;
}

.detail-dialog .el-dialog__footer {
  border-top: 1px solid #E5E7EB;
  padding: 12px 20px;
}

.detail-content {
  padding: 20px 24px;
  max-height: 60vh;
  overflow-y: auto;
  font-size: 14px;
  line-height: 1.8;
  color: #374151;
  word-break: break-word;
  white-space: pre-wrap;
}

/* 内容区滚动条美化 */
.detail-content::-webkit-scrollbar {
  width: 6px;
}

.detail-content::-webkit-scrollbar-thumb {
  background: #D1D5DB;
  border-radius: 3px;
}

.detail-content::-webkit-scrollbar-thumb:hover {
  background: #9CA3AF;
}

/* 失败详情弹窗 */
.failure-dialog .el-dialog__header {
  border-bottom: 1px solid #E5E7EB;
  padding: 16px 20px;
  margin: 0;
}

.failure-dialog .el-dialog__title {
  font-size: 16px;
  font-weight: 600;
  color: #1F2937;
}

.failure-dialog .el-dialog__footer {
  border-top: 1px solid #E5E7EB;
  padding: 12px 20px;
}

.failure-body {
  padding: 8px 0;
}

.failure-section {
  margin-bottom: 16px;
}

.failure-section:last-child {
  margin-bottom: 0;
}

.failure-section-title {
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
  font-size: 14px;
}

.failure-error-box {
  background: #f5f7fa;
  padding: 12px;
  border-radius: 6px;
  font-size: 13px;
  color: #374151;
}

.failure-error-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
}

/* 失败原因汇总弹窗 */
.failure-summary-hint {
  margin: 0 0 12px;
  color: #909399;
  font-size: 13px;
  line-height: 1.6;
}

.failure-summary-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.failure-summary-count {
  flex-shrink: 0;
  margin-top: 2px;
}

.failure-summary-reason {
  flex: 1;
  max-height: 120px;
  overflow-y: auto;
}

.failure-summary-rows {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  padding-left: 4px;
}

.failure-judge-name {
  color: #909399;
  font-size: 13px;
}

.failure-input-row {
  display: flex;
  gap: 4px;
  padding: 6px 0;
  border-bottom: 1px dashed #ebeef5;
}

.failure-input-row:last-child {
  border-bottom: none;
}

.failure-input-label {
  color: #909399;
  font-size: 13px;
  flex-shrink: 0;
}

.failure-model-output {
  cursor: pointer;
}

.failure-model-output:hover {
  color: #3662EC;
}

/* 裁判详情弹窗：分维度理由 tooltip（popper 挂在 body 上，样式必须全局才生效） */
.dimension-feedback-tooltip {
  max-width: 480px;
  line-height: 1.6;
}

/* 插件评测结果 */
.badge-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  padding: 4px 0;
}

.badge-summary-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.badge-count {
  font-weight: 600;
  color: #606266;
}

.plugin-expand-detail {
  padding: 8px 16px 8px 8px;
}

.plugin-expand-detail h5 {
  margin: 10px 0 6px;
  color: #303133;
}

.plugin-sub-scores {
  font-size: 12px;
  word-break: break-all;
}

/* trace 口径：指标行与口径说明（报告正文的网页版） */
.trace-metric-line {
  font-size: 13px;
  line-height: 1.9;
  color: #303133;
}

.trace-note {
  color: #909399;
  font-size: 12px;
  line-height: 1.7;
}

.trace-line-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin: 4px 0 10px;
  padding-left: 8px;
  border-left: 3px solid #409eff;
}
</style>
