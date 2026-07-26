import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { TestDashboard } from '../components/TestDashboard';
import type { PlaybackMetrics } from '../hooks/useAudioPlayback';
import type {
  RenderedDigitalRecorderFatalDiagnostic,
} from '../hooks/useRenderedDigitalCapture';
import type { SessionStatus } from '../types/messages';

// --- Mocks ---

const mockSetMuted = vi.fn();
const mockGetPlaybackPosition = vi.fn(() => 0);
const mockPlaybackStart = vi.fn();
const mockPlaybackStop = vi.fn();
const mockQueueAudio = vi.fn();
const mockRenderedCaptureStart = vi.fn(async () => ({
  audioContext: {},
  recorderNode: {},
}));
const mockCreatePlaybackRouting = vi.fn((captureInputIndex: number) => ({
  audioContext: { sampleRate: 16000 },
  captureNode: {},
  captureInputIndex,
}));
const mockCaptureCurrentContextFrame = vi.fn(() => 16000);
const mockArmSourceClock = vi.fn();
const mockRenderedCaptureStop = vi.fn();
const mockRenderedCaptureAbort = vi.fn(async () => {});
let mockRenderedCaptureFatalError: Error | null = null;
let mockRenderedCaptureFatalDiagnostic:
RenderedDigitalRecorderFatalDiagnostic | null = null;
const mockTrackerStartTest = vi.fn();
const mockLogChunkSent = vi.fn();
const mockLogAudioReceived = vi.fn();
const mockLogAudioParentComplete = vi.fn();
const mockLogPlaybackClockSample = vi.fn();
const mockGetPlaybackMetrics = vi.fn<() => PlaybackMetrics>(() => ({
  queueDepthSeconds: 0,
  peakQueueDepthSeconds: 0,
  playbackRate: 1,
  playbackMode: 'normal',
  totalSourceDurationSeconds: 0,
  totalScheduledDurationSeconds: 0,
  aboveTarget: false,
  aboveLimit: false,
  limitExceededCount: 0,
}));

// Track which instances were created (input first, output second)
let playbackInstances: Array<{
  isMuted: boolean;
  setMuted: ReturnType<typeof vi.fn>;
  getPlaybackPosition: ReturnType<typeof vi.fn>;
  getPlaybackMetrics: ReturnType<typeof vi.fn>;
}>;
let playbackOptions: Array<{
  initialMuted?: boolean;
  adaptivePlayback?: boolean;
  onSchedule?: (event: unknown) => void;
  onClockSample?: (event: unknown) => void;
}>;

vi.mock('../hooks/useAudioPlayback', () => ({
  useAudioPlayback: vi.fn((opts?: {
    initialMuted?: boolean;
    adaptivePlayback?: boolean;
    onSchedule?: (event: unknown) => void;
    onClockSample?: (event: unknown) => void;
  }) => {
    const instance = {
      isPlaying: false,
      isMuted: opts?.initialMuted ?? false,
      queueAudio: mockQueueAudio,
      start: mockPlaybackStart,
      stop: mockPlaybackStop,
      setMuted: mockSetMuted,
      getPlaybackPosition: mockGetPlaybackPosition,
      getPlaybackMetrics: mockGetPlaybackMetrics,
    };
    playbackOptions.push(opts ?? {});
    playbackInstances.push(instance);
    return instance;
  }),
}));

vi.mock('../hooks/useRenderedDigitalCapture', () => ({
  useRenderedDigitalCapture: vi.fn(() => ({
    isCapturing: false,
    fatalError: mockRenderedCaptureFatalError,
    fatalDiagnostic: mockRenderedCaptureFatalDiagnostic,
    start: mockRenderedCaptureStart,
    createPlaybackRouting: mockCreatePlaybackRouting,
    getCurrentContextFrame: mockCaptureCurrentContextFrame,
    armSourceClock: mockArmSourceClock,
    stop: mockRenderedCaptureStop,
    abort: mockRenderedCaptureAbort,
  })),
}));

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(() => ({
    isConnected: false,
    status: 'disconnected',
    sendMessage: vi.fn(() => true),
    sendAudio: vi.fn(),
    connect: vi.fn(),
    disconnect: vi.fn(),
  })),
}));

vi.mock('../hooks/useFileAudioSource', () => ({
  useFileAudioSource: vi.fn(() => ({
    isLoaded: false,
    isStreaming: false,
    duration: 0,
    position: 0,
    loadFile: vi.fn(),
    startStreaming: vi.fn(),
    stopStreaming: vi.fn(),
  })),
}));

vi.mock('../hooks/useTimingTracker', () => ({
  useTimingTracker: vi.fn(() => ({
    startTest: mockTrackerStartTest,
    logChunkSent: mockLogChunkSent,
    logAudioReceived: mockLogAudioReceived,
    logAudioParentComplete: mockLogAudioParentComplete,
    logPlaybackScheduled: vi.fn(),
    logPlaybackClockSample: mockLogPlaybackClockSample,
    logPlaybackQueueSample: vi.fn(),
    logServerTerminal: vi.fn(),
    logInputEnded: vi.fn(),
    getEvents: vi.fn(() => []),
    getSendCount: vi.fn(() => 0),
    getReceiveCount: vi.fn(() => 0),
    getSourcePosition: vi.fn(() => 0),
    getCumulativeOutputDuration: vi.fn(() => 0),
  })),
}));

vi.mock('../hooks/useMetricsSocket', () => ({
  useMetricsSocket: vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn(),
    clearEvents: vi.fn(),
  })),
}));

vi.mock('../components/DriftChart', () => ({
  DriftChart: ({ data }: { data: unknown[] }) => (
    <div data-testid="drift-chart">Points: {data.length}</div>
  ),
}));

// Mock fetch. Keep the response shape broad enough to cover both the normal
// start acknowledgement and FastAPI error payloads.
type MockFetchResponse = {
  ok: boolean;
  status?: number;
  json: () => Promise<Record<string, unknown>>;
};
const defaultFetch = (input: RequestInfo | URL): Promise<MockFetchResponse> =>
  Promise.resolve(String(input) === '/api/config'
    ? {
        ok: true,
        json: () => Promise.resolve({
          audioMetadataProtocolVersions: [1],
        }),
      }
    : {
        ok: true,
        json: () => Promise.resolve({ status: 'started' }),
      });
const mockFetch = vi.fn<
  (input: RequestInfo | URL) => Promise<MockFetchResponse>
>(defaultFetch);
vi.stubGlobal('fetch', mockFetch);

describe('TestDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockImplementation(defaultFetch);
    mockRenderedCaptureFatalError = null;
    mockRenderedCaptureFatalDiagnostic = null;
    playbackInstances = [];
    playbackOptions = [];
  });

  // --- Existing tests ---

  it('renders the dashboard header', () => {
    render(<TestDashboard />);
    expect(screen.getByText('Latency Test Dashboard')).toBeInTheDocument();
  });

  it('renders file upload input', () => {
    render(<TestDashboard />);
    const fileInput = document.querySelector('input[type="file"]');
    expect(fileInput).toBeInTheDocument();
    expect(fileInput?.getAttribute('accept')).toBe('.wav,.mp3');
  });

  it('shows Start Test button when idle', () => {
    render(<TestDashboard />);
    const startBtn = screen.getByText('Start Test');
    expect(startBtn).toBeInTheDocument();
  });

  it('Start Test button is disabled when no file loaded', () => {
    render(<TestDashboard />);
    const startBtn = screen.getByText('Start Test');
    expect(startBtn).toBeDisabled();
  });

  it('renders Back to Translation link', () => {
    render(<TestDashboard />);
    const link = screen.getByText('Back to Translation');
    expect(link).toBeInTheDocument();
    expect(link.getAttribute('href')).toBe('#/');
  });

  it('does not show stats panel when idle', () => {
    render(<TestDashboard />);
    expect(screen.queryByText('Audience Playback Statistics')).not.toBeInTheDocument();
  });

  it('does not show drift chart when idle', () => {
    render(<TestDashboard />);
    expect(screen.queryByTestId('drift-chart')).not.toBeInTheDocument();
  });

  it('Start Test button enabled when file is loaded', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    const mockUseFileAudioSource = vi.mocked(useFileAudioSource);
    mockUseFileAudioSource.mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });

    render(<TestDashboard />);
    const startBtn = screen.getByText('Start Test');
    expect(startBtn).not.toBeDisabled();
  });

  it('does not start playback when the backend rejects a new evidence window', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });
    mockFetch.mockImplementation((input) =>
      String(input) === '/api/config'
        ? defaultFetch(input)
        : Promise.resolve({
            ok: false,
            status: 409,
            json: () => Promise.resolve({
              detail: 'Cannot start a new test while a staged stream is active',
            }),
          }));
    render(<TestDashboard />);

    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not start test: Cannot start a new test while a staged stream is active',
    );
    expect(screen.getByText('Start Test')).toBeInTheDocument();
    expect(mockPlaybackStart).not.toHaveBeenCalled();
    expect(mockTrackerStartTest).not.toHaveBeenCalled();
  });

  it('does not start capture when audio metadata v1 is not advertised', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });
    mockFetch.mockImplementation((input) =>
      Promise.resolve(String(input) === '/api/config'
        ? {
            ok: true,
            json: () => Promise.resolve({
              audioMetadataProtocolVersions: [],
            }),
          }
        : {
            ok: true,
            json: () => Promise.resolve({ status: 'started' }),
          }));

    render(<TestDashboard />);
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not start test: Audio metadata protocol version 1 is unavailable',
    );
    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(mockFetch).toHaveBeenCalledWith('/api/config');
    expect(mockPlaybackStart).not.toHaveBeenCalled();
    expect(mockTrackerStartTest).not.toHaveBeenCalled();
  });

  // --- Audio playback integration tests ---

  it('creates two useAudioPlayback instances, both with initialMuted=true', async () => {
    const { useAudioPlayback } = await import('../hooks/useAudioPlayback');
    const mockHook = vi.mocked(useAudioPlayback);

    render(<TestDashboard />);

    // useAudioPlayback is called on every render; check at least 2 calls
    // with the correct options (input and output both start muted)
    const calls = mockHook.mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(2);

    // First call = inputPlayback, second = outputPlayback
    expect(calls[0][0]).toEqual({
      sampleRate: 16000,
      initialMuted: true,
      adaptivePlayback: false,
      minimumScheduleLeadSeconds: 0,
      quantizeScheduleToSampleFrames: false,
    });
    expect(calls[1][0]).toEqual(expect.objectContaining({
      sampleRate: 16000,
      initialMuted: true,
      adaptivePlayback: true,
      onSchedule: expect.any(Function),
    }));
  });

  it('routes output playback clock samples into the timing tracker', () => {
    render(<TestDashboard />);
    const clockEvent = { clockSampleSequence: 7 };

    expect(playbackOptions).toHaveLength(2);
    expect(playbackOptions[0].onClockSample).toBeUndefined();
    expect(playbackOptions[1].onClockSample).toBeTypeOf('function');

    playbackOptions[1].onClockSample?.(clockEvent);
    expect(mockLogPlaybackClockSample).toHaveBeenCalledWith(clockEvent);
  });

  it('keeps raw rendered-digital capture default-off', () => {
    render(<TestDashboard />);

    const checkbox = screen.getByRole('checkbox', {
      name: /rendered-digital common-clock preflight/i,
    });
    expect(checkbox).not.toBeChecked();
    expect(mockRenderedCaptureStart).not.toHaveBeenCalled();
    expect(screen.getByText(/keep all exported audio untracked/i))
      .toBeInTheDocument();
  });

  it('starts both playback paths on one capture-owned context when opted in', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });
    render(<TestDashboard />);
    fireEvent.click(screen.getByRole('checkbox', {
      name: /rendered-digital common-clock preflight/i,
    }));

    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    expect(mockRenderedCaptureStart).toHaveBeenCalledTimes(1);
    expect(mockCreatePlaybackRouting.mock.calls.map(([index]) => index))
      .toEqual([0, 1]);
    expect(mockPlaybackStart).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ captureInputIndex: 0 }),
    );
    expect(mockPlaybackStart).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ captureInputIndex: 1 }),
    );
  });

  it('opts only the test transport into audio metadata v1 and records observations', async () => {
    const { useWebSocket } = await import('../hooks/useWebSocket');
    render(<TestDashboard />);
    const options = vi.mocked(useWebSocket).mock.calls.at(-1)?.[0];
    expect(options).toEqual(expect.objectContaining({
      audioMetadataProtocolVersion: 1,
      onAudio: expect.any(Function),
      onAudioParentComplete: expect.any(Function),
    }));

    const audio = new ArrayBuffer(4);
    const frameObservation = {
      metadata: {
        type: 'audio_frame' as const,
        protocolVersion: 1 as const,
        streamGeneration: 1,
        parentSequenceId: 0,
        audioFrameId: 0,
        audioBytes: 4,
        sampleRateHz: 16000,
        channels: 1,
        bytesPerSample: 2,
        sourceStartMs: null,
        sourceEndMs: 300,
      },
      binaryReceivedAtMs: 1000,
    };
    act(() => options?.onAudio?.(audio, frameObservation));
    expect(mockLogAudioReceived).toHaveBeenCalledWith(
      audio.byteLength,
      frameObservation,
    );
    expect(mockQueueAudio).toHaveBeenCalledWith(audio, frameObservation);

    const completionObservation = {
      metadata: {
        type: 'audio_parent_complete' as const,
        protocolVersion: 1 as const,
        streamGeneration: 1,
        parentSequenceId: 0,
        audioFrameCount: 1,
        audioBytes: 4,
        sourceStartMs: null,
        sourceEndMs: 300,
      },
      receivedAtMs: 1001,
    };
    act(() => options?.onAudioParentComplete?.(completionObservation));
    expect(mockLogAudioParentComplete).toHaveBeenCalledWith(
      completionObservation,
    );
  });

  it('can select a fixed 1.00x control before a run starts', async () => {
    const { useAudioPlayback } = await import('../hooks/useAudioPlayback');
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });
    render(<TestDashboard />);
    const checkbox = screen.getByRole('checkbox', {
      name: /Adaptive Spanish playback/,
    });

    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();

    const outputCalls = vi.mocked(useAudioPlayback).mock.calls.filter(
      ([options]) => options?.onSchedule !== undefined,
    );
    expect(outputCalls.at(-1)?.[0]).toEqual(expect.objectContaining({
      adaptivePlayback: false,
    }));

    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    expect(mockTrackerStartTest).toHaveBeenCalledWith({
      adaptivePlaybackEnabled: false,
    });
  });

  it('does not show audio toggle buttons when idle', () => {
    render(<TestDashboard />);
    expect(screen.queryByText(/Input Audio/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Output Audio/)).not.toBeInTheDocument();
  });

  it('shows audio toggle buttons during running phase', async () => {
    // Set up file as loaded so we can start
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });

    render(<TestDashboard />);

    // Click Start Test
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    expect(screen.getByText(/Input Audio/)).toBeInTheDocument();
    expect(screen.getByText(/Output Audio/)).toBeInTheDocument();
  });

  it('clicking audio toggle buttons calls setMuted', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });

    render(<TestDashboard />);

    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    // Click input audio toggle (currently muted, so should unmute -> setMuted(false))
    mockSetMuted.mockClear();
    fireEvent.click(screen.getByText(/Input Audio/));
    expect(mockSetMuted).toHaveBeenCalledWith(false);

    // Click output audio toggle
    mockSetMuted.mockClear();
    fireEvent.click(screen.getByText(/Output Audio/));
    expect(mockSetMuted).toHaveBeenCalledWith(false);
  });

  it('starts both playback instances when test starts', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });

    render(<TestDashboard />);

    mockPlaybackStart.mockClear();

    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    // Both input and output playback should have start() called
    expect(mockPlaybackStart).toHaveBeenCalledTimes(2);
  });

  it('shows Stop Test button and stats during running phase', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 0,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });

    render(<TestDashboard />);

    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    expect(screen.getByText('Stop Test')).toBeInTheDocument();
    expect(screen.getByText('Audience Playback Statistics')).toBeInTheDocument();
    expect(screen.getByText('Current Queue')).toBeInTheDocument();
    expect(screen.getByText('Playback Rate')).toBeInTheDocument();
    expect(screen.getByTestId('drift-chart')).toBeInTheDocument();
  });

  it('sends end_input when the source file completes naturally', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    const { useWebSocket } = await import('../hooks/useWebSocket');
    let completeSource: (() => void) | undefined;
    const sendMessage = vi.fn(() => true);

    vi.mocked(useFileAudioSource).mockImplementation((options) => {
      completeSource = options.onComplete;
      return {
        isLoaded: true,
        isStreaming: false,
        duration: 60,
        position: 0,
        loadFile: vi.fn(),
        startStreaming: vi.fn(),
        stopStreaming: vi.fn(),
      };
    });
    vi.mocked(useWebSocket).mockReturnValue({
      isConnected: false,
      status: 'disconnected',
      sendMessage,
      sendAudio: vi.fn(),
      connect: vi.fn(),
      disconnect: vi.fn(),
    });

    render(<TestDashboard />);
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    act(() => completeSource?.());

    expect(sendMessage).toHaveBeenCalledWith({ type: 'end_input' });
    expect(screen.getByText(/File input ended/)).toBeInTheDocument();
  });

  it('requires server completion and an empty playback queue before finishing naturally', async () => {
    vi.useFakeTimers();
    try {
      const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
      const { useWebSocket } = await import('../hooks/useWebSocket');
      let completeSource: (() => void) | undefined;
      let notifyStatus: ((status: SessionStatus, message: string) => void) | undefined;
      const sendMessage = vi.fn(() => true);
      const disconnect = vi.fn();

      vi.mocked(useFileAudioSource).mockImplementation((options) => {
        completeSource = options.onComplete;
        return {
          isLoaded: true,
          isStreaming: false,
          duration: 60,
          position: 60,
          loadFile: vi.fn(),
          startStreaming: vi.fn(),
          stopStreaming: vi.fn(),
        };
      });
      vi.mocked(useWebSocket).mockImplementation((options) => {
        notifyStatus = options.onStatus;
        return {
          isConnected: false,
          status: 'disconnected',
          sendMessage,
          sendAudio: vi.fn(),
          connect: vi.fn(),
          disconnect,
        };
      });
      mockGetPlaybackMetrics.mockReturnValue({
        queueDepthSeconds: 12,
        peakQueueDepthSeconds: 12,
        playbackRate: 1.1,
        playbackMode: 'over-limit',
        totalSourceDurationSeconds: 60,
        totalScheduledDurationSeconds: 55,
        aboveTarget: true,
        aboveLimit: true,
        limitExceededCount: 1,
      });

      render(<TestDashboard />);
      await act(async () => {
        fireEvent.click(screen.getByText('Start Test'));
      });
      act(() => completeSource?.());

      await act(async () => {
        await vi.advanceTimersByTimeAsync(11_000);
      });
      expect(disconnect).not.toHaveBeenCalled();
      expect(sendMessage).not.toHaveBeenCalledWith({ type: 'stop_stream' });

      mockGetPlaybackMetrics.mockReturnValue({
        queueDepthSeconds: 0.05,
        peakQueueDepthSeconds: 12,
        playbackRate: 1.1,
        playbackMode: 'urgent',
        totalSourceDurationSeconds: 60,
        totalScheduledDurationSeconds: 55,
        aboveTarget: false,
        aboveLimit: false,
        limitExceededCount: 1,
      });
      act(() => notifyStatus?.('completed', 'Riva output complete'));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(disconnect).not.toHaveBeenCalled();
      expect(sendMessage).not.toHaveBeenCalledWith({ type: 'stop_stream' });

      mockGetPlaybackMetrics.mockReturnValue({
        queueDepthSeconds: 0,
        peakQueueDepthSeconds: 12,
        playbackRate: 1.1,
        playbackMode: 'normal',
        totalSourceDurationSeconds: 60,
        totalScheduledDurationSeconds: 55,
        aboveTarget: false,
        aboveLimit: false,
        limitExceededCount: 1,
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(sendMessage).toHaveBeenCalledWith({ type: 'stop_stream' });
      expect(disconnect).toHaveBeenCalledTimes(1);
      expect(screen.getByText('Export CSV')).toBeInTheDocument();
    } finally {
      const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
      const { useWebSocket } = await import('../hooks/useWebSocket');
      vi.mocked(useFileAudioSource).mockReturnValue({
        isLoaded: false,
        isStreaming: false,
        duration: 0,
        position: 0,
        loadFile: vi.fn(),
        startStreaming: vi.fn(),
        stopStreaming: vi.fn(),
      });
      vi.mocked(useWebSocket).mockReturnValue({
        isConnected: false,
        status: 'disconnected',
        sendMessage: vi.fn(() => true),
        sendAudio: vi.fn(),
        connect: vi.fn(),
        disconnect: vi.fn(),
      });
      vi.useRealTimers();
    }
  });

  it('marks a server error as failed instead of completed', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    const { useWebSocket } = await import('../hooks/useWebSocket');
    let notifyError: ((message: string) => void) | undefined;
    const stopStreaming = vi.fn();
    const disconnect = vi.fn();

    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 10,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming,
    });
    vi.mocked(useWebSocket).mockImplementation((options) => {
      notifyError = options.onError;
      return {
        isConnected: false,
        status: 'disconnected',
        sendMessage: vi.fn(() => true),
        sendAudio: vi.fn(),
        connect: vi.fn(),
        disconnect,
      };
    });

    render(<TestDashboard />);
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    await act(async () => {
      notifyError?.('Staged TTS failed');
      await Promise.resolve();
    });

    expect(stopStreaming).toHaveBeenCalledTimes(1);
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Test failed: Staged TTS failed',
    );
    expect(screen.getByText('Export CSV')).toBeInTheDocument();
    expect(screen.getByText('New Test')).toBeInTheDocument();
  });

  it('fails immediately and exposes safe diagnostics on recorder fatal', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    const { useWebSocket } = await import('../hooks/useWebSocket');
    let emitChunk: Parameters<typeof useFileAudioSource>[0]['onChunk']
      | undefined;
    const stopStreaming = vi.fn();
    const disconnect = vi.fn();

    vi.mocked(useFileAudioSource).mockImplementation((options) => {
      emitChunk = options.onChunk;
      return {
        isLoaded: true,
        isStreaming: false,
        duration: 60,
        position: 0,
        loadFile: vi.fn(),
        startStreaming: vi.fn(),
        stopStreaming,
      };
    });
    vi.mocked(useWebSocket).mockReturnValue({
      isConnected: false,
      status: 'connected',
      sendMessage: vi.fn(() => true),
      sendAudio: vi.fn(() => true),
      connect: vi.fn(),
      disconnect,
    });

    const { container, rerender } = render(<TestDashboard />);
    fireEvent.click(screen.getByRole('checkbox', {
      name: /rendered-digital common-clock preflight/i,
    }));
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    await act(async () => {
      emitChunk?.(new ArrayBuffer(4), {
        chunkIndex: 0,
        sampleRateHz: 16000,
        sourceSampleStart: 0,
        sourceSampleEndExclusive: 2,
        inputPcmSha256: 'a'.repeat(64),
        inputPcmSampleCount: 2,
        emittedAtMs: 300,
        inputSampleZeroClientMs: 0,
        inputSourceBoundaryContextFrame: 14400,
        inputSourceBoundaryDeliveredAfterContextFrame: 14464,
        inputSourceBoundaryReceivedContextFrameBefore: 14480,
        inputSourceBoundaryReceivedContextFrameAfter: 14480,
        inputSourceBoundaryReceivedClientMs: 1200,
        inputChunkEmittedContextFrame: 14500,
      });
    });

    const fatalError = new Error(
      'Recorder rejected the capture (noncontiguous_render_quantum).',
    );
    mockRenderedCaptureFatalError = fatalError;
    mockRenderedCaptureFatalDiagnostic = {
      code: 'noncontiguous_render_quantum',
      expectedContextFrame: 16128,
      observedContextFrame: 16384,
      deltaFrames: 256,
    };
    mockRenderedCaptureStop.mockRejectedValueOnce(fatalError);
    await act(async () => {
      rerender(<TestDashboard />);
      await Promise.resolve();
      await Promise.resolve();
    });

    const root = container.querySelector('[data-s2s-phase]');
    expect(stopStreaming).toHaveBeenCalledTimes(1);
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(mockRenderedCaptureStop).toHaveBeenCalledTimes(1);
    expect(mockRenderedCaptureAbort).toHaveBeenCalledTimes(1);
    expect(root).toHaveAttribute('data-s2s-phase', 'failed');
    expect(root).toHaveAttribute('data-s2s-source-chunks-sent', '1');
    expect(root).toHaveAttribute('data-s2s-server-terminal-state', 'error');
    expect(root).toHaveAttribute(
      'data-s2s-recorder-fatal-code',
      'noncontiguous_render_quantum',
    );
    expect(root).toHaveAttribute(
      'data-s2s-recorder-gap-expected-context-frame',
      '16128',
    );
    expect(root).toHaveAttribute(
      'data-s2s-recorder-gap-observed-context-frame',
      '16384',
    );
    expect(root).toHaveAttribute(
      'data-s2s-recorder-gap-delta-frames',
      '256',
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Test failed: Recorder rejected the capture '
      + '(noncontiguous_render_quantum).',
    );

    fireEvent.click(screen.getByText('New Test'));
    expect(root).toHaveAttribute('data-s2s-phase', 'idle');
    expect(root).toHaveAttribute('data-s2s-source-chunks-sent', '0');
    expect(root).toHaveAttribute(
      'data-s2s-server-terminal-state',
      'pending',
    );
    expect(root).not.toHaveAttribute('data-s2s-recorder-fatal-code');
    expect(root).not.toHaveAttribute(
      'data-s2s-recorder-gap-expected-context-frame',
    );
    expect(root).not.toHaveAttribute(
      'data-s2s-recorder-gap-observed-context-frame',
    );
    expect(root).not.toHaveAttribute(
      'data-s2s-recorder-gap-delta-frames',
    );

    fireEvent.click(screen.getByRole('checkbox', {
      name: /rendered-digital common-clock preflight/i,
    }));
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    expect(root).toHaveAttribute('data-s2s-phase', 'running');
    expect(root).toHaveAttribute('data-s2s-source-chunks-sent', '0');
    expect(root).toHaveAttribute(
      'data-s2s-server-terminal-state',
      'pending',
    );
    expect(root).not.toHaveAttribute('data-s2s-recorder-fatal-code');
    expect(mockRenderedCaptureStart).toHaveBeenCalledTimes(1);
  });

  it('does not advance the timing ledger when an audio send is rejected', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    const { useWebSocket } = await import('../hooks/useWebSocket');
    let emitChunk: Parameters<typeof useFileAudioSource>[0]['onChunk']
      | undefined;
    const stopStreaming = vi.fn();
    const disconnect = vi.fn();

    vi.mocked(useFileAudioSource).mockImplementation((options) => {
      emitChunk = options.onChunk;
      return {
        isLoaded: true,
        isStreaming: false,
        duration: 60,
        position: 0,
        loadFile: vi.fn(),
        startStreaming: vi.fn(),
        stopStreaming,
      };
    });
    vi.mocked(useWebSocket).mockReturnValue({
      isConnected: false,
      status: 'connected',
      sendMessage: vi.fn(() => true),
      sendAudio: vi.fn(() => false),
      connect: vi.fn(),
      disconnect,
    });

    render(<TestDashboard />);
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    await act(async () => {
      emitChunk?.(new ArrayBuffer(4), {
        chunkIndex: 0,
        sampleRateHz: 16000,
        sourceSampleStart: 0,
        sourceSampleEndExclusive: 2,
        inputPcmSha256: 'a'.repeat(64),
        inputPcmSampleCount: 2,
        emittedAtMs: 300,
        inputSampleZeroClientMs: 0,
      });
      await Promise.resolve();
    });

    expect(mockLogChunkSent).not.toHaveBeenCalled();
    expect(mockQueueAudio).not.toHaveBeenCalled();
    expect(stopStreaming).toHaveBeenCalledTimes(1);
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Test failed: Audio capture stopped because a chunk could not be sent.',
    );
  });

  it('records the formal send frame only after WebSocket handoff succeeds', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    const { useWebSocket } = await import('../hooks/useWebSocket');
    let emitChunk: Parameters<typeof useFileAudioSource>[0]['onChunk']
      | undefined;
    const sendAudio = vi.fn(() => true);

    vi.mocked(useFileAudioSource).mockImplementation((options) => {
      emitChunk = options.onChunk;
      return {
        isLoaded: true,
        isStreaming: false,
        duration: 60,
        position: 0,
        loadFile: vi.fn(),
        startStreaming: vi.fn(),
        stopStreaming: vi.fn(),
      };
    });
    vi.mocked(useWebSocket).mockReturnValue({
      isConnected: false,
      status: 'connected',
      sendMessage: vi.fn(() => true),
      sendAudio,
      connect: vi.fn(),
      disconnect: vi.fn(),
    });

    render(<TestDashboard />);
    fireEvent.click(screen.getByRole('checkbox', {
      name: /rendered-digital common-clock preflight/i,
    }));
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });

    const now = vi.spyOn(performance, 'now').mockReturnValue(1234);
    await act(async () => {
      emitChunk?.(new ArrayBuffer(9600), {
        chunkIndex: 0,
        sampleRateHz: 16000,
        sourceSampleStart: 0,
        sourceSampleEndExclusive: 4800,
        inputPcmSha256: 'a'.repeat(64),
        inputPcmSampleCount: 960000,
        emittedAtMs: 300,
        inputSampleZeroClientMs: 0,
        inputSourceBoundaryContextFrame: 14400,
        inputSourceBoundaryDeliveredAfterContextFrame: 14464,
        inputSourceBoundaryReceivedContextFrameBefore: 14480,
        inputSourceBoundaryReceivedContextFrameAfter: 14480,
        inputSourceBoundaryReceivedClientMs: 1200,
        inputChunkEmittedContextFrame: 14500,
      });
      await Promise.resolve();
    });
    now.mockRestore();

    expect(sendAudio).toHaveBeenCalledTimes(1);
    expect(mockCaptureCurrentContextFrame).toHaveBeenCalledTimes(1);
    expect(
      mockCaptureCurrentContextFrame.mock.invocationCallOrder[0],
    ).toBeGreaterThan(sendAudio.mock.invocationCallOrder[0]);
    expect(mockLogChunkSent).toHaveBeenCalledWith(
      9600,
      expect.objectContaining({
        emittedAtMs: 1234,
        inputChunkEmittedContextFrame: 16000,
      }),
    );
  });

  it('finishes cleanup once when audio and stop-control sends both fail', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(
      () => undefined,
    );
    try {
      const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
      const { useWebSocket } = await import('../hooks/useWebSocket');
      let emitChunk: Parameters<typeof useFileAudioSource>[0]['onChunk']
        | undefined;
      let notifyError: ((message: string) => void) | undefined;
      const stopStreaming = vi.fn();
      const disconnect = vi.fn();
      const sendMessage = vi.fn(() => {
        throw new Error('same socket rejected control');
      });
      const sendAudio = vi.fn(() => {
        notifyError?.('Audio chunk send failed: transport rejected frame');
        return false;
      });

      vi.mocked(useFileAudioSource).mockImplementation((options) => {
        emitChunk = options.onChunk;
        return {
          isLoaded: true,
          isStreaming: false,
          duration: 60,
          position: 0,
          loadFile: vi.fn(),
          startStreaming: vi.fn(),
          stopStreaming,
        };
      });
      vi.mocked(useWebSocket).mockImplementation((options) => {
        notifyError = options.onError;
        return {
          isConnected: false,
          status: 'connected',
          sendMessage,
          sendAudio,
          connect: vi.fn(),
          disconnect,
        };
      });

      render(<TestDashboard />);
      await act(async () => {
        fireEvent.click(screen.getByText('Start Test'));
      });
      const observation = {
        chunkIndex: 0,
        sampleRateHz: 16000,
        sourceSampleStart: 0,
        sourceSampleEndExclusive: 2,
        inputPcmSha256: 'a'.repeat(64),
        inputPcmSampleCount: 2,
        emittedAtMs: 300,
        inputSampleZeroClientMs: 0,
      };
      await act(async () => {
        emitChunk?.(new ArrayBuffer(4), observation);
        // Model one already-queued callback arriving after stopStreaming.
        emitChunk?.(new ArrayBuffer(4), observation);
        await Promise.resolve();
      });

      expect(sendAudio).toHaveBeenCalledTimes(2);
      expect(sendMessage).toHaveBeenCalledTimes(1);
      expect(sendMessage).toHaveBeenCalledWith({ type: 'stop_stream' });
      expect(stopStreaming).toHaveBeenCalledTimes(1);
      expect(disconnect).toHaveBeenCalledTimes(1);
      expect(mockPlaybackStop).toHaveBeenCalledTimes(2);
      expect(mockLogChunkSent).not.toHaveBeenCalled();
      expect(
        mockFetch.mock.calls.filter(
          ([input]) => String(input) === '/api/test/stop',
        ),
      ).toHaveLength(1);
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Test failed: Audio chunk send failed: transport rejected frame',
      );
      expect(consoleError).toHaveBeenCalledWith(
        expect.stringMatching(/stop_stream control failed during cleanup/),
        expect.any(Error),
      );
    } finally {
      consoleError.mockRestore();
    }
  });

  it('cancels delayed file input when metadata negotiation fails', async () => {
    vi.useFakeTimers();
    try {
      const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
      const { useWebSocket } = await import('../hooks/useWebSocket');
      let notifyError: ((message: string) => void) | undefined;
      const startStreaming = vi.fn();
      const stopStreaming = vi.fn();

      vi.mocked(useFileAudioSource).mockReturnValue({
        isLoaded: true,
        isStreaming: false,
        duration: 60,
        position: 0,
        loadFile: vi.fn(),
        startStreaming,
        stopStreaming,
      });
      vi.mocked(useWebSocket).mockImplementation((options) => {
        notifyError = options.onError;
        return {
          isConnected: true,
          status: 'connected',
          sendMessage: vi.fn(() => true),
          sendAudio: vi.fn(),
          connect: vi.fn(),
          disconnect: vi.fn(),
        };
      });

      render(<TestDashboard />);
      await act(async () => {
        fireEvent.click(screen.getByText('Start Test'));
      });

      await act(async () => {
        notifyError?.('Audio metadata protocol is unavailable');
        await Promise.resolve();
        await vi.advanceTimersByTimeAsync(500);
      });

      expect(stopStreaming).toHaveBeenCalledTimes(1);
      expect(startStreaming).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Test failed: Audio metadata protocol is unavailable',
      );
    } finally {
      const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
      const { useWebSocket } = await import('../hooks/useWebSocket');
      vi.mocked(useFileAudioSource).mockReturnValue({
        isLoaded: false,
        isStreaming: false,
        duration: 0,
        position: 0,
        loadFile: vi.fn(),
        startStreaming: vi.fn(),
        stopStreaming: vi.fn(),
      });
      vi.mocked(useWebSocket).mockReturnValue({
        isConnected: false,
        status: 'disconnected',
        sendMessage: vi.fn(() => true),
        sendAudio: vi.fn(),
        connect: vi.fn(),
        disconnect: vi.fn(),
      });
      vi.useRealTimers();
    }
  });

  it('marks a missing server completion as failed at the drain timeout', async () => {
    vi.useFakeTimers();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const consoleLog = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    try {
      const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
      let completeSource: (() => void) | undefined;

      vi.mocked(useFileAudioSource).mockImplementation((options) => {
        completeSource = options.onComplete;
        return {
          isLoaded: true,
          isStreaming: false,
          duration: 60,
          position: 60,
          loadFile: vi.fn(),
          startStreaming: vi.fn(),
          stopStreaming: vi.fn(),
        };
      });
      mockGetPlaybackMetrics.mockReturnValue({
        queueDepthSeconds: 0,
        peakQueueDepthSeconds: 0,
        playbackRate: 1,
        playbackMode: 'normal',
        totalSourceDurationSeconds: 60,
        totalScheduledDurationSeconds: 60,
        aboveTarget: false,
        aboveLimit: false,
        limitExceededCount: 0,
      });

      render(<TestDashboard />);
      await act(async () => {
        fireEvent.click(screen.getByText('Start Test'));
      });
      act(() => completeSource?.());
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300_000);
      });

      expect(screen.getByRole('alert')).toHaveTextContent(
        'Test failed: Timed out waiting for Riva to confirm translation completion.',
      );
      expect(screen.getByText('Export CSV')).toBeInTheDocument();
    } finally {
      consoleLog.mockRestore();
      consoleError.mockRestore();
      vi.useRealTimers();
    }
  });

  it('keeps manual stop independent of the server completion signal', async () => {
    const { useFileAudioSource } = await import('../hooks/useFileAudioSource');
    vi.mocked(useFileAudioSource).mockReturnValue({
      isLoaded: true,
      isStreaming: false,
      duration: 60,
      position: 10,
      loadFile: vi.fn(),
      startStreaming: vi.fn(),
      stopStreaming: vi.fn(),
    });

    render(<TestDashboard />);
    await act(async () => {
      fireEvent.click(screen.getByText('Start Test'));
    });
    await act(async () => {
      fireEvent.click(screen.getByText('Stop Test'));
    });

    expect(screen.getByText('Export CSV')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
