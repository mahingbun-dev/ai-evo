/**
 * 并发池配置表单校验（纯函数，无 Vue 依赖，供 node:test 直接导入）
 *
 * 约定：
 * - 只校验表单里「提供了值」的字段。PUT /judge-pool/config 是局部更新语义，
 *   未提供的字段由后端在 DB 现值合并之后再校验（心跳 < 租约一半的关系校验同理）；
 * - 数值范围与后端 JudgePoolConfigPayload 的 Field(ge/le) 保持一一对应，
 *   前端先挡住明显非法的输入，减少一次必然 400 的往返。
 */

const LIMIT_FIELDS = ['graph_limit', 'claude_agent_limit', 'harness_limit', 'plugin_limit']

/** undefined / null / '' 视为「未提供」，跳过校验 */
const isProvided = (value) => value !== undefined && value !== null && value !== ''

const toNumber = (value) => Number(value)

/**
 * 校验并发池配置表单
 * @param {object} [form] - 表单对象（可能只包含部分字段）
 * @returns {string|null} 错误提示；校验通过返回 null
 */
export function validatePoolConfig(form = {}) {
  const values = form || {}

  for (const key of LIMIT_FIELDS) {
    if (!isProvided(values[key])) continue
    const value = toNumber(values[key])
    if (!Number.isInteger(value) || value < 1 || value > 128) {
      return '每类引擎的并发上限必须是 1~128 的整数'
    }
  }

  let ttl = null
  if (isProvided(values.lease_ttl_seconds)) {
    ttl = toNumber(values.lease_ttl_seconds)
    if (!Number.isInteger(ttl) || ttl < 30 || ttl > 3600) return '租约有效期必须是 30~3600 秒'
  }

  let heartbeat = null
  if (isProvided(values.heartbeat_interval_seconds)) {
    heartbeat = toNumber(values.heartbeat_interval_seconds)
    if (!Number.isInteger(heartbeat) || heartbeat < 5 || heartbeat > 600) {
      return '心跳间隔必须是 5~600 秒'
    }
  }

  // 两个字段同时提供时才做关系校验；局部更新交给后端合并现值后判断
  if (ttl !== null && heartbeat !== null && heartbeat * 2 >= ttl) {
    return '心跳间隔必须小于租约有效期的一半'
  }

  if (isProvided(values.wait_timeout_seconds)) {
    const wait = toNumber(values.wait_timeout_seconds)
    if (!Number.isInteger(wait) || wait < 10 || wait > 7200) return '等待超时必须是 10~7200 秒'
  }

  if (isProvided(values.scan_interval_seconds)) {
    const scan = toNumber(values.scan_interval_seconds)
    if (!Number.isInteger(scan) || scan < 10 || scan > 600) return '扫描间隔必须是 10~600 秒'
  }

  if (isProvided(values.audit_retention_days)) {
    const days = toNumber(values.audit_retention_days)
    if (!Number.isInteger(days) || days < 1 || days > 365) return '审计保留天数必须是 1~365 天'
  }

  return null
}
