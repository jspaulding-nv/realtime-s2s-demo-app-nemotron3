import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useTimingTracker } from '../hooks/useTimingTracker';

describe('useTimingTracker', () => {
  it('returns a stable object reference (useMemo)', () => {
    const { result, rerender } = renderHook(() => useTimingTracker());
    const first = result.current;
    rerender();
    const second = result.current;
    expect(first).toBe(second);
  });

  it('starts with zero counts', () => {
    const { result } = renderHook(() => useTimingTracker());
    expect(result.current.getSendCount()).toBe(0);
    expect(result.current.getReceiveCount()).toBe(0);
    expect(result.current.getCumulativeOutputDuration()).toBe(0);
    expect(result.current.getSourcePosition()).toBe(0);
    expect(result.current.getEvents()).toEqual([]);
  });

  it('logChunkSent increments send count', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600));
    expect(result.current.getSendCount()).toBe(1);

    act(() => result.current.logChunkSent(9600));
    act(() => result.current.logChunkSent(9600));
    expect(result.current.getSendCount()).toBe(3);
  });

  it('computes source position from send count', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    // 4800 samples / 16000 Hz = 0.3s per chunk
    for (let i = 0; i < 10; i++) {
      act(() => result.current.logChunkSent(9600));
    }
    expect(result.current.getSourcePosition()).toBeCloseTo(3.0, 5);
  });

  it('logAudioReceived increments receive count and cumulative output', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    // 32000 bytes / 2 bytes per sample = 16000 samples / 16000 Hz = 1.0s
    act(() => result.current.logAudioReceived(32000));
    expect(result.current.getReceiveCount()).toBe(1);
    expect(result.current.getCumulativeOutputDuration()).toBeCloseTo(1.0, 5);

    act(() => result.current.logAudioReceived(32000));
    expect(result.current.getReceiveCount()).toBe(2);
    expect(result.current.getCumulativeOutputDuration()).toBeCloseTo(2.0, 5);
  });

  it('startTest resets all state', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600));
    act(() => result.current.logChunkSent(9600));
    act(() => result.current.logAudioReceived(32000));
    expect(result.current.getSendCount()).toBe(2);
    expect(result.current.getReceiveCount()).toBe(1);
    expect(result.current.getEvents().length).toBe(3);

    act(() => result.current.startTest());
    expect(result.current.getSendCount()).toBe(0);
    expect(result.current.getReceiveCount()).toBe(0);
    expect(result.current.getCumulativeOutputDuration()).toBe(0);
    expect(result.current.getEvents()).toEqual([]);
  });

  it('getEvents returns events with correct stages and timestamps', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600));
    act(() => result.current.logAudioReceived(32000));
    act(() => result.current.logChunkSent(9600));

    const events = result.current.getEvents();
    expect(events).toHaveLength(3);
    expect(events[0].stage).toBe('chunk_sent');
    expect(events[1].stage).toBe('audio_received');
    expect(events[2].stage).toBe('chunk_sent');

    // All timestamps should be non-negative
    events.forEach(e => expect(e.timestamp).toBeGreaterThanOrEqual(0));

    // Chunk indices for sent events
    expect(events[0].chunkIndex).toBe(0);
    expect(events[2].chunkIndex).toBe(1);
  });

  it('getEvents returns a copy (not the internal array)', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600));

    const events1 = result.current.getEvents();
    const events2 = result.current.getEvents();
    expect(events1).not.toBe(events2);
    expect(events1).toEqual(events2);
  });

  it('individual callbacks are stable across renders', () => {
    const { result, rerender } = renderHook(() => useTimingTracker());
    const { logChunkSent, logAudioReceived, startTest, getSendCount } = result.current;

    rerender();
    expect(result.current.logChunkSent).toBe(logChunkSent);
    expect(result.current.logAudioReceived).toBe(logAudioReceived);
    expect(result.current.startTest).toBe(startTest);
    expect(result.current.getSendCount).toBe(getSendCount);
  });
});
