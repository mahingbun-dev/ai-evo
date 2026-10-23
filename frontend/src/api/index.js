import axios from 'axios'

const request = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  withCredentials: true
})

// ==================== 本地登录 token 管理 ====================
const TOKEN_KEY = 'ai_evo_token'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (token) => localStorage.setItem(TOKEN_KEY, token)
export const clearToken = () => localStorage.removeItem(TOKEN_KEY)
// WebSocket 连接用：以 query 参数携带 token（握手阶段无法自定义 header）
export const getWsTokenQuery = () =>
  getToken() ? `?token=${encodeURIComponent(getToken())}` : ''


// 请求拦截器：注入 Authorization: Bearer <token>
request.interceptors.request.use(
  config => {
    const token = getToken()
    if (token) {
      config.headers['Authorization'] = `Bearer ${token}`
    }
    return config
  },
  error => {
    return Promise.reject(error)
  }
)

// 响应拦截器
request.interceptors.response.use(
  response => {
    // 需要读取响应头（如下载文件名）的场景，通过 rawResponse 取回完整响应
    if (response.config?.rawResponse) {
      return response
    }
    return response.data
  },
  async error => {
    // 401：登录态失效，清除本地 token 并跳转登录页
    // 登录/注册接口自身的 401（密码错误等）不触发跳转，正常走错误提示
    // W14 修复：跳转后 reject 让调用方拿到明确失败——原实现返回永不
    // settle 的空 Promise，所有 await 该请求的调用链（含 finally）被永久挂起。
    if (error.response?.status === 401) {
      const reqUrl = error.config?.url || ''
      const isAuthRequest = reqUrl.includes('/auth/login') || reqUrl.includes('/auth/register')
      if (!isAuthRequest) {
        clearToken()
        if (!window.location.pathname.startsWith('/login')) {
          const redirect = encodeURIComponent(window.location.pathname + window.location.search)
          window.location.href = `/login?redirect=${redirect}`
        }
        return Promise.reject(new Error('登录已失效，请重新登录'))
      }
    }

    // responseType 为 blob 时，后端返回的 JSON 错误体也是 Blob，先解析出 detail
    let payload = error.response?.data
    if (payload instanceof Blob) {
      try {
        payload = JSON.parse(await payload.text())
        error.response.data = payload
      } catch {
        // 不是 JSON，保持原样
      }
    }

    // 提取后端返回的错误消息
    if (error.response && payload) {
      const detail = payload.detail
      if (detail) {
        // 创建新错误对象，包含后端返回的详细错误消息
        const customError = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
        customError.response = error.response
        return Promise.reject(customError)
      }
    }
    return Promise.reject(error)
  }
)

export default request

// 模型配置API
export const modelApi = {
  list: (params) => request.get('/models', { params }),
  get: (id) => request.get(`/models/${id}`),
  create: (data) => request.post('/models', data),
  update: (id, data) => request.put(`/models/${id}`, data),
  delete: (id) => request.delete(`/models/${id}`),
  getVariables: (id) => request.get(`/models/${id}/variables`),
  parseCurl: (curlCommand) => request.post('/models/parse-curl', { curl_command: curlCommand })
}

// 裁判模型API
export const judgeApi = {
  list: (params) => request.get('/judge-models', { params }),
  get: (id) => request.get(`/judge-models/${id}`),
  create: (data) => request.post('/judge-models', data)
}

// Claude Code 裁判全局配置API
export const claudeCodeApi = {
  getConfig: () => request.get('/claude-code-config'),
  update: (data) => request.put('/claude-code-config', data),
  test: () => request.post('/claude-code-config/test')
}

// DeepSeek Harness 裁判全局配置API
export const deepSeekHarnessApi = {
  getConfig: () => request.get('/deepseek-harness-config'),
  update: (data) => request.put('/deepseek-harness-config', data),
  test: () => request.post('/deepseek-harness-config/test')
}

// 评测前处理脚本API
export const preJudgeScriptApi = {
  list: (params) => request.get('/pre-judge-scripts', { params }),
  get: (id) => request.get(`/pre-judge-scripts/${id}`),
  create: (data) => request.post('/pre-judge-scripts', data),
  update: (id, data) => request.put(`/pre-judge-scripts/${id}`, data),
  delete: (id) => request.delete(`/pre-judge-scripts/${id}`),
  debug: (id, data) => request.post(`/pre-judge-scripts/${id}/debug`, data),
  // 导出单个脚本（含 entrypoint/契约 JSON），responseType blob 供前端触发下载
  export: (id) => request.get(`/pre-judge-scripts/${id}/export`, { responseType: 'blob' }),
  // 上传 script 类型脚本的 .py 文件（multipart 字段 file），服务端存 uploads/pre_judge_scripts/{id}.py；
  // 阶段E起可附带 risk_acknowledged / declared_requirements 表单字段（风险声明 / 依赖声明 JSON 数组字符串）
  uploadFile: (id, formData) => request.post(`/pre-judge-scripts/${id}/upload`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  // ---------- 脚本包 / 多文件管理接口（供脚本库后续阶段的编辑器与包导入导出使用） ----------
  // 从预置模板创建脚本（JSON body：模板标识与初始参数）
  createFromTemplate: (data) => request.post('/pre-judge-scripts/from-template', data),
  // 导入脚本包（唯一导入通道，multipart：files 与 relative_paths 一一对应 + overwrite；
  // 单 .py / .json 契约导入已下线，后端 POST /pre-judge-scripts/import 端点已删除）
  importPackage: (formData) => request.post('/pre-judge-scripts/import-package', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  // 导出脚本包（契约 + 全量文件打包为 zip），responseType blob 供前端触发下载
  exportPackage: (id) => request.get(`/pre-judge-scripts/${id}/export-package`, { responseType: 'blob' }),
  // 脚本文件列表（script 类型多文件管理）
  listFiles: (id) => request.get(`/pre-judge-scripts/${id}/files`),
  // 读取单个脚本文件内容（按 path 定位）
  readFile: (id, path) => request.get(`/pre-judge-scripts/${id}/file`, { params: { path } }),
  // 保存（覆盖）单个脚本文件内容；非 2xx（如 409 冲突）经响应拦截器抛错并保留 e.response.status 可查
  saveFile: (id, data) => request.put(`/pre-judge-scripts/${id}/file`, data),
  // 新建脚本文件
  createFile: (id, data) => request.post(`/pre-judge-scripts/${id}/file`, data),
  // 删除脚本文件（按 path 定位）
  deleteFile: (id, path) => request.delete(`/pre-judge-scripts/${id}/file`, { params: { path } }),
  // 重命名/移动脚本文件
  renameFile: (id, data) => request.post(`/pre-judge-scripts/${id}/file/rename`, data),
  // 上传 zip 脚本包（multipart 字段 file，包根必须含 main.py，≤20MB；可附带风险/依赖声明）
  uploadPackage: (id, formData) => request.post(`/pre-judge-scripts/${id}/upload-package`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  // 脚手架模板列表（阶段E）：{total, items:[{name, title, description, filename, size, download_path}]}
  listTemplates: () => request.get('/pre-judge-scripts/templates'),
  // 下载脚手架模板 zip（blob 保存）
  // 注意：templates 列表返回的 download_path 缺 /api/v1 前缀（后端已知问题，不改后端）——
  // 这里统一剥掉可能存在的 /api/v1 前缀后交给 axios 实例（baseURL=/api/v1）拼接，两种形态都能工作
  downloadTemplate: (tpl) => {
    const fallback = `/pre-judge-scripts/templates/${encodeURIComponent(tpl?.name || '')}/download`
    const path = String(tpl?.download_path || fallback).replace(/^\/api\/v1/, '')
    return request.get(path, { responseType: 'blob' })
  }
}

// 评测指令API
export const instructionApi = {
  list: (params) => request.get('/instruction/config', { params }),
  create: (data) => request.post('/instruction/config', data),
  update: (id, data) => request.put(`/instruction/config/${id}`, data),
  delete: (id) => request.delete(`/instruction/config/${id}`),
  preview: (data) => request.post('/instruction/preview', data),
  scenes: () => request.get('/instruction/scenes'),
  // 批量迁移旧版评测模板（steps 合并进 score_details，幂等；返回 {total, migrated, skipped, errors}）
  migrateAllLegacy: () => request.post('/instruction/configs/migrate-legacy-all'),
  // ==================== 模板版本（M5 QE5-2） ====================
  // 版本列表（新→旧，不含 content）：[{version_no, changelog, created_by, created_at}]
  listTemplateVersions: (id) => request.get(`/instruction/config/${id}/versions`),
  // 打版（以当前内容创建不可变版本，body: {changelog}）→ 版本信息
  createTemplateVersion: (id, data) => request.post(`/instruction/config/${id}/versions`, data),
  // 两版本字段级 diff（params from/to 为版本号）→ {summary, fields}
  diffTemplateVersions: (id, fromVersion, toVersion) => request.get(`/instruction/config/${id}/versions/diff`, {
    params: { from: fromVersion, to: toVersion }
  }),
  // 单版本详情（含完整 content 快照）
  getTemplateVersion: (id, versionNo) => request.get(`/instruction/config/${id}/versions/${versionNo}`),
  // 恢复模板到指定版本（恢复前活内容自动留存进版本链）
  restoreTemplateVersion: (id, versionNo) => request.post(`/instruction/config/${id}/versions/${versionNo}/restore`)
}

// 数据集API
export const datasetApi = {
  list: (params) => request.get('/evaluation/datasets', { params }),
  get: (id) => request.get(`/evaluation/datasets/${id}`),
  create: (data) => request.post('/evaluation/datasets', data),
  update: (id, data) => request.put(`/evaluation/datasets/${id}`, data),
  delete: (id) => request.delete(`/evaluation/datasets/${id}`),
  cleanupDuplicates: () => request.post('/evaluation/datasets/cleanup-duplicates'),
  // 下载模板
  downloadTemplate: () => request.get('/evaluation/datasets/download-template', { responseType: 'blob' }),
  // 导出数据集为 Excel（rawResponse 取回完整响应，用于解析 Content-Disposition 中的文件名）
  exportExcel: (id) => request.get(`/evaluation/datasets/${id}/export/excel`, {
    responseType: 'blob',
    rawResponse: true
  }),
  // 获取数据集字段结构
  getFields: (id) => request.get(`/evaluation/datasets/${id}/fields`),
  // 预览导入（支持动态字段）
  previewImportV2: (id, formData) => request.post(`/evaluation/datasets/${id}/import/preview/v2`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  // 删除预览阶段上传的临时文件
  deleteTempFile: (datasetId, dataIndex, tempFileId, field = 'answer') => request.delete(`/evaluation/datasets/${datasetId}/import/preview/temp-file/${tempFileId}?data_index=${dataIndex}&field=${field}`),
  // 确认导入（编辑态追加大库 + 慢环境 confirm 耗时可达分钟级，与 from-hub 同样放宽超时）
  confirmImport: (id, formData) => request.post(`/evaluation/datasets/${id}/import/confirm`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 600000
  }),
  // Agent 会话文件导入预览（multipart，文件字段名 files）
  previewSessionImport: (formData) => request.post('/evaluation/datasets/import/sessions/preview', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  // Agent 会话文件导入确认（multipart，文件字段名 files）
  confirmSessionImport: (formData) => request.post('/evaluation/datasets/import/sessions/confirm', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  // 从 anc-hub 拉取会话文件并导入（JSON：{file_ids, name, description, real_dataset_id}）
  // @deprecated W14 起废弃：Datasets.vue 已改走 hubImportApi 异步 job 链路
  // （POST /evaluation/datasets/import/hub-files + WS 进度推送），同步 10 分钟
  // 链路不再有页面调用入口；后端端点与客户端方法暂保留兼容外部调用方
  importSessionsFromHub: (data) => request.post('/evaluation/datasets/import/sessions/from-hub', data, { timeout: 600000 }),
  dataListV2: (id, params) => request.get(`/evaluation/datasets/${id}/data/v2`, { params }),
  addData: (id, data) => request.post(`/evaluation/datasets/${id}/data`, data),
  updateData: (datasetId, dataId, data) => request.put(`/evaluation/datasets/${datasetId}/data/${dataId}`, data),
  deleteData: (dataId) => request.delete(`/evaluation/datasets/data/${dataId}`),
  getDataFiles: (datasetId, dataId, field) => request.get(`/evaluation/datasets/${datasetId}/data/${dataId}/files`, { params: { field } }),
  deleteDataFile: (datasetId, dataId, fileId, field = 'answer') => request.delete(`/evaluation/datasets/${datasetId}/data/${dataId}/files/${fileId}?field=${field}`),
  // ==================== 数据集版本（M5 QE5-1） ====================
  // 版本列表（新→旧）：[{version_no, changelog, row_count, snapshot_id, created_by, created_at}]
  listVersions: (id) => request.get(`/evaluation/datasets/${id}/versions`),
  // 打版（以当前数据创建版本，body: {changelog}）→ 版本信息
  createVersion: (id, data) => request.post(`/evaluation/datasets/${id}/versions`, data),
  // 两版本行级 diff（params from/to 为版本号）→ {summary, rows, truncated}
  diffVersions: (id, fromVersion, toVersion) => request.get(`/evaluation/datasets/${id}/versions/diff`, {
    params: { from: fromVersion, to: toVersion }
  }),
  // 单版本详情（含快照字段结构）
  getVersion: (id, versionNo) => request.get(`/evaluation/datasets/${id}/versions/${versionNo}`),
  // 恢复数据集到指定版本（破坏性：当前活行软删后按版本快照重写）
  restoreVersion: (id, versionNo) => request.post(`/evaluation/datasets/${id}/versions/${versionNo}/restore`)
}

// anc-hub 文件API（file_query 透传，驱动「从 anc-hub 导入」页签远端浏览；阶段D 扩展文件树/预览/下载/匹配预览）
export const hubApi = {
  queryFiles: (data) => request.post('/hub-files/query', data),
  // 执行包文件树（登记表 file_index，零文件 IO）：{file_id, unpack_status, file_index:[{path,type,size,is_nested_zip}]}
  getFileTree: (fileId) => request.get(`/hub-files/${encodeURIComponent(fileId)}/tree`),
  // 嵌套 zip 内层清单：{file_id, path, entries:[...]}
  getNestedTree: (fileId, path) => request.get(`/hub-files/${encodeURIComponent(fileId)}/nested-tree`, { params: { path } }),
  // 文本类预览（JSON 返回 {content, mime, truncated}）；图片同样走此接口但需 blob 方式（见 getFileContentBlob）
  getFileContent: (fileId, path) => request.get(`/hub-files/${encodeURIComponent(fileId)}/content`, { params: { path } }),
  // 图片预览（content 接口对图片返回 FileResponse，走 blob 转 objectURL 展示，鉴权随 cookie）
  getFileContentBlob: (fileId, path) => request.get(`/hub-files/${encodeURIComponent(fileId)}/content`, { params: { path }, responseType: 'blob' }),
  // 下载包内任意文件（blob 保存，中文文件名后端已 URL 编码）
  downloadFile: (fileId, path) => request.get(`/hub-files/${encodeURIComponent(fileId)}/download`, { params: { path }, responseType: 'blob' }),
  // glob 匹配预览：{matched_count, sample_paths[], sample_data_index}
  matchPreview: (data) => request.post('/hub-files/match-preview', data),
  // 人工补传文件（multipart：files ×N + relative_paths ×N + target_dir；zip 后端自动解压）
  // → {file_id, added_count, added:[{path,size}], overwritten:[被覆盖路径]}
  uploadSupplements: (fileId, formData) => request.post(
    `/hub-files/${encodeURIComponent(fileId)}/supplements`,
    formData,
    { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 300000 }
  ),
  // 删除单个人工补传文件（原始执行包文件只读，后端拒删）→ {file_id, deleted, remaining_count}
  deleteSupplement: (fileId, path) => request.delete(`/hub-files/${encodeURIComponent(fileId)}/supplements`, { params: { path } })
}

// anc-hub 执行包批量导入任务API（阶段C）
export const hubImportApi = {
  // 创建导入 job（JSON：{file_ids, name, description, real_dataset_id}）→ {id(job_id), dataset_id, status, total_count, ...}
  createJob: (data) => request.post('/evaluation/datasets/import/hub-files', data),
  // 查询导入进度：{status, total_count, success_count, fail_count, fail_detail}
  getJob: (jobId) => request.get(`/hub-import-jobs/${encodeURIComponent(jobId)}`),
  // 重跑失败项（幂等，已成功的不重复导入）
  retryJob: (jobId) => request.post(`/hub-import-jobs/${encodeURIComponent(jobId)}/retry`)
}

// 统计API（ESM 规范要求显式扩展名：node:test 行为测试直接导入本模块）
export { statsApi, coverageApi, efficiencyApi } from './stats.js'

// 评测任务API
export const taskApi = {
  list: (params) => request.get('/evaluation/tasks', { params }),
  get: (id) => request.get(`/evaluation/tasks/${id}`),
  getConfig: (id) => request.get(`/evaluation/tasks/${id}/config`),
  create: (data) => request.post('/evaluation/tasks', data),
  update: (id, data) => request.put(`/evaluation/tasks/${id}`, data),
  delete: (id) => request.delete(`/evaluation/tasks/${id}`),
  run: (id) => request.post(`/evaluation/tasks/${id}/run`),
  stop: (id) => request.post(`/evaluation/tasks/${id}/stop`),
  executions: (id, params) => request.get(`/evaluation/tasks/${id}/executions`, { params }),
  getDatasetFields: (datasetId) => request.get(`/evaluation/tasks/datasets/${datasetId}/fields`),
  // 轮询专用轻端点（W14）：{status, progress, completed_questions, total} 四字段，
  // 供 useTaskPolling 高频拉取，替代每 8s 拉全量 detail
  getStatus: (id) => request.get(`/evaluation/tasks/${id}/status`),
  // 上传插件包/SKILL.md（插件评测手动指定来源）：zip 返回解包目录绝对路径，md 返回文本内容
  uploadPluginPackage: (formData) => request.post('/evaluation/tasks/plugin-eval/upload', formData, { headers: { 'Content-Type': 'multipart/form-data' } }),
  // 迁移单个旧版任务配置（steps 合并进 score_details、pre_scripts 转为 script_output 待评测数据行，幂等）
  migrateConfig: (id) => request.post(`/evaluation/tasks/${id}/migrate-config`),
  // 批量迁移全部旧版任务配置（幂等；返回 {total, migrated, skipped, errors}）
  migrateAllLegacy: () => request.post('/evaluation/tasks/migrate-legacy-all'),
  // 门禁状态轻量查询（M1：{verdict, gate_enabled, gate_result, ...}，可带 execution_number 查指定版本）
  gate: (id, params) => request.get(`/evaluation/tasks/${id}/gate`, { params }),
  // 校验器试跑（M2 QE2-1：单条样例试跑，script 类型复用脚本试跑通道；仅超管）
  debugVerifier: (data) => request.post('/evaluation/tasks/verifiers/debug', data),
  // 任务定时配置（M5 QE5-3：cron 5 段 + 时段限制 + 运行历史；未配置 get 返回 404）
  getSchedule: (id) => request.get(`/evaluation/tasks/${id}/schedule`),
  saveSchedule: (id, data) => request.put(`/evaluation/tasks/${id}/schedule`, data),
  deleteSchedule: (id) => request.delete(`/evaluation/tasks/${id}/schedule`),
  // 双引擎裁判对拍（M8 QE8-2）：同一批既有输出交 Claude Code × DeepSeek 两引擎评分
  judgeComparison: (id, data) => request.post(`/evaluation/tasks/${id}/judge-comparison`, data)
}

// 评测结果API
// 报告分享链接API（M5 QE5-4：免登录只读报告；明文 token 仅创建响应返回一次）
export const shareApi = {
  createLink: (taskId, data) => request.post(`/evaluation/tasks/${taskId}/share-links`, data),
  listLinks: (taskId) => request.get(`/evaluation/tasks/${taskId}/share-links`),
  revokeLink: (taskId, linkId) => request.delete(`/evaluation/tasks/${taskId}/share-links/${linkId}`),
  // 公开端点：分享页无登录态调用（无本地 token 时 Authorization 头自然为空）
  getReport: (token) => request.get(`/share/${token}/report`)
}

export const resultApi = {
  get: (id) => request.get(`/evaluation/results/${id}`),
  // history 传 include_result=false 走轻量版本列表（不带 result 大列）
  history: (id, params) => request.get(`/evaluation/results/${id}/history`, { params }),
  // 按执行序号取单版完整结果
  historyVersion: (id, executionNumber) => request.get(`/evaluation/results/${id}/history/${executionNumber}`),
  // 多次执行维度分数趋势（后端按 execution_number 倒序返回、最近一次在前，仅 completed 版本，
  // 前端使用前显式排序；limit 限制返回次数）
  dimensionTrend: (id, limit = 50) => request.get('/evaluation/results/' + id + '/dimension-trend', { params: { limit } }),
  // 人工标注闭环（M8 QE8-1）：待标队列（失败行/低分行优先）+ 金分提交（一行一条幂等覆盖）
  annotationQueue: (id, params) => request.get(`/evaluation/results/${id}/annotations`, { params }),
  submitAnnotation: (id, data) => request.post(`/evaluation/results/${id}/annotations`, data),
  // 批量导出（Excel/CSV）：rawResponse 取完整响应以读 blob 与 content-type
  exportBatch: (data) => request.post('/evaluation/results/batch-export', data, { responseType: 'blob', rawResponse: true })
}

// 插件市场API（插件评测「插件市场」来源）
export const pluginMarketplaceApi = {
  list: () => request.get('/evaluation/plugin-marketplaces'),
  create: (data) => request.post('/evaluation/plugin-marketplaces', data),
  sync: (id) => request.post(`/evaluation/plugin-marketplaces/${id}/sync`),
  remove: (id) => request.delete(`/evaluation/plugin-marketplaces/${id}`),
  plugins: (id) => request.get(`/evaluation/plugin-marketplaces/${id}/plugins`)
}

// 红队攻击样本库API（M3 安全评测）：列表 + 导入为数据集（可导入可编辑）
export const redteamApi = {
  // 内置样本库列表：[{name, version, categories: [{key, name, sample_count}]}]
  packs: () => request.get('/evaluation/redteam/packs'),
  // 样本库导入为数据集 → {dataset_id, dataset_name, pack, pack_version, categories, imported_count}
  importPack: (packName, data) => request.post(`/evaluation/redteam/packs/${encodeURIComponent(packName)}/import-dataset`, data)
}

// 批量操作API
export const batchApi = {
  run: (taskIds) => request.post('/evaluation/tasks/batch-run', { task_ids: taskIds }),
  delete: (taskIds) => request.post('/evaluation/tasks/batch-delete', { task_ids: taskIds }),
  stop: (taskIds) => request.post('/evaluation/tasks/batch-stop', { task_ids: taskIds })
}

// A/B 测试API
export const abTestApi = {
  listSessions: (params) => request.get('/ab-test/sessions', { params }),
  getSession: (id) => request.get(`/ab-test/sessions/${id}`),
  saveSession: (data) => request.post('/ab-test/sessions', data),
  deleteSession: (id) => request.delete(`/ab-test/sessions/${id}`),
  // M2 QE2-3：多轮配对记录的显著性复算（历史详情回放共用）
  computeSignificance: (rounds) => request.post('/ab-test/significance', { rounds }),
  // ==================== 模板版本对照（M5 QE5-2，任务化实验） ====================
  // 创建对照实验：{source_task_id, template_id, version_a, version_b, name?} →
  // 两个变体评测任务 + 对照记录（含 comparison 初始载荷）
  createTemplateSession: (data) => request.post('/ab-test/template-sessions', data)
}

// API Key 管理（M1 开发者设置）：明文 token 仅创建响应返回一次
export const apiKeyApi = {
  list: () => request.get('/api-keys'),
  create: (data) => request.post('/api-keys', data),
  revoke: (id) => request.delete(`/api-keys/${id}`)
}

// Webhook 管理（M1 开发者设置）：secret 仅创建响应返回一次
export const webhookApi = {
  list: () => request.get('/webhooks'),
  create: (data) => request.post('/webhooks', data),
  remove: (id) => request.delete(`/webhooks/${id}`),
  deliveries: (id, params) => request.get(`/webhooks/${id}/deliveries`, { params })
}

// 用户文档API
export const docsApi = {
  getUserGuide: () => request.get('/docs/user-guide'),
  updateUserGuide: (content) => request.put('/docs/user-guide', { content })
}

// 团队管理API（V2 新增；M4 QE4-3 增成员角色调整）
export const teamApi = {
  list: (params) => request.get('/teams', { params }),
  getMembers: (teamCode, params) => request.get(`/teams/${teamCode}/members`, { params }),
  setMemberStatus: (teamCode, userCode, isActive) => request.patch(`/teams/${teamCode}/members/${userCode}/status`, { is_active: isActive }),
  setMemberRole: (teamCode, userCode, role) => request.patch(`/teams/${teamCode}/members/${userCode}/role`, { role })
}

// 审计日志API（V2 新增；QE4-4 导出挂企业功能位 AUDIT_EXPORT_ENABLED）
export const auditApi = {
  list: (params) => request.get('/audit-logs', { params }),
  export: async (params) => {
    const resp = await request.get('/audit-logs/export', { params, responseType: 'blob' })
    return resp
  }
}

// 认证API（本地账号密码登录 + M4 QE4-1 OIDC SSO 登录方式公告）
export const authApi = {
  status: () => request.get('/auth/status'),
  register: (data) => request.post('/auth/register', data),
  login: (data) => request.post('/auth/login', data),
  logout: () => request.post('/auth/logout'),
  // 登录页渲染依据（匿名）：methods/register_enabled/oidc_configured
  methods: () => request.get('/auth/methods')
}

// OIDC SSO 管理（M4 QE4-1，超管开发者设置页）：config 读接口脱敏回显，
// client_secret 提交脱敏回显值=保留原值
export const ssoApi = {
  getConfig: () => request.get('/auth/oidc/config'),
  saveConfig: (data) => request.put('/auth/oidc/config', data),
  testConnectivity: (data) => request.post('/auth/oidc/test', data),
  listTeamMappings: () => request.get('/auth/oidc/team-mappings'),
  addTeamMapping: (data) => request.post('/auth/oidc/team-mappings', data),
  removeTeamMapping: (id) => request.delete(`/auth/oidc/team-mappings/${id}`)
}

// LDAP/AD 只读认证管理（M4 QE4-2，超管开发者设置页）：config 读接口脱敏
// 回显，bind_password 提交脱敏回显值=保留原值
export const ldapApi = {
  getConfig: () => request.get('/auth/ldap/config'),
  saveConfig: (data) => request.put('/auth/ldap/config', data),
  testConnectivity: (data) => request.post('/auth/ldap/test', data)
}

// License 管理（M6 QE6-1，超管开发者设置页）：上传为 multipart 文件；
// status 返回生效档位 effective_tier/降级原因/规模与功能位
export const licenseApi = {
  status: () => request.get('/admin/license/status'),
  upload: (formData) => request.post('/admin/license', formData)
}

// OSS 直传上传API（企业内网 OSS 可选功能，地址经 VITE_OSS_BASE_URL 构建时注入；未配置则功能不可用）
const OSS_BASE_URL = import.meta.env?.VITE_OSS_BASE_URL || ''
const OSS_TENANT_CODE = import.meta.env?.VITE_OSS_TENANT_CODE || ''

export const ossApi = {
  get enabled() {
    return Boolean(OSS_BASE_URL)
  },
  ossUrlPattern: OSS_BASE_URL
    ? new RegExp(`^https?://${OSS_BASE_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/api-oss/public/oss/file/hashDownload/.+\\.\\w+$`)
    : null,
  /**
   * 上传文件到 OSS
   * @param {File} file - 要上传的文件
   * @returns {Promise<string>} - 返回 download URL
   */
  upload: async (file) => {
    if (!OSS_BASE_URL) {
      throw new Error('OSS 上传未配置（缺少 VITE_OSS_BASE_URL）')
    }
    const ossRequest = axios.create({
      baseURL: OSS_BASE_URL,
      timeout: 60000,
      withCredentials: true
    })
    ossRequest.interceptors.request.use(config => {
      if (OSS_TENANT_CODE) {
        config.headers['tenantCode'] = OSS_TENANT_CODE
      }
      return config
    })
    const formData = new FormData()
    formData.append('bucket', 'uap')
    formData.append('businessKey', 'default')
    formData.append('publicRead', '1')
    formData.append('file', file)
    const response = await ossRequest.post('/api-oss/oss/file/v2/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
    // 从响应中解析 download URL
    const data = response.data
    const downloadUrl = data?.data?.downUrl || ''
    return downloadUrl
  }
}
