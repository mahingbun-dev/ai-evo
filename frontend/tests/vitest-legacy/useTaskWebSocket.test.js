/**
 * useTaskWebSocket 单元测试
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Mock WebSocket
class MockWebSocket {
  constructor(url) {
    this.url = url
    this.readyState = 1 // OPEN
    this.onopen = null
    this.onmessage = null
    this.onerror = null
    this.onclose = null
    this.sentMessages = []

    // Simulate async open
    setTimeout(() => {
      if (this.onopen) this.onopen({})
    }, 0)
  }

  send(data) {
    this.sentMessages.push(data)
  }

  close() {
    this.readyState = 3 // CLOSED
    if (this.onclose) this.onclose({})
  }
}

// Mock window.location
const mockLocation = {
  protocol: 'http:',
  host: 'localhost:8100'
}

// Mock Vue composition API functions
vi.stubGlobal('WebSocket', MockWebSocket)
vi.stubGlobal('window', {
  location: mockLocation
})

describe('useTaskWebSocket', () => {
  let useTaskWebSocket
  let mockOnStatusUpdate
  let mockOnError
  let mockOnConnect
  let mockOnDisconnect
  let mockOnFallback

  beforeEach(async () => {
    vi.clearAllMocks()
    // Dynamic import to get fresh module
    const module = await import('../../src/composables/useTaskWebSocket.js')
    useTaskWebSocket = module.useTaskWebSocket

    mockOnStatusUpdate = vi.fn()
    mockOnError = vi.fn()
    mockOnConnect = vi.fn()
    mockOnDisconnect = vi.fn()
    mockOnFallback = vi.fn()
  })

  afterEach(() => {
    // Cleanup
  })

  it('should create useTaskWebSocket hook', () => {
    const result = useTaskWebSocket({
      onStatusUpdate: mockOnStatusUpdate,
      onError: mockOnError,
      onConnect: mockOnConnect,
      onDisconnect: mockOnDisconnect,
      onFallback: mockOnFallback
    })

    expect(result).toBeDefined()
    expect(result.startWebSocket).toBeDefined()
    expect(result.stopWebSocket).toBeDefined()
    expect(result.stopAll).toBeDefined()
  })

  it('should return connection status', () => {
    const result = useTaskWebSocket({
      onStatusUpdate: mockOnStatusUpdate,
      onError: mockOnError,
      onConnect: mockOnConnect,
      onDisconnect: mockOnDisconnect,
      onFallback: mockOnFallback
    })

    const status = result.getConnectionStatus('test-task-1')
    expect(status).toBe(false)
  })

  it('should have isConnected ref', () => {
    const result = useTaskWebSocket({
      onStatusUpdate: mockOnStatusUpdate,
      onError: mockOnError,
      onConnect: mockOnConnect,
      onDisconnect: mockOnDisconnect,
      onFallback: mockOnFallback
    })

    expect(result.isConnected).toBeDefined()
  })
})
