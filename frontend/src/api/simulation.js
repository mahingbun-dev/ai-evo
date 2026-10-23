/**
 * 仿真评测 API（M7 QE7-5）
 *
 * 对应后端 /api/v1/simulation（app/api/simulation_api.py）：
 * - 场景 CRUD（创建/更新即发布门禁——无终态校验器 400）
 * - 运行：发起（配额校验+celery 入队）/详情（trial 明细+指标）/停止
 */
import request from './index'

export const simulationApi = {
  // 场景列表 → [{id, name, description, config, version, created_by, ...}]
  listScenarios: (params) => request.get('/simulation/scenarios', { params }),
  getScenario: (id) => request.get(`/simulation/scenarios/${id}`),
  createScenario: (data) => request.post('/simulation/scenarios', data),
  updateScenario: (id, data) => request.put(`/simulation/scenarios/${id}`, data),
  deleteScenario: (id) => request.delete(`/simulation/scenarios/${id}`),
  // 发起运行 → {run_id, scenario_id, status, trials_total}（配额满 429）
  run: (id) => request.post(`/simulation/scenarios/${id}/run`),
  // 场景的运行历史（不带 result）
  listRuns: (id, params) => request.get(`/simulation/scenarios/${id}/runs`, { params }),
  // 运行详情（含 result：trials/pass_k/average_turns/tool_waste_rate/...）
  getRun: (runId) => request.get(`/simulation/runs/${runId}`),
  // 停止运行（Redis 停止信号 + revoke celery）
  stopRun: (runId) => request.post(`/simulation/runs/${runId}/stop`)
}

export default simulationApi
