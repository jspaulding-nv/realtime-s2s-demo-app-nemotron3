import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useRenderedDigitalCapture } from '../hooks/useRenderedDigitalCapture';

class MockPort {
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  close = vi.fn();
  postMessage = vi.fn((message: {
    type?: string;
    sourceStartContextFrame?: number;
  }) => {
    if (message.type === 'arm_source_clock') {
      queueMicrotask(() => {
        this.emit({ type: 'source_clock_armed' });
        this.emit({
          type: 'started',
          captureStartContextFrame: armCaptureStartContextFrame,
        });
      });
    } else if (message.type === 'stop') {
      queueMicrotask(() => emitStopFixture(this));
    }
  });

  emit(data: unknown) {
    this.onmessage?.({ data } as MessageEvent<unknown>);
  }
}

let stopBlocks: Array<{
  sequence: number;
  startContextFrame: number;
  frameCount: number;
  interleavedPcm16: Int16Array;
}>;
let armCaptureStartContextFrame: number;

function emitStopFixture(port: MockPort) {
  for (const block of stopBlocks) {
    port.emit({ type: 'pcm_block', ...block });
  }
  port.emit({
    type: 'stopped',
    captureStartContextFrame: 64,
    captureEndContextFrameExclusive: 68,
    blockCount: stopBlocks.length,
  });
}

class MockRecorderNode {
  port = new MockPort();
  connect = vi.fn(() => {
    queueMicrotask(() => this.port.emit({
      type: 'ready',
      readyContextFrame: 0,
    }));
  });
  disconnect = vi.fn();
  onprocessorerror: (() => void) | null = null;
}

class MockContext extends EventTarget {
  sampleRate: number;
  state: AudioContextState = 'running';
  currentTime = 0.01;
  destination = {};
  audioWorklet = { addModule: vi.fn(async () => {}) };
  close = vi.fn(async () => {
    this.state = 'closed';
  });
  resume = vi.fn(async () => {
    this.state = 'running';
  });
  gain = {
    gain: { value: 1 },
    connect: vi.fn(),
    disconnect: vi.fn(),
  };
  createGain = vi.fn(() => this.gain);

  constructor(sampleRate = 16000) {
    super();
    this.sampleRate = sampleRate;
  }
}

describe('useRenderedDigitalCapture', () => {
  let context: MockContext;
  let recorder: MockRecorderNode;

  beforeEach(() => {
    armCaptureStartContextFrame = 64;
    stopBlocks = [{
      sequence: 0,
      startContextFrame: 64,
      frameCount: 4,
      interleavedPcm16: Int16Array.of(
        1, 10,
        2, 20,
        3, 30,
        4, 40,
      ),
    }];
    context = new MockContext();
    recorder = new MockRecorderNode();
    vi.stubGlobal('isSecureContext', true);
    vi.stubGlobal('AudioContext', vi.fn(() => context));
    vi.stubGlobal('AudioWorkletNode', vi.fn(() => recorder));
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      arrayBuffer: async () => (
        new TextEncoder().encode('registerProcessor("test", class {})')
          .buffer
      ),
    })));
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:rendered-worklet'),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('owns one 16 kHz context, routes both inputs, and flushes exact stereo PCM', async () => {
    const { result } = renderHook(() => useRenderedDigitalCapture());

    await act(async () => {
      await result.current.start();
    });
    const sourceRouting = result.current.createPlaybackRouting(0);
    const translatedRouting = result.current.createPlaybackRouting(1);
    expect(sourceRouting.audioContext).toBe(context);
    expect(translatedRouting.audioContext).toBe(context);
    expect(sourceRouting.captureNode).toBe(recorder);
    expect(translatedRouting.captureNode).toBe(recorder);

    let sourceClock!: Awaited<
      ReturnType<typeof result.current.armSourceClock>
    >;
    await act(async () => {
      sourceClock = await result.current.armSourceClock({
        sourceStartContextFrame: 8000,
        sourceFrameCount: 960000,
        sourceChunkFrames: 4800,
      });
    });
    expect(sourceClock.sourceStartContextFrame).toBe(8000);
    const tickListener = vi.fn();
    sourceClock.subscribe(tickListener);
    context.currentTime = 0.805;
    recorder.port.emit({
      type: 'source_chunk_due',
      chunkIndex: 0,
      sourceSampleStart: 0,
      sourceSampleEndExclusive: 4800,
      boundaryContextFrame: 12800,
      deliveredAfterContextFrame: 12864,
    });
    expect(tickListener).toHaveBeenCalledWith(expect.objectContaining({
      chunkIndex: 0,
      boundaryContextFrame: 12800,
      receivedContextFrameBefore: 12880,
      receivedContextFrameAfter: 12880,
      receivedAtClientMs: expect.any(Number),
    }));
    expect(sourceClock.getCurrentContextFrame()).toBe(12880);

    let capture!: Awaited<ReturnType<typeof result.current.stop>>;
    await act(async () => {
      capture = await result.current.stop();
    });
    expect(Array.from(capture.sourcePcm16)).toEqual([1, 2, 3, 4]);
    expect(Array.from(capture.translatedPcm16)).toEqual([10, 20, 30, 40]);
    expect(capture.frameCount).toBe(4);
    expect(capture.blocks).toHaveLength(1);
    expect(capture.wavBytes.byteLength).toBe(60);
    expect(capture.workletModuleSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(context.audioWorklet.addModule).toHaveBeenCalledWith(
      'blob:rendered-worklet',
    );
    expect(AudioWorkletNode).toHaveBeenCalledWith(
      context,
      'rendered-digital-recorder',
      expect.objectContaining({
        processorOptions: {
          chunkFrames: 8000,
          maximumFrames: 16000 * 420,
        },
      }),
    );
    expect(context.close).toHaveBeenCalledTimes(1);
    expect(recorder.port.close).toHaveBeenCalledTimes(1);
  });

  it('fails closed when the browser ignores the required sample rate', async () => {
    context = new MockContext(48000);
    vi.stubGlobal('AudioContext', vi.fn(() => context));
    const { result } = renderHook(() => useRenderedDigitalCapture());

    await expect(act(async () => {
      await result.current.start();
    })).rejects.toThrow(/required 16 kHz/);
    expect(context.close).toHaveBeenCalledTimes(1);
  });

  it('rejects a capture epoch that begins after scheduled source playback', async () => {
    armCaptureStartContextFrame = 8001;
    const { result } = renderHook(() => useRenderedDigitalCapture());

    await act(async () => {
      await result.current.start();
    });
    await expect(act(async () => {
      await result.current.armSourceClock({
        sourceStartContextFrame: 8000,
        sourceFrameCount: 960000,
        sourceChunkFrames: 4800,
      });
    })).rejects.toThrow(/began after source playback/);
  });

  it('rejects a recorder capture start before the source clock is armed', async () => {
    const { result } = renderHook(() => useRenderedDigitalCapture());

    await act(async () => {
      await result.current.start();
    });
    act(() => {
      recorder.port.emit({
        type: 'started',
        captureStartContextFrame: 64,
      });
    });

    expect(() => result.current.createPlaybackRouting(0)).toThrow(
      /not ready for playback/,
    );
  });

  it('propagates a post-arm render-quantum discontinuity', async () => {
    const { result } = renderHook(() => useRenderedDigitalCapture());

    await act(async () => {
      await result.current.start();
      await result.current.armSourceClock({
        sourceStartContextFrame: 8000,
        sourceFrameCount: 960000,
        sourceChunkFrames: 4800,
      });
    });
    act(() => {
      recorder.port.emit({
        type: 'capture_error',
        code: 'noncontiguous_render_quantum',
      });
    });

    await expect(act(async () => {
      await result.current.stop();
    })).rejects.toThrow(/noncontiguous_render_quantum/);
  });

  it('rejects a gap in the worklet block ledger', async () => {
    stopBlocks = [
      {
        sequence: 0,
        startContextFrame: 64,
        frameCount: 2,
        interleavedPcm16: Int16Array.of(1, 2, 3, 4),
      },
      {
        sequence: 1,
        startContextFrame: 67,
        frameCount: 1,
        interleavedPcm16: Int16Array.of(5, 6),
      },
    ];
    const { result } = renderHook(() => useRenderedDigitalCapture());
    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      await result.current.armSourceClock({
        sourceStartContextFrame: 8000,
        sourceFrameCount: 960000,
        sourceChunkFrames: 4800,
      });
    });

    await expect(act(async () => {
      await result.current.stop();
    })).rejects.toThrow(/gap or overlap/);
  });

  it('rejects recorder startup immediately when the component unmounts', async () => {
    recorder.connect.mockImplementation(() => undefined);
    const { result, unmount } = renderHook(
      () => useRenderedDigitalCapture(),
    );

    let startup!: Promise<unknown>;
    act(() => {
      startup = result.current.start();
    });
    await vi.waitFor(() => {
      expect(recorder.connect).toHaveBeenCalledTimes(1);
    });
    unmount();

    await expect(startup).rejects.toThrow(/cancelled by navigation/);
    expect(context.close).toHaveBeenCalledTimes(1);
  });

  it('keeps abort idempotent when AudioContext close rejects', async () => {
    context.close.mockRejectedValueOnce(new Error('browser close failed'));
    const { result } = renderHook(() => useRenderedDigitalCapture());

    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      await result.current.abort();
    });
    await act(async () => {
      await result.current.abort();
    });

    expect(context.close).toHaveBeenCalledTimes(1);
    expect(recorder.port.postMessage).toHaveBeenCalledWith({ type: 'abort' });
  });
});
