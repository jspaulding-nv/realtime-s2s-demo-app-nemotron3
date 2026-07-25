import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useTimingTracker } from '../hooks/useTimingTracker';

const INPUT_PCM = {
  inputPcmSha256: '0'.repeat(64),
  inputPcmSampleCount: 9600,
};

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

  it('records playback schedule and queue sample telemetry', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logPlaybackScheduled({
      playbackClockSessionId: 7,
      timestampMs: performance.now(),
      schedulePerformanceMs: performance.now(),
      audioContextTimeAtScheduleSeconds: 10,
      scheduledStartContextSeconds: 15.2,
      scheduledEndContextSeconds: 15.295238,
      scheduledStartContextFrameFloor: 243200,
      scheduledEndContextFrameExclusive: 244724,
      projectedScheduledStartClientMs: performance.now() + 5200,
      audioBytes: 3200,
      sourceDurationSeconds: 0.1,
      scheduledDurationSeconds: 0.095238,
      waitBeforePlaybackSeconds: 5.2,
      queueDepthSeconds: 5.295238,
      playbackRate: 1.05,
      playbackMode: 'catch-up',
      modeChanged: true,
      aboveTarget: true,
      aboveLimit: false,
    }));
    act(() => result.current.logPlaybackQueueSample(5.1, 1.05, 'catch-up'));

    expect(result.current.getEvents()).toEqual([
      expect.objectContaining({
        stage: 'playback_chunk_scheduled',
        playbackClockSessionId: 7,
        audioBytes: 3200,
        queueDepthSec: 5.295238,
        playbackRate: 1.05,
        playbackMode: 'catch-up',
      }),
      expect.objectContaining({
        stage: 'playback_queue_sample',
        queueDepthSec: 5.1,
        playbackRate: 1.05,
      }),
    ]);
  });

  it('records a transcript-free terminal boundary', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());
    act(() => result.current.logServerTerminal('completed', 1234.5));

    expect(result.current.getEvents()).toEqual([
      expect.objectContaining({
        stage: 'server_terminal',
        terminalStatus: 'completed',
        audioBytes: 0,
      }),
    ]);
  });

  it('records the explicit end-of-input boundary after sent source audio', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600));
    act(() => result.current.logInputEnded(1300));

    expect(result.current.getEvents().at(-1)).toMatchObject({
      stage: 'input_ended',
      chunkIndex: -1,
      sourcePositionSec: 0.3,
      audioBytes: 0,
    });
  });

  it('records privacy-safe browser clock samples', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logPlaybackClockSample({
      playbackClockSessionId: 7,
      clockSampleSequence: 3,
      clockSampleReason: 'interval',
      clockSamplePerformanceClientMs: 1234.25,
      clockSamplePerformanceBeforeClientMs: 1234.2,
      clockSamplePerformanceAfterClientMs: 1234.3,
      clockSampleContextSeconds: 2.5,
      clockSampleOutputContextSeconds: 2.48,
      clockSampleOutputPerformanceClientMs: 1214.1,
      clockSampleBasis: 'get_output_timestamp',
      clockSampleQueueEndContextSeconds: 5.75,
    }));

    expect(result.current.getEvents()).toEqual([
      expect.objectContaining({
        stage: 'playback_clock_sample',
        chunkIndex: -1,
        audioBytes: 0,
        playbackClockSessionId: 7,
        clockSampleSequence: 3,
        clockSampleReason: 'interval',
        clockSamplePerformanceClientMs: 1234.25,
        clockSamplePerformanceBeforeClientMs: 1234.2,
        clockSamplePerformanceAfterClientMs: 1234.3,
        clockSampleContextSec: 2.5,
        clockSampleOutputContextSec: 2.48,
        clockSampleOutputPerformanceClientMs: 1214.1,
        clockSampleBasis: 'get_output_timestamp',
        clockSampleQueueEndContextSec: 5.75,
      }),
    ]);
  });

  it('records the playback condition at session start', () => {
    const { result } = renderHook(() => useTimingTracker());

    act(() => result.current.startTest({ adaptivePlaybackEnabled: false }));

    expect(result.current.getEvents()).toEqual([
      expect.objectContaining({
        stage: 'playback_session_started',
        timestamp: 0,
        adaptivePlaybackEnabled: false,
      }),
    ]);
  });

  it('records a validated input ledger and same-clock frame delays', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 1,
      sampleRateHz: 16000,
      sourceSampleStart: 4800,
      sourceSampleEndExclusive: 9600,
      emittedAtMs: 1600,
      inputSampleZeroClientMs: 1000,
    }));

    const audioFrame = {
      metadata: {
        type: 'audio_frame' as const,
        protocolVersion: 1 as const,
        streamGeneration: 4,
        parentSequenceId: 2,
        audioFrameId: 3,
        audioBytes: 3200,
        sampleRateHz: 16000,
        channels: 1,
        bytesPerSample: 2,
        sourceStartMs: null,
        sourceEndMs: 500,
      },
      binaryReceivedAtMs: 1800,
    };
    act(() => result.current.logAudioReceived(3200, audioFrame));
    act(() => result.current.logPlaybackScheduled({
      playbackClockSessionId: 7,
      timestampMs: 1801,
      schedulePerformanceMs: 1801,
      audioContextTimeAtScheduleSeconds: 10,
      scheduledStartContextSeconds: 12,
      scheduledEndContextSeconds: 12.1,
      scheduledStartContextFrameFloor: 192000,
      scheduledEndContextFrameExclusive: 193600,
      projectedScheduledStartClientMs: 3801,
      audioBytes: 3200,
      sourceDurationSeconds: 0.1,
      scheduledDurationSeconds: 0.1,
      waitBeforePlaybackSeconds: 2,
      queueDepthSeconds: 2.1,
      playbackRate: 1,
      playbackMode: 'normal',
      modeChanged: false,
      aboveTarget: false,
      aboveLimit: false,
      audioFrame,
    }));
    act(() => result.current.logAudioParentComplete({
      metadata: {
        type: 'audio_parent_complete',
        protocolVersion: 1,
        streamGeneration: 4,
        parentSequenceId: 2,
        audioFrameCount: 4,
        audioBytes: 12800,
        sourceStartMs: null,
        sourceEndMs: 500,
      },
      receivedAtMs: 1900,
    }));

    const [
      firstChunk,
      secondChunk,
      received,
      scheduled,
      parentComplete,
    ] = result.current.getEvents();
    expect(firstChunk).toMatchObject({
      stage: 'chunk_sent',
      inputSampleZeroClientMs: 1000,
      inputChunkEmittedClientMs: 1300,
      inputSourceSampleStart: 0,
      inputSourceSampleEndExclusive: 4800,
      inputSampleRateHz: 16000,
      inputPcmSha256: INPUT_PCM.inputPcmSha256,
      inputPcmSampleCount: INPUT_PCM.inputPcmSampleCount,
      inputLedgerValid: true,
    });
    expect(secondChunk).toMatchObject({
      inputSourceSampleStart: 4800,
      inputSourceSampleEndExclusive: 9600,
      inputLedgerValid: true,
    });
    expect(received).toMatchObject({
      audioMetadataProtocolVersion: 1,
      streamGeneration: 4,
      parentSequenceId: 2,
      audioFrameId: 3,
      sourceStartMs: null,
      sourceEndMs: 500,
      sourceTimingBasis: 'audio_processed/nonsemantic',
      binaryReceiptClientMs: 1800,
      sourceEndBoundaryClientMs: 1500,
      sourceEndToBinaryReceiptMs: 300,
    });
    expect(scheduled).toMatchObject({
      playbackClockSessionId: 7,
      schedulePerformanceClientMs: 1801,
      audioContextTimeAtScheduleSec: 10,
      scheduledStartContextSec: 12,
      scheduledEndContextSec: 12.1,
      projectedScheduledStartClientMs: 3801,
      sourceEndToBinaryReceiptMs: 300,
      sourceEndToProjectedScheduledStartMs: 2301,
    });
    expect(parentComplete).toMatchObject({
      stage: 'audio_parent_complete',
      parentSequenceId: 2,
      audioFrameCount: 4,
      audioBytes: 12800,
      parentCompleteReceivedClientMs: 1900,
      sourceEndToParentCompleteMs: 400,
    });
  });

  it('leaves source-boundary delays unavailable after a broken ledger', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 1,
      sourceSampleEndExclusive: 4801,
      emittedAtMs: 1301,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logAudioReceived(3200, {
      metadata: {
        type: 'audio_frame',
        protocolVersion: 1,
        streamGeneration: 1,
        parentSequenceId: 0,
        audioFrameId: 0,
        audioBytes: 3200,
        sampleRateHz: 16000,
        channels: 1,
        bytesPerSample: 2,
        sourceStartMs: null,
        sourceEndMs: 100,
      },
      binaryReceivedAtMs: 1500,
    }));

    const [chunk, received] = result.current.getEvents();
    expect(chunk.inputLedgerValid).toBe(false);
    expect(received.sourceTimingBasis).toBe(
      'audio_processed/nonsemantic',
    );
    expect(received.sourceEndBoundaryClientMs).toBeUndefined();
    expect(received.sourceEndToBinaryReceiptMs).toBeUndefined();
  });

  it('does not project a source boundary beyond the recorded sample ledger', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logAudioReceived(3200, {
      metadata: {
        type: 'audio_frame',
        protocolVersion: 1,
        streamGeneration: 1,
        parentSequenceId: 0,
        audioFrameId: 0,
        audioBytes: 3200,
        sampleRateHz: 16000,
        channels: 1,
        bytesPerSample: 2,
        sourceStartMs: null,
        sourceEndMs: 500,
      },
      binaryReceivedAtMs: 1600,
    }));

    const received = result.current.getEvents()[1];
    expect(received.sourceTimingBasis).toBe(
      'audio_processed/nonsemantic',
    );
    expect(received.sourceEndBoundaryClientMs).toBeUndefined();
    expect(received.sourceEndToBinaryReceiptMs).toBeUndefined();
  });

  it('rejects a chunk emitted before its source-end boundary', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1298,
      inputSampleZeroClientMs: 1000,
    }));

    expect(result.current.getEvents()[0]).toMatchObject({
      stage: 'chunk_sent',
      inputSampleZeroClientMs: 1000,
      inputChunkEmittedClientMs: 1298,
      inputLedgerValid: false,
    });
  });

  it('accepts sub-millisecond timer rounding at the source boundary', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1299.5,
      inputSampleZeroClientMs: 1000,
    }));

    expect(result.current.getEvents()[0].inputLedgerValid).toBe(true);
  });

  it('uses exact render-clock boundaries instead of the approximate client anchor', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      // The estimated client anchor is deliberately 8 ms late. This would
      // fail the legacy timer check, but the render-thread boundary proves
      // that the common-clock send was not issued early.
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1008,
      inputSourceBoundaryContextFrame: 8000,
      inputSourceBoundaryDeliveredAfterContextFrame: 8064,
      inputSourceBoundaryReceivedContextFrameBefore: 8080,
      inputSourceBoundaryReceivedContextFrameAfter: 8080,
      inputSourceBoundaryReceivedClientMs: 1299.5,
      inputChunkEmittedContextFrame: 8090,
    }));

    expect(result.current.getEvents()[0]).toMatchObject({
      inputLedgerValid: true,
      inputSourceBoundaryContextFrame: 8000,
      inputSourceBoundaryDeliveredAfterContextFrame: 8064,
    });
  });

  it('rejects partial or inconsistent render-clock boundary evidence', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1000,
      inputSourceBoundaryContextFrame: 8000,
    }));
    expect(result.current.getEvents()[0].inputLedgerValid).toBe(false);

    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1000,
      inputSourceBoundaryContextFrame: 8000,
      inputSourceBoundaryDeliveredAfterContextFrame: 7999,
      inputSourceBoundaryReceivedContextFrameBefore: 8000,
      inputSourceBoundaryReceivedContextFrameAfter: 8000,
      inputSourceBoundaryReceivedClientMs: 1299,
      inputChunkEmittedContextFrame: 8000,
    }));
    expect(result.current.getEvents()[0].inputLedgerValid).toBe(false);
  });

  it('rejects a common-clock send after a stalled main thread', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1450,
      inputSampleZeroClientMs: 1000,
      inputSourceBoundaryContextFrame: 8000,
      inputSourceBoundaryDeliveredAfterContextFrame: 8064,
      inputSourceBoundaryReceivedContextFrameBefore: 9700,
      inputSourceBoundaryReceivedContextFrameAfter: 9700,
      inputSourceBoundaryReceivedClientMs: 1449,
      inputChunkEmittedContextFrame: 9700,
    }));

    expect(result.current.getEvents()[0].inputLedgerValid).toBe(false);
  });

  it('accepts late chunks and retains their real source-boundary delay', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());

    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1600,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 1,
      sampleRateHz: 16000,
      sourceSampleStart: 4800,
      sourceSampleEndExclusive: 9600,
      emittedAtMs: 1700,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logAudioReceived(3200, {
      metadata: {
        type: 'audio_frame',
        protocolVersion: 1,
        streamGeneration: 1,
        parentSequenceId: 0,
        audioFrameId: 0,
        audioBytes: 3200,
        sampleRateHz: 16000,
        channels: 1,
        bytesPerSample: 2,
        sourceStartMs: null,
        sourceEndMs: 500,
      },
      binaryReceivedAtMs: 2000,
    }));

    const [first, second, received] = result.current.getEvents();
    expect(first.inputLedgerValid).toBe(true);
    expect(second.inputLedgerValid).toBe(true);
    expect(received).toMatchObject({
      sourceEndBoundaryClientMs: 1500,
      sourceEndToBinaryReceiptMs: 500,
    });
  });

  it('invalidates a ledger when its PCM digest or total changes', () => {
    const { result } = renderHook(() => useTimingTracker());
    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      inputPcmSha256: '1'.repeat(64),
      chunkIndex: 1,
      sampleRateHz: 16000,
      sourceSampleStart: 4800,
      sourceSampleEndExclusive: 9600,
      emittedAtMs: 1600,
      inputSampleZeroClientMs: 1000,
    }));

    expect(result.current.getEvents().map((event) => (
      event.inputLedgerValid
    ))).toEqual([true, false]);

    act(() => result.current.startTest());
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      emittedAtMs: 1300,
      inputSampleZeroClientMs: 1000,
    }));
    act(() => result.current.logChunkSent(9600, {
      ...INPUT_PCM,
      inputPcmSampleCount: 14400,
      chunkIndex: 1,
      sampleRateHz: 16000,
      sourceSampleStart: 4800,
      sourceSampleEndExclusive: 9600,
      emittedAtMs: 1600,
      inputSampleZeroClientMs: 1000,
    }));
    expect(result.current.getEvents().map((event) => (
      event.inputLedgerValid
    ))).toEqual([true, false]);
  });
});
