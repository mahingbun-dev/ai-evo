/**
 * LLM 裁判并发池 API（仅超管，后端 /judge-pool/* 已挂 ensure_super_admin）
 *
 * 响应经 api/index.js 的响应拦截器后直接是响应体：
 * - getStatus → {redis_available, degraded, engines:{graph:{...}}, leases:[...], error?}
 * - getConfig → 扁平配置对象（与后端 JudgePoolConfig.to_dict() 一致）
 * - updateConfig → 配置对象 + redis_synced（false 表示已落库但 Redis 未同步）
 * - releaseLease / releaseAll → {released}；Redis 不可用时后端返回 503
 * - getAudit → {total, items:[...]}；engine/event 非法时后端返回 400
 */
import request from './index.js'

export const judgePoolApi = {
  getStatus: () => request.get('/judge-pool/status'),
  getConfig: () => request.get('/judge-pool/config'),
  updateConfig: (data) => request.put('/judge-pool/config', data),
  releaseLease: (leaseId, engine) =>
    request.post(`/judge-pool/leases/${encodeURIComponent(leaseId)}/release`, null, { params: { engine } }),
  releaseAll: (engine) =>
    request.post('/judge-pool/release-all', null, { params: engine ? { engine } : {} }),
  getAudit: (params) => request.get('/judge-pool/audit', { params })
}
