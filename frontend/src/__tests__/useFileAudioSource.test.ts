import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useFileAudioSource } from '../hooks/useFileAudioSource';

// Mock AudioBuffer that works in jsdom
class MockAudioBuffer {
  duration: number;
  numberOfChannels = 1;
  sampleRate: number;
  length: number;
  private data: Float32Array;

  constructor(sampleRate: number, length: number) {
    this.sampleRate = sampleRate;
    this.length = length;
    this.duration = length / sampleRate;
    this.data = new Float32Array(length);
    for (let i = 0; i < length; i++) {
      this.data[i] = Math.sin(i * 0.01) * 0.5;
    }
  }

  getChannelData(_channel: number): Float32Array {
    return this.data;
  }
}

// Helper: create a File-like object with arrayBuffer()
function createMockFile(name: string): File {
  const blob = new Blob([new ArrayBuffer(100)], { type: 'audio/wav' });
  const file = new File([blob], name, { type: 'audio/wav' });
  // jsdom File may not support arrayBuffer, so polyfill
  if (!file.arrayBuffer) {
    (file as unknown as Record<string, unknown>).arrayBuffer = () =>
      Promise.resolve(new ArrayBuffer(100));
  }
  return file;
}

function setupMockOfflineAudioContext(sampleRate: number, length: number) {
  const mockBuffer = new MockAudioBuffer(sampleRate, length);
  const mockOfflineCtx = {
    decodeAudioData: vi.fn().mockResolvedValue(mockBuffer),
    createBufferSource: vi.fn().mockReturnValue({
      buffer: null,
      connect: vi.fn(),
      start: vi.fn(),
    }),
    destination: {},
    startRendering: vi.fn().mockResolvedValue(mockBuffer),
  };
  vi.stubGlobal('OfflineAudioContext', vi.fn().mockReturnValue(mockOfflineCtx));
  return mockBuffer;
}

describe('useFileAudioSource', () => {
  let onChunk: ReturnType<typeof vi.fn>;
  let onComplete: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onChunk = vi.fn();
    onComplete = vi.fn();
    vi.useFakeTimers();
    setupMockOfflineAudioContext(16000, 16000); // 1 second of audio
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('starts with unloaded state', () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    expect(result.current.isLoaded).toBe(false);
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.duration).toBe(0);
    expect(result.current.position).toBe(0);
  });

  it('loadFile sets isLoaded and duration', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');

    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    expect(result.current.isLoaded).toBe(true);
    expect(result.current.duration).toBe(1.0); // 16000 samples / 16000 Hz
  });

  it('startStreaming sends chunks of correct size (9600 bytes)', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());

    // First chunk sent immediately
    expect(onChunk).toHaveBeenCalledTimes(1);

    // Each chunk: 4800 samples * 2 bytes/sample = 9600 bytes
    const firstChunk = onChunk.mock.calls[0][0] as ArrayBuffer;
    expect(firstChunk.byteLength).toBe(9600);
  });

  it('converts Float32 to Int16 correctly', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());

    const firstChunk = onChunk.mock.calls[0][0] as ArrayBuffer;
    const int16View = new Int16Array(firstChunk);

    // All values should be valid Int16 range
    for (let i = 0; i < int16View.length; i++) {
      expect(int16View[i]).toBeGreaterThanOrEqual(-32768);
      expect(int16View[i]).toBeLessThanOrEqual(32767);
    }

    // Non-zero values expected (sine wave input)
    const hasNonZero = Array.from(int16View).some(v => v !== 0);
    expect(hasNonZero).toBe(true);
  });

  it('stopStreaming stops sending chunks', async () => {
    // Use a longer buffer so it doesn't complete immediately
    setupMockOfflineAudioContext(16000, 160000); // 10 seconds

    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());
    expect(result.current.isStreaming).toBe(true);

    const chunksBeforeStop = onChunk.mock.calls.length;

    act(() => result.current.stopStreaming());
    expect(result.current.isStreaming).toBe(false);

    // Advance time — no more chunks should be sent
    act(() => vi.advanceTimersByTime(1000));
    expect(onChunk.mock.calls.length).toBe(chunksBeforeStop);
  });

  it('calls onComplete when file finishes streaming', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());

    // 16000 samples / 4800 per chunk = 3.33 -> 4 chunks
    // First chunk sent immediately, remaining need timer advancement
    for (let i = 0; i < 5; i++) {
      act(() => vi.advanceTimersByTime(300));
    }

    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it('updates position as streaming progresses', async () => {
    // 3 seconds of audio
    setupMockOfflineAudioContext(16000, 48000);

    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    expect(result.current.position).toBe(0);

    act(() => result.current.startStreaming());

    // After first chunk: position = 0.3s (4800/16000)
    expect(result.current.position).toBeCloseTo(0.3, 1);

    // Self-correcting timer: first chunk fires at T, expected was T+300,
    // so drift = -300, next fires at T+600. Advance enough to trigger it.
    act(() => vi.advanceTimersByTime(600));
    expect(result.current.position).toBeCloseTo(0.6, 1);
  });
});
