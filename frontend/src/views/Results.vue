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
                  <el-tag size="small" :type="getStatusTagType(v.status)">{{ v.status }}</el-tag>
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
          </div>
        </div>
      </template>

      <div v-if="result">
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

        <!-- 详细结果 -->
        <div v-if="isResultRenderable(result)" class="result-detail">
          <el-divider />
          <h3>评测详情</h3>

          <!-- 统计卡片 - 一致性评测、耗时评测、二分分类使用不同的指标，不显示此卡片 -->
          <el-row :gutter="20" class="stats-row" v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety'">
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
              <div class="stat-card-small">
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

          <!-- 图表区域 - 一致性评测、耗时评测、二分分类没有评分相关指标，不显示图表 -->
          <el-row :gutter="20" v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety'">
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

          <el-row :gutter="20" style="margin-top: 20px;" v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety'">
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
                  :label="`第 ${(entry.item.idx ?? i) + 1} 条数据`"
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

          <!-- 安全评测结果 -->
          <div v-if="result.evaluation_type === 'safety'">
            <!-- 安全告警提示 -->
            <el-alert
              v-if="hasSafetyAlerts"
              :title="safetyAlertTitle"
              :type="safetyAlertType"
              :closable="false"
              show-icon
              style="margin-bottom: 20px;"
            >
              <template #default>
                <div>检测到 {{ safetyAlertCount }} 个安全问题，请仔细审查</div>
              </template>
            </el-alert>

            <!-- 安全评测指标卡片 -->
            <el-row :gutter="20">
              <el-col :span="6">
                <el-card shadow="hover" class="safety-metric-card">
                  <div class="safety-metric-icon">
                    <el-icon :size="32" color="#e6a23c"><Warning /></el-icon>
                  </div>
                  <div class="safety-metric-name">偏见检测</div>
                  <div class="safety-metric-value">
                    <el-tag :type="getSafetyTagType(result.result.bias_score)" size="large">
                      {{ getSafetyLabel(result.result.bias_score) }}
                    </el-tag>
                  </div>
                  <div class="safety-metric-score">
                    {{ result.result.bias_score != null ? (result.result.bias_score * 100).toFixed(1) + '%' : '-' }}
                  </div>
                </el-card>
              </el-col>
              <el-col :span="6">
                <el-card shadow="hover" class="safety-metric-card">
                  <div class="safety-metric-icon">
                    <el-icon :size="32" color="#f56c6c"><CircleClose /></el-icon>
                  </div>
                  <div class="safety-metric-name">毒性检测</div>
                  <div class="safety-metric-value">
                    <el-tag :type="getSafetyTagType(result.result.toxicity_score)" size="large">
                      {{ getSafetyLabel(result.result.toxicity_score) }}
                    </el-tag>
                  </div>
                  <div class="safety-metric-score">
                    {{ result.result.toxicity_score != null ? (result.result.toxicity_score * 100).toFixed(1) + '%' : '-' }}
                  </div>
                </el-card>
              </el-col>
              <el-col :span="6">
                <el-card shadow="hover" class="safety-metric-card">
                  <div class="safety-metric-icon">
                    <el-icon :size="32" color="#409eff"><User /></el-icon>
                  </div>
                  <div class="safety-metric-name">个人信息泄露</div>
                  <div class="safety-metric-value">
                    <el-tag :type="getSafetyTagType(result.result.pii_score)" size="large">
                      {{ getSafetyLabel(result.result.pii_score) }}
                    </el-tag>
                  </div>
                  <div class="safety-metric-score">
                    {{ result.result.pii_score != null ? (result.result.pii_score * 100).toFixed(1) + '%' : '-' }}
                  </div>
                </el-card>
              </el-col>
              <el-col :span="6">
                <el-card shadow="hover" class="safety-metric-card">
                  <div class="safety-metric-icon">
                    <el-icon :size="32" color="#67c23a"><CircleCheck /></el-icon>
                  </div>
                  <div class="safety-metric-name">综合安全分</div>
                  <div class="safety-metric-value">
                    <el-tag :type="getSafetyTagType(result.result.overall_safety_score)" size="large">
                      {{ getSafetyLabel(result.result.overall_safety_score) }}
                    </el-tag>
                  </div>
                  <div class="safety-metric-score">
                    {{ result.result.overall_safety_score != null ? (result.result.overall_safety_score * 100).toFixed(1) + '%' : '-' }}
                  </div>
                </el-card>
              </el-col>
            </el-row>

            <!-- 安全评测概览 -->
            <el-row :gutter="20" style="margin-top: 20px;" v-if="result.result.total_questions">
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">总检测数</div>
                  <div class="stat-value">{{ result.result.total_questions ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">安全通过</div>
                  <div class="stat-value success">{{ result.result.passed_count ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">风险检出</div>
                  <div class="stat-value danger">{{ result.result.flagged_count ?? '-' }}</div>
                </div>
              </el-col>
              <el-col :span="6">
                <div class="stat-card-small">
                  <div class="stat-label">通过率</div>
                  <div class="stat-value highlight">{{ safePercent(result.result.pass_rate) }}%</div>
                </div>
              </el-col>
            </el-row>

            <!-- 安全评测逐条明细 -->
            <el-divider />
            <h3>安全评测明细</h3>
            <el-table
              :data="result.result.items || []"
              style="width: 100%"
              max-height="450"
              stripe
            >
              <el-table-column label="序号" width="60">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column label="输入内容" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('输入内容', row.user_input || row.input)">
                    {{ truncateText(row.user_input || row.input, 60) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="模型输出" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('模型输出', row.model_output)">
                    {{ truncateText(row.model_output, 60) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="偏见检测" width="100">
                <template #default="{ row }">
                  <el-tag :type="getSafetyItemTagType(row.bias_flagged)" size="small">
                    {{ row.bias_flagged ? '检出' : '通过' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="毒性检测" width="100">
                <template #default="{ row }">
                  <el-tag :type="getSafetyItemTagType(row.toxicity_flagged)" size="small">
                    {{ row.toxicity_flagged ? '检出' : '通过' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="PII 泄露" width="100">
                <template #default="{ row }">
                  <el-tag :type="getSafetyItemTagType(row.pii_flagged)" size="small">
                    {{ row.pii_flagged ? '检出' : '通过' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="综合状态" width="100">
                <template #default="{ row }">
                  <el-tag :type="row.flagged ? 'danger' : 'success'" size="small">
                    {{ row.flagged ? '风险' : '安全' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="评分" width="100">
                <template #default="{ row }">
                  <el-tag v-if="row.score !== undefined" :type="getSafetyItemTagType(row.score < 0.5)">
                    {{ row.score.toFixed(2) }}
                  </el-tag>
                  <span v-else>-</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 详细结果表格 - 仅对 judge / agent / success_rate / dialogue 类型显示 -->
          <template v-if="result.evaluation_type !== 'rag' && result.evaluation_type !== 'consistency' && result.evaluation_type !== 'latency' && result.evaluation_type !== 'binary_classification' && result.evaluation_type !== 'safety'">
            <el-divider />
            <h3>详细结果</h3>
            <el-table :data="resultItems" style="width: 100%" max-height="400">
              <el-table-column label="序号" width="60">
                <template #default="{ $index }">{{ $index + 1 }}</template>
              </el-table-column>
              <el-table-column prop="average_score" label="评分" width="100">
                <template #default="{ row }">
                  <el-tag v-if="row.average_score !== undefined" :type="getScoreType(row.average_score)">
                    {{ row.average_score.toFixed(2) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="judge_count" label="裁判数" width="80" />
              <!-- 会话摘要 / 会话来源：仅长会话 Agent 评测显示 -->
              <el-table-column v-if="result.evaluation_type === 'agent'" label="会话摘要" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('会话摘要', row.session_summary || row.user_input)">
                    {{ truncateText(row.session_summary || row.user_input, 80) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column v-if="result.evaluation_type === 'agent'" label="会话来源" width="120">
                <template #default="{ row }">
                  <el-tag v-if="formatSessionSource(row.session_source)" size="small">{{ formatSessionSource(row.session_source) }}</el-tag>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column prop="model_output" label="模型输出" min-width="200">
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('模型输出', row.model_output)">
                    {{ truncateText(row.model_output, 80) }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="feedback" label="反馈" min-width="200">
                <template #default="{ row }">
                  <!-- judge_scores 折叠面板 - 多裁判横向汇总，点击后垂直展开详情 -->
                  <div v-if="row.judge_scores && row.judge_scores.length > 0" class="judge-scores-summary">
                    <el-tag size="small" style="margin-right: 4px;">{{ row.judge_count }}位裁判</el-tag>
                    <span
                      v-for="(js, idx) in row.judge_scores"
                      :key="idx"
                      class="judge-score-pill"
                      :class="{ 'judge-score-pill-clickable': js.feedback || (js.dimension_scores && Object.keys(js.dimension_scores).length > 0) || js.langfuse_trace_url }"
                      @click.stop="(js.feedback || (js.dimension_scores && Object.keys(js.dimension_scores).length > 0) || js.langfuse_trace_url) && openJudgeDetail(row, idx)"
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
              <el-table-column prop="model_raw_response" label="原始响应" min-width="150">
                <template #default="{ row }">
                  <div class="cell-preview" @click="openDetail('原始响应', row.model_raw_response)">
                    {{ truncateText(row.model_raw_response, 60) }}
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </div>
      </div>

      <el-empty v-else description="请选择一个已完成的任务查看结果" />
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

        <!-- 各维度评分 -->
        <div v-if="judgeDetailDialog.dimensionScores && Object.keys(judgeDetailDialog.dimensionScores).length > 0" style="margin-top: 16px;">
          <p style="font-weight: 600; color: #303133; margin-bottom: 8px;">各维度评分</p>
          <div
            v-for="(score, name) in judgeDetailDialog.dimensionScores"
            :key="name"
            style="padding: 4px 0; font-size: 13px;"
          >
            <span style="color: #606266;">{{ name }}：</span>
            <el-tag size="small">{{ score }}</el-tag>
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
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { InfoFilled, ArrowDown, Timer, CircleCheck, Link, Warning, CircleClose, User } from '@element-plus/icons-vue'
import { taskApi, resultApi } from '@/api'
import { useTaskPolling } from '@/composables/useTaskPolling'
import { formatTime } from '@/utils/time'
import * as echarts from 'echarts'

const tasks = ref([])
const allTasks = ref([])
const selectedTaskId = ref(null)
const result = ref(null)
const resultItems = ref([])

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
  traceUrl: null,
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

// 打开裁判详情弹窗（含 LangFuse Trace 链接）
const openJudgeDetail = (row, judgeIdx) => {
  const js = row.judge_scores[judgeIdx]
  if (!js) return
  judgeDetailDialog.judgeIndex = js.judge_index !== undefined ? js.judge_index + 1 : judgeIdx + 1
  judgeDetailDialog.score = js.score !== undefined ? js.score : null
  judgeDetailDialog.feedback = js.feedback || ''
  judgeDetailDialog.dimensionScores = js.dimension_scores || null
  judgeDetailDialog.traceUrl = js.langfuse_trace_url || null
  judgeDetailDialog.visible = true
}

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

// 安全地计算百分比，防止 null * 100 崩溃
const safePercent = (val) => {
  if (val == null || isNaN(Number(val))) return '-'
  return (Number(val) * 100).toFixed(1)
}

// 安全获取 result.items，防止字段不存在时崩溃
const safeGetItems = (resultData) => {
  if (!resultData) return []
  if (resultData.items && Array.isArray(resultData.items)) return resultData.items
  if (resultData.results && Array.isArray(resultData.results)) return resultData.results
  if (resultData.detail_results && Array.isArray(resultData.detail_results)) return resultData.detail_results
  return []
}

// 判断结果是否可渲染图表（异常兼容：failed 不渲染，stopped 有部分结果可渲染）
const isResultRenderable = (r) => {
  if (!r || !r.result) return false
  if (r.status === 'failed') return false
  return true
}

// 会话来源展示文案（长会话 Agent 评测，兜底直接展示原始值）
const formatSessionSource = (source) => {
  if (!source) return ''
  if (source === 'claude_code') return 'Claude Code'
  if (source === 'langfuse') return 'Langfuse'
  return source
}

// 安全评测辅助函数
const hasSafetyAlerts = computed(() => {
  if (!result.value?.result || result.value.evaluation_type !== 'safety') return false
  return (result.value.result.flagged_count || 0) > 0
})

const safetyAlertTitle = computed(() => {
  if (!result.value?.result) return ''
  const count = result.value.result.flagged_count || 0
  return `安全风险告警：发现 ${count} 条风险内容`
})

const safetyAlertType = computed(() => {
  if (!result.value?.result) return 'warning'
  const count = result.value.result.flagged_count || 0
  if (count > 10) return 'error'
  if (count > 0) return 'warning'
  return 'success'
})

const safetyAlertCount = computed(() => {
  return result.value?.result?.flagged_count || 0
})

const getSafetyTagType = (score) => {
  if (score == null) return 'info'
  // 安全评分：分数越高越安全
  if (score >= 0.8) return 'success'
  if (score >= 0.6) return ''
  if (score >= 0.4) return 'warning'
  return 'danger'
}

const getSafetyLabel = (score) => {
  if (score == null) return '-'
  if (score >= 0.8) return '安全'
  if (score >= 0.6) return '良好'
  if (score >= 0.4) return '注意'
  return '风险'
}

const getSafetyItemTagType = (flagged) => {
  return flagged ? 'danger' : 'success'
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
        ElMessage.success(`任务 "${updatedTask.name}" 已${updatedTask.status === 'completed' ? '完成' : '结束'}`)
        // 刷新历史版本
        resultApi.history(taskId, { limit: 50 }).then(versions => {
          executionVersions.value = Array.isArray(versions) ? versions : []
        })
      }
    }
  },
  onTaskComplete: (taskId, task) => {
    if (taskId === selectedTaskId.value) {
      ElMessage.success(`任务 "${task.name}" 已完成`)
    }
  },
  onTaskFailed: (taskId, task) => {
    if (taskId === selectedTaskId.value) {
      ElMessage.error(`任务 "${task.name}" 执行失败`)
    }
  }
})

// 选择任务后加载历史版本列表
const onTaskChange = async (taskId) => {
  result.value = null
  resultItems.value = []
  executionVersions.value = []
  selectedExecutionNumber.value = null

  if (!taskId) return

  // 查找选中的任务
  const selectedTask = allTasks.value.find(t => t.id === taskId)

  // 如果任务是 running 状态，开始轮询
  if (selectedTask?.status === 'running') {
    startPolling(taskId)
  }

  try {
    const versions = await resultApi.history(taskId, { limit: 50 })
    executionVersions.value = Array.isArray(versions) ? versions : []

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
  }
}

// 按版本号加载结果
const loadResultByVersion = async (executionNumber) => {
  const version = executionVersions.value.find(v => v.execution_number === executionNumber)
  if (!version) return

  const currentTask = allTasks.value.find(t => t.id === selectedTaskId.value)
  result.value = {
    task_id: selectedTaskId.value,
    task_name: currentTask?.name || '',
    evaluation_type: currentTask?.evaluation_type || '',
    status: version.status,
    result: version.result,
    total_cost: version.total_cost,
    completed_at: version.completed_at,
    error_message: version.error_message,
  }

  resultItems.value = safeGetItems(version.result)
  // 重置脚本产物的数据条目选择，避免跨任务/版本残留旧选中项
  preScriptSelectedIdx.value = 0
  await nextTick()
  if (isResultRenderable(result.value)) {
    renderCharts()
  }
}

const loadResult = async () => {
  if (!selectedTaskId.value) return

  try {
    const res = await resultApi.get(selectedTaskId.value)
    result.value = res
    resultItems.value = safeGetItems(res.result)
    // 重置脚本产物的数据条目选择，避免跨任务残留旧选中项
    preScriptSelectedIdx.value = 0
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

  // 渲染雷达图 - 各维度评分
  renderRadarChart(res.dimension_scores)

  // 渲染柱状图 - 评分分布
  renderBarChart(res)

  // 渲染饼图 - 成功率
  renderPieChart(res)

  // 渲染折线图 - 评分趋势
  renderLineChart(resultItems.value)
}

const renderRagRadarChart = (metricsSummary) => {
  const chartDom = document.getElementById('ragRadarChart')
  if (!chartDom) return

  const myChart = echarts.init(chartDom)

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

  myChart.setOption({
    tooltip: {},
    radar: {
      indicator: indicators,
      shape: 'polygon',
      splitNumber: 5,
      axisName: { color: '#666' }
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

  const myChart = echarts.init(chartDom)

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

  myChart.setOption({
    tooltip: {},
    radar: {
      indicator: indicators,
      shape: 'polygon',
      splitNumber: 5,
      axisName: { color: '#666' }
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

  const myChart = echarts.init(chartDom)

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
    tooltip: { trigger: 'axis' },
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

  const myChart = echarts.init(chartDom)

  const successCount = res.success_count || 0
  const failedCount = res.failed_count || 0

  myChart.setOption({
    tooltip: { trigger: 'item' },
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

  const myChart = echarts.init(chartDom)

  const scores = items.map(item => item.average_score).filter(s => s !== undefined)

  myChart.setOption({
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: scores.map((_, i) => i + 1)
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 10
    },
    series: [{
      type: 'line',
      data: scores,
      smooth: true,
      areaStyle: { color: 'rgba(54, 98, 236, 0.1)' },
      lineStyle: { color: '#3662EC' },
      itemStyle: { color: '#3662EC' }
    }]
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

  const myChart = echarts.init(chartDom)
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

.stats-row {
  margin-bottom: 20px;
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
</style>
