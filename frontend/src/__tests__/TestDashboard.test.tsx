import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { TestDashboard } from '../components/TestDashboard';

// Mock all hooks so TestDashboard renders in isolation
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
  });

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
});
