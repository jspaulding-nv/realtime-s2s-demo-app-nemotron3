import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAudioPlayback } from '../hooks/useAudioPlayback';

// --- Mock Web Audio API ---

let mockGainNode: {
  gain: { value: number };
  connect: ReturnType<typeof vi.fn>;
};

let mockCtxCurrentTime: number;
let mockCtxState: string;

// Track created sources so tests can fire onended
let createdSources: Array<{
  buffer: { duration: number } | null;
  connect: ReturnType<typeof vi.fn>;
  start: ReturnType<typeof vi.fn>;
  onended: (() => void) | null;
}>;

function setupMockAudioContext() {
  mockCtxCurrentTime = 0;
  mockCtxState = 'running';
  createdSources = [];

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
});
