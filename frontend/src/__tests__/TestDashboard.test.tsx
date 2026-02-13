import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { TestDashboard } from '../components/TestDashboard';

// --- Mocks ---

const mockSetMuted = vi.fn();
const mockGetPlaybackPosition = vi.fn(() => 0);
const mockPlaybackStart = vi.fn();
const mockPlaybackStop = vi.fn();
const mockQueueAudio = vi.fn();

// Track which instances were created (input first, output second)
let playbackInstances: Array<{
  isMuted: boolean;
  setMuted: ReturnType<typeof vi.fn>;
  getPlaybackPosition: ReturnType<typeof vi.fn>;
}>;

vi.mock('../hooks/useAudioPlayback', () => ({
  useAudioPlayback: vi.fn((opts?: { initialMuted?: boolean }) => {
    const instance = {
      isPlaying: false,
      isMuted: opts?.initialMuted ?? false,
      queueAudio: mockQueueAudio,
      start: mockPlaybackStart,
      stop: mockPlaybackStop,
      setMuted: mockSetMuted,
      getPlaybackPosition: mockGetPlaybackPosition,
    };
    playbackInstances.push(instance);
    return instance;
  }),
}));

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(() => ({
    isConnected: false,
    status: 'disconnected',
    sendMessage: vi.fn(),
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
    startTest: vi.fn(),
    logChunkSent: vi.fn(),
    logAudioReceived: vi.fn(),
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

// Mock fetch
const mockFetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ status: 'started' }),
  }),
);
vi.stubGlobal('fetch', mockFetch);

describe('TestDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    playbackInstances = [];
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
    expect(screen.queryByText('Statistics')).not.toBeInTheDocument();
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
    expect(calls[0][0]).toEqual({ sampleRate: 16000, initialMuted: true });
    expect(calls[1][0]).toEqual({ sampleRate: 16000, initialMuted: true });
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
    expect(screen.getByText('Statistics')).toBeInTheDocument();
    expect(screen.getByTestId('drift-chart')).toBeInTheDocument();
  });
});
