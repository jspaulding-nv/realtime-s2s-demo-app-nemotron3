import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { createHash } from 'node:crypto';
import { useFileAudioSource } from '../hooks/useFileAudioSource';
import { pcm16LeBytes, pcm16MonoWave } from './wavTestFixtures';

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

  getChannelData(): Float32Array {
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

function createByteFile(
  name: string,
  bytes: Uint8Array<ArrayBuffer>,
): File {
  const file = new File([bytes], name, { type: 'audio/wav' });
  Object.defineProperty(file, 'arrayBuffer', {
    value: vi.fn(async () => Uint8Array.from(bytes).buffer),
  });
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

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
}

describe('useFileAudioSource', () => {
  let onChunk: ReturnType<typeof vi.fn>;
  let onComplete: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onChunk = vi.fn();
    onComplete = vi.fn();
    vi.useFakeTimers();
    vi.stubGlobal('crypto', {
      subtle: {
        digest: vi.fn(async (
          _algorithm: string,
          data: BufferSource,
        ) => {
          const bytes = ArrayBuffer.isView(data)
            ? new Uint8Array(
              data.buffer,
              data.byteOffset,
              data.byteLength,
            )
            : new Uint8Array(data);
          const digest = createHash('sha256').update(bytes).digest();
          return Uint8Array.from(digest).buffer;
        }),
      },
    });
    setupMockOfflineAudioContext(16000, 16000); // 1 second of audio
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
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

    // The first frame is held until its 300 ms source-end boundary.
    expect(onChunk).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(299));
    expect(onChunk).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(onChunk).toHaveBeenCalledTimes(1);

    // Each chunk: 4800 samples * 2 bytes/sample = 9600 bytes
    const firstChunk = onChunk.mock.calls[0][0] as ArrayBuffer;
    expect(firstChunk.byteLength).toBe(9600);
    expect(onChunk.mock.calls[0][1]).toMatchObject({
      chunkIndex: 0,
      sampleRateHz: 16000,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      inputPcmSampleCount: 19200,
    });
    expect(onChunk.mock.calls[0][1].inputPcmSha256).toMatch(
      /^[0-9a-f]{64}$/,
    );
    expect(
      onChunk.mock.calls[0][1].emittedAtMs
      - onChunk.mock.calls[0][1].inputSampleZeroClientMs,
    ).toBe(300);
  });

  it('paces chunks at exact absolute source-end boundaries', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());
    expect(onChunk).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(300));
    expect(onChunk).toHaveBeenCalledTimes(1);
    const first = onChunk.mock.calls[0][1];
    expect(first.emittedAtMs - first.inputSampleZeroClientMs).toBe(300);

    act(() => vi.advanceTimersByTime(299));
    expect(onChunk).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(1));
    expect(onChunk).toHaveBeenCalledTimes(2);
    const second = onChunk.mock.calls[1][1];
    expect(second.emittedAtMs - second.inputSampleZeroClientMs).toBe(600);
  });

  it('emits a contiguous transmitted-sample ledger with a stable anchor', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );
    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(600));

    const first = onChunk.mock.calls[0][1];
    const second = onChunk.mock.calls[1][1];
    expect(second).toMatchObject({
      chunkIndex: 1,
      sampleRateHz: 16000,
      sourceSampleStart: 4800,
      sourceSampleEndExclusive: 9600,
      inputPcmSha256: first.inputPcmSha256,
      inputPcmSampleCount: 19200,
      inputSampleZeroClientMs: first.inputSampleZeroClientMs,
    });
    expect(first.emittedAtMs - first.inputSampleZeroClientMs).toBe(300);
    expect(second.emittedAtMs - first.inputSampleZeroClientMs).toBe(600);
  });

  it('hashes the exact padded PCM bytes transmitted by every chunk', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );
    await act(async () => {
      await result.current.loadFile(createMockFile('test.wav'));
    });

    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(1200));

    const chunks = onChunk.mock.calls.map(
      (call) => new Uint8Array(call[0] as ArrayBuffer),
    );
    const transmitted = new Uint8Array(
      chunks.reduce((total, chunk) => total + chunk.byteLength, 0),
    );
    let offset = 0;
    for (const chunk of chunks) {
      transmitted.set(chunk, offset);
      offset += chunk.byteLength;
    }
    const expectedDigest = await sha256Hex(transmitted.buffer);
    const observations = onChunk.mock.calls.map((call) => call[1]);

    expect(chunks).toHaveLength(4);
    expect(transmitted.byteLength).toBe(19200 * 2);
    expect(
      Array.from(new Int16Array(transmitted.buffer).slice(16000)),
    ).toEqual(Array(3200).fill(0));
    expect(
      new Set(observations.map((item) => item.inputPcmSha256)),
    ).toEqual(new Set([expectedDigest]));
    expect(
      new Set(observations.map((item) => item.inputPcmSampleCount)),
    ).toEqual(new Set([19200]));
  });

  it('passes matching PCM WAV bytes through exactly and pads only the wire tail', async () => {
    const samples = [0x1234, -2, -32768, 32767, 1];
    const sourceBytes = pcm16LeBytes(samples);
    const file = createByteFile(
      'exact-16khz-mono-pcm16.wav',
      pcm16MonoWave(samples),
    );
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    await act(async () => {
      await result.current.loadFile(file);
    });

    expect(result.current.isLoaded).toBe(true);
    expect(result.current.duration).toBe(samples.length / 16000);
    expect(OfflineAudioContext).not.toHaveBeenCalled();
    expect(file.arrayBuffer).toHaveBeenCalledTimes(1);

    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(300));

    expect(onChunk).toHaveBeenCalledTimes(1);
    const transmitted = new Uint8Array(
      onChunk.mock.calls[0][0] as ArrayBuffer,
    );
    const expected = new Uint8Array(4800 * Int16Array.BYTES_PER_ELEMENT);
    expected.set(sourceBytes);
    const expectedDigest = createHash('sha256')
      .update(expected)
      .digest('hex');

    expect(expectedDigest).toBe(
      '181ebc7fcdf1139c9430d4145f841689acadd310a196570510ec493eec2a8ac5',
    );
    expect(Array.from(transmitted.slice(0, sourceBytes.byteLength))).toEqual(
      Array.from(sourceBytes),
    );
    expect(
      transmitted.slice(sourceBytes.byteLength).every((value) => value === 0),
    ).toBe(true);
    expect(onChunk.mock.calls[0][1]).toMatchObject({
      inputPcmSampleCount: 4800,
      inputPcmSha256: expectedDigest,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
    });
  });

  it('reconstructs a multi-frame exact WAV without changing any source byte', async () => {
    const samples = Array.from(
      { length: 4803 },
      (_, index) => ((index * 97) % 65536) - 32768,
    );
    const sourceBytes = pcm16LeBytes(samples);
    const file = createByteFile(
      'multi-frame-exact.wav',
      pcm16MonoWave(samples),
    );
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    await act(async () => {
      await result.current.loadFile(file);
    });
    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(600));

    expect(onChunk).toHaveBeenCalledTimes(2);
    const transmitted = new Uint8Array(9600 * 2);
    transmitted.set(
      new Uint8Array(onChunk.mock.calls[0][0] as ArrayBuffer),
      0,
    );
    transmitted.set(
      new Uint8Array(onChunk.mock.calls[1][0] as ArrayBuffer),
      4800 * 2,
    );
    const expectedDigest = createHash('sha256')
      .update(transmitted)
      .digest('hex');

    expect(Array.from(transmitted.slice(0, sourceBytes.length))).toEqual(
      Array.from(sourceBytes),
    );
    expect(
      transmitted.slice(sourceBytes.length).every((value) => value === 0),
    ).toBe(true);
    expect(
      new Set(onChunk.mock.calls.map((call) => call[1].inputPcmSha256)),
    ).toEqual(new Set([expectedDigest]));
    expect(
      onChunk.mock.calls.map((call) => call[1].sourceSampleStart),
    ).toEqual([0, 4800]);
    expect(
      onChunk.mock.calls.map((call) => call[1].sourceSampleEndExclusive),
    ).toEqual([4800, 9600]);
  });

  it('falls back to WebAudio for a malformed WAV instead of passing bytes through', async () => {
    const malformed = createByteFile(
      'malformed.wav',
      Uint8Array.of(
        0x52, 0x49, 0x46, 0x46,
        0xff, 0xff, 0xff, 0x7f,
        0x57, 0x41, 0x56, 0x45,
      ),
    );
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    await act(async () => {
      await result.current.loadFile(malformed);
    });

    expect(result.current.isLoaded).toBe(true);
    expect(OfflineAudioContext).toHaveBeenCalledTimes(2);
    expect(malformed.arrayBuffer).toHaveBeenCalledTimes(2);
  });

  it('hashes the exact transmitted PCM without Web Crypto on plain HTTP', async () => {
    vi.stubGlobal('crypto', {});
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );
    await act(async () => {
      await result.current.loadFile(createMockFile('plain-http.wav'));
    });

    expect(result.current.isLoaded).toBe(true);
    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(1200));

    const transmitted = new Uint8Array(19200 * Int16Array.BYTES_PER_ELEMENT);
    let offset = 0;
    for (const [chunk] of onChunk.mock.calls) {
      const chunkBytes = new Uint8Array(chunk as ArrayBuffer);
      transmitted.set(chunkBytes, offset);
      offset += chunkBytes.byteLength;
    }
    const expectedDigest = createHash('sha256')
      .update(transmitted)
      .digest('hex');

    expect(onChunk).toHaveBeenCalledTimes(4);
    expect(offset).toBe(transmitted.byteLength);
    expect(
      new Set(onChunk.mock.calls.map((call) => call[1].inputPcmSha256)),
    ).toEqual(new Set([expectedDigest]));
  });

  it('changes the digest when the exact input PCM changes', async () => {
    const firstBuffer = setupMockOfflineAudioContext(16000, 16000);
    firstBuffer.getChannelData()[0] = 0.25;
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );
    await act(async () => {
      await result.current.loadFile(createMockFile('a.wav'));
    });
    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(300));
    const firstDigest = onChunk.mock.calls[0][1].inputPcmSha256;

    act(() => result.current.stopStreaming());
    onChunk.mockClear();
    const secondBuffer = setupMockOfflineAudioContext(16000, 16000);
    secondBuffer.getChannelData()[0] = -0.25;
    await act(async () => {
      await result.current.loadFile(createMockFile('b.wav'));
    });
    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(300));

    expect(onChunk.mock.calls[0][1].inputPcmSha256).not.toBe(firstDigest);
  });

  it('invalidates a prior PCM image before a replacement load fails', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );
    await act(async () => {
      await result.current.loadFile(createMockFile('good.wav'));
    });
    expect(result.current.isLoaded).toBe(true);

    const failedFile = createMockFile('failed.wav');
    Object.defineProperty(failedFile, 'arrayBuffer', {
      value: vi.fn().mockRejectedValue(new Error('decode input unavailable')),
    });
    let failure: unknown;
    await act(async () => {
      try {
        await result.current.loadFile(failedFile);
      } catch (error) {
        failure = error;
      }
    });

    expect(failure).toEqual(new Error('decode input unavailable'));
    expect(result.current.isLoaded).toBe(false);
    act(() => result.current.startStreaming());
    act(() => vi.advanceTimersByTime(1200));
    expect(onChunk).not.toHaveBeenCalled();
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
    act(() => vi.advanceTimersByTime(300));

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
    expect(onChunk).not.toHaveBeenCalled();

    const chunksBeforeStop = onChunk.mock.calls.length;

    act(() => result.current.stopStreaming());
    expect(result.current.isStreaming).toBe(false);

    // Advance time — no more chunks should be sent
    act(() => vi.advanceTimersByTime(1000));
    expect(onChunk.mock.calls.length).toBe(chunksBeforeStop);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('completes immediately after transmitting the final padded chunk', async () => {
    const { result } = renderHook(() =>
      useFileAudioSource({ onChunk, onComplete }),
    );

    const mockFile = createMockFile('test.wav');
    await act(async () => {
      await result.current.loadFile(mockFile);
    });

    act(() => result.current.startStreaming());

    // 16000 samples / 4800 per chunk = 3.33 -> 4 chunks
    // The padded final frame ends at sample 19200, or 1200 ms.
    act(() => vi.advanceTimersByTime(1199));
    expect(onChunk).toHaveBeenCalledTimes(3);
    expect(onComplete).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(onChunk).toHaveBeenCalledTimes(4);
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(result.current.isStreaming).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
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
    expect(result.current.position).toBe(0);

    // After the first source-end boundary: position = 0.3s.
    act(() => vi.advanceTimersByTime(300));
    expect(result.current.position).toBeCloseTo(0.3, 1);

    act(() => vi.advanceTimersByTime(300));
    expect(result.current.position).toBeCloseTo(0.6, 1);
  });
});
