/**
 * 并发池配置校验（纯函数）单元测试
 * 文件: frontend/tests/unit/judge-pool-config.test.mjs
 * 运行: node --test frontend/tests/unit/judge-pool-config.test.mjs
 *
 * 契约：
 * - validatePoolConfig(form) 返回 string（错误提示）或 null（通过）
 * - 只校验「表单里提供了值」的字段：PUT /judge-pool/config 支持局部更新，
 *   未提供的字段由后端在 DB 现值合并后再校验（心跳 < 租约一半的关系校验同理）
 * - 范围与后端 JudgePoolConfigPayload 的 Field(ge/le) 保持一致
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import { validatePoolConfig } from '../../src/utils/judgePoolConfig.js'

test('heartbeat must be less than half of lease ttl', () => {
  assert.match(validatePoolConfig({ lease_ttl_seconds: 60, heartbeat_interval_seconds: 30 }), /心跳/)
})

test('valid config returns null', () => {
  assert.equal(validatePoolConfig({ lease_ttl_seconds: 180, heartbeat_interval_seconds: 30 }), null)
})

test('limit must be positive', () => {
  assert.match(validatePoolConfig({ lease_ttl_seconds: 180, heartbeat_interval_seconds: 30, graph_limit: 0 }), /并发上限/)
})

test('limit must be an integer within 1~128', () => {
  assert.match(validatePoolConfig({ graph_limit: 1.5 }), /并发上限/)
  assert.match(validatePoolConfig({ plugin_limit: 129 }), /并发上限/)
  assert.equal(validatePoolConfig({ plugin_limit: 128 }), null)
})

test('wait timeout out of range is rejected', () => {
  assert.match(validatePoolConfig({ wait_timeout_seconds: 9 }), /等待超时/)
  assert.match(validatePoolConfig({ wait_timeout_seconds: 7201 }), /等待超时/)
})

test('lease ttl and heartbeat ranges are enforced', () => {
  assert.match(validatePoolConfig({ lease_ttl_seconds: 29 }), /租约有效期/)
  assert.match(validatePoolConfig({ heartbeat_interval_seconds: 4 }), /心跳间隔/)
})

test('partial config (nothing provided) is allowed — backend merges with stored values', () => {
  assert.equal(validatePoolConfig({}), null)
  assert.equal(validatePoolConfig(), null)
})
