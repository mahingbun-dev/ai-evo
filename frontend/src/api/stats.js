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
