import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAudioPlayback } from '../hooks/useAudioPlayback';
import type { AudioFrameObservation } from '../types/audioMetadata';

// --- Mock Web Audio API ---

let mockGainNode: {
  gain: { value: number };
  connect: ReturnType<typeof vi.fn>;
};

let mockCtxCurrentTime: number;
let mockCtxState: string;
let mockGetOutputTimestamp: ReturnType<typeof vi.fn>;

// Track created sources so tests can fire onended
let createdSources: Array<{
  buffer: { duration: number } | null;
  connect: ReturnType<typeof vi.fn>;
  start: ReturnType<typeof vi.fn>;
  onended: (() => void) | null;
  playbackRate: { value: number };
}>;

function setupMockAudioContext() {
  mockCtxCurrentTime = 0;
  mockCtxState = 'running';
  createdSources = [];
  mockGetOutputTimestamp = vi.fn(() => ({
    contextTime: 0,
    performanceTime: 0,
  }));

  mockGainNode = {
    gain: { value: 1 },
    connect: vi.fn(),
  };

  const mockCtx = {
    get currentTime() {
      return mockCtxCurrentTime;
    },
    get state() {
      return mockCtxState;
    },
    resume: vi.fn(),
    close: vi.fn(),
    getOutputTimestamp: mockGetOutputTimestamp,
    destination: { type: 'destination' },
    sampleRate: 16000,
    createGain: vi.fn(() => mockGainNode),
    createBuffer: vi.fn((_channels: number, length: number, sampleRate: number) => ({
      duration: length / sampleRate,
      numberOfChannels: 1,
      length,
      sampleRate,
      getChannelData: vi.fn(() => new Float32Array(length)),
    })),
    createBufferSource: vi.fn(() => {
      const source = {
        buffer: null as { duration: number } | null,
        connect: vi.fn(),
        start: vi.fn(),
        onended: null as (() => void) | null,
        playbackRate: { value: 1 },
      };
      createdSources.push(source);
      return source;
    }),
  };

  vi.stubGlobal('AudioContext', vi.fn(() => mockCtx));
  return mockCtx;
}

describe('useAudioPlayback', () => {
  beforeEach(() => {
    setupMockAudioContext();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // --- Default state ---

  it('starts with isPlaying=false, isMuted=false, playbackPosition=0', () => {
    const { result } = renderHook(() => useAudioPlayback());

    expect(result.current.isPlaying).toBe(false);
    expect(result.current.isMuted).toBe(false);
    expect(result.current.getPlaybackPosition()).toBe(0);
  });

  it('respects initialMuted option', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ initialMuted: true }),
    );

    expect(result.current.isMuted).toBe(true);
  });

  // --- start/stop lifecycle ---

  it('start() sets isPlaying=true and creates GainNode', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    expect(result.current.isPlaying).toBe(true);
    expect(mockGainNode.connect).toHaveBeenCalled();
  });

  it('start() with initialMuted=true sets gain to 0', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ initialMuted: true }),
    );

    act(() => result.current.start());

    expect(mockGainNode.gain.value).toBe(0);
  });

  it('start() with initialMuted=false sets gain to 1', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ initialMuted: false }),
    );

    act(() => result.current.start());

    expect(mockGainNode.gain.value).toBe(1);
  });

  it('stop() sets isPlaying=false and closes AudioContext', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());
    expect(result.current.isPlaying).toBe(true);

    act(() => result.current.stop());
    expect(result.current.isPlaying).toBe(false);
  });

  it('samples the paired browser clocks through queue drain', () => {
    vi.useFakeTimers();
    const onClockSample = vi.fn();
    const { result } = renderHook(() => useAudioPlayback({
      onClockSample,
    }));

    act(() => result.current.start());
    act(() => (
      result.current.queueAudio(new Int16Array(16000).buffer)
    ));
    act(() => vi.advanceTimersByTime(400));
    mockCtxCurrentTime = 0.75;
    act(() => vi.advanceTimersByTime(200));
    mockCtxCurrentTime = 1;
    act(() => vi.advanceTimersByTime(200));

    const beforeIdleWait = onClockSample.mock.calls.length;
    act(() => vi.advanceTimersByTime(400));
    expect(onClockSample).toHaveBeenCalledTimes(beforeIdleWait);

    act(() => result.current.stop());
    const afterStop = onClockSample.mock.calls.length;
    act(() => vi.advanceTimersByTime(400));
    expect(onClockSample).toHaveBeenCalledTimes(afterStop);
    const samples = onClockSample.mock.calls.map(([sample]) => sample);
    expect(samples.map((sample) => sample.clockSampleReason)).toEqual([
      'session_started',
      'queue_started',
      'interval',
      'interval',
      'interval',
      'queue_drained',
      'session_stopped',
    ]);
    expect(samples.map((sample) => sample.clockSampleSequence)).toEqual(
      [0, 1, 2, 3, 4, 5, 6],
    );
    expect(samples.every((sample) => (
      sample.playbackClockSessionId === 1
    ))).toBe(true);
    expect(samples[1].clockSampleQueueEndContextSeconds).toBe(1);
    expect(samples[5].clockSampleContextSeconds).toBe(1);
    expect(samples[5].clockSampleQueueEndContextSeconds).toBe(1);
    for (let index = 1; index < samples.length; index += 1) {
      expect(
        samples[index].clockSamplePerformanceClientMs,
      ).toBeGreaterThanOrEqual(
        samples[index - 1].clockSamplePerformanceClientMs,
      );
    }
  });

  it('uses initialized output timestamps and brackets zero placeholders', () => {
    vi.useFakeTimers();
    const onClockSample = vi.fn();
    const onSchedule = vi.fn();
    const { result } = renderHook(() => useAudioPlayback({
      onClockSample,
      onSchedule,
    }));

    act(() => vi.advanceTimersByTime(1250));
    act(() => result.current.start());
    expect(onClockSample.mock.calls[0][0]).toMatchObject({
      clockSampleBasis: 'current_time_bracket',
      clockSampleContextSeconds: 0,
    });
    expect(
      onClockSample.mock.calls[0][0].clockSampleOutputContextSeconds,
    ).toBeUndefined();

    mockCtxCurrentTime = 0.5;
    mockGetOutputTimestamp.mockReturnValue({
      contextTime: 0.48,
      performanceTime: 1234.1,
    });
    act(() => (
      result.current.queueAudio(new Int16Array(1600).buffer)
    ));

    const queueStarted = onClockSample.mock.calls
      .map(([sample]) => sample)
      .find((sample) => sample.clockSampleReason === 'queue_started');
    expect(queueStarted).toMatchObject({
      playbackClockSessionId: 1,
      clockSampleBasis: 'get_output_timestamp',
      clockSampleContextSeconds: 0.5,
      clockSampleOutputContextSeconds: 0.48,
      clockSampleOutputPerformanceClientMs: 1234.1,
      clockSampleQueueEndContextSeconds: 0.6,
    });
    expect(
      queueStarted.clockSamplePerformanceBeforeClientMs,
    ).toBeLessThanOrEqual(
      queueStarted.clockSamplePerformanceClientMs,
    );
    expect(
      queueStarted.clockSamplePerformanceClientMs,
    ).toBeLessThanOrEqual(
      queueStarted.clockSamplePerformanceAfterClientMs,
    );
    expect(onSchedule.mock.calls[0][0].playbackClockSessionId).toBe(
      queueStarted.playbackClockSessionId,
    );
  });

  it.each([
    {
      label: 'AudioContext clock is stale',
      outputContextSeconds: 0.49,
      outputPerformanceClientMs: 990,
    },
    {
      label: 'performance clock is stale',
      outputContextSeconds: 0.99,
      outputPerformanceClientMs: 489,
    },
  ])(
    'falls back when a frozen output timestamp $label',
    ({
      outputContextSeconds,
      outputPerformanceClientMs,
    }) => {
      vi.useFakeTimers();
      act(() => vi.advanceTimersByTime(1000));
      const onClockSample = vi.fn();
      const { result } = renderHook(() => useAudioPlayback({
        onClockSample,
      }));

      act(() => result.current.start());
      mockCtxCurrentTime = 1;
      mockGetOutputTimestamp.mockReturnValue({
        contextTime: outputContextSeconds,
        performanceTime: outputPerformanceClientMs,
      });
      act(() => (
        result.current.queueAudio(new Int16Array(1600).buffer)
      ));

      const queueStarted = onClockSample.mock.calls
        .map(([sample]) => sample)
        .find((sample) => sample.clockSampleReason === 'queue_started');
      expect(queueStarted).toMatchObject({
        clockSampleBasis: 'current_time_bracket',
        clockSampleContextSeconds: 1,
      });
      expect(
        queueStarted.clockSampleOutputContextSeconds,
      ).toBeUndefined();
      expect(
        queueStarted.clockSampleOutputPerformanceClientMs,
      ).toBeUndefined();
    },
  );

  it.each([
    {
      label: 'AudioContext clock is in the future',
      outputContextSeconds: 1.001,
      outputPerformanceClientMs: 990,
    },
    {
      label: 'performance clock is in the future',
      outputContextSeconds: 0.99,
      outputPerformanceClientMs: 1000.1,
    },
  ])(
    'falls back when an output timestamp $label',
    ({
      outputContextSeconds,
      outputPerformanceClientMs,
    }) => {
      vi.useFakeTimers();
      act(() => vi.advanceTimersByTime(1000));
      const onClockSample = vi.fn();
      const { result } = renderHook(() => useAudioPlayback({
        onClockSample,
      }));

      act(() => result.current.start());
      mockCtxCurrentTime = 1;
      mockGetOutputTimestamp.mockReturnValue({
        contextTime: outputContextSeconds,
        performanceTime: outputPerformanceClientMs,
      });
      act(() => (
        result.current.queueAudio(new Int16Array(1600).buffer)
      ));

      const queueStarted = onClockSample.mock.calls
        .map(([sample]) => sample)
        .find((sample) => sample.clockSampleReason === 'queue_started');
      expect(queueStarted.clockSampleBasis).toBe('current_time_bracket');
      expect(
        queueStarted.clockSampleOutputContextSeconds,
      ).toBeUndefined();
      expect(
        queueStarted.clockSampleOutputPerformanceClientMs,
      ).toBeUndefined();
    },
  );

  it('closes a drained queue before a new buffer replaces its endpoint', () => {
    vi.useFakeTimers();
    const onClockSample = vi.fn();
    const { result } = renderHook(() => useAudioPlayback({
      onClockSample,
    }));

    act(() => result.current.start());
    act(() => (
      result.current.queueAudio(new Int16Array(1600).buffer)
    ));
    mockCtxCurrentTime = 0.11;
    // Do not advance the 200 ms timer: the arriving buffer must detect and
    // close the old queue interval itself.
    act(() => (
      result.current.queueAudio(new Int16Array(1600).buffer)
    ));

    const transitions = onClockSample.mock.calls
      .map(([sample]) => sample)
      .filter((sample) => (
        sample.clockSampleReason === 'queue_started'
        || sample.clockSampleReason === 'queue_drained'
      ));
    expect(transitions.map((sample) => sample.clockSampleReason)).toEqual([
      'queue_started',
      'queue_drained',
      'queue_started',
    ]);
    expect(
      transitions[0].clockSampleQueueEndContextSeconds,
    ).toBeCloseTo(0.1);
    expect(
      transitions[1].clockSampleQueueEndContextSeconds,
    ).toBeCloseTo(0.1);
    expect(
      transitions[2].clockSampleQueueEndContextSeconds,
    ).toBeCloseTo(0.21);
    expect(transitions[1].clockSampleContextSeconds).toBe(0.11);
    expect(transitions[2].clockSampleContextSeconds).toBe(0.11);
  });

  it('records a proven drain when stop wins the interval-timer race', () => {
    vi.useFakeTimers();
    const onClockSample = vi.fn();
    const { result } = renderHook(() => useAudioPlayback({
      onClockSample,
    }));

    act(() => result.current.start());
    act(() => (
      result.current.queueAudio(new Int16Array(1600).buffer)
    ));
    mockCtxCurrentTime = 0.1;
    act(() => result.current.stop());

    expect(onClockSample.mock.calls.map(([sample]) => (
      sample.clockSampleReason
    ))).toEqual([
      'session_started',
      'queue_started',
      'queue_drained',
      'session_stopped',
    ]);
  });

  it('separates clock evidence across playback restarts', () => {
    vi.useFakeTimers();
    const onClockSample = vi.fn();
    const onSchedule = vi.fn();
    const { result } = renderHook(() => useAudioPlayback({
      onClockSample,
      onSchedule,
    }));

    act(() => result.current.start());
    act(() => (
      result.current.queueAudio(new Int16Array(1600).buffer)
    ));
    act(() => result.current.stop());
    mockCtxCurrentTime = 0.25;
    act(() => result.current.start());
    act(() => (
      result.current.queueAudio(new Int16Array(1600).buffer)
    ));

    expect(onSchedule.mock.calls.map(([event]) => (
      event.playbackClockSessionId
    ))).toEqual([1, 3]);
    const sessionStarts = onClockSample.mock.calls
      .map(([sample]) => sample)
      .filter((sample) => sample.clockSampleReason === 'session_started');
    expect(sessionStarts).toMatchObject([
      {
        playbackClockSessionId: 1,
        clockSampleSequence: 0,
      },
      {
        playbackClockSessionId: 3,
        clockSampleSequence: 0,
      },
    ]);
  });

  it('start() resets playbackPosition to 0', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    // Queue some audio and fire onended to advance position
    const pcm = new Int16Array(1600).buffer; // 0.1s at 16kHz
    act(() => result.current.queueAudio(pcm));
    act(() => createdSources[0].onended?.());
    expect(result.current.getPlaybackPosition()).toBeGreaterThan(0);

    // Restart should reset
    act(() => result.current.stop());
    act(() => result.current.start());
    expect(result.current.getPlaybackPosition()).toBe(0);
  });

  // --- setMuted ---

  it('setMuted(true) sets isMuted=true and gain=0', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());
    act(() => result.current.setMuted(true));

    expect(result.current.isMuted).toBe(true);
    expect(mockGainNode.gain.value).toBe(0);
  });

  it('setMuted(false) sets isMuted=false and gain=1', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ initialMuted: true }),
    );

    act(() => result.current.start());
    expect(mockGainNode.gain.value).toBe(0);

    act(() => result.current.setMuted(false));
    expect(result.current.isMuted).toBe(false);
    expect(mockGainNode.gain.value).toBe(1);
  });

  it('preserves the selected mute state across playback sessions', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());
    act(() => result.current.setMuted(true));
    act(() => result.current.stop());
    act(() => result.current.start());

    expect(result.current.isMuted).toBe(true);
    expect(mockGainNode.gain.value).toBe(0);
  });

  // --- queueAudio ---

  it('queueAudio ignores data when not active', () => {
    const { result } = renderHook(() => useAudioPlayback());

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    expect(createdSources.length).toBe(0);
  });

  it('queueAudio creates BufferSource connected to GainNode', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    expect(createdSources.length).toBe(1);
    expect(createdSources[0].connect).toHaveBeenCalledWith(mockGainNode);
    expect(createdSources[0].start).toHaveBeenCalled();
  });

  it('queueAudio sets onended handler on the source', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    expect(createdSources[0].onended).toBeTypeOf('function');
  });

  it('forwards observed frame identity without changing audio scheduling', () => {
    const onSchedule = vi.fn();
    const observation: AudioFrameObservation = {
      metadata: {
        type: 'audio_frame',
        protocolVersion: 1,
        streamGeneration: 7,
        parentSequenceId: 3,
        audioFrameId: 2,
        audioBytes: 3200,
        sampleRateHz: 16000,
        channels: 1,
        bytesPerSample: 2,
        sourceStartMs: null,
        sourceEndMs: 1234,
      },
      binaryReceivedAtMs: 42,
    };
    const { result } = renderHook(() => useAudioPlayback({ onSchedule }));
    act(() => result.current.start());
    vi.spyOn(performance, 'now').mockReturnValue(250);

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm, observation));

    expect(createdSources[0].start).toHaveBeenCalledWith(0);
    expect(createdSources[0].playbackRate.value).toBe(1);
    expect(onSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        audioBytes: pcm.byteLength,
        sourceDurationSeconds: 0.1,
        playbackRate: 1,
        audioFrame: observation,
        timestampMs: 250,
        schedulePerformanceMs: 250,
        audioContextTimeAtScheduleSeconds: 0,
        scheduledStartContextSeconds: 0,
        scheduledEndContextSeconds: 0.1,
        projectedScheduledStartClientMs: 250,
      }),
    );
  });

  // --- Client-clock playback projection ---

  it('keeps contiguous projections despite AudioContext/performance clock jitter', () => {
    const onSchedule = vi.fn();
    let nowMs = 1000.25;
    vi.spyOn(performance, 'now').mockImplementation(() => nowMs);
    const { result } = renderHook(() => useAudioPlayback({ onSchedule }));
    act(() => result.current.start());

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    // Simulate independently quantized clocks before the next contiguous
    // AudioContext buffer is scheduled.
    mockCtxCurrentTime = 0.001;
    nowMs = 1001.125;
    act(() => result.current.queueAudio(pcm));

    const first = onSchedule.mock.calls[0][0];
    const second = onSchedule.mock.calls[1][0];
    expect(createdSources[0].start).toHaveBeenCalledWith(0);
    expect(createdSources[1].start).toHaveBeenCalledWith(0.1);
    expect(second.projectedScheduledStartClientMs).toBe(
      first.projectedScheduledStartClientMs
        + first.scheduledDurationSeconds * 1000,
    );
  });

  it('reanchors the projected start to performance time after a true underrun', () => {
    const onSchedule = vi.fn();
    let nowMs = 2000;
    vi.spyOn(performance, 'now').mockImplementation(() => nowMs);
    const { result } = renderHook(() => useAudioPlayback({ onSchedule }));
    act(() => result.current.start());

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    mockCtxCurrentTime = 0.25;
    nowMs = 2250;
    act(() => result.current.queueAudio(pcm));

    expect(createdSources[1].start).toHaveBeenCalledWith(0.25);
    expect(
      onSchedule.mock.calls[1][0].projectedScheduledStartClientMs,
    ).toBe(2250);
  });

  it('does not carry a projected end across a stop/start restart', () => {
    const onSchedule = vi.fn();
    let nowMs = 1000;
    vi.spyOn(performance, 'now').mockImplementation(() => nowMs);
    const { result } = renderHook(() => useAudioPlayback({ onSchedule }));
    act(() => result.current.start());
    act(() => result.current.queueAudio(new Int16Array(16000).buffer));

    act(() => result.current.stop());
    mockCtxCurrentTime = 0.2;
    nowMs = 1100;
    act(() => result.current.start());
    act(() => result.current.queueAudio(new Int16Array(1600).buffer));

    expect(
      onSchedule.mock.calls[1][0].projectedScheduledStartClientMs,
    ).toBe(1100);
  });

  it('projects exact contiguous spacing at adaptive 1.05x playback', () => {
    const onSchedule = vi.fn();
    let nowMs = 500;
    vi.spyOn(performance, 'now').mockImplementation(() => nowMs);
    const { result } = renderHook(() =>
      useAudioPlayback({ adaptivePlayback: true, onSchedule }),
    );
    act(() => result.current.start());

    act(() => result.current.queueAudio(new Int16Array(80000).buffer));
    mockCtxCurrentTime = 0.001;
    nowMs = 501.25;
    act(() => result.current.queueAudio(new Int16Array(1600).buffer));

    const first = onSchedule.mock.calls[0][0];
    const second = onSchedule.mock.calls[1][0];
    expect(first.playbackRate).toBe(1.05);
    expect(second.playbackRate).toBe(1.05);
    expect(second.projectedScheduledStartClientMs).toBe(
      first.projectedScheduledStartClientMs + (5 / 1.05) * 1000,
    );
  });

  // --- Playback position tracking ---

  it('getPlaybackPosition returns 0 before any audio finishes', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    const pcm = new Int16Array(1600).buffer; // 0.1s at 16kHz
    act(() => result.current.queueAudio(pcm));

    // Audio queued but not yet finished
    expect(result.current.getPlaybackPosition()).toBe(0);
  });

  it('getPlaybackPosition increments when onended fires', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    // Queue 0.1s of audio (1600 samples at 16kHz)
    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    // Fire onended
    act(() => createdSources[0].onended?.());

    expect(result.current.getPlaybackPosition()).toBeCloseTo(0.1, 2);
  });

  it('getPlaybackPosition accumulates across multiple buffers', () => {
    const { result } = renderHook(() => useAudioPlayback());

    act(() => result.current.start());

    // Queue three 0.1s buffers
    for (let i = 0; i < 3; i++) {
      const pcm = new Int16Array(1600).buffer;
      act(() => result.current.queueAudio(pcm));
    }

    // Fire onended for first two
    act(() => createdSources[0].onended?.());
    act(() => createdSources[1].onended?.());

    expect(result.current.getPlaybackPosition()).toBeCloseTo(0.2, 2);

    // Fire third
    act(() => createdSources[2].onended?.());
    expect(result.current.getPlaybackPosition()).toBeCloseTo(0.3, 2);
  });

  it('playback position works regardless of muted state', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ initialMuted: true }),
    );

    act(() => result.current.start());

    const pcm = new Int16Array(1600).buffer;
    act(() => result.current.queueAudio(pcm));

    // Even though muted, onended still fires in Web Audio
    act(() => createdSources[0].onended?.());

    expect(result.current.getPlaybackPosition()).toBeCloseTo(0.1, 2);
  });

  // --- Adaptive bounded-target playback ---

  it('keeps 1.00x playback when adaptation is disabled', () => {
    const { result } = renderHook(() => useAudioPlayback());
    act(() => result.current.start());

    act(() => result.current.queueAudio(new Int16Array(160000).buffer));

    expect(createdSources[0].playbackRate.value).toBe(1);
    expect(result.current.getPlaybackMetrics().playbackMode).toBe('normal');
  });

  it('selects 1.05x when the projected queue reaches five seconds', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ adaptivePlayback: true }),
    );
    act(() => result.current.start());

    act(() => result.current.queueAudio(new Int16Array(80000).buffer));

    expect(createdSources[0].playbackRate.value).toBe(1.05);
    expect(result.current.getPlaybackMetrics().playbackMode).toBe('catch-up');
    expect(result.current.getPlaybackMetrics().queueDepthSeconds).toBeCloseTo(
      5 / 1.05,
      5,
    );
  });

  it('selects 1.10x when the projected queue reaches eight seconds', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ adaptivePlayback: true }),
    );
    act(() => result.current.start());

    act(() => result.current.queueAudio(new Int16Array(128000).buffer));

    expect(createdSources[0].playbackRate.value).toBe(1.1);
    expect(result.current.getPlaybackMetrics().playbackMode).toBe('urgent');
  });

  it('advances the schedule by media duration divided by playback rate', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ adaptivePlayback: true }),
    );
    act(() => result.current.start());

    const fiveSeconds = new Int16Array(80000).buffer;
    act(() => result.current.queueAudio(fiveSeconds));
    act(() => result.current.queueAudio(new Int16Array(1600).buffer));

    expect(createdSources[1].start).toHaveBeenCalledWith(5 / 1.05);
  });

  it('reports queue decay as AudioContext time advances', () => {
    const { result } = renderHook(() => useAudioPlayback());
    act(() => result.current.start());
    act(() => result.current.queueAudio(new Int16Array(16000).buffer));

    expect(result.current.getPlaybackMetrics().queueDepthSeconds).toBeCloseTo(1);
    mockCtxCurrentTime = 0.4;
    expect(result.current.getPlaybackMetrics().queueDepthSeconds).toBeCloseTo(0.6);
  });

  it('preserves all buffers and edge-counts an over-limit breach', () => {
    const onSchedule = vi.fn();
    const { result } = renderHook(() =>
      useAudioPlayback({ adaptivePlayback: true, onSchedule }),
    );
    act(() => result.current.start());

    act(() => result.current.queueAudio(new Int16Array(192000).buffer));
    act(() => result.current.queueAudio(new Int16Array(1600).buffer));

    expect(createdSources).toHaveLength(2);
    expect(createdSources[0].playbackRate.value).toBe(1.1);
    expect(createdSources[1].playbackRate.value).toBe(1.1);
    expect(result.current.getPlaybackMetrics().limitExceededCount).toBe(1);
    expect(onSchedule).toHaveBeenCalledTimes(2);
    expect(onSchedule.mock.calls[0][0].aboveLimit).toBe(true);
  });

  it('resets adaptive metrics when a new playback session starts', () => {
    const { result } = renderHook(() =>
      useAudioPlayback({ adaptivePlayback: true }),
    );
    act(() => result.current.start());
    act(() => result.current.queueAudio(new Int16Array(192000).buffer));
    expect(result.current.getPlaybackMetrics().limitExceededCount).toBe(1);

    act(() => result.current.stop());
    act(() => result.current.start());

    expect(result.current.getPlaybackMetrics()).toMatchObject({
      queueDepthSeconds: 0,
      peakQueueDepthSeconds: 0,
      playbackRate: 1,
      playbackMode: 'normal',
      limitExceededCount: 0,
    });
  });
});
