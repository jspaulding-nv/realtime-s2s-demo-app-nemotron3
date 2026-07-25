import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PlaybackMetrics } from '../hooks/useAudioPlayback';

const playback = vi.hoisted(() => ({
  getMetrics: vi.fn(),
  queueAudio: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
}));

const capture = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
}));

const socket = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  connect: vi.fn(),
  disconnect: vi.fn(),
}));

vi.mock('../hooks/useAudioPlayback', () => ({
  useAudioPlayback: vi.fn(() => ({
    isPlaying: false,
    isMuted: false,
    queueAudio: playback.queueAudio,
    start: playback.start,
    stop: playback.stop,
    setMuted: vi.fn(),
    getPlaybackPosition: vi.fn(() => 0),
    getPlaybackMetrics: playback.getMetrics,
  })),
}));

vi.mock('../hooks/useAudioCapture', () => ({
  useAudioCapture: vi.fn(() => ({
    startCapture: capture.start,
    stopCapture: capture.stop,
    audioLevel: 0,
  })),
}));

vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: vi.fn(() => ({
    isConnected: true,
    status: 'connected',
    sendMessage: socket.sendMessage,
    sendAudio: vi.fn(),
    connect: socket.connect,
    disconnect: socket.disconnect,
  })),
}));

import { TranslationPanel } from '../components/TranslationPanel';

const emptyMetrics: PlaybackMetrics = {
  queueDepthSeconds: 0,
  peakQueueDepthSeconds: 0,
  playbackRate: 1,
  playbackMode: 'normal',
  totalSourceDurationSeconds: 0,
  totalScheduledDurationSeconds: 0,
  aboveTarget: false,
  aboveLimit: false,
  limitExceededCount: 0,
};

describe('TranslationPanel playback telemetry', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    playback.getMetrics.mockReturnValue(emptyMetrics);
    capture.start.mockResolvedValue(undefined);
    // Language discovery is unrelated to these telemetry assertions. Leave
    // the request pending so it cannot schedule a post-render state update.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => undefined)));
  });

  it('keeps telemetry hidden until a playback session starts', () => {
    render(<TranslationPanel />);

    expect(
      screen.queryByLabelText('Spanish playback telemetry'),
    ).not.toBeInTheDocument();
  });

  it('keeps the live translation transport on the legacy audio protocol', async () => {
    const { useWebSocket } = await import('../hooks/useWebSocket');
    render(<TranslationPanel />);

    const options = vi.mocked(useWebSocket).mock.calls.at(-1)?.[0];
    expect(options?.audioMetadataProtocolVersion).toBeUndefined();
  });

  it('reports current and peak browser queue metrics during translation', async () => {
    vi.useFakeTimers();
    try {
      render(<TranslationPanel />);
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Start translation' }));
      });

      playback.getMetrics.mockReturnValue({
        ...emptyMetrics,
        queueDepthSeconds: 12.25,
        peakQueueDepthSeconds: 14.5,
        playbackRate: 1.1,
        playbackMode: 'over-limit',
        aboveTarget: true,
        aboveLimit: true,
        limitExceededCount: 2,
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });

      const telemetry = screen.getByLabelText('Spanish playback telemetry');
      expect(telemetry).toHaveAttribute('data-session-active', 'true');
      expect(telemetry).toHaveAttribute('data-queue-current-seconds', '12.25');
      expect(telemetry).toHaveAttribute('data-queue-peak-seconds', '14.5');
      expect(telemetry).toHaveAttribute('data-playback-rate', '1.1');
      expect(telemetry).toHaveAttribute('data-playback-mode', 'over-limit');
      expect(telemetry).toHaveAttribute('data-limit-breaches', '2');
      expect(screen.getByText('12.25s')).toBeInTheDocument();
      expect(screen.getByText('14.50s')).toBeInTheDocument();
      expect(screen.getByText('1.10x (over-limit)')).toBeInTheDocument();
      expect(
        screen.getByText(/live queue is above the audience limit/i),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('retains an exact browser queue snapshot when the user stops', async () => {
    const finalMetrics: PlaybackMetrics = {
      ...emptyMetrics,
      queueDepthSeconds: 6.75,
      peakQueueDepthSeconds: 11.25,
      playbackRate: 1.05,
      playbackMode: 'catch-up',
      aboveTarget: true,
      limitExceededCount: 1,
    };
    playback.getMetrics.mockReturnValue(finalMetrics);
    render(<TranslationPanel />);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Start translation' }));
    });
    fireEvent.click(screen.getByRole('button', { name: 'Stop translation' }));

    const telemetry = screen.getByLabelText('Spanish playback telemetry');
    expect(telemetry).toHaveAttribute('data-session-active', 'false');
    expect(telemetry).toHaveAttribute('data-queue-current-seconds', '6.75');
    expect(telemetry).toHaveAttribute('data-queue-peak-seconds', '11.25');
    expect(screen.getByText('Final stop snapshot')).toBeInTheDocument();
    expect(screen.getByText('Queue at stop')).toBeInTheDocument();
    expect(screen.getByText('6.75s')).toBeInTheDocument();
    expect(playback.stop).toHaveBeenCalledTimes(1);
    expect(socket.sendMessage).toHaveBeenLastCalledWith({ type: 'stop_stream' });
  });
});
