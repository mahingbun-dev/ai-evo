<template>
  <div class="model-test-log">
    <el-scrollbar :style="`height: ${height}px`">
      <div class="result-content">
        <!-- 加载中占位 -->
        <div v-if="isLoading && logs.length === 0" class="log-loading">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span>等待响应...</span>
        </div>

        <div v-for="(log, index) in logs" :key="index" class="result-log-item">
          <!-- 开始消息 -->
          <div v-if="log.type === 'start'" class="log-start">
            <el-icon class="is-loading"><Loading /></el-icon>
            <span>{{ log.content }}</span>
          </div>

          <!-- 请求开始（request_start） -->
          <div v-else-if="log.type === 'request_start'" class="log-request-start">
            <el-icon><ArrowRight /></el-icon>
            <span>发起请求 {{ log.request_index }}: {{ log.name }} — {{ log.method }} {{ log.url }}</span>
          </div>

          <!-- 请求详情 -->
          <div v-else-if="log.type === 'request_detail'" class="log-request">
            <el-divider content-position="left">请求 {{ log.request_index }}: {{ log.name }}</el-divider>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="URL">{{ log.url }}</el-descriptions-item>
              <el-descriptions-item label="方法">{{ log.method }}</el-descriptions-item>
              <el-descriptions-item label="输出变量" v-if="log.output_var">
                {{ log.output_var }}: {{ log.output_value }}
              </el-descriptions-item>
            </el-descriptions>
            <div v-if="log.headers && Object.keys(log.headers).length > 0" style="margin-top: 8px;">
              <strong>请求头:</strong>
              <pre class="code-block">{{ JSON.stringify(log.headers, null, 2) }}</pre>
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

        <!-- 空状态 -->
        <div v-if="!isLoading && logs.length === 0" class="log-empty">
          <span>暂无日志</span>
        </div>
      </div>
    </el-scrollbar>
  </div>
</template>

<script setup>
import { Loading, CircleCheck, CircleClose, Finished, ArrowRight } from '@element-plus/icons-vue'

defineProps({
  logs: {
    type: Array,
    default: () => []
  },
  isLoading: {
    type: Boolean,
    default: false
  },
  height: {
    type: Number,
    default: 400
  }
})
</script>

<style scoped>
.model-test-log {
  width: 100%;
}

.result-content {
  padding: 8px;
}

.result-log-item {
  margin-bottom: 8px;
}

.log-loading,
.log-start,
.log-request-start,
.log-success,
.log-error,
.log-complete {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 13px;
}

.log-loading,
.log-start {
  color: #909399;
  background: #f4f4f5;
}

.log-request-start {
  color: #606266;
  background: #f0f2f5;
}

.log-success {
  color: #67c23a;
  background: #f0f9eb;
}

.log-error {
  color: #f56c6c;
  background: #fef0f0;
}

.log-complete {
  color: #409eff;
  background: #ecf5ff;
}

.log-request {
  background: #fafafa;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 12px;
  margin: 4px 0;
}

.log-response {
  background: #f9f9f9;
  border-left: 3px solid #409eff;
  padding: 8px 12px;
  border-radius: 0 4px 4px 0;
}

.log-response strong {
  font-size: 13px;
  color: #606266;
}

.code-block {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  padding: 8px;
  font-size: 12px;
  font-family: 'Courier New', Courier, monospace;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 4px 0 0;
  max-height: 300px;
  overflow-y: auto;
}

.code-block.error {
  color: #f56c6c;
  border-color: #fbc4c4;
  background: #fef0f0;
}

.log-empty {
  text-align: center;
  color: #c0c4cc;
  padding: 40px 0;
  font-size: 13px;
}
</style>
