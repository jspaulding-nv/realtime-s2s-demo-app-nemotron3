import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AudioFrameMetadata,
  AudioParentCompleteMetadata,
} from '../types/audioMetadata';
import { useWebSocket } from '../hooks/useWebSocket';

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];

  readonly url: string;
  binaryType: BinaryType = 'blob';
  readyState = MockWebSocket.CONNECTING;
  sent: Array<string | ArrayBufferLike | Blob | ArrayBufferView> = [];
  sendError: unknown = null;
  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    MockWebSocket.instances.push(this);
  }

  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (this.sendError !== null) {
      throw this.sendError;
    }
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === MockWebSocket.CLOSED) {
      return;
    }
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.(new CloseEvent('close', {
      code: 1000,
      reason: 'test close',
    }));
  }

  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  receive(data: string | ArrayBuffer): void {
    this.onmessage?.(new MessageEvent('message', { data }));
  }
}

function frame(
  overrides: Partial<AudioFrameMetadata> = {},
): AudioFrameMetadata {
  return {
    type: 'audio_frame',
    protocolVersion: 1,
    streamGeneration: 1,
    parentSequenceId: 0,
    audioFrameId: 0,
    audioBytes: 4,
    sampleRateHz: 16000,
    channels: 1,
    bytesPerSample: 2,
    sourceStartMs: null,
    sourceEndMs: 500,
    ...overrides,
  };
}

function completion(
  overrides: Partial<AudioParentCompleteMetadata> = {},
): AudioParentCompleteMetadata {
  return {
    type: 'audio_parent_complete',
    protocolVersion: 1,
    streamGeneration: 1,
    parentSequenceId: 0,
    audioFrameCount: 1,
    audioBytes: 4,
    sourceStartMs: null,
    sourceEndMs: 500,
    ...overrides,
  };
}

function connect(
  result: { current: ReturnType<typeof useWebSocket> },
): MockWebSocket {
  act(() => result.current.connect());
  const socket = MockWebSocket.instances.at(-1);
  if (!socket) {
    throw new Error('expected useWebSocket to create a WebSocket');
  }
  act(() => socket.open());
  return socket;
}

function startStream(
  result: { current: ReturnType<typeof useWebSocket> },
): void {
  act(() => result.current.sendMessage({
    type: 'start_stream',
    targetLanguage: 'es-US',
  }));
}

describe('useWebSocket audio metadata protocol', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket);
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('keeps legacy raw-binary delivery unchanged when not opted in', () => {
    const onAudio = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      onAudio,
    }));
    const socket = connect(result);
    const audio = new ArrayBuffer(4);

    act(() => socket.receive(audio));

    expect(onAudio).toHaveBeenCalledOnce();
    expect(onAudio).toHaveBeenCalledWith(audio);
    unmount();
  });

  it('reports whether an audio frame was accepted by an open socket', () => {
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      onError,
    }));
    const audio = new ArrayBuffer(4);
    let accepted = true;

    act(() => {
      accepted = result.current.sendAudio(audio);
    });

    expect(accepted).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/WebSocket is not open/),
    );
    expect(result.current.status).toBe('error');

    const socket = connect(result);
    onError.mockClear();
    act(() => {
      accepted = result.current.sendAudio(audio);
    });

    expect(accepted).toBe(true);
    expect(socket.sent).toContain(audio);
    expect(onError).not.toHaveBeenCalled();
    unmount();
  });

  it('returns false and reports a synchronous WebSocket send failure', () => {
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      onError,
    }));
    const socket = connect(result);
    socket.sendError = new Error('transport rejected frame');
    let accepted = true;

    act(() => {
      accepted = result.current.sendAudio(new ArrayBuffer(4));
    });

    expect(accepted).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      'Audio chunk send failed: transport rejected frame',
    );
    expect(result.current.status).toBe('error');
    unmount();
  });

  it('returns false instead of throwing when a control send fails', () => {
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      onError,
    }));
    const socket = connect(result);
    socket.sendError = new Error('transport rejected control');
    let accepted = true;

    expect(() => {
      act(() => {
        accepted = result.current.sendMessage({ type: 'stop_stream' });
      });
    }).not.toThrow();

    expect(accepted).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      'WebSocket stop_stream send failed: transport rejected control',
    );
    expect(result.current.status).toBe('error');
    unmount();
  });

  it('fails closed when a legacy stream receives unexpected metadata', () => {
    const onAudio = vi.fn();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      onAudio,
      onError,
    }));
    const socket = connect(result);

    act(() => socket.receive(JSON.stringify(frame())));
    act(() => socket.receive(new ArrayBuffer(4)));
    act(() => socket.receive(JSON.stringify({
      type: 'status',
      status: 'processing',
      message: 'must remain failed',
    })));

    expect(onAudio).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/metadata during a legacy stream/),
    );
    expect(result.current.status).toBe('error');
    unmount();
  });

  it('negotiates v1, pairs a header with PCM, and observes completion', () => {
    const onAudio = vi.fn();
    const onAudioParentComplete = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onAudio,
      onAudioParentComplete,
    }));
    const socket = connect(result);
    startStream(result);
    vi.spyOn(performance, 'now')
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(125)
      .mockReturnValueOnce(140);

    expect(JSON.parse(socket.sent[0] as string)).toEqual({
      type: 'start_stream',
      targetLanguage: 'es-US',
      audioMetadataProtocolVersion: 1,
    });

    const metadata = frame();
    const audio = new ArrayBuffer(metadata.audioBytes);
    act(() => socket.receive(JSON.stringify(metadata)));
    expect(onAudio).not.toHaveBeenCalled();
    act(() => socket.receive(audio));

    expect(onAudio).toHaveBeenCalledWith(audio, {
      metadata,
      binaryReceivedAtMs: 125,
    });

    const parentComplete = completion();
    act(() => socket.receive(JSON.stringify(parentComplete)));
    expect(onAudioParentComplete).toHaveBeenCalledWith({
      metadata: parentComplete,
      receivedAtMs: 140,
    });

    act(() => socket.receive(JSON.stringify({
      type: 'status',
      status: 'completed',
      message: 'complete',
    })));
    unmount();
  });

  it('fails closed once when binary arrives without a header', () => {
    const onAudio = vi.fn();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onAudio,
      onError,
    }));
    const socket = connect(result);
    startStream(result);

    act(() => socket.receive(new ArrayBuffer(4)));
    act(() => socket.receive(JSON.stringify(frame())));
    act(() => socket.receive(new ArrayBuffer(4)));

    expect(onAudio).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/exactly one preceding audio_frame header/),
    );
    expect(result.current.status).toBe('error');
    unmount();
  });

  it('rejects text interleaving between a frame header and its PCM', () => {
    const onAudio = vi.fn();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onAudio,
      onError,
    }));
    const socket = connect(result);
    startStream(result);

    act(() => socket.receive(JSON.stringify(frame())));
    act(() => socket.receive(JSON.stringify({ type: 'pong' })));
    act(() => socket.receive(new ArrayBuffer(4)));

    expect(onAudio).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/followed immediately by its binary PCM/),
    );
    unmount();
  });

  it('latches closed after malformed JSON instead of accepting later PCM', () => {
    const onAudio = vi.fn();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onAudio,
      onError,
    }));
    const socket = connect(result);
    startStream(result);

    act(() => socket.receive('{'));
    act(() => socket.receive(JSON.stringify(frame())));
    act(() => socket.receive(new ArrayBuffer(4)));

    expect(onAudio).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
    expect(result.current.status).toBe('error');
    unmount();
  });

  it('reports a dangling parent on terminal status and does not emit completion', () => {
    const onAudio = vi.fn();
    const onAudioParentComplete = vi.fn();
    const onError = vi.fn();
    const onStatus = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onAudio,
      onAudioParentComplete,
      onError,
      onStatus,
    }));
    const socket = connect(result);
    startStream(result);

    act(() => socket.receive(JSON.stringify(frame())));
    act(() => socket.receive(new ArrayBuffer(4)));
    act(() => socket.receive(JSON.stringify({
      type: 'status',
      status: 'completed',
      message: 'complete',
    })));

    expect(onAudio).toHaveBeenCalledOnce();
    expect(onAudioParentComplete).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/without audio_parent_complete/),
    );
    expect(onStatus).not.toHaveBeenCalledWith(
      'completed',
      expect.any(String),
    );
    expect(result.current.status).toBe('error');
    unmount();
  });

  it('clears dangling state on restart and requires a higher generation', () => {
    const onAudio = vi.fn();
    const onError = vi.fn();
    const { result, unmount } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onAudio,
      onError,
    }));
    const socket = connect(result);
    startStream(result);
    act(() => socket.receive(JSON.stringify(frame())));

    startStream(result);
    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/stream restart left an audio_frame header/),
    );
    expect(socket.sent).toHaveLength(1);

    startStream(result);
    const nextFrame = frame({ streamGeneration: 2 });
    const audio = new ArrayBuffer(nextFrame.audioBytes);
    act(() => socket.receive(JSON.stringify(nextFrame)));
    act(() => socket.receive(audio));
    act(() => socket.receive(JSON.stringify(completion({
      streamGeneration: 2,
    }))));

    expect(onAudio).toHaveBeenCalledWith(
      audio,
      expect.objectContaining({ metadata: nextFrame }),
    );
    expect(socket.sent).toHaveLength(2);
    unmount();
  });

  it('reports and clears a dangling parent on disconnect', () => {
    const onError = vi.fn();
    const { result } = renderHook(() => useWebSocket({
      url: 'ws://example.test/ws/translate',
      audioMetadataProtocolVersion: 1,
      onError,
    }));
    const socket = connect(result);
    startStream(result);
    act(() => socket.receive(JSON.stringify(frame())));
    act(() => socket.receive(new ArrayBuffer(4)));

    act(() => result.current.disconnect());

    expect(onError).toHaveBeenCalledWith(
      expect.stringMatching(/WebSocket disconnect left parentSequenceId 0/),
    );
    expect(result.current.isConnected).toBe(false);
  });
});
