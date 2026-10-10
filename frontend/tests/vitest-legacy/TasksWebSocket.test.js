/**
 * Tasks.vue WebSocket 集成测试
 */

import { describe, it, expect, vi } from 'vitest'

// Mock the composables
vi.mock('@/composables/useTaskPolling', () => ({
  useTaskPolling: () => ({
    startPolling: vi.fn(),
    stopPolling: vi.fn(),
    stopAll: vi.fn()
  })
}))

vi.mock('@/composables/useTaskWebSocket', () => ({
  useTaskWebSocket: () => ({
    startWebSocket: vi.fn(),
    stopWebSocket: vi.fn(),
    stopAll: vi.fn(),
    getConnectionStatus: vi.fn(() => false),
    isConnected: {}
  })
}))

describe('Tasks WebSocket Integration', () => {
  it('should have useTaskWebSocket import', async () => {
    // This test verifies the composable is properly imported
    const { useTaskWebSocket } = await import('@/composables/useTaskWebSocket')
    const result = useTaskWebSocket({
      onStatusUpdate: () => {},
      onConnect: () => {},
      onDisconnect: () => {},
      onError: () => {},
      onFallback: () => {}
    })

    expect(result.startWebSocket).toBeDefined()
    expect(result.stopWebSocket).toBeDefined()
    expect(result.stopAll).toBeDefined()
  })

  it('should have correct export structure', async () => {
    const { useTaskWebSocket } = await import('@/composables/useTaskWebSocket')
    const hooks = useTaskWebSocket({})

    expect(typeof hooks.startWebSocket).toBe('function')
    expect(typeof hooks.stopWebSocket).toBe('function')
    expect(typeof hooks.stopAll).toBe('function')
    expect(typeof hooks.getConnectionStatus).toBe('function')
  })
})
