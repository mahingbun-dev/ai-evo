<template>
  <div class="simulation-page">
    <!-- ============ 顶部：场景列表 ============ -->
    <el-card shadow="never">
      <template #header>
        <div class="page-header">
          <div>
            <span class="page-title">仿真评测</span>
            <span class="page-sub">
              用户模拟器（persona）与被测 Agent 多轮对话，环境重置 + 故障注入，
              终态校验出 pass^k（第 9 种评测类型）
            </span>
          </div>
          <div>
            <el-button @click="loadScenarios" :loading="listLoading">刷新</el-button>
            <el-button type="primary" @click="openCreate">新建场景</el-button>
          </div>
        </div>
      </template>

      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
                title="仿真场景必须至少声明一个终态校验器（无终态校验的仿真不允许发布——强制 end-state verification 纪律）" />

      <el-table :data="scenarios" v-loading="listLoading" size="default">
        <el-table-column prop="name" label="场景名称" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="scenario-name">{{ row.name }}</span>
            <el-tag size="small" type="info" style="margin-left: 6px">v{{ row.version }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="被测 Agent" min-width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="row.config?.agent?.exec_type === 'claude_agent' ? 'primary' : 'success'">
              {{ agentTypeLabel(row.config?.agent?.exec_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="persona" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            {{ (row.config?.personas || []).map(p => p.name).join('、') || '—' }}
          </template>
        </el-table-column>
        <el-table-column label="组合" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ comboLabel(row.config?.combo_mode) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="trials" width="70" align="center">
          <template #default="{ row }">{{ row.config?.trials ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="回合上限" width="80" align="center">
          <template #default="{ row }">{{ row.config?.max_turns ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="校验器" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small" type="warning">{{ (row.config?.verifiers || []).length }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="故障" width="70" align="center">
          <template #default="{ row }">{{ (row.config?.faults || []).length || '—' }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" :loading="runningId === row.id" @click="runScenario(row)">
              运行
            </el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="removeScenario(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ============ 运行列表 ============ -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div class="page-header">
          <span class="page-title">运行列表</span>
          <el-select v-model="runsScenarioFilter" placeholder="全部场景" clearable
                     size="small" style="width: 240px" @change="loadRuns">
            <el-option v-for="s in scenarios" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </div>
      </template>

      <el-table :data="runs" v-loading="runsLoading" size="default">
        <el-table-column prop="id" label="运行ID" min-width="170" show-overflow-tooltip />
        <el-table-column label="场景" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ scenarioName(row.scenario_id) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="120">
          <template #default="{ row }">
            <span v-if="row.status === 'running' || row.trials_done > 0">
              {{ row.trials_done }} / {{ row.trials_total }}
            </span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="pass@1" width="90" align="center">
          <template #default="{ row }">
            <span v-if="row._pass_at_1 != null">{{ (row._pass_at_1 * 100).toFixed(0) }}%</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="pass^k" width="90" align="center">
          <template #default="{ row }">
            <el-tag v-if="row._pass_pow_k != null" size="small"
                    :type="row._pass_pow_k >= 1 ? 'success' : 'danger'">
              {{ row._pass_pow_k >= 1 ? '全过' : '未全过' }}
            </el-tag>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="发起时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" @click="openRunDetail(row)">详情</el-button>
            <el-button v-if="row.status === 'running'" size="small" type="warning"
                       :loading="stoppingRunId === row.id" @click="stopRun(row)">停止</el-button>
            <el-button v-else size="small" @click="openRunDetail(row); refreshRun(row)">刷新</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ============ 场景构建器 ============ -->
    <el-dialog v-model="editorVisible" :title="editingId ? '编辑场景' : '新建场景'"
               width="880px" top="4vh" :close-on-click-modal="false" destroy-on-close>
      <el-form label-position="top" v-loading="editorSaving">
        <el-form-item label="场景名称" required>
          <el-input v-model="editor.name" maxlength="200" placeholder="如：报销助手仿真-新人persona" />
        </el-form-item>
        <el-form-item label="场景说明">
          <el-input v-model="editor.description" type="textarea" :rows="2" maxlength="1000"
                    placeholder="描述仿真目的与被测系统（可选）" />
        </el-form-item>

        <!-- 被测 Agent -->
        <el-divider content-position="left">被测 Agent</el-divider>
        <div class="form-row">
          <div class="form-col">
            <el-form-item label="Harness 类型" required>
              <el-select v-model="editor.agent.exec_type" style="width: 100%">
                <el-option label="Claude Agent（claude_agent）" value="claude_agent" />
                <el-option label="DeepSeek Agent（deepseek_agent）" value="deepseek_agent" />
              </el-select>
              <div class="form-tip">复用既有 harness 沙箱通道与镜像变体拉起被测 agent（每轮全新容器，工作区 trial 内持久）</div>
            </el-form-item>
          </div>
          <div class="form-col">
            <el-form-item label="单次调用超时（秒）">
              <el-input-number v-model="editor.agent.timeout_seconds" :min="30" :max="3600"
                               :step="30" controls-position="right" style="width: 100%" />
            </el-form-item>
          </div>
        </div>
        <el-form-item label="指令（自然语言，entrypoint 契约）" required>
          <el-input v-model="editor.agent.instruction" type="textarea" :rows="3"
                    placeholder="如：你是公司报销助手。按 stdin.json 中的 user_message 回应员工，处理报销提交，产出写入 outputs/result.json" />
        </el-form-item>

        <!-- persona 卡片 -->
        <el-divider content-position="left">
          用户模拟器 persona
          <span class="divider-count">{{ editor.personas.length }}/4</span>
        </el-divider>
        <div v-for="(persona, idx) in editor.personas" :key="idx" class="persona-card">
          <div class="persona-card-header">
            <span class="persona-idx">#{{ idx + 1 }}</span>
            <el-input v-model="persona.name" placeholder="称呼（如：报销新人小王）"
                      maxlength="50" style="width: 200px" size="small" />
            <span class="header-spacer" />
            <el-button type="danger" size="small" circle :icon="Delete"
                       :disabled="editor.personas.length <= 1"
                       @click="editor.personas.splice(idx, 1)" />
          </div>
          <el-input v-model="persona.role" placeholder="角色设定（如：首次使用报销系统的财务新人）"
                    maxlength="200" size="small" style="margin: 8px 0" />
          <el-input v-model="persona.goal" placeholder="目标（必填，终止判定依据：如 拿到报销受理编号）"
                    maxlength="500" size="small" style="margin-bottom: 8px" />
          <div class="form-row">
            <div class="form-col">
              <div class="slider-label">专业度：{{ expertiseLabel(persona.expertise) }}</div>
              <el-radio-group v-model="persona.expertise" size="small">
                <el-radio-button label="novice">新手</el-radio-button>
                <el-radio-button label="intermediate">进阶</el-radio-button>
                <el-radio-button label="expert">专家</el-radio-button>
              </el-radio-group>
            </div>
            <div class="form-col">
              <div class="slider-label">耐心：{{ persona.patience }} 回合</div>
              <el-slider v-model="persona.patience" :min="1" :max="15" style="width: 90%" />
            </div>
            <div class="form-col">
              <div class="slider-label">对抗性：{{ persona.adversarial.toFixed(1) }}</div>
              <el-slider v-model="persona.adversarial" :min="0" :max="1" :step="0.1"
                         style="width: 90%" />
            </div>
          </div>
          <el-input v-model="persona.style_hint" placeholder="文风提示（可选，如：口语化，爱打错别字）"
                    maxlength="200" size="small" />
        </div>
        <el-button size="small" style="margin-top: 8px"
                   :disabled="editor.personas.length >= 4"
                   @click="addPersona">+ 添加 persona</el-button>

        <div class="form-row" style="margin-top: 12px">
          <div class="form-col">
            <el-form-item label="组合模式">
              <el-select v-model="editor.combo_mode" style="width: 100%" :disabled="editor.personas.length <= 1">
                <el-option label="单人（single，多 persona 只取第一个）" value="single" />
                <el-option label="合成（blend，多身份同一用户，全目标达成才终止）" value="blend" />
                <el-option label="轮流（rotate，多用户轮换发言）" value="rotate" />
              </el-select>
            </el-form-item>
          </div>
          <div class="form-col">
            <el-form-item label="trials（pass^k 的 k）">
              <el-input-number v-model="editor.trials" :min="1" :max="5"
                               controls-position="right" style="width: 100%" />
              <div class="form-tip">1-5（对齐平台 pass^k 口径）；3-trial 全过才记 pass^k=全过</div>
            </el-form-item>
          </div>
          <div class="form-col">
            <el-form-item label="回合上限（max_turns）">
              <el-input-number v-model="editor.max_turns" :min="1" :max="50"
                               controls-position="right" style="width: 100%" />
              <div class="form-tip">一条用户消息+一条 Agent 回复记 1 回合；缺省 15</div>
            </el-form-item>
          </div>
          <div class="form-col">
            <el-form-item label="trial 软超时（秒）">
              <el-input-number v-model="editor.trial_soft_timeout_seconds" :min="60" :max="7200"
                               :step="60" controls-position="right" style="width: 100%" />
              <div class="form-tip">回合间检查（不打断进行中的回合）；缺省 1800</div>
            </el-form-item>
          </div>
        </div>

        <!-- 环境初始文件 -->
        <el-divider content-position="left">环境初始文件（每 trial 重置后 staged，可选）</el-divider>
        <el-table :data="editor.envFiles" size="small" empty-text="未配置初始文件">
          <el-table-column label="工作区相对路径" min-width="220">
            <template #default="{ row }">
              <el-input v-model="row.path" placeholder="如：README.md 或 config/app.yaml" size="small" />
            </template>
          </el-table-column>
          <el-table-column label="内容" min-width="280">
            <template #default="{ row }">
              <el-input v-model="row.content" type="textarea" :rows="1" size="small" />
            </template>
          </el-table-column>
          <el-table-column width="60" align="center">
            <template #default="{ $index }">
              <el-button type="danger" size="small" circle :icon="Delete"
                         @click="editor.envFiles.splice($index, 1)" />
            </template>
          </el-table-column>
        </el-table>
        <el-button size="small" style="margin-top: 6px" @click="editor.envFiles.push({ path: '', content: '' })">
          + 添加文件
        </el-button>

        <!-- 终态校验器（引用 M2 口径） -->
        <el-divider content-position="left">
          终态校验器
          <span class="divider-count required-star">必填 ≥1</span>
        </el-divider>
        <div class="form-tip" style="margin-bottom: 8px">
          复用 M2 校验器口径（db_assert/file_assert/http_assert；无终态校验的仿真不允许发布）。
          file_assert 断言路径以 trial 工作区为根（被测 agent 产物在 <code>sim-agent/</code> 前缀下，
          如 <code>sim-agent/outputs/result.json</code>；环境初始文件按 staging 相对路径）。
        </div>
        <div v-for="(verifier, vIdx) in editor.verifiers" :key="vIdx" class="verifier-card">
          <div class="persona-card-header">
            <el-select v-model="verifier.type" size="small" style="width: 200px"
                       @change="onVerifierTypeChange(verifier)">
              <el-option v-for="t in verifierTypes" :key="t.value" :label="t.label" :value="t.value" />
            </el-select>
            <el-input v-model="verifier.name" size="small" placeholder="名称（可选）" style="width: 160px" />
            <span class="header-spacer" />
            <el-button type="danger" size="small" circle :icon="Delete"
                       @click="editor.verifiers.splice(vIdx, 1)" />
          </div>

          <!-- db_assert -->
          <template v-if="verifier.type === 'db_assert'">
            <el-table :data="verifier.config.assertions" size="small">
              <el-table-column label="名称" width="120">
                <template #default="{ row }"><el-input v-model="row.name" size="small" /></template>
              </el-table-column>
              <el-table-column label="SQL（只读）" min-width="240">
                <template #default="{ row }"><el-input v-model="row.sql" size="small" /></template>
              </el-table-column>
              <el-table-column label="期望 JSON" width="180">
                <template #default="{ row }"><el-input v-model="row.expectedJson" size="small" /></template>
              </el-table-column>
              <el-table-column width="50" align="center">
                <template #default="{ $index }">
                  <el-button type="danger" size="small" circle :icon="Delete"
                             @click="verifier.config.assertions.splice($index, 1)" />
                </template>
              </el-table-column>
            </el-table>
            <el-button size="small" style="margin-top: 4px"
                       @click="addDbAssertion(verifier)">
              + 断言
            </el-button>
          </template>

          <!-- file_assert -->
          <template v-else-if="verifier.type === 'file_assert'">
            <el-table :data="verifier.config.assertions" size="small">
              <el-table-column label="名称" width="110">
                <template #default="{ row }"><el-input v-model="row.name" size="small" /></template>
              </el-table-column>
              <el-table-column label="路径（trial 工作区相对）" min-width="200">
                <template #default="{ row }"><el-input v-model="row.path" size="small" /></template>
              </el-table-column>
              <el-table-column label="模式" width="110">
                <template #default="{ row }">
                  <el-select v-model="row.mode" size="small">
                    <el-option label="存在" value="exists" />
                    <el-option label="正则" value="regex" />
                    <el-option label="jsonpath" value="jsonpath" />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column v-if="hasFileAssertExtra(verifier)" label="参数" min-width="160">
                <template #default="{ row }">
                  <el-input v-if="row.mode === 'regex'" v-model="row.pattern" placeholder="pattern" size="small" />
                  <template v-else-if="row.mode === 'jsonpath'">
                    <el-input v-model="row.jsonpath" placeholder="jsonpath（如 status）" size="small"
                              style="margin-bottom: 4px" />
                    <el-input v-model="row.expected" placeholder="期望值" size="small" />
                  </template>
                </template>
              </el-table-column>
              <el-table-column width="50" align="center">
                <template #default="{ $index }">
                  <el-button type="danger" size="small" circle :icon="Delete"
                             @click="verifier.config.assertions.splice($index, 1)" />
                </template>
              </el-table-column>
            </el-table>
            <el-button size="small" style="margin-top: 4px"
                       @click="verifier.config.assertions.push({ name: '', path: '', mode: 'exists', pattern: '', jsonpath: '', expected: '' })">
              + 断言
            </el-button>
          </template>

          <!-- http_assert -->
          <template v-else>
            <div class="form-row">
              <div class="form-col" style="flex: 2">
                <el-input v-model="verifier.config.url" placeholder="回调 URL（如 https://sut.example.com/api/orders）" size="small" />
              </div>
              <div class="form-col">
                <el-select v-model="verifier.config.method" size="small">
                  <el-option label="GET" value="GET" />
                  <el-option label="POST" value="POST" />
                </el-select>
              </div>
              <div class="form-col">
                <el-input-number v-model="verifier.config.expect_status" :min="100" :max="599"
                                 controls-position="right" size="small" placeholder="状态码" />
              </div>
            </div>
            <el-input v-model="verifier.config.body_regex" placeholder="响应体正则（可选）" size="small"
                      style="margin-top: 6px" />
          </template>
        </div>
        <el-button size="small" style="margin-top: 6px" @click="addVerifier">+ 添加校验器</el-button>

        <!-- 故障注入 -->
        <el-divider content-position="left">故障注入（可选）</el-divider>
        <div class="form-tip" style="margin-bottom: 8px">
          harness 调用层拦截，命中回合由「注入回合」显式声明（确定性，可复现）。
          工具超时/错误/污染为响应侧模拟（会话继续，考察注入后恢复）；矛盾指令/上下文超长改写发给 agent 的消息。
        </div>
        <el-checkbox-group v-model="editor.faultTypes">
          <el-checkbox v-for="f in faultOptions" :key="f.value" :label="f.value">
            {{ f.label }}
          </el-checkbox>
        </el-checkbox-group>
        <div class="form-row" style="margin-top: 8px" v-if="editor.faultTypes.length">
          <div class="form-col">
            <el-form-item label="注入回合（用户回合号，逗号分隔）">
              <el-input v-model="editor.faultTurns" placeholder="如：2（缺省第 2 回合）" />
              <div class="form-tip">对全部勾选的故障统一生效；多个回合逗号分隔（如 2,4）</div>
            </el-form-item>
          </div>
          <div class="form-col" v-if="editor.faultTypes.includes('context_overflow')">
            <el-form-item label="超长填充字符数">
              <el-input-number v-model="editor.faultPadChars" :min="1000" :max="2000000" :step="10000"
                               controls-position="right" style="width: 100%" />
            </el-form-item>
          </div>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="editorVisible = false">取消</el-button>
        <el-button type="primary" :loading="editorSaving" @click="saveScenario">
          {{ editingId ? '保存' : '创建' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 运行详情：逐 trial 对话回放 ============ -->
    <el-drawer v-model="runDetailVisible" size="72%" :title="`运行详情 · ${runDetail?.id || ''}`"
               destroy-on-close>
      <div v-if="runDetail" v-loading="runDetailLoading" class="run-detail">
        <!-- 指标卡 -->
        <div class="metric-cards" v-if="runDetail.result">
          <div class="metric-card">
            <div class="metric-value">{{ percent(runDetail.result.task_success_rate) }}</div>
            <div class="metric-label">任务成功率（pass@1）</div>
          </div>
          <div class="metric-card">
            <div class="metric-value" :class="runDetail.result.pass_k?.pass_pow_k >= 1 ? 'ok' : 'bad'">
              {{ runDetail.result.pass_k?.pass_pow_k >= 1 ? '全过' : '未全过' }}
            </div>
            <div class="metric-label">
              pass^k（{{ runDetail.result.pass_k?.pass_count ?? 0 }}/{{ runDetail.result.pass_k?.trials ?? 0 }}）
            </div>
          </div>
          <div class="metric-card">
            <div class="metric-value">{{ runDetail.result.average_turns ?? '—' }}</div>
            <div class="metric-label">平均回合数</div>
          </div>
          <div class="metric-card">
            <div class="metric-value">{{ percent(runDetail.result.tool_waste_rate) }}</div>
            <div class="metric-label">工具浪费率</div>
          </div>
          <div class="metric-card">
            <div class="metric-value">{{ percent(runDetail.result.fault_recovery_rate) }}</div>
            <div class="metric-label">故障恢复率</div>
          </div>
        </div>
        <el-alert v-else-if="runDetail.status === 'failed'" type="error" :closable="false"
                  :title="`运行失败：${runDetail.error || '未知错误'}`" style="margin-bottom: 12px" />

        <!-- 逐 trial -->
        <el-collapse v-model="activeTrials" v-if="(runDetail.result?.trials || []).length">
          <el-collapse-item v-for="trial in runDetail.result.trials" :key="trial.trial"
                            :name="String(trial.trial)">
            <template #title>
              <div class="trial-title">
                <span>Trial {{ trial.trial }}</span>
                <el-tag size="small" :type="trial.passed ? 'success' : 'danger'"
                        style="margin-left: 8px">
                  {{ trial.passed ? '通过' : '未通过' }}
                </el-tag>
                <el-tag size="small" type="info" effect="plain" style="margin-left: 6px">
                  {{ terminationLabel(trial.termination?.reason) }}
                </el-tag>
                <span class="trial-meta">
                  {{ (trial.turns?.user_turn_count ?? 0) }} 回合 ·
                  {{ trial.agent?.tool_count ?? 0 }} 工具 ·
                  {{ ((trial.duration_ms ?? 0) / 1000).toFixed(1) }}s
                </span>
                <span class="header-spacer" />
                <el-tag v-if="(trial.faults_fired || []).length" size="small" type="warning">
                  注入 × {{ trial.faults_fired.length }}
                </el-tag>
                <el-button v-if="!trial.passed" size="small" type="primary" plain
                           style="margin-left: 10px"
                           :loading="convertingTrial === `${runDetail.id}:${trial.trial}`"
                           @click.stop="convertTrialToDataset(trial)">
                  转数据集
                </el-button>
              </div>
            </template>

            <!-- 终态校验结果 -->
            <div class="verify-block" v-if="trial.verification">
              <div class="verify-title">
                终态校验：
                <el-tag size="small" :type="verifyTagType(trial.verification.status)">
                  {{ trial.verification.status }}
                </el-tag>
                <span class="verify-meta">
                  {{ trial.verification.passed ?? 0 }}/{{ trial.verification.total ?? 0 }} 通过
                </span>
              </div>
              <el-table :data="trial.verification.results || []" size="small">
                <el-table-column prop="type" label="类型" width="110" />
                <el-table-column prop="name" label="名称" min-width="130" />
                <el-table-column label="状态" width="80">
                  <template #default="{ row }">
                    <el-tag size="small" :type="verifyTagType(row.status)">{{ row.status }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="证据/错误" min-width="260" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span class="mono">{{ row.error || evidenceSummary(row.evidence) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <!-- 对话回放：双气泡时间线 + 工具调用轨道 -->
            <div class="dialog-replay">
              <div v-for="(turn, tIdx) in trial.turns?.turns || []" :key="tIdx" class="replay-turn">
                <!-- 用户气泡（左） -->
                <div v-if="turn.role === 'user'" class="bubble-row user-row">
                  <div class="bubble user-bubble">
                    <div class="bubble-speaker">
                      <el-icon><User /></el-icon>
                      {{ turn.speaker || '模拟用户' }}
                      <span class="bubble-round">第 {{ turn.round }} 回合</span>
                    </div>
                    <div class="bubble-content">{{ turn.content }}</div>
                    <div class="bubble-meta" v-if="turn.action === 'give_up'">
                      <el-tag size="small" type="warning">模拟器放弃</el-tag>
                    </div>
                  </div>
                </div>
                <!-- Agent 气泡（右）+ 工具轨道 -->
                <div v-else class="bubble-row agent-row">
                  <div class="bubble agent-bubble">
                    <div class="bubble-speaker">
                      <el-icon><Monitor /></el-icon>
                      被测 Agent
                      <span class="bubble-round" v-if="turn.round">第 {{ turn.round }} 回合</span>
                    </div>
                    <div class="bubble-content">{{ turn.content }}</div>
                    <!-- 工具调用轨道 -->
                    <div class="tool-track" v-if="(turn.tools || []).length">
                      <div v-for="tool in turn.tools" :key="tool" class="tool-chip">
                        <el-icon><SetUp /></el-icon>{{ tool }}
                      </div>
                    </div>
                    <div class="bubble-meta" v-if="turn.meta">
                      <el-tag v-if="turn.meta.finish_reason" size="small" type="info" effect="plain">
                        {{ turn.meta.finish_reason }}
                      </el-tag>
                      <el-tag v-for="f in turn.meta.faults || []" :key="f.type" size="small" type="warning">
                        故障注入·{{ faultLabel(f.type) }}
                      </el-tag>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
        <el-empty v-else-if="runDetail.status === 'running'" description="运行中——完成后此处展示逐 trial 回放" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
/**
 * 仿真评测页（M7 QE7-5，plans §M7 7.3）
 *
 * - 场景构建器：persona 卡片编辑（角色/目标/专业度/耐心/对抗性、组合模式）、
 *   被测 Agent 指令、环境初始文件、终态校验器（引用 M2 口径的编辑形态）、
 *   故障注入勾选 + 注入回合、trials/max_turns/软超时；
 * - 运行列表：状态/进度/pass@1/pass^k，详情抽屉逐 trial 对话回放
 *   （双气泡时间线 + 工具调用轨道）、终态校验结果表、指标卡；
 * - 失败 trial 一键转数据集：完整对话回放（user_input）+ 终态期望
 *   （ref_answer 与元数据：termination/verification/faults），沉淀回归资产。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Monitor, SetUp, User } from '@element-plus/icons-vue'
import simulationApi from '@/api/simulation'
import request, { datasetApi } from '@/api'

// ---------------- 常量与标签 ----------------
const verifierTypes = [
  { value: 'db_assert', label: 'db_assert（数据库）' },
  { value: 'file_assert', label: 'file_assert（文件）' },
  { value: 'http_assert', label: 'http_assert（回调）' }
]
// db_assert 期望断言默认 JSON 模板（内联字符串转义易错，收敛为常量；同 Tasks.vue 口径）
const DB_ASSERT_EXPECTED_DEFAULT = '{"scalar": 1}'
const faultOptions = [
  { value: 'tool_timeout', label: '工具超时' },
  { value: 'tool_error', label: '工具错误响应' },
  { value: 'dirty_data', label: '返回污染数据' },
  { value: 'contradiction', label: '矛盾指令' },
  { value: 'context_overflow', label: '上下文超长' }
]
const agentTypeLabel = (t) => t === 'deepseek_agent' ? 'DeepSeek' : t === 'claude_agent' ? 'Claude' : t || '—'
const comboLabel = (m) => ({ single: '单人', blend: '合成', rotate: '轮流' }[m] || m || '单人')
const expertiseLabel = (e) => ({ novice: '新手', intermediate: '进阶', expert: '专家' }[e] || e)
const faultLabel = (t) => (faultOptions.find(f => f.value === t) || { label: t }).label
const statusLabel = (s) => ({
  pending: '排队中', running: '运行中', completed: '已完成', failed: '失败', stopped: '已停止'
}[s] || s)
const statusTagType = (s) => ({
  pending: 'info', running: 'primary', completed: 'success', failed: 'danger', stopped: 'warning'
}[s] || 'info')
const terminationLabel = (r) => ({
  goal_achieved: '目标达成', give_up: '用户放弃', max_turns: '回合耗尽',
  stopped: '任务中止', soft_timeout: '软超时', agent_error: 'Agent 故障', simulator_error: '模拟器故障'
}[r] || r || '—')
const verifyTagType = (s) => s === 'pass' ? 'success' : s === 'fail' ? 'danger' : 'warning'
const percent = (v) => v == null ? '—' : `${(v * 100).toFixed(0)}%`
const formatTime = (t) => t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '—'
const evidenceSummary = (e) => {
  if (e == null) return ''
  if (typeof e === 'string') return e
  try { return JSON.stringify(e).slice(0, 160) } catch { return String(e) }
}

// ---------------- 场景列表 ----------------
const scenarios = ref([])
const listLoading = ref(false)
const runningId = ref('')

const loadScenarios = async () => {
  listLoading.value = true
  try {
    scenarios.value = await simulationApi.listScenarios({ limit: 100 })
  } catch (e) {
    ElMessage.error(e.message || '场景列表加载失败')
  } finally {
    listLoading.value = false
  }
}

const runScenario = async (row) => {
  try {
    await ElMessageBox.confirm(
      `运行场景「${row.name}」（${row.config?.trials ?? 3} 个 trial）？`, '发起仿真', { type: 'info' })
  } catch { return }
  runningId.value = row.id
  try {
    const res = await simulationApi.run(row.id)
    ElMessage.success(`已发起运行 ${res.run_id}（${res.trials_total} trials）`)
    await loadRuns()
  } catch (e) {
    ElMessage.error(e.message || '发起失败')
  } finally {
    runningId.value = ''
  }
}

const removeScenario = async (row) => {
  try {
    await ElMessageBox.confirm(
      `删除场景「${row.name}」？历史运行将随场景级联删除。`, '删除场景', { type: 'warning' })
  } catch { return }
  try {
    await simulationApi.deleteScenario(row.id)
    ElMessage.success('已删除')
    await loadScenarios()
    await loadRuns()
  } catch (e) {
    ElMessage.error(e.message || '删除失败')
  }
}

// ---------------- 场景编辑器 ----------------
const editorVisible = ref(false)
const editorSaving = ref(false)
const editingId = ref('')
const editor = ref(emptyEditor())

function emptyEditor() {
  return {
    name: '',
    description: '',
    agent: { exec_type: 'claude_agent', instruction: '', timeout_seconds: 300 },
    personas: [emptyPersona()],
    combo_mode: 'single',
    trials: 3,
    max_turns: 15,
    trial_soft_timeout_seconds: 1800,
    envFiles: [],
    verifiers: [],
    faultTypes: [],
    faultTurns: '',
    faultPadChars: 100000
  }
}

function emptyPersona() {
  return {
    name: '', role: '', goal: '',
    expertise: 'intermediate', patience: 5, adversarial: 0, style_hint: ''
  }
}

const addPersona = () => {
  editor.value.personas.push(emptyPersona())
}

const onVerifierTypeChange = (verifier) => {
  verifier.config = verifierDefaultConfig(verifier.type)
}

// 模板内联 JSON 字符串易踩引号转义坑，收敛为方法（与 Tasks.vue 的常量口径一致）
const addDbAssertion = (verifier) => {
  verifier.config.assertions.push({ name: '', sql: '', expectedJson: DB_ASSERT_EXPECTED_DEFAULT })
}

const verifierDefaultConfig = (type) => {
  if (type === 'db_assert') return { assertions: [{ name: '', sql: '', expectedJson: '{"scalar": 1}' }] }
  if (type === 'file_assert') {
    return { assertions: [{ name: '', path: 'sim-agent/outputs/result.json', mode: 'jsonpath', jsonpath: 'status', expected: '', pattern: '' }] }
  }
  return { url: '', method: 'GET', expect_status: 200, body_regex: '', jsonpath: '', expected: '' }
}

const addVerifier = () => {
  const type = 'file_assert'
  editor.value.verifiers.push({ type, name: '', config: verifierDefaultConfig(type) })
}

const hasFileAssertExtra = (verifier) =>
  (verifier.config.assertions || []).some(a => a.mode === 'regex' || a.mode === 'jsonpath')

const openCreate = () => {
  editingId.value = ''
  editor.value = emptyEditor()
  editorVisible.value = true
}

const openEdit = (row) => {
  editingId.value = row.id
  const config = row.config || {}
  editor.value = {
    name: row.name,
    description: row.description || '',
    agent: {
      exec_type: config.agent?.exec_type || 'claude_agent',
      instruction: config.agent?.instruction || '',
      timeout_seconds: config.agent?.timeout_seconds || 300
    },
    personas: (config.personas?.length ? config.personas : [{}]).map(p => ({
      name: p.name || '', role: p.role || '', goal: p.goal || '',
      expertise: p.expertise || 'intermediate',
      patience: p.patience ?? 5, adversarial: p.adversarial ?? 0,
      style_hint: p.style_hint || ''
    })),
    combo_mode: config.combo_mode || 'single',
    trials: config.trials ?? 3,
    max_turns: config.max_turns ?? 15,
    trial_soft_timeout_seconds: config.trial_soft_timeout_seconds ?? 1800,
    envFiles: Object.entries(config.env?.files || {}).map(([path, content]) => ({ path, content })),
    verifiers: (config.verifiers || []).map(v => ({
      type: v.type,
      name: v.name || '',
      config: normalizeVerifierConfig(v)
    })),
    faultTypes: (config.faults || []).map(f => f.type),
    faultTurns: (config.faults?.[0]?.config?.turns || []).join(',') || '',
    faultPadChars: config.faults?.find(f => f.type === 'context_overflow')?.config?.pad_chars || 100000
  }
  editorVisible.value = true
}

const normalizeVerifierConfig = (v) => {
  const config = { ...(v.config || {}) }
  if (v.type === 'db_assert') {
    config.assertions = (config.assertions || []).map(a => ({
      name: a?.name || '', sql: a?.sql || '',
      expectedJson: a?.expected != null ? JSON.stringify(a.expected) : '{"scalar": 1}'
    }))
  }
  return config
}

// 收集校验器（与 Tasks.vue 的 _collectVerifierList 同构：无效行过滤）
const collectVerifiers = (list) => {
  const collected = []
  for (const verifier of (list || [])) {
    const config = verifier.config || {}
    if (verifier.type === 'db_assert') {
      const assertions = (config.assertions || [])
        .filter(a => a && (a.sql || '').trim())
        .map(a => {
          let expected
          try { expected = JSON.parse(a.expectedJson || '{}') } catch { expected = { scalar: null } }
          return { name: (a.name || '').trim(), sql: a.sql, expected }
        })
      if (!assertions.length) continue
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), config: { assertions } })
      continue
    }
    if (verifier.type === 'file_assert') {
      const assertions = (config.assertions || [])
        .filter(a => a && (a.path || '').trim())
        .map(a => {
          const item = { name: (a.name || '').trim(), path: a.path.trim(), mode: a.mode || 'exists' }
          if (item.mode === 'regex' && (a.pattern || '').trim()) item.pattern = a.pattern.trim()
          if (item.mode === 'jsonpath') {
            if ((a.jsonpath || '').trim()) item.jsonpath = a.jsonpath.trim()
            if ((a.expected || '') !== '') item.expected = a.expected
          }
          return item
        })
      if (!assertions.length) continue
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), config: { assertions } })
      continue
    }
    if (verifier.type === 'http_assert') {
      const url = (config.url || '').trim()
      if (!url) continue
      const httpConfig = {
        url, method: config.method || 'GET', expect_status: config.expect_status ?? 200
      }
      if ((config.body_regex || '').trim()) httpConfig.body_regex = config.body_regex.trim()
      collected.push({ type: verifier.type, name: (verifier.name || '').trim(), config: httpConfig })
    }
  }
  return collected
}

const parseFaultTurns = (raw) => {
  const turns = String(raw || '')
    .split(/[,，\s]+/)
    .map(t => parseInt(t, 10))
    .filter(t => Number.isInteger(t) && t >= 1)
  return [...new Set(turns)]
}

const buildScenarioConfig = () => {
  const editor_ = editor.value
  const envFiles = {}
  for (const f of editor_.envFiles) {
    const path = (f.path || '').trim()
    if (path) envFiles[path] = f.content || ''
  }
  const faults = editor_.faultTypes.map(type => {
    const config = {}
    const turns = parseFaultTurns(editor_.faultTurns)
    if (turns.length) config.turns = turns
    if (type === 'context_overflow') config.pad_chars = editor_.faultPadChars
    return { type, ...(Object.keys(config).length ? { config } : {}) }
  })
  const config = {
    agent: {
      exec_type: editor_.agent.exec_type,
      instruction: (editor_.agent.instruction || '').trim(),
      timeout_seconds: editor_.agent.timeout_seconds
    },
    personas: editor_.personas.filter(p => (p.goal || '').trim()).map(p => ({
      name: (p.name || '').trim(), role: (p.role || '').trim(), goal: p.goal.trim(),
      expertise: p.expertise, patience: p.patience,
      adversarial: p.adversarial, style_hint: (p.style_hint || '').trim()
    })),
    combo_mode: editor_.combo_mode,
    trials: editor_.trials,
    max_turns: editor_.max_turns,
    trial_soft_timeout_seconds: editor_.trial_soft_timeout_seconds,
    verifiers: collectVerifiers(editor_.verifiers)
  }
  if (Object.keys(envFiles).length) config.env = { files: envFiles }
  if (faults.length) config.faults = faults
  return config
}

const saveScenario = async () => {
  const editor_ = editor.value
  if (!(editor_.name || '').trim()) return ElMessage.warning('请填写场景名称')
  if (!(editor_.agent.instruction || '').trim()) return ElMessage.warning('请填写被测 Agent 指令')
  const config = buildScenarioConfig()
  if (!config.personas.length) return ElMessage.warning('至少一个有目标的 persona')
  if (!config.verifiers.length) {
    return ElMessage.warning('仿真场景必须至少配置一个终态校验器（发布门禁）')
  }
  editorSaving.value = true
  try {
    if (editingId.value) {
      await simulationApi.updateScenario(editingId.value, {
        name: editor_.name.trim(), description: editor_.description, config
      })
      ElMessage.success('已保存（版本 +1）')
    } else {
      await simulationApi.createScenario({
        name: editor_.name.trim(), description: editor_.description, config
      })
      ElMessage.success('场景已创建')
    }
    editorVisible.value = false
    await loadScenarios()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    editorSaving.value = false
  }
}

// ---------------- 运行列表与详情 ----------------
const runs = ref([])
const runsLoading = ref(false)
const runsScenarioFilter = ref('')
const stoppingRunId = ref('')
const runDetailVisible = ref(false)
const runDetailLoading = ref(false)
const runDetail = ref(null)
const activeTrials = ref(['1'])
const convertingTrial = ref('')

const scenarioName = (id) => scenarios.value.find(s => s.id === id)?.name || id

const loadRuns = async () => {
  runsLoading.value = true
  try {
    if (runsScenarioFilter.value) {
      const list = await simulationApi.listRuns(runsScenarioFilter.value, { limit: 50 })
      runs.value = list.map(r => ({ ...r, _pass_at_1: null, _pass_pow_k: null }))
    } else {
      // 未过滤：并行取各场景运行历史（场景数量有限，串行合并即可）
      const all = []
      for (const s of scenarios.value) {
        const list = await simulationApi.listRuns(s.id, { limit: 20 }).catch(() => [])
        all.push(...list)
      }
      runs.value = all.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    }
    // 补齐已跑完 run 的 pass 口径（详情里有 result；轻量逐个拉取）
    for (const run of runs.value) {
      if (run.status === 'completed') {
        await refreshRun(run, true)
      }
    }
  } catch (e) {
    ElMessage.error(e.message || '运行列表加载失败')
  } finally {
    runsLoading.value = false
  }
}

const refreshRun = async (run, silent = false) => {
  try {
    const detail = await simulationApi.getRun(run.id)
    Object.assign(run, {
      status: detail.status, trials_done: detail.trials_done, result: detail.result,
      _pass_at_1: detail.result?.pass_k?.pass_at_1 ?? null,
      _pass_pow_k: detail.result?.pass_k?.pass_pow_k ?? null
    })
    if (runDetail.value?.id === run.id) runDetail.value = { ...runDetail.value, ...detail }
  } catch (e) {
    if (!silent) ElMessage.error(e.message || '刷新失败')
  }
}

const stopRun = async (run) => {
  stoppingRunId.value = run.id
  try {
    await simulationApi.stopRun(run.id)
    ElMessage.success('已发送停止信号（回合间生效）')
    await refreshRun(run)
  } catch (e) {
    ElMessage.error(e.message || '停止失败')
  } finally {
    stoppingRunId.value = ''
  }
}

const openRunDetail = async (run) => {
  runDetailVisible.value = true
  runDetailLoading.value = true
  runDetail.value = { ...run, result: run.result }
  try {
    const detail = await simulationApi.getRun(run.id)
    runDetail.value = detail
    activeTrials.value = detail.result?.trials?.length ? [String(detail.result.trials[0].trial)] : []
  } catch (e) {
    ElMessage.error(e.message || '详情加载失败')
  } finally {
    runDetailLoading.value = false
  }
}

// ---------------- 失败 trial 一键转数据集 ----------------
const convertTrialToDataset = async (trial) => {
  const run = runDetail.value
  if (!run) return
  const scenario = scenarios.value.find(s => s.id === run.scenario_id)
  const scenarioLabel = scenario?.name || run.scenario_id
  try {
    await ElMessageBox.confirm(
      `把 Trial ${trial.trial}（${terminationLabel(trial.termination?.reason)}）的完整对话转为数据集行？` +
      '沉淀为回归资产：user_input=对话回放、ref_answer=终态期望、元数据记录失败原因与校验结果。',
      '失败 trial 转数据集', { type: 'info' })
  } catch { return }
  convertingTrial.value = `${run.id}:${trial.trial}`
  try {
    // 对话回放文本（与终态校验 model_output 同构）
    const lines = (trial.turns?.turns || []).map(t =>
      t.role === 'user' ? `用户·${t.speaker || ''}: ${t.content}` : `Agent: ${t.content}`)
    const dialogText = lines.join('\n')
    const dataset = await datasetApi.create({
      name: `仿真回归-${scenarioLabel}-trial${trial.trial}-${new Date().toISOString().slice(0, 10)}`,
      description: `由仿真运行 ${run.id} Trial ${trial.trial} 失败记录沉淀（${scenarioLabel}）`
    })
    // POST /evaluation/datasets/{id}/import/json（ImportJsonRequest.data；其余键平铺进 data JSON）
    await request.post(`/evaluation/datasets/${dataset.id}/import/json`, {
      data: [{
        question: dialogText,
        ref_answer: 'Agent 应在仿真对话中达成 persona 目标并通过全部终态校验',
        simulation_run_id: run.id,
        simulation_trial: trial.trial,
        termination_reason: trial.termination?.reason || '',
        termination_detail: trial.termination?.detail || '',
        verification_status: trial.verification?.status || '',
        faults_fired: (trial.faults_fired || []).map(f => f.type).join(','),
        scenario_name: scenarioLabel
      }]
    })
    ElMessage.success(`已转数据集 ${dataset.id}（1 行）`)
  } catch (e) {
    ElMessage.error(e.message || '转数据集失败')
  } finally {
    convertingTrial.value = ''
  }
}

// ---------------- 初始化 ----------------
onMounted(async () => {
  await loadScenarios()
  await loadRuns()
})
</script>

<style scoped>
.simulation-page { padding: 0; }
.page-header { display: flex; align-items: center; justify-content: space-between; }
.page-title { font-size: 16px; font-weight: 600; margin-right: 10px; }
.page-sub { font-size: 12px; color: var(--el-text-color-secondary); }
.scenario-name { font-weight: 500; }

.form-row { display: flex; gap: 16px; }
.form-col { flex: 1; min-width: 0; }
.form-tip { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.divider-count { font-size: 12px; color: var(--el-text-color-secondary); margin-left: 6px; }
.required-star { color: var(--el-color-danger); }

.persona-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px; padding: 10px 12px; margin-bottom: 10px;
  background: var(--el-fill-color-blank);
}
.persona-card-header { display: flex; align-items: center; gap: 8px; }
.persona-idx { font-size: 12px; color: var(--el-text-color-secondary); }
.header-spacer { flex: 1; }
.slider-label { font-size: 12px; color: var(--el-text-color-regular); margin-bottom: 4px; }

.verifier-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px; padding: 10px 12px; margin-bottom: 8px;
}

.metric-cards { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.metric-card {
  flex: 1; min-width: 130px; text-align: center;
  border: 1px solid var(--el-border-color-lighter); border-radius: 6px;
  padding: 12px 8px; background: var(--el-fill-color-light);
}
.metric-value { font-size: 22px; font-weight: 600; }
.metric-value.ok { color: var(--el-color-success); }
.metric-value.bad { color: var(--el-color-danger); }
.metric-label { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 4px; }

.trial-title { display: flex; align-items: center; width: 100%; padding-right: 8px; }
.trial-meta { font-size: 12px; color: var(--el-text-color-secondary); margin-left: 10px; }

.verify-block { margin-bottom: 12px; }
.verify-title { font-size: 13px; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }
.verify-meta { font-size: 12px; color: var(--el-text-color-secondary); }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }

.dialog-replay { margin-top: 8px; }
.bubble-row { display: flex; margin-bottom: 10px; }
.user-row { justify-content: flex-start; }
.agent-row { justify-content: flex-end; }
.bubble {
  max-width: 78%; border-radius: 8px; padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter);
}
.user-bubble { background: var(--el-color-primary-light-9); }
.agent-bubble { background: var(--el-fill-color-light); }
.bubble-speaker {
  font-size: 12px; font-weight: 600; color: var(--el-text-color-regular);
  display: flex; align-items: center; gap: 4px; margin-bottom: 4px;
}
.bubble-round { font-weight: 400; color: var(--el-text-color-secondary); margin-left: 4px; }
.bubble-content { font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.bubble-meta { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.tool-track { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.tool-chip {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; font-family: monospace;
  border: 1px dashed var(--el-border-color); border-radius: 4px;
  padding: 2px 6px; color: var(--el-text-color-regular);
  background: var(--el-fill-color-blank);
}
</style>
