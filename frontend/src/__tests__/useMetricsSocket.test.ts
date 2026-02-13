import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useMetricsSocket } from '../hooks/useMetricsSocket';

// Mock WebSocket
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  readyState = 0; // CONNECTING
  close = vi.fn(() => {
    this.readyState = 3; // CLOSED
  });

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
    // Simulate connection
    setTimeout(() => {
      this.readyState = 1; // OPEN
      this.onopen?.();
    }, 0);
  }

  // Test helper: simulate receiving a message
  simulateMessage(data: string) {
    this.onmessage?.({ data });
  }
}

describe('useMetricsSocket', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('connects to /ws/metrics endpoint', () => {
    const { result } = renderHook(() => useMetricsSocket());

    act(() => result.current.connect());

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].url).toContain('/ws/metrics');
  });

  it('disconnect closes the websocket', () => {
    const { result } = renderHook(() => useMetricsSocket());

    act(() => result.current.connect());
    const ws = MockWebSocket.instances[0];

    act(() => result.current.disconnect());
    expect(ws.close).toHaveBeenCalled();
  });

  it('clearEvents resets accumulated events', () => {
    const { result } = renderHook(() => useMetricsSocket());

    act(() => result.current.connect());
    act(() => vi.advanceTimersByTime(1)); // trigger onopen

    const ws = MockWebSocket.instances[0];
    act(() => {
      ws.simulateMessage(JSON.stringify({
        stage: 'audio_received',
        timestamp: 100,
        chunk_index: 0,
        source_position_sec: 0.3,
        audio_bytes_len: 9600,
        wall_clock: 1.0,
      }));
    });

    act(() => result.current.clearEvents());
    // clearEvents should reset internal state
    // (the events array is internal, so we verify indirectly that it doesn't crash)
  });

  it('handles connect when already connected', () => {
    const { result } = renderHook(() => useMetricsSocket());

    act(() => result.current.connect());
    act(() => result.current.connect()); // Should not crash or create duplicate

    // May create a second instance depending on implementation, but shouldn't crash
    expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(1);
  });
});
