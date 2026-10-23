<template>
  <div class="models-page">
    <!-- 裁判配置卡片：Claude Code / DeepSeek Harness 用 tab 切换 -->
    <el-card class="judge-config-card">
      <el-tabs v-model="judgeActiveTab">
        <el-tab-pane name="cc">
          <template #label>
            <span class="judge-tab-label">
              Claude Code 裁判配置
              <el-tag :type="ccConfigured ? 'success' : 'info'" size="small">
                {{ ccConfigured ? '已配置' : '尚未配置' }}
              </el-tag>
            </span>
          </template>
          <div v-loading="ccLoading">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
            title="全局裁判配置，所有 AI 裁判评测默认使用（Claude Code Agent 引擎与裁判图引擎共用）。"
          />
          <el-alert
            v-if="!ccConfigured"
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
            title="尚未配置：请填写以下表单并保存后再使用 AI 裁判评测。"
          />

          <el-form :model="ccForm" :rules="ccRules" ref="ccFormRef" label-width="140px">
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="API 网关地址" prop="base_url">
                  <el-input v-model="ccForm.base_url" placeholder="https://api.anthropic.com 或兼容网关地址" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Auth Token" prop="auth_token">
                  <el-input v-model="ccForm.auth_token" type="password" show-password autocomplete="off" placeholder="不修改请保持原值（后端返回脱敏值）" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="模型" prop="model">
                  <el-input v-model="ccForm.model" placeholder="如 claude-sonnet-4-5" />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="超时(秒)" prop="timeout_seconds">
                  <div>
                    <el-input-number v-model="ccForm.timeout_seconds" :min="10" :max="3600" controls-position="right" style="width: 140px" />
                    <div class="cc-field-tip">单次裁判调用超时(秒)</div>
                  </div>
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="最大轮次" prop="max_turns">
                  <div>
                    <el-input-number v-model="ccForm.max_turns" :min="1" :max="200" controls-position="right" style="width: 140px" />
                    <div class="cc-field-tip">Agent 裁判最大轮次</div>
                  </div>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="HTTPS 代理" prop="proxy_url">
              <el-input v-model="ccForm.proxy_url" placeholder="HTTPS 代理(可选)，如 http://127.0.0.1:7890" style="max-width: 560px" />
            </el-form-item>
            <el-form-item label="额外环境变量">
              <JsonFieldEditor v-model:fields="ccExtraEnvConfig" />
            </el-form-item>
            <el-form-item label="脚本 MCP 工具" prop="enable_script_mcp">
              <el-switch v-model="ccForm.enable_script_mcp" />
              <span class="cc-field-tip" style="margin-left: 10px; max-width: 640px; display: inline-block;">
                开启后把进程内前处理脚本（统一落库后含 allure_vs_cases / xml_diff / log_grep / db_assert）以 stdio MCP server
                注入 Claude Code Agent 裁判，供其在评测循环内按需调用确定性比对工具；默认关闭。
              </span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="ccSaving || ccTesting" :loading="ccSaving" @click="saveCcConfig">保存配置</el-button>
              <el-button :disabled="ccSaving || ccTesting" :loading="ccTesting" @click="testCcConfig">测试连接</el-button>
            </el-form-item>
          </el-form>

          <!-- 测试连接结果展示 -->
          <div v-if="ccTestResult" class="cc-test-result">
            <el-alert
              v-if="ccHttpAlert"
              :type="ccHttpAlert.type"
              :title="ccHttpAlert.title"
              :closable="false"
              show-icon
              style="margin-bottom: 8px"
            />
            <el-alert
              v-if="ccAgentAlert"
              :type="ccAgentAlert.type"
              :title="ccAgentAlert.title"
              :closable="false"
              show-icon
            />
          </div>
          </div>
        </el-tab-pane>
        <el-tab-pane name="dsh">
          <template #label>
            <span class="judge-tab-label">
              DeepSeek Harness 裁判配置
              <el-tag :type="dshConfigured ? 'success' : 'info'" size="small">
                {{ dshConfigured ? '已配置' : '尚未配置' }}
              </el-tag>
            </span>
          </template>
          <div v-loading="dshLoading">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
            title="DeepSeek Agent 引擎全局配置（deepseek_agent 类型脚本与 DeepSeek Agent 裁判引擎使用）。"
          />
          <el-alert
            v-if="!dshConfigured"
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
            title="尚未配置：请填写以下表单并保存后再使用 DeepSeek Agent 引擎。"
          />

          <el-form :model="dshForm" :rules="dshRules" ref="dshFormRef" label-width="140px">
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="API 地址" prop="base_url">
                  <el-input v-model="dshForm.base_url" placeholder="网关地址(可选)，如 https://api.deepseek.com" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="API Key" prop="api_key">
                  <el-input v-model="dshForm.api_key" type="password" show-password autocomplete="off" placeholder="不修改请保持原值（后端返回脱敏值）" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="API 协议" prop="protocol">
                  <el-select v-model="dshForm.protocol" style="width: 100%">
                    <el-option label="DeepSeek 官方协议" value="deepseek" />
                    <el-option label="OpenAI 兼容 (chat/completions)" value="openai" />
                    <el-option label="Anthropic Messages (/v1/messages)" value="anthropic" />
                  </el-select>
                  <div class="cc-field-tip">非 DeepSeek 协议经 pi-ai 适配器接入（如内部 UAT 网关的 /api-sse-anthropic）</div>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Provider" prop="provider">
                  <el-input v-model="dshForm.provider" placeholder="DSH provider 标识，如 deepseek-official" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="模型" prop="model">
                  <el-input v-model="dshForm.model" placeholder="如 deepseek-v4-flash" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Profile" prop="profile">
                  <el-select v-model="dshForm.profile" filterable allow-create default-first-option placeholder="DSH profile" style="width: 100%">
                    <el-option v-for="p in dshProfileOptions" :key="p" :label="p" :value="p" />
                  </el-select>
                  <div class="cc-field-tip">DSH 启动 profile，默认 sdk-minimal</div>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="20">
              <el-col :span="8">
                <el-form-item label="超时(秒)" prop="timeout_seconds">
                  <div>
                    <el-input-number v-model="dshForm.timeout_seconds" :min="10" :max="3600" controls-position="right" style="width: 140px" />
                    <div class="cc-field-tip">单次 Agent 调用超时(秒)</div>
                  </div>
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="Max Tokens" prop="max_tokens">
                  <div>
                    <el-input-number v-model="dshForm.max_tokens" :min="256" :max="200000" :step="1024" controls-position="right" style="width: 140px" />
                    <div class="cc-field-tip">单次请求最大 token 数</div>
                  </div>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="额外环境变量">
              <JsonFieldEditor v-model:fields="dshExtraEnvConfig" />
            </el-form-item>
            <el-form-item label="额外 Patch">
              <div style="width: 100%">
                <el-input
                  v-model="dshExtraPatchesText"
                  type="textarea"
                  :rows="3"
                  placeholder='JSON 字符串数组，如 ["my-patch-id"]'
                  style="max-width: 560px; font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace"
                />
                <div class="cc-field-tip">DSH 启动时叠加的 patch id 列表（JSON 数组，可为空）</div>
              </div>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :disabled="dshSaving || dshTesting" :loading="dshSaving" @click="saveDshConfig">保存配置</el-button>
              <el-button :disabled="dshSaving || dshTesting" :loading="dshTesting" @click="testDshConfig">测试连接</el-button>
              <!-- M8 QE8-2：双引擎裁判对拍（同批既有输出交 CC × DSH 评分出一致性报告） -->
              <el-button :disabled="readonlyRole" @click="openJudgeComparison">与 Claude Code 对拍</el-button>
            </el-form-item>
          </el-form>

          <!-- 测试连接结果展示（分步：HTTP 直调 / Agent 链路） -->
          <div v-if="dshTestResult" class="cc-test-result">
            <el-alert
              v-if="dshHttpAlert"
              :type="dshHttpAlert.type"
              :title="dshHttpAlert.title"
              :closable="false"
              show-icon
              style="margin-bottom: 8px"
            />
            <el-alert
              v-if="dshAgentAlert"
              :type="dshAgentAlert.type"
              :title="dshAgentAlert.title"
              :closable="false"
              show-icon
            />
          </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-card>
      <template #header>
        <div class="card-header">
          <span>模型配置</span>
          <div>
            <el-select v-model="roleFilter" placeholder="筛选模型角色" clearable style="width: 150px; margin-right: 10px;">
              <el-option label="全部" value="" />
              <el-option label="待评测模型" value="evaluation" />
              <el-option label="裁判模型" value="judge" />
            </el-select>
            <el-button type="primary" :disabled="readonlyRole" :title="readonlyRole ? '当前角色（viewer）只读' : ''" @click="openDialog()">新增模型</el-button>
          </div>
        </div>
      </template>

      <el-table :data="models" style="width: 100%">
        <el-table-column prop="name" label="模型名称" min-width="150" />
        <el-table-column prop="model_role" label="模型角色" min-width="100">
          <template #default="{ row }">
            <el-tag :type="row.model_role === 'judge' ? 'warning' : 'primary'">
              {{ row.model_role === 'judge' ? '裁判模型' : '待评测' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="provider" label="接入方式" min-width="100" />
        <el-table-column prop="model_type" label="具体模型" min-width="150" />
        <el-table-column prop="endpoint" label="端点" min-width="200" show-overflow-tooltip />
        <el-table-column prop="auth_type" label="鉴权方式" min-width="100" />
        <el-table-column prop="supports_multimodal" label="多模态" min-width="80">
          <template #default="{ row }">
            <el-tag :type="row.supports_multimodal ? 'success' : 'info'">
              {{ row.supports_multimodal ? '支持' : '不支持' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="is_active" label="状态" min-width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'danger'">
              {{ row.is_active ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="testConnection(row)" :disabled="testingLoading || rowOpLoading">测试</el-button>
            <el-button link type="primary" @click="openDialog(row)" :disabled="rowOpLoading">编辑</el-button>
            <el-button link type="danger" @click="deleteModel(row)" :disabled="rowOpLoading" :loading="deletingId === row.id">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新增/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑模型' : '新增模型'" width="1400px" class="model-dialog">
      <el-form :model="form" :rules="rules" ref="formRef" label-width="100px">
        <el-form-item label="模型名称" prop="name">
          <el-input v-model="form.name" placeholder="请输入模型名称" />
        </el-form-item>
        <el-form-item label="模型角色" prop="model_role">
          <el-select v-model="form.model_role" placeholder="请选择模型角色" style="width: 100%">
            <el-option label="待评测模型" value="evaluation" />
            <el-option label="裁判模型" value="judge" />
          </el-select>
        </el-form-item>
        <el-form-item label="接入方式" prop="provider">
          <el-select v-model="form.provider" placeholder="请选择接入方式" style="width: 100%" @change="handleProviderChange">
            <el-option label="OpenAI" value="openai" />
            <el-option label="Anthropic" value="anthropic" />
            <el-option label="自定义HTTP" value="custom_http" />
          </el-select>
        </el-form-item>
        <el-form-item label="具体模型" prop="model_type" v-if="form.provider !== 'custom_http'">
          <el-input v-model="form.model_type" placeholder="请输入具体模型名称（如 gpt-4、claude-3-opus-20240229）" />
        </el-form-item>
        <el-form-item label="API端点" prop="endpoint">
          <el-input v-model="form.endpoint" placeholder="请输入API端点URL" />
        </el-form-item>
        <el-form-item label="API Key" prop="api_key">
          <!-- 新建模式：支持查看密码 -->
          <el-input v-if="!isEdit" v-model="form.api_key" type="password" placeholder="请输入API Key" show-password />
          <!-- 编辑模式：不可查看、不可复制，允许粘贴，显示脱敏占位 -->
          <el-input v-else v-model="form.api_key" type="password" :placeholder="isEdit ? '不修改请留空' : '请输入API Key'" autocomplete="off" @copy.prevent @cut.prevent @contextmenu.prevent />
        </el-form-item>
        <el-form-item label="鉴权方式" prop="auth_type">
          <el-select v-model="form.auth_type" placeholder="请选择鉴权方式" style="width: 100%" @change="handleAuthTypeChange">
            <el-option label="无鉴权" value="none" />
            <el-option label="API Key" value="api_key" />
            <el-option label="Bearer Token" value="bearer" />
            <el-option label="Basic Auth" value="basic" />
            <el-option label="OAuth 2.0" value="oauth" />
            <el-option label="自定义Header" value="custom_header" />
            <el-option label="Query参数" value="query" />
            <el-option label="登录接口Token" value="login_token" />
          </el-select>
        </el-form-item>

        <!-- 登录接口Token 鉴权配置 -->
        <template v-if="form.auth_type === 'login_token'">
          <el-divider content-position="left">
            登录接口配置
            <el-button type="text" @click="openCurlImportDialog('login_token')" style="margin-left: 10px;">
              <el-icon><Upload /></el-icon> 导入curl
            </el-button>
          </el-divider>
          <el-form-item label="登录URL">
            <el-input v-model="form.auth_config.login_url" placeholder="https://api.example.com/v1/auth/login" />
          </el-form-item>
          <el-form-item label="请求方法">
            <el-select v-model="form.auth_config.login_method" style="width: 100%">
              <el-option label="POST" value="POST" />
              <el-option label="GET" value="GET" />
            </el-select>
          </el-form-item>
          <el-form-item label="登录Headers">
            <JsonFieldEditor
              v-model:fields="loginHeadersConfig"
            />
          </el-form-item>
          <el-form-item label="登录Body" v-if="form.auth_config.login_method === 'POST'">
            <JsonFieldEditor
              v-model:fields="loginBodyConfig"
            />
          </el-form-item>
          <el-form-item label="Token Header">
            <el-input v-model="form.auth_config.token_header" placeholder="Authorization" />
          </el-form-item>
          <el-form-item label="Token前缀">
            <el-input v-model="form.auth_config.token_prefix" placeholder="Bearer " />
          </el-form-item>
          <el-form-item label="Token过期时间(秒)">
            <el-input-number v-model="form.auth_config.token_expires_in" :min="60" :max="86400" />
          </el-form-item>
          <el-divider content-position="left">响应解析配置</el-divider>
          <el-form-item label="解析方式">
            <el-select v-model="form.response_parse_config.parse_type" style="width: 100%">
              <el-option label="JSONPath" value="json_path" />
              <el-option label="正则表达式" value="json_regex" />
              <el-option label="文本匹配" value="text_match" />
            </el-select>
          </el-form-item>
          <el-form-item label="解析规则" v-if="form.response_parse_config.parse_type === 'json_path'">
            <el-input v-model="form.response_parse_config.parse_rule.content" placeholder="$.data.access_token" />
          </el-form-item>
          <el-form-item label="正则规则" v-if="form.response_parse_config.parse_type === 'json_regex'">
            <el-input v-model="form.response_parse_config.parse_rule.pattern" placeholder='"token":\s*"([^"]+)"' />
          </el-form-item>
        </template>
        <!-- 请求链配置 -->
        <template v-if="form.provider === 'custom_http'">
          <el-divider content-position="left">请求链配置</el-divider>
          <el-form-item label="启用请求链">
            <el-switch v-model="form.request_config.chain_requests_enabled" />
          </el-form-item>
          <template v-if="form.request_config.chain_requests_enabled">
            <el-form-item>
              <div class="request-chain-container">
                <!-- 左侧：请求列表 -->
                <div class="request-list-panel">
                  <div class="panel-header">
                    <span>请求列表</span>
                    <div class="panel-buttons">
                      <el-button type="primary" size="small" @click="addChainRequest">新增请求</el-button>
                      <el-button size="small" @click="openCurlImportDialog('chain_request')">curl</el-button>
                    </div>
                  </div>
                  <div class="request-list">
                    <div
                      v-for="(req, index) in chainRequests"
                      :key="index"
                      class="request-list-item"
                      :class="{ 'selected': selectedRequestIndex === index }"
                      draggable="true"
                      @dragstart="handleDragStart($event, index)"
                      @dragover="handleDragOver($event, index)"
                      @drop="handleDrop($event, index)"
                      @dragend="handleDragEnd"
                      @click="selectRequest(index)"
                    >
                      <el-icon class="drag-handle"><Rank /></el-icon>
                      <el-tooltip :content="req.name || `请求${index + 1}`" placement="top" :show-after="300">
                        <span class="request-name">{{ req.name || `请求${index + 1}` }}</span>
                      </el-tooltip>
                      <span class="request-method">{{ req.method || 'POST' }}</span>
                      <el-button link type="danger" size="small" @click.stop="removeChainRequest(index)">
                        <el-icon><Delete /></el-icon>
                      </el-button>
                    </div>
                    <div v-if="chainRequests.length === 0" class="empty-list">
                      <span>暂无请求，点击上方按钮添加</span>
                    </div>
                  </div>
                </div>
                <!-- 中间：选中请求的详情 -->
                <div class="request-detail-panel" v-if="selectedRequest">
                  <el-card shadow="hover">
                    <template #header>
                      <div class="request-chain-header">
                        <span>{{ selectedRequest.name || `请求 ${selectedRequestIndex + 1}` }}</span>
                        <el-button link type="danger" @click="removeChainRequest(selectedRequestIndex)">删除</el-button>
                      </div>
                    </template>
                    <el-form label-position="top" size="small">
                    <el-form-item label="别名">
                      <el-input v-model="selectedRequest.alias" placeholder="用于变量引用，如: login" />
                    </el-form-item>
                    <el-form-item label="名称">
                      <el-input v-model="selectedRequest.name" placeholder="请求名称" />
                    </el-form-item>
                    <el-form-item label="URL">
                      <el-input v-model="selectedRequest.url" placeholder="https://api.example.com/v1/..." />
                    </el-form-item>
                    <el-form-item label="方法">
                      <el-select v-model="selectedRequest.method" style="width: 100%">
                        <el-option label="GET" value="GET" />
                        <el-option label="POST" value="POST" />
                        <el-option label="PUT" value="PUT" />
                        <el-option label="DELETE" value="DELETE" />
                        <el-option label="PATCH" value="PATCH" />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="Headers">
                      <JsonFieldEditor
                        v-model:fields="selectedRequest.headersConfig"
                      />
                    </el-form-item>
                    <el-form-item label="Params">
                      <JsonFieldEditor
                        v-model:fields="selectedRequest.paramsConfig"
                      />
                    </el-form-item>
                    <el-form-item label="Body">
                      <JsonFieldEditor
                        v-model:fields="selectedRequest.bodyConfig"
                      />
                    </el-form-item>
                    <el-form-item label="输出变量">
                      <div class="output-vars-container">
                        <!-- SSE 模式开关 -->
                        <div class="sse-toggle" style="margin-bottom: 10px;">
                          <el-switch
                            v-model="selectedRequest.is_sse"
                            active-text="SSE流式响应"
                            inactive-text="普通JSON响应"
                            @change="(val) => handleSseToggle(selectedRequest, val)"
                          />
                        </div>
                        <!-- SSE 配置（仅 SSE 模式，且无多变量时显示） -->
                        <template v-if="selectedRequest.is_sse && (!selectedRequest.output_vars || selectedRequest.output_vars.length === 0)">
                          <div class="sse-config" style="margin-bottom: 10px;">
                            <el-input v-model="selectedRequest.sse_event_type" placeholder="事件类型，如: TestPointsGenerated" style="width: 200px; margin-right: 8px;" />
                            <el-input v-model="selectedRequest.sse_data_field" placeholder="数据字段，如: test_points" style="width: 200px;" />
                          </div>
                        </template>
                        <!-- 变量列表 -->
                        <template v-if="selectedRequest.is_sse">
                          <!-- SSE 模式：简化的变量配置（只需字段名） -->
                          <div v-for="(ov, ovIdx) in selectedRequest.output_vars" :key="ovIdx" class="output-var-item">
                            <el-input v-model="ov.name" placeholder="字段名（从SSE event data中提取）" style="width: 200px;" />
                            <el-input v-model="ov.default" placeholder="默认值" style="width: 100px;" />
                            <el-button type="danger" @click="removeOutputVar(selectedRequest, ovIdx)" :icon="Delete" circle />
                          </div>
                          <el-button type="primary" plain @click="addSseOutputVar(selectedRequest)" size="small">添加变量</el-button>
                        </template>
                        <template v-else>
                          <!-- 普通JSON模式：完整解析配置 -->
                          <div v-for="(ov, ovIdx) in selectedRequest.output_vars" :key="ovIdx" class="output-var-item">
                            <el-input v-model="ov.name" placeholder="变量名" style="width: 120px;" />
                            <el-select v-model="ov.parse_type" placeholder="解析类型" style="width: 100px;">
                              <el-option label="JSONPath" value="json_path" />
                              <el-option label="正则" value="json_regex" />
                              <el-option label="文本" value="text_match" />
                            </el-select>
                            <el-input v-model="ov.parse_rule" placeholder="解析规则，如: $.data.id" style="flex: 1;" />
                            <el-input v-model="ov.default" placeholder="默认值" style="width: 100px;" />
                            <el-button type="danger" @click="removeOutputVar(selectedRequest, ovIdx)" :icon="Delete" circle />
                          </div>
                          <el-button type="success" plain @click="openJsonPasteDialog" size="small" style="margin-right: 8px;">
  <el-icon><Upload /></el-icon> 粘贴JSON解析
</el-button>
<el-button type="primary" plain @click="addOutputVar(selectedRequest)" size="small">添加变量</el-button>
                        </template>
                      </div>
                    </el-form-item>
                    <el-alert type="info" :closable="false" style="margin-bottom: 10px;">
                      <strong>变量说明：</strong><br/>
                      <code>[[variable]]</code> - 外部变量（需手动输入，如 <code>[[project_id]]</code>）<br/>
                      <code>${uuid}</code> / <code>${timestamp}</code> / <code>${datetime}</code> - 自动生成
                    </el-alert>

                    <el-divider content-position="left">重试配置</el-divider>
                    <el-form-item label="重试次数">
                      <el-input-number v-model="selectedRequest.max_retries" :min="0" :max="20" />
                    </el-form-item>
                    <el-form-item label="重试间隔(ms)">
                      <el-input-number v-model="selectedRequest.retry_interval" :min="100" :max="30000" :step="100" />
                    </el-form-item>
                    <el-form-item label="失败继续">
                      <el-switch v-model="selectedRequest.continue_on_error" />
                    </el-form-item>
                    </el-form>
                  </el-card>
                </div>
                <!-- 无选中请求时的占位 -->
                <div v-else class="request-detail-placeholder">
                  <el-icon><InfoFilled /></el-icon>
                  <span>请从左侧选择一个请求进行编辑</span>
                </div>
                <!-- 右侧：变量面板 (可折叠) -->
                <div class="variable-panel-wrapper" v-if="showVariablePanel">
                  <ChainVariablePanel
                    :chain-variables="getVariablePanelData().chainVars"
                    :runtime-variables="getVariablePanelData().runtimeVars"
                    @var-click="(data) => handleVarPanelClick(data)"
                    @runtime-click="(data) => handleRuntimePanelClick(data)"
                    @close="showVariablePanel = false"
                  />
                </div>
                <div class="variable-panel-toggle" v-else>
                  <el-button link @click="showVariablePanel = true" title="显示变量面板">
                    <el-icon><InfoFilled /></el-icon>
                  </el-button>
                </div>
              </div>
            </el-form-item>
          </template>
        </template>
        <el-form-item label="支持多模态">
          <el-switch v-model="form.supports_multimodal" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="请输入描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" :disabled="submitting">取消</el-button>
        <el-button type="primary" @click="submitForm" :loading="submitting">确定</el-button>
      </template>
    </el-dialog>

    <!-- curl 导入弹窗 -->
    <el-dialog v-model="curlImportDialogVisible" title="导入curl命令" width="600px" @close="closeCurlImportDialog">
      <el-alert type="info" :closable="false" style="margin-bottom: 15px;">
        请粘贴完整的 curl 命令，系统将自动解析并填充配置。
      </el-alert>
      <el-input
        v-model="curlCommand"
        type="textarea"
        :rows="8"
        placeholder="请粘贴 curl 命令，例如：curl -X POST https://api.example.com/v1/auth/login -H 'Content-Type: application/json' -d '{&quot;username&quot;:&quot;admin&quot;,&quot;password&quot;:&quot;123456&quot;}'"
        style="font-family: monospace;"
      />
      <template #footer>
        <el-button @click="closeCurlImportDialog" :disabled="curlImportLoading">取消</el-button>
        <el-button type="primary" @click="confirmCurlImport" :loading="curlImportLoading">解析并导入</el-button>
      </template>
    </el-dialog>

    <!-- JSON粘贴解析对话框 -->
    <el-dialog
      v-model="jsonPasteDialogVisible"
      title="JSON粘贴解析"
      width="700px"
      :close-on-click-modal="false"
      class="json-paste-dialog"
    >
      <div style="margin-bottom: 15px;">
        <p style="color: #909399; font-size: 13px;">
          粘贴JSON响应示例，系统将自动解析所有字段路径。解析后的路径可直接填充到输出变量配置中。
        </p>
      </div>
      <el-input
        v-model="jsonPasteContent"
        type="textarea"
        :rows="8"
        placeholder="请粘贴JSON格式的响应数据，例如：
{
  &quot;code&quot;: 200,
  &quot;data&quot;: {
    &quot;items&quot;: [
      {&quot;id&quot;: 1, &quot;name&quot;: &quot;item1&quot;}
    ]
  }
}"
        style="font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace; font-size: 12px;"
      />
      <div v-if="jsonParseError" style="color: #f56c6c; margin-top: 10px; font-size: 13px;">
        {{ jsonParseError }}
      </div>
      <div v-if="parsedJsonPaths.length > 0" style="margin-top: 15px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
          <span style="font-weight: 500;">解析结果 ({{ parsedJsonPaths.length }} 个字段，已选 {{ selectedJsonPathIndices.length }} 个)</span>
          <div>
            <el-button size="small" @click="selectAllJsonPaths">全选</el-button>
            <el-button size="small" @click="clearAllJsonPaths">取消全选</el-button>
          </div>
        </div>
        <el-table
          ref="jsonPathsTableRef"
          :data="parsedJsonPaths"
          max-height="250"
          size="small"
          border
          @selection-change="handleJsonPathSelectionChange"
        >
          <el-table-column type="selection" width="40" />
          <el-table-column prop="path" label="JSONPath" min-width="200">
            <template #default="{ row }">
              <code style="color: #409eff;">{{ row.path }}</code>
            </template>
          </el-table-column>
          <el-table-column prop="value" label="示例值" min-width="150">
            <template #default="{ row }">
              <span style="color: #67c23a;">{{ row.value.length > 30 ? row.value.substring(0, 30) + '...' : row.value }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <template #footer>
        <div>
          <el-button @click="closeJsonPasteDialog">取消</el-button>
          <el-button @click="parseJsonContent" :disabled="!jsonPasteContent.trim()">解析JSON</el-button>
          <el-button type="primary" @click="confirmJsonParseResult(selectedRequest)" :disabled="parsedJsonPaths.length === 0">
            填充到输出变量
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 测试连接弹窗（合并变量输入和结果展示） -->
    <el-dialog v-model="testDialogVisible" title="测试模型连接" width="800px" :close-on-click-modal="!testingLoading" @close="closeTestDialog">
      <!-- 变量输入区域（仅在没有运行时变量时隐藏，或始终显示） -->
      <div v-if="!testingLoading && !testStarted" class="test-input-section">
        <el-alert type="info" :closable="false" style="margin-bottom: 15px;">
          该模型的请求链中使用了动态变量，请输入变量值。后端会根据选择的类型进行转换。
        </el-alert>
        <el-form v-if="requiredVariables.length > 0">
          <el-form-item v-for="varName in requiredVariables" :key="varName" :label="varName">
            <div style="display: flex; gap: 10px; width: 100%;">
              <el-select v-model="testVariables[varName].type" style="width: 100px;">
                <el-option label="字符串" value="string" />
                <el-option label="数字" value="number" />
                <el-option label="布尔值" value="boolean" />
              </el-select>
              <el-input
                v-model="testVariables[varName].value"
                :placeholder="'请输入 ' + varName"
                style="flex: 1;"
              />
            </div>
          </el-form-item>
        </el-form>
        <el-alert v-else type="info" :closable="false">
          点击"开始测试"按钮进行测试
        </el-alert>
      </div>

      <!-- 测试结果区域 -->
      <div v-if="testStarted" class="test-result-section">
        <el-scrollbar style="height: 400px;">
          <div class="result-content">
            <!-- 加载中状态（WebSocket已连接但还没有日志） -->
            <div v-if="testingLoading && testLogs.length === 0" class="log-loading">
              <el-icon class="is-loading"><Loading /></el-icon>
              <span>等待响应...</span>
            </div>

            <!-- 空状态（测试已结束但无日志） -->
            <div v-if="!testingLoading && testLogs.length === 0" class="log-empty">
              <span>暂无日志</span>
            </div>

            <div v-for="(log, index) in testLogs" :key="index" class="result-log-item">
              <!-- 开始消息 -->
              <div v-if="log.type === 'start'" class="log-start">
                <el-icon><Loading /></el-icon>
                <span>{{ log.content }}</span>
              </div>

              <!-- 请求详情 -->
              <div v-else-if="log.type === 'request_detail'" class="log-request">
                <el-divider content-position="left">请求 {{ log.request_index }}: {{ log.name }}</el-divider>
                <el-descriptions :column="2" border size="small">
                  <el-descriptions-item label="URL">{{ log.url }}</el-descriptions-item>
                  <el-descriptions-item label="方法">{{ log.method }}</el-descriptions-item>
                  <el-descriptions-item label="输出变量" v-if="log.output_var">{{ log.output_var }}: {{ log.output_value }}</el-descriptions-item>
                </el-descriptions>
                <div v-if="log.headers && Object.keys(log.headers).length > 0" style="margin-top: 8px;">
                  <strong>请求头:</strong>
                  <pre class="code-block">{{ JSON.stringify(log.headers, null, 2) }}</pre>
                </div>
                <div v-if="log.request_params && Object.keys(log.request_params).length > 0" style="margin-top: 8px;">
                  <strong>请求Params:</strong>
                  <pre class="code-block">{{ JSON.stringify(log.request_params, null, 2) }}</pre>
                </div>
                <div v-if="log.request_body && Object.keys(log.request_body).length > 0" style="margin-top: 8px;">
                  <strong>请求体:</strong>
                  <pre class="code-block">{{ JSON.stringify(log.request_body, null, 2) }}</pre>
                </div>
                <div v-if="log.response" style="margin-top: 8px;">
                  <strong>响应:</strong>
                  <el-tag :type="log.response.success ? 'success' : 'danger'" size="small" style="margin-left: 8px;">
                    {{ log.response.success ? '成功' : '失败' }}
                  </el-tag>
                  <pre v-if="log.response.content" class="code-block">{{ log.response.content.substring(0, 500) }}{{ log.response.content.length > 500 ? '...' : '' }}</pre>
                  <pre v-else-if="log.response.error" class="code-block error">{{ log.response.error }}</pre>
                </div>
              </div>

              <!-- 成功消息 -->
              <div v-else-if="log.type === 'success'" class="log-success">
                <el-icon color="#67c23a"><CircleCheck /></el-icon>
                <span>{{ log.content }}</span>
              </div>

              <!-- 响应内容 -->
              <div v-else-if="log.type === 'response'" class="log-response">
                <strong>响应内容:</strong>
                <pre class="code-block">{{ log.content }}</pre>
              </div>

              <!-- 错误消息 -->
              <div v-else-if="log.type === 'error'" class="log-error">
                <el-icon color="#f56c6c"><CircleClose /></el-icon>
                <span>{{ log.content }}</span>
              </div>

              <!-- 完成消息 -->
              <div v-else-if="log.type === 'complete'" class="log-complete">
                <el-icon><Finished /></el-icon>
                <span>{{ log.content }}</span>
              </div>
            </div>
          </div>
        </el-scrollbar>
      </div>

      <template #footer>
        <el-button @click="closeTestDialog" :disabled="testingLoading">关闭</el-button>
        <el-button v-if="!testStarted" type="primary" @click="startTest" :loading="testingLoading">开始测试</el-button>
        <el-button v-else type="primary" @click="startTest" :disabled="testingLoading" :loading="testingLoading">重新测试</el-button>
      </template>
    </el-dialog>

    <!-- 双引擎裁判对拍弹窗（M8 QE8-2）：同批既有输出交 CC × DSH 两引擎评分 -->
    <el-dialog v-model="compareDialog.visible" title="双引擎裁判对拍（Claude Code × DeepSeek）" width="860px" :close-on-click-modal="!compareDialog.loading">
      <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom: 12px;">
        <el-select v-model="compareDialog.taskId" placeholder="选择使用裁判的已完成任务" style="width: 320px;" filterable>
          <el-option v-for="t in compareTasks" :key="t.id" :label="t.name" :value="t.id" />
        </el-select>
        <el-input-number v-model="compareDialog.sampleCount" :min="1" :max="10" />
        <span style="font-size:12px; color:#909399;">样本数（取该任务最新执行的行级结果）</span>
        <el-button type="primary" :loading="compareDialog.loading" :disabled="!compareDialog.taskId" @click="runJudgeComparison">开始对拍</el-button>
      </div>

      <div v-if="compareDialog.report" v-loading="compareDialog.loading">
        <!-- 汇总卡 -->
        <el-row :gutter="12" style="margin-bottom: 12px;">
          <el-col :span="6"><el-statistic title="对拍样本" :value="compareDialog.report.summary.sample_count" /></el-col>
          <el-col :span="6"><el-statistic title="平均绝对分差" :value="compareDialog.report.summary.mean_abs_diff ?? '—'" /></el-col>
          <el-col :span="6">
            <el-statistic title="容差内一致率（≤1分）" :value="compareDialog.report.summary.within_tolerance_rate != null ? (compareDialog.report.summary.within_tolerance_rate * 100).toFixed(1) + '%' : '—'" />
          </el-col>
          <el-col :span="6">
            <el-statistic title="失败（CC / DSH）" :value="`${compareDialog.report.summary.claude_failed} / ${compareDialog.report.summary.deepseek_failed}`" />
          </el-col>
        </el-row>
        <!-- 引擎配置摘要 -->
        <div style="font-size:12px; color:#909399; margin-bottom: 8px;">
          Claude Code：{{ compareDialog.report.engines.claude_agent?.model || '—' }} ·
          DeepSeek：{{ compareDialog.report.engines.deepseek_agent?.model || '—' }}（{{ compareDialog.report.engines.deepseek_agent?.protocol || 'deepseek' }} 协议）
        </div>
        <!-- 逐行对照 -->
        <el-table :data="compareDialog.report.rows" stripe size="small" max-height="320">
          <el-table-column label="行" width="60">
            <template #default="{ row }">{{ row.data_index + 1 }}</template>
          </el-table-column>
          <el-table-column prop="user_input" label="输入（截断）" min-width="200" show-overflow-tooltip />
          <el-table-column label="Claude Code" width="110" align="right">
            <template #default="{ row }">
              <span v-if="row.claude_agent.score != null">{{ row.claude_agent.score }}</span>
              <el-tag v-else size="small" type="danger">失败</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="DeepSeek" width="110" align="right">
            <template #default="{ row }">
              <span v-if="row.deepseek_agent.score != null">{{ row.deepseek_agent.score }}</span>
              <el-tag v-else size="small" type="danger">失败</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="分差 (CC−DSH)" width="120" align="right">
            <template #default="{ row }">
              <span v-if="row.score_diff != null" :style="{ color: Math.abs(row.score_diff) > 1 ? '#e6a23c' : '#67c23a', fontWeight: 600 }">
                {{ row.score_diff > 0 ? '+' : '' }}{{ row.score_diff }}
              </span>
              <span v-else style="color:#c0c4cc;">—</span>
            </template>
          </el-table-column>
          <el-table-column label="原执行均分" width="100" align="right">
            <template #default="{ row }">{{ row.existing_average_score ?? '—' }}</template>
          </el-table-column>
        </el-table>
      </div>
      <el-empty
        v-else-if="!compareDialog.loading"
        description="选任务后开始对拍：同一批既有模型输出分别交两引擎评分，产出分数差与一致率报告（DeepSeek 裁判接通的回归验收）"
        :image-size="70"
      />
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, inject, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Loading, CircleCheck, CircleClose, Finished, Delete, Rank, InfoFilled, Upload } from '@element-plus/icons-vue'
import { modelApi, claudeCodeApi, deepSeekHarnessApi, taskApi } from '@/api'
import { formatTime } from '@/utils/time'
import JsonFieldEditor from '@/components/JsonFieldEditor.vue'
import ChainVariablePanel from '@/components/ChainVariablePanel.vue'
import appConfig from '@/config'
import { canWrite } from '@/utils/permissions'

// M4 QE4-3：viewer 只读——写按钮禁用联动（canWrite 见 utils/permissions）
const currentUser = inject('currentUser', null)
const readonlyRole = computed(() => !canWrite(currentUser.value))

const models = ref([])
const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const roleFilter = ref('')

// curl 导入相关
const curlImportDialogVisible = ref(false)
const curlImportLoading = ref(false)
const curlImportType = ref('login_token')  // 'login_token' | 'chain_request'
const curlImportTarget = ref(null)  // 导入目标：chain request index or null
const curlCommand = ref('')

// 写操作 loading 反馈
const submitting = ref(false)   // 新建/编辑弹窗提交中
const deletingId = ref(null)    // 正在删除的模型 id
const rowOpLoading = computed(() => deletingId.value !== null)  // 任一行删除中，禁用列表操作列全部按钮

// 动态JSON配置相关
const loginHeadersConfig = ref([])
const loginBodyConfig = ref([])

// 字段类型选项
const fieldTypeOptions = [
  { label: '字符串', value: 'string' },
  { label: '数字', value: 'number' },
  { label: '布尔值', value: 'boolean' },
  { label: '对象', value: 'object' },
  { label: '数组', value: 'array' },
  { label: 'null', value: 'null' }
]

// 请求链相关变量
const chainRequests = ref([])
const chainRequestsJson = ref('')

// JSON粘贴解析相关变量
const jsonPasteDialogVisible = ref(false)  // 对话框显示状态
const jsonPasteContent = ref('')  // 用户粘贴的JSON内容
const parsedJsonPaths = ref([])  // 解析后的JSONPath列表
const selectedJsonPathIndices = ref([])  // 用户选择的字段索引
const jsonParseError = ref('')  // JSON解析错误信息

// 测试连接相关变量
const testDialogVisible = ref(false)
const testVariables = ref({})
const lastTestVariables = ref({})  // 存储上次测试时的变量值，用于再次打开时带入
const requiredVariables = ref([])
const testingModelId = ref(null)
const testingLoading = ref(false)
const testStarted = ref(false)  // 是否已开始测试
const testLogs = ref([])  // 测试日志
const ws = ref(null)  // WebSocket 连接
const testResultDetails = ref(null)
const testResultDialogVisible = ref(false)
const activeTab = ref('0')

const form = ref({
  name: '',
  provider: 'openai',
  model_type: '',
  model_role: 'evaluation',
  endpoint: '',
  api_key: '',
  auth_type: 'none',
  auth_config: {},
  response_parse_config: {},
  request_config: {
    chain_requests_enabled: false,
    chain_requests: { enabled: false, requests: [] }
  },
  supports_multimodal: false,
  description: ''
})

const rules = {
  name: [{ required: true, message: '请输入模型名称', trigger: 'blur' }],
  provider: [{ required: true, message: '请选择接入方式', trigger: 'change' }],
  model_type: [{ required: true, message: '请输入具体模型名称', trigger: 'blur' }],
  model_role: [{ required: true, message: '请选择模型角色', trigger: 'change' }]
}

// ==================== Claude Code 裁判配置（模块 B） ====================

// 裁判配置区当前激活的 tab：cc=Claude Code，dsh=DeepSeek Harness
const judgeActiveTab = ref('cc')

// Claude Code 裁判默认配置表单（未配置时的默认值）
const getDefaultCcForm = () => ({
  name: 'Claude Code 裁判',
  base_url: '',
  auth_token: '',
  model: '',
  timeout_seconds: 300,  // 单次裁判调用超时(秒)
  max_turns: 30,         // Agent 裁判最大轮次
  proxy_url: '',
  enable_script_mcp: false  // 脚本 MCP 工具化开关（设计 §十二 二期，默认关闭）
})

// 裁判配置卡片状态
const ccLoading = ref(false)
const ccSaving = ref(false)
const ccTesting = ref(false)
const ccConfigured = ref(false)   // 后端是否已配置
const ccFormRef = ref(null)
const ccTestResult = ref(null)    // 测试连接结果

// Claude Code 裁判配置表单（name 由后端返回，界面不展示）
const ccForm = ref(getDefaultCcForm())

// 额外环境变量 extra_env 的动态字段配置（复用 JsonFieldEditor，空则为 {}）
const ccExtraEnvConfig = ref([])

const ccRules = {
  base_url: [{ required: true, message: '请输入 API 网关地址', trigger: 'blur' }],
  model: [{ required: true, message: '请输入模型名称', trigger: 'blur' }]
}

// 网关连通性（HTTP）测试结果
const ccHttpAlert = computed(() => {
  const http = ccTestResult.value?.steps?.http
  if (!http) return null
  return http.success
    ? { type: 'success', title: `网关连通性：${http.message || '验证成功'}` }
    : { type: 'error', title: `网关连通性：${http.message || '验证失败'}` }
})

// Agent CLI 链路测试结果
const ccAgentAlert = computed(() => {
  const agent = ccTestResult.value?.steps?.agent
  if (!agent) return null
  if (agent.skipped) {
    return { type: 'info', title: `Agent CLI 链路：已跳过（${agent.message || '未启用验证'}）` }
  }
  if (agent.success) {
    return { type: 'success', title: `Agent CLI 链路：${agent.message || '验证成功'}` }
  }
  return {
    type: 'warning',
    title: `Agent CLI 链路：${agent.message || '验证失败'}。图模式可用，但 Claude Code Agent 引擎需检查 CLI 环境`
  }
})

// 加载 Claude Code 裁判配置
const loadCcConfig = async () => {
  ccLoading.value = true
  try {
    const res = await claudeCodeApi.getConfig()
    ccConfigured.value = !!res.configured
    const cfg = res.config
    if (cfg) {
      ccForm.value = {
        name: cfg.name || 'Claude Code 裁判',
        base_url: cfg.base_url || '',
        // auth_token 为后端返回的脱敏值，用户不修改则原样提交（后端不会更新）
        auth_token: cfg.auth_token || '',
        model: cfg.model || '',
        timeout_seconds: cfg.timeout_seconds != null ? cfg.timeout_seconds : 300,
        max_turns: cfg.max_turns != null ? cfg.max_turns : 30,
        proxy_url: cfg.proxy_url || '',
        // 脚本 MCP 工具化开关（后端 GET 返回布尔，缺省视为关）
        enable_script_mcp: !!cfg.enable_script_mcp
      }
      // extra_env 可能是对象或 JSON 字符串，统一安全解析
      ccExtraEnvConfig.value = jsonToConfig(parseJsonField(cfg.extra_env))
    } else {
      // 未配置时显示默认值
      ccForm.value = getDefaultCcForm()
      ccExtraEnvConfig.value = []
    }
  } catch (e) {
    ElMessage.error(e.message || '加载 Claude Code 裁判配置失败')
  } finally {
    ccLoading.value = false
  }
}

// 保存 Claude Code 裁判配置
const saveCcConfig = async () => {
  try {
    await ccFormRef.value.validate()
  } catch (e) {
    return
  }
  ccSaving.value = true
  try {
    await claudeCodeApi.update({
      ...ccForm.value,
      proxy_url: ccForm.value.proxy_url || '',
      extra_env: configToJson(ccExtraEnvConfig.value),
      // 显式提交开关状态：后端 PUT 为全量语义（缺省即 False），漏提交会把已开启的
      // 脚本 MCP 工具化开关静默关闭（二期 UI 验收缺陷）
      enable_script_mcp: !!ccForm.value.enable_script_mcp
    })
    ElMessage.success('配置已保存')
    // 重新加载配置状态（刷新脱敏 token 与配置标记）
    await loadCcConfig()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    ccSaving.value = false
  }
}

// 测试 Claude Code 裁判连接（网关连通性 + Agent CLI 链路）
const testCcConfig = async () => {
  ccTesting.value = true
  ccTestResult.value = null
  try {
    const res = await claudeCodeApi.test()
    ccTestResult.value = res
    if (res.success) {
      ElMessage.success('连接测试通过')
    }
  } catch (e) {
    ElMessage.error(e.message || '测试连接失败')
  } finally {
    ccTesting.value = false
  }
}

// ==================== DeepSeek Harness 裁判配置 ====================

// DeepSeek Harness 裁判默认配置表单（未配置时的默认值）
const getDefaultDshForm = () => ({
  name: '默认配置',
  base_url: '',
  api_key: '',
  protocol: 'deepseek',
  provider: 'deepseek-official',
  model: 'deepseek-v4-flash',
  profile: 'sdk-minimal',
  timeout_seconds: 300,  // 单次 Agent 调用超时(秒)
  max_tokens: 49152      // 单次请求最大 token 数
})

// DSH 配置卡片状态
const dshLoading = ref(false)
const dshSaving = ref(false)
const dshTesting = ref(false)
const dshConfigured = ref(false)  // 后端是否已配置
const dshFormRef = ref(null)
const dshTestResult = ref(null)   // 测试连接结果
const dshForm = ref(getDefaultDshForm())

// profile 可选项（SDK 随附 profile，允许自定义输入）
const dshProfileOptions = ['sdk-minimal', 'sdk', 'headless', 'web', 'acp']

// 额外环境变量 extra_env 的动态字段配置（复用 JsonFieldEditor，空则为 {}）
const dshExtraEnvConfig = ref([])

// 额外 patch 列表（JSON 字符串数组文本，保存时解析校验）
const dshExtraPatchesText = ref('[]')

const dshRules = {
  provider: [{ required: true, message: '请输入 Provider', trigger: 'blur' }],
  model: [{ required: true, message: '请输入模型名称', trigger: 'blur' }]
}

// 第一步 HTTP 直调测试结果
const dshHttpAlert = computed(() => {
  const http = dshTestResult.value?.steps?.http
  if (!http) return null
  if (http.skipped) {
    return { type: 'info', title: `HTTP 直调：已跳过（${http.message || '未配置 base_url/api_key'}）` }
  }
  return http.success
    ? { type: 'success', title: `HTTP 直调：${http.message || '验证成功'}` }
    : { type: 'error', title: `HTTP 直调：${http.message || '验证失败'}` }
})

// 第二步 DSH Agent 最小 run 测试结果
const dshAgentAlert = computed(() => {
  const agent = dshTestResult.value?.steps?.agent
  if (!agent) return null
  if (agent.skipped) {
    return { type: 'info', title: `Agent 链路：已跳过（${agent.message || '未启用验证'}）` }
  }
  if (agent.success) {
    return { type: 'success', title: `Agent 链路：${agent.message || '验证成功'}` }
  }
  return { type: 'error', title: `Agent 链路：${agent.message || '验证失败'}` }
})

// 加载 DeepSeek Harness 裁判配置
const loadDshConfig = async () => {
  dshLoading.value = true
  try {
    const res = await deepSeekHarnessApi.getConfig()
    dshConfigured.value = !!res.configured
    const cfg = res.config
    if (cfg) {
      dshForm.value = {
        name: cfg.name || '默认配置',
        base_url: cfg.base_url || '',
        // api_key 为后端返回的脱敏值，用户不修改则原样提交（后端识别 **** 不更新）
        api_key: cfg.api_key || '',
        provider: cfg.provider || 'deepseek-official',
        protocol: cfg.protocol || 'deepseek',
        model: cfg.model || '',
        profile: cfg.profile || 'sdk-minimal',
        timeout_seconds: cfg.timeout_seconds != null ? cfg.timeout_seconds : 300,
        max_tokens: cfg.max_tokens != null ? cfg.max_tokens : 49152
      }
      // extra_env 可能是对象或 JSON 字符串，统一安全解析
      dshExtraEnvConfig.value = jsonToConfig(parseJsonField(cfg.extra_env))
      dshExtraPatchesText.value = JSON.stringify(cfg.extra_patches || [], null, 2)
    } else {
      // 未配置时显示默认值
      dshForm.value = getDefaultDshForm()
      dshExtraEnvConfig.value = []
      dshExtraPatchesText.value = '[]'
    }
  } catch (e) {
    ElMessage.error(e.message || '加载 DeepSeek Harness 裁判配置失败')
  } finally {
    dshLoading.value = false
  }
}

// 解析额外 patch 文本（必须是 JSON 字符串数组）
const parseExtraPatches = () => {
  const text = (dshExtraPatchesText.value || '').trim()
  if (!text) return []
  const parsed = JSON.parse(text)
  if (!Array.isArray(parsed)) {
    throw new Error('需为 JSON 字符串数组')
  }
  return parsed.map(item => String(item))
}

// 保存 DeepSeek Harness 裁判配置
const saveDshConfig = async () => {
  try {
    await dshFormRef.value.validate()
  } catch (e) {
    return
  }
  let extraPatches = []
  try {
    extraPatches = parseExtraPatches()
  } catch (e) {
    ElMessage.error('额外 Patch 格式错误：需为 JSON 字符串数组，如 ["my-patch-id"]')
    return
  }
  dshSaving.value = true
  try {
    await deepSeekHarnessApi.update({
      ...dshForm.value,
      extra_env: configToJson(dshExtraEnvConfig.value),
      extra_patches: extraPatches
    })
    ElMessage.success('配置已保存')
    // 重新加载配置状态（刷新脱敏 api_key 与配置标记）
    await loadDshConfig()
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  } finally {
    dshSaving.value = false
  }
}

// 测试 DeepSeek Harness 连接（HTTP 直调 + DSH Agent 最小 run，分步展示）
const testDshConfig = async () => {
  dshTesting.value = true
  dshTestResult.value = null
  try {
    const res = await deepSeekHarnessApi.test()
    dshTestResult.value = res
    if (res.success) {
      ElMessage.success('连接测试通过')
    }
  } catch (e) {
    ElMessage.error(e.message || '测试连接失败')
  } finally {
    dshTesting.value = false
  }
}

// ==================== 双引擎裁判对拍（M8 QE8-2） ====================
const compareDialog = reactive({
  visible: false,
  taskId: '',
  sampleCount: 5,
  loading: false,
  report: null,
})
const compareTasks = ref([])

const openJudgeComparison = async () => {
  compareDialog.visible = true
  compareDialog.report = null
  if (!compareTasks.value.length) {
    try {
      const res = await taskApi.list({ limit: 200 })
      // 只列使用裁判的任务（judge/agent + 启用裁判的一致性），且有执行历史可锚定
      compareTasks.value = (res || []).filter(
        t => t.status === 'completed' && ['judge', 'agent'].includes(t.evaluation_type))
    } catch (e) {
      console.error(e)
    }
  }
}

const runJudgeComparison = async () => {
  if (!compareDialog.taskId || compareDialog.loading) return
  compareDialog.loading = true
  try {
    compareDialog.report = await taskApi.judgeComparison(compareDialog.taskId, {
      sample_count: compareDialog.sampleCount,
    })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message || '对拍失败')
  } finally {
    compareDialog.loading = false
  }
}

// 处理接入方式变化
const handleProviderChange = (value) => {
  if (value === 'custom_http') {
    form.value.model_type = 'custom_http'
  } else {
    form.value.model_type = ''
  }
}

// 处理鉴权类型变化
const handleAuthTypeChange = (value) => {
  // 初始化 request_config（如果不存在）
  if (!form.value.request_config) {
    form.value.request_config = {}
  }
  if (value === 'login_token') {
    form.value.auth_config = {
      login_url: '',
      login_method: 'POST',
      login_headers: {},
      login_body: {},
      token_header: 'Authorization',
      token_prefix: 'Bearer ',
      token_expires_in: 3600
    }
    form.value.response_parse_config = {
      parse_type: 'json_path',
      parse_rule: { content: '' }
    }
    // 初始化动态配置
    loginHeadersConfig.value = []
    loginBodyConfig.value = []
  } else {
    form.value.auth_config = {}
    form.value.response_parse_config = {}
  }
}

// ==================== 动态JSON配置相关方法 ====================

// 安全解析JSON，支持字符串或对象格式
const parseJsonField = (field) => {
  if (!field) return {}
  if (typeof field === 'object') return field
  if (typeof field === 'string') {
    try {
      return JSON.parse(field)
    } catch (e) {
      console.warn('JSON解析失败:', e)
      return {}
    }
  }
  return {}
}

// 将JSON对象转换为配置数组（用于渲染）
const jsonToConfig = (jsonObj, parentPath = '') => {
  if (!jsonObj || typeof jsonObj !== 'object') {
    return []
  }

  const config = []

  const parseValue = (value, path) => {
    if (value === null) {
      return { type: 'null', value: 'null' }
    } else if (Array.isArray(value)) {
      return {
        type: 'array',
        value: value.map((item, idx) => {
          if (typeof item === 'object' && item !== null) {
            return {
              type: 'object',
              children: jsonToConfig(item, `${path}[${idx}]`),
              isExpanded: true
            }
          }
          return { type: getType(item), value: String(item) }
        }),
        isExpanded: true
      }
    } else if (typeof value === 'object') {
      return {
        type: 'object',
        children: jsonToConfig(value, path),
        isExpanded: true
      }
    } else {
      return { type: getType(value), value: String(value) }
    }
  }

  const getType = (val) => {
    if (val === null) return 'null'
    if (typeof val === 'number') return 'number'
    if (typeof val === 'boolean') return 'boolean'
    if (Array.isArray(val)) return 'array'
    if (typeof val === 'object') return 'object'
    return 'string'
  }

  for (const key in jsonObj) {
    const value = jsonObj[key]
    config.push({
      key: key,
      ...parseValue(value, parentPath ? `${parentPath}.${key}` : key)
    })
  }

  return config
}

// 将配置数组转换为JSON对象（用于保存）
const configToJson = (config) => {
  const result = {}

  const processField = (field) => {
    let value

    switch (field.type) {
      case 'string':
        value = field.value
        break
      case 'number':
        value = field.value === '' ? 0 : Number(field.value)
        break
      case 'boolean':
        value = field.value === 'true' || field.value === true
        break
      case 'null':
        value = null
        break
      case 'array':
        value = (field.value || []).map(item => {
          if (item.type === 'object' && item.children) {
            return processObject(item.children)
          }
          return item.value
        })
        break
      case 'object':
        value = processObject(field.children || [])
        break
      default:
        value = field.value
    }

    return value
  }

  const processObject = (fields) => {
    const obj = {}
    for (const field of fields) {
      obj[field.key] = processField(field)
    }
    return obj
  }

  for (const field of config) {
    result[field.key] = processField(field)
  }

  return result
}

// 添加字段
const addField = (config, parentType = 'object') => {
  if (parentType === 'array') {
    config.push({
      type: 'string',
      value: ''
    })
  } else {
    config.push({
      key: `field_${config.length + 1}`,
      type: 'string',
      value: ''
    })
  }
}

// 删除字段
const removeField = (config, index) => {
  config.splice(index, 1)
}

// 添加数组项
const addArrayItem = (field) => {
  if (!field.value) {
    field.value = []
  }
  field.value.push({
    type: 'string',
    value: ''
  })
}

// 删除数组项
const removeArrayItem = (field, index) => {
  field.value.splice(index, 1)
}

// 初始化登录Headers配置
const initLoginHeadersConfig = () => {
  try {
    const headers = form.value.auth_config?.login_headers || {}
    loginHeadersConfig.value = jsonToConfig(headers)
  } catch (e) {
    loginHeadersConfig.value = []
  }
}

// 初始化登录Body配置
const initLoginBodyConfig = () => {
  try {
    const body = form.value.auth_config?.login_body || {}
    loginBodyConfig.value = jsonToConfig(body)
  } catch (e) {
    loginBodyConfig.value = []
  }
}

// 保存时将配置转换为JSON
const saveLoginHeadersConfig = () => {
  form.value.auth_config.login_headers = configToJson(loginHeadersConfig.value)
}

const saveLoginBodyConfig = () => {
  form.value.auth_config.login_body = configToJson(loginBodyConfig.value)
}

// ==================== 请求链JSON配置相关方法 ====================

// 请求链相关方法
const addChainRequest = () => {
  chainRequests.value.push({
    name: '',
    order: chainRequests.value.length + 1,
    url: '',
    method: 'POST',
    headers: {},
    headers_json: '{}',
    headersConfig: [],  // 动态配置
    body: {},
    body_json: '{}',
    bodyConfig: [],    // 动态配置
    params: {},
    params_json: '{}',
    paramsConfig: [],  // 动态配置
    output_vars: [],  // 多变量模式
    is_sse: false,  // 是否为 SSE 流式响应
    response_type: 'json',  // json 或 sse
    response_parse_type: 'json_path',
    response_parse_content: '',
    response_parse_pattern: '',
    // SSE 流式响应配置
    sse_event_type: '',
    sse_data_field: '',
    max_retries: 0,
    retry_interval: 1000,
    continue_on_error: false
  })
}

// JSON路径递归解析函数
const parseJsonToPaths = (obj, currentPath = '$') => {
  const paths = []

  if (obj === null || obj === undefined) {
    return paths
  }

  if (Array.isArray(obj)) {
    // 数组类型
    obj.forEach((item, index) => {
      const itemPath = `${currentPath}[${index}]`
      paths.push({ path: itemPath, value: JSON.stringify(item) })
      if (typeof item === 'object' && item !== null) {
        paths.push(...parseJsonToPaths(item, itemPath))
      }
    })
  } else if (typeof obj === 'object') {
    // 对象类型
    Object.keys(obj).forEach(key => {
      const keyPath = `${currentPath}.${key}`
      paths.push({ path: keyPath, value: JSON.stringify(obj[key]) })
      if (typeof obj[key] === 'object' && obj[key] !== null) {
        paths.push(...parseJsonToPaths(obj[key], keyPath))
      }
    })
  }

  return paths
}

// 从JSON内容解析出字段路径
const parseJsonContent = () => {
  const content = jsonPasteContent.value.trim()
  if (!content) {
    jsonParseError.value = '请输入JSON内容'
    parsedJsonPaths.value = []
    return
  }

  try {
    const parsed = JSON.parse(content)
    parsedJsonPaths.value = parseJsonToPaths(parsed)
    // 清空选择状态
    selectedJsonPathIndices.value = []
    jsonParseError.value = ''
  } catch (e) {
    jsonParseError.value = `JSON解析失败: ${e.message}`
    parsedJsonPaths.value = []
  }
}

const jsonPathsTableRef = ref(null)

// 处理选择变化
const handleJsonPathSelectionChange = (selection) => {
  selectedJsonPathIndices.value = selection.map(row => row.path)
}

// 全选
const selectAllJsonPaths = () => {
  jsonPathsTableRef.value?.toggleAllSelection()
}

// 取消全选
const clearAllJsonPaths = () => {
  jsonPathsTableRef.value?.clearSelection()
}

// 添加输出变量
const addOutputVar = (req) => {
  if (!req.output_vars) {
    req.output_vars = []
  }
  req.output_vars.push({
    name: '',
    parse_type: 'json_path',
    parse_rule: '',
    default: ''
  })
}

// 添加 SSE 输出变量（简化配置）
const addSseOutputVar = (req) => {
  if (!req.output_vars) {
    req.output_vars = []
  }
  req.output_vars.push({
    name: '',
    default: ''
  })
}

// 打开JSON粘贴解析对话框
const openJsonPasteDialog = () => {
  jsonPasteContent.value = ''
  parsedJsonPaths.value = []
  jsonParseError.value = ''
  jsonPasteDialogVisible.value = true
}

// 关闭JSON粘贴解析对话框
const closeJsonPasteDialog = () => {
  jsonPasteDialogVisible.value = false
}

// 确认解析结果，填充到output_vars
const confirmJsonParseResult = (req) => {
  if (parsedJsonPaths.value.length === 0) {
    ElMessage.warning('没有可填充的字段路径')
    return
  }

  // 检查是否有选中的字段
  if (selectedJsonPathIndices.value.length === 0) {
    ElMessage.warning('请先选择要填充的字段')
    return
  }

  if (!req.output_vars) {
    req.output_vars = []
  }

  // 获取已存在的变量名集合（用于去重）
  const existingNames = new Set(req.output_vars.map(ov => ov.name))
  let addedCount = 0
  let skippedCount = 0

  // 只处理选中的字段
  const selectedPaths = parsedJsonPaths.value.filter(item => selectedJsonPathIndices.value.includes(item.path))

  // 将解析结果添加到output_vars
  selectedPaths.forEach(item => {
    const fieldName = extractFieldName(item.path)  // 从路径提取字段名
    // 检查是否已存在同名变量
    if (existingNames.has(fieldName)) {
      skippedCount++
      return  // 跳过已存在的变量
    }
    req.output_vars.push({
      name: fieldName,
      parse_type: 'json_path',
      parse_rule: item.path,
      default: ''
    })
    existingNames.add(fieldName)
    addedCount++
  })

  if (addedCount > 0) {
    ElMessage.success(`已添加 ${addedCount} 个输出变量${skippedCount > 0 ? `，跳过 ${skippedCount} 个已存在的变量` : ''}`)
  } else {
    ElMessage.warning('所有变量均已存在，无需重复添加')
  }
  closeJsonPasteDialog()
}

// 从JSONPath提取字段名（取最后一个路径段的key）
const extractFieldName = (jsonPath) => {
  // 例如: $.data.items[0].id -> id, $.data[0] -> data, $[0] -> item0
  const segments = jsonPath.split('.')
  let lastSegment = segments[segments.length - 1]
  // 移除数组索引 [0] 等
  lastSegment = lastSegment.replace(/\[\d+\]$/, '')
  // 如果移除数组索引后为空（根数组场景如 $[0]），使用上一级路径段并加上 item 前缀
  if (!lastSegment && segments.length > 1) {
    const parentSegment = segments[segments.length - 2]
    // 如果上一级也是数组索引格式（如 items[0]），提取前面的名称
    const match = parentSegment.match(/^(.+)\[(\d+)\]$/)
    if (match) {
      lastSegment = `item${match[2]}`
    } else if (/^\[?\d+\]?$/.test(parentSegment)) {
      // parentSegment 是纯数字或数组索引
      lastSegment = `item${parentSegment.replace(/\D/g, '')}`
    } else {
      lastSegment = parentSegment
    }
  }
  // 如果仍然为空（如 $[0] 只有一级的情况），生成 item0, item1 等
  if (!lastSegment) {
    const arrayIndexMatch = jsonPath.match(/\[(\d+)\]$/)
    if (arrayIndexMatch) {
      lastSegment = `item${arrayIndexMatch[1]}`
    }
  }
  return lastSegment
}

// 移除输出变量
const removeOutputVar = (req, index) => {
  if (req.output_vars && req.output_vars.length > index) {
    req.output_vars.splice(index, 1)
  }
}

// ==================== 拖拽排序相关 ====================

// 拖拽开始
const handleDragStart = (event, index) => {
  dragState.dragging = true
  dragState.fromIndex = index
  event.dataTransfer.effectAllowed = 'move'
}

// 拖拽进入
const handleDragOver = (event, index) => {
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
}

// 拖拽放下
const handleDrop = (event, toIndex) => {
  event.preventDefault()
  if (dragState.fromIndex !== null && dragState.fromIndex !== toIndex) {
    // 移动请求
    const item = chainRequests.value.splice(dragState.fromIndex, 1)[0]
    chainRequests.value.splice(toIndex, 0, item)
    // 更新 order 字段
    updateRequestOrders()
  }
  dragState.dragging = false
  dragState.fromIndex = null
}

// 拖拽结束
const handleDragEnd = () => {
  dragState.dragging = false
  dragState.fromIndex = null
}

// 更新请求顺序号
const updateRequestOrders = () => {
  chainRequests.value.forEach((req, index) => {
    req.order = index + 1
  })
}

// 拖拽状态
const dragState = ref({
  dragging: false,
  fromIndex: null
})

// ==================== 变量面板相关 ====================

// 变量面板是否可见
const showVariablePanel = ref(true)

// 选中的请求索引
const selectedRequestIndex = ref(null)

// 选择请求
const selectRequest = (index) => {
  selectedRequestIndex.value = index
}

// 获取选中的请求
const selectedRequest = computed(() => {
  if (selectedRequestIndex.value !== null && chainRequests.value[selectedRequestIndex.value]) {
    return chainRequests.value[selectedRequestIndex.value]
  }
  return null
})

// 获取变量面板数据
const getVariablePanelData = () => {
  const chainVars = []
  const runtimeVars = []

  // 收集所有请求的输出变量
  chainRequests.value.forEach((req, index) => {
    const alias = req.alias || req.name || `request_${index + 1}`
    const outputVars = req.output_vars || []

    // SSE 模式的 data_field 也作为变量添加
    if (req.is_sse && req.sse_data_field) {
      chainVars.push({
        name: alias,
        children: [{
          name: req.sse_data_field,
          value: '',
          isSse: true
        }]
      })
    } else if (outputVars.length > 0) {
      chainVars.push({
        name: alias,
        children: outputVars.map(ov => ({
          name: ov.name || ov.parse_rule?.content || '未命名',
          value: ov.default || ''
        }))
      })
    }

    // 兼容旧的 output_var
    if (req.output_var && !outputVars.some(ov => ov.name === req.output_var)) {
      const existingGroup = chainVars.find(g => g.name === alias)
      if (existingGroup) {
        existingGroup.children.push({
          name: req.output_var,
          value: ''
        })
      } else {
        chainVars.push({
          name: alias,
          children: [{
            name: req.output_var,
            value: ''
          }]
        })
      }
    }
  })

  // 运行时变量可以从 runtimeVariables 读取
  if (requiredVariables.value) {
    requiredVariables.value.forEach(varName => {
      runtimeVars.push({
        name: varName,
        value: ''
      })
    })
  }

  // 添加鉴权登录接口的响应变量（登录接口Token模式）
  if (form.value.auth_type === 'login_token' && form.value.response_parse_config) {
    const parseConfig = form.value.response_parse_config
    if (parseConfig.parse_rule && parseConfig.parse_rule.content) {
      // 从解析规则中提取变量名，如 $.data.accessToken -> accessToken
      const pathParts = parseConfig.parse_rule.content.split('.')
      const varName = pathParts[pathParts.length - 1]
      runtimeVars.push({
        name: varName,
        value: '',
        source: 'auth'  // 标记来源为鉴权
      })
    }
  }

  return { chainVars, runtimeVars }
}

// 获取所有变量面板中的变量名（用于排除）
const getPanelVariableNames = () => {
  const panelData = getVariablePanelData()
  const names = new Set()

  // 添加请求链变量名
  panelData.chainVars.forEach(group => {
    group.children.forEach(child => {
      names.add(child.name)
    })
  })

  // 添加运行时变量名
  panelData.runtimeVars.forEach(rv => {
    names.add(rv.name)
  })

  return names
}

// 变量拖拽开始
const handleVarDragStart = (event, groupName, varName) => {
  const varRef = `{{${groupName}.${varName}}}`
  event.dataTransfer.setData('text/plain', varRef)
}

// 运行时变量拖拽开始
const handleRuntimeVarDragStart = (event, varName) => {
  const varRef = `{{${varName}}}`
  event.dataTransfer.setData('text/plain', varRef)
}

// 变量面板请求链变量点击
const handleVarPanelClick = (data) => {
  // 复制变量引用到剪贴板
  navigator.clipboard.writeText(data.ref).then(() => {
    ElMessage.success(`已复制变量引用: ${data.ref}`)
  }).catch(() => {
    ElMessage.info(`变量引用: ${data.ref}`)
  })
}

// 变量面板运行时变量点击
const handleRuntimePanelClick = (data) => {
  // 复制变量引用到剪贴板
  navigator.clipboard.writeText(data.ref).then(() => {
    ElMessage.success(`已复制变量引用: ${data.ref}`)
  }).catch(() => {
    ElMessage.info(`变量引用: ${data.ref}`)
  })
}

// 处理 SSE 模式切换
const handleSseToggle = (req, isSse) => {
  if (isSse) {
    req.response_type = 'sse'
  } else {
    req.response_type = 'json'
    req.sse_event_type = ''
    req.sse_data_field = ''
  }
}

// 处理响应类型变化（兼容旧代码）
const handleResponseTypeChange = (req) => {
  if (req.response_type === 'sse') {
    req.is_sse = true
    // SSE 模式下清空普通解析配置
    req.response_parse_type = 'json_path'
    req.response_parse_content = ''
    req.response_parse_pattern = ''
  } else {
    req.is_sse = false
    // 普通模式下清空 SSE 配置
    req.sse_event_type = ''
    req.sse_data_field = ''
  }
}

const removeChainRequest = (index) => {
  chainRequests.value.splice(index, 1)
  // 重新排序
  chainRequests.value.forEach((req, i) => {
    req.order = i + 1
  })
}

// 深度合并函数 - 用于编辑时合并默认配置和已有数据
const deepMerge = (target, source) => {
  if (!source) return target
  const result = { ...target }
  for (const key in source) {
    const sourceValue = source[key]
    if (sourceValue !== null && typeof sourceValue === 'object') {
      if (Array.isArray(sourceValue)) {
        // 数组直接替换
        result[key] = sourceValue
      } else {
        // 对象递归合并
        result[key] = deepMerge(result[key] || {}, sourceValue)
      }
    } else if (sourceValue !== undefined) {
      // 处理 undefined 值，不覆盖 target 中已有的值
      result[key] = sourceValue
    }
  }
  return result
}

// 获取默认表单配置
const getDefaultForm = () => ({
  name: '',
  provider: 'openai',
  model_type: '',
  model_role: 'evaluation',
  endpoint: '',
  api_key: '',
  auth_type: 'none',
  auth_config: {},
  response_parse_config: {},
  request_config: {
    chain_requests_enabled: false,
    chain_requests: { enabled: false, requests: [] }
  },
  supports_multimodal: false,
  description: ''
})

// 构建请求链配置
const buildChainRequestsConfig = () => {
  const requests = chainRequests.value.map(req => {
    // 根据 is_sse 构建不同的解析配置
    let response_parse = {}
    if (req.is_sse) {
      // SSE 流式响应配置
      response_parse = {
        parse_type: 'sse_stream',
        event_type: req.sse_event_type,
        data_field: req.sse_data_field
      }
    } else {
      // 普通 JSON 响应配置：从 output_vars[0] 派生，保持向后兼容
      const firstOv = req.output_vars && req.output_vars.length > 0 ? req.output_vars[0] : null
      if (firstOv) {
        response_parse = {
          parse_type: firstOv.parse_type || 'json_path',
          parse_rule: firstOv.parse_type === 'json_path'
            ? { content: firstOv.parse_rule }
            : firstOv.parse_type === 'json_regex'
              ? { pattern: firstOv.parse_rule }
              : {}
        }
      }
      // 如果没有 output_vars，使用旧的字段（向后兼容）
      else if (req.response_parse_type) {
        response_parse = {
          parse_type: req.response_parse_type,
          parse_rule: req.response_parse_type === 'json_path'
            ? { content: req.response_parse_content }
            : req.response_parse_type === 'json_regex'
              ? { pattern: req.response_parse_pattern }
              : {}
        }
      }
    }

    // 保存动态配置为JSON对象（优先使用Config，否则解析headers/body/params，可能是字符串或对象）
    const headersData = req.headersConfig?.length ? configToJson(req.headersConfig) : parseJsonField(req.headers)
    const bodyData = req.bodyConfig?.length ? configToJson(req.bodyConfig) : parseJsonField(req.body)
    const paramsData = req.paramsConfig?.length ? configToJson(req.paramsConfig) : parseJsonField(req.params)

    // 构建 output_vars 配置（多变量模式）
    const outputVars = (req.output_vars || []).map(ov => {
      if (req.is_sse) {
        // SSE 模式：简化配置，只保留 name 和 default
        return {
          name: ov.name,
          default: ov.default || ''
        }
      }
      // 普通 JSON 模式：完整配置
      return {
        name: ov.name,
        parse_type: ov.parse_type || 'json_path',
        parse_rule: ov.parse_type === 'json_path'
          ? { content: ov.parse_rule }
          : ov.parse_type === 'json_regex'
            ? { pattern: ov.parse_rule }
            : {},
        default: ov.default || ''
      }
    })

    return {
      name: req.name,
      order: req.order,
      url: req.url,
      method: req.method,
      headers: headersData,
      body: bodyData,
      params: paramsData,
      output_vars: outputVars.length > 0 ? outputVars : undefined,  // 多变量模式
      is_sse: req.is_sse,
      response_type: req.is_sse ? 'sse' : 'json',
      response_parse: response_parse,
      max_retries: req.max_retries,
      retry_interval: req.retry_interval,
      continue_on_error: req.continue_on_error
    }
  })

  form.value.request_config.chain_requests = {
    enabled: form.value.request_config.chain_requests_enabled,
    requests: requests
  }
}

// 从配置加载请求链
const loadChainRequestsFromConfig = () => {
  const chainConfig = form.value.request_config?.chain_requests || {}
  // 兼容两种配置结构：chain_requests.enabled 或 chain_requests_enabled
  const isEnabled = chainConfig.enabled !== undefined ? chainConfig.enabled : (form.value.request_config?.chain_requests_enabled || false)
  form.value.request_config.chain_requests_enabled = isEnabled

  const requests = chainConfig.requests || []
  chainRequests.value = requests.map(req => {
    // 判断是否为 SSE 流式响应
    const isSSE = req.response_parse?.parse_type === 'sse_stream'

    // 加载 output_vars（多变量模式）
    const outputVars = (req.output_vars || []).map(ov => {
      if (isSSE) {
        // SSE 模式：简化配置
        return {
          name: ov.name || '',
          default: ov.default || ''
        }
      }
      // 普通 JSON 模式：完整配置
      return {
        name: ov.name || '',
        parse_type: ov.parse_type || 'json_path',
        parse_rule: ov.parse_rule?.content || ov.parse_rule?.pattern || '',
        default: ov.default || ''
      }
    })

    return {
      name: req.name || '',
      order: req.order || 1,
      url: req.url || '',
      method: req.method || 'POST',
      headers: parseJsonField(req.headers),
      headers_json: JSON.stringify(parseJsonField(req.headers), null, 2),
      headersConfig: jsonToConfig(parseJsonField(req.headers)),  // 加载动态配置
      body: parseJsonField(req.body),
      body_json: JSON.stringify(parseJsonField(req.body), null, 2),
      bodyConfig: jsonToConfig(parseJsonField(req.body)),         // 加载动态配置
      params: parseJsonField(req.params),
      params_json: JSON.stringify(parseJsonField(req.params), null, 2),
      paramsConfig: jsonToConfig(parseJsonField(req.params)),     // 加载动态配置
      output_vars: outputVars,
      is_sse: isSSE,  // 是否为 SSE 流式响应
      response_type: req.response_type || (isSSE ? 'sse' : 'json'),
      response_parse_type: req.response_parse?.parse_type || 'json_path',
      response_parse_content: req.response_parse?.parse_rule?.content || '',
      response_parse_pattern: req.response_parse?.parse_rule?.pattern || '',
      // SSE 流式响应配置
      sse_event_type: req.response_parse?.event_type || '',
      sse_data_field: req.response_parse?.data_field || '',
      max_retries: req.max_retries || 0,
      retry_interval: req.retry_interval || 1000,
      continue_on_error: req.continue_on_error || false
    }
  })
}

const loadModels = async () => {
  try {
    const params = { limit: 100 }
    if (roleFilter.value) {
      params.model_role = roleFilter.value
    }
    const res = await modelApi.list(params)
    models.value = res || []
  } catch (e) {
    ElMessage.error('加载失败')
  }
}

// 监听筛选变化
watch(roleFilter, () => {
  loadModels()
})

// 监听测试变量弹窗关闭，实时保存当前输入的值
watch(testDialogVisible, (newVal) => {
  if (newVal === false) {
    // 弹窗关闭时，保存当前输入的值到 lastTestVariables
    // 这样即使点击取消，下次打开也能带入上次的值
    for (const key in testVariables.value) {
      lastTestVariables.value[key] = { ...testVariables.value[key] }
    }
    // 清理 WebSocket
    if (ws.value) {
      ws.value.close()
      ws.value = null
    }
  }
})

const openDialog = async (row = null) => {
  if (row) {
    // 编辑模式 - 先获取详情数据
    isEdit.value = true

    try {
      // 调用详情接口获取完整数据
      const detailRes = await modelApi.get(row.id)
      const rowData = detailRes

      // 初始化 form
      form.value = {
        id: rowData.id,
        name: rowData.name || '',
        provider: rowData.provider || 'openai',
        model_type: rowData.model_type || '',
        model_role: rowData.model_role || 'evaluation',
        endpoint: rowData.endpoint || '',
        api_key: '',  // 编辑模式下不显示 API Key（后端返回脱敏值），留空表示不修改
        auth_type: rowData.auth_type || 'none',
        auth_config: rowData.auth_config || {},
        response_parse_config: rowData.response_parse_config || {},
        request_config: rowData.request_config || {
          chain_requests_enabled: false,
          chain_requests: { enabled: false, requests: [] }
        },
        supports_multimodal: rowData.supports_multimodal || false,
        description: rowData.description || ''
      }

      // 如果是 login_token 类型，初始化 JSON 编辑器
      if (form.value.auth_type === 'login_token') {
        // 使用动态配置
        initLoginHeadersConfig()
        initLoginBodyConfig()
      } else {
        loginHeadersConfig.value = []
        loginBodyConfig.value = []
      }

      // 加载请求链配置
      if (form.value.request_config?.chain_requests?.requests?.length > 0) {
        loadChainRequestsFromConfig()
      } else {
        chainRequests.value = []
      }
    } catch (e) {
      ElMessage.error('获取模型详情失败')
      return
    }
  } else {
    // 新增模式 - 使用默认值
    isEdit.value = false
    form.value = getDefaultForm()
    loginHeadersConfig.value = []
    loginBodyConfig.value = []
    chainRequests.value = []
  }
  // 重置运行时变量，避免切换模型时残留上一个模型的变量
  requiredVariables.value = []
  dialogVisible.value = true
}

const submitForm = async () => {
  if (submitting.value) return
  submitting.value = true
  try {
    // 确保 model_type 有值
    if (!form.value.model_type) {
      form.value.model_type = 'custom_http'
    }

    // 如果是 login_token 类型，保存动态配置为 JSON
    if (form.value.auth_type === 'login_token') {
      saveLoginHeadersConfig()
      saveLoginBodyConfig()
    }

    // 构建请求链配置
    buildChainRequestsConfig()
    await formRef.value.validate()
    if (isEdit.value) {
      await modelApi.update(form.value.id, form.value)
      ElMessage.success('更新成功')
    } else {
      await modelApi.create(form.value)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadModels()
  } catch (e) {
    ElMessage.error(e.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

const testConnection = async (row) => {
  testingModelId.value = row.id

  // 重置测试状态
  testStarted.value = false
  testLogs.value = []

  // 先获取完整模型数据（列表可能不包含 request_config）
  let modelData = row
  try {
    const fullModel = await modelApi.get(row.id)
    if (fullModel) {
      modelData = fullModel
    }
  } catch (e) {
    console.warn('获取完整模型数据失败，使用列表数据:', e)
  }

  // 加载鉴权配置（因为 getVariablePanelData 需要用到 auth_type 和 response_parse_config）
  if (modelData.auth_type === 'login_token') {
    form.value.auth_type = modelData.auth_type
    form.value.auth_config = modelData.auth_config || {}
    form.value.response_parse_config = modelData.response_parse_config || {}
  } else {
    form.value.auth_type = modelData.auth_type || 'none'
    form.value.auth_config = {}
    form.value.response_parse_config = {}
  }

  // 加载请求链配置，以便获取变量面板数据
  form.value.request_config = modelData.request_config || { chain_requests_enabled: false, chain_requests: {} }
  loadChainRequestsFromConfig()

  // 获取需要的变量列表
  try {
    // 提取所有 [[variable]] 格式的变量（需要手动输入的外部变量）
    const externalVars = new Set()
    chainRequests.value.forEach(req => {
      // 检查 URL
      if (req.url) {
        const matches = req.url.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      }
      // 检查 headers（原始对象或 Config 数组）
      if (req.headers) {
        const headerStr = JSON.stringify(req.headers)
        const matches = headerStr.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      } else if (req.headersConfig && req.headersConfig.length > 0) {
        // 从 Config 数组中提取 [[variable]]
        const configStr = JSON.stringify(req.headersConfig)
        const matches = configStr.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      }
      // 检查 body（原始对象或 Config 数组）
      if (req.body) {
        const bodyStr = JSON.stringify(req.body)
        const matches = bodyStr.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      } else if (req.bodyConfig && req.bodyConfig.length > 0) {
        const configStr = JSON.stringify(req.bodyConfig)
        const matches = configStr.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      }
      // 检查 params（原始对象或 Config 数组）
      if (req.params) {
        const paramsStr = JSON.stringify(req.params)
        const matches = paramsStr.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      } else if (req.paramsConfig && req.paramsConfig.length > 0) {
        const configStr = JSON.stringify(req.paramsConfig)
        const matches = configStr.match(/\[\[(\w+)\]\]/g) || []
        matches.forEach(m => externalVars.add(m.replace(/\[\[|\]\]/g, '')))
      }
    })

    requiredVariables.value = [...externalVars]

    // 无论是否有变量，都显示弹窗
    // 直接使用 lastTestVariables 的值（实时保存的），不再重置
    requiredVariables.value.forEach(v => {
      // 如果上次有保存的值，直接使用
      if (lastTestVariables.value[v] !== undefined) {
        testVariables.value[v] = { ...lastTestVariables.value[v] }
      } else {
        // 检查变量名是否包含 type 后缀，如果有则根据后缀设置默认值
        if (v.endsWith('_bool')) {
          testVariables.value[v] = { value: 'false', type: 'boolean' }
        } else if (v.endsWith('_num')) {
          testVariables.value[v] = { value: '0', type: 'number' }
        } else {
          testVariables.value[v] = { value: '', type: 'string' }
        }
      }
    })

    // 显示弹窗
    testDialogVisible.value = true
  } catch (e) {
    // 如果获取变量失败，仍然显示弹窗（变量为空）
    requiredVariables.value = []
    testDialogVisible.value = true
  }
}

// 开始测试（通过 WebSocket）
const startTest = async () => {
  testStarted.value = true
  testingLoading.value = true
  testLogs.value = []

  // 构建 WebSocket URL（使用配置）
  const wsUrl = appConfig.getWebSocketUrl(`/api/v1/models/ws/test/${testingModelId.value}`)

  try {
    ws.value = new WebSocket(wsUrl)

    ws.value.onopen = () => {
      // 发送变量配置
      const message = {
        variables: testVariables.value
      }
      ws.value.send(JSON.stringify(message))
    }

    ws.value.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type !== 'stream_chunk') {
          testLogs.value.push(data)
        }
        // 如果是完成或错误消息，停止 loading
        if (data.type === 'complete' || data.type === 'error') {
          testingLoading.value = false
        }
      } catch (e) {
        console.error('解析 WebSocket 消息失败:', e)
      }
    }

    ws.value.onerror = (error) => {
      console.error('WebSocket 错误:', error)
      testLogs.value.push({
        type: 'error',
        content: 'WebSocket 连接错误'
      })
      testingLoading.value = false
    }

    ws.value.onclose = () => {
      testingLoading.value = false
    }
  } catch (e) {
    console.error('创建 WebSocket 失败:', e)
    testLogs.value.push({
      type: 'error',
      content: `连接失败: ${e.message}`
    })
    testingLoading.value = false
  }
}

// 关闭测试弹窗
const closeTestDialog = () => {
  // 关闭 WebSocket 连接
  if (ws.value) {
    ws.value.close()
    ws.value = null
  }
  testingLoading.value = false
  testStarted.value = false
  testLogs.value = []
  testDialogVisible.value = false
}

const deleteModel = async (row) => {
  try {
    await ElMessageBox.confirm('确定要删除该模型吗？', '提示', {
      type: 'warning'
    })
    deletingId.value = row.id
    try {
      await modelApi.delete(row.id)
      ElMessage.success('删除成功')
      loadModels()
    } finally {
      deletingId.value = null
    }
  } catch (e) {
    if (e !== 'cancel') {
      // 显示后端返回的具体错误信息
      const errorMsg = e.response?.data?.detail || e.message || '删除失败'
      ElMessage.error(errorMsg)
    }
  }
}

// ==================== curl 导入相关 ====================

// 打开 curl 导入弹窗（用于登录接口Token）
const openCurlImportDialog = (type = 'login_token', target = null) => {
  curlImportType.value = type
  curlImportTarget.value = target
  curlCommand.value = ''
  curlImportDialogVisible.value = true
}

// 关闭 curl 导入弹窗
const closeCurlImportDialog = () => {
  curlImportDialogVisible.value = false
  curlCommand.value = ''
  curlImportLoading.value = false
}

// 确认 curl 导入
const confirmCurlImport = async () => {
  if (!curlCommand.value.trim()) {
    ElMessage.warning('请输入 curl 命令')
    return
  }

  curlImportLoading.value = true
  try {
    const result = await modelApi.parseCurl(curlCommand.value)

    if (curlImportType.value === 'login_token') {
      // 导入到登录接口Token配置
      importCurlToLoginToken(result)
    } else if (curlImportType.value === 'chain_request') {
      // 导入到请求链请求
      importCurlToChainRequest(result, curlImportTarget.value)
    }

    ElMessage.success('curl 命令解析成功')
    closeCurlImportDialog()
  } catch (e) {
    ElMessage.error(e.message || 'curl 命令解析失败')
  } finally {
    curlImportLoading.value = false
  }
}

// 将 curl 解析结果导入到登录接口Token配置
const importCurlToLoginToken = (result) => {
  // 设置登录URL（从 endpoint 提取）
  if (result.endpoint) {
    form.value.auth_config.login_url = result.endpoint
  }

  // 设置登录方法
  if (result.method) {
    form.value.auth_config.login_method = result.method.toUpperCase()
  }

  // 设置登录 Headers
  if (result.headers) {
    // 移除 Content-Type 和 Authorization 等通用头（登录接口通常不需要）
    const loginHeaders = { ...result.headers }
    delete loginHeaders['Content-Type']
    delete loginHeaders['Authorization']
    delete loginHeaders['content-type']
    delete loginHeaders['authorization']
    form.value.auth_config.login_headers = loginHeaders
    // 同步更新动态配置编辑器
    loginHeadersConfig.value = jsonToConfig(loginHeaders)
  }

  // 设置登录 Body
  if (result.body) {
    form.value.auth_config.login_body = result.body
    // 同步更新动态配置编辑器
    loginBodyConfig.value = jsonToConfig(result.body)
  }

  // 处理鉴权配置（Bearer Token 或 Basic Auth）
  if (result.auth_config) {
    if (result.auth_config.type === 'bearer' && result.auth_config.token) {
      // 如果curl命令包含Bearer token，设置token
      form.value.auth_config.token_header = 'Authorization'
      form.value.auth_config.token_prefix = 'Bearer '
    } else if (result.auth_config.type === 'basic') {
      // Basic Auth 暂不支持
      ElMessage.info('检测到 Basic Auth 鉴权，请手动配置')
    }
  }
}

// 将 curl 解析结果导入到请求链请求
const importCurlToChainRequest = (result, targetIndex) => {
  if (targetIndex !== null && targetIndex >= 0 && targetIndex < chainRequests.value.length) {
    // 导入到指定的请求
    const req = chainRequests.value[targetIndex]
    applyCurlResultToRequest(req, result)
  } else {
    // 新增一个请求
    addChainRequest()
    const newReq = chainRequests.value[chainRequests.value.length - 1]
    applyCurlResultToRequest(newReq, result)
    // 选中新添加的请求
    selectedRequestIndex.value = chainRequests.value.length - 1
  }
}

// 将 curl 解析结果应用到请求对象
const applyCurlResultToRequest = (req, result) => {
  if (result.endpoint) {
    req.url = result.endpoint
  }
  if (result.method) {
    req.method = result.method.toUpperCase()
  }
  if (result.headers) {
    req.headers = result.headers
    req.headersConfig = jsonToConfig(result.headers)
  }
  if (result.body) {
    req.body = result.body
    req.bodyConfig = jsonToConfig(result.body)
  }
  if (result.params) {
    req.params = result.params
    req.paramsConfig = jsonToConfig(result.params)
  }
}

onMounted(() => {
  loadModels()
  // 加载 Claude Code 裁判全局配置
  loadCcConfig()
  // 加载 DeepSeek Harness 裁判全局配置
  loadDshConfig()
})
</script>

<style scoped>
.models-page {
  max-width: 1400px;
  margin: 0 auto;
}

.code-block {
  background: #f5f5f5;
  padding: 10px;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 12px;
  max-height: 200px;
  overflow-y: auto;
}

.code-block.error {
  color: #f56c6c;
  background: #fef0f0;
}

/* 裁判配置卡片样式（Claude Code / DeepSeek Harness tab 切换） */
.judge-config-card {
  margin-bottom: 20px;
}

.judge-tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.cc-field-tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.4;
  margin-top: 4px;
}

.cc-test-result {
  border-top: 1px solid #ebeef5;
  padding-top: 12px;
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

/* 请求链样式 */
.request-chain-list {
  width: 100%;
}

.request-chain-item {
  margin-bottom: 15px;
}

.request-chain-item .el-card {
  border: 1px solid #E5E7EB;
}

.request-chain-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.request-chain-header span {
  font-weight: 600;
  color: #374151;
}

/* 多变量输出配置样式 */
.output-vars-container {
  width: 100%;
}

.output-var-item {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.output-var-item:last-child {
  margin-bottom: 0;
}

/* 请求链三栏布局样式 */
.request-chain-container {
  display: flex;
  gap: 12px;
  width: 100%;
  min-height: 500px;
  max-height: 70vh;
  overflow: hidden;
}

/* 左侧请求列表 */
.request-list-panel {
  width: 160px;
  min-width: 160px;
  max-width: 160px;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  background: #FAFAFA;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.request-list-panel .panel-header {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  padding: 8px 10px;
  border-bottom: 1px solid #E5E7EB;
  background: #F5F5F5;
  border-radius: 6px 6px 0 0;
  gap: 6px;
}

.request-list-panel .panel-header span {
  font-weight: 600;
  font-size: 12px;
  color: #374151;
}

.request-list-panel .panel-buttons {
  display: flex;
  flex-direction: row;
  gap: 4px;
}

.request-list-panel .panel-buttons .el-button {
  flex: 1;
  font-size: 11px;
  padding: 6px 8px;
}

.request-list {
  flex: 1;
  padding: 8px;
  overflow-y: auto;
}

.request-list-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  margin-bottom: 4px;
  background: white;
  border: 1px solid #E5E7EB;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.request-list-item .request-name {
  flex: 1;
  font-size: 11px;
  color: #374151;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.request-list-item .request-method {
  font-size: 9px;
  color: #909399;
  padding: 2px 4px;
  background: #F3F4F6;
  border-radius: 3px;
  flex-shrink: 0;
}

.request-list-item:hover {
  background: #F5F7FA;
  border-color: #C0C4CC;
}

.request-list-item.selected {
  background: #ECF5FF;
  border-color: #409EFF;
}

.request-list-item .drag-handle {
  cursor: grab;
  color: #909399;
  font-size: 14px;
}

.request-list-item .drag-handle:active {
  cursor: grabbing;
}

.request-list-item .el-button {
  padding: 2px;
}

.empty-list {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 30px 10px;
  color: #909399;
  font-size: 12px;
  text-align: center;
}

/* 中间详情面板 - 上下结构表单 */
.request-detail-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.request-detail-panel .el-card {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: 1px solid #E5E7EB;
}

.request-detail-panel .el-card__body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}

/* 详情面板内的表单 - 改为更紧凑的布局 */
.request-detail-panel .el-form-item {
  margin-bottom: 14px;
}

.request-detail-panel .el-form-item__label {
  font-size: 13px;
  padding: 0 0 6px 0;
  line-height: 20px;
  color: #606266;
}

.request-detail-panel .el-form-item__content {
  line-height: 32px;
}

.request-detail-panel .el-input {
  font-size: 13px;
}

.request-detail-panel .el-select {
  width: 100%;
}

.request-detail-panel .el-input-number {
  width: 100%;
}

.request-detail-panel .el-input__inner {
  height: 32px;
  line-height: 32px;
}

/* 请求链头部 */
.request-chain-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 0;
}

.request-chain-header span {
  font-weight: 600;
  font-size: 14px;
}

/* 输出变量容器 */
.output-vars-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.output-var-item {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

/* 占位符 */
.request-detail-placeholder {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  border: 1px dashed #D1D5DB;
  border-radius: 6px;
  background: #FAFAFA;
  color: #9CA3AF;
  gap: 12px;
  padding: 40px;
  min-height: 400px;
}

.request-detail-placeholder .el-icon {
  font-size: 36px;
  color: #C0C4CC;
}

.variable-panel-placeholder {
  display: none;
}

/* 变量面板可折叠 */
.variable-panel-wrapper {
  width: 200px;
  min-width: 200px;
  flex-shrink: 0;
}

.variable-panel-toggle {
  width: 40px;
  min-width: 40px;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 10px;
}

.variable-panel-toggle .el-button {
  font-size: 20px;
  color: #909399;
}

/* 模型对话框样式优化 */
:deep(.model-dialog) {
  max-height: 95vh;
  max-width: 1400px;
  width: 95vw;
}

:deep(.model-dialog .el-dialog__body) {
  max-height: calc(95vh - 130px);
  overflow-y: auto;
  padding: 20px 24px;
}

:deep(.request-chain-item .el-card__body) {
  padding: 15px;
}

:deep(.request-chain-item .el-form-item) {
  margin-bottom: 12px;
}

/* 请求详情面板 SSE 配置样式 */
.request-detail-panel .sse-toggle {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
}

.request-detail-panel .sse-config {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

/* 测试结果样式 */
.test-input-section, .test-result-section {
  min-height: 200px;
}

.test-result-section .log-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 60px 20px;
  color: #909399;
  font-size: 14px;
}

.test-result-section .log-empty {
  text-align: center;
  color: #c0c4cc;
  padding: 60px 20px;
  font-size: 14px;
}

.result-content {
  padding: 10px;
}

.result-log-item {
  margin-bottom: 12px;
  padding: 8px;
  border-radius: 4px;
  background: #fafafa;
}

.log-start {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #409eff;
  font-weight: 500;
}

.log-success {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #67c23a;
  font-weight: 500;
  padding: 8px;
  background: #f0f9ff;
  border-radius: 4px;
}

.log-error {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #f56c6c;
  font-weight: 500;
  padding: 8px;
  background: #fef0f0;
  border-radius: 4px;
}

.log-complete {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  padding: 8px;
  background: #f4f4f5;
  border-radius: 4px;
}

.log-response {
  margin-top: 8px;
}

.log-request {
  margin-top: 8px;
}

.log-request .el-divider {
  margin: 12px 0;
}

/* curl 导入弹窗样式 */
:deep(.curl-import-dialog .el-textarea__inner) {
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 12px;
}

/* JSON粘贴解析弹窗样式 */
:deep(.json-paste-dialog .el-textarea__inner) {
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 12px;
}
</style>
