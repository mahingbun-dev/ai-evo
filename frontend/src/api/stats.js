import request from './index.js'

export const statsApi = {
  getRecords(params) {
    return request.get('/stats/records', { params })
  },

  getSummary(params) {
    return request.get('/stats/summary', { params })
  },

  getTrends(params) {
    return request.get('/stats/trends', { params })
  },

  // 周报归档（M5 QE5-3）：每周一 08:00 聚合上周覆盖度/提效/门禁通过率
  listWeeklyReports(params) {
    return request.get('/stats/weekly-reports', { params })
  },

  getWeeklyReport(id) {
    return request.get(`/stats/weekly-reports/${id}`)
  },

  // 裁判-人工一致性报告（M8 QE8-1）：Pearson/Spearman + 分带 Cohen's kappa，
  // task_id 为空=全任务聚合（附 by_task 摘要）
  judgeAgreement(params) {
    return request.get('/stats/judge-agreement', { params })
  },
}

export const coverageApi = {
  getConfig() {
    return request.get('/coverage/config')
  },

  updateConfig(data) {
    return request.put('/coverage/config', data)
  },

  upsertPlan(data) {
    return request.post('/coverage/plans', data)
  },

  getCoverage(params) {
    return request.get('/coverage/stats', { params })
  },
}

export const efficiencyApi = {
  updateManualHours(data) {
    return request.put('/efficiency/manual-hours', data)
  },

  getSummary(params) {
    return request.get('/efficiency/summary', { params })
  },
}
